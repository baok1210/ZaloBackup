#!/usr/bin/env python3
"""zalo_merge.py — merge duplicate Zalo conversation exports into one master file.

Why: the same conversation backed up from different accounts has different UIDs
(Zalo assigns per-viewer IDs), so the same chat can exist as several export files.
This tool detects those duplicates and merges them into one deduplicated master.

Matching logic (cross-account safe):
  * Message IDs differ between accounts' views -> cannot key on msgId.
  * The same physical message lands on both sides within ~1-2 s and has the same
    content. Key = (msgType, |Δt| <= 2000 ms, normalized text equal).
  * Media rows (photo/video): text contains URLs that differ per viewer, so
    match on (msgType, |Δt| <= 2000 ms) alone.
  * When both copies match, the RAWer copy wins (raw JSON row > friendly row);
    every row keeps `sources: [files it came from]`.

Usage:
  python zalo_merge.py file1.json file2.csv [...] -o master.json [--md master.md]
  python zalo_merge.py --scan DIR                 # list duplicate groups
  python zalo_merge.py --auto DIR                 # merge every duplicate group in DIR
"""
import csv
import io
import json
import os
import re
import sys
from datetime import datetime

MEDIA_TYPES = {"2", "18"}          # photo / video
UIDLIKE = re.compile(r"^(g?\d{6,})$")  # trailing _<uid> token in export filenames

WINDOW_TEXT = 2000    # ms — same text from the other view lands ~1 s apart
WINDOW_OTHER = 10000  # ms — non-text rows have no comparable content; be generous
MASTER_PREFIX = "master_"


# ------------------------------------------------------------------ loading
def _ts_of(row):
    for k in ("serverTime", "sendDttm"):
        v = row.get(k)
        if v:
            try:
                return int(str(v))
            except ValueError:
                pass
    if row.get("time"):  # friendly CSV/JSON
        try:
            return int(datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
        except ValueError:
            pass
    return 0


def _type_of(row):
    t = row.get("msgType")
    return str(t) if t is not None else "?"


def _text_of(row):
    v = row.get("message")
    if isinstance(v, str):
        return v
    if v is None:
        if isinstance(row.get("text"), str):
            return row["text"]
        return ""
    t = _type_of(row)
    if t == "1":                          # rich-text: real text lives in title
        if isinstance(v, dict) and v.get("action") == "rtf" and v.get("title"):
            return str(v["title"])
    if t in ("3", "5", "17", "52", "6"):
        try:
            pv = v.get("params") if isinstance(v, dict) else None
            pd = json.loads(pv) if isinstance(pv, str) and pv.startswith("{") else {}
            return pretty_media_text(t, params=pd if isinstance(pd, dict) else {}, payload=v)
        except Exception:
            pass
    if t in ("7", "4"):
        return "[Sticker]"          # sticker payload dict — don't dump raw JSON
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


def _is_raw(row):
    """Raw store exports carry msgId + serverTime; friendly rows don't."""
    return bool(row.get("msgId")) and bool(row.get("serverTime") or row.get("sendDttm"))


def load_source(path):
    """Load one export file (raw JSON, friendly JSON, or friendly CSV) -> list[dict]."""
    p = path.lower()
    rows = []
    if p.endswith(".csv"):
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                rows.append({"time": row.get("time"), "sender": row.get("sender"),
                             "msgType": row.get("msgType"), "message": row.get("text"),
                             "text": row.get("text")})
    else:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            rows = data.get("messages") or []
        elif isinstance(data, list):
            rows = data
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "msgId": str(r.get("msgId") or ""),
            "cliMsgId": str(r.get("cliMsgId") or ""),
            "ts": _ts_of(r),
            "type": _type_of(r),
            "sender": r.get("sender") or r.get("dName") or "",
            "text": _text_of(r),
            "raw": r if _is_raw(r) else None,
            "src": os.path.basename(path),
        })
    return out


def _norm_text(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


# ------------------------------------------------------------------ merging
def merge_sources(list_of_rows, progress=None):
    """list_of_rows: list of per-file row lists. Returns deduped, time-sorted rows.

    Two rows are the SAME physical message when they come from DIFFERENT source
    files (each single export is already unique by msgId) AND:
      * text rows: same type, equal normalized text, |Δt| <= 2 s; or
      * media rows: same type, |Δt| <= 10 s (URLs differ per viewing account;
        album photos seconds apart in one file are distinct messages).
    When several kept rows qualify, the CLOSEST in time wins."""
    allrows = []
    for rows in list_of_rows:
        allrows.extend(rows)
    # Same-device copies within ONE file: the store returns a sent/synced message
    # twice (PC src=10 + phone-sync src=7) sharing cliMsgId with different msgIds.
    # Merge those first so cross-file matching never sees them as two messages.
    by_cli = {}
    for i, r in enumerate(allrows):
        if r.get("cliMsgId"):
            by_cli.setdefault(r["cliMsgId"], []).append(i)
    drop = set()
    for idxs in by_cli.values():
        if len(idxs) < 2:
            continue
        keep = idxs[0]
        for j in idxs[1:]:
            a, b = allrows[keep], allrows[j]
            b_better = (bool(b["raw"]) and not bool(a["raw"])) or \
                       (bool(b["raw"]) == bool(a["raw"]) and b["ts"] >= a["ts"])
            if b_better:
                drop.add(keep)
                keep = j
            else:
                drop.add(j)
    if drop:
        allrows = [r for i, r in enumerate(allrows) if i not in drop]
    allrows.sort(key=lambda r: (r["ts"], r["type"], _norm_text(r["text"])))

    WINDOW_TEXT = 2000    # ms — same text from the other account lands ~1 s apart
    WINDOW_OTHER = 10000  # ms — non-text rows have no comparable content; be generous
    out = []
    for r in allrows:
        folded = False
        # collect kept rows inside the widest window, pick the CLOSEST match
        cands = []
        for k in reversed(out):
            if k["ts"] and r["ts"]:
                if r["ts"] > k["ts"] + WINDOW_OTHER:
                    break          # k is newer than the widest window; older even more so
                if abs(k["ts"] - r["ts"]) > (WINDOW_TEXT if k["type"] == "1"
                                              else WINDOW_OTHER):
                    continue
            else:
                continue           # no timestamps -> never fold (can't prove same msg)
            if k["type"] != r["type"]:
                continue
            if r["src"] in k["sources"]:
                continue           # this file already contributed => distinct message
            if k["type"] == "1" and _norm_text(k["text"]) != _norm_text(r["text"]):
                continue           # plain text must match; other types can't be compared
            cands.append((abs(k["ts"] - r["ts"]), k))
        if cands:
            k = min(cands, key=lambda c: c[0])[1]
            folded = True
        if folded:
            k["count"] += 1
            k["sources"].add(r["src"])
            if r["raw"] and not k["raw"]:  # upgrade friendly -> raw when possible
                k["raw"], k["sender"], k["text"] = r["raw"], r["sender"], r["text"]
        else:
            r = dict(r)
            r["count"] = 1
            r["sources"] = {r["src"]}
            out.append(r)
        if progress:
            progress(len(out), len(allrows))
    for r in out:
        r["sources"] = sorted(r["sources"])
    return out


def _fmt_call_dur(sec):
    try:
        sec = int(sec or 0)
    except Exception:
        sec = 0
    if sec <= 0:
        return "0 giây"
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    parts = []
    if h: parts.append(f"{h} giờ")
    if m: parts.append(f"{m} phút")
    if s or not parts: parts.append(f"{s} giây")
    return " ".join(parts)


def pretty_media_text(mt, txt=None, params=None, payload=None):
    """Human-readable one-liner for photo/video/sticker/voice/doodle/location payloads.
    Accepts the raw JSON text (txt), an already-parsed params dict, and/or the full
    message payload dict (needed for type 17 desc)."""
    t = str(mt or "")
    jouter = {}
    if params is None and not txt:
        params = {}                      # payload-only call (e.g. type 17 desc)
    if params is None:
        if not txt or not str(txt).lstrip().startswith("{"):
            return txt
        try:
            j = json.loads(txt)
            if not isinstance(j, dict):
                return txt
            jouter = j
            params = json.loads(j.get("params") or "{}")
            if not isinstance(params, dict):
                params = {}
        except Exception:
            return txt
    p = params or {}
    pl = payload if isinstance(payload, dict) else {}
    if t in ("7", "4"):
        return "[Sticker]"
    if t == "3":
        try:
            dur = int(p.get("duration") or 0)
        except Exception:
            dur = 0
        ds = f" {round(dur / 1000)}s" if dur else ""
        return f"[🎙️ Tin nhắn thoại{ds}]"
    if t == "5":
        w, h = p.get("width"), p.get("height")
        dim = f" {w}×{h}" if w and h else ""
        return f"[🖼️ Doodle{dim}]"
    if t == "17":
        desc = str(pl.get("desc") or jouter.get("desc") or "").strip() or "Vị trí"
        return f"📍 {desc}"
    if t == "52":
        cm = (p.get("customMsg") or {}).get("msg") or {}
        label = str(cm.get("vi") or cm.get("en") or "Web").strip()
        return f"🧩 {label}"
    if t == "6":
        pl2 = pl or jouter
        act = str(pl2.get("action") or "")
        if act == "recommened.misscall":
            return "[📹 Cuộc gọi video nhỡ]" if str(p.get("calltype")) == "1" \
                else "[📞 Cuộc gọi nhỡ]"
        if act == "recommened.calltime":
            vid = str(p.get("calltype")) == "1"
            icon = "📹 Cuộc gọi video" if vid else "📞 Cuộc gọi thoại"
            return f"[{icon} {_fmt_call_dur(p.get('duration'))}]"
        if act == "recommened.user":
            who = str(pl2.get("title") or "").strip() or "Danh thiếp"
            return f"[👤 {who}]"
        # recommened.link and anything else: [Link] <title>
        ttl = str(pl2.get("title") or "").strip()
        return f"[Link] {ttl}" if ttl else (txt or "[Link]")
    if t == "2":
        w, h = p.get("width"), p.get("height")
        dim = f" {w}×{h}" if w and h else ""
        return f"[Ảnh{dim}]"
    if t == "18":
        try:
            dur = int(p.get("duration") or 0)
        except Exception:
            dur = 0
        ds = f" {dur // 1000}s" if dur else ""
        try:
            fs = int(p.get("fileSize") or 0)
            sz = (f" · {fs / 1048576:.1f} MB" if fs >= 1048576
                  else (f" · {fs / 1024:.0f} KB" if fs else ""))
        except Exception:
            sz = ""
        grp = ""
        try:
            tot = int(p.get("total_item_in_group") or 0)
            if tot > 1:
                grp = f" (album {int(p.get('id_in_group') or 0) + 1}/{tot})"
        except Exception:
            pass
        return f"[Video{ds}{sz}{grp}]"
    return txt


def _friendly_row(r, peer_name=""):
    ts = r["ts"]
    try:
        t = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        t = ""
    txt = r["text"]
    if r["type"] in ("2", "18", "7", "4", "3", "5", "17", "52"):
        txt = pretty_media_text(r["type"], txt)
    return {"time": t, "sender": r["sender"] or ("Tôi" if r["raw"] and
            str(r["raw"].get("fromUid")) == "0" else peer_name or "?"),
            "msgType": r["type"], "text": txt,
            "copies": r["count"], "sources": r["sources"]}


def build_master(rows, meta):
    """Friendly master JSON — readable, with provenance per row."""
    out = dict(meta)
    out["count"] = len(rows)
    out["messages"] = [_friendly_row(r, meta.get("conversation", "")) for r in rows]
    return json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")


def build_master_md(rows, meta):
    buf = io.StringIO()
    buf.write(f"# Zalo master (merged) — {meta.get('conversation', '?')}\n\n")
    buf.write(f"*{len(rows)} tin sau khi gộp & khử trùng lặp — nguồn: "
              f"{', '.join(meta.get('sources', []))}*\n\n")
    cur = ""
    for r in rows:
        fr = _friendly_row(r, meta.get("conversation", ""))
        day = fr["time"][:10]
        if day != cur:
            cur = day
            buf.write(f"\n## {day[8:10]}/{day[5:7]}/{day[:4]}\n\n")
        buf.write(f"**{fr['time'][11:]} — {fr['sender']}**: "
                  f"{fr['text'].replace(chr(10), '  ')}  \n")
    return buf.getvalue().encode("utf-8")


# ------------------------------------------------------- duplicate detection
def scan_dir(dirs):
    """Group exports by conversation name (filename minus _<uid>.<ext>).
    `dirs` may be one path or a list of paths (e.g. backup root + exports).
    Returns [{name, files:[...]}] for groups with >= 2 message-export files."""
    if isinstance(dirs, str):
        dirs = [dirs]
    groups = {}
    for d in dirs:
        for f in sorted(os.listdir(d)):
            low = f.lower()
            if f.startswith("media_") or f.startswith("merged_") or \
                    f.startswith(MASTER_PREFIX) or \
                    not low.endswith((".json", ".csv", ".md", ".txt")):
                continue
            stem = os.path.splitext(f)[0]
            parts = stem.rsplit("_", 1)
            name = parts[0] if len(parts) == 2 and UIDLIKE.match(parts[1]) else stem
            groups.setdefault(name, []).append(os.path.join(d, f))
    return [{"name": n, "files": fs} for n, fs in sorted(groups.items()) if len(fs) >= 2]


def merge_group(group, out_dir, fmts=("json", "md"), progress=None):
    """Merge one scan_dir group -> master files. Returns (out_paths, n_msgs, n_src_files)."""
    srcs, lists = [], []
    for i, p in enumerate(group["files"]):
        if p.lower().endswith((".md", ".txt")):
            continue  # chat-render formats carry no reliable per-row structure
        rows = load_source(p)
        if rows:
            srcs.append(os.path.basename(p))
            lists.append(rows)
        if progress:
            progress(i + 1, len(group["files"]))
    if not lists:
        return [], 0, 0
    merged = merge_sources(lists, progress)
    safe = "".join(ch for ch in group["name"] if ch not in '\\/:*?"<>|').strip()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    meta = {"conversation": group["name"], "mergedAt":
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "sources": srcs}
    outs = []
    for fmt in fmts:
        path = os.path.join(out_dir, f"merged_{safe}_{stamp}.{fmt}")
        data = build_master(merged, meta) if fmt == "json" else build_master_md(merged, meta)
        with open(path, "wb") as f:
            f.write(data)
        outs.append(path)
    return outs, len(merged), len(srcs)


# ------------------------------------------------------- snapshot → master
# Auto-crawl model: crawl #1 (mùng 1) and crawl #2 (mùng 6) are snapshots of the
# same conversation. The LATER crawl is the superset of the earlier one, so the
# master accumulates: new messages are added, known messages just get their
# lastSeen bumped. A message a *complete* crawl should have covered but didn't
# return is marked missingSince (most likely recalled/deleted server-side) —
# the master keeps the content anyway, so nothing ever leaves the archive.


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def empty_master(name, uid=""):
    return {"conversation": name, "uid": uid, "createdAt": _now_str(),
            "updatedAt": _now_str(), "snapshots": [], "count": 0, "messages": []}


def _heal_device_copies(msgs):
    """Collapse in-file device copies inside an EXISTING master (masters created
    before cliMsgId dedup kept both the PC copy and the phone-sync copy of the
    same message). Matching, in order of confidence:
      * rows whose raw (or top-level) cliMsgId matches — exact;
      * cli-less rows joined into a cli-group with equal sender+text within 2 s;
      * cli-less pairs (legacy friendly masters): equal sender+text inside the
        same wall-clock second.
    The LATER row wins (that is the copy Zalo displays); msgId/cliMsgId/raw are
    merged onto the kept row. Runs before every fold, so old masters self-heal
    on the next crawl/merge. Returns number of dropped rows."""
    def cli_of(m):
        return str((m.get("raw") or {}).get("cliMsgId") or m.get("cliMsgId") or "")

    def key2(m):
        return (str(m.get("sender") or ""), _norm_text(m.get("text")))

    groups, loose = {}, {}
    for m in msgs:
        c = cli_of(m)
        if c:
            groups.setdefault("c" + c, []).append(m)
        else:
            loose.setdefault(key2(m), []).append(m)

    # cli-groups indexed by (sender, text) so legacy cli-less copies can join
    gindex = {}
    for g in groups.values():
        gindex.setdefault(key2(g[0]), []).append(g)

    sec_groups, rest = {}, []
    for rows in loose.values():
        for m in rows:
            ts = m.get("_ts")
            joined = False
            if ts:
                for g in gindex.get(key2(m), ()):
                    gts = g[0].get("_ts")
                    if gts and abs(int(gts) - int(ts)) <= 2000:
                        g.append(m)
                        joined = True
                        break
            if not joined:
                if ts:
                    sec_groups.setdefault((key2(m)[0], key2(m)[1],
                                           int(ts) // 1000), []).append(m)
                else:
                    rest.append(m)

    drop = set()
    for members in list(groups.values()) + list(sec_groups.values()):
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (str(m.get("time") or ""), str(m.get("msgId") or "")))
        keep = members[-1]                      # later copy = what Zalo displays
        for m in members[:-1]:
            drop.add(id(m))
            if keep.get("raw") is None and m.get("raw") is not None:
                keep["raw"] = m["raw"]
                keep["sender"] = m.get("sender") or keep.get("sender")
            if not keep.get("msgId") and m.get("msgId"):
                keep["msgId"] = m["msgId"]
            if not keep.get("cliMsgId") and m.get("cliMsgId"):
                keep["cliMsgId"] = m["cliMsgId"]
    if drop:
        msgs[:] = [m for m in msgs if id(m) not in drop]
    return len(drop)


def _fmt_ts(ts):
    try:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def file_crawled_at(path):
    """When was this snapshot taken? Priority: exportedAt field (JSON) >
    timestamp encoded in the filename (media_..._YYYYMMDD_HHMMSS.zip) > mtime."""
    try:
        if path.lower().endswith(".json"):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and d.get("exportedAt"):
                return str(d["exportedAt"])
    except Exception:
        pass
    m = re.search(r"_(\d{8})_(\d{6})\.zip$", path, re.I)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")\
                .strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d %H:%M:%S")


def fold_snapshot(master, rows, crawled_at=None, complete=True):
    """Fold one crawl snapshot into the master. rows = load_source() output.
    Matching: msgId exact hit first (new exports carry msgId); otherwise fuzzy —
    same type + Δt within window (+ equal text for type 1), nearest wins, and a
    matched master row is consumed (multiplicity of repeated 'ok' rows kept).
    A row never folds into another row added by the SAME snapshot (each export is
    unique by msgId; friendly rows have no ids, so same-source folding would be
    self-collision — album photos seconds apart must stay distinct).
    Returns (n_new, n_updated, n_marked_missing)."""
    crawled_at = crawled_at or _now_str()
    msgs = master.setdefault("messages", [])
    _heal_device_copies(msgs)                 # collapse pre-fix duplicates
    for m in msgs:                            # self-heal legacy JSON-dump payloads
        t = str(m.get("msgType"))
        raw = m.get("raw") if isinstance(m.get("raw"), dict) else None
        txt0 = str(m.get("text") or "")
        if t == "6" and txt0.startswith("[Link] ") and isinstance(raw, dict):
            act = str((raw.get("message") or {}).get("action") or "") \
                if isinstance(raw.get("message"), dict) else ""
            if act in ("recommened.calltime", "recommened.misscall", "recommened.user"):
                m["text"] = _text_of(raw)    # call logs / contact cards, not links
            continue
        if t in ("1", "3", "4", "5", "7", "17", "52") and txt0.lstrip().startswith("{"):
            if raw is not None:
                m["text"] = _text_of(raw)   # voice/doodle/location/sticker/webcontent
            else:
                m["text"] = "[Sticker]"          # legacy friendly row: stickers only
    by_id = {m["msgId"]: m for m in msgs if m.get("msgId")}
    by_cli = {m["cliMsgId"]: m for m in msgs if m.get("cliMsgId")}
    # per-type ts-sorted index over ALL master rows (any row can fuzzy-match a
    # msgId-less snapshot row — e.g. the same message seen from another account).
    # Kept incrementally sorted (bisect.insort) + a parallel ts array so folding
    # a 30k-row snapshot stays O(n log n), not O(n²).  # perf: was re-sort+rebuild per row
    fuzzy, ts_arr = {}, {}
    for m in msgs:
        fuzzy.setdefault(m["msgType"], []).append([m["_ts"], m])
    for t, v in fuzzy.items():
        v.sort(key=lambda p: p[0])
        ts_arr[t] = [p[0] for p in v]

    import bisect
    n_new = n_upd = 0
    max_ts = 0
    for r in sorted(rows, key=lambda x: x["ts"]):
        max_ts = max(max_ts, r["ts"])
        m = by_id.get(r["msgId"]) if r["msgId"] else None
        if m is None and r.get("cliMsgId"):
            m = by_cli.get(r["cliMsgId"])     # device copies share cliMsgId
        if m is None:
            win = WINDOW_TEXT if r["type"] == "1" else WINDOW_OTHER
            lst = fuzzy.setdefault(r["type"], [])
            ts_list = ts_arr.setdefault(r["type"], [])
            i = bisect.bisect_left(ts_list, r["ts"] - win)
            best = None
            while i < len(lst) and lst[i][0] <= r["ts"] + win:
                cand = lst[i][1]
                if cand.get("firstSeen") == crawled_at:
                    i += 1
                    continue  # same snapshot already contributed this row
                if r["type"] == "1" and _norm_text(cand["text"]) != _norm_text(r["text"]):
                    i += 1
                    continue
                d = abs(lst[i][0] - r["ts"])
                if best is None or d < best[0]:
                    best = (d, cand)
                i += 1
            m = best[1] if best else None
        if m is None:  # brand new — first time this message was ever seen
            m = {"msgId": r["msgId"], "cliMsgId": r.get("cliMsgId") or "",
                 "msgType": r["type"], "_ts": r["ts"],
                 "time": _fmt_ts(r["ts"]), "sender": r["sender"] or "",
                 "text": r["text"], "raw": r["raw"],
                 "firstSeen": crawled_at, "lastSeen": crawled_at, "missingSince": None}
            msgs.append(m)
            n_new += 1
            if m["msgId"]:
                by_id[m["msgId"]] = m
            if m["cliMsgId"]:
                by_cli[m["cliMsgId"]] = m
            nl = fuzzy.setdefault(m["msgType"], [])
            nt = ts_arr.setdefault(m["msgType"], [])
            j = bisect.bisect_left(nt, m["_ts"])
            nl.insert(j, [m["_ts"], m]); nt.insert(j, m["_ts"])
        else:
            n_upd += 1
            m["lastSeen"] = crawled_at
            m["missingSince"] = None          # seen again -> not missing
            if r["msgId"] and not m.get("msgId"):
                m["msgId"] = r["msgId"]       # backfill id onto a legacy row
                by_id[m["msgId"]] = m
            if r.get("cliMsgId") and not m.get("cliMsgId"):
                m["cliMsgId"] = r["cliMsgId"]  # backfill cliMsgId onto a legacy row
                by_cli[m["cliMsgId"]] = m
            if r["raw"] and not m.get("raw"):  # upgrade to the rawer copy
                m["raw"], m["sender"] = r["raw"], r["sender"] or m["sender"]

    n_miss = 0
    if complete:  # only a same-account full crawl can prove absence
        for m in msgs:
            if m["_ts"] and m["_ts"] <= max_ts and \
                    m["lastSeen"] != crawled_at and not m.get("missingSince"):
                m["missingSince"] = crawled_at  # likely recalled — content kept
                n_miss += 1

    msgs.sort(key=lambda m: m["_ts"])
    master["snapshots"].append({"crawledAt": crawled_at, "count": len(rows),
                                "new": n_new, "missingMarked": n_miss})
    master["updatedAt"] = _now_str()
    master["count"] = len(msgs)
    return n_new, n_upd, n_miss


def master_path_for(out_dir, name, uid):
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    return os.path.join(out_dir, f"{MASTER_PREFIX}{safe}_{uid}.json")


def _backfill_senders(master):
    """Fill sender on master rows exported from RAW store dumps (Zalo's raw rows
    often carry no dName, so the viewer showed '?'). Evidence used, in order:
    the row's own dName, then a fromUid->name map built from rows that DO have
    dName, then the master's own account name for fromUid=0 rows. Ambiguous
    uids (several different names) are skipped. Runs on every save."""
    msgs = master.get("messages") or []
    name_map, ambiguous = {}, set()
    for m in msgs:
        raw = m.get("raw") or {}
        fu, dn = str(raw.get("fromUid") or ""), str(raw.get("dName") or "")
        if fu and dn:
            if name_map.get(fu, dn) != dn:
                ambiguous.add(fu)
            name_map[fu] = dn
    for fu in ambiguous:
        name_map.pop(fu, None)
    muid = str(master.get("uid") or "")
    fixed = 0
    for m in msgs:
        if str(m.get("sender") or "").strip():
            continue
        raw = m.get("raw") or {}
        fu = str(raw.get("fromUid") or "")
        name = str(raw.get("dName") or "") or name_map.get(fu) \
            or (name_map.get(muid) if fu in ("0", muid) and muid in name_map else "")
        if name:
            m["sender"] = name
            fixed += 1
    return fixed


def save_master(master, out_dir):
    """Write master_<name>_<uid>.json + .md. Returns [paths]."""
    _backfill_senders(master)
    path = master_path_for(out_dir, master["conversation"], master.get("uid") or "x")
    with open(path, "wb") as f:
        f.write(json.dumps(master, ensure_ascii=False, indent=1).encode("utf-8"))
    md = os.path.splitext(path)[0] + ".md"
    with open(md, "wb") as f:
        f.write(build_master_md_folded(master))
    return [path, md]


def build_master_md_folded(master):
    buf = io.StringIO()
    n_miss = sum(1 for m in master["messages"] if m.get("missingSince"))
    buf.write(f"# Master (tích lũy) — {master['conversation']}\n\n")
    buf.write(f"*{master['count']} tin — cập nhật {master['updatedAt']} — "
              f"{len(master['snapshots'])} lần crawl — {n_miss} tin nghi bị thu hồi (vẫn giữ nội dung)*\n\n")
    cur = ""
    for m in master["messages"]:
        day = (m["time"] or "")[:10]
        if day and day != cur:
            cur = day
            buf.write(f"\n## {day[8:10]}/{day[5:7]}/{day[:4]}\n\n")
        flag = " ⚠️_đã_bị_xóa" if m.get("missingSince") else ""
        txt = m["text"] or ""
        if str(m.get("msgType")) in ("2", "18"):
            txt = pretty_media_text(m.get("msgType"), txt)
        buf.write(f"**{(m['time'] or '')[11:]} — {m['sender'] or '?'}**: "
                  f"{txt.replace(chr(10), '  ')}{flag}  \n")
    return buf.getvalue().encode("utf-8")


def fold_files(master_path, snap_paths, out_dir):
    """CLI helper: load-or-create master, fold snapshots (oldest first), save.
    Returns (master_dict, per-snapshot stats)."""
    if master_path and os.path.exists(master_path):
        with open(master_path, encoding="utf-8") as f:
            master = json.load(f)
    else:
        name = os.path.basename(snap_paths[0]).rsplit("_", 1)[0]
        master = empty_master(name, os.path.basename(snap_paths[0]).rsplit("_", 1)[-1].split(".")[0])
    stats = []
    for p in sorted(snap_paths, key=file_crawled_at):
        rows = load_source(p)
        n_new, n_upd, n_miss = fold_snapshot(master, rows, file_crawled_at(p), complete=True)
        stats.append((os.path.basename(p), len(rows), n_new, n_upd, n_miss))
    for p in save_master(master, out_dir):
        pass
    return master, stats


# ------------------------------------------------------- media master (auto-crawl)
# Same accumulation model as messages, but for downloaded photos/videos.
# Crawl #1 downloads a media ZIP; crawl #2 downloads another one; media_master
# keeps ONE copy of every distinct file ever seen (dedupe by content hash),
# so re-downloads across crawls are free and nothing is ever lost.


def media_master_dir(out_dir, name, uid):
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip()
    return os.path.join(out_dir, f"media_master_{safe}_{uid}")


def _media_index_path(mdir):
    return os.path.join(mdir, "index.json")


def load_media_master(out_dir, name, uid):
    mdir = media_master_dir(out_dir, name, uid)
    os.makedirs(mdir, exist_ok=True)
    idxp = _media_index_path(mdir)
    if os.path.exists(idxp):
        try:
            with open(idxp, encoding="utf-8") as f:
                return mdir, json.load(f)
        except Exception:
            pass
    return mdir, {"conversation": name, "uid": uid, "createdAt": _now_str(),
                  "updatedAt": _now_str(), "files": {}, "snapshots": []}


def save_media_master(mdir, idx):
    idx["updatedAt"] = _now_str()
    idx["count"] = len(idx["files"])
    with open(_media_index_path(mdir), "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)


def fold_media_zips(zips, out_dir, name, uid, progress=None, hash_chunk=1 << 20):
    """Fold downloaded media ZIPs (any crawl) into the per-conversation media master.
    Dedupe key = SHA-256 of content; entries with the same hash are stored once.
    Keeps the ORIGINAL filename when a hash is new; later duplicates only bump
    `lastSeen`. Returns (kept_new, dup_seen, total_entries, index_dict)."""
    import hashlib
    import zipfile
    mdir, idx = load_media_master(out_dir, name, uid)
    files = idx.setdefault("files", {})
    n_new = n_dup = n_total = 0
    for zp in zips:
        crawled_at = file_crawled_at(zp)
        try:
            zf = zipfile.ZipFile(zp)
        except Exception as e:
            idx.setdefault("snapshots", []).append(
                {"zip": os.path.basename(zp), "error": str(e)[:120]})
            continue
        entries = [i for i in zf.infolist() if not i.is_dir() and i.file_size > 0]
        for i, info in enumerate(entries):
            if progress:
                progress(n_total, None)
            n_total += 1
            h = hashlib.sha256()
            with zf.open(info) as f:
                while True:
                    chunk = f.read(hash_chunk)
                    if not chunk:
                        break
                    h.update(chunk)
            digest = h.hexdigest()
            rec = files.get(digest)
            if rec:  # already archived in an earlier crawl
                n_dup += 1
                if crawled_at > rec.get("lastSeen", ""):
                    rec["lastSeen"] = crawled_at
                al = rec.setdefault("alsoAs", [])
                if info.filename not in al and info.filename != rec.get("file"):
                    al.append(info.filename)  # alt name only (set-like: re-fold safe)
                continue
            # brand-new content — store under its (already date-stamped) name
            fname = os.path.basename(info.filename)
            sub = "videos" if fname.lower().endswith(".mp4") else "photos"
            target = os.path.join(mdir, sub, fname)
            k = 1
            while os.path.exists(target) and \
                    os.path.basename(target) in {r.get("file") for r in files.values()}:
                stem, ext = os.path.splitext(fname)
                target = os.path.join(mdir, sub, f"{stem}_{k}{ext}")
                k += 1
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as f, open(target, "wb") as out:
                while True:
                    chunk = f.read(hash_chunk)
                    if not chunk:
                        break
                    out.write(chunk)
            files[digest] = {"file": os.path.relpath(target, mdir).replace("\\", "/"),
                             "bytes": info.file_size, "sentAt": info.date_time,
                             "firstSeen": crawled_at, "lastSeen": crawled_at}
            n_new += 1
        idx.setdefault("snapshots", []).append(
            {"zip": os.path.basename(zp), "crawledAt": crawled_at,
             "entries": len(entries), "new": n_new})
    save_media_master(mdir, idx)
    return n_new, n_dup, n_total, idx


# --------------------------------------------------------------------- CLI
def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = sys.argv[1:]
    if not argv:
        print(__doc__)
        return
    if argv[0] == "--scan":
        for g in scan_dir(argv[1:]):
            print(f"{len(g['files'])} files: {g['name']}")
            for f in g["files"]:
                print("   ", os.path.basename(f))
        return
    if argv[0] == "--auto":
        for g in scan_dir(argv[1:]):
            outs, n, nsrc = merge_group(g, os.path.dirname(g["files"][0]))
            print(f"{g['name']}: {n} msgs (from {nsrc} files) -> "
                  f"{', '.join(os.path.basename(o) for o in outs)}")
        return
    if argv[0] == "--fold":
        # python zalo_merge.py --fold OUT_DIR [master.json] snap1.json snap2.json ...
        out_dir = argv[1]
        rest = argv[2:]
        mp = rest[0] if rest and os.path.basename(rest[0]).startswith(MASTER_PREFIX) else None
        snaps = rest[1:] if mp else rest
        master, stats = fold_files(mp, snaps, out_dir)
        for s, total, nn, nu, nm in stats:
            print(f"  {s}: {total} rows -> new {nn}, known {nu}, missing-marked {nm}")
        print(f"master: {master['count']} msgs")
        return
    # explicit file list mode
    args, out, md = argv, "master.json", None
    if "-o" in args:
        i = args.index("-o")
        out = args[i + 1]
        args = args[:i] + args[i + 2:]
    if "--md" in args:
        i = args.index("--md")
        md = args[i + 1]
        args = args[:i] + args[i + 2:]
    lists = [load_source(p) for p in args]
    merged = merge_sources(lists)
    meta = {"conversation": os.path.basename(args[0]).rsplit("_", 1)[0],
            "mergedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "sources": [os.path.basename(p) for p in args]}
    with open(out, "wb") as f:
        f.write(build_master(merged, meta))
    print(f"{out}: {len(merged)} msgs (inputs: "
          f"{sum(len(l) for l in lists)})")
    if md:
        with open(md, "wb") as f:
            f.write(build_master_md(merged, meta))
        print(md)


if __name__ == "__main__":
    main()
