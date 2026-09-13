#!/usr/bin/env python3
"""Zalo Backup WebUI — giao diện web backup tin nhắn Zalo.

Yêu cầu: Zalo PC đang chạy với --remote-debugging-port=8315
         (nút "Restart Zalo" trong UI sẽ tự làm việc này nếu chưa)

Chạy:  python zalo_backup_webui.py     # mở http://localhost:8320
"""
import csv
import io
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote as urlquote, urlparse, parse_qs

import websocket

try:
    import pillow_jxl  # registers JPEG-XL codec with Pillow (Zalo CDN serves .jxl)
except ImportError:
    pillow_jxl = None
from PIL import Image

try:
    import piexif
except ImportError:
    piexif = None

CDP_HTTP = "http://localhost:8315"
# When frozen (PyInstaller exe), __file__ points into the temp extraction dir;
# use the exe's own folder so exports land next to ZaloBackup.exe.
if getattr(sys, "frozen", False):
    BACKUP_DIR = os.path.dirname(sys.executable)
else:
    BACKUP_DIR = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.path.join(BACKUP_DIR, "exports")
os.makedirs(EXPORT_DIR, exist_ok=True)

# merge module lives next to this script (also bundled by PyInstaller)
sys.path.insert(0, BACKUP_DIR)
try:
    import zalo_merge as ZMERGE
except Exception:
    ZMERGE = None

ZALO_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Zalo", "Zalo.exe")
ME_UID_FALLBACK = ""

JOBS = {}  # job_id -> {"status","uid","name","fmt","done","total","file","error","started"}


# ---------------------------------------------------------------- media helpers
MEDIA_KINDS = {"2": "image", "18": "video"}  # msgType -> kind
UA_HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ZaloPC/26.8.20",
           "Referer": "https://chat.zalo.me/"}


def _kind_of(m):
    k = MEDIA_KINDS.get(str(m.get("msgType")))
    if k:
        return k
    t = str(m.get("originMsgType") or "")
    if "photo" in t or "image" in t:
        return "image"
    if "video" in t:
        return "video"
    return None


def _media_url(m):
    v = m.get("message")
    if not isinstance(v, dict):
        return None
    for k in ("oriUrl", "normalUrl", "href"):
        u = v.get(k)
        if isinstance(u, str) and u.startswith("http"):
            return u
    return None


def _media_url_chain(m):
    """All candidate URLs for a media message — CDN links expire one by one,
    so try every variant Zalo stored (hd inside params, thumb, ...)."""
    v = m.get("message")
    urls = []
    if isinstance(v, dict):
        for k in ("oriUrl", "normalUrl", "hdUrl", "thumbUrl", "href"):
            u = v.get(k)
            if isinstance(u, str) and u.startswith("http") and u not in urls:
                urls.append(u)
        p = v.get("params")
        if isinstance(p, str) and p.startswith("{"):
            try:
                pj = json.loads(p)
                for k in ("hd", "thumb", "oriUrl"):
                    u = pj.get(k)
                    if isinstance(u, str) and u.startswith("http") and u not in urls:
                        urls.append(u)
            except Exception:
                pass
    return urls


def _http_get(url, timeout=90, retries=3):
    last = None
    for att in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA_HDRS)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            time.sleep(1.5 * (att + 1))
    raise last


def _sniff(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] in (b"GIF8",):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:2] == b"\xff\x0a" or data[:12] == b"\x00\x00\x00\x0cJXL ":
        return "jxl"
    if data[4:8] == b"ftyp":
        return "mp4"
    return "bin"


def _exif_date_jpeg(data, dt):
    """Write DateTimeOriginal/DateTime into JPEG EXIF so Google Photos dates it. Returns new bytes."""
    if not piexif:
        return data
    ts = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    try:
        loaded = piexif.load(data)
        for k in ("0th", "Exif", "GPS", "1st"):
            exif_dict[k] = loaded.get(k) or {}
    except Exception:
        pass
    exif_dict["0th"][piexif.ImageIFD.DateTime] = ts
    exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = ts
    exif_dict["Exif"][piexif.ExifIFD.DateTimeDigitized] = ts
    try:
        out = io.BytesIO()
        piexif.insert(piexif.dump(exif_dict), data, out)
        return out.getvalue()
    except Exception:
        return data


def _convert_image(data, dt):
    """Return (bytes, ext). JXL is converted to JPEG (Google Photos can't read JXL);
    other formats pass through. JPEG gets EXIF date."""
    kind = _sniff(data)
    if kind == "jxl":
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            if im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=92)
            data = _exif_date_jpeg(buf.getvalue(), dt)
            return data, "jpg"
        except Exception:
            return data, "jxl"  # keep raw if conversion fails
    if kind == "jpg":
        return _exif_date_jpeg(data, dt), "jpg"
    return data, kind


def _find_mvhd(data):
    """Locate 'mvhd' inside the moov/mvhd path; returns index of 'mvhd' or -1."""
    return data.find(b"mvhd")


def _stamp_mp4(data, dt):
    """Patch mvhd creation+modification times (seconds since 1904-01-01).
    Cheap and makes media libraries sort the video by send date."""
    off = _find_mvhd(data)
    if off < 0:
        return data
    try:
        epoch1904 = 2082844800
        secs = int(dt.replace(tzinfo=timezone.utc).timestamp()) + epoch1904
        base = off + 4          # base points at version byte; creation_time starts at base+4
        ver = data[base]
        if ver == 1:            # 64-bit versions
            struct.pack_into(">Q", data, base + 4, secs)
            struct.pack_into(">Q", data, base + 12, secs)
        else:                   # version 0, 32-bit
            struct.pack_into(">I", data, base + 4, secs)
            struct.pack_into(">I", data, base + 8, secs)
        return data
    except Exception:
        return data


def _mtime_stamp(path, dt):
    try:
        ts = dt.timestamp()
        os.utime(path, (ts, ts))
    except Exception:
        pass


def _get_conv_name(cdp, uid):
    try:
        convs = json.loads(cdp.evaluate(JS["convs"]))
        hit = next((c for c in convs if c["uid"] == uid), None)
        if hit:
            return hit["name"]
    except Exception:
        pass
    return uid


def run_media_job(job_id, uid, cancel=None):
    """Download all photos+videos of a conversation in parallel and pack a date-stamped ZIP.
    Returns (ok, zip_path, n_ok, n_fail) so run_all_job can call it directly."""
    job = JOBS[job_id]
    try:
        cdp = Cdp(find_page())
        conv_name = _get_conv_name(cdp, uid)
        job["name"] = conv_name

        def progress(done, total):
            job["done"], job["total"] = done, total

        job["status"] = "scan"
        msgs, _ = fetch_all(uid, progress, cdp=cdp)
        media = []
        for m in msgs:
            k = _kind_of(m)
            if not k:
                continue
            urls = _media_url_chain(m)
            if urls:
                media.append((k, urls, int(m.get("serverTime") or m.get("sendDttm") or 0),
                              str(m.get("msgId") or "")))
        job["status"], job["done"], job["total"] = "downloading", 0, len(media)
        job["found"] = len(media)
        if not media:
            job["status"], job["error"] = "error", "Không tìm thấy ảnh/video nào (hoặc toàn bộ link đã chết)"
            return False, None, 0, 0

        safe = "".join(ch for ch in conv_name if ch not in '\\/:*?"<>|').strip() or uid
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_path = os.path.join(EXPORT_DIR, f"media_{safe}_{uid[:12]}_{stamp}.zip")
        n_ok = n_fail = 0
        LOCK = threading.Lock()

        def fetch_one(idx_item):
            i, (k, url_chain, ts, mid) = idx_item
            dt = datetime.fromtimestamp(ts / 1000) if ts else datetime.now()
            base = dt.strftime("%Y%m%d_%H%M%S")
            name = f"photos/{base}_{mid[:10]}_{i}.jpg" if k == "image" else f"videos/{base}_{mid[:10]}_{i}.mp4"
            data = None
            last_err = None
            for url in url_chain:  # CDN links die at different times — try every variant
                try:
                    cand = _http_get(url, timeout=120, retries=2)
                    if _sniff(cand) == "bin":
                        raise ValueError("not media (dead link?)")
                    data = cand
                    break
                except Exception as e:
                    last_err = e
            if data is None:
                return ("fail", None, name)
            kind = _sniff(data)
            try:
                if k == "image":
                    data, ext = _convert_image(data, dt)
                    name = name[:-3] + ext
                elif kind == "mp4":
                    data = _stamp_mp4(data, dt)
                else:
                    name = name[:-3] + kind  # unexpected kind for this msg — keep honest ext
            except Exception:
                pass
            return ("ok", zipfile.ZipInfo(name, date_time=dt.timetuple()[:6]), data)

        # Write each finished item to the ZIP immediately (no giant in-memory buffer)
        with ThreadPoolExecutor(max_workers=10) as ex, \
                zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as zf:
            futs = [ex.submit(fetch_one, it) for it in enumerate(media)]
            for n, fut in enumerate(as_completed(futs), 1):
                if cancel and cancel.is_set():
                    job["status"], job["error"] = "error", "Đã hủy"
                    return False, None, n_ok, n_fail
                status, info, data = fut.result()
                if status == "ok":
                    zf.writestr(info, data)
                    n_ok += 1
                else:
                    n_fail += 1
                with LOCK:
                    job["done"] = n
        job["file"] = zip_path
        job["okCount"], job["failCount"] = n_ok, n_fail
        job["status"] = "done"
        return True, zip_path, n_ok, n_fail
    except Exception as e:
        job["status"], job["error"] = "error", str(e)[:400]
        return False, None, 0, 0


# ---------------------------------------------------------------- CDP helpers
def find_page():
    with urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=8) as r:
        pages = json.load(r)
    for p in pages:
        if p.get("type") == "page" and "index.html" in p.get("url", ""):
            return p["webSocketDebuggerUrl"]
    return None


class Cdp:
    def __init__(self, url):
        self.ws = websocket.create_connection(url, timeout=180, origin="http://localhost:8315")
        self._id = 0

    def evaluate(self, expr, timeout=150):
        self._id += 1
        rid = self._id
        self.ws.send(json.dumps({"id": rid, "method": "Runtime.evaluate",
                                 "params": {"expression": expr, "awaitPromise": True,
                                            "returnByValue": True, "timeout": timeout * 1000}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == rid:
                res = msg.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError("Page exception: " + json.dumps(res["exceptionDetails"])[:800])
                return res.get("result", {}).get("value")
        raise TimeoutError("CDP evaluate timeout")


def cdp_ready():
    try:
        return find_page() is not None
    except Exception:
        return False


JS = {
    "me": r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  const out = {};
  try { out.uid = store.getUserId(); } catch(e) {}
  try { const me = await store.getMe();
        out.name = me && (me.displayName || me.zaloName); } catch(e) {}
  return JSON.stringify(out);
})()
""",
    "convs": r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  const convs = await store.getConversations();
  return JSON.stringify((convs || []).map(c => ({
    uid: c.userId || c.id,
    name: c.displayName || c.zaloName || c.username || c.userId,
    lastTime: (c.infoCheckSearch && c.infoCheckSearch.lastMessageTime) || null,
  })));
})()
""",
    "count": r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  return String(await store.countTotalMessageOfConversation('UID'));
})()
""",
    "page": r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  const uid = 'UID';
  const count = COUNT;
  const before = { sendDttm: SDTTM, msgId: MSGID };
  let items = [];
  try {
    const r = await store.getMessageFromConversationByLimit(uid, count, {
      count: count,
      fromMsg: (before.sendDttm || before.msgId) ? before : undefined,
      include: false,
    });
    items = Array.isArray(r) ? r : ((r && r.items) || []);
  } catch (e) { return JSON.stringify({ __err: String(e).slice(0, 300) }); }
  const out = [];
  for (const m of items) {
    const o = {};
    for (const k in m) {
      const v = m[k];
      o[k] = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : v;
    }
    out.push(o);
  }
  return JSON.stringify(out);
})()
""",
}


def fetch_all(uid, progress=None, cdp=None):
    """Keyset pagination through the whole conversation. Returns list of messages."""
    if cdp is None:
        cdp = Cdp(find_page())
    msgs = {}
    sdttm = msgid = None
    BATCH = 500
    total = None
    try:
        total = int(cdp.evaluate(JS["count"].replace("UID", uid)) or 0)
    except Exception:
        pass
    while True:
        expr = (JS["page"].replace("UID", uid).replace("COUNT", str(BATCH))
                .replace("SDTTM", json.dumps(str(sdttm)) if sdttm is not None else "undefined")
                .replace("MSGID", json.dumps(str(msgid)) if msgid is not None else "undefined"))
        items = None
        for attempt in range(5):
            try:
                items = json.loads(cdp.evaluate(expr))
                break
            except Exception as e:
                time.sleep(2 ** attempt)
                try:
                    cdp = Cdp(find_page())
                except Exception:
                    pass
        if items is None:
            break
        if isinstance(items, dict) and items.get("__err"):
            raise RuntimeError(items["__err"])
        if not items:
            break
        for m in items:
            mid = str(m.get("msgId") or m.get("cliMsgId") or "")
            if mid:
                msgs[mid] = m
        if progress:
            progress(len(msgs), total)
        if len(items) < BATCH:
            break
        tail = min(items, key=lambda m: (int(str(m.get("sendDttm") or 0) or 0),
                                         int(str(m.get("msgId") or 0) or 0)))
        sdttm = tail.get("sendDttm") or tail.get("serverTime") or ""
        msgid = tail.get("msgId") or tail.get("cliMsgId") or ""
        time.sleep(0.25)
    arr = sorted(msgs.values(),
                 key=lambda m: str(m.get("serverTime") or m.get("sendDttm") or ""))

    # Collapse multi-device copies: when a message was sent from / synced across
    # devices, the store returns it TWICE — a PC copy (src=10, plain msgId) and
    # a phone-sync copy (src=7, msgId suffixed `_NNN`) — sharing cliMsgId but
    # carrying different msgId, ~50 ms apart. Deduping by msgId alone kept both,
    # so exports showed every own-device message duplicated. Keep the copy Zalo
    # displays (later serverTime, then larger msgId) per cliMsgId.
    def _copykey(m):
        return (int(str(m.get("serverTime") or m.get("sendDttm") or 0) or 0),
                str(m.get("msgId") or ""))

    best = {}
    for m in arr:
        cli = str(m.get("cliMsgId") or "")
        if cli and (cli not in best or _copykey(m) > _copykey(best[cli])):
            best[cli] = m
    out, done = [], set()
    for m in arr:
        cli = str(m.get("cliMsgId") or "")
        if cli:
            if cli in done:
                continue
            done.add(cli)
            m = best[cli]
        out.append(m)
    return out, total


# ---------------------------------------------------------------- formatters
def _fmt_time(t):
    try:
        return datetime.fromtimestamp(int(t) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _sender_name(m, me_uid, peer_name=""):
    fu = str(m.get("fromUid"))
    if fu in ("0", me_uid, "me"):
        return "Tôi"
    return m.get("dName") or peer_name or (fu if fu else "Tôi")


def _msg_text(m):
    v = m.get("message")
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    return json.dumps(v, ensure_ascii=False)


# friendly text for media/link/file/sticker messages (used by JSON-friendly + TXT/CSV/MD)
def _friendly(m, peer_name=""):
    t = str(m.get("msgType"))
    v = m.get("message")
    if isinstance(v, str):
        return v
    if not isinstance(v, dict):
        return ""
    if t in ("2", "18"):
        try:
            pm = json.loads(v.get("params") or "{}")
            return ZMERGE.pretty_media_text(t, params=pm)
        except Exception:
            pass
        return ("[Ảnh] " if t == "2" else "[Video] ") + (v.get("oriUrl") or v.get("normalUrl") or "")
    if t == "19":
        return "[File] " + str(v.get("title") or "") + " " + str(v.get("href") or "")
    if t == "6":
        return "[Link] " + str(v.get("title") or v.get("href") or "")
    if t == "7":
        return "[Sticker]"
    return _msg_text(m)


def build_output(msgs, fmt, uid, conv_name, me_uid, friendly=True, peer_name=""):
    if fmt == "json":
        if not friendly:
            payload = {"uid": uid, "conversation": conv_name, "exportedAt":
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "count": len(msgs),
                       "messages": msgs}
            return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
        out = {"uid": uid, "conversation": conv_name,
               "exportedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "count": len(msgs), "messages": []}
        for m in msgs:
            ts = int(m.get("serverTime") or m.get("sendDttm") or 0)
            out["messages"].append({
                "msgId": str(m.get("msgId") or m.get("cliMsgId") or ""),
                "cliMsgId": str(m.get("cliMsgId") or ""),
                "time": _fmt_time(ts),
                "sender": _sender_name(m, me_uid, peer_name),
                "msgType": m.get("msgType"),
                "text": _friendly(m, peer_name),
            })
        return json.dumps(out, ensure_ascii=False, indent=1).encode("utf-8")

    if fmt == "txt":
        buf = io.StringIO()
        buf.write(f"ZALO BACKUP — {conv_name} ({uid})\n")
        buf.write(f"Xuất lúc: {datetime.now():%Y-%m-%d %H:%M:%S} — {len(msgs)} tin nhắn\n")
        buf.write("=" * 60 + "\n\n")
        for m in msgs:
            t = _fmt_time(m.get("serverTime") or m.get("sendDttm"))
            who = _sender_name(m, me_uid, peer_name)
            text = _friendly(m, peer_name) if friendly else _msg_text(m)
            buf.write(f"[{t}] {who}:\n{text}\n\n")
        return buf.getvalue().encode("utf-8")

    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time", "sender", "msgType", "text"])
        for m in msgs:
            w.writerow([_fmt_time(m.get("serverTime") or m.get("sendDttm")),
                        _sender_name(m, me_uid, peer_name), m.get("msgType"),
                        _friendly(m, peer_name) if friendly else _msg_text(m)])
        return buf.getvalue().encode("utf-8-sig")

    if fmt == "md":
        buf = io.StringIO()
        buf.write(f"# Zalo backup — {conv_name}\n\n")
        buf.write(f"*UID `{uid}` — {len(msgs)} tin — xuất {datetime.now():%Y-%m-%d %H:%M}*\n\n")
        cur_day = ""
        for m in msgs:
            ts = int(m.get("serverTime") or m.get("sendDttm") or 0)
            dt = datetime.fromtimestamp(ts / 1000)
            if dt.strftime("%Y-%m-%d") != cur_day:
                cur_day = dt.strftime("%Y-%m-%d")
                buf.write(f"\n## {dt.strftime('%d/%m/%Y')}\n\n")
            who = _sender_name(m, me_uid, peer_name)
            text = (_friendly(m, peer_name) if friendly else _msg_text(m)).replace("\n", "  \n  ")
            buf.write(f"**{dt.strftime('%H:%M')} — {who}**: {text}  \n")
        return buf.getvalue().encode("utf-8")

    raise ValueError("unknown format " + fmt)


EXT = {"json": "json", "txt": "txt", "csv": "csv", "md": "md"}
# JSON friendly by default (time/sender/type/text); TXT/CSV/MD already human-readable
FRIENDLY = {"json": True, "txt": True, "csv": True, "md": True}


# ---------------------------------------------------------------- export job
def run_job(job_id, uid, fmt, friendly=True):
    job = JOBS[job_id]
    try:
        me_uid = ""
        try:
            me_uid = json.loads(Cdp(find_page()).evaluate(JS["me"])).get("uid") or ""
        except Exception:
            pass
        name = f"conv_{uid}"
        conv_name = uid
        try:
            convs = json.loads(Cdp(find_page()).evaluate(JS["convs"]))
            hit = next((c for c in convs if c["uid"] == uid), None)
            if hit:
                conv_name = hit["name"]
        except Exception:
            pass
        job["name"] = conv_name

        def progress(done, total):
            job["done"], job["total"] = done, total

        msgs, _ = fetch_all(uid, progress)
        if not msgs:
            job["status"], job["error"] = "error", "Không lấy được tin nhắn nào (hội thoại rỗng hoặc chưa sync)"
            return
        job["status"] = "saving"
        safe = "".join(ch for ch in conv_name if ch not in '\\/:*?"<>|').strip() or uid
        fname = f"{safe}_{uid}.{EXT[fmt]}"
        data = build_output(msgs, fmt, uid, conv_name, me_uid,
                            friendly=friendly, peer_name=conv_name)
        out_path = os.path.join(EXPORT_DIR, fname)
        with open(out_path, "wb") as f:
            f.write(data)
        job["file"] = out_path
        job["done"] = job["total"] = len(msgs)
        job["status"] = "done"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)[:400]


# ------------------------------------------------- export ALL conversations (one click)
CANCEL = threading.Event()  # shared cancel flag; set by /api/cancel


def run_all_job(job_id, fmt="md"):
    """One-click backup of EVERY conversation of the logged-in account.
    Per conversation: text export (fmt) + media ZIP. Skips conversations already backed
    up (exports/<name>_<uid>.md exists and is newer than 24h). Cancelable via /api/cancel."""
    job = JOBS[job_id]
    try:
        cdp = Cdp(find_page())
        me = json.loads(cdp.evaluate(JS["me"]))
        me_uid = me.get("uid") or ""
        convs = json.loads(cdp.evaluate(JS["convs"]))
        convs.sort(key=lambda c: c.get("lastTime") or 0, reverse=True)
        job["total"] = len(convs)
        job["status"] = "running"
        summary = []

        def record(name, uid, n_msgs, kind, status, extra=""):
            summary.append({"name": name, "uid": uid, "msgs": n_msgs,
                            "kind": kind, "status": status, "extra": extra})
            job["summary"] = summary
            job["done"] = len(summary)

        for i, c in enumerate(convs):
            if CANCEL.is_set():
                job["status"] = "done"
                job["extra"] = "Đã hủy — những hội thoại chưa chạy bị bỏ qua"
                break
            uid, name = c["uid"], c.get("name") or uid
            job["current"] = f"({i + 1}/{len(convs)}) {name}"
            safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or uid
            mpath = os.path.join(EXPORT_DIR, f"{safe}_{uid}.{EXT[fmt]}")

            # ---- messages ----
            fresh = os.path.exists(mpath) and (time.time() - os.path.getmtime(mpath) < 86400)
            if fresh:
                record(name, uid, None, "msg", "skip (có sẵn)")
            else:
                job["status"] = "scan"
                try:
                    msgs, _ = fetch_all(uid, None, cdp=cdp)
                except Exception as e:
                    record(name, uid, None, "msg", "fail", str(e)[:120])
                    msgs = None
                if msgs is not None:
                    job["status"] = "saving"
                    if msgs:
                        try:
                            data = build_output(msgs, fmt, uid, name, me_uid,
                                                friendly=True, peer_name=name)
                            with open(mpath, "wb") as f:
                                f.write(data)
                            record(name, uid, len(msgs), "msg", "ok")
                        except Exception as e:
                            record(name, uid, len(msgs), "msg", "fail", str(e)[:120])
                    else:
                        record(name, uid, 0, "msg", "empty")
                if CANCEL.is_set():
                    break

            # ---- media ----
            prefix = os.path.join(EXPORT_DIR, f"media_{safe}_{uid[:12]}")
            recent_zip = any(f.startswith(os.path.basename(prefix)) and f.endswith(".zip")
                             and time.time() - os.path.getmtime(os.path.join(EXPORT_DIR, f)) < 86400
                             for f in os.listdir(EXPORT_DIR))
            if recent_zip:
                record(name, uid, None, "media", "skip (có sẵn)")
            else:
                mjid = job_id + ":m" + uid[-8:]
                JOBS[mjid] = {"status": "running", "uid": uid, "fmt": "media",
                              "done": 0, "total": 0, "file": None, "error": None, "name": name}
                ok, _path, n_ok, n_fail = run_media_job(mjid, uid, cancel=CANCEL)
                merr = str(JOBS.get(mjid, {}).get("error") or "")[:120]
                JOBS.pop(mjid, None)
                if CANCEL.is_set():
                    job["status"] = "done"
                    job["extra"] = "Đã hủy"
                    break
                if ok:
                    label = f"ok: {n_ok}" + (f", chết/lỗi: {n_fail}" if n_fail else "")
                else:
                    label = "empty" if n_fail == 0 else ("fail" + (f": {merr}" if merr else ""))
                record(name, uid, None, "media", label)

        if job["status"] != "done":
            job["status"] = "done"
        job["file"] = None
    except Exception as e:
        job["status"], job["error"] = "error", str(e)[:400]


# ------------------------------------------------- merge duplicate exports (one click)
def run_merge_job(job_id):
    """Accumulate every crawl snapshot of a conversation into ONE master file
    (master_<name>_<uid>.json). Two cases per group:
      * snapshots share one UID (later crawl supersedes earlier) -> fold into the
        master by msgId: new msgs appended, known ones just touched, absent ones
        (when a newer msg proves coverage) flagged missingSince — nothing lost,
        this is the data layer for the auto-crawl feature;
      * snapshots have different UIDs (same chat seen from 2 accounts) -> also
        fold cross-account snapshots into the same master (fuzzy matching).
    No Zalo/CDP needed — offline job."""
    job = JOBS[job_id]
    if ZMERGE is None:
        job["status"], job["error"] = "error", "zalo_merge.py not found next to the app"
        return
    try:
        groups = ZMERGE.scan_dir([EXPORT_DIR, BACKUP_DIR])
        job["total"] = len(groups)
        summary = []

        def record(name, status, extra=""):
            summary.append({"name": name, "uid": "", "msgs": None,
                            "kind": "merge", "status": status, "extra": extra})
            job["summary"] = summary
            job["done"] = len(summary)

        for g in groups:
            if CANCEL.is_set():
                job["status"], job["extra"] = "done", "Đã hủy"
                break
            job["current"] = f"merge: {g['name']} ({len(g['files'])} bản)"
            try:
                # snapshot files = structured message exports (json/csv), oldest crawl first
                snaps = [p for p in g["files"] if p.lower().endswith((".json", ".csv"))]
                if not snaps:
                    record(g["name"], "skip (không có bản dữ liệu)")
                    continue
                snaps.sort(key=ZMERGE.file_crawled_at)
                # uid = the one shared by the most snapshots (single-account case)
                from collections import Counter
                uids = []
                for p in snaps:
                    stem = os.path.basename(p).rsplit("_", 1)[-1].split(".")[0]
                    if ZMERGE.UIDLIKE.match(stem):
                        uids.append(stem)
                uid = Counter(uids).most_common(1)[0][0] if uids else "x"
                mpath = ZMERGE.master_path_for(EXPORT_DIR, g["name"], uid)
                safe = "".join(ch for ch in g["name"] if ch not in '\\/:*?"<>|').strip()
                if os.path.exists(mpath):
                    with open(mpath, encoding="utf-8") as f:
                        master = json.load(f)
                else:
                    master = ZMERGE.empty_master(g["name"], uid)
                tot_new = tot_miss = 0
                folded_ats = {s.get("crawledAt") for s in master.get("snapshots", [])}
                for p in snaps:
                    cat = ZMERGE.file_crawled_at(p)
                    if cat in folded_ats:  # already folded — keep the button idempotent
                        continue
                    rows = ZMERGE.load_source(p)
                    # only a same-account full crawl can prove a message is missing;
                    # cross-account views legitimately differ -> never mark missing
                    stem_uid = os.path.basename(p).rsplit("_", 1)[-1].split(".")[0]
                    same_acct = bool(ZMERGE.UIDLIKE.match(stem_uid)) and stem_uid == uid
                    n_new, _n_upd, n_miss = ZMERGE.fold_snapshot(
                        master, rows, cat, complete=same_acct)
                    tot_new += n_new
                    tot_miss += n_miss
                ZMERGE.save_master(master, EXPORT_DIR)

                # ---- media: fold this conversation's media ZIPs into its media master
                media_note = ""
                zips = [os.path.join(d, f) for d in {EXPORT_DIR, BACKUP_DIR}
                        for f in os.listdir(d)
                        if f.startswith("media_") and safe in f and f.lower().endswith(".zip")
                        and os.path.getsize(os.path.join(d, f)) > 0]
                if zips:
                    mn_new, mn_dup, mn_tot, _idx = ZMERGE.fold_media_zips(
                        zips, EXPORT_DIR, g["name"], uid)
                    media_note = (f"; media: +{mn_new} mới/{mn_dup} trùng "
                                  f"(tổng {mn_tot} mục) -> media_master_{g['name']}_{uid}")
                record(g["name"],
                       f"ok: master {master['count']} tin (+{tot_new} mới, "
                       f"{tot_miss} nghi bị thu hồi) từ {len(snaps)} lần crawl{media_note}")
            except Exception as e:
                record(g["name"], "fail", str(e)[:150])
        if job["status"] != "done":
            job["status"] = "done"
        if not groups:
            job["extra"] = "Không tìm thấy bản nào để gộp (cần >=1 file dữ liệu JSON/CSV)"
    except Exception as e:
        job["status"], job["error"] = "error", str(e)[:400]


# ---------------------------------------------------------------- restart zalo
def restart_zalo_debug():
    subprocess.run(["taskkill", "/F", "/IM", "Zalo.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([ZALO_EXE, "--remote-debugging-port=8315", "--remote-allow-origins=*"],
                     shell=False)
    for _ in range(60):
        time.sleep(2)
        if cdp_ready():
            return True
    return False


# ---------------------------------------------------------------- HTTP server
HTML = """<!doctype html><html lang="vi"><head><meta charset="utf-8">
<title>Zalo Backup</title><style>
:root{--bg:#0f1420;--panel:#1a2233;--line:#2a3550;--tx:#e8ecf5;--mut:#8fa0bf;--acc:#4da3ff;--ok:#3ddc84;--err:#ff6b6b}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,Segoe UI,Arial;background:var(--bg);color:var(--tx)}
.wrap{max-width:960px;margin:0 auto;padding:20px}
h1{font-size:20px;margin:0 0 4px}.sub{color:var(--mut);font-size:12px;margin-bottom:16px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.status b{color:var(--acc)}
button{background:var(--acc);border:0;color:#04101f;font-weight:600;padding:8px 14px;border-radius:8px;cursor:pointer}
button.ghost{background:transparent;color:var(--acc);border:1px solid var(--acc)}
button:disabled{opacity:.5;cursor:default}
input[type=search]{flex:1;min-width:200px;background:#0d1322;border:1px solid var(--line);color:var(--tx);padding:8px 10px;border-radius:8px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--mut);text-align:left;font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:7px 8px;border-bottom:1px solid #202a42}
tr.sel td{background:#20305a}tbody tr{cursor:pointer}tbody tr:hover td{background:#1c2740}
.fmt label{margin-right:14px;cursor:pointer}.fmt input{vertical-align:-2px}
#progwrap{display:none;margin-top:10px}
.bar{height:10px;background:#0d1322;border-radius:6px;overflow:hidden;border:1px solid var(--line)}
.bar>i{display:block;height:100%;background:linear-gradient(90deg,#4da3ff,#3ddc84);width:0%;transition:width .3s}
#msg{margin-top:8px;font-size:13px}.ok{color:var(--ok)}.err{color:var(--err)}
a.dl{color:var(--ok);font-weight:600}
.small{color:var(--mut);font-size:12px;margin-top:8px}
</style></head><body><div class="wrap">
<h1>🟦 Zalo Backup</h1><div class="sub">Chạy cục bộ trên máy bạn — dữ liệu không rời khỏi máy (localhost only) · <a href="/viewer" style="color:#38bdf8">📖 Xem & đọc master (tin nhắn + media)</a></div>

<div class="panel status">
 <div class="row">
  <span id="acct">Đang kiểm tra Zalo…</span>
  <span style="flex:1"></span>
  <button class="ghost" onclick="refresh()">↻ Làm mới</button>
  <button class="ghost" onclick="restart()">⟳ Restart Zalo (debug mode)</button>
 </div>
</div>

<div class="panel">
 <div class="row"><input id="q" type="search" placeholder="Tìm theo tên hoặc UID…">
 <span class="small" id="cnt"></span></div>
 <div style="max-height:340px;overflow:auto;margin-top:10px">
 <table><thead><tr><th style="width:45%">Tên</th><th>UID</th><th>Tin cuối</th></tr></thead>
 <tbody id="tbody"></tbody></table></div>
</div>

<div class="panel">
 <div class="row fmt"><b>Định dạng:</b>
  <label><input type="radio" name="fmt" value="json" checked> JSON (dễ đọc)</label>
  <label><input type="radio" name="fmt" value="txt"> TXT (chat)</label>
  <label><input type="radio" name="fmt" value="csv"> CSV (Excel)</label>
  <label><input type="radio" name="fmt" value="md"> Markdown</label>
  <label title="Giữ nguyên mọi trường dữ liệu thô của Zalo (msgId, status, properties...) — chỉ áp dụng cho JSON"><input type="checkbox" id="rawjson"> JSON gốc (thô, đầy đủ)</label>
 </div>
 <div class="row" style="margin-top:12px">
  <button id="go" onclick="doExport()" disabled>⬇ Xuất hội thoại đã chọn</button>
  <button id="gomedia" onclick="doMedia()" disabled style="background:#3ddc84">🖼 Tải tất cả ảnh &amp; video</button>
  <button id="goall" onclick="doAll()" style="background:#ffb84d" title="Backup MỌI hội thoại: tin nhắn + ảnh/video, bỏ qua cái đã có trong 24h qua">⚡ Backup TẤT CẢ hội thoại</button>
  <button id="gomerge" onclick="doMerge()" style="background:#b388ff" title="Tìm các hội thoại đã xuất từ nhiều account (UID khác nhau cùng một chat) và gộp thành 1 file master khử trùng lặp">🔗 Gộp bản trùng lặp</button>
  <button id="stop" onclick="doCancel()" style="display:none;background:#ff6b6b">■ Hủy</button>
  <span class="small" id="pickinfo">Chưa chọn hội thoại</span>
 </div>
 <div id="progwrap"><div class="bar"><i id="bar"></i></div><div id="msg"></div></div>
</div>

<div class="small">File xuất lưu tại <b>H:\\zalo-backup\\exports\\</b> — hoặc tải trực tiếp qua link sau khi xong.<br>
Ảnh/video đóng gói ZIP kèm ngày gửi: ảnh dán EXIF, video đặt mtime + ngày trong file mp4 — Google Photos tự nhận đúng ngày.</div>
</div>
<script>
let CONVS=[],SEL=null,TIMER=null,JOB=null;
async function refresh(){location.reload===0;loadStatus();loadConvs();}
async function api(p){const r=await fetch(p);return r.json();}
async function loadStatus(){
 const el=document.getElementById('acct');
 try{const s=await api('/api/status');
  if(!s.cdp){el.innerHTML='⚠ Zalo chưa chạy debug mode — bấm <b>Restart Zalo</b>';return;}
  el.innerHTML='Zalo OK — account: <b>'+(s.name||'?')+'</b> ('+(s.uid||'?')+') — '+s.convCount+' hội thoại';
 }catch(e){el.textContent='⚠ Không kết nối được server';}
}
async function loadConvs(){
 try{const s=await api('/api/conversations');CONVS=s.convs||[];
  document.getElementById('cnt').textContent=CONVS.length+' hội thoại';
  render('');
 }catch(e){}
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]));}
function fmtT(t){if(!t)return '';const d=new Date(+t);return d.toLocaleDateString('vi-VN')+' '+d.toLocaleTimeString('vi-VN',{hour:'2-digit',minute:'2-digit'});}
function render(q){
 const tb=document.getElementById('tbody');tb.innerHTML='';
 q=q.toLowerCase();
 const list=CONVS.filter(c=>!q||(c.name||'').toLowerCase().includes(q)||(c.uid||'').includes(q));
 for(const c of list){
  const tr=document.createElement('tr');
  tr.innerHTML='<td>'+esc(c.name)+'</td><td style="color:#8fa0bf">'+esc(c.uid)+'</td><td style="color:#8fa0bf">'+fmtT(c.lastTime)+'</td>';
  if(SEL&&SEL.uid===c.uid)tr.classList.add('sel');
 tr.onclick=()=>{SEL=c;document.getElementById('pickinfo').textContent='Đã chọn: '+c.name;
   if(!document.getElementById('goall').disabled){document.getElementById('go').disabled=false;document.getElementById('gomedia').disabled=false;}
   render(document.getElementById('q').value);};
  tb.appendChild(tr);
 }
}
document.getElementById('q').addEventListener('input',e=>render(e.target.value));
async function restart(){
 const el=document.getElementById('acct');el.textContent='Đang restart Zalo với debug mode… (10–40s)';
 await api('/api/restart');setTimeout(()=>{loadStatus();loadConvs();},1500);
}
async function doExport(){
 if(!SEL)return;
 const fmt=document.querySelector('input[name=fmt]:checked').value;
 const raw=document.getElementById('rawjson').checked;
 const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({uid:SEL.uid,fmt:fmt,raw:raw})});
 JOB=await r.json();
 startUI();
}
async function doMedia(){
 if(!SEL)return;
 const r=await fetch('/api/exportmedia',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({uid:SEL.uid})});
 JOB=await r.json();
 startUI();
}
async function doAll(){
 const fmt=document.querySelector('input[name=fmt]:checked').value;
 if(!confirm('Backup TẤT CẢ hội thoại của account?\n\n- Tin nhắn: định dạng đã chọn\n- Ảnh/video: ZIP từng hội thoại\n- Bỏ qua cái đã backup trong 24h qua\n\nChạy dài (có thể hàng giờ nếu lần đầu). Tiếp tục?'))return;
 const r=await fetch('/api/exportall',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({fmt:fmt})});
 JOB=await r.json();
 startUI();
}
async function doCancel(){
 await fetch('/api/cancel',{method:'POST'});
 document.getElementById('msg').innerHTML='<span class="err">Đang dừng…</span>';
}
function startUI(){
 document.getElementById('progwrap').style.display='block';
 document.getElementById('go').disabled=true;document.getElementById('gomedia').disabled=true;
 document.getElementById('goall').disabled=true;document.getElementById('gomerge').disabled=true;
 document.getElementById('stop').style.display='';
 if(TIMER)clearInterval(TIMER);
 TIMER=setInterval(poll,700);
}
function finishUI(){
 document.getElementById('go').disabled=!SEL;document.getElementById('gomedia').disabled=!SEL;
 document.getElementById('goall').disabled=false;document.getElementById('gomerge').disabled=false;
 document.getElementById('stop').style.display='none';
}
async function doMerge(){
 const r=await fetch('/api/merge',{method:'POST'});
 JOB=await r.json();
 startUI();
}
async function poll(){
 const j=await api('/api/progress?job='+JOB.job);
 const bar=document.getElementById('bar'),msg=document.getElementById('msg');
 let pct=j.total?Math.round(100*j.done/j.total):0;
 if(j.status==='done')pct=100;
 bar.style.width=pct+'%';
 let label;
 if(j.current!==undefined&&j.current)label='⏳ '+j.current+' — '+j.status;
 else if(j.status==='scan')label='⏳ Đang quét tin nhắn — '+j.done+(j.total?('/'+j.total):'');
 else if(j.status==='downloading')label='⏳ Đang tải '+(j.done||0)+'/'+(j.total||j.found||'?')+' ảnh/video';
 else label='⏳ '+j.status+' — '+(j.done||0)+(j.total?('/'+j.total):'');
 if(j.summary&&j.summary.length){
  const lines=j.summary.slice(-8).map(s=>'  '+esc(s.name)+': '+esc(s.status)+(s.msgs?' ('+s.msgs+' tin)':''));
  label+='<br><span style="color:#8fa0bf">'+lines.join('<br>')+'</span>';
 }
 if(j.extra)label+='<br><span class="err">'+esc(j.extra)+'</span>';
 msg.innerHTML=label;
 if(j.status==='done'){clearInterval(TIMER);finishUI();
  if(j.count&&j.okCount!==undefined){
   const extra=' — tải được: '+j.okCount+(j.failCount?', chết/lỗi: '+j.failCount:'');
   msg.innerHTML='<span class="ok">✔ Xong: '+j.count+' (tìm thấy '+j.found+') mục'+extra+'</span> — <a class="dl" href="/api/download?job='+JOB.job+'">Tải file '+esc(j.filename)+'</a>';
  } else if(j.summary){
   const okN=j.summary.filter(s=>s.status.startsWith('ok')).length;
   const skipN=j.summary.filter(s=>s.status.startsWith('skip')).length;
   const failN=j.summary.filter(s=>s.status.startsWith('fail')).length;
   msg.innerHTML='<span class="ok">✔ Xong TẤT CẢ: '+j.total+' hội thoại — '+okN+' ok, '+skipN+' bỏ qua (đã có), '+failN+' lỗi</span>. File nằm trong H:\\zalo-backup\\exports\\';
  } else {
   msg.innerHTML='<span class="ok">✔ Xong</span> — <a class="dl" href="/api/download?job='+JOB.job+'">Tải file '+esc(j.filename)+'</a>';
  }
 }
 if(j.status==='error'){clearInterval(TIMER);finishUI();
  msg.innerHTML='<span class="err">✖ Lỗi: '+esc(j.error)+'</span>';
 }
}
loadStatus();loadConvs();
</script></body></html>"""


# -------------------------------------------------------------- viewer ----

VIEWER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
               ".webm": "video/webm", ".mov": "video/quicktime"}
_M_CACHE = {}  # path -> (mtime, size, parsed dict)
_AUDIT_STATE = {}  # uid -> {running, done, total} for background media-link audit


def _viewer_load_master(fp):
    st = os.stat(fp)
    key = (st.st_mtime, st.st_size)
    c = _M_CACHE.get(fp)
    if c and c[0] == key:
        return c[1]
    d = json.load(open(fp, encoding="utf-8"))
    _M_CACHE[fp] = (key, d)
    return d


def _viewer_media_dir_for(uid):
    if not uid:
        return None
    try:
        for e in sorted(os.listdir(EXPORT_DIR)):
            full = os.path.join(EXPORT_DIR, e)
            if e.startswith("media_master_") and e.endswith(str(uid)) and os.path.isdir(full):
                return full
    except OSError:
        pass
    return None


def _viewer_media_info(uid, with_files=False):
    d = _viewer_media_dir_for(uid)
    if not d:
        return None
    photos = videos = 0
    total = 0
    files = []
    try:
        idx = json.load(open(os.path.join(d, "index.json"), encoding="utf-8"))
        for v in (idx.get("files") or {}).values():
            rel = v.get("file") or ""
            if rel.startswith("videos/"):
                videos += 1
            elif rel.startswith("photos/"):
                photos += 1
            total += v.get("bytes") or 0
            if with_files:
                sa = v.get("sentAt")
                files.append({"file": rel, "bytes": v.get("bytes") or 0,
                              "sentAt": "%04d-%02d-%02d %02d:%02d:%02d" % tuple(sa) if sa else None,
                              "msgType": "18" if rel.startswith("videos/") else ("2" if rel.startswith("photos/") else None),
                              "alsoAs": len(v.get("alsoAs") or [])})
    except Exception:
        for sub, isv in (("photos", False), ("videos", True)):
            p = os.path.join(d, sub)
            if os.path.isdir(p):
                n = len([x for x in os.listdir(p) if not x.startswith(".")])
                if isv:
                    videos += n
                else:
                    photos += n
    info = {"dir": os.path.basename(d), "photos": photos, "videos": videos, "bytes": total}
    if with_files:
        files.sort(key=lambda x: x.get("sentAt") or "", reverse=True)
        info["files"] = files
    return info


def _viewer_api_masters():
    out = []
    try:
        names = sorted(os.listdir(EXPORT_DIR))
    except OSError:
        names = []
    for fn in names:
        if not (fn.startswith("master_") and fn.endswith(".json")):
            continue
        fp = os.path.join(EXPORT_DIR, fn)
        try:
            d = _viewer_load_master(fp)
        except Exception:
            continue
        msgs = d.get("messages") or []
        uid = str(d.get("uid") or "")
        out.append({
            "file": fn,
            "conversation": d.get("conversation") or fn[7:-5],
            "uid": uid,
            "count": len(msgs),
            "first": msgs[0].get("time") if msgs else None,
            "last": msgs[-1].get("time") if msgs else None,
            "recalled": sum(1 for m in msgs if m.get("missingSince")),
            "updatedAt": d.get("updatedAt"),
            "media": _viewer_media_info(uid),
        })
    out.sort(key=lambda m: m.get("updatedAt") or "", reverse=True)
    return {"masters": out}


def _viewer_master_path(uid):
    uid = str(uid or "")
    if not uid:
        return None
    try:
        names = sorted(os.listdir(EXPORT_DIR))
    except OSError:
        return None
    for fn in names:
        if not (fn.startswith("master_") and fn.endswith(".json")):
            continue
        fp = os.path.join(EXPORT_DIR, fn)
        try:
            d = _viewer_load_master(fp)
        except Exception:
            continue
        if str(d.get("uid") or "") == uid:
            return fp
    return None


def _viewer_media_map(msgs, uid):
    """Assign media-master files to photo/video messages by msgType + send-time.
    One shared implementation for the viewer payload and the media audit."""
    photo_list = []  # (epoch_seconds, rel, msgType)
    md = _viewer_media_dir_for(uid)
    if md:
        try:
            idx = json.load(open(os.path.join(md, "index.json"), encoding="utf-8"))
            for v in (idx.get("files") or {}).values():
                rel = v.get("file") or ""
                if rel.startswith("photos/"):
                    mt = "2"
                elif rel.startswith("videos/"):
                    mt = "18"
                else:
                    mt = None
                if not mt:
                    continue
                sa = v.get("sentAt")
                if not sa:
                    continue
                try:
                    import calendar
                    ep = calendar.timegm(tuple(sa[:6]) + (0, 0, -1))
                    photo_list.append((ep, rel, mt))
                except Exception:
                    continue
            photo_list.sort()
        except Exception:
            pass
    used = set()
    assign = []  # per-message media file (parallel to msgs)
    import bisect
    eps = [p[0] for p in photo_list]
    for m in msgs:
        rel = None
        if str(m.get("msgType") or "") in ("2", "18"):
            t = m.get("time") or ""
            try:
                import calendar
                mt = time.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
                ep = calendar.timegm(mt)
                i = bisect.bisect_left(eps, ep)
                # collect all unused candidates within +/-2s window
                cands = []
                j = i - 1
                while j >= 0 and ep - photo_list[j][0] <= 2:
                    cands.append(photo_list[j]); j -= 1
                j = i
                while j < len(photo_list) and photo_list[j][0] - ep <= 2:
                    cands.append(photo_list[j]); j += 1
                cands.sort(key=lambda p: abs(p[0] - ep))
                for c in cands:
                    if c[1] not in used and c[2] == str(m.get("msgType") or ""):
                        rel = c[1]
                        used.add(rel)
                        break
            except Exception:
                rel = None
        assign.append(rel)
    return assign


def _viewer_media_stats(uid, max_probe=80):
    """Audit every photo/video message in the master:
      saved      -> file exists in media master (independent of any CDN link)
      linkAlive  -> CDN link still downloadable (probed in a background thread)
      linkDead   -> probed but gone
      unknown    -> not probed yet (audit still running or not started)
    lost = linkDead (no local file AND dead link -> unrecoverable).
    Results cached next to the master JSON; key = first CDN URL of the message."""
    fp = _viewer_master_path(uid)
    if not fp:
        return None
    try:
        d = _viewer_load_master(fp)
    except Exception:
        return None
    msgs = d.get("messages") or []
    mmsgs = [m for m in msgs if str(m.get("msgType") or "") in ("2", "18")]
    assign = _viewer_media_map(mmsgs, uid)
    saved = sum(1 for r in assign if r)
    # link probe: URL per message from stored raw payload
    import calendar as _cal
    def _urls_of(m):
        raw = m.get("text") or ""
        if not raw.lstrip().startswith("{"):
            return []
        try:
            j = json.loads(raw)
        except Exception:
            return []
        out = []
        for k in ("oriUrl", "normalUrl", "hdUrl", "thumbUrl", "href"):
            u = j.get(k)
            if isinstance(u, str) and u.startswith("http") and u not in out:
                out.append(u)
        try:
            pj = json.loads(j.get("params") or "{}")
            u = pj.get("hd")
            if isinstance(u, str) and u.startswith("http") and u not in out:
                out.append(u)
        except Exception:
            pass
        return out
    cache = {}
    try:
        cache = json.load(open(fp + ".mediastats", encoding="utf-8"))
    except Exception:
        pass
    import urllib.error
    def _probe(url):
        try:
            req = urllib.request.Request(url, headers=UA_HDRS)
            req.add_header("Range", "bytes=0-1")
            with urllib.request.urlopen(req, timeout=10) as r:
                return 200 <= getattr(r, "status", 200) < 400
        except urllib.error.HTTPError as e:
            # some CDNs reject Range but still serve the file
            if e.code in (403, 404, 410):
                return False
            return e.code < 500
        except Exception:
            return False
    # background audit: probe every not-yet-cached link, 12 workers, atomic cache writes
    todo = []
    for m, rel in zip(mmsgs, assign):
        if rel:
            continue
        urls = _urls_of(m)
        if urls and urls[0] not in cache and urls[0] not in todo:
            todo.append(urls[0])
    st = _AUDIT_STATE.setdefault(uid, {"running": False})
    if todo and not st.get("running"):
        st["running"] = True
        st["total"] = len(todo)
        st["done"] = 0
        def _audit():
            done = 0
            try:
                with ThreadPoolExecutor(max_workers=12) as ex:
                    for url, ok in zip(todo, ex.map(_probe, todo)):
                        cache[url] = {"alive": bool(ok),
                                      "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                        done += 1
                        if done % 40 == 0 or done == len(todo):
                            st["done"] = done
                            tmp = fp + ".mediastats.tmp"
                            try:
                                with open(tmp, "w", encoding="utf-8") as f:
                                    json.dump(cache, f)
                                os.replace(tmp, fp + ".mediastats")
                            except Exception:
                                pass
            finally:
                st["running"] = False
                try:
                    with open(fp + ".mediastats", "w", encoding="utf-8") as f:
                        json.dump(cache, f)
                except Exception:
                    pass
        threading.Thread(target=_audit, daemon=True, name="media-audit-" + str(uid)).start()
    n_saved = n_alive = n_dead = n_unknown = 0
    for m, rel in zip(mmsgs, assign):
        if rel:
            n_saved += 1
            continue
        urls = _urls_of(m)
        if not urls:
            n_unknown += 1
            continue
        ent = cache.get(urls[0])
        if ent is None:
            n_unknown += 1
        elif ent.get("alive"):
            n_alive += 1
        else:
            n_dead += 1
    return {"mediaMsgs": len(mmsgs), "photos": sum(1 for m in mmsgs
                if str(m.get("msgType")) == "2"),
            "videos": sum(1 for m in mmsgs if str(m.get("msgType")) == "18"),
            "saved": n_saved, "linkAlive": n_alive, "linkDead": n_dead,
            "unknown": n_unknown, "linkProbed": len(cache),
            "auditing": bool(st.get("running")),
            "auditDone": st.get("done", 0), "auditTotal": st.get("total", 0),
            "lost": n_dead}


def _viewer_api_master(uid, d1, d2):
    fp = _viewer_master_path(uid)
    if not fp:
        return {"error": "master not found for uid " + str(uid)}
    d = _viewer_load_master(fp)
    msgs = d.get("messages") or []
    # map photo/video send-times -> media-master files (for inline display in Zalo mode)
    photo_list = []  # (epoch_seconds, rel, msgType)
    md = _viewer_media_dir_for(uid)
    if md:
        try:
            idx = json.load(open(os.path.join(md, "index.json"), encoding="utf-8"))
            for v in (idx.get("files") or {}).values():
                rel = v.get("file") or ""
                if rel.startswith("photos/"):
                    mt = "2"
                elif rel.startswith("videos/"):
                    mt = "18"
                else:
                    mt = None
                if not mt:
                    continue
                sa = v.get("sentAt")
                if not sa:
                    continue
                try:
                    import calendar
                    ep = calendar.timegm(tuple(sa[:6]) + (0, 0, -1))
                    photo_list.append((ep, rel, mt))
                except Exception:
                    continue
            photo_list.sort()
        except Exception:
            pass
    assign = _viewer_media_map(msgs, uid)
    keys = ("time", "sender", "msgType", "text", "msgId", "firstSeen",
            "lastSeen", "missingSince", "copies", "sources")  # noqa: E501
    out = []
    for m, rel in zip(msgs, assign):
        t = m.get("time") or ""
        if d1 and t[:10] < d1:
            continue
        if d2 and t[:10] > d2:
            continue
        rec = {k: m.get(k) for k in keys}
        if rel:
            rec["f"] = rel
        out.append(rec)
    # NOTE: media payload gets msgType per file so the UI can pick the right renderer
    return {"conversation": d.get("conversation"), "uid": str(d.get("uid") or ""),
            "masterFile": os.path.basename(fp), "total": len(msgs), "count": len(out),
            "messages": out, "media": _viewer_media_info(uid, with_files=True),
            "mediaStats": _viewer_media_stats(uid)}


def _viewer_mediafile(self, qs):
    uid = qs.get("uid", [""])[0]
    rel = qs.get("f", [""])[0]
    d = _viewer_media_dir_for(uid)
    if not d:
        self._json({"error": "no media master for uid"}, 404); return
    fp = os.path.realpath(os.path.join(d, rel))
    base = os.path.realpath(d)
    if not fp.startswith(base + os.sep):
        self._json({"error": "bad path"}, 400); return
    if not os.path.isfile(fp):
        self._json({"error": "not found"}, 404); return
    ctype = VIEWER_MIME.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
    size = os.path.getsize(fp)
    start, end = 0, size - 1
    rng = self.headers.get("Range")
    code = 200
    if rng and rng.startswith("bytes="):
        try:
            s, e = rng[6:].split("-", 1)
            start = int(s) if s else 0
            end = int(e) if e else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            code = 206
        except ValueError:
            pass
    self.send_response(code)
    self.send_header("Content-Type", ctype)
    self.send_header("Accept-Ranges", "bytes")
    if code == 206:
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    self.send_header("Content-Length", str(end - start + 1))
    self.end_headers()
    with open(fp, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(262144, remaining))
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (ConnectionAbortedError, BrokenPipeError):
                return
            remaining -= len(chunk)


def _viewer_fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


VIEWER_HTML = r"""<!DOCTYPE html>
<html lang="vi"><head><meta charset="utf-8">
<title>Zalo Masters — Viewer</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--tx:#e2e8f0;--dim:#94a3b8;--acc:#38bdf8;--warn:#f87171;--ok:#4ade80}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.45 system-ui,'Segoe UI',Arial}
a{color:var(--acc)}
#wrap{display:flex;height:100vh}
#side{width:290px;min-width:220px;border-right:1px solid var(--line);overflow-y:auto;padding:12px;background:#0c1322}
#side h1{font-size:16px;margin:0 0 2px}#side .sub{color:var(--dim);font-size:11px;margin-bottom:12px}
.mcard{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;cursor:pointer;background:var(--panel)}
.mcard:hover{border-color:var(--acc)}.mcard.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.mcard .nm{font-weight:600;font-size:15px}.mcard .meta{color:var(--dim);font-size:11.5px;margin-top:4px}
.badge{display:inline-block;background:#334155;border-radius:20px;padding:1px 8px;font-size:10.5px;margin-right:4px;margin-top:5px}
.badge.warn{background:#7f1d1d;color:#fecaca}.badge.acc{background:#0c4a6e;color:#bae6fd}
#main{flex:1;display:flex;flex-direction:column;min-width:0}
#bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line);background:#0c1322}
#bar input[type=date],#bar input[type=text]{background:var(--panel);border:1px solid var(--line);color:var(--tx);border-radius:8px;padding:6px 8px;font-size:13px}
#bar input[type=text]{width:220px}
.chip{background:var(--panel);border:1px solid var(--line);color:var(--dim);border-radius:20px;padding:5px 12px;cursor:pointer;font-size:12px}
.chip.on,.chip:hover{color:var(--tx);border-color:var(--acc)}
label.chk{color:var(--dim);font-size:12.5px;display:flex;align-items:center;gap:5px;cursor:pointer}
#tabs{display:flex;gap:2px;padding:0 14px;border-bottom:1px solid var(--line);background:#0c1322}
#tabs button{background:none;border:none;color:var(--dim);padding:10px 16px;cursor:pointer;font-size:14px;border-bottom:2px solid transparent}
#tabs button.on{color:var(--tx);border-bottom-color:var(--acc)}
#content{flex:1;overflow-y:auto;padding:6px 14px 40px}
.dayhdr{position:sticky;top:0;background:var(--bg);color:var(--acc);font-weight:600;font-size:12.5px;padding:10px 2px 6px;z-index:2;border-bottom:1px solid var(--line);margin-top:8px}
.msg{display:flex;gap:10px;padding:5px 2px;border-bottom:1px solid #1a2438}
.msg .t{color:var(--dim);font-size:11.5px;min-width:118px;padding-top:2px}
.msg .w{min-width:92px;text-align:right;color:var(--dim);font-size:12px;padding-top:2px}
.msg .s{font-weight:600;color:var(--acc);font-size:12.5px}
.msg .x{white-space:pre-wrap;word-break:break-word}
.msg.media .x{color:var(--dim);font-style:italic}
.msg.recalled{background:#2a1215;border-radius:8px}.msg.recalled .x{color:var(--warn);text-decoration:line-through}
.rec{color:var(--warn);font-size:11px;margin-top:2px}
.cp{color:var(--dim);font-size:10.5px}
.empty{color:var(--dim);text-align:center;padding:60px 20px}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;padding:14px 0}
.cell{background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.cell img,.cell video{width:100%;height:160px;object-fit:cover;display:block;background:#000}
.cell .cap{padding:7px 9px;font-size:11px;color:var(--dim);word-break:break-all}
.cell .cap b{color:var(--tx);font-size:12px}
#statchip{color:var(--dim);font-size:12px;margin-left:auto}
.more{display:block;margin:14px auto;background:var(--panel);border:1px solid var(--acc);color:var(--acc);border-radius:20px;padding:8px 22px;cursor:pointer}
/* ---- Zalo-style chat mode ---- */
.zwrap{max-width:760px;margin:0 auto}
.zbar{display:flex;gap:8px;align-items:center;justify-content:center;background:#009ef7;color:#fff;padding:10px 14px;font-weight:600;font-size:14.5px;position:sticky;top:0;z-index:3;border-radius:0 0 10px 10px;box-shadow:0 1px 4px rgba(0,0,0,.25)}
.zbar .zback{cursor:pointer;font-size:17px;background:none;border:none;color:#fff}
.zbar .zme{background:#0087d4;border:1px solid #7fd0ff;color:#fff;border-radius:8px;font-size:12px;padding:3px 6px;font-weight:400}
.zscroll{padding:12px 10px 30px}
.zday{text-align:center;margin:14px 0 8px}
.zday span{background:rgba(0,0,0,.14);color:#e8f4ff;border-radius:16px;padding:3px 12px;font-size:11.5px}
.zrow{display:flex;margin:3px 0;align-items:flex-end}
.zrow.me{justify-content:flex-end}
.zrow .zav{width:30px;height:30px;border-radius:50%;background:#cbd5e1;color:#0f172a;font-weight:700;font-size:13px;display:flex;align-items:center;justify-content:center;margin-right:8px;flex:none;overflow:hidden}
.zbubble{max-width:66%;background:#fff;color:#000;border-radius:10px;padding:7px 10px 6px;box-shadow:0 1px 1px rgba(0,0,0,.18);font-size:13.6px;line-height:1.45;white-space:pre-wrap;word-break:break-word;position:relative}
.zrow.me .zbubble{background:#e0fbdc}
.zbubble.zmedia{padding:3px}
.zbubble.zmedia img,.zbubble.zmedia video{display:block;max-width:320px;max-height:320px;border-radius:8px;width:auto;height:auto}
.zbubble .ztime{float:right;margin:5px -2px -1px 8px;color:#8fa6b5;font-size:10.5px;white-space:nowrap}
.zrow.me .zbubble .ztime{color:#57b34f}
.zbubble .zsend{color:#0068b3;font-weight:600;font-size:12px;display:block;margin-bottom:1px}
.zbubble .zmedia-meta{display:block;font-size:10.5px;color:#8fa6b5;padding:4px 4px 2px;word-break:break-all}
.zbubble.zrecalled{background:#fdecea;color:#b3261e;text-decoration:line-through}
.zbubble .zrec{display:block;color:#d93025;font-size:10.5px;text-decoration:none}
.zmsgimg{display:block;max-width:320px;max-height:320px;border-radius:8px;cursor:zoom-in}
.zmsgvid{display:block;max-width:320px;max-height:320px;border-radius:8px;background:#000}
#mstat{display:none;flex-wrap:wrap;gap:8px 14px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px;margin:10px 0;font-size:12.5px;color:var(--dim)}
.ms-title{color:var(--tx);font-weight:600}
.ms-seg b{color:var(--tx)}
.ms-ok b{color:#4ade80}.ms-alive b{color:#38bdf8}.ms-dead b{color:#f87171}.ms-unk b{color:#fbbf24}
.ms-audit{color:#fbbf24;font-style:italic}
#zlightbox{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;z-index:99;cursor:zoom-out}
#zlightbox img{max-width:94vw;max-height:94vh}
.zempty{color:var(--dim);text-align:center;padding:60px 20px}
@media print{body{background:#fff}#wrap{height:auto}#side,#bar,#tabs,.zbar .zback,#zlightbox{display:none!important}#content{overflow:visible;padding:0}.zscroll{max-width:760px}}
</style></head><body>
<div id="wrap">
 <div id="side"><h1>📖 Master Viewer</h1><div class="sub">Dữ liệu đọc từ exports\ — không cần Zalo đang chạy</div><div id="mlist"></div></div>
 <div id="main">
  <div id="bar">
   <input type="date" id="d1"> <span style="color:var(--dim)">→</span> <input type="date" id="d2">
   <button class="chip on" data-r="all">Tất cả</button><button class="chip" data-r="7">7 ngày</button><button class="chip" data-r="30">30 ngày</button><button class="chip" data-r="90">90 ngày</button><button class="chip" data-r="y">Năm nay</button>
   <input type="text" id="q" placeholder="🔍 Tìm trong kết quả…">
   <label class="chk"><input type="checkbox" id="or"> Chỉ tin bị thu hồi</label>
   <span id="statchip"></span>
  </div>
  <div id="mstat"></div>
  <div id="tabs"><button id="tmsg" class="on">💬 Tin nhắn</button><button id="tmed">🖼 Media</button><button id="tzalo">💬 Dạng Zalo</button><span style="flex:1"></span><button id="tcap">📷 Chụp màn hình (PNG)</button></div>
  <div id="content"></div>
 </div>
</div>
<div id="zlightbox"><img alt=""></div>
<script>
/*! html2canvas 1.4.1 (MIT) https://html2canvas.hertzen.com - vendored for full-offline use */
/*!
 * html2canvas 1.4.1 <https://html2canvas.hertzen.com>
 * Copyright (c) 2022 Niklas von Hertzen <https://hertzen.com>
 * Released under MIT License
 */
!function(A,e){"object"==typeof exports&&"undefined"!=typeof module?module.exports=e():"function"==typeof define&&define.amd?define(e):(A="undefined"!=typeof globalThis?globalThis:A||self).html2canvas=e()}(this,function(){"use strict";
/*! *****************************************************************************
    Copyright (c) Microsoft Corporation.

    Permission to use, copy, modify, and/or distribute this software for any
    purpose with or without fee is hereby granted.

    THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
    REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
    AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
    INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
    LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
    OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
    PERFORMANCE OF THIS SOFTWARE.
    ***************************************************************************** */var r=function(A,e){return(r=Object.setPrototypeOf||{__proto__:[]}instanceof Array&&function(A,e){A.__proto__=e}||function(A,e){for(var t in e)Object.prototype.hasOwnProperty.call(e,t)&&(A[t]=e[t])})(A,e)};function A(A,e){if("function"!=typeof e&&null!==e)throw new TypeError("Class extends value "+String(e)+" is not a constructor or null");function t(){this.constructor=A}r(A,e),A.prototype=null===e?Object.create(e):(t.prototype=e.prototype,new t)}var h=function(){return(h=Object.assign||function(A){for(var e,t=1,r=arguments.length;t<r;t++)for(var B in e=arguments[t])Object.prototype.hasOwnProperty.call(e,B)&&(A[B]=e[B]);return A}).apply(this,arguments)};function a(A,s,o,i){return new(o=o||Promise)(function(t,e){function r(A){try{n(i.next(A))}catch(A){e(A)}}function B(A){try{n(i.throw(A))}catch(A){e(A)}}function n(A){var e;A.done?t(A.value):((e=A.value)instanceof o?e:new o(function(A){A(e)})).then(r,B)}n((i=i.apply(A,s||[])).next())})}function H(t,r){var B,n,s,o={label:0,sent:function(){if(1&s[0])throw s[1];return s[1]},trys:[],ops:[]},A={next:e(0),throw:e(1),return:e(2)};return"function"==typeof Symbol&&(A[Symbol.iterator]=function(){return this}),A;function e(e){return function(A){return function(e){if(B)throw new TypeError("Generator is already executing.");for(;o;)try{if(B=1,n&&(s=2&e[0]?n.return:e[0]?n.throw||((s=n.return)&&s.call(n),0):n.next)&&!(s=s.call(n,e[1])).done)return s;switch(n=0,(e=s?[2&e[0],s.value]:e)[0]){case 0:case 1:s=e;break;case 4:return o.label++,{value:e[1],done:!1};case 5:o.label++,n=e[1],e=[0];continue;case 7:e=o.ops.pop(),o.trys.pop();continue;default:if(!(s=0<(s=o.trys).length&&s[s.length-1])&&(6===e[0]||2===e[0])){o=0;continue}if(3===e[0]&&(!s||e[1]>s[0]&&e[1]<s[3])){o.label=e[1];break}if(6===e[0]&&o.label<s[1]){o.label=s[1],s=e;break}if(s&&o.label<s[2]){o.label=s[2],o.ops.push(e);break}s[2]&&o.ops.pop(),o.trys.pop();continue}e=r.call(t,o)}catch(A){e=[6,A],n=0}finally{B=s=0}if(5&e[0])throw e[1];return{value:e[0]?e[1]:void 0,done:!0}}([e,A])}}}function t(A,e,t){if(t||2===arguments.length)for(var r,B=0,n=e.length;B<n;B++)!r&&B in e||((r=r||Array.prototype.slice.call(e,0,B))[B]=e[B]);return A.concat(r||e)}var d=(B.prototype.add=function(A,e,t,r){return new B(this.left+A,this.top+e,this.width+t,this.height+r)},B.fromClientRect=function(A,e){return new B(e.left+A.windowBounds.left,e.top+A.windowBounds.top,e.width,e.height)},B.fromDOMRectList=function(A,e){e=Array.from(e).find(function(A){return 0!==A.width});return e?new B(e.left+A.windowBounds.left,e.top+A.windowBounds.top,e.width,e.height):B.EMPTY},B.EMPTY=new B(0,0,0,0),B);function B(A,e,t,r){this.left=A,this.top=e,this.width=t,this.height=r}for(var f=function(A,e){return d.fromClientRect(A,e.getBoundingClientRect())},Q=function(A){for(var e=[],t=0,r=A.length;t<r;){var B,n=A.charCodeAt(t++);55296<=n&&n<=56319&&t<r?56320==(64512&(B=A.charCodeAt(t++)))?e.push(((1023&n)<<10)+(1023&B)+65536):(e.push(n),t--):e.push(n)}return e},g=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];if(String.fromCodePoint)return String.fromCodePoint.apply(String,A);var t=A.length;if(!t)return"";for(var r=[],B=-1,n="";++B<t;){var s=A[B];s<=65535?r.push(s):(s-=65536,r.push(55296+(s>>10),s%1024+56320)),(B+1===t||16384<r.length)&&(n+=String.fromCharCode.apply(String,r),r.length=0)}return n},e="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",n="undefined"==typeof Uint8Array?[]:new Uint8Array(256),s=0;s<e.length;s++)n[e.charCodeAt(s)]=s;for(var o="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",c="undefined"==typeof Uint8Array?[]:new Uint8Array(256),i=0;i<o.length;i++)c[o.charCodeAt(i)]=i;function w(A,e,t){return A.slice?A.slice(e,t):new Uint16Array(Array.prototype.slice.call(A,e,t))}var U=(l.prototype.get=function(A){var e;if(0<=A){if(A<55296||56319<A&&A<=65535)return e=this.index[A>>5],this.data[e=(e<<2)+(31&A)];if(A<=65535)return e=this.index[2048+(A-55296>>5)],this.data[e=(e<<2)+(31&A)];if(A<this.highStart)return e=this.index[e=2080+(A>>11)],e=this.index[e+=A>>5&63],this.data[e=(e<<2)+(31&A)];if(A<=1114111)return this.data[this.highValueIndex]}return this.errorValue},l);function l(A,e,t,r,B,n){this.initialValue=A,this.errorValue=e,this.highStart=t,this.highValueIndex=r,this.index=B,this.data=n}for(var C="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",u="undefined"==typeof Uint8Array?[]:new Uint8Array(256),F=0;F<C.length;F++)u[C.charCodeAt(F)]=F;function p(A,e,t,r){var B=r[t];if(Array.isArray(A)?-1!==A.indexOf(B):A===B)for(var n=t;n<=r.length;){if((o=r[++n])===e)return 1;if(o!==D)break}if(B===D)for(n=t;0<n;){var s=r[--n];if(Array.isArray(A)?-1!==A.indexOf(s):A===s)for(var o,i=t;i<=r.length;){if((o=r[++i])===e)return 1;if(o!==D)break}if(s!==D)break}}function E(A,e){for(var t=A;0<=t;){var r=e[t];if(r!==D)return r;t--}return 0}function I(t,A){var e=(B=function(A,r){void 0===r&&(r="strict");var B=[],n=[],s=[];return A.forEach(function(A,e){var t=rA.get(A);if(50<t?(s.push(!0),t-=50):s.push(!1),-1!==["normal","auto","loose"].indexOf(r)&&-1!==[8208,8211,12316,12448].indexOf(A))return n.push(e),B.push(16);if(4!==t&&11!==t)return n.push(e),31===t?B.push("strict"===r?O:q):t===AA||29===t?B.push(J):43===t?131072<=A&&A<=196605||196608<=A&&A<=262141?B.push(q):B.push(J):void B.push(t);if(0===e)return n.push(e),B.push(J);t=B[e-1];return-1===iA.indexOf(t)?(n.push(n[e-1]),B.push(t)):(n.push(e),B.push(J))}),[n,B,s]}(t,(A=A||{lineBreak:"normal",wordBreak:"normal"}).lineBreak))[0],r=B[1],B=B[2];return[e,r="break-all"===A.wordBreak||"break-word"===A.wordBreak?r.map(function(A){return-1!==[R,J,AA].indexOf(A)?q:A}):r,"keep-all"===A.wordBreak?B.map(function(A,e){return A&&19968<=t[e]&&t[e]<=40959}):void 0]}var y,K,m,L,b,D=10,v=13,x=15,M=17,S=18,T=19,G=20,O=21,V=22,k=24,R=25,N=26,P=27,X=28,J=30,Y=32,W=33,Z=34,_=35,q=37,j=38,z=39,$=40,AA=42,eA=[9001,65288],tA="×",rA=(m=function(A){var e,t,r,B,n=.75*A.length,s=A.length,o=0;"="===A[A.length-1]&&(n--,"="===A[A.length-2]&&n--);for(var n=new("undefined"!=typeof ArrayBuffer&&"undefined"!=typeof Uint8Array&&void 0!==Uint8Array.prototype.slice?ArrayBuffer:Array)(n),i=Array.isArray(n)?n:new Uint8Array(n),Q=0;Q<s;Q+=4)e=c[A.charCodeAt(Q)],t=c[A.charCodeAt(Q+1)],r=c[A.charCodeAt(Q+2)],B=c[A.charCodeAt(Q+3)],i[o++]=e<<2|t>>4,i[o++]=(15&t)<<4|r>>2,i[o++]=(3&r)<<6|63&B;return n}(y="KwAAAAAAAAAACA4AUD0AADAgAAACAAAAAAAIABAAGABAAEgAUABYAGAAaABgAGgAYgBqAF8AZwBgAGgAcQB5AHUAfQCFAI0AlQCdAKIAqgCyALoAYABoAGAAaABgAGgAwgDKAGAAaADGAM4A0wDbAOEA6QDxAPkAAQEJAQ8BFwF1AH0AHAEkASwBNAE6AUIBQQFJAVEBWQFhAWgBcAF4ATAAgAGGAY4BlQGXAZ8BpwGvAbUBvQHFAc0B0wHbAeMB6wHxAfkBAQIJAvEBEQIZAiECKQIxAjgCQAJGAk4CVgJeAmQCbAJ0AnwCgQKJApECmQKgAqgCsAK4ArwCxAIwAMwC0wLbAjAA4wLrAvMC+AIAAwcDDwMwABcDHQMlAy0DNQN1AD0DQQNJA0kDSQNRA1EDVwNZA1kDdQB1AGEDdQBpA20DdQN1AHsDdQCBA4kDkQN1AHUAmQOhA3UAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AKYDrgN1AHUAtgO+A8YDzgPWAxcD3gPjA+sD8wN1AHUA+wMDBAkEdQANBBUEHQQlBCoEFwMyBDgEYABABBcDSARQBFgEYARoBDAAcAQzAXgEgASIBJAEdQCXBHUAnwSnBK4EtgS6BMIEyAR1AHUAdQB1AHUAdQCVANAEYABgAGAAYABgAGAAYABgANgEYADcBOQEYADsBPQE/AQEBQwFFAUcBSQFLAU0BWQEPAVEBUsFUwVbBWAAYgVgAGoFcgV6BYIFigWRBWAAmQWfBaYFYABgAGAAYABgAKoFYACxBbAFuQW6BcEFwQXHBcEFwQXPBdMF2wXjBeoF8gX6BQIGCgYSBhoGIgYqBjIGOgZgAD4GRgZMBmAAUwZaBmAAYABgAGAAYABgAGAAYABgAGAAYABgAGIGYABpBnAGYABgAGAAYABgAGAAYABgAGAAYAB4Bn8GhQZgAGAAYAB1AHcDFQSLBmAAYABgAJMGdQA9A3UAmwajBqsGqwaVALMGuwbDBjAAywbSBtIG1QbSBtIG0gbSBtIG0gbdBuMG6wbzBvsGAwcLBxMHAwcbByMHJwcsBywHMQcsB9IGOAdAB0gHTgfSBkgHVgfSBtIG0gbSBtIG0gbSBtIG0gbSBiwHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAdgAGAALAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAdbB2MHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsB2kH0gZwB64EdQB1AHUAdQB1AHUAdQB1AHUHfQdgAIUHjQd1AHUAlQedB2AAYAClB6sHYACzB7YHvgfGB3UAzgfWBzMB3gfmB1EB7gf1B/0HlQENAQUIDQh1ABUIHQglCBcDLQg1CD0IRQhNCEEDUwh1AHUAdQBbCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIaQhjCGQIZQhmCGcIaAhpCGMIZAhlCGYIZwhoCGkIYwhkCGUIZghnCGgIcAh3CHoIMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwAIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIgggwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAALAcsBywHLAcsBywHLAcsBywHLAcsB4oILAcsB44I0gaWCJ4Ipgh1AHUAqgiyCHUAdQB1AHUAdQB1AHUAdQB1AHUAtwh8AXUAvwh1AMUIyQjRCNkI4AjoCHUAdQB1AO4I9gj+CAYJDgkTCS0HGwkjCYIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiCCIIIggiAAIAAAAFAAYABgAGIAXwBgAHEAdQBFAJUAogCyAKAAYABgAEIA4ABGANMA4QDxAMEBDwE1AFwBLAE6AQEBUQF4QkhCmEKoQrhCgAHIQsAB0MLAAcABwAHAAeDC6ABoAHDCwMMAAcABwAHAAdDDGMMAAcAB6MM4wwjDWMNow3jDaABoAGgAaABoAGgAaABoAGgAaABoAGgAaABoAGgAaABoAGgAaABoAEjDqABWw6bDqABpg6gAaABoAHcDvwOPA+gAaABfA/8DvwO/A78DvwO/A78DvwO/A78DvwO/A78DvwO/A78DvwO/A78DvwO/A78DvwO/A78DvwO/A78DpcPAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcAB9cPKwkyCToJMAB1AHUAdQBCCUoJTQl1AFUJXAljCWcJawkwADAAMAAwAHMJdQB2CX4JdQCECYoJjgmWCXUAngkwAGAAYABxAHUApgn3A64JtAl1ALkJdQDACTAAMAAwADAAdQB1AHUAdQB1AHUAdQB1AHUAowYNBMUIMAAwADAAMADICcsJ0wnZCRUE4QkwAOkJ8An4CTAAMAB1AAAKvwh1AAgKDwoXCh8KdQAwACcKLgp1ADYKqAmICT4KRgowADAAdQB1AE4KMAB1AFYKdQBeCnUAZQowADAAMAAwADAAMAAwADAAMAAVBHUAbQowADAAdQC5CXUKMAAwAHwBxAijBogEMgF9CoQKiASMCpQKmgqIBKIKqgquCogEDQG2Cr4KxgrLCjAAMADTCtsKCgHjCusK8Qr5CgELMAAwADAAMAB1AIsECQsRC3UANAEZCzAAMAAwADAAMAB1ACELKQswAHUANAExCzkLdQBBC0kLMABRC1kLMAAwADAAMAAwADAAdQBhCzAAMAAwAGAAYABpC3ELdwt/CzAAMACHC4sLkwubC58Lpwt1AK4Ltgt1APsDMAAwADAAMAAwADAAMAAwAL4LwwvLC9IL1wvdCzAAMADlC+kL8Qv5C/8LSQswADAAMAAwADAAMAAwADAAMAAHDDAAMAAwADAAMAAODBYMHgx1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1ACYMMAAwADAAdQB1AHUALgx1AHUAdQB1AHUAdQA2DDAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwAHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AD4MdQBGDHUAdQB1AHUAdQB1AEkMdQB1AHUAdQB1AFAMMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwAHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQBYDHUAdQB1AF8MMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUA+wMVBGcMMAAwAHwBbwx1AHcMfwyHDI8MMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAYABgAJcMMAAwADAAdQB1AJ8MlQClDDAAMACtDCwHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsB7UMLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHdQB1AHUAdQB1AHUAdQB1AHUAdQB1AHUAdQB1AA0EMAC9DDAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAsBywHLAcsBywHLAcsBywHLQcwAMEMyAwsBywHLAcsBywHLAcsBywHLAcsBywHzAwwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwAHUAdQB1ANQM2QzhDDAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMABgAGAAYABgAGAAYABgAOkMYADxDGAA+AwADQYNYABhCWAAYAAODTAAMAAwADAAFg1gAGAAHg37AzAAMAAwADAAYABgACYNYAAsDTQNPA1gAEMNPg1LDWAAYABgAGAAYABgAGAAYABgAGAAUg1aDYsGVglhDV0NcQBnDW0NdQ15DWAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAlQCBDZUAiA2PDZcNMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAnw2nDTAAMAAwADAAMAAwAHUArw23DTAAMAAwADAAMAAwADAAMAAwADAAMAB1AL8NMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAB1AHUAdQB1AHUAdQDHDTAAYABgAM8NMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAA1w11ANwNMAAwAD0B5A0wADAAMAAwADAAMADsDfQN/A0EDgwOFA4wABsOMAAwADAAMAAwADAAMAAwANIG0gbSBtIG0gbSBtIG0gYjDigOwQUuDsEFMw7SBjoO0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIGQg5KDlIOVg7SBtIGXg5lDm0OdQ7SBtIGfQ6EDooOjQ6UDtIGmg6hDtIG0gaoDqwO0ga0DrwO0gZgAGAAYADEDmAAYAAkBtIGzA5gANIOYADaDokO0gbSBt8O5w7SBu8O0gb1DvwO0gZgAGAAxA7SBtIG0gbSBtIGYABgAGAAYAAED2AAsAUMD9IG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIGFA8sBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAccD9IGLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHJA8sBywHLAcsBywHLAccDywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywPLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAc0D9IG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIGLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAccD9IG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIGFA8sBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHLAcsBywHPA/SBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gbSBtIG0gYUD0QPlQCVAJUAMAAwADAAMACVAJUAlQCVAJUAlQCVAEwPMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAA//8EAAQABAAEAAQABAAEAAQABAANAAMAAQABAAIABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQACgATABcAHgAbABoAHgAXABYAEgAeABsAGAAPABgAHABLAEsASwBLAEsASwBLAEsASwBLABgAGAAeAB4AHgATAB4AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQABYAGwASAB4AHgAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAWAA0AEQAeAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAAQABAAEAAQABAAFAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAJABYAGgAbABsAGwAeAB0AHQAeAE8AFwAeAA0AHgAeABoAGwBPAE8ADgBQAB0AHQAdAE8ATwAXAE8ATwBPABYAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAFAAUABQAFAAUABQAFAAUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAFAAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAeAB4AHgAeAFAATwBAAE8ATwBPAEAATwBQAFAATwBQAB4AHgAeAB4AHgAeAB0AHQAdAB0AHgAdAB4ADgBQAFAAUABQAFAAHgAeAB4AHgAeAB4AHgBQAB4AUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4ABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAJAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAkACQAJAAkACQAJAAkABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAeAB4AHgAeAFAAHgAeAB4AKwArAFAAUABQAFAAGABQACsAKwArACsAHgAeAFAAHgBQAFAAUAArAFAAKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4ABAAEAAQABAAEAAQABAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAUAAeAB4AHgAeAB4AHgBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAYAA0AKwArAB4AHgAbACsABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQADQAEAB4ABAAEAB4ABAAEABMABAArACsAKwArACsAKwArACsAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAKwArACsAKwBWAFYAVgBWAB4AHgArACsAKwArACsAKwArACsAKwArACsAHgAeAB4AHgAeAB4AHgAeAB4AGgAaABoAGAAYAB4AHgAEAAQABAAEAAQABAAEAAQABAAEAAQAEwAEACsAEwATAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABABLAEsASwBLAEsASwBLAEsASwBLABoAGQAZAB4AUABQAAQAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQABMAUAAEAAQABAAEAAQABAAEAB4AHgAEAAQABAAEAAQABABQAFAABAAEAB4ABAAEAAQABABQAFAASwBLAEsASwBLAEsASwBLAEsASwBQAFAAUAAeAB4AUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwAeAFAABABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAFAAKwArACsAKwArACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQAUABQAB4AHgAYABMAUAArACsABAAbABsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAFAABAAEAAQABAAEAFAABAAEAAQAUAAEAAQABAAEAAQAKwArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAArACsAHgArAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwArACsAKwArACsAKwArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAB4ABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAUAAEAAQABAAEAAQABAAEAFAAUABQAFAAUABQAFAAUABQAFAABAAEAA0ADQBLAEsASwBLAEsASwBLAEsASwBLAB4AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAArAFAAUABQAFAAUABQAFAAUAArACsAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUAArACsAKwBQAFAAUABQACsAKwAEAFAABAAEAAQABAAEAAQABAArACsABAAEACsAKwAEAAQABABQACsAKwArACsAKwArACsAKwAEACsAKwArACsAUABQACsAUABQAFAABAAEACsAKwBLAEsASwBLAEsASwBLAEsASwBLAFAAUAAaABoAUABQAFAAUABQAEwAHgAbAFAAHgAEACsAKwAEAAQABAArAFAAUABQAFAAUABQACsAKwArACsAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUABQACsAUABQACsAUABQACsAKwAEACsABAAEAAQABAAEACsAKwArACsABAAEACsAKwAEAAQABAArACsAKwAEACsAKwArACsAKwArACsAUABQAFAAUAArAFAAKwArACsAKwArACsAKwBLAEsASwBLAEsASwBLAEsASwBLAAQABABQAFAAUAAEAB4AKwArACsAKwArACsAKwArACsAKwAEAAQABAArAFAAUABQAFAAUABQAFAAUABQACsAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUABQACsAUABQAFAAUABQACsAKwAEAFAABAAEAAQABAAEAAQABAAEACsABAAEAAQAKwAEAAQABAArACsAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAABAAEACsAKwBLAEsASwBLAEsASwBLAEsASwBLAB4AGwArACsAKwArACsAKwArAFAABAAEAAQABAAEAAQAKwAEAAQABAArAFAAUABQAFAAUABQAFAAUAArACsAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAArACsABAAEACsAKwAEAAQABAArACsAKwArACsAKwArAAQABAAEACsAKwArACsAUABQACsAUABQAFAABAAEACsAKwBLAEsASwBLAEsASwBLAEsASwBLAB4AUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArAAQAUAArAFAAUABQAFAAUABQACsAKwArAFAAUABQACsAUABQAFAAUAArACsAKwBQAFAAKwBQACsAUABQACsAKwArAFAAUAArACsAKwBQAFAAUAArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArAAQABAAEAAQABAArACsAKwAEAAQABAArAAQABAAEAAQAKwArAFAAKwArACsAKwArACsABAArACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAUABQAFAAHgAeAB4AHgAeAB4AGwAeACsAKwArACsAKwAEAAQABAAEAAQAUABQAFAAUABQAFAAUABQACsAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAUAAEAAQABAAEAAQABAAEACsABAAEAAQAKwAEAAQABAAEACsAKwArACsAKwArACsABAAEACsAUABQAFAAKwArACsAKwArAFAAUAAEAAQAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAKwAOAFAAUABQAFAAUABQAFAAHgBQAAQABAAEAA4AUABQAFAAUABQAFAAUABQACsAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAKwArAAQAUAAEAAQABAAEAAQABAAEACsABAAEAAQAKwAEAAQABAAEACsAKwArACsAKwArACsABAAEACsAKwArACsAKwArACsAUAArAFAAUAAEAAQAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwBQAFAAKwArACsAKwArACsAKwArACsAKwArACsAKwAEAAQABAAEAFAAUABQAFAAUABQAFAAUABQACsAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAFAABAAEAAQABAAEAAQABAArAAQABAAEACsABAAEAAQABABQAB4AKwArACsAKwBQAFAAUAAEAFAAUABQAFAAUABQAFAAUABQAFAABAAEACsAKwBLAEsASwBLAEsASwBLAEsASwBLAFAAUABQAFAAUABQAFAAUABQABoAUABQAFAAUABQAFAAKwAEAAQABAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQACsAUAArACsAUABQAFAAUABQAFAAUAArACsAKwAEACsAKwArACsABAAEAAQABAAEAAQAKwAEACsABAAEAAQABAAEAAQABAAEACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArAAQABAAeACsAKwArACsAKwArACsAKwArACsAKwArAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXAAqAFwAXAAqACoAKgAqACoAKgAqACsAKwArACsAGwBcAFwAXABcAFwAXABcACoAKgAqACoAKgAqACoAKgAeAEsASwBLAEsASwBLAEsASwBLAEsADQANACsAKwArACsAKwBcAFwAKwBcACsAXABcAFwAXABcACsAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcACsAXAArAFwAXABcAFwAXABcAFwAXABcAFwAKgBcAFwAKgAqACoAKgAqACoAKgAqACoAXAArACsAXABcAFwAXABcACsAXAArACoAKgAqACoAKgAqACsAKwBLAEsASwBLAEsASwBLAEsASwBLACsAKwBcAFwAXABcAFAADgAOAA4ADgAeAA4ADgAJAA4ADgANAAkAEwATABMAEwATAAkAHgATAB4AHgAeAAQABAAeAB4AHgAeAB4AHgBLAEsASwBLAEsASwBLAEsASwBLAFAAUABQAFAAUABQAFAAUABQAFAADQAEAB4ABAAeAAQAFgARABYAEQAEAAQAUABQAFAAUABQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQADQAEAAQABAAEAAQADQAEAAQAUABQAFAAUABQAAQABAAEAAQABAAEAAQABAAEAAQABAArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAArAA0ADQAeAB4AHgAeAB4AHgAEAB4AHgAeAB4AHgAeACsAHgAeAA4ADgANAA4AHgAeAB4AHgAeAAkACQArACsAKwArACsAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgBcAEsASwBLAEsASwBLAEsASwBLAEsADQANAB4AHgAeAB4AXABcAFwAXABcAFwAKgAqACoAKgBcAFwAXABcACoAKgAqAFwAKgAqACoAXABcACoAKgAqACoAKgAqACoAXABcAFwAKgAqACoAKgBcAFwAXABcAFwAXABcAFwAXABcAFwAXABcACoAKgAqACoAKgAqACoAKgAqACoAKgAqAFwAKgBLAEsASwBLAEsASwBLAEsASwBLACoAKgAqACoAKgAqAFAAUABQAFAAUABQACsAUAArACsAKwArACsAUAArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgBQAFAAUABQAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFAAUABQAFAAUABQAFAAUABQACsAUABQAFAAUAArACsAUABQAFAAUABQAFAAUAArAFAAKwBQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAKwArAFAAUABQAFAAUABQAFAAKwBQACsAUABQAFAAUAArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsABAAEAAQAHgANAB4AHgAeAB4AHgAeAB4AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUAArACsADQBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAANAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAWABEAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAA0ADQANAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAAQABAAEACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAANAA0AKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUAArAAQABAArACsAKwArACsAKwArACsAKwArACsAKwBcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqAA0ADQAVAFwADQAeAA0AGwBcACoAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwAeAB4AEwATAA0ADQAOAB4AEwATAB4ABAAEAAQACQArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAFAAUABQAFAAUAAEAAQAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQAUAArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAArACsAKwArAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwArACsAHgArACsAKwATABMASwBLAEsASwBLAEsASwBLAEsASwBcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXAArACsAXABcAFwAXABcACsAKwArACsAKwArACsAKwArACsAKwBcAFwAXABcAFwAXABcAFwAXABcAFwAXAArACsAKwArAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAXAArACsAKwAqACoAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAArACsAHgAeAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcACoAKgAqACoAKgAqACoAKgAqACoAKwAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKwArAAQASwBLAEsASwBLAEsASwBLAEsASwArACsAKwArACsAKwBLAEsASwBLAEsASwBLAEsASwBLACsAKwArACsAKwArACoAKgAqACoAKgAqACoAXAAqACoAKgAqACoAKgArACsABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsABAAEAAQABAAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABABQAFAAUABQAFAAUABQACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwANAA0AHgANAA0ADQANAB4AHgAeAB4AHgAeAB4AHgAeAB4ABAAEAAQABAAEAAQABAAEAAQAHgAeAB4AHgAeAB4AHgAeAB4AKwArACsABAAEAAQAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABABQAFAASwBLAEsASwBLAEsASwBLAEsASwBQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwArACsAKwArACsAKwAeAB4AHgAeAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwArAA0ADQANAA0ADQBLAEsASwBLAEsASwBLAEsASwBLACsAKwArAFAAUABQAEsASwBLAEsASwBLAEsASwBLAEsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAA0ADQBQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUAAeAB4AHgAeAB4AHgAeAB4AKwArACsAKwArACsAKwArAAQABAAEAB4ABAAEAAQABAAEAAQABAAEAAQABAAEAAQABABQAFAAUABQAAQAUABQAFAAUABQAFAABABQAFAABAAEAAQAUAArACsAKwArACsABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsABAAEAAQABAAEAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwArAFAAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAKwBQACsAUAArAFAAKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACsAKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArAB4AHgAeAB4AHgAeAB4AHgBQAB4AHgAeAFAAUABQACsAHgAeAB4AHgAeAB4AHgAeAB4AHgBQAFAAUABQACsAKwAeAB4AHgAeAB4AHgArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwArAFAAUABQACsAHgAeAB4AHgAeAB4AHgAOAB4AKwANAA0ADQANAA0ADQANAAkADQANAA0ACAAEAAsABAAEAA0ACQANAA0ADAAdAB0AHgAXABcAFgAXABcAFwAWABcAHQAdAB4AHgAUABQAFAANAAEAAQAEAAQABAAEAAQACQAaABoAGgAaABoAGgAaABoAHgAXABcAHQAVABUAHgAeAB4AHgAeAB4AGAAWABEAFQAVABUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4ADQAeAA0ADQANAA0AHgANAA0ADQAHAB4AHgAeAB4AKwAEAAQABAAEAAQABAAEAAQABAAEAFAAUAArACsATwBQAFAAUABQAFAAHgAeAB4AFgARAE8AUABPAE8ATwBPAFAAUABQAFAAUAAeAB4AHgAWABEAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArABsAGwAbABsAGwAbABsAGgAbABsAGwAbABsAGwAbABsAGwAbABsAGwAbABsAGgAbABsAGwAbABoAGwAbABoAGwAbABsAGwAbABsAGwAbABsAGwAbABsAGwAbABsAGwAbAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAHgAeAFAAGgAeAB0AHgBQAB4AGgAeAB4AHgAeAB4AHgAeAB4AHgBPAB4AUAAbAB4AHgBQAFAAUABQAFAAHgAeAB4AHQAdAB4AUAAeAFAAHgBQAB4AUABPAFAAUAAeAB4AHgAeAB4AHgAeAFAAUABQAFAAUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAFAAHgBQAFAAUABQAE8ATwBQAFAAUABQAFAATwBQAFAATwBQAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAFAAUABQAFAATwBPAE8ATwBPAE8ATwBPAE8ATwBQAFAAUABQAFAAUABQAFAAUAAeAB4AUABQAFAAUABPAB4AHgArACsAKwArAB0AHQAdAB0AHQAdAB0AHQAdAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB0AHgAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB4AHQAdAB4AHgAeAB0AHQAeAB4AHQAeAB4AHgAdAB4AHQAbABsAHgAdAB4AHgAeAB4AHQAeAB4AHQAdAB0AHQAeAB4AHQAeAB0AHgAdAB0AHQAdAB0AHQAeAB0AHgAeAB4AHgAeAB0AHQAdAB0AHgAeAB4AHgAdAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB4AHgAeAB0AHgAeAB4AHgAeAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB0AHgAeAB0AHQAdAB0AHgAeAB0AHQAeAB4AHQAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB0AHQAeAB4AHQAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHQAeAB4AHgAdAB4AHgAeAB4AHgAeAB4AHQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AFAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeABYAEQAWABEAHgAeAB4AHgAeAB4AHQAeAB4AHgAeAB4AHgAeACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAWABEAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AJQAlACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAFAAHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHgAeAB4AHgAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAeAB4AHQAdAB0AHQAeAB4AHgAeAB4AHgAeAB4AHgAeAB0AHQAeAB0AHQAdAB0AHQAdAB0AHgAeAB4AHgAeAB4AHgAeAB0AHQAeAB4AHQAdAB4AHgAeAB4AHQAdAB4AHgAeAB4AHQAdAB0AHgAeAB0AHgAeAB0AHQAdAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB0AHQAdAB4AHgAeAB4AHgAeAB4AHgAeAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAlACUAJQAlAB4AHQAdAB4AHgAdAB4AHgAeAB4AHQAdAB4AHgAeAB4AJQAlAB0AHQAlAB4AJQAlACUAIAAlACUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAlACUAJQAeAB4AHgAeAB0AHgAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB0AHgAdAB0AHQAeAB0AJQAdAB0AHgAdAB0AHgAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHQAdAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAlACUAJQAlACUAJQAlACUAJQAlACUAJQAdAB0AHQAdACUAHgAlACUAJQAdACUAJQAdAB0AHQAlACUAHQAdACUAHQAdACUAJQAlAB4AHQAeAB4AHgAeAB0AHQAlAB0AHQAdAB0AHQAdACUAJQAlACUAJQAdACUAJQAgACUAHQAdACUAJQAlACUAJQAlACUAJQAeAB4AHgAlACUAIAAgACAAIAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB0AHgAeAB4AFwAXABcAFwAXABcAHgATABMAJQAeAB4AHgAWABEAFgARABYAEQAWABEAFgARABYAEQAWABEATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeABYAEQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAWABEAFgARABYAEQAWABEAFgARAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AFgARABYAEQAWABEAFgARABYAEQAWABEAFgARABYAEQAWABEAFgARABYAEQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAWABEAFgARAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AFgARAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAdAB0AHQAdAB0AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AUABQAFAAUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAEAAQABAAeAB4AKwArACsAKwArABMADQANAA0AUAATAA0AUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAUAANACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQACsAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXAA0ADQANAA0ADQANAA0ADQAeAA0AFgANAB4AHgAXABcAHgAeABcAFwAWABEAFgARABYAEQAWABEADQANAA0ADQATAFAADQANAB4ADQANAB4AHgAeAB4AHgAMAAwADQANAA0AHgANAA0AFgANAA0ADQANAA0ADQANAA0AHgANAB4ADQANAB4AHgAeACsAKwArACsAKwArACsAKwArACsAKwArACsAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACsAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAKwArACsAKwArACsAKwArACsAKwArACsAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAlACUAJQAlACUAJQAlACUAJQAlACUAJQArACsAKwArAA0AEQARACUAJQBHAFcAVwAWABEAFgARABYAEQAWABEAFgARACUAJQAWABEAFgARABYAEQAWABEAFQAWABEAEQAlAFcAVwBXAFcAVwBXAFcAVwBXAAQABAAEAAQABAAEACUAVwBXAFcAVwA2ACUAJQBXAFcAVwBHAEcAJQAlACUAKwBRAFcAUQBXAFEAVwBRAFcAUQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFEAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBRAFcAUQBXAFEAVwBXAFcAVwBXAFcAUQBXAFcAVwBXAFcAVwBRAFEAKwArAAQABAAVABUARwBHAFcAFQBRAFcAUQBXAFEAVwBRAFcAUQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFEAVwBRAFcAUQBXAFcAVwBXAFcAVwBRAFcAVwBXAFcAVwBXAFEAUQBXAFcAVwBXABUAUQBHAEcAVwArACsAKwArACsAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAKwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAKwAlACUAVwBXAFcAVwAlACUAJQAlACUAJQAlACUAJQAlACsAKwArACsAKwArACsAKwArACsAKwArAFEAUQBRAFEAUQBRAFEAUQBRAFEAUQBRAFEAUQBRAFEAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQArAFcAVwBXAFcAVwBXAFcAVwBXAFcAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQBPAE8ATwBPAE8ATwBPAE8AJQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXACUAJQAlAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAEcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAKwArACsAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAADQATAA0AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABLAEsASwBLAEsASwBLAEsASwBLAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAFAABAAEAAQABAAeAAQABAAEAAQABAAEAAQABAAEAAQAHgBQAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AUABQAAQABABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAeAA0ADQANAA0ADQArACsAKwArACsAKwArACsAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAFAAUABQAFAAUABQAFAAUABQAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AUAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgBQAB4AHgAeAB4AHgAeAFAAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAHgAeAB4AHgAeAB4AHgAeAB4AKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAeAB4AUABQAFAAUABQAFAAUABQAFAAUABQAAQAUABQAFAABABQAFAAUABQAAQAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAAeAB4AHgAeAAQAKwArACsAUABQAFAAUABQAFAAHgAeABoAHgArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAADgAOABMAEwArACsAKwArACsAKwArACsABAAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAAEACsAKwArACsAKwArACsAKwANAA0ASwBLAEsASwBLAEsASwBLAEsASwArACsAKwArACsAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABABQAFAAUABQAFAAUAAeAB4AHgBQAA4AUABQAAQAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAA0ADQBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAKwArACsAKwArACsAKwArACsAKwArAB4AWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYAFgAWABYACsAKwArAAQAHgAeAB4AHgAeAB4ADQANAA0AHgAeAB4AHgArAFAASwBLAEsASwBLAEsASwBLAEsASwArACsAKwArAB4AHgBcAFwAXABcAFwAKgBcAFwAXABcAFwAXABcAFwAXABcAEsASwBLAEsASwBLAEsASwBLAEsAXABcAFwAXABcACsAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsAKwArACsAKwArACsAKwArAFAAUABQAAQAUABQAFAAUABQAFAAUABQAAQABAArACsASwBLAEsASwBLAEsASwBLAEsASwArACsAHgANAA0ADQBcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAKgAqACoAXAAqACoAKgBcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXAAqAFwAKgAqACoAXABcACoAKgBcAFwAXABcAFwAKgAqAFwAKgBcACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFwAXABcACoAKgBQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAA0ADQBQAFAAUAAEAAQAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUAArACsAUABQAFAAUABQAFAAKwArAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgAeACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQADQAEAAQAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAVABVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBUAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVAFUAVQBVACsAKwArACsAKwArACsAKwArACsAKwArAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAWQBZAFkAKwArACsAKwBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAWgBaAFoAKwArACsAKwAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYABgAGAAYAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXACUAJQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAJQAlACUAJQAlACUAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAKwArACsAKwArAFYABABWAFYAVgBWAFYAVgBWAFYAVgBWAB4AVgBWAFYAVgBWAFYAVgBWAFYAVgBWAFYAVgArAFYAVgBWAFYAVgArAFYAKwBWAFYAKwBWAFYAKwBWAFYAVgBWAFYAVgBWAFYAVgBWAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAEQAWAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUAAaAB4AKwArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAGAARABEAGAAYABMAEwAWABEAFAArACsAKwArACsAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACUAJQAlACUAJQAWABEAFgARABYAEQAWABEAFgARABYAEQAlACUAFgARACUAJQAlACUAJQAlACUAEQAlABEAKwAVABUAEwATACUAFgARABYAEQAWABEAJQAlACUAJQAlACUAJQAlACsAJQAbABoAJQArACsAKwArAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArAAcAKwATACUAJQAbABoAJQAlABYAEQAlACUAEQAlABEAJQBXAFcAVwBXAFcAVwBXAFcAVwBXABUAFQAlACUAJQATACUAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXABYAJQARACUAJQAlAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwAWACUAEQAlABYAEQARABYAEQARABUAVwBRAFEAUQBRAFEAUQBRAFEAUQBRAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAEcARwArACsAVwBXAFcAVwBXAFcAKwArAFcAVwBXAFcAVwBXACsAKwBXAFcAVwBXAFcAVwArACsAVwBXAFcAKwArACsAGgAbACUAJQAlABsAGwArAB4AHgAeAB4AHgAeAB4AKwArACsAKwArACsAKwArACsAKwAEAAQABAAQAB0AKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsADQANAA0AKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArAB4AHgAeAB4AHgAeAB4AHgAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgBQAFAAHgAeAB4AKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAAQAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAA0AUABQAFAAUAArACsAKwArAFAAUABQAFAAUABQAFAAUAANAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwAeACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAKwArAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUAArACsAKwBQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwANAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAeAB4AUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUAArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArAA0AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAUABQAFAAUABQAAQABAAEACsABAAEACsAKwArACsAKwAEAAQABAAEAFAAUABQAFAAKwBQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArAAQABAAEACsAKwArACsABABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAA0ADQANAA0ADQANAA0ADQAeACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAeAFAAUABQAFAAUABQAFAAUAAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAArACsAKwArAFAAUABQAFAAUAANAA0ADQANAA0ADQAUACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsADQANAA0ADQANAA0ADQBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAB4AHgAeAB4AKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArAFAAUABQAFAAUABQAAQABAAEAAQAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUAArAAQABAANACsAKwBQAFAAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAAQABAAEAAQABAAEAAQABAAEAAQABABQAFAAUABQAB4AHgAeAB4AHgArACsAKwArACsAKwAEAAQABAAEAAQABAAEAA0ADQAeAB4AHgAeAB4AKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsABABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAQABAAEAAQABAAEAAQABAAEAAQABAAeAB4AHgANAA0ADQANACsAKwArACsAKwArACsAKwArACsAKwAeACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwArACsAKwBLAEsASwBLAEsASwBLAEsASwBLACsAKwArACsAKwArAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEACsASwBLAEsASwBLAEsASwBLAEsASwANAA0ADQANAFAABAAEAFAAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAeAA4AUAArACsAKwArACsAKwArACsAKwAEAFAAUABQAFAADQANAB4ADQAEAAQABAAEAB4ABAAEAEsASwBLAEsASwBLAEsASwBLAEsAUAAOAFAADQANAA0AKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAANAA0AHgANAA0AHgAEACsAUABQAFAAUABQAFAAUAArAFAAKwBQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAA0AKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsABAAEAAQABAArAFAAUABQAFAAUABQAFAAUAArACsAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUABQACsAUABQAFAAUABQACsABAAEAFAABAAEAAQABAAEAAQABAArACsABAAEACsAKwAEAAQABAArACsAUAArACsAKwArACsAKwAEACsAKwArACsAKwBQAFAAUABQAFAABAAEACsAKwAEAAQABAAEAAQABAAEACsAKwArAAQABAAEAAQABAArACsAKwArACsAKwArACsAKwArACsABAAEAAQABAAEAAQABABQAFAAUABQAA0ADQANAA0AHgBLAEsASwBLAEsASwBLAEsASwBLAA0ADQArAB4ABABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAEAAQABAAEAFAAUAAeAFAAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAArACsABAAEAAQABAAEAAQABAAEAAQADgANAA0AEwATAB4AHgAeAA0ADQANAA0ADQANAA0ADQANAA0ADQANAA0ADQANAFAAUABQAFAABAAEACsAKwAEAA0ADQAeAFAAKwArACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAFAAKwArACsAKwArACsAKwBLAEsASwBLAEsASwBLAEsASwBLACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAXABcAFwAKwArACoAKgAqACoAKgAqACoAKgAqACoAKgAqACoAKgAqACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwBcAFwADQANAA0AKgBQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAeACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwBQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAKwArAFAAKwArAFAAUABQAFAAUABQAFAAUAArAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQAKwAEAAQAKwArAAQABAAEAAQAUAAEAFAABAAEAA0ADQANACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAArACsABAAEAAQABAAEAAQABABQAA4AUAAEACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAABAAEAAQABAAEAAQABAAEAAQABABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAFAABAAEAAQABAAOAB4ADQANAA0ADQAOAB4ABAArACsAKwArACsAKwArACsAUAAEAAQABAAEAAQABAAEAAQABAAEAAQAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAA0ADQANAFAADgAOAA4ADQANACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAEAAQABAAEACsABAAEAAQABAAEAAQABAAEAFAADQANAA0ADQANACsAKwArACsAKwArACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwAOABMAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQACsAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAArACsAKwAEACsABAAEACsABAAEAAQABAAEAAQABABQAAQAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAUABQAFAAUABQAFAAKwBQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQAKwAEAAQAKwAEAAQABAAEAAQAUAArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAABAAEAAQABAAeAB4AKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAB4AHgAeAB4AHgAeAB4AHgAaABoAGgAaAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwArACsAKwArACsAKwArAA0AUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsADQANAA0ADQANACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAASABIAEgAQwBDAEMAUABQAFAAUABDAFAAUABQAEgAQwBIAEMAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAASABDAEMAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwAJAAkACQAJAAkACQAJABYAEQArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABIAEMAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwANAA0AKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArAAQABAAEAAQABAANACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEAA0ADQANAB4AHgAeAB4AHgAeAFAAUABQAFAADQAeACsAKwArACsAKwArACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwArAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAANAA0AHgAeACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwAEAFAABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAKwArACsAKwArACsAKwAEAAQABAAEAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAARwBHABUARwAJACsAKwArACsAKwArACsAKwArACsAKwAEAAQAKwArACsAKwArACsAKwArACsAKwArACsAKwArAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXACsAKwArACsAKwArACsAKwBXAFcAVwBXAFcAVwBXAFcAVwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUQBRAFEAKwArACsAKwArACsAKwArACsAKwArACsAKwBRAFEAUQBRACsAKwArACsAKwArACsAKwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUAArACsAHgAEAAQADQAEAAQABAAEACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwArACsAKwArAB4AHgAeAB4AHgAeAB4AKwArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAAQABAAEAAQABAAeAB4AHgAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAB4AHgAEAAQABAAEAAQABAAEAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4ABAAEAAQABAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4ABAAEAAQAHgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwArACsAKwArACsAKwArACsAKwArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwArACsAKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwBQAFAAKwArAFAAKwArAFAAUAArACsAUABQAFAAUAArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACsAUAArAFAAUABQAFAAUABQAFAAKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwBQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAHgAeAFAAUABQAFAAUAArAFAAKwArACsAUABQAFAAUABQAFAAUAArAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAHgBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgBQAFAAUABQAFAAUABQAFAAUABQAFAAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAB4AHgAeAB4AHgAeAB4AHgAeACsAKwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAEsASwBLAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAeAB4AHgAeAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAeAB4AHgAeAB4AHgAeAB4ABAAeAB4AHgAeAB4AHgAeAB4AHgAeAAQAHgAeAA0ADQANAA0AHgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAEAAQABAAEAAQAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAAQABAAEAAQABAAEAAQAKwAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAKwArAAQABAAEAAQABAAEAAQAKwAEAAQAKwAEAAQABAAEAAQAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwAEAAQABAAEAAQABAAEAFAAUABQAFAAUABQAFAAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwBQAB4AKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArABsAUABQAFAAUABQACsAKwBQAFAAUABQAFAAUABQAFAAUAAEAAQABAAEAAQABAAEACsAKwArACsAKwArACsAKwArAB4AHgAeAB4ABAAEAAQABAAEAAQABABQACsAKwArACsASwBLAEsASwBLAEsASwBLAEsASwArACsAKwArABYAFgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAGgBQAFAAUAAaAFAAUABQAFAAKwArACsAKwArACsAKwArACsAKwArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAeAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQACsAKwBQAFAAUABQACsAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwBQAFAAKwBQACsAKwBQACsAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAKwBQACsAUAArACsAKwArACsAKwBQACsAKwArACsAUAArAFAAKwBQACsAUABQAFAAKwBQAFAAKwBQACsAKwBQACsAUAArAFAAKwBQACsAUAArAFAAUAArAFAAKwArAFAAUABQAFAAKwBQAFAAUABQAFAAUABQACsAUABQAFAAUAArAFAAUABQAFAAKwBQACsAUABQAFAAUABQAFAAUABQAFAAUAArAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAArACsAKwArACsAUABQAFAAKwBQAFAAUABQAFAAKwBQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwAeAB4AKwArACsAKwArACsAKwArACsAKwArACsAKwArAE8ATwBPAE8ATwBPAE8ATwBPAE8ATwBPAE8AJQAlACUAHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHgAeAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB4AHgAeACUAJQAlAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAdAB0AHQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQApACkAKQApACkAKQApACkAKQApACkAKQApACkAKQApACkAKQApACkAKQApACkAKQApACkAJQAlACUAJQAlACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAeAB4AJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlAB4AHgAlACUAJQAlACUAHgAlACUAJQAlACUAIAAgACAAJQAlACAAJQAlACAAIAAgACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACEAIQAhACEAIQAlACUAIAAgACUAJQAgACAAIAAgACAAIAAgACAAIAAgACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAJQAlACUAIAAlACUAJQAlACAAIAAgACUAIAAgACAAJQAlACUAJQAlACUAJQAgACUAIAAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAHgAlAB4AJQAeACUAJQAlACUAJQAgACUAJQAlACUAHgAlAB4AHgAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlAB4AHgAeAB4AHgAeAB4AJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAeACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACAAIAAlACUAJQAlACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACAAJQAlACUAJQAgACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAHgAeAB4AHgAeAB4AHgAeACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAeAB4AHgAeAB4AHgAlACUAJQAlACUAJQAlACAAIAAgACUAJQAlACAAIAAgACAAIAAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeABcAFwAXABUAFQAVAB4AHgAeAB4AJQAlACUAIAAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACAAIAAgACUAJQAlACUAJQAlACUAJQAlACAAJQAlACUAJQAlACUAJQAlACUAJQAlACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AJQAlACUAJQAlACUAJQAlACUAJQAlACUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AJQAlACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeACUAJQAlACUAJQAlACUAJQAeAB4AHgAeAB4AHgAeAB4AHgAeACUAJQAlACUAJQAlAB4AHgAeAB4AHgAeAB4AHgAlACUAJQAlACUAJQAlACUAHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAgACUAJQAgACUAJQAlACUAJQAlACUAJQAgACAAIAAgACAAIAAgACAAJQAlACUAJQAlACUAIAAlACUAJQAlACUAJQAlACUAJQAgACAAIAAgACAAIAAgACAAIAAgACUAJQAgACAAIAAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAgACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACAAIAAlACAAIAAlACAAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAgACAAIAAlACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAJQAlAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AKwAeAB4AHgAeAB4AHgAeAB4AHgAeAB4AHgArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAEsASwBLAEsASwBLAEsASwBLAEsAKwArACsAKwArACsAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAKwArAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXACUAJQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwAlACUAJQAlACUAJQAlACUAJQAlACUAVwBXACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQBXAFcAVwBXAFcAVwBXAFcAVwBXAFcAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAJQAlACUAKwAEACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArACsAKwArAA=="),L=Array.isArray(m)?function(A){for(var e=A.length,t=[],r=0;r<e;r+=4)t.push(A[r+3]<<24|A[r+2]<<16|A[r+1]<<8|A[r]);return t}(m):new Uint32Array(m),b=Array.isArray(m)?function(A){for(var e=A.length,t=[],r=0;r<e;r+=2)t.push(A[r+1]<<8|A[r]);return t}(m):new Uint16Array(m),y=w(b,12,L[4]/2),K=2===L[5]?w(b,(24+L[4])/2):(m=L,b=Math.ceil((24+L[4])/4),m.slice?m.slice(b,K):new Uint32Array(Array.prototype.slice.call(m,b,K))),new U(L[0],L[1],L[2],L[3],y,K)),BA=[J,36],nA=[1,2,3,5],sA=[D,8],oA=[P,N],iA=nA.concat(sA),QA=[j,z,$,Z,_],cA=[x,v],aA=(gA.prototype.slice=function(){return g.apply(void 0,this.codePoints.slice(this.start,this.end))},gA);function gA(A,e,t,r){this.codePoints=A,this.required="!"===e,this.start=t,this.end=r}function wA(A,e){var t=Q(A),r=(e=I(t,e))[0],B=e[1],n=e[2],s=t.length,o=0,i=0;return{next:function(){if(s<=i)return{done:!0,value:null};for(var A=tA;i<s&&(A=function(A,e,t,r,B){if(0===t[r])return tA;var n=r-1;if(Array.isArray(B)&&!0===B[n])return tA;var s=n-1,o=1+n,i=e[n],r=0<=s?e[s]:0,B=e[o];if(2===i&&3===B)return tA;if(-1!==nA.indexOf(i))return"!";if(-1!==nA.indexOf(B))return tA;if(-1!==sA.indexOf(B))return tA;if(8===E(n,e))return"÷";if(11===rA.get(A[n]))return tA;if((i===Y||i===W)&&11===rA.get(A[o]))return tA;if(7===i||7===B)return tA;if(9===i)return tA;if(-1===[D,v,x].indexOf(i)&&9===B)return tA;if(-1!==[M,S,T,k,X].indexOf(B))return tA;if(E(n,e)===V)return tA;if(p(23,V,n,e))return tA;if(p([M,S],O,n,e))return tA;if(p(12,12,n,e))return tA;if(i===D)return"÷";if(23===i||23===B)return tA;if(16===B||16===i)return"÷";if(-1!==[v,x,O].indexOf(B)||14===i)return tA;if(36===r&&-1!==cA.indexOf(i))return tA;if(i===X&&36===B)return tA;if(B===G)return tA;if(-1!==BA.indexOf(B)&&i===R||-1!==BA.indexOf(i)&&B===R)return tA;if(i===P&&-1!==[q,Y,W].indexOf(B)||-1!==[q,Y,W].indexOf(i)&&B===N)return tA;if(-1!==BA.indexOf(i)&&-1!==oA.indexOf(B)||-1!==oA.indexOf(i)&&-1!==BA.indexOf(B))return tA;if(-1!==[P,N].indexOf(i)&&(B===R||-1!==[V,x].indexOf(B)&&e[1+o]===R)||-1!==[V,x].indexOf(i)&&B===R||i===R&&-1!==[R,X,k].indexOf(B))return tA;if(-1!==[R,X,k,M,S].indexOf(B))for(var Q=n;0<=Q;){if((c=e[Q])===R)return tA;if(-1===[X,k].indexOf(c))break;Q--}if(-1!==[P,N].indexOf(B))for(var c,Q=-1!==[M,S].indexOf(i)?s:n;0<=Q;){if((c=e[Q])===R)return tA;if(-1===[X,k].indexOf(c))break;Q--}if(j===i&&-1!==[j,z,Z,_].indexOf(B)||-1!==[z,Z].indexOf(i)&&-1!==[z,$].indexOf(B)||-1!==[$,_].indexOf(i)&&B===$)return tA;if(-1!==QA.indexOf(i)&&-1!==[G,N].indexOf(B)||-1!==QA.indexOf(B)&&i===P)return tA;if(-1!==BA.indexOf(i)&&-1!==BA.indexOf(B))return tA;if(i===k&&-1!==BA.indexOf(B))return tA;if(-1!==BA.concat(R).indexOf(i)&&B===V&&-1===eA.indexOf(A[o])||-1!==BA.concat(R).indexOf(B)&&i===S)return tA;if(41===i&&41===B){for(var a=t[n],g=1;0<a&&41===e[--a];)g++;if(g%2!=0)return tA}return i===Y&&B===W?tA:"÷"}(t,B,r,++i,n))===tA;);if(A===tA&&i!==s)return{done:!0,value:null};var e=new aA(t,A,o,i);return o=i,{value:e,done:!1}}}}function UA(A){return 48<=A&&A<=57}function lA(A){return UA(A)||65<=A&&A<=70||97<=A&&A<=102}function CA(A){return 10===A||9===A||32===A}function uA(A){return 97<=(t=e=A)&&t<=122||65<=(e=e)&&e<=90||128<=A||95===A;var e,t}function FA(A){return uA(A)||UA(A)||45===A}function hA(A,e){return 92===A&&10!==e}function dA(A,e,t){return 45===A?uA(e)||hA(e,t):!!uA(A)||92===A&&10!==e}function fA(A,e,t){return 43===A||45===A?!!UA(e)||46===e&&UA(t):UA(46===A?e:A)}var HA={type:2},pA={type:3},EA={type:4},IA={type:13},yA={type:8},KA={type:21},mA={type:9},LA={type:10},bA={type:11},DA={type:12},vA={type:14},xA={type:23},MA={type:1},SA={type:25},TA={type:24},GA={type:26},OA={type:27},VA={type:28},kA={type:29},RA={type:31},NA={type:32},PA=(XA.prototype.write=function(A){this._value=this._value.concat(Q(A))},XA.prototype.read=function(){for(var A=[],e=this.consumeToken();e!==NA;)A.push(e),e=this.consumeToken();return A},XA.prototype.consumeToken=function(){var A=this.consumeCodePoint();switch(A){case 34:return this.consumeStringToken(34);case 35:var e=this.peekCodePoint(0),t=this.peekCodePoint(1),r=this.peekCodePoint(2);if(FA(e)||hA(t,r)){var B=dA(e,t,r)?2:1;return{type:5,value:this.consumeName(),flags:B}}break;case 36:if(61===this.peekCodePoint(0))return this.consumeCodePoint(),IA;break;case 39:return this.consumeStringToken(39);case 40:return HA;case 41:return pA;case 42:if(61===this.peekCodePoint(0))return this.consumeCodePoint(),vA;break;case 43:if(fA(A,this.peekCodePoint(0),this.peekCodePoint(1)))return this.reconsumeCodePoint(A),this.consumeNumericToken();break;case 44:return EA;case 45:var r=A,B=this.peekCodePoint(0),n=this.peekCodePoint(1);if(fA(r,B,n))return this.reconsumeCodePoint(A),this.consumeNumericToken();if(dA(r,B,n))return this.reconsumeCodePoint(A),this.consumeIdentLikeToken();if(45===B&&62===n)return this.consumeCodePoint(),this.consumeCodePoint(),TA;break;case 46:if(fA(A,this.peekCodePoint(0),this.peekCodePoint(1)))return this.reconsumeCodePoint(A),this.consumeNumericToken();break;case 47:if(42===this.peekCodePoint(0))for(this.consumeCodePoint();;){var s=this.consumeCodePoint();if(42===s&&47===(s=this.consumeCodePoint()))return this.consumeToken();if(-1===s)return this.consumeToken()}break;case 58:return GA;case 59:return OA;case 60:if(33===this.peekCodePoint(0)&&45===this.peekCodePoint(1)&&45===this.peekCodePoint(2))return this.consumeCodePoint(),this.consumeCodePoint(),SA;break;case 64:var n=this.peekCodePoint(0),o=this.peekCodePoint(1),i=this.peekCodePoint(2);if(dA(n,o,i))return{type:7,value:this.consumeName()};break;case 91:return VA;case 92:if(hA(A,this.peekCodePoint(0)))return this.reconsumeCodePoint(A),this.consumeIdentLikeToken();break;case 93:return kA;case 61:if(61===this.peekCodePoint(0))return this.consumeCodePoint(),yA;break;case 123:return bA;case 125:return DA;case 117:case 85:o=this.peekCodePoint(0),i=this.peekCodePoint(1);return 43!==o||!lA(i)&&63!==i||(this.consumeCodePoint(),this.consumeUnicodeRangeToken()),this.reconsumeCodePoint(A),this.consumeIdentLikeToken();case 124:if(61===this.peekCodePoint(0))return this.consumeCodePoint(),mA;if(124===this.peekCodePoint(0))return this.consumeCodePoint(),KA;break;case 126:if(61===this.peekCodePoint(0))return this.consumeCodePoint(),LA;break;case-1:return NA}return CA(A)?(this.consumeWhiteSpace(),RA):UA(A)?(this.reconsumeCodePoint(A),this.consumeNumericToken()):uA(A)?(this.reconsumeCodePoint(A),this.consumeIdentLikeToken()):{type:6,value:g(A)}},XA.prototype.consumeCodePoint=function(){var A=this._value.shift();return void 0===A?-1:A},XA.prototype.reconsumeCodePoint=function(A){this._value.unshift(A)},XA.prototype.peekCodePoint=function(A){return A>=this._value.length?-1:this._value[A]},XA.prototype.consumeUnicodeRangeToken=function(){for(var A=[],e=this.consumeCodePoint();lA(e)&&A.length<6;)A.push(e),e=this.consumeCodePoint();for(var t=!1;63===e&&A.length<6;)A.push(e),e=this.consumeCodePoint(),t=!0;if(t)return{type:30,start:parseInt(g.apply(void 0,A.map(function(A){return 63===A?48:A})),16),end:parseInt(g.apply(void 0,A.map(function(A){return 63===A?70:A})),16)};var r=parseInt(g.apply(void 0,A),16);if(45===this.peekCodePoint(0)&&lA(this.peekCodePoint(1))){this.consumeCodePoint();for(var e=this.consumeCodePoint(),B=[];lA(e)&&B.length<6;)B.push(e),e=this.consumeCodePoint();return{type:30,start:r,end:parseInt(g.apply(void 0,B),16)}}return{type:30,start:r,end:r}},XA.prototype.consumeIdentLikeToken=function(){var A=this.consumeName();return"url"===A.toLowerCase()&&40===this.peekCodePoint(0)?(this.consumeCodePoint(),this.consumeUrlToken()):40===this.peekCodePoint(0)?(this.consumeCodePoint(),{type:19,value:A}):{type:20,value:A}},XA.prototype.consumeUrlToken=function(){var A=[];if(this.consumeWhiteSpace(),-1===this.peekCodePoint(0))return{type:22,value:""};var e,t=this.peekCodePoint(0);if(39===t||34===t){t=this.consumeStringToken(this.consumeCodePoint());return 0===t.type&&(this.consumeWhiteSpace(),-1===this.peekCodePoint(0)||41===this.peekCodePoint(0))?(this.consumeCodePoint(),{type:22,value:t.value}):(this.consumeBadUrlRemnants(),xA)}for(;;){var r=this.consumeCodePoint();if(-1===r||41===r)return{type:22,value:g.apply(void 0,A)};if(CA(r))return this.consumeWhiteSpace(),-1===this.peekCodePoint(0)||41===this.peekCodePoint(0)?(this.consumeCodePoint(),{type:22,value:g.apply(void 0,A)}):(this.consumeBadUrlRemnants(),xA);if(34===r||39===r||40===r||(0<=(e=r)&&e<=8||11===e||14<=e&&e<=31||127===e))return this.consumeBadUrlRemnants(),xA;if(92===r){if(!hA(r,this.peekCodePoint(0)))return this.consumeBadUrlRemnants(),xA;A.push(this.consumeEscapedCodePoint())}else A.push(r)}},XA.prototype.consumeWhiteSpace=function(){for(;CA(this.peekCodePoint(0));)this.consumeCodePoint()},XA.prototype.consumeBadUrlRemnants=function(){for(;;){var A=this.consumeCodePoint();if(41===A||-1===A)return;hA(A,this.peekCodePoint(0))&&this.consumeEscapedCodePoint()}},XA.prototype.consumeStringSlice=function(A){for(var e="";0<A;){var t=Math.min(5e4,A);e+=g.apply(void 0,this._value.splice(0,t)),A-=t}return this._value.shift(),e},XA.prototype.consumeStringToken=function(A){for(var e="",t=0;;){var r,B=this._value[t];if(-1===B||void 0===B||B===A)return{type:0,value:e+=this.consumeStringSlice(t)};if(10===B)return this._value.splice(0,t),MA;92!==B||-1!==(r=this._value[t+1])&&void 0!==r&&(10===r?(e+=this.consumeStringSlice(t),t=-1,this._value.shift()):hA(B,r)&&(e+=this.consumeStringSlice(t),e+=g(this.consumeEscapedCodePoint()),t=-1)),t++}},XA.prototype.consumeNumber=function(){var A=[],e=4;for(43!==(t=this.peekCodePoint(0))&&45!==t||A.push(this.consumeCodePoint());UA(this.peekCodePoint(0));)A.push(this.consumeCodePoint());var t=this.peekCodePoint(0),r=this.peekCodePoint(1);if(46===t&&UA(r))for(A.push(this.consumeCodePoint(),this.consumeCodePoint()),e=8;UA(this.peekCodePoint(0));)A.push(this.consumeCodePoint());t=this.peekCodePoint(0);var r=this.peekCodePoint(1),B=this.peekCodePoint(2);if((69===t||101===t)&&((43===r||45===r)&&UA(B)||UA(r)))for(A.push(this.consumeCodePoint(),this.consumeCodePoint()),e=8;UA(this.peekCodePoint(0));)A.push(this.consumeCodePoint());return[function(A){var e=0,t=1;43!==A[e]&&45!==A[e]||(45===A[e]&&(t=-1),e++);for(var r=[];UA(A[e]);)r.push(A[e++]);var B=r.length?parseInt(g.apply(void 0,r),10):0;46===A[e]&&e++;for(var n=[];UA(A[e]);)n.push(A[e++]);var s=n.length,o=s?parseInt(g.apply(void 0,n),10):0;69!==A[e]&&101!==A[e]||e++;var i=1;43!==A[e]&&45!==A[e]||(45===A[e]&&(i=-1),e++);for(var Q=[];UA(A[e]);)Q.push(A[e++]);var c=Q.length?parseInt(g.apply(void 0,Q),10):0;return t*(B+o*Math.pow(10,-s))*Math.pow(10,i*c)}(A),e]},XA.prototype.consumeNumericToken=function(){var A=this.consumeNumber(),e=A[0],t=A[1],r=this.peekCodePoint(0),B=this.peekCodePoint(1),A=this.peekCodePoint(2);return dA(r,B,A)?{type:15,number:e,flags:t,unit:this.consumeName()}:37===r?(this.consumeCodePoint(),{type:16,number:e,flags:t}):{type:17,number:e,flags:t}},XA.prototype.consumeEscapedCodePoint=function(){var A,e=this.consumeCodePoint();if(lA(e)){for(var t=g(e);lA(this.peekCodePoint(0))&&t.length<6;)t+=g(this.consumeCodePoint());CA(this.peekCodePoint(0))&&this.consumeCodePoint();var r=parseInt(t,16);return 0===r||55296<=(A=r)&&A<=57343||1114111<r?65533:r}return-1===e?65533:e},XA.prototype.consumeName=function(){for(var A="";;){var e=this.consumeCodePoint();if(FA(e))A+=g(e);else{if(!hA(e,this.peekCodePoint(0)))return this.reconsumeCodePoint(e),A;A+=g(this.consumeEscapedCodePoint())}}},XA);function XA(){this._value=[]}var JA=(YA.create=function(A){var e=new PA;return e.write(A),new YA(e.read())},YA.parseValue=function(A){return YA.create(A).parseComponentValue()},YA.parseValues=function(A){return YA.create(A).parseComponentValues()},YA.prototype.parseComponentValue=function(){for(var A=this.consumeToken();31===A.type;)A=this.consumeToken();if(32===A.type)throw new SyntaxError("Error parsing CSS component value, unexpected EOF");this.reconsumeToken(A);for(var e=this.consumeComponentValue();31===(A=this.consumeToken()).type;);if(32===A.type)return e;throw new SyntaxError("Error parsing CSS component value, multiple values found when expecting only one")},YA.prototype.parseComponentValues=function(){for(var A=[];;){var e=this.consumeComponentValue();if(32===e.type)return A;A.push(e),A.push()}},YA.prototype.consumeComponentValue=function(){var A=this.consumeToken();switch(A.type){case 11:case 28:case 2:return this.consumeSimpleBlock(A.type);case 19:return this.consumeFunction(A)}return A},YA.prototype.consumeSimpleBlock=function(A){for(var e={type:A,values:[]},t=this.consumeToken();;){if(32===t.type||ce(t,A))return e;this.reconsumeToken(t),e.values.push(this.consumeComponentValue()),t=this.consumeToken()}},YA.prototype.consumeFunction=function(A){for(var e={name:A.value,values:[],type:18};;){var t=this.consumeToken();if(32===t.type||3===t.type)return e;this.reconsumeToken(t),e.values.push(this.consumeComponentValue())}},YA.prototype.consumeToken=function(){var A=this._tokens.shift();return void 0===A?NA:A},YA.prototype.reconsumeToken=function(A){this._tokens.unshift(A)},YA);function YA(A){this._tokens=A}function WA(A){return 15===A.type}function ZA(A){return 17===A.type}function _A(A){return 20===A.type}function qA(A){return 0===A.type}function jA(A,e){return _A(A)&&A.value===e}function zA(A){return 31!==A.type}function $A(A){return 31!==A.type&&4!==A.type}function Ae(A){var e=[],t=[];return A.forEach(function(A){if(4===A.type){if(0===t.length)throw new Error("Error parsing function args, zero tokens for arg");return e.push(t),void(t=[])}31!==A.type&&t.push(A)}),t.length&&e.push(t),e}function ee(A){return 17===A.type||15===A.type}function te(A){return 16===A.type||ee(A)}function re(A){return 1<A.length?[A[0],A[1]]:[A[0]]}function Be(A,e,t){var r=A[0],A=A[1];return[Ue(r,e),Ue(void 0!==A?A:r,t)]}function ne(A){return 15===A.type&&("deg"===A.unit||"grad"===A.unit||"rad"===A.unit||"turn"===A.unit)}function se(A){switch(A.filter(_A).map(function(A){return A.value}).join(" ")){case"to bottom right":case"to right bottom":case"left top":case"top left":return[ae,ae];case"to top":case"bottom":return Ce(0);case"to bottom left":case"to left bottom":case"right top":case"top right":return[ae,we];case"to right":case"left":return Ce(90);case"to top left":case"to left top":case"right bottom":case"bottom right":return[we,we];case"to bottom":case"top":return Ce(180);case"to top right":case"to right top":case"left bottom":case"bottom left":return[we,ae];case"to left":case"right":return Ce(270)}return 0}function oe(A){return 0==(255&A)}function ie(A){var e=255&A,t=255&A>>8,r=255&A>>16,A=255&A>>24;return e<255?"rgba("+A+","+r+","+t+","+e/255+")":"rgb("+A+","+r+","+t+")"}function Qe(A,e){if(17===A.type)return A.number;if(16!==A.type)return 0;var t=3===e?1:255;return 3===e?A.number/100*t:Math.round(A.number/100*t)}var ce=function(A,e){return 11===e&&12===A.type||(28===e&&29===A.type||2===e&&3===A.type)},ae={type:17,number:0,flags:4},ge={type:16,number:50,flags:4},we={type:16,number:100,flags:4},Ue=function(A,e){if(16===A.type)return A.number/100*e;if(WA(A))switch(A.unit){case"rem":case"em":return 16*A.number;default:return A.number}return A.number},le=function(A,e){if(15===e.type)switch(e.unit){case"deg":return Math.PI*e.number/180;case"grad":return Math.PI/200*e.number;case"rad":return e.number;case"turn":return 2*Math.PI*e.number}throw new Error("Unsupported angle type")},Ce=function(A){return Math.PI*A/180},ue=function(A,e){if(18===e.type){var t=me[e.name];if(void 0===t)throw new Error('Attempting to parse an unsupported color function "'+e.name+'"');return t(A,e.values)}if(5===e.type){if(3===e.value.length){var r=e.value.substring(0,1),B=e.value.substring(1,2),n=e.value.substring(2,3);return Fe(parseInt(r+r,16),parseInt(B+B,16),parseInt(n+n,16),1)}if(4===e.value.length){var r=e.value.substring(0,1),B=e.value.substring(1,2),n=e.value.substring(2,3),s=e.value.substring(3,4);return Fe(parseInt(r+r,16),parseInt(B+B,16),parseInt(n+n,16),parseInt(s+s,16)/255)}if(6===e.value.length){r=e.value.substring(0,2),B=e.value.substring(2,4),n=e.value.substring(4,6);return Fe(parseInt(r,16),parseInt(B,16),parseInt(n,16),1)}if(8===e.value.length){r=e.value.substring(0,2),B=e.value.substring(2,4),n=e.value.substring(4,6),s=e.value.substring(6,8);return Fe(parseInt(r,16),parseInt(B,16),parseInt(n,16),parseInt(s,16)/255)}}if(20===e.type){e=Le[e.value.toUpperCase()];if(void 0!==e)return e}return Le.TRANSPARENT},Fe=function(A,e,t,r){return(A<<24|e<<16|t<<8|Math.round(255*r)<<0)>>>0},he=function(A,e){e=e.filter($A);if(3===e.length){var t=e.map(Qe),r=t[0],B=t[1],t=t[2];return Fe(r,B,t,1)}if(4!==e.length)return 0;e=e.map(Qe),r=e[0],B=e[1],t=e[2],e=e[3];return Fe(r,B,t,e)};function de(A,e,t){return t<0&&(t+=1),1<=t&&--t,t<1/6?(e-A)*t*6+A:t<.5?e:t<2/3?6*(e-A)*(2/3-t)+A:A}function fe(A,e){return ue(A,JA.create(e).parseComponentValue())}function He(A,e){return A=ue(A,e[0]),(e=e[1])&&te(e)?{color:A,stop:e}:{color:A,stop:null}}function pe(A,t){var e=A[0],r=A[A.length-1];null===e.stop&&(e.stop=ae),null===r.stop&&(r.stop=we);for(var B=[],n=0,s=0;s<A.length;s++){var o=A[s].stop;null!==o?(n<(o=Ue(o,t))?B.push(o):B.push(n),n=o):B.push(null)}for(var i=null,s=0;s<B.length;s++){var Q=B[s];if(null===Q)null===i&&(i=s);else if(null!==i){for(var c=s-i,a=(Q-B[i-1])/(1+c),g=1;g<=c;g++)B[i+g-1]=a*g;i=null}}return A.map(function(A,e){return{color:A.color,stop:Math.max(Math.min(1,B[e]/t),0)}})}function Ee(A,e,t){var r="number"==typeof A?A:(s=e/2,r=(n=t)/2,s=Ue((B=A)[0],e)-s,n=r-Ue(B[1],n),(Math.atan2(n,s)+2*Math.PI)%(2*Math.PI)),B=Math.abs(e*Math.sin(r))+Math.abs(t*Math.cos(r)),n=e/2,s=t/2,e=B/2,t=Math.sin(r-Math.PI/2)*e,e=Math.cos(r-Math.PI/2)*e;return[B,n-e,n+e,s-t,s+t]}function Ie(A,e){return Math.sqrt(A*A+e*e)}function ye(A,e,B,n,s){return[[0,0],[0,e],[A,0],[A,e]].reduce(function(A,e){var t=e[0],r=e[1],r=Ie(B-t,n-r);return(s?r<A.optimumDistance:r>A.optimumDistance)?{optimumCorner:e,optimumDistance:r}:A},{optimumDistance:s?1/0:-1/0,optimumCorner:null}).optimumCorner}var Ke=function(A,e){var t=e.filter($A),r=t[0],B=t[1],n=t[2],e=t[3],t=(17===r.type?Ce(r.number):le(A,r))/(2*Math.PI),A=te(B)?B.number/100:0,r=te(n)?n.number/100:0,B=void 0!==e&&te(e)?Ue(e,1):1;if(0==A)return Fe(255*r,255*r,255*r,1);n=r<=.5?r*(1+A):r+A-r*A,e=2*r-n,A=de(e,n,t+1/3),r=de(e,n,t),t=de(e,n,t-1/3);return Fe(255*A,255*r,255*t,B)},me={hsl:Ke,hsla:Ke,rgb:he,rgba:he},Le={ALICEBLUE:4042850303,ANTIQUEWHITE:4209760255,AQUA:16777215,AQUAMARINE:2147472639,AZURE:4043309055,BEIGE:4126530815,BISQUE:4293182719,BLACK:255,BLANCHEDALMOND:4293643775,BLUE:65535,BLUEVIOLET:2318131967,BROWN:2771004159,BURLYWOOD:3736635391,CADETBLUE:1604231423,CHARTREUSE:2147418367,CHOCOLATE:3530104575,CORAL:4286533887,CORNFLOWERBLUE:1687547391,CORNSILK:4294499583,CRIMSON:3692313855,CYAN:16777215,DARKBLUE:35839,DARKCYAN:9145343,DARKGOLDENROD:3095837695,DARKGRAY:2846468607,DARKGREEN:6553855,DARKGREY:2846468607,DARKKHAKI:3182914559,DARKMAGENTA:2332068863,DARKOLIVEGREEN:1433087999,DARKORANGE:4287365375,DARKORCHID:2570243327,DARKRED:2332033279,DARKSALMON:3918953215,DARKSEAGREEN:2411499519,DARKSLATEBLUE:1211993087,DARKSLATEGRAY:793726975,DARKSLATEGREY:793726975,DARKTURQUOISE:13554175,DARKVIOLET:2483082239,DEEPPINK:4279538687,DEEPSKYBLUE:12582911,DIMGRAY:1768516095,DIMGREY:1768516095,DODGERBLUE:512819199,FIREBRICK:2988581631,FLORALWHITE:4294635775,FORESTGREEN:579543807,FUCHSIA:4278255615,GAINSBORO:3705462015,GHOSTWHITE:4177068031,GOLD:4292280575,GOLDENROD:3668254975,GRAY:2155905279,GREEN:8388863,GREENYELLOW:2919182335,GREY:2155905279,HONEYDEW:4043305215,HOTPINK:4285117695,INDIANRED:3445382399,INDIGO:1258324735,IVORY:4294963455,KHAKI:4041641215,LAVENDER:3873897215,LAVENDERBLUSH:4293981695,LAWNGREEN:2096890111,LEMONCHIFFON:4294626815,LIGHTBLUE:2916673279,LIGHTCORAL:4034953471,LIGHTCYAN:3774873599,LIGHTGOLDENRODYELLOW:4210742015,LIGHTGRAY:3553874943,LIGHTGREEN:2431553791,LIGHTGREY:3553874943,LIGHTPINK:4290167295,LIGHTSALMON:4288707327,LIGHTSEAGREEN:548580095,LIGHTSKYBLUE:2278488831,LIGHTSLATEGRAY:2005441023,LIGHTSLATEGREY:2005441023,LIGHTSTEELBLUE:2965692159,LIGHTYELLOW:4294959359,LIME:16711935,LIMEGREEN:852308735,LINEN:4210091775,MAGENTA:4278255615,MAROON:2147483903,MEDIUMAQUAMARINE:1724754687,MEDIUMBLUE:52735,MEDIUMORCHID:3126187007,MEDIUMPURPLE:2473647103,MEDIUMSEAGREEN:1018393087,MEDIUMSLATEBLUE:2070474495,MEDIUMSPRINGGREEN:16423679,MEDIUMTURQUOISE:1221709055,MEDIUMVIOLETRED:3340076543,MIDNIGHTBLUE:421097727,MINTCREAM:4127193855,MISTYROSE:4293190143,MOCCASIN:4293178879,NAVAJOWHITE:4292783615,NAVY:33023,OLDLACE:4260751103,OLIVE:2155872511,OLIVEDRAB:1804477439,ORANGE:4289003775,ORANGERED:4282712319,ORCHID:3664828159,PALEGOLDENROD:4008225535,PALEGREEN:2566625535,PALETURQUOISE:2951671551,PALEVIOLETRED:3681588223,PAPAYAWHIP:4293907967,PEACHPUFF:4292524543,PERU:3448061951,PINK:4290825215,PLUM:3718307327,POWDERBLUE:2967529215,PURPLE:2147516671,REBECCAPURPLE:1714657791,RED:4278190335,ROSYBROWN:3163525119,ROYALBLUE:1097458175,SADDLEBROWN:2336560127,SALMON:4202722047,SANDYBROWN:4104413439,SEAGREEN:780883967,SEASHELL:4294307583,SIENNA:2689740287,SILVER:3233857791,SKYBLUE:2278484991,SLATEBLUE:1784335871,SLATEGRAY:1887473919,SLATEGREY:1887473919,SNOW:4294638335,SPRINGGREEN:16744447,STEELBLUE:1182971135,TAN:3535047935,TEAL:8421631,THISTLE:3636451583,TOMATO:4284696575,TRANSPARENT:0,TURQUOISE:1088475391,VIOLET:4001558271,WHEAT:4125012991,WHITE:4294967295,WHITESMOKE:4126537215,YELLOW:4294902015,YELLOWGREEN:2597139199},be={name:"background-clip",initialValue:"border-box",prefix:!1,type:1,parse:function(A,e){return e.map(function(A){if(_A(A))switch(A.value){case"padding-box":return 1;case"content-box":return 2}return 0})}},De={name:"background-color",initialValue:"transparent",prefix:!1,type:3,format:"color"},Ke=function(t,A){var r=Ce(180),B=[];return Ae(A).forEach(function(A,e){if(0===e){e=A[0];if(20===e.type&&-1!==["top","left","right","bottom"].indexOf(e.value))return void(r=se(A));if(ne(e))return void(r=(le(t,e)+Ce(270))%Ce(360))}A=He(t,A);B.push(A)}),{angle:r,stops:B,type:1}},ve="closest-side",xe="farthest-side",Me="closest-corner",Se="farthest-corner",Te="ellipse",Ge="contain",he=function(r,A){var B=0,n=3,s=[],o=[];return Ae(A).forEach(function(A,e){var t=!0;0===e?t=A.reduce(function(A,e){if(_A(e))switch(e.value){case"center":return o.push(ge),!1;case"top":case"left":return o.push(ae),!1;case"right":case"bottom":return o.push(we),!1}else if(te(e)||ee(e))return o.push(e),!1;return A},t):1===e&&(t=A.reduce(function(A,e){if(_A(e))switch(e.value){case"circle":return B=0,!1;case Te:return!(B=1);case Ge:case ve:return n=0,!1;case xe:return!(n=1);case Me:return!(n=2);case"cover":case Se:return!(n=3)}else if(ee(e)||te(e))return(n=!Array.isArray(n)?[]:n).push(e),!1;return A},t)),t&&(A=He(r,A),s.push(A))}),{size:n,shape:B,stops:s,position:o,type:2}},Oe=function(A,e){if(22===e.type){var t={url:e.value,type:0};return A.cache.addImage(e.value),t}if(18!==e.type)throw new Error("Unsupported image type "+e.type);t=ke[e.name];if(void 0===t)throw new Error('Attempting to parse an unsupported image function "'+e.name+'"');return t(A,e.values)};var Ve,ke={"linear-gradient":function(t,A){var r=Ce(180),B=[];return Ae(A).forEach(function(A,e){if(0===e){e=A[0];if(20===e.type&&"to"===e.value)return void(r=se(A));if(ne(e))return void(r=le(t,e))}A=He(t,A);B.push(A)}),{angle:r,stops:B,type:1}},"-moz-linear-gradient":Ke,"-ms-linear-gradient":Ke,"-o-linear-gradient":Ke,"-webkit-linear-gradient":Ke,"radial-gradient":function(B,A){var n=0,s=3,o=[],i=[];return Ae(A).forEach(function(A,e){var t,r=!0;0===e&&(t=!1,r=A.reduce(function(A,e){if(t)if(_A(e))switch(e.value){case"center":return i.push(ge),A;case"top":case"left":return i.push(ae),A;case"right":case"bottom":return i.push(we),A}else(te(e)||ee(e))&&i.push(e);else if(_A(e))switch(e.value){case"circle":return n=0,!1;case Te:return!(n=1);case"at":return!(t=!0);case ve:return s=0,!1;case"cover":case xe:return!(s=1);case Ge:case Me:return!(s=2);case Se:return!(s=3)}else if(ee(e)||te(e))return(s=!Array.isArray(s)?[]:s).push(e),!1;return A},r)),r&&(A=He(B,A),o.push(A))}),{size:s,shape:n,stops:o,position:i,type:2}},"-moz-radial-gradient":he,"-ms-radial-gradient":he,"-o-radial-gradient":he,"-webkit-radial-gradient":he,"-webkit-gradient":function(r,A){var e=Ce(180),B=[],n=1;return Ae(A).forEach(function(A,e){var t,A=A[0];if(0===e){if(_A(A)&&"linear"===A.value)return void(n=1);if(_A(A)&&"radial"===A.value)return void(n=2)}18===A.type&&("from"===A.name?(t=ue(r,A.values[0]),B.push({stop:ae,color:t})):"to"===A.name?(t=ue(r,A.values[0]),B.push({stop:we,color:t})):"color-stop"!==A.name||2===(A=A.values.filter($A)).length&&(t=ue(r,A[1]),A=A[0],ZA(A)&&B.push({stop:{type:16,number:100*A.number,flags:A.flags},color:t})))}),1===n?{angle:(e+Ce(180))%Ce(360),stops:B,type:n}:{size:3,shape:0,stops:B,position:[],type:n}}},Re={name:"background-image",initialValue:"none",type:1,prefix:!1,parse:function(e,A){if(0===A.length)return[];var t=A[0];return 20===t.type&&"none"===t.value?[]:A.filter(function(A){return $A(A)&&!(20===(A=A).type&&"none"===A.value||18===A.type&&!ke[A.name])}).map(function(A){return Oe(e,A)})}},Ne={name:"background-origin",initialValue:"border-box",prefix:!1,type:1,parse:function(A,e){return e.map(function(A){if(_A(A))switch(A.value){case"padding-box":return 1;case"content-box":return 2}return 0})}},Pe={name:"background-position",initialValue:"0% 0%",type:1,prefix:!1,parse:function(A,e){return Ae(e).map(function(A){return A.filter(te)}).map(re)}},Xe={name:"background-repeat",initialValue:"repeat",prefix:!1,type:1,parse:function(A,e){return Ae(e).map(function(A){return A.filter(_A).map(function(A){return A.value}).join(" ")}).map(Je)}},Je=function(A){switch(A){case"no-repeat":return 1;case"repeat-x":case"repeat no-repeat":return 2;case"repeat-y":case"no-repeat repeat":return 3;default:return 0}};(he=Ve=Ve||{}).AUTO="auto",he.CONTAIN="contain";function Ye(A,e){return _A(A)&&"normal"===A.value?1.2*e:17===A.type?e*A.number:te(A)?Ue(A,e):e}var We,Ze,_e={name:"background-size",initialValue:"0",prefix:!(he.COVER="cover"),type:1,parse:function(A,e){return Ae(e).map(function(A){return A.filter(qe)})}},qe=function(A){return _A(A)||te(A)},he=function(A){return{name:"border-"+A+"-color",initialValue:"transparent",prefix:!1,type:3,format:"color"}},je=he("top"),ze=he("right"),$e=he("bottom"),At=he("left"),he=function(A){return{name:"border-radius-"+A,initialValue:"0 0",prefix:!1,type:1,parse:function(A,e){return re(e.filter(te))}}},et=he("top-left"),tt=he("top-right"),rt=he("bottom-right"),Bt=he("bottom-left"),he=function(A){return{name:"border-"+A+"-style",initialValue:"solid",prefix:!1,type:2,parse:function(A,e){switch(e){case"none":return 0;case"dashed":return 2;case"dotted":return 3;case"double":return 4}return 1}}},nt=he("top"),st=he("right"),ot=he("bottom"),it=he("left"),he=function(A){return{name:"border-"+A+"-width",initialValue:"0",type:0,prefix:!1,parse:function(A,e){return WA(e)?e.number:0}}},Qt=he("top"),ct=he("right"),at=he("bottom"),gt=he("left"),wt={name:"color",initialValue:"transparent",prefix:!1,type:3,format:"color"},Ut={name:"direction",initialValue:"ltr",prefix:!1,type:2,parse:function(A,e){return"rtl"!==e?0:1}},lt={name:"display",initialValue:"inline-block",prefix:!1,type:1,parse:function(A,e){return e.filter(_A).reduce(function(A,e){return A|Ct(e.value)},0)}},Ct=function(A){switch(A){case"block":case"-webkit-box":return 2;case"inline":return 4;case"run-in":return 8;case"flow":return 16;case"flow-root":return 32;case"table":return 64;case"flex":case"-webkit-flex":return 128;case"grid":case"-ms-grid":return 256;case"ruby":return 512;case"subgrid":return 1024;case"list-item":return 2048;case"table-row-group":return 4096;case"table-header-group":return 8192;case"table-footer-group":return 16384;case"table-row":return 32768;case"table-cell":return 65536;case"table-column-group":return 131072;case"table-column":return 262144;case"table-caption":return 524288;case"ruby-base":return 1048576;case"ruby-text":return 2097152;case"ruby-base-container":return 4194304;case"ruby-text-container":return 8388608;case"contents":return 16777216;case"inline-block":return 33554432;case"inline-list-item":return 67108864;case"inline-table":return 134217728;case"inline-flex":return 268435456;case"inline-grid":return 536870912}return 0},ut={name:"float",initialValue:"none",prefix:!1,type:2,parse:function(A,e){switch(e){case"left":return 1;case"right":return 2;case"inline-start":return 3;case"inline-end":return 4}return 0}},Ft={name:"letter-spacing",initialValue:"0",prefix:!1,type:0,parse:function(A,e){return!(20===e.type&&"normal"===e.value||17!==e.type&&15!==e.type)?e.number:0}},ht={name:"line-break",initialValue:(he=We=We||{}).NORMAL="normal",prefix:!(he.STRICT="strict"),type:2,parse:function(A,e){return"strict"!==e?We.NORMAL:We.STRICT}},dt={name:"line-height",initialValue:"normal",prefix:!1,type:4},ft={name:"list-style-image",initialValue:"none",type:0,prefix:!1,parse:function(A,e){return 20===e.type&&"none"===e.value?null:Oe(A,e)}},Ht={name:"list-style-position",initialValue:"outside",prefix:!1,type:2,parse:function(A,e){return"inside"!==e?1:0}},pt={name:"list-style-type",initialValue:"none",prefix:!1,type:2,parse:function(A,e){switch(e){case"disc":return 0;case"circle":return 1;case"square":return 2;case"decimal":return 3;case"cjk-decimal":return 4;case"decimal-leading-zero":return 5;case"lower-roman":return 6;case"upper-roman":return 7;case"lower-greek":return 8;case"lower-alpha":return 9;case"upper-alpha":return 10;case"arabic-indic":return 11;case"armenian":return 12;case"bengali":return 13;case"cambodian":return 14;case"cjk-earthly-branch":return 15;case"cjk-heavenly-stem":return 16;case"cjk-ideographic":return 17;case"devanagari":return 18;case"ethiopic-numeric":return 19;case"georgian":return 20;case"gujarati":return 21;case"gurmukhi":case"hebrew":return 22;case"hiragana":return 23;case"hiragana-iroha":return 24;case"japanese-formal":return 25;case"japanese-informal":return 26;case"kannada":return 27;case"katakana":return 28;case"katakana-iroha":return 29;case"khmer":return 30;case"korean-hangul-formal":return 31;case"korean-hanja-formal":return 32;case"korean-hanja-informal":return 33;case"lao":return 34;case"lower-armenian":return 35;case"malayalam":return 36;case"mongolian":return 37;case"myanmar":return 38;case"oriya":return 39;case"persian":return 40;case"simp-chinese-formal":return 41;case"simp-chinese-informal":return 42;case"tamil":return 43;case"telugu":return 44;case"thai":return 45;case"tibetan":return 46;case"trad-chinese-formal":return 47;case"trad-chinese-informal":return 48;case"upper-armenian":return 49;case"disclosure-open":return 50;case"disclosure-closed":return 51;default:return-1}}},he=function(A){return{name:"margin-"+A,initialValue:"0",prefix:!1,type:4}},Et=he("top"),It=he("right"),yt=he("bottom"),Kt=he("left"),mt={name:"overflow",initialValue:"visible",prefix:!1,type:1,parse:function(A,e){return e.filter(_A).map(function(A){switch(A.value){case"hidden":return 1;case"scroll":return 2;case"clip":return 3;case"auto":return 4;default:return 0}})}},Lt={name:"overflow-wrap",initialValue:"normal",prefix:!1,type:2,parse:function(A,e){return"break-word"!==e?"normal":"break-word"}},he=function(A){return{name:"padding-"+A,initialValue:"0",prefix:!1,type:3,format:"length-percentage"}},bt=he("top"),Dt=he("right"),vt=he("bottom"),xt=he("left"),Mt={name:"text-align",initialValue:"left",prefix:!1,type:2,parse:function(A,e){switch(e){case"right":return 2;case"center":case"justify":return 1;default:return 0}}},St={name:"position",initialValue:"static",prefix:!1,type:2,parse:function(A,e){switch(e){case"relative":return 1;case"absolute":return 2;case"fixed":return 3;case"sticky":return 4}return 0}},Tt={name:"text-shadow",initialValue:"none",type:1,prefix:!1,parse:function(n,A){return 1===A.length&&jA(A[0],"none")?[]:Ae(A).map(function(A){for(var e={color:Le.TRANSPARENT,offsetX:ae,offsetY:ae,blur:ae},t=0,r=0;r<A.length;r++){var B=A[r];ee(B)?(0===t?e.offsetX=B:1===t?e.offsetY=B:e.blur=B,t++):e.color=ue(n,B)}return e})}},Gt={name:"text-transform",initialValue:"none",prefix:!1,type:2,parse:function(A,e){switch(e){case"uppercase":return 2;case"lowercase":return 1;case"capitalize":return 3}return 0}},Ot={name:"transform",initialValue:"none",prefix:!0,type:0,parse:function(A,e){if(20===e.type&&"none"===e.value)return null;if(18!==e.type)return null;var t=Vt[e.name];if(void 0===t)throw new Error('Attempting to parse an unsupported transform function "'+e.name+'"');return t(e.values)}},Vt={matrix:function(A){A=A.filter(function(A){return 17===A.type}).map(function(A){return A.number});return 6===A.length?A:null},matrix3d:function(A){var e=A.filter(function(A){return 17===A.type}).map(function(A){return A.number}),t=e[0],r=e[1];e[2],e[3];var B=e[4],n=e[5];e[6],e[7],e[8],e[9],e[10],e[11];var s=e[12],A=e[13];return e[14],e[15],16===e.length?[t,r,B,n,s,A]:null}},he={type:16,number:50,flags:4},kt=[he,he],Rt={name:"transform-origin",initialValue:"50% 50%",prefix:!0,type:1,parse:function(A,e){e=e.filter(te);return 2!==e.length?kt:[e[0],e[1]]}},Nt={name:"visible",initialValue:"none",prefix:!1,type:2,parse:function(A,e){switch(e){case"hidden":return 1;case"collapse":return 2;default:return 0}}};(he=Ze=Ze||{}).NORMAL="normal",he.BREAK_ALL="break-all";function Pt(A,e){return 0!=(A&e)}function Xt(A,e,t){return(A=A&&A[Math.min(e,A.length-1)])?t?A.open:A.close:""}var Jt={name:"word-break",initialValue:"normal",prefix:!(he.KEEP_ALL="keep-all"),type:2,parse:function(A,e){switch(e){case"break-all":return Ze.BREAK_ALL;case"keep-all":return Ze.KEEP_ALL;default:return Ze.NORMAL}}},Yt={name:"z-index",initialValue:"auto",prefix:!1,type:0,parse:function(A,e){if(20===e.type)return{auto:!0,order:0};if(ZA(e))return{auto:!1,order:e.number};throw new Error("Invalid z-index number parsed")}},Wt=function(A,e){if(15===e.type)switch(e.unit.toLowerCase()){case"s":return 1e3*e.number;case"ms":return e.number}throw new Error("Unsupported time type")},Zt={name:"opacity",initialValue:"1",type:0,prefix:!1,parse:function(A,e){return ZA(e)?e.number:1}},_t={name:"text-decoration-color",initialValue:"transparent",prefix:!1,type:3,format:"color"},qt={name:"text-decoration-line",initialValue:"none",prefix:!1,type:1,parse:function(A,e){return e.filter(_A).map(function(A){switch(A.value){case"underline":return 1;case"overline":return 2;case"line-through":return 3;case"none":return 4}return 0}).filter(function(A){return 0!==A})}},jt={name:"font-family",initialValue:"",prefix:!1,type:1,parse:function(A,e){var t=[],r=[];return e.forEach(function(A){switch(A.type){case 20:case 0:t.push(A.value);break;case 17:t.push(A.number.toString());break;case 4:r.push(t.join(" ")),t.length=0}}),t.length&&r.push(t.join(" ")),r.map(function(A){return-1===A.indexOf(" ")?A:"'"+A+"'"})}},zt={name:"font-size",initialValue:"0",prefix:!1,type:3,format:"length"},$t={name:"font-weight",initialValue:"normal",type:0,prefix:!1,parse:function(A,e){return ZA(e)?e.number:!_A(e)||"bold"!==e.value?400:700}},Ar={name:"font-variant",initialValue:"none",type:1,prefix:!1,parse:function(A,e){return e.filter(_A).map(function(A){return A.value})}},er={name:"font-style",initialValue:"normal",prefix:!1,type:2,parse:function(A,e){switch(e){case"oblique":return"oblique";case"italic":return"italic";default:return"normal"}}},tr={name:"content",initialValue:"none",type:1,prefix:!1,parse:function(A,e){if(0===e.length)return[];var t=e[0];return 20===t.type&&"none"===t.value?[]:e}},rr={name:"counter-increment",initialValue:"none",prefix:!0,type:1,parse:function(A,e){if(0===e.length)return null;var t=e[0];if(20===t.type&&"none"===t.value)return null;for(var r=[],B=e.filter(zA),n=0;n<B.length;n++){var s=B[n],o=B[n+1];20===s.type&&(o=o&&ZA(o)?o.number:1,r.push({counter:s.value,increment:o}))}return r}},Br={name:"counter-reset",initialValue:"none",prefix:!0,type:1,parse:function(A,e){if(0===e.length)return[];for(var t=[],r=e.filter(zA),B=0;B<r.length;B++){var n=r[B],s=r[B+1];_A(n)&&"none"!==n.value&&(s=s&&ZA(s)?s.number:0,t.push({counter:n.value,reset:s}))}return t}},nr={name:"duration",initialValue:"0s",prefix:!1,type:1,parse:function(e,A){return A.filter(WA).map(function(A){return Wt(e,A)})}},sr={name:"quotes",initialValue:"none",prefix:!0,type:1,parse:function(A,e){if(0===e.length)return null;var t=e[0];if(20===t.type&&"none"===t.value)return null;var r=[],B=e.filter(qA);if(B.length%2!=0)return null;for(var n=0;n<B.length;n+=2){var s=B[n].value,o=B[n+1].value;r.push({open:s,close:o})}return r}},or={name:"box-shadow",initialValue:"none",type:1,prefix:!1,parse:function(n,A){return 1===A.length&&jA(A[0],"none")?[]:Ae(A).map(function(A){for(var e={color:255,offsetX:ae,offsetY:ae,blur:ae,spread:ae,inset:!1},t=0,r=0;r<A.length;r++){var B=A[r];jA(B,"inset")?e.inset=!0:ee(B)?(0===t?e.offsetX=B:1===t?e.offsetY=B:2===t?e.blur=B:e.spread=B,t++):e.color=ue(n,B)}return e})}},ir={name:"paint-order",initialValue:"normal",prefix:!1,type:1,parse:function(A,e){var t=[];return e.filter(_A).forEach(function(A){switch(A.value){case"stroke":t.push(1);break;case"fill":t.push(0);break;case"markers":t.push(2)}}),[0,1,2].forEach(function(A){-1===t.indexOf(A)&&t.push(A)}),t}},Qr={name:"-webkit-text-stroke-color",initialValue:"currentcolor",prefix:!1,type:3,format:"color"},cr={name:"-webkit-text-stroke-width",initialValue:"0",type:0,prefix:!1,parse:function(A,e){return WA(e)?e.number:0}},ar=(gr.prototype.isVisible=function(){return 0<this.display&&0<this.opacity&&0===this.visibility},gr.prototype.isTransparent=function(){return oe(this.backgroundColor)},gr.prototype.isTransformed=function(){return null!==this.transform},gr.prototype.isPositioned=function(){return 0!==this.position},gr.prototype.isPositionedWithZIndex=function(){return this.isPositioned()&&!this.zIndex.auto},gr.prototype.isFloating=function(){return 0!==this.float},gr.prototype.isInlineLevel=function(){return Pt(this.display,4)||Pt(this.display,33554432)||Pt(this.display,268435456)||Pt(this.display,536870912)||Pt(this.display,67108864)||Pt(this.display,134217728)},gr);function gr(A,e){this.animationDuration=lr(A,nr,e.animationDuration),this.backgroundClip=lr(A,be,e.backgroundClip),this.backgroundColor=lr(A,De,e.backgroundColor),this.backgroundImage=lr(A,Re,e.backgroundImage),this.backgroundOrigin=lr(A,Ne,e.backgroundOrigin),this.backgroundPosition=lr(A,Pe,e.backgroundPosition),this.backgroundRepeat=lr(A,Xe,e.backgroundRepeat),this.backgroundSize=lr(A,_e,e.backgroundSize),this.borderTopColor=lr(A,je,e.borderTopColor),this.borderRightColor=lr(A,ze,e.borderRightColor),this.borderBottomColor=lr(A,$e,e.borderBottomColor),this.borderLeftColor=lr(A,At,e.borderLeftColor),this.borderTopLeftRadius=lr(A,et,e.borderTopLeftRadius),this.borderTopRightRadius=lr(A,tt,e.borderTopRightRadius),this.borderBottomRightRadius=lr(A,rt,e.borderBottomRightRadius),this.borderBottomLeftRadius=lr(A,Bt,e.borderBottomLeftRadius),this.borderTopStyle=lr(A,nt,e.borderTopStyle),this.borderRightStyle=lr(A,st,e.borderRightStyle),this.borderBottomStyle=lr(A,ot,e.borderBottomStyle),this.borderLeftStyle=lr(A,it,e.borderLeftStyle),this.borderTopWidth=lr(A,Qt,e.borderTopWidth),this.borderRightWidth=lr(A,ct,e.borderRightWidth),this.borderBottomWidth=lr(A,at,e.borderBottomWidth),this.borderLeftWidth=lr(A,gt,e.borderLeftWidth),this.boxShadow=lr(A,or,e.boxShadow),this.color=lr(A,wt,e.color),this.direction=lr(A,Ut,e.direction),this.display=lr(A,lt,e.display),this.float=lr(A,ut,e.cssFloat),this.fontFamily=lr(A,jt,e.fontFamily),this.fontSize=lr(A,zt,e.fontSize),this.fontStyle=lr(A,er,e.fontStyle),this.fontVariant=lr(A,Ar,e.fontVariant),this.fontWeight=lr(A,$t,e.fontWeight),this.letterSpacing=lr(A,Ft,e.letterSpacing),this.lineBreak=lr(A,ht,e.lineBreak),this.lineHeight=lr(A,dt,e.lineHeight),this.listStyleImage=lr(A,ft,e.listStyleImage),this.listStylePosition=lr(A,Ht,e.listStylePosition),this.listStyleType=lr(A,pt,e.listStyleType),this.marginTop=lr(A,Et,e.marginTop),this.marginRight=lr(A,It,e.marginRight),this.marginBottom=lr(A,yt,e.marginBottom),this.marginLeft=lr(A,Kt,e.marginLeft),this.opacity=lr(A,Zt,e.opacity);var t=lr(A,mt,e.overflow);this.overflowX=t[0],this.overflowY=t[1<t.length?1:0],this.overflowWrap=lr(A,Lt,e.overflowWrap),this.paddingTop=lr(A,bt,e.paddingTop),this.paddingRight=lr(A,Dt,e.paddingRight),this.paddingBottom=lr(A,vt,e.paddingBottom),this.paddingLeft=lr(A,xt,e.paddingLeft),this.paintOrder=lr(A,ir,e.paintOrder),this.position=lr(A,St,e.position),this.textAlign=lr(A,Mt,e.textAlign),this.textDecorationColor=lr(A,_t,null!==(t=e.textDecorationColor)&&void 0!==t?t:e.color),this.textDecorationLine=lr(A,qt,null!==(t=e.textDecorationLine)&&void 0!==t?t:e.textDecoration),this.textShadow=lr(A,Tt,e.textShadow),this.textTransform=lr(A,Gt,e.textTransform),this.transform=lr(A,Ot,e.transform),this.transformOrigin=lr(A,Rt,e.transformOrigin),this.visibility=lr(A,Nt,e.visibility),this.webkitTextStrokeColor=lr(A,Qr,e.webkitTextStrokeColor),this.webkitTextStrokeWidth=lr(A,cr,e.webkitTextStrokeWidth),this.wordBreak=lr(A,Jt,e.wordBreak),this.zIndex=lr(A,Yt,e.zIndex)}for(var wr=function(A,e){this.content=lr(A,tr,e.content),this.quotes=lr(A,sr,e.quotes)},Ur=function(A,e){this.counterIncrement=lr(A,rr,e.counterIncrement),this.counterReset=lr(A,Br,e.counterReset)},lr=function(A,e,t){var r=new PA,t=null!=t?t.toString():e.initialValue;r.write(t);var B=new JA(r.read());switch(e.type){case 2:var n=B.parseComponentValue();return e.parse(A,_A(n)?n.value:e.initialValue);case 0:return e.parse(A,B.parseComponentValue());case 1:return e.parse(A,B.parseComponentValues());case 4:return B.parseComponentValue();case 3:switch(e.format){case"angle":return le(A,B.parseComponentValue());case"color":return ue(A,B.parseComponentValue());case"image":return Oe(A,B.parseComponentValue());case"length":var s=B.parseComponentValue();return ee(s)?s:ae;case"length-percentage":s=B.parseComponentValue();return te(s)?s:ae;case"time":return Wt(A,B.parseComponentValue())}}},Cr=function(A,e){A=function(A){switch(A.getAttribute("data-html2canvas-debug")){case"all":return 1;case"clone":return 2;case"parse":return 3;case"render":return 4;default:return 0}}(A);return 1===A||e===A},ur=function(A,e){this.context=A,this.textNodes=[],this.elements=[],this.flags=0,Cr(e,3),this.styles=new ar(A,window.getComputedStyle(e,null)),JB(e)&&(this.styles.animationDuration.some(function(A){return 0<A})&&(e.style.animationDuration="0s"),null!==this.styles.transform&&(e.style.transform="none")),this.bounds=f(this.context,e),Cr(e,4)&&(this.flags|=16)},Fr="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",hr="undefined"==typeof Uint8Array?[]:new Uint8Array(256),dr=0;dr<Fr.length;dr++)hr[Fr.charCodeAt(dr)]=dr;function fr(A,e,t){return A.slice?A.slice(e,t):new Uint16Array(Array.prototype.slice.call(A,e,t))}var Hr=(pr.prototype.get=function(A){var e;if(0<=A){if(A<55296||56319<A&&A<=65535)return e=this.index[A>>5],this.data[e=(e<<2)+(31&A)];if(A<=65535)return e=this.index[2048+(A-55296>>5)],this.data[e=(e<<2)+(31&A)];if(A<this.highStart)return e=this.index[e=2080+(A>>11)],e=this.index[e+=A>>5&63],this.data[e=(e<<2)+(31&A)];if(A<=1114111)return this.data[this.highValueIndex]}return this.errorValue},pr);function pr(A,e,t,r,B,n){this.initialValue=A,this.errorValue=e,this.highStart=t,this.highValueIndex=r,this.index=B,this.data=n}for(var Er="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/",Ir="undefined"==typeof Uint8Array?[]:new Uint8Array(256),yr=0;yr<Er.length;yr++)Ir[Er.charCodeAt(yr)]=yr;function Kr(A){return kr.get(A)}function mr(A){var t=function(A){for(var e=[],t=0,r=A.length;t<r;){var B,n=A.charCodeAt(t++);55296<=n&&n<=56319&&t<r?56320==(64512&(B=A.charCodeAt(t++)))?e.push(((1023&n)<<10)+(1023&B)+65536):(e.push(n),t--):e.push(n)}return e}(A),r=t.length,B=0,n=0,s=t.map(Kr);return{next:function(){if(r<=B)return{done:!0,value:null};for(var A=Rr;B<r&&(A=function(A,e){var t=e-2,r=A[t],B=A[e-1],e=A[e];if(2===B&&3===e)return Rr;if(2===B||3===B||4===B)return"÷";if(2===e||3===e||4===e)return"÷";if(B===Tr&&-1!==[Tr,Gr,Or,Vr].indexOf(e))return Rr;if(!(B!==Or&&B!==Gr||e!==Gr&&10!==e))return Rr;if((B===Vr||10===B)&&10===e)return Rr;if(13===e||5===e)return Rr;if(7===e)return Rr;if(1===B)return Rr;if(13===B&&14===e){for(;5===r;)r=A[--t];if(14===r)return Rr}if(15===B&&15===e){for(var n=0;15===r;)n++,r=A[--t];if(n%2==0)return Rr}return"÷"}(s,++B))===Rr;);if(A===Rr&&B!==r)return{done:!0,value:null};var e=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];if(String.fromCodePoint)return String.fromCodePoint.apply(String,A);var t=A.length;if(!t)return"";for(var r=[],B=-1,n="";++B<t;){var s=A[B];s<=65535?r.push(s):(s-=65536,r.push(55296+(s>>10),s%1024+56320)),(B+1===t||16384<r.length)&&(n+=String.fromCharCode.apply(String,r),r.length=0)}return n}.apply(null,t.slice(n,B));return n=B,{value:e,done:!1}}}}function Lr(A){return 0===A[0]&&255===A[1]&&0===A[2]&&255===A[3]}var br,Dr,vr,xr,Mr,Sr,Tr=8,Gr=9,Or=11,Vr=12,kr=(vr=function(A){var e,t,r,B,n=.75*A.length,s=A.length,o=0;"="===A[A.length-1]&&(n--,"="===A[A.length-2]&&n--);for(var n=new("undefined"!=typeof ArrayBuffer&&"undefined"!=typeof Uint8Array&&void 0!==Uint8Array.prototype.slice?ArrayBuffer:Array)(n),i=Array.isArray(n)?n:new Uint8Array(n),Q=0;Q<s;Q+=4)e=hr[A.charCodeAt(Q)],t=hr[A.charCodeAt(Q+1)],r=hr[A.charCodeAt(Q+2)],B=hr[A.charCodeAt(Q+3)],i[o++]=e<<2|t>>4,i[o++]=(15&t)<<4|r>>2,i[o++]=(3&r)<<6|63&B;return n}(br="AAAAAAAAAAAAEA4AGBkAAFAaAAACAAAAAAAIABAAGAAwADgACAAQAAgAEAAIABAACAAQAAgAEAAIABAACAAQAAgAEAAIABAAQABIAEQATAAIABAACAAQAAgAEAAIABAAVABcAAgAEAAIABAACAAQAGAAaABwAHgAgACIAI4AlgAIABAAmwCjAKgAsAC2AL4AvQDFAMoA0gBPAVYBWgEIAAgACACMANoAYgFkAWwBdAF8AX0BhQGNAZUBlgGeAaMBlQGWAasBswF8AbsBwwF0AcsBYwHTAQgA2wG/AOMBdAF8AekB8QF0AfkB+wHiAHQBfAEIAAMC5gQIAAsCEgIIAAgAFgIeAggAIgIpAggAMQI5AkACygEIAAgASAJQAlgCYAIIAAgACAAKBQoFCgUTBRMFGQUrBSsFCAAIAAgACAAIAAgACAAIAAgACABdAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACABoAmgCrwGvAQgAbgJ2AggAHgEIAAgACADnAXsCCAAIAAgAgwIIAAgACAAIAAgACACKAggAkQKZAggAPADJAAgAoQKkAqwCsgK6AsICCADJAggA0AIIAAgACAAIANYC3gIIAAgACAAIAAgACABAAOYCCAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAkASoB+QIEAAgACAA8AEMCCABCBQgACABJBVAFCAAIAAgACAAIAAgACAAIAAgACABTBVoFCAAIAFoFCABfBWUFCAAIAAgACAAIAAgAbQUIAAgACAAIAAgACABzBXsFfQWFBYoFigWKBZEFigWKBYoFmAWfBaYFrgWxBbkFCAAIAAgACAAIAAgACAAIAAgACAAIAMEFCAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAMgFCADQBQgACAAIAAgACAAIAAgACAAIAAgACAAIAO4CCAAIAAgAiQAIAAgACABAAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAD0AggACAD8AggACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIANYFCAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAMDvwAIAAgAJAIIAAgACAAIAAgACAAIAAgACwMTAwgACAB9BOsEGwMjAwgAKwMyAwsFYgE3A/MEPwMIAEUDTQNRAwgAWQOsAGEDCAAIAAgACAAIAAgACABpAzQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFOgU0BTUFNgU3BTgFOQU6BTQFNQU2BTcFOAU5BToFNAU1BTYFNwU4BTkFIQUoBSwFCAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACABtAwgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACABMAEwACAAIAAgACAAIABgACAAIAAgACAC/AAgACAAyAQgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACACAAIAAwAAgACAAIAAgACAAIAAgACAAIAAAARABIAAgACAAIABQASAAIAAgAIABwAEAAjgCIABsAqAC2AL0AigDQAtwC+IJIQqVAZUBWQqVAZUBlQGVAZUBlQGrC5UBlQGVAZUBlQGVAZUBlQGVAXsKlQGVAbAK6wsrDGUMpQzlDJUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAZUBlQGVAfAKAAuZA64AtwCJALoC6ADwAAgAuACgA/oEpgO6AqsD+AAIAAgAswMIAAgACAAIAIkAuwP5AfsBwwPLAwgACAAIAAgACADRA9kDCAAIAOED6QMIAAgACAAIAAgACADuA/YDCAAIAP4DyQAIAAgABgQIAAgAXQAOBAgACAAIAAgACAAIABMECAAIAAgACAAIAAgACAD8AAQBCAAIAAgAGgQiBCoECAExBAgAEAEIAAgACAAIAAgACAAIAAgACAAIAAgACAA4BAgACABABEYECAAIAAgATAQYAQgAVAQIAAgACAAIAAgACAAIAAgACAAIAFoECAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgAOQEIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAB+BAcACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAEABhgSMBAgACAAIAAgAlAQIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAwAEAAQABAADAAMAAwADAAQABAAEAAQABAAEAAQABHATAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgAdQMIAAgACAAIAAgACAAIAMkACAAIAAgAfQMIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACACFA4kDCAAIAAgACAAIAOcBCAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAIcDCAAIAAgACAAIAAgACAAIAAgACAAIAJEDCAAIAAgACADFAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACABgBAgAZgQIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgAbAQCBXIECAAIAHkECAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACABAAJwEQACjBKoEsgQIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAC6BMIECAAIAAgACAAIAAgACABmBAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgAxwQIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAGYECAAIAAgAzgQIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgAigWKBYoFigWKBYoFigWKBd0FXwUIAOIF6gXxBYoF3gT5BQAGCAaKBYoFigWKBYoFigWKBYoFigWKBYoFigXWBIoFigWKBYoFigWKBYoFigWKBYsFEAaKBYoFigWKBYoFigWKBRQGCACKBYoFigWKBQgACAAIANEECAAIABgGigUgBggAJgYIAC4GMwaKBYoF0wQ3Bj4GigWKBYoFigWKBYoFigWKBYoFigWKBYoFigUIAAgACAAIAAgACAAIAAgAigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWKBYoFigWLBf///////wQABAAEAAQABAAEAAQABAAEAAQAAwAEAAQAAgAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAQADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAUAAAAFAAUAAAAFAAUAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAEAAQABAAEAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUAAQAAAAUABQAFAAUABQAFAAAAAAAFAAUAAAAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAFAAUAAQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABwAFAAUABQAFAAAABwAHAAcAAAAHAAcABwAFAAEAAAAAAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAFAAUABQAFAAcABwAFAAUAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAAAAQABAAAAAAAAAAAAAAAFAAUABQAFAAAABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAHAAcABwAHAAcAAAAHAAcAAAAAAAUABQAHAAUAAQAHAAEABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABwABAAUABQAFAAUAAAAAAAAAAAAAAAEAAQABAAEAAQABAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABwAFAAUAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUAAQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQABQANAAQABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQABAAEAAQABAAEAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABAAEAAQABAAEAAQABAAEAAQABAAEAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEAAQABAAEAAQABAAEAAQABAAAAAAAAAAAAAAAAAAAAAAABQAHAAUABQAFAAAAAAAAAAcABQAFAAUABQAFAAQABAAEAAQABAAEAAQABAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAEAAQABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUAAAAFAAUABQAFAAUAAAAFAAUABQAAAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAAAAAAAAAAAAUABQAFAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAHAAUAAAAHAAcABwAFAAUABQAFAAUABQAFAAUABwAHAAcABwAFAAcABwAAAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABwAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAUABwAHAAUABQAFAAUAAAAAAAcABwAAAAAABwAHAAUAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAABQAFAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAABwAHAAcABQAFAAAAAAAAAAAABQAFAAAAAAAFAAUABQAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAFAAUABQAFAAUAAAAFAAUABwAAAAcABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAFAAUABwAFAAUABQAFAAAAAAAHAAcAAAAAAAcABwAFAAAAAAAAAAAAAAAAAAAABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAcABwAAAAAAAAAHAAcABwAAAAcABwAHAAUAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAABQAHAAcABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABwAHAAcABwAAAAUABQAFAAAABQAFAAUABQAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAcABQAHAAcABQAHAAcAAAAFAAcABwAAAAcABwAFAAUAAAAAAAAAAAAAAAAAAAAFAAUAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAUABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAFAAcABwAFAAUABQAAAAUAAAAHAAcABwAHAAcABwAHAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAHAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAABwAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAUAAAAFAAAAAAAAAAAABwAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABwAFAAUABQAFAAUAAAAFAAUAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABwAFAAUABQAFAAUABQAAAAUABQAHAAcABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABQAFAAAAAAAAAAAABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAcABQAFAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAHAAUABQAFAAUABQAFAAUABwAHAAcABwAHAAcABwAHAAUABwAHAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABwAHAAcABwAFAAUABwAHAAcAAAAAAAAAAAAHAAcABQAHAAcABwAHAAcABwAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAcABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABQAHAAUABQAFAAUABQAFAAUAAAAFAAAABQAAAAAABQAFAAUABQAFAAUABQAFAAcABwAHAAcABwAHAAUABQAFAAUABQAFAAUABQAFAAUAAAAAAAUABQAFAAUABQAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABwAFAAcABwAHAAcABwAFAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAUABQAFAAUABwAHAAUABQAHAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAcABQAFAAcABwAHAAUABwAFAAUABQAHAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAcABwAHAAcABwAHAAUABQAFAAUABQAFAAUABQAHAAcABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUAAAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAcABQAFAAUABQAFAAUABQAAAAAAAAAAAAUAAAAAAAAAAAAAAAAABQAAAAAABwAFAAUAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAAABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUAAAAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAABQAAAAAAAAAFAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAUABQAHAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABwAHAAcABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAUABQAFAAUABQAHAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAcABwAFAAUABQAFAAcABwAFAAUABwAHAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAFAAcABwAFAAUABwAHAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAFAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAFAAUABQAAAAAABQAFAAAAAAAAAAAAAAAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABQAFAAcABwAAAAAAAAAAAAAABwAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABwAFAAcABwAFAAcABwAAAAcABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAAAAAAAAAAAAAAAAAFAAUABQAAAAUABQAAAAAAAAAAAAAABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABQAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABwAFAAUABQAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAcABQAFAAUABQAFAAUABQAFAAUABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAFAAUABQAHAAcABQAHAAUABQAAAAAAAAAAAAAAAAAFAAAABwAHAAcABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABwAHAAcABwAAAAAABwAHAAAAAAAHAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAAAAAAFAAUABQAFAAUABQAFAAAAAAAAAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAFAAUABQAFAAUABQAFAAUABwAHAAUABQAFAAcABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAHAAcABQAFAAUABQAFAAUABwAFAAcABwAFAAcABQAFAAcABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAHAAcABQAFAAUABQAAAAAABwAHAAcABwAFAAUABwAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABwAHAAUABQAFAAUABQAFAAUABQAHAAcABQAHAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABwAFAAcABwAFAAUABQAFAAUABQAHAAUAAAAAAAAAAAAAAAAAAAAAAAcABwAFAAUABQAFAAcABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAFAAUABQAFAAUABQAFAAUABQAHAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAFAAUABQAFAAAAAAAFAAUABwAHAAcABwAFAAAAAAAAAAcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABwAHAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABQAFAAUABQAFAAUABQAAAAUABQAFAAUABQAFAAcABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUAAAAHAAUABQAFAAUABQAFAAUABwAFAAUABwAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUAAAAAAAAABQAAAAUABQAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAHAAcABwAHAAcAAAAFAAUAAAAHAAcABQAHAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABwAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAAAAAAAAAAAAAAAAAAABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAAAAUABQAFAAAAAAAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAFAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUABQAFAAUABQAAAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAAAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAFAAUABQAAAAAABQAFAAUABQAFAAUABQAAAAUABQAAAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFAAUABQAFAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABQAFAAUABQAFAAUABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAFAAUABQAFAAUADgAOAA4ADgAOAA4ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAA8ADwAPAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAcABwAHAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAgACAAIAAAAAAAAAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAMAAwADAAMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkACQAJAAkAAAAAAAAAAAAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAKAAoACgAAAAAAAAAAAAsADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwACwAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAMAAwADAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAAAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAOAAAAAAAAAAAADgAOAA4AAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAAAA4ADgAOAA4ADgAOAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4AAAAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4AAAAAAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAAAA4AAAAOAAAAAAAAAAAAAAAAAA4AAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAADgAAAAAAAAAAAA4AAAAOAAAAAAAAAAAADgAOAA4AAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAAAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4AAAAAAA4ADgAOAA4ADgAOAA4ADgAOAAAADgAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4AAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4ADgAOAAAAAAAAAAAAAAAAAAAAAAAAAAAADgAOAA4ADgAOAA4AAAAAAAAAAAAAAAAAAAAAAA4ADgAOAA4ADgAOAA4ADgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4AAAAOAA4ADgAOAA4ADgAAAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4ADgAOAA4AAAAAAAAAAAA="),xr=Array.isArray(vr)?function(A){for(var e=A.length,t=[],r=0;r<e;r+=4)t.push(A[r+3]<<24|A[r+2]<<16|A[r+1]<<8|A[r]);return t}(vr):new Uint32Array(vr),Mr=Array.isArray(vr)?function(A){for(var e=A.length,t=[],r=0;r<e;r+=2)t.push(A[r+1]<<8|A[r]);return t}(vr):new Uint16Array(vr),br=fr(Mr,12,xr[4]/2),Dr=2===xr[5]?fr(Mr,(24+xr[4])/2):(vr=xr,Mr=Math.ceil((24+xr[4])/4),vr.slice?vr.slice(Mr,Dr):new Uint32Array(Array.prototype.slice.call(vr,Mr,Dr))),new Hr(xr[0],xr[1],xr[2],xr[3],br,Dr)),Rr="×",Nr=function(A,e,t,r,B){var n="http://www.w3.org/2000/svg",s=document.createElementNS(n,"svg"),n=document.createElementNS(n,"foreignObject");return s.setAttributeNS(null,"width",A.toString()),s.setAttributeNS(null,"height",e.toString()),n.setAttributeNS(null,"width","100%"),n.setAttributeNS(null,"height","100%"),n.setAttributeNS(null,"x",t.toString()),n.setAttributeNS(null,"y",r.toString()),n.setAttributeNS(null,"externalResourcesRequired","true"),s.appendChild(n),n.appendChild(B),s},Pr=function(r){return new Promise(function(A,e){var t=new Image;t.onload=function(){return A(t)},t.onerror=e,t.src="data:image/svg+xml;charset=utf-8,"+encodeURIComponent((new XMLSerializer).serializeToString(r))})},Xr={get SUPPORT_RANGE_BOUNDS(){var A=function(A){if(A.createRange){var e=A.createRange();if(e.getBoundingClientRect){var t=A.createElement("boundtest");t.style.height="123px",t.style.display="block",A.body.appendChild(t),e.selectNode(t);e=e.getBoundingClientRect(),e=Math.round(e.height);if(A.body.removeChild(t),123===e)return!0}}return!1}(document);return Object.defineProperty(Xr,"SUPPORT_RANGE_BOUNDS",{value:A}),A},get SUPPORT_WORD_BREAKING(){var A=Xr.SUPPORT_RANGE_BOUNDS&&function(A){var e=A.createElement("boundtest");e.style.width="50px",e.style.display="block",e.style.fontSize="12px",e.style.letterSpacing="0px",e.style.wordSpacing="0px",A.body.appendChild(e);var r=A.createRange();e.innerHTML="function"==typeof"".repeat?"&#128104;".repeat(10):"";var B=e.firstChild,t=Q(B.data).map(function(A){return g(A)}),n=0,s={},t=t.every(function(A,e){r.setStart(B,n),r.setEnd(B,n+A.length);var t=r.getBoundingClientRect();n+=A.length;A=t.x>s.x||t.y>s.y;return s=t,0===e||A});return A.body.removeChild(e),t}(document);return Object.defineProperty(Xr,"SUPPORT_WORD_BREAKING",{value:A}),A},get SUPPORT_SVG_DRAWING(){var A=function(A){var e=new Image,t=A.createElement("canvas"),A=t.getContext("2d");if(!A)return!1;e.src="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'></svg>";try{A.drawImage(e,0,0),t.toDataURL()}catch(A){return!1}return!0}(document);return Object.defineProperty(Xr,"SUPPORT_SVG_DRAWING",{value:A}),A},get SUPPORT_FOREIGNOBJECT_DRAWING(){var A="function"==typeof Array.from&&"function"==typeof window.fetch?function(t){var A=t.createElement("canvas"),r=100;A.width=r,A.height=r;var B=A.getContext("2d");if(!B)return Promise.reject(!1);B.fillStyle="rgb(0, 255, 0)",B.fillRect(0,0,r,r);var e=new Image,n=A.toDataURL();e.src=n;e=Nr(r,r,0,0,e);return B.fillStyle="red",B.fillRect(0,0,r,r),Pr(e).then(function(A){B.drawImage(A,0,0);var e=B.getImageData(0,0,r,r).data;B.fillStyle="red",B.fillRect(0,0,r,r);A=t.createElement("div");return A.style.backgroundImage="url("+n+")",A.style.height="100px",Lr(e)?Pr(Nr(r,r,0,0,A)):Promise.reject(!1)}).then(function(A){return B.drawImage(A,0,0),Lr(B.getImageData(0,0,r,r).data)}).catch(function(){return!1})}(document):Promise.resolve(!1);return Object.defineProperty(Xr,"SUPPORT_FOREIGNOBJECT_DRAWING",{value:A}),A},get SUPPORT_CORS_IMAGES(){var A=void 0!==(new Image).crossOrigin;return Object.defineProperty(Xr,"SUPPORT_CORS_IMAGES",{value:A}),A},get SUPPORT_RESPONSE_TYPE(){var A="string"==typeof(new XMLHttpRequest).responseType;return Object.defineProperty(Xr,"SUPPORT_RESPONSE_TYPE",{value:A}),A},get SUPPORT_CORS_XHR(){var A="withCredentials"in new XMLHttpRequest;return Object.defineProperty(Xr,"SUPPORT_CORS_XHR",{value:A}),A},get SUPPORT_NATIVE_TEXT_SEGMENTATION(){var A=!("undefined"==typeof Intl||!Intl.Segmenter);return Object.defineProperty(Xr,"SUPPORT_NATIVE_TEXT_SEGMENTATION",{value:A}),A}},Jr=function(A,e){this.text=A,this.bounds=e},Yr=function(A,e){var t=e.ownerDocument;if(t){var r=t.createElement("html2canvaswrapper");r.appendChild(e.cloneNode(!0));t=e.parentNode;if(t){t.replaceChild(r,e);A=f(A,r);return r.firstChild&&t.replaceChild(r.firstChild,r),A}}return d.EMPTY},Wr=function(A,e,t){var r=A.ownerDocument;if(!r)throw new Error("Node has no owner document");r=r.createRange();return r.setStart(A,e),r.setEnd(A,e+t),r},Zr=function(A){if(Xr.SUPPORT_NATIVE_TEXT_SEGMENTATION){var e=new Intl.Segmenter(void 0,{granularity:"grapheme"});return Array.from(e.segment(A)).map(function(A){return A.segment})}return function(A){for(var e,t=mr(A),r=[];!(e=t.next()).done;)e.value&&r.push(e.value.slice());return r}(A)},_r=function(A,e){return 0!==e.letterSpacing?Zr(A):function(A,e){if(Xr.SUPPORT_NATIVE_TEXT_SEGMENTATION){var t=new Intl.Segmenter(void 0,{granularity:"word"});return Array.from(t.segment(A)).map(function(A){return A.segment})}return jr(A,e)}(A,e)},qr=[32,160,4961,65792,65793,4153,4241],jr=function(A,e){for(var t,r=wA(A,{lineBreak:e.lineBreak,wordBreak:"break-word"===e.overflowWrap?"break-word":e.wordBreak}),B=[];!(t=r.next()).done;)!function(){var A,e;t.value&&(A=t.value.slice(),A=Q(A),e="",A.forEach(function(A){-1===qr.indexOf(A)?e+=g(A):(e.length&&B.push(e),B.push(g(A)),e="")}),e.length&&B.push(e))}();return B},zr=function(A,e,t){var B,n,s,o,i;this.text=$r(e.data,t.textTransform),this.textBounds=(B=A,A=this.text,s=e,A=_r(A,n=t),o=[],i=0,A.forEach(function(A){var e,t,r;n.textDecorationLine.length||0<A.trim().length?Xr.SUPPORT_RANGE_BOUNDS?1<(r=Wr(s,i,A.length).getClientRects()).length?(e=Zr(A),t=0,e.forEach(function(A){o.push(new Jr(A,d.fromDOMRectList(B,Wr(s,t+i,A.length).getClientRects()))),t+=A.length})):o.push(new Jr(A,d.fromDOMRectList(B,r))):(r=s.splitText(A.length),o.push(new Jr(A,Yr(B,s))),s=r):Xr.SUPPORT_RANGE_BOUNDS||(s=s.splitText(A.length)),i+=A.length}),o)},$r=function(A,e){switch(e){case 1:return A.toLowerCase();case 3:return A.replace(AB,eB);case 2:return A.toUpperCase();default:return A}},AB=/(^|\s|:|-|\(|\))([a-z])/g,eB=function(A,e,t){return 0<A.length?e+t.toUpperCase():A},tB=(A(rB,Sr=ur),rB);function rB(A,e){A=Sr.call(this,A,e)||this;return A.src=e.currentSrc||e.src,A.intrinsicWidth=e.naturalWidth,A.intrinsicHeight=e.naturalHeight,A.context.cache.addImage(A.src),A}var BB,nB=(A(sB,BB=ur),sB);function sB(A,e){A=BB.call(this,A,e)||this;return A.canvas=e,A.intrinsicWidth=e.width,A.intrinsicHeight=e.height,A}var oB,iB=(A(QB,oB=ur),QB);function QB(A,e){var t=oB.call(this,A,e)||this,r=new XMLSerializer,A=f(A,e);return e.setAttribute("width",A.width+"px"),e.setAttribute("height",A.height+"px"),t.svg="data:image/svg+xml,"+encodeURIComponent(r.serializeToString(e)),t.intrinsicWidth=e.width.baseVal.value,t.intrinsicHeight=e.height.baseVal.value,t.context.cache.addImage(t.svg),t}var cB,aB=(A(gB,cB=ur),gB);function gB(A,e){A=cB.call(this,A,e)||this;return A.value=e.value,A}var wB,UB=(A(lB,wB=ur),lB);function lB(A,e){A=wB.call(this,A,e)||this;return A.start=e.start,A.reversed="boolean"==typeof e.reversed&&!0===e.reversed,A}var CB,uB=[{type:15,flags:0,unit:"px",number:3}],FB=[{type:16,flags:0,number:50}],hB="checkbox",dB="radio",fB="password",HB=707406591,pB=(A(EB,CB=ur),EB);function EB(A,e){var t=CB.call(this,A,e)||this;switch(t.type=e.type.toLowerCase(),t.checked=e.checked,t.value=0===(e=(A=e).type===fB?new Array(A.value.length+1).join("•"):A.value).length?A.placeholder||"":e,t.type!==hB&&t.type!==dB||(t.styles.backgroundColor=3739148031,t.styles.borderTopColor=t.styles.borderRightColor=t.styles.borderBottomColor=t.styles.borderLeftColor=2779096575,t.styles.borderTopWidth=t.styles.borderRightWidth=t.styles.borderBottomWidth=t.styles.borderLeftWidth=1,t.styles.borderTopStyle=t.styles.borderRightStyle=t.styles.borderBottomStyle=t.styles.borderLeftStyle=1,t.styles.backgroundClip=[0],t.styles.backgroundOrigin=[0],t.bounds=(e=t.bounds).width>e.height?new d(e.left+(e.width-e.height)/2,e.top,e.height,e.height):e.width<e.height?new d(e.left,e.top+(e.height-e.width)/2,e.width,e.width):e),t.type){case hB:t.styles.borderTopRightRadius=t.styles.borderTopLeftRadius=t.styles.borderBottomRightRadius=t.styles.borderBottomLeftRadius=uB;break;case dB:t.styles.borderTopRightRadius=t.styles.borderTopLeftRadius=t.styles.borderBottomRightRadius=t.styles.borderBottomLeftRadius=FB}return t}var IB,yB=(A(KB,IB=ur),KB);function KB(A,e){A=IB.call(this,A,e)||this,e=e.options[e.selectedIndex||0];return A.value=e&&e.text||"",A}var mB,LB=(A(bB,mB=ur),bB);function bB(A,e){A=mB.call(this,A,e)||this;return A.value=e.value,A}var DB,vB=(A(xB,DB=ur),xB);function xB(A,e){var t,r,B=DB.call(this,A,e)||this;B.src=e.src,B.width=parseInt(e.width,10)||0,B.height=parseInt(e.height,10)||0,B.backgroundColor=B.styles.backgroundColor;try{e.contentWindow&&e.contentWindow.document&&e.contentWindow.document.documentElement&&(B.tree=kB(A,e.contentWindow.document.documentElement),t=e.contentWindow.document.documentElement?fe(A,getComputedStyle(e.contentWindow.document.documentElement).backgroundColor):Le.TRANSPARENT,r=e.contentWindow.document.body?fe(A,getComputedStyle(e.contentWindow.document.body).backgroundColor):Le.TRANSPARENT,B.backgroundColor=oe(t)?oe(r)?B.styles.backgroundColor:r:t)}catch(A){}return B}function MB(A){return"VIDEO"===A.tagName}function SB(A){return"STYLE"===A.tagName}function TB(A){return 0<A.tagName.indexOf("-")}var GB=["OL","UL","MENU"],OB=function(e,A,t,r){for(var B=A.firstChild;B;B=s){var n,s=B.nextSibling;PB(B)&&0<B.data.trim().length?t.textNodes.push(new zr(e,B,t.styles)):XB(B)&&(rn(B)&&B.assignedNodes?B.assignedNodes().forEach(function(A){return OB(e,A,t,r)}):(n=VB(e,B)).styles.isVisible()&&(RB(B,n,r)?n.flags|=4:NB(n.styles)&&(n.flags|=2),-1!==GB.indexOf(B.tagName)&&(n.flags|=8),t.elements.push(n),B.slot,B.shadowRoot?OB(e,B.shadowRoot,n,r):en(B)||qB(B)||tn(B)||OB(e,B,n,r)))}},VB=function(A,e){return new($B(e)?tB:zB(e)?nB:qB(e)?iB:WB(e)?aB:ZB(e)?UB:_B(e)?pB:tn(e)?yB:en(e)?LB:An(e)?vB:ur)(A,e)},kB=function(A,e){var t=VB(A,e);return t.flags|=4,OB(A,e,t,t),t},RB=function(A,e,t){return e.styles.isPositionedWithZIndex()||e.styles.opacity<1||e.styles.isTransformed()||jB(A)&&t.styles.isTransparent()},NB=function(A){return A.isPositioned()||A.isFloating()},PB=function(A){return A.nodeType===Node.TEXT_NODE},XB=function(A){return A.nodeType===Node.ELEMENT_NODE},JB=function(A){return XB(A)&&void 0!==A.style&&!YB(A)},YB=function(A){return"object"==typeof A.className},WB=function(A){return"LI"===A.tagName},ZB=function(A){return"OL"===A.tagName},_B=function(A){return"INPUT"===A.tagName},qB=function(A){return"svg"===A.tagName},jB=function(A){return"BODY"===A.tagName},zB=function(A){return"CANVAS"===A.tagName},$B=function(A){return"IMG"===A.tagName},An=function(A){return"IFRAME"===A.tagName},en=function(A){return"TEXTAREA"===A.tagName},tn=function(A){return"SELECT"===A.tagName},rn=function(A){return"SLOT"===A.tagName},Bn=(nn.prototype.getCounterValue=function(A){A=this.counters[A];return A&&A.length?A[A.length-1]:1},nn.prototype.getCounterValues=function(A){A=this.counters[A];return A||[]},nn.prototype.pop=function(A){var e=this;A.forEach(function(A){return e.counters[A].pop()})},nn.prototype.parse=function(A){var t=this,e=A.counterIncrement,A=A.counterReset,r=!0;null!==e&&e.forEach(function(A){var e=t.counters[A.counter];e&&0!==A.increment&&(r=!1,e.length||e.push(1),e[Math.max(0,e.length-1)]+=A.increment)});var B=[];return r&&A.forEach(function(A){var e=t.counters[A.counter];B.push(A.counter),(e=e||(t.counters[A.counter]=[])).push(A.reset)}),B},nn);function nn(){this.counters={}}function sn(r,A,e,B,t,n){return r<A||e<r?Fn(r,t,0<n.length):B.integers.reduce(function(A,e,t){for(;e<=r;)r-=e,A+=B.values[t];return A},"")+n}function on(A,e,t,r){for(var B="";t||A--,B=r(A)+B,e<=(A/=e)*e;);return B}function Qn(A,e,t,r,B){var n=t-e+1;return(A<0?"-":"")+(on(Math.abs(A),n,r,function(A){return g(Math.floor(A%n)+e)})+B)}function cn(A,e,t){void 0===t&&(t=". ");var r=e.length;return on(Math.abs(A),r,!1,function(A){return e[Math.floor(A%r)]})+t}function an(A,e,t,r,B,n){if(A<-9999||9999<A)return Fn(A,4,0<B.length);var s=Math.abs(A),o=B;if(0===s)return e[0]+o;for(var i=0;0<s&&i<=4;i++){var Q=s%10;0==Q&&Pt(n,1)&&""!==o?o=e[Q]+o:1<Q||1==Q&&0===i||1==Q&&1===i&&Pt(n,2)||1==Q&&1===i&&Pt(n,4)&&100<A||1==Q&&1<i&&Pt(n,8)?o=e[Q]+(0<i?t[i-1]:"")+o:1==Q&&0<i&&(o=t[i-1]+o),s=Math.floor(s/10)}return(A<0?r:"")+o}var gn,wn={integers:[1e3,900,500,400,100,90,50,40,10,9,5,4,1],values:["M","CM","D","CD","C","XC","L","XL","X","IX","V","IV","I"]},Un={integers:[9e3,8e3,7e3,6e3,5e3,4e3,3e3,2e3,1e3,900,800,700,600,500,400,300,200,100,90,80,70,60,50,40,30,20,10,9,8,7,6,5,4,3,2,1],values:["Ք","Փ","Ւ","Ց","Ր","Տ","Վ","Ս","Ռ","Ջ","Պ","Չ","Ո","Շ","Ն","Յ","Մ","Ճ","Ղ","Ձ","Հ","Կ","Ծ","Խ","Լ","Ի","Ժ","Թ","Ը","Է","Զ","Ե","Դ","Գ","Բ","Ա"]},ln={integers:[1e4,9e3,8e3,7e3,6e3,5e3,4e3,3e3,2e3,1e3,400,300,200,100,90,80,70,60,50,40,30,20,19,18,17,16,15,10,9,8,7,6,5,4,3,2,1],values:["י׳","ט׳","ח׳","ז׳","ו׳","ה׳","ד׳","ג׳","ב׳","א׳","ת","ש","ר","ק","צ","פ","ע","ס","נ","מ","ל","כ","יט","יח","יז","טז","טו","י","ט","ח","ז","ו","ה","ד","ג","ב","א"]},Cn={integers:[1e4,9e3,8e3,7e3,6e3,5e3,4e3,3e3,2e3,1e3,900,800,700,600,500,400,300,200,100,90,80,70,60,50,40,30,20,10,9,8,7,6,5,4,3,2,1],values:["ჵ","ჰ","ჯ","ჴ","ხ","ჭ","წ","ძ","ც","ჩ","შ","ყ","ღ","ქ","ფ","ჳ","ტ","ს","რ","ჟ","პ","ო","ჲ","ნ","მ","ლ","კ","ი","თ","ჱ","ზ","ვ","ე","დ","გ","ბ","ა"]},un="마이너스",Fn=function(A,e,t){var r=t?". ":"",B=t?"、":"",n=t?", ":"",s=t?" ":"";switch(e){case 0:return"•"+s;case 1:return"◦"+s;case 2:return"◾"+s;case 5:var o=Qn(A,48,57,!0,r);return o.length<4?"0"+o:o;case 4:return cn(A,"〇一二三四五六七八九",B);case 6:return sn(A,1,3999,wn,3,r).toLowerCase();case 7:return sn(A,1,3999,wn,3,r);case 8:return Qn(A,945,969,!1,r);case 9:return Qn(A,97,122,!1,r);case 10:return Qn(A,65,90,!1,r);case 11:return Qn(A,1632,1641,!0,r);case 12:case 49:return sn(A,1,9999,Un,3,r);case 35:return sn(A,1,9999,Un,3,r).toLowerCase();case 13:return Qn(A,2534,2543,!0,r);case 14:case 30:return Qn(A,6112,6121,!0,r);case 15:return cn(A,"子丑寅卯辰巳午未申酉戌亥",B);case 16:return cn(A,"甲乙丙丁戊己庚辛壬癸",B);case 17:case 48:return an(A,"零一二三四五六七八九","十百千萬","負",B,14);case 47:return an(A,"零壹貳參肆伍陸柒捌玖","拾佰仟萬","負",B,15);case 42:return an(A,"零一二三四五六七八九","十百千萬","负",B,14);case 41:return an(A,"零壹贰叁肆伍陆柒捌玖","拾佰仟萬","负",B,15);case 26:return an(A,"〇一二三四五六七八九","十百千万","マイナス",B,0);case 25:return an(A,"零壱弐参四伍六七八九","拾百千万","マイナス",B,7);case 31:return an(A,"영일이삼사오육칠팔구","십백천만",un,n,7);case 33:return an(A,"零一二三四五六七八九","十百千萬",un,n,0);case 32:return an(A,"零壹貳參四五六七八九","拾百千",un,n,7);case 18:return Qn(A,2406,2415,!0,r);case 20:return sn(A,1,19999,Cn,3,r);case 21:return Qn(A,2790,2799,!0,r);case 22:return Qn(A,2662,2671,!0,r);case 22:return sn(A,1,10999,ln,3,r);case 23:return cn(A,"あいうえおかきくけこさしすせそたちつてとなにぬねのはひふへほまみむめもやゆよらりるれろわゐゑをん");case 24:return cn(A,"いろはにほへとちりぬるをわかよたれそつねならむうゐのおくやまけふこえてあさきゆめみしゑひもせす");case 27:return Qn(A,3302,3311,!0,r);case 28:return cn(A,"アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヰヱヲン",B);case 29:return cn(A,"イロハニホヘトチリヌルヲワカヨタレソツネナラムウヰノオクヤマケフコエテアサキユメミシヱヒモセス",B);case 34:return Qn(A,3792,3801,!0,r);case 37:return Qn(A,6160,6169,!0,r);case 38:return Qn(A,4160,4169,!0,r);case 39:return Qn(A,2918,2927,!0,r);case 40:return Qn(A,1776,1785,!0,r);case 43:return Qn(A,3046,3055,!0,r);case 44:return Qn(A,3174,3183,!0,r);case 45:return Qn(A,3664,3673,!0,r);case 46:return Qn(A,3872,3881,!0,r);default:return Qn(A,48,57,!0,r)}},hn="data-html2canvas-ignore",dn=(fn.prototype.toIFrame=function(A,r){var e=this,B=pn(A,r);if(!B.contentWindow)return Promise.reject("Unable to find iframe window");var t=A.defaultView.pageXOffset,n=A.defaultView.pageYOffset,s=B.contentWindow,o=s.document,A=In(B).then(function(){return a(e,void 0,void 0,function(){var e,t;return H(this,function(A){switch(A.label){case 0:return this.scrolledElements.forEach(bn),s&&(s.scrollTo(r.left,r.top),!/(iPad|iPhone|iPod)/g.test(navigator.userAgent)||s.scrollY===r.top&&s.scrollX===r.left||(this.context.logger.warn("Unable to restore scroll position for cloned document"),this.context.windowBounds=this.context.windowBounds.add(s.scrollX-r.left,s.scrollY-r.top,0,0))),e=this.options.onclone,void 0===(t=this.clonedReferenceElement)?[2,Promise.reject("Error finding the "+this.referenceElement.nodeName+" in the cloned document")]:o.fonts&&o.fonts.ready?[4,o.fonts.ready]:[3,2];case 1:A.sent(),A.label=2;case 2:return/(AppleWebKit)/g.test(navigator.userAgent)?[4,En(o)]:[3,4];case 3:A.sent(),A.label=4;case 4:return"function"==typeof e?[2,Promise.resolve().then(function(){return e(o,t)}).then(function(){return B})]:[2,B]}})})});return o.open(),o.write(mn(document.doctype)+"<html></html>"),Ln(this.referenceElement.ownerDocument,t,n),o.replaceChild(o.adoptNode(this.documentElement),o.documentElement),o.close(),A},fn.prototype.createElementClone=function(A){if(Cr(A,2),zB(A))return this.createCanvasClone(A);if(MB(A))return this.createVideoClone(A);if(SB(A))return this.createStyleClone(A);var e=A.cloneNode(!1);return $B(e)&&($B(A)&&A.currentSrc&&A.currentSrc!==A.src&&(e.src=A.currentSrc,e.srcset=""),"lazy"===e.loading&&(e.loading="eager")),TB(e)?this.createCustomElementClone(e):e},fn.prototype.createCustomElementClone=function(A){var e=document.createElement("html2canvascustomelement");return Kn(A.style,e),e},fn.prototype.createStyleClone=function(A){try{var e=A.sheet;if(e&&e.cssRules){var t=[].slice.call(e.cssRules,0).reduce(function(A,e){return e&&"string"==typeof e.cssText?A+e.cssText:A},""),r=A.cloneNode(!1);return r.textContent=t,r}}catch(A){if(this.context.logger.error("Unable to access cssRules property",A),"SecurityError"!==A.name)throw A}return A.cloneNode(!1)},fn.prototype.createCanvasClone=function(e){var A;if(this.options.inlineImages&&e.ownerDocument){var t=e.ownerDocument.createElement("img");try{return t.src=e.toDataURL(),t}catch(A){this.context.logger.info("Unable to inline canvas contents, canvas is tainted",e)}}t=e.cloneNode(!1);try{t.width=e.width,t.height=e.height;var r,B,n=e.getContext("2d"),s=t.getContext("2d");return s&&(!this.options.allowTaint&&n?s.putImageData(n.getImageData(0,0,e.width,e.height),0,0):(!(r=null!==(A=e.getContext("webgl2"))&&void 0!==A?A:e.getContext("webgl"))||!1===(null==(B=r.getContextAttributes())?void 0:B.preserveDrawingBuffer)&&this.context.logger.warn("Unable to clone WebGL context as it has preserveDrawingBuffer=false",e),s.drawImage(e,0,0))),t}catch(A){this.context.logger.info("Unable to clone canvas as it is tainted",e)}return t},fn.prototype.createVideoClone=function(e){var A=e.ownerDocument.createElement("canvas");A.width=e.offsetWidth,A.height=e.offsetHeight;var t=A.getContext("2d");try{return t&&(t.drawImage(e,0,0,A.width,A.height),this.options.allowTaint||t.getImageData(0,0,A.width,A.height)),A}catch(A){this.context.logger.info("Unable to clone video as it is tainted",e)}A=e.ownerDocument.createElement("canvas");return A.width=e.offsetWidth,A.height=e.offsetHeight,A},fn.prototype.appendChildNode=function(A,e,t){XB(e)&&("SCRIPT"===e.tagName||e.hasAttribute(hn)||"function"==typeof this.options.ignoreElements&&this.options.ignoreElements(e))||this.options.copyStyles&&XB(e)&&SB(e)||A.appendChild(this.cloneNode(e,t))},fn.prototype.cloneChildNodes=function(A,e,t){for(var r,B=this,n=(A.shadowRoot||A).firstChild;n;n=n.nextSibling)XB(n)&&rn(n)&&"function"==typeof n.assignedNodes?(r=n.assignedNodes()).length&&r.forEach(function(A){return B.appendChildNode(e,A,t)}):this.appendChildNode(e,n,t)},fn.prototype.cloneNode=function(A,e){if(PB(A))return document.createTextNode(A.data);if(!A.ownerDocument)return A.cloneNode(!1);var t=A.ownerDocument.defaultView;if(t&&XB(A)&&(JB(A)||YB(A))){var r=this.createElementClone(A);r.style.transitionProperty="none";var B=t.getComputedStyle(A),n=t.getComputedStyle(A,":before"),s=t.getComputedStyle(A,":after");this.referenceElement===A&&JB(r)&&(this.clonedReferenceElement=r),jB(r)&&Mn(r);t=this.counters.parse(new Ur(this.context,B)),n=this.resolvePseudoContent(A,r,n,gn.BEFORE);TB(A)&&(e=!0),MB(A)||this.cloneChildNodes(A,r,e),n&&r.insertBefore(n,r.firstChild);s=this.resolvePseudoContent(A,r,s,gn.AFTER);return s&&r.appendChild(s),this.counters.pop(t),(B&&(this.options.copyStyles||YB(A))&&!An(A)||e)&&Kn(B,r),0===A.scrollTop&&0===A.scrollLeft||this.scrolledElements.push([r,A.scrollLeft,A.scrollTop]),(en(A)||tn(A))&&(en(r)||tn(r))&&(r.value=A.value),r}return A.cloneNode(!1)},fn.prototype.resolvePseudoContent=function(o,A,e,t){var i=this;if(e){var r=e.content,Q=A.ownerDocument;if(Q&&r&&"none"!==r&&"-moz-alt-content"!==r&&"none"!==e.display){this.counters.parse(new Ur(this.context,e));var c=new wr(this.context,e),a=Q.createElement("html2canvaspseudoelement");Kn(e,a),c.content.forEach(function(A){if(0===A.type)a.appendChild(Q.createTextNode(A.value));else if(22===A.type){var e=Q.createElement("img");e.src=A.value,e.style.opacity="1",a.appendChild(e)}else if(18===A.type){var t,r,B,n,s;"attr"===A.name?(e=A.values.filter(_A)).length&&a.appendChild(Q.createTextNode(o.getAttribute(e[0].value)||"")):"counter"===A.name?(B=(r=A.values.filter($A))[0],r=r[1],B&&_A(B)&&(t=i.counters.getCounterValue(B.value),s=r&&_A(r)?pt.parse(i.context,r.value):3,a.appendChild(Q.createTextNode(Fn(t,s,!1))))):"counters"===A.name&&(B=(t=A.values.filter($A))[0],s=t[1],r=t[2],B&&_A(B)&&(B=i.counters.getCounterValues(B.value),n=r&&_A(r)?pt.parse(i.context,r.value):3,s=s&&0===s.type?s.value:"",s=B.map(function(A){return Fn(A,n,!1)}).join(s),a.appendChild(Q.createTextNode(s))))}else if(20===A.type)switch(A.value){case"open-quote":a.appendChild(Q.createTextNode(Xt(c.quotes,i.quoteDepth++,!0)));break;case"close-quote":a.appendChild(Q.createTextNode(Xt(c.quotes,--i.quoteDepth,!1)));break;default:a.appendChild(Q.createTextNode(A.value))}}),a.className=Dn+" "+vn;t=t===gn.BEFORE?" "+Dn:" "+vn;return YB(A)?A.className.baseValue+=t:A.className+=t,a}}},fn.destroy=function(A){return!!A.parentNode&&(A.parentNode.removeChild(A),!0)},fn);function fn(A,e,t){if(this.context=A,this.options=t,this.scrolledElements=[],this.referenceElement=e,this.counters=new Bn,this.quoteDepth=0,!e.ownerDocument)throw new Error("Cloned element does not have an owner document");this.documentElement=this.cloneNode(e.ownerDocument.documentElement,!1)}(he=gn=gn||{})[he.BEFORE=0]="BEFORE",he[he.AFTER=1]="AFTER";function Hn(e){return new Promise(function(A){!e.complete&&e.src?(e.onload=A,e.onerror=A):A()})}var pn=function(A,e){var t=A.createElement("iframe");return t.className="html2canvas-container",t.style.visibility="hidden",t.style.position="fixed",t.style.left="-10000px",t.style.top="0px",t.style.border="0",t.width=e.width.toString(),t.height=e.height.toString(),t.scrolling="no",t.setAttribute(hn,"true"),A.body.appendChild(t),t},En=function(A){return Promise.all([].slice.call(A.images,0).map(Hn))},In=function(B){return new Promise(function(e,A){var t=B.contentWindow;if(!t)return A("No window assigned for iframe");var r=t.document;t.onload=B.onload=function(){t.onload=B.onload=null;var A=setInterval(function(){0<r.body.childNodes.length&&"complete"===r.readyState&&(clearInterval(A),e(B))},50)}})},yn=["all","d","content"],Kn=function(A,e){for(var t=A.length-1;0<=t;t--){var r=A.item(t);-1===yn.indexOf(r)&&e.style.setProperty(r,A.getPropertyValue(r))}return e},mn=function(A){var e="";return A&&(e+="<!DOCTYPE ",A.name&&(e+=A.name),A.internalSubset&&(e+=A.internalSubset),A.publicId&&(e+='"'+A.publicId+'"'),A.systemId&&(e+='"'+A.systemId+'"'),e+=">"),e},Ln=function(A,e,t){A&&A.defaultView&&(e!==A.defaultView.pageXOffset||t!==A.defaultView.pageYOffset)&&A.defaultView.scrollTo(e,t)},bn=function(A){var e=A[0],t=A[1],A=A[2];e.scrollLeft=t,e.scrollTop=A},Dn="___html2canvas___pseudoelement_before",vn="___html2canvas___pseudoelement_after",xn='{\n    content: "" !important;\n    display: none !important;\n}',Mn=function(A){Sn(A,"."+Dn+":before"+xn+"\n         ."+vn+":after"+xn)},Sn=function(A,e){var t=A.ownerDocument;t&&((t=t.createElement("style")).textContent=e,A.appendChild(t))},Tn=(Gn.getOrigin=function(A){var e=Gn._link;return e?(e.href=A,e.href=e.href,e.protocol+e.hostname+e.port):"about:blank"},Gn.isSameOrigin=function(A){return Gn.getOrigin(A)===Gn._origin},Gn.setContext=function(A){Gn._link=A.document.createElement("a"),Gn._origin=Gn.getOrigin(A.location.href)},Gn._origin="about:blank",Gn);function Gn(){}var On=(Vn.prototype.addImage=function(A){var e=Promise.resolve();return this.has(A)||(Yn(A)||Pn(A))&&(this._cache[A]=this.loadImage(A)).catch(function(){}),e},Vn.prototype.match=function(A){return this._cache[A]},Vn.prototype.loadImage=function(s){return a(this,void 0,void 0,function(){var e,r,t,B,n=this;return H(this,function(A){switch(A.label){case 0:return(e=Tn.isSameOrigin(s),r=!Xn(s)&&!0===this._options.useCORS&&Xr.SUPPORT_CORS_IMAGES&&!e,t=!Xn(s)&&!e&&!Yn(s)&&"string"==typeof this._options.proxy&&Xr.SUPPORT_CORS_XHR&&!r,e||!1!==this._options.allowTaint||Xn(s)||Yn(s)||t||r)?(B=s,t?[4,this.proxy(B)]:[3,2]):[2];case 1:B=A.sent(),A.label=2;case 2:return this.context.logger.debug("Added image "+s.substring(0,256)),[4,new Promise(function(A,e){var t=new Image;t.onload=function(){return A(t)},t.onerror=e,(Jn(B)||r)&&(t.crossOrigin="anonymous"),t.src=B,!0===t.complete&&setTimeout(function(){return A(t)},500),0<n._options.imageTimeout&&setTimeout(function(){return e("Timed out ("+n._options.imageTimeout+"ms) loading image")},n._options.imageTimeout)})];case 3:return[2,A.sent()]}})})},Vn.prototype.has=function(A){return void 0!==this._cache[A]},Vn.prototype.keys=function(){return Promise.resolve(Object.keys(this._cache))},Vn.prototype.proxy=function(s){var o=this,i=this._options.proxy;if(!i)throw new Error("No proxy defined");var Q=s.substring(0,256);return new Promise(function(e,t){var r=Xr.SUPPORT_RESPONSE_TYPE?"blob":"text",B=new XMLHttpRequest;B.onload=function(){var A;200===B.status?"text"==r?e(B.response):((A=new FileReader).addEventListener("load",function(){return e(A.result)},!1),A.addEventListener("error",function(A){return t(A)},!1),A.readAsDataURL(B.response)):t("Failed to proxy resource "+Q+" with status code "+B.status)},B.onerror=t;var A,n=-1<i.indexOf("?")?"&":"?";B.open("GET",i+n+"url="+encodeURIComponent(s)+"&responseType="+r),"text"!=r&&B instanceof XMLHttpRequest&&(B.responseType=r),o._options.imageTimeout&&(A=o._options.imageTimeout,B.timeout=A,B.ontimeout=function(){return t("Timed out ("+A+"ms) proxying "+Q)}),B.send()})},Vn);function Vn(A,e){this.context=A,this._options=e,this._cache={}}var kn=/^data:image\/svg\+xml/i,Rn=/^data:image\/.*;base64,/i,Nn=/^data:image\/.*/i,Pn=function(A){return Xr.SUPPORT_SVG_DRAWING||!Wn(A)},Xn=function(A){return Nn.test(A)},Jn=function(A){return Rn.test(A)},Yn=function(A){return"blob"===A.substr(0,4)},Wn=function(A){return"svg"===A.substr(-3).toLowerCase()||kn.test(A)},Zn=(_n.prototype.add=function(A,e){return new _n(this.x+A,this.y+e)},_n);function _n(A,e){this.type=0,this.x=A,this.y=e}function qn(A,e,t){return new Zn(A.x+(e.x-A.x)*t,A.y+(e.y-A.y)*t)}var jn=(zn.prototype.subdivide=function(A,e){var t=qn(this.start,this.startControl,A),r=qn(this.startControl,this.endControl,A),B=qn(this.endControl,this.end,A),n=qn(t,r,A),r=qn(r,B,A),A=qn(n,r,A);return e?new zn(this.start,t,n,A):new zn(A,r,B,this.end)},zn.prototype.add=function(A,e){return new zn(this.start.add(A,e),this.startControl.add(A,e),this.endControl.add(A,e),this.end.add(A,e))},zn.prototype.reverse=function(){return new zn(this.end,this.endControl,this.startControl,this.start)},zn);function zn(A,e,t,r){this.type=1,this.start=A,this.startControl=e,this.endControl=t,this.end=r}function $n(A){return 1===A.type}var As,es=function(A){var e=A.styles,t=A.bounds,r=(C=Be(e.borderTopLeftRadius,t.width,t.height))[0],B=C[1],n=(u=Be(e.borderTopRightRadius,t.width,t.height))[0],s=u[1],o=(F=Be(e.borderBottomRightRadius,t.width,t.height))[0],i=F[1],Q=(h=Be(e.borderBottomLeftRadius,t.width,t.height))[0],c=h[1];(d=[]).push((r+n)/t.width),d.push((Q+o)/t.width),d.push((B+c)/t.height),d.push((s+i)/t.height),1<(f=Math.max.apply(Math,d))&&(r/=f,B/=f,n/=f,s/=f,o/=f,i/=f,Q/=f,c/=f);var a=t.width-n,g=t.height-i,w=t.width-o,U=t.height-c,l=e.borderTopWidth,C=e.borderRightWidth,u=e.borderBottomWidth,F=e.borderLeftWidth,h=Ue(e.paddingTop,A.bounds.width),d=Ue(e.paddingRight,A.bounds.width),f=Ue(e.paddingBottom,A.bounds.width),A=Ue(e.paddingLeft,A.bounds.width);this.topLeftBorderDoubleOuterBox=0<r||0<B?ss(t.left+F/3,t.top+l/3,r-F/3,B-l/3,As.TOP_LEFT):new Zn(t.left+F/3,t.top+l/3),this.topRightBorderDoubleOuterBox=0<r||0<B?ss(t.left+a,t.top+l/3,n-C/3,s-l/3,As.TOP_RIGHT):new Zn(t.left+t.width-C/3,t.top+l/3),this.bottomRightBorderDoubleOuterBox=0<o||0<i?ss(t.left+w,t.top+g,o-C/3,i-u/3,As.BOTTOM_RIGHT):new Zn(t.left+t.width-C/3,t.top+t.height-u/3),this.bottomLeftBorderDoubleOuterBox=0<Q||0<c?ss(t.left+F/3,t.top+U,Q-F/3,c-u/3,As.BOTTOM_LEFT):new Zn(t.left+F/3,t.top+t.height-u/3),this.topLeftBorderDoubleInnerBox=0<r||0<B?ss(t.left+2*F/3,t.top+2*l/3,r-2*F/3,B-2*l/3,As.TOP_LEFT):new Zn(t.left+2*F/3,t.top+2*l/3),this.topRightBorderDoubleInnerBox=0<r||0<B?ss(t.left+a,t.top+2*l/3,n-2*C/3,s-2*l/3,As.TOP_RIGHT):new Zn(t.left+t.width-2*C/3,t.top+2*l/3),this.bottomRightBorderDoubleInnerBox=0<o||0<i?ss(t.left+w,t.top+g,o-2*C/3,i-2*u/3,As.BOTTOM_RIGHT):new Zn(t.left+t.width-2*C/3,t.top+t.height-2*u/3),this.bottomLeftBorderDoubleInnerBox=0<Q||0<c?ss(t.left+2*F/3,t.top+U,Q-2*F/3,c-2*u/3,As.BOTTOM_LEFT):new Zn(t.left+2*F/3,t.top+t.height-2*u/3),this.topLeftBorderStroke=0<r||0<B?ss(t.left+F/2,t.top+l/2,r-F/2,B-l/2,As.TOP_LEFT):new Zn(t.left+F/2,t.top+l/2),this.topRightBorderStroke=0<r||0<B?ss(t.left+a,t.top+l/2,n-C/2,s-l/2,As.TOP_RIGHT):new Zn(t.left+t.width-C/2,t.top+l/2),this.bottomRightBorderStroke=0<o||0<i?ss(t.left+w,t.top+g,o-C/2,i-u/2,As.BOTTOM_RIGHT):new Zn(t.left+t.width-C/2,t.top+t.height-u/2),this.bottomLeftBorderStroke=0<Q||0<c?ss(t.left+F/2,t.top+U,Q-F/2,c-u/2,As.BOTTOM_LEFT):new Zn(t.left+F/2,t.top+t.height-u/2),this.topLeftBorderBox=0<r||0<B?ss(t.left,t.top,r,B,As.TOP_LEFT):new Zn(t.left,t.top),this.topRightBorderBox=0<n||0<s?ss(t.left+a,t.top,n,s,As.TOP_RIGHT):new Zn(t.left+t.width,t.top),this.bottomRightBorderBox=0<o||0<i?ss(t.left+w,t.top+g,o,i,As.BOTTOM_RIGHT):new Zn(t.left+t.width,t.top+t.height),this.bottomLeftBorderBox=0<Q||0<c?ss(t.left,t.top+U,Q,c,As.BOTTOM_LEFT):new Zn(t.left,t.top+t.height),this.topLeftPaddingBox=0<r||0<B?ss(t.left+F,t.top+l,Math.max(0,r-F),Math.max(0,B-l),As.TOP_LEFT):new Zn(t.left+F,t.top+l),this.topRightPaddingBox=0<n||0<s?ss(t.left+Math.min(a,t.width-C),t.top+l,a>t.width+C?0:Math.max(0,n-C),Math.max(0,s-l),As.TOP_RIGHT):new Zn(t.left+t.width-C,t.top+l),this.bottomRightPaddingBox=0<o||0<i?ss(t.left+Math.min(w,t.width-F),t.top+Math.min(g,t.height-u),Math.max(0,o-C),Math.max(0,i-u),As.BOTTOM_RIGHT):new Zn(t.left+t.width-C,t.top+t.height-u),this.bottomLeftPaddingBox=0<Q||0<c?ss(t.left+F,t.top+Math.min(U,t.height-u),Math.max(0,Q-F),Math.max(0,c-u),As.BOTTOM_LEFT):new Zn(t.left+F,t.top+t.height-u),this.topLeftContentBox=0<r||0<B?ss(t.left+F+A,t.top+l+h,Math.max(0,r-(F+A)),Math.max(0,B-(l+h)),As.TOP_LEFT):new Zn(t.left+F+A,t.top+l+h),this.topRightContentBox=0<n||0<s?ss(t.left+Math.min(a,t.width+F+A),t.top+l+h,a>t.width+F+A?0:n-F+A,s-(l+h),As.TOP_RIGHT):new Zn(t.left+t.width-(C+d),t.top+l+h),this.bottomRightContentBox=0<o||0<i?ss(t.left+Math.min(w,t.width-(F+A)),t.top+Math.min(g,t.height+l+h),Math.max(0,o-(C+d)),i-(u+f),As.BOTTOM_RIGHT):new Zn(t.left+t.width-(C+d),t.top+t.height-(u+f)),this.bottomLeftContentBox=0<Q||0<c?ss(t.left+F+A,t.top+U,Math.max(0,Q-(F+A)),c-(u+f),As.BOTTOM_LEFT):new Zn(t.left+F+A,t.top+t.height-(u+f))};(he=As=As||{})[he.TOP_LEFT=0]="TOP_LEFT",he[he.TOP_RIGHT=1]="TOP_RIGHT",he[he.BOTTOM_RIGHT=2]="BOTTOM_RIGHT",he[he.BOTTOM_LEFT=3]="BOTTOM_LEFT";function ts(A){return[A.topLeftBorderBox,A.topRightBorderBox,A.bottomRightBorderBox,A.bottomLeftBorderBox]}function rs(A){return[A.topLeftPaddingBox,A.topRightPaddingBox,A.bottomRightPaddingBox,A.bottomLeftPaddingBox]}function Bs(A){return 1===A.type}function ns(A,t){return A.length===t.length&&A.some(function(A,e){return A===t[e]})}var ss=function(A,e,t,r,B){var n=(Math.sqrt(2)-1)/3*4,s=t*n,o=r*n,i=A+t,Q=e+r;switch(B){case As.TOP_LEFT:return new jn(new Zn(A,Q),new Zn(A,Q-o),new Zn(i-s,e),new Zn(i,e));case As.TOP_RIGHT:return new jn(new Zn(A,e),new Zn(A+s,e),new Zn(i,Q-o),new Zn(i,Q));case As.BOTTOM_RIGHT:return new jn(new Zn(i,e),new Zn(i,e+o),new Zn(A+s,Q),new Zn(A,Q));default:As.BOTTOM_LEFT;return new jn(new Zn(i,Q),new Zn(i-s,Q),new Zn(A,e+o),new Zn(A,e))}},os=function(A,e,t){this.offsetX=A,this.offsetY=e,this.matrix=t,this.type=0,this.target=6},is=function(A,e){this.path=A,this.target=e,this.type=1},Qs=function(A){this.opacity=A,this.type=2,this.target=6},cs=function(A){this.element=A,this.inlineLevel=[],this.nonInlineLevel=[],this.negativeZIndex=[],this.zeroOrAutoZIndexOrTransformedOrOpacity=[],this.positiveZIndex=[],this.nonPositionedFloats=[],this.nonPositionedInlineLevel=[]},as=(gs.prototype.getEffects=function(e){for(var A=-1===[2,3].indexOf(this.container.styles.position),t=this.parent,r=this.effects.slice(0);t;){var B,n,s=t.effects.filter(function(A){return!Bs(A)});A||0!==t.container.styles.position||!t.parent?(r.unshift.apply(r,s),A=-1===[2,3].indexOf(t.container.styles.position),0!==t.container.styles.overflowX&&(B=ts(t.curves),n=rs(t.curves),ns(B,n)||r.unshift(new is(n,6)))):r.unshift.apply(r,s),t=t.parent}return r.filter(function(A){return Pt(A.target,e)})},gs);function gs(A,e){var t,r;this.container=A,this.parent=e,this.effects=[],this.curves=new es(this.container),this.container.styles.opacity<1&&this.effects.push(new Qs(this.container.styles.opacity)),null!==this.container.styles.transform&&(e=this.container.bounds.left+this.container.styles.transformOrigin[0].number,t=this.container.bounds.top+this.container.styles.transformOrigin[1].number,r=this.container.styles.transform,this.effects.push(new os(e,t,r))),0!==this.container.styles.overflowX&&(t=ts(this.curves),r=rs(this.curves),ns(t,r)?this.effects.push(new is(t,6)):(this.effects.push(new is(t,2)),this.effects.push(new is(r,4))))}function ws(A,e){switch(e){case 0:return Hs(A.topLeftBorderBox,A.topLeftPaddingBox,A.topRightBorderBox,A.topRightPaddingBox);case 1:return Hs(A.topRightBorderBox,A.topRightPaddingBox,A.bottomRightBorderBox,A.bottomRightPaddingBox);case 2:return Hs(A.bottomRightBorderBox,A.bottomRightPaddingBox,A.bottomLeftBorderBox,A.bottomLeftPaddingBox);default:return Hs(A.bottomLeftBorderBox,A.bottomLeftPaddingBox,A.topLeftBorderBox,A.topLeftPaddingBox)}}function Us(A){var e=A.bounds,A=A.styles;return e.add(A.borderLeftWidth,A.borderTopWidth,-(A.borderRightWidth+A.borderLeftWidth),-(A.borderTopWidth+A.borderBottomWidth))}function ls(A){var e=A.styles,t=A.bounds,r=Ue(e.paddingLeft,t.width),B=Ue(e.paddingRight,t.width),n=Ue(e.paddingTop,t.width),A=Ue(e.paddingBottom,t.width);return t.add(r+e.borderLeftWidth,n+e.borderTopWidth,-(e.borderRightWidth+e.borderLeftWidth+r+B),-(e.borderTopWidth+e.borderBottomWidth+n+A))}function Cs(A,e,t){var r=(B=Es(A.styles.backgroundOrigin,e),n=A,0===B?n.bounds:(2===B?ls:Us)(n)),B=(s=Es(A.styles.backgroundClip,e),o=A,0===s?o.bounds:(2===s?ls:Us)(o)),n=ps(Es(A.styles.backgroundSize,e),t,r),s=n[0],o=n[1],t=Be(Es(A.styles.backgroundPosition,e),r.width-s,r.height-o);return[Is(Es(A.styles.backgroundRepeat,e),t,n,r,B),Math.round(r.left+t[0]),Math.round(r.top+t[1]),s,o]}function us(A){return _A(A)&&A.value===Ve.AUTO}function Fs(A){return"number"==typeof A}var hs=function(Q,c,a,g){Q.container.elements.forEach(function(A){var e=Pt(A.flags,4),t=Pt(A.flags,2),r=new as(A,Q);Pt(A.styles.display,2048)&&g.push(r);var B,n,s,o,i=Pt(A.flags,8)?[]:g;e||t?(B=e||A.styles.isPositioned()?a:c,t=new cs(r),A.styles.isPositioned()||A.styles.opacity<1||A.styles.isTransformed()?(n=A.styles.zIndex.order)<0?(s=0,B.negativeZIndex.some(function(A,e){return n>A.element.container.styles.zIndex.order?(s=e,!1):0<s}),B.negativeZIndex.splice(s,0,t)):0<n?(o=0,B.positiveZIndex.some(function(A,e){return n>=A.element.container.styles.zIndex.order?(o=e+1,!1):0<o}),B.positiveZIndex.splice(o,0,t)):B.zeroOrAutoZIndexOrTransformedOrOpacity.push(t):(A.styles.isFloating()?B.nonPositionedFloats:B.nonPositionedInlineLevel).push(t),hs(r,t,e?t:a,i)):((A.styles.isInlineLevel()?c.inlineLevel:c.nonInlineLevel).push(r),hs(r,c,a,i)),Pt(A.flags,8)&&ds(A,i)})},ds=function(A,e){for(var t=A instanceof UB?A.start:1,r=A instanceof UB&&A.reversed,B=0;B<e.length;B++){var n=e[B];n.container instanceof aB&&"number"==typeof n.container.value&&0!==n.container.value&&(t=n.container.value),n.listValue=Fn(t,n.container.styles.listStyleType,!0),t+=r?-1:1}},fs=function(A,e){var t=[];return $n(A)?t.push(A.subdivide(.5,!1)):t.push(A),$n(e)?t.push(e.subdivide(.5,!0)):t.push(e),t},Hs=function(A,e,t,r){var B=[];return $n(A)?B.push(A.subdivide(.5,!1)):B.push(A),$n(t)?B.push(t.subdivide(.5,!0)):B.push(t),$n(r)?B.push(r.subdivide(.5,!0).reverse()):B.push(r),$n(e)?B.push(e.subdivide(.5,!1).reverse()):B.push(e),B},ps=function(A,e,t){var r=e[0],B=e[1],n=e[2],s=A[0],o=A[1];if(!s)return[0,0];if(te(s)&&o&&te(o))return[Ue(s,t.width),Ue(o,t.height)];var i=Fs(n);if(_A(s)&&(s.value===Ve.CONTAIN||s.value===Ve.COVER))return Fs(n)?t.width/t.height<n!=(s.value===Ve.COVER)?[t.width,t.width/n]:[t.height*n,t.height]:[t.width,t.height];var Q=Fs(r),e=Fs(B),A=Q||e;if(us(s)&&(!o||us(o)))return Q&&e?[r,B]:i||A?A&&i?[Q?r:B*n,e?B:r/n]:[Q?r:t.width,e?B:t.height]:[t.width,t.height];if(i){var c=0,a=0;return te(s)?c=Ue(s,t.width):te(o)&&(a=Ue(o,t.height)),us(s)?c=a*n:o&&!us(o)||(a=c/n),[c,a]}c=null,a=null;if(te(s)?c=Ue(s,t.width):o&&te(o)&&(a=Ue(o,t.height)),null!==(c=null!==(a=null!==c&&(!o||us(o))?Q&&e?c/r*B:t.height:a)&&us(s)?Q&&e?a/B*r:t.width:c)&&null!==a)return[c,a];throw new Error("Unable to calculate background-size for element")},Es=function(A,e){e=A[e];return void 0===e?A[0]:e},Is=function(A,e,t,r,B){var n=e[0],s=e[1],o=t[0],i=t[1];switch(A){case 2:return[new Zn(Math.round(r.left),Math.round(r.top+s)),new Zn(Math.round(r.left+r.width),Math.round(r.top+s)),new Zn(Math.round(r.left+r.width),Math.round(i+r.top+s)),new Zn(Math.round(r.left),Math.round(i+r.top+s))];case 3:return[new Zn(Math.round(r.left+n),Math.round(r.top)),new Zn(Math.round(r.left+n+o),Math.round(r.top)),new Zn(Math.round(r.left+n+o),Math.round(r.height+r.top)),new Zn(Math.round(r.left+n),Math.round(r.height+r.top))];case 1:return[new Zn(Math.round(r.left+n),Math.round(r.top+s)),new Zn(Math.round(r.left+n+o),Math.round(r.top+s)),new Zn(Math.round(r.left+n+o),Math.round(r.top+s+i)),new Zn(Math.round(r.left+n),Math.round(r.top+s+i))];default:return[new Zn(Math.round(B.left),Math.round(B.top)),new Zn(Math.round(B.left+B.width),Math.round(B.top)),new Zn(Math.round(B.left+B.width),Math.round(B.height+B.top)),new Zn(Math.round(B.left),Math.round(B.height+B.top))]}},ys="Hidden Text",Ks=(ms.prototype.parseMetrics=function(A,e){var t=this._document.createElement("div"),r=this._document.createElement("img"),B=this._document.createElement("span"),n=this._document.body;t.style.visibility="hidden",t.style.fontFamily=A,t.style.fontSize=e,t.style.margin="0",t.style.padding="0",t.style.whiteSpace="nowrap",n.appendChild(t),r.src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7",r.width=1,r.height=1,r.style.margin="0",r.style.padding="0",r.style.verticalAlign="baseline",B.style.fontFamily=A,B.style.fontSize=e,B.style.margin="0",B.style.padding="0",B.appendChild(this._document.createTextNode(ys)),t.appendChild(B),t.appendChild(r);e=r.offsetTop-B.offsetTop+2;t.removeChild(B),t.appendChild(this._document.createTextNode(ys)),t.style.lineHeight="normal",r.style.verticalAlign="super";r=r.offsetTop-t.offsetTop+2;return n.removeChild(t),{baseline:e,middle:r}},ms.prototype.getMetrics=function(A,e){var t=A+" "+e;return void 0===this._data[t]&&(this._data[t]=this.parseMetrics(A,e)),this._data[t]},ms);function ms(A){this._data={},this._document=A}var Ls,he=function(A,e){this.context=A,this.options=e},bs=(A(Ds,Ls=he),Ds.prototype.applyEffects=function(A){for(var e=this;this._activeEffects.length;)this.popEffect();A.forEach(function(A){return e.applyEffect(A)})},Ds.prototype.applyEffect=function(A){this.ctx.save(),2===A.type&&(this.ctx.globalAlpha=A.opacity),0===A.type&&(this.ctx.translate(A.offsetX,A.offsetY),this.ctx.transform(A.matrix[0],A.matrix[1],A.matrix[2],A.matrix[3],A.matrix[4],A.matrix[5]),this.ctx.translate(-A.offsetX,-A.offsetY)),Bs(A)&&(this.path(A.path),this.ctx.clip()),this._activeEffects.push(A)},Ds.prototype.popEffect=function(){this._activeEffects.pop(),this.ctx.restore()},Ds.prototype.renderStack=function(e){return a(this,void 0,void 0,function(){return H(this,function(A){switch(A.label){case 0:return e.element.container.styles.isVisible()?[4,this.renderStackContent(e)]:[3,2];case 1:A.sent(),A.label=2;case 2:return[2]}})})},Ds.prototype.renderNode=function(e){return a(this,void 0,void 0,function(){return H(this,function(A){switch(A.label){case 0:return Pt(e.container.flags,16),e.container.styles.isVisible()?[4,this.renderNodeBackgroundAndBorders(e)]:[3,3];case 1:return A.sent(),[4,this.renderNodeContent(e)];case 2:A.sent(),A.label=3;case 3:return[2]}})})},Ds.prototype.renderTextWithLetterSpacing=function(t,A,r){var B=this;0===A?this.ctx.fillText(t.text,t.bounds.left,t.bounds.top+r):Zr(t.text).reduce(function(A,e){return B.ctx.fillText(e,A,t.bounds.top+r),A+B.ctx.measureText(e).width},t.bounds.left)},Ds.prototype.createFontStyle=function(A){var e=A.fontVariant.filter(function(A){return"normal"===A||"small-caps"===A}).join(""),t=Gs(A.fontFamily).join(", "),r=WA(A.fontSize)?""+A.fontSize.number+A.fontSize.unit:A.fontSize.number+"px";return[[A.fontStyle,e,A.fontWeight,r,t].join(" "),t,r]},Ds.prototype.renderTextNode=function(i,Q){return a(this,void 0,void 0,function(){var e,t,r,B,n,s,o=this;return H(this,function(A){return r=this.createFontStyle(Q),e=r[0],t=r[1],r=r[2],this.ctx.font=e,this.ctx.direction=1===Q.direction?"rtl":"ltr",this.ctx.textAlign="left",this.ctx.textBaseline="alphabetic",r=this.fontMetrics.getMetrics(t,r),B=r.baseline,n=r.middle,s=Q.paintOrder,i.textBounds.forEach(function(t){s.forEach(function(A){switch(A){case 0:o.ctx.fillStyle=ie(Q.color),o.renderTextWithLetterSpacing(t,Q.letterSpacing,B);var e=Q.textShadow;e.length&&t.text.trim().length&&(e.slice(0).reverse().forEach(function(A){o.ctx.shadowColor=ie(A.color),o.ctx.shadowOffsetX=A.offsetX.number*o.options.scale,o.ctx.shadowOffsetY=A.offsetY.number*o.options.scale,o.ctx.shadowBlur=A.blur.number,o.renderTextWithLetterSpacing(t,Q.letterSpacing,B)}),o.ctx.shadowColor="",o.ctx.shadowOffsetX=0,o.ctx.shadowOffsetY=0,o.ctx.shadowBlur=0),Q.textDecorationLine.length&&(o.ctx.fillStyle=ie(Q.textDecorationColor||Q.color),Q.textDecorationLine.forEach(function(A){switch(A){case 1:o.ctx.fillRect(t.bounds.left,Math.round(t.bounds.top+B),t.bounds.width,1);break;case 2:o.ctx.fillRect(t.bounds.left,Math.round(t.bounds.top),t.bounds.width,1);break;case 3:o.ctx.fillRect(t.bounds.left,Math.ceil(t.bounds.top+n),t.bounds.width,1)}}));break;case 1:Q.webkitTextStrokeWidth&&t.text.trim().length&&(o.ctx.strokeStyle=ie(Q.webkitTextStrokeColor),o.ctx.lineWidth=Q.webkitTextStrokeWidth,o.ctx.lineJoin=window.chrome?"miter":"round",o.ctx.strokeText(t.text,t.bounds.left,t.bounds.top+B)),o.ctx.strokeStyle="",o.ctx.lineWidth=0,o.ctx.lineJoin="miter"}})}),[2]})})},Ds.prototype.renderReplacedElement=function(A,e,t){var r;t&&0<A.intrinsicWidth&&0<A.intrinsicHeight&&(r=ls(A),e=rs(e),this.path(e),this.ctx.save(),this.ctx.clip(),this.ctx.drawImage(t,0,0,A.intrinsicWidth,A.intrinsicHeight,r.left,r.top,r.width,r.height),this.ctx.restore())},Ds.prototype.renderNodeContent=function(w){return a(this,void 0,void 0,function(){var e,t,r,B,n,s,o,i,Q,c,a,g;return H(this,function(A){switch(A.label){case 0:this.applyEffects(w.getEffects(4)),e=w.container,t=w.curves,r=e.styles,B=0,n=e.textNodes,A.label=1;case 1:return B<n.length?(s=n[B],[4,this.renderTextNode(s,r)]):[3,4];case 2:A.sent(),A.label=3;case 3:return B++,[3,1];case 4:if(!(e instanceof tB))return[3,8];A.label=5;case 5:return A.trys.push([5,7,,8]),[4,this.context.cache.match(e.src)];case 6:return Q=A.sent(),this.renderReplacedElement(e,t,Q),[3,8];case 7:return A.sent(),this.context.logger.error("Error loading image "+e.src),[3,8];case 8:if(e instanceof nB&&this.renderReplacedElement(e,t,e.canvas),!(e instanceof iB))return[3,12];A.label=9;case 9:return A.trys.push([9,11,,12]),[4,this.context.cache.match(e.svg)];case 10:return Q=A.sent(),this.renderReplacedElement(e,t,Q),[3,12];case 11:return A.sent(),this.context.logger.error("Error loading svg "+e.svg.substring(0,255)),[3,12];case 12:return e instanceof vB&&e.tree?[4,new Ds(this.context,{scale:this.options.scale,backgroundColor:e.backgroundColor,x:0,y:0,width:e.width,height:e.height}).render(e.tree)]:[3,14];case 13:s=A.sent(),e.width&&e.height&&this.ctx.drawImage(s,0,0,e.width,e.height,e.bounds.left,e.bounds.top,e.bounds.width,e.bounds.height),A.label=14;case 14:if(e instanceof pB&&(i=Math.min(e.bounds.width,e.bounds.height),e.type===hB?e.checked&&(this.ctx.save(),this.path([new Zn(e.bounds.left+.39363*i,e.bounds.top+.79*i),new Zn(e.bounds.left+.16*i,e.bounds.top+.5549*i),new Zn(e.bounds.left+.27347*i,e.bounds.top+.44071*i),new Zn(e.bounds.left+.39694*i,e.bounds.top+.5649*i),new Zn(e.bounds.left+.72983*i,e.bounds.top+.23*i),new Zn(e.bounds.left+.84*i,e.bounds.top+.34085*i),new Zn(e.bounds.left+.39363*i,e.bounds.top+.79*i)]),this.ctx.fillStyle=ie(HB),this.ctx.fill(),this.ctx.restore()):e.type===dB&&e.checked&&(this.ctx.save(),this.ctx.beginPath(),this.ctx.arc(e.bounds.left+i/2,e.bounds.top+i/2,i/4,0,2*Math.PI,!0),this.ctx.fillStyle=ie(HB),this.ctx.fill(),this.ctx.restore())),xs(e)&&e.value.length){switch(c=this.createFontStyle(r),a=c[0],i=c[1],c=this.fontMetrics.getMetrics(a,i).baseline,this.ctx.font=a,this.ctx.fillStyle=ie(r.color),this.ctx.textBaseline="alphabetic",this.ctx.textAlign=Ss(e.styles.textAlign),g=ls(e),o=0,e.styles.textAlign){case 1:o+=g.width/2;break;case 2:o+=g.width}i=g.add(o,0,0,-g.height/2+1),this.ctx.save(),this.path([new Zn(g.left,g.top),new Zn(g.left+g.width,g.top),new Zn(g.left+g.width,g.top+g.height),new Zn(g.left,g.top+g.height)]),this.ctx.clip(),this.renderTextWithLetterSpacing(new Jr(e.value,i),r.letterSpacing,c),this.ctx.restore(),this.ctx.textBaseline="alphabetic",this.ctx.textAlign="left"}if(!Pt(e.styles.display,2048))return[3,20];if(null===e.styles.listStyleImage)return[3,19];if(0!==(c=e.styles.listStyleImage).type)return[3,18];Q=void 0,c=c.url,A.label=15;case 15:return A.trys.push([15,17,,18]),[4,this.context.cache.match(c)];case 16:return Q=A.sent(),this.ctx.drawImage(Q,e.bounds.left-(Q.width+10),e.bounds.top),[3,18];case 17:return A.sent(),this.context.logger.error("Error loading list-style-image "+c),[3,18];case 18:return[3,20];case 19:w.listValue&&-1!==e.styles.listStyleType&&(a=this.createFontStyle(r)[0],this.ctx.font=a,this.ctx.fillStyle=ie(r.color),this.ctx.textBaseline="middle",this.ctx.textAlign="right",g=new d(e.bounds.left,e.bounds.top+Ue(e.styles.paddingTop,e.bounds.width),e.bounds.width,Ye(r.lineHeight,r.fontSize.number)/2+1),this.renderTextWithLetterSpacing(new Jr(w.listValue,g),r.letterSpacing,Ye(r.lineHeight,r.fontSize.number)/2+2),this.ctx.textBaseline="bottom",this.ctx.textAlign="left"),A.label=20;case 20:return[2]}})})},Ds.prototype.renderStackContent=function(C){return a(this,void 0,void 0,function(){var e,t,r,B,n,s,o,i,Q,c,a,g,w,U,l;return H(this,function(A){switch(A.label){case 0:return Pt(C.element.container.flags,16),[4,this.renderNodeBackgroundAndBorders(C.element)];case 1:A.sent(),e=0,t=C.negativeZIndex,A.label=2;case 2:return e<t.length?(l=t[e],[4,this.renderStack(l)]):[3,5];case 3:A.sent(),A.label=4;case 4:return e++,[3,2];case 5:return[4,this.renderNodeContent(C.element)];case 6:A.sent(),r=0,B=C.nonInlineLevel,A.label=7;case 7:return r<B.length?(l=B[r],[4,this.renderNode(l)]):[3,10];case 8:A.sent(),A.label=9;case 9:return r++,[3,7];case 10:n=0,s=C.nonPositionedFloats,A.label=11;case 11:return n<s.length?(l=s[n],[4,this.renderStack(l)]):[3,14];case 12:A.sent(),A.label=13;case 13:return n++,[3,11];case 14:o=0,i=C.nonPositionedInlineLevel,A.label=15;case 15:return o<i.length?(l=i[o],[4,this.renderStack(l)]):[3,18];case 16:A.sent(),A.label=17;case 17:return o++,[3,15];case 18:Q=0,c=C.inlineLevel,A.label=19;case 19:return Q<c.length?(l=c[Q],[4,this.renderNode(l)]):[3,22];case 20:A.sent(),A.label=21;case 21:return Q++,[3,19];case 22:a=0,g=C.zeroOrAutoZIndexOrTransformedOrOpacity,A.label=23;case 23:return a<g.length?(l=g[a],[4,this.renderStack(l)]):[3,26];case 24:A.sent(),A.label=25;case 25:return a++,[3,23];case 26:w=0,U=C.positiveZIndex,A.label=27;case 27:return w<U.length?(l=U[w],[4,this.renderStack(l)]):[3,30];case 28:A.sent(),A.label=29;case 29:return w++,[3,27];case 30:return[2]}})})},Ds.prototype.mask=function(A){this.ctx.beginPath(),this.ctx.moveTo(0,0),this.ctx.lineTo(this.canvas.width,0),this.ctx.lineTo(this.canvas.width,this.canvas.height),this.ctx.lineTo(0,this.canvas.height),this.ctx.lineTo(0,0),this.formatPath(A.slice(0).reverse()),this.ctx.closePath()},Ds.prototype.path=function(A){this.ctx.beginPath(),this.formatPath(A),this.ctx.closePath()},Ds.prototype.formatPath=function(A){var r=this;A.forEach(function(A,e){var t=$n(A)?A.start:A;0===e?r.ctx.moveTo(t.x,t.y):r.ctx.lineTo(t.x,t.y),$n(A)&&r.ctx.bezierCurveTo(A.startControl.x,A.startControl.y,A.endControl.x,A.endControl.y,A.end.x,A.end.y)})},Ds.prototype.renderRepeat=function(A,e,t,r){this.path(A),this.ctx.fillStyle=e,this.ctx.translate(t,r),this.ctx.fill(),this.ctx.translate(-t,-r)},Ds.prototype.resizeImage=function(A,e,t){if(A.width===e&&A.height===t)return A;var r=(null!==(r=this.canvas.ownerDocument)&&void 0!==r?r:document).createElement("canvas");return r.width=Math.max(1,e),r.height=Math.max(1,t),r.getContext("2d").drawImage(A,0,0,A.width,A.height,0,0,e,t),r},Ds.prototype.renderBackgroundImage=function(f){return a(this,void 0,void 0,function(){var h,e,d,t,r,B;return H(this,function(A){switch(A.label){case 0:h=f.styles.backgroundImage.length-1,e=function(e){var t,r,B,n,s,o,i,Q,c,a,g,w,U,l,C,u,F;return H(this,function(A){switch(A.label){case 0:if(0!==e.type)return[3,5];t=void 0,r=e.url,A.label=1;case 1:return A.trys.push([1,3,,4]),[4,d.context.cache.match(r)];case 2:return t=A.sent(),[3,4];case 3:return A.sent(),d.context.logger.error("Error loading background-image "+r),[3,4];case 4:return t&&(B=Cs(f,h,[t.width,t.height,t.width/t.height]),o=B[0],g=B[1],w=B[2],c=B[3],a=B[4],s=d.ctx.createPattern(d.resizeImage(t,c,a),"repeat"),d.renderRepeat(o,s,g,w)),[3,6];case 5:1===e.type?(F=Cs(f,h,[null,null,null]),o=F[0],g=F[1],w=F[2],c=F[3],a=F[4],C=Ee(e.angle,c,a),l=C[0],B=C[1],i=C[2],u=C[3],Q=C[4],(F=document.createElement("canvas")).width=c,F.height=a,C=F.getContext("2d"),n=C.createLinearGradient(B,u,i,Q),pe(e.stops,l).forEach(function(A){return n.addColorStop(A.stop,ie(A.color))}),C.fillStyle=n,C.fillRect(0,0,c,a),0<c&&0<a&&(s=d.ctx.createPattern(F,"repeat"),d.renderRepeat(o,s,g,w))):2===e.type&&(u=Cs(f,h,[null,null,null]),o=u[0],i=u[1],Q=u[2],c=u[3],a=u[4],l=0===e.position.length?[ge]:e.position,g=Ue(l[0],c),w=Ue(l[l.length-1],a),C=function(A,e,t,r,B){var n,s,o,i,Q=0,c=0;switch(A.size){case 0:0===A.shape?Q=c=Math.min(Math.abs(e),Math.abs(e-r),Math.abs(t),Math.abs(t-B)):1===A.shape&&(Q=Math.min(Math.abs(e),Math.abs(e-r)),c=Math.min(Math.abs(t),Math.abs(t-B)));break;case 2:0===A.shape?Q=c=Math.min(Ie(e,t),Ie(e,t-B),Ie(e-r,t),Ie(e-r,t-B)):1===A.shape&&(n=Math.min(Math.abs(t),Math.abs(t-B))/Math.min(Math.abs(e),Math.abs(e-r)),o=(s=ye(r,B,e,t,!0))[0],i=s[1],c=n*(Q=Ie(o-e,(i-t)/n)));break;case 1:0===A.shape?Q=c=Math.max(Math.abs(e),Math.abs(e-r),Math.abs(t),Math.abs(t-B)):1===A.shape&&(Q=Math.max(Math.abs(e),Math.abs(e-r)),c=Math.max(Math.abs(t),Math.abs(t-B)));break;case 3:0===A.shape?Q=c=Math.max(Ie(e,t),Ie(e,t-B),Ie(e-r,t),Ie(e-r,t-B)):1===A.shape&&(n=Math.max(Math.abs(t),Math.abs(t-B))/Math.max(Math.abs(e),Math.abs(e-r)),o=(s=ye(r,B,e,t,!1))[0],i=s[1],c=n*(Q=Ie(o-e,(i-t)/n)))}return Array.isArray(A.size)&&(Q=Ue(A.size[0],r),c=2===A.size.length?Ue(A.size[1],B):Q),[Q,c]}(e,g,w,c,a),F=C[0],u=C[1],0<F&&0<u&&(U=d.ctx.createRadialGradient(i+g,Q+w,0,i+g,Q+w,F),pe(e.stops,2*F).forEach(function(A){return U.addColorStop(A.stop,ie(A.color))}),d.path(o),d.ctx.fillStyle=U,F!==u?(l=f.bounds.left+.5*f.bounds.width,C=f.bounds.top+.5*f.bounds.height,F=1/(u=u/F),d.ctx.save(),d.ctx.translate(l,C),d.ctx.transform(1,0,0,u,0,0),d.ctx.translate(-l,-C),d.ctx.fillRect(i,F*(Q-C)+C,c,a*F),d.ctx.restore()):d.ctx.fill())),A.label=6;case 6:return h--,[2]}})},d=this,t=0,r=f.styles.backgroundImage.slice(0).reverse(),A.label=1;case 1:return t<r.length?(B=r[t],[5,e(B)]):[3,4];case 2:A.sent(),A.label=3;case 3:return t++,[3,1];case 4:return[2]}})})},Ds.prototype.renderSolidBorder=function(e,t,r){return a(this,void 0,void 0,function(){return H(this,function(A){return this.path(ws(r,t)),this.ctx.fillStyle=ie(e),this.ctx.fill(),[2]})})},Ds.prototype.renderDoubleBorder=function(t,r,B,n){return a(this,void 0,void 0,function(){var e;return H(this,function(A){switch(A.label){case 0:return r<3?[4,this.renderSolidBorder(t,B,n)]:[3,2];case 1:return A.sent(),[2];case 2:return e=function(A,e){switch(e){case 0:return Hs(A.topLeftBorderBox,A.topLeftBorderDoubleOuterBox,A.topRightBorderBox,A.topRightBorderDoubleOuterBox);case 1:return Hs(A.topRightBorderBox,A.topRightBorderDoubleOuterBox,A.bottomRightBorderBox,A.bottomRightBorderDoubleOuterBox);case 2:return Hs(A.bottomRightBorderBox,A.bottomRightBorderDoubleOuterBox,A.bottomLeftBorderBox,A.bottomLeftBorderDoubleOuterBox);default:return Hs(A.bottomLeftBorderBox,A.bottomLeftBorderDoubleOuterBox,A.topLeftBorderBox,A.topLeftBorderDoubleOuterBox)}}(n,B),this.path(e),this.ctx.fillStyle=ie(t),this.ctx.fill(),e=function(A,e){switch(e){case 0:return Hs(A.topLeftBorderDoubleInnerBox,A.topLeftPaddingBox,A.topRightBorderDoubleInnerBox,A.topRightPaddingBox);case 1:return Hs(A.topRightBorderDoubleInnerBox,A.topRightPaddingBox,A.bottomRightBorderDoubleInnerBox,A.bottomRightPaddingBox);case 2:return Hs(A.bottomRightBorderDoubleInnerBox,A.bottomRightPaddingBox,A.bottomLeftBorderDoubleInnerBox,A.bottomLeftPaddingBox);default:return Hs(A.bottomLeftBorderDoubleInnerBox,A.bottomLeftPaddingBox,A.topLeftBorderDoubleInnerBox,A.topLeftPaddingBox)}}(n,B),this.path(e),this.ctx.fill(),[2]}})})},Ds.prototype.renderNodeBackgroundAndBorders=function(c){return a(this,void 0,void 0,function(){var e,t,r,B,n,s,o,i,Q=this;return H(this,function(A){switch(A.label){case 0:return(this.applyEffects(c.getEffects(2)),e=c.container.styles,t=!oe(e.backgroundColor)||e.backgroundImage.length,r=[{style:e.borderTopStyle,color:e.borderTopColor,width:e.borderTopWidth},{style:e.borderRightStyle,color:e.borderRightColor,width:e.borderRightWidth},{style:e.borderBottomStyle,color:e.borderBottomColor,width:e.borderBottomWidth},{style:e.borderLeftStyle,color:e.borderLeftColor,width:e.borderLeftWidth}],B=Ms(Es(e.backgroundClip,0),c.curves),t||e.boxShadow.length)?(this.ctx.save(),this.path(B),this.ctx.clip(),oe(e.backgroundColor)||(this.ctx.fillStyle=ie(e.backgroundColor),this.ctx.fill()),[4,this.renderBackgroundImage(c.container)]):[3,2];case 1:A.sent(),this.ctx.restore(),e.boxShadow.slice(0).reverse().forEach(function(A){Q.ctx.save();var t,r,B,n,e=ts(c.curves),s=A.inset?0:1e4,o=(t=-s+(A.inset?1:-1)*A.spread.number,r=(A.inset?1:-1)*A.spread.number,B=A.spread.number*(A.inset?-2:2),n=A.spread.number*(A.inset?-2:2),e.map(function(A,e){switch(e){case 0:return A.add(t,r);case 1:return A.add(t+B,r);case 2:return A.add(t+B,r+n);case 3:return A.add(t,r+n)}return A}));A.inset?(Q.path(e),Q.ctx.clip(),Q.mask(o)):(Q.mask(e),Q.ctx.clip(),Q.path(o)),Q.ctx.shadowOffsetX=A.offsetX.number+s,Q.ctx.shadowOffsetY=A.offsetY.number,Q.ctx.shadowColor=ie(A.color),Q.ctx.shadowBlur=A.blur.number,Q.ctx.fillStyle=A.inset?ie(A.color):"rgba(0,0,0,1)",Q.ctx.fill(),Q.ctx.restore()}),A.label=2;case 2:s=n=0,o=r,A.label=3;case 3:return s<o.length?0!==(i=o[s]).style&&!oe(i.color)&&0<i.width?2!==i.style?[3,5]:[4,this.renderDashedDottedBorder(i.color,i.width,n,c.curves,2)]:[3,11]:[3,13];case 4:return A.sent(),[3,11];case 5:return 3!==i.style?[3,7]:[4,this.renderDashedDottedBorder(i.color,i.width,n,c.curves,3)];case 6:return A.sent(),[3,11];case 7:return 4!==i.style?[3,9]:[4,this.renderDoubleBorder(i.color,i.width,n,c.curves)];case 8:return A.sent(),[3,11];case 9:return[4,this.renderSolidBorder(i.color,n,c.curves)];case 10:A.sent(),A.label=11;case 11:n++,A.label=12;case 12:return s++,[3,3];case 13:return[2]}})})},Ds.prototype.renderDashedDottedBorder=function(g,w,U,l,C){return a(this,void 0,void 0,function(){var e,t,r,B,n,s,o,i,Q,c,a;return H(this,function(A){return this.ctx.save(),Q=function(A,e){switch(e){case 0:return fs(A.topLeftBorderStroke,A.topRightBorderStroke);case 1:return fs(A.topRightBorderStroke,A.bottomRightBorderStroke);case 2:return fs(A.bottomRightBorderStroke,A.bottomLeftBorderStroke);default:return fs(A.bottomLeftBorderStroke,A.topLeftBorderStroke)}}(l,U),e=ws(l,U),2===C&&(this.path(e),this.ctx.clip()),s=$n(e[0])?(t=e[0].start.x,e[0].start.y):(t=e[0].x,e[0].y),o=$n(e[1])?(r=e[1].end.x,e[1].end.y):(r=e[1].x,e[1].y),B=0===U||2===U?Math.abs(t-r):Math.abs(s-o),this.ctx.beginPath(),3===C?this.formatPath(Q):this.formatPath(e.slice(0,2)),n=w<3?3*w:2*w,s=w<3?2*w:w,3===C&&(s=n=w),o=!0,B<=2*n?o=!1:B<=2*n+s?(n*=i=B/(2*n+s),s*=i):(Q=Math.floor((B+s)/(n+s)),i=(B-Q*n)/(Q-1),s=(Q=(B-(Q+1)*n)/Q)<=0||Math.abs(s-i)<Math.abs(s-Q)?i:Q),o&&(3===C?this.ctx.setLineDash([0,n+s]):this.ctx.setLineDash([n,s])),3===C?(this.ctx.lineCap="round",this.ctx.lineWidth=w):this.ctx.lineWidth=2*w+1.1,this.ctx.strokeStyle=ie(g),this.ctx.stroke(),this.ctx.setLineDash([]),2===C&&($n(e[0])&&(c=e[3],a=e[0],this.ctx.beginPath(),this.formatPath([new Zn(c.end.x,c.end.y),new Zn(a.start.x,a.start.y)]),this.ctx.stroke()),$n(e[1])&&(c=e[1],a=e[2],this.ctx.beginPath(),this.formatPath([new Zn(c.end.x,c.end.y),new Zn(a.start.x,a.start.y)]),this.ctx.stroke())),this.ctx.restore(),[2]})})},Ds.prototype.render=function(B){return a(this,void 0,void 0,function(){return H(this,function(A){switch(A.label){case 0:return this.options.backgroundColor&&(this.ctx.fillStyle=ie(this.options.backgroundColor),this.ctx.fillRect(this.options.x,this.options.y,this.options.width,this.options.height)),t=new as(e=B,null),r=new cs(t),hs(t,r,r,e=[]),ds(t.container,e),[4,this.renderStack(r)];case 1:return A.sent(),this.applyEffects([]),[2,this.canvas]}var e,t,r})})},Ds);function Ds(A,e){A=Ls.call(this,A,e)||this;return A._activeEffects=[],A.canvas=e.canvas||document.createElement("canvas"),A.ctx=A.canvas.getContext("2d"),e.canvas||(A.canvas.width=Math.floor(e.width*e.scale),A.canvas.height=Math.floor(e.height*e.scale),A.canvas.style.width=e.width+"px",A.canvas.style.height=e.height+"px"),A.fontMetrics=new Ks(document),A.ctx.scale(A.options.scale,A.options.scale),A.ctx.translate(-e.x,-e.y),A.ctx.textBaseline="bottom",A._activeEffects=[],A.context.logger.debug("Canvas renderer initialized ("+e.width+"x"+e.height+") with scale "+e.scale),A}var vs,xs=function(A){return A instanceof LB||(A instanceof yB||A instanceof pB&&A.type!==dB&&A.type!==hB)},Ms=function(A,e){switch(A){case 0:return ts(e);case 2:return[e.topLeftContentBox,e.topRightContentBox,e.bottomRightContentBox,e.bottomLeftContentBox];default:return rs(e)}},Ss=function(A){switch(A){case 1:return"center";case 2:return"right";default:return"left"}},Ts=["-apple-system","system-ui"],Gs=function(A){return/iPhone OS 15_(0|1)/.test(window.navigator.userAgent)?A.filter(function(A){return-1===Ts.indexOf(A)}):A},Os=(A(Vs,vs=he),Vs.prototype.render=function(t){return a(this,void 0,void 0,function(){var e;return H(this,function(A){switch(A.label){case 0:return e=Nr(this.options.width*this.options.scale,this.options.height*this.options.scale,this.options.scale,this.options.scale,t),[4,ks(e)];case 1:return e=A.sent(),this.options.backgroundColor&&(this.ctx.fillStyle=ie(this.options.backgroundColor),this.ctx.fillRect(0,0,this.options.width*this.options.scale,this.options.height*this.options.scale)),this.ctx.drawImage(e,-this.options.x*this.options.scale,-this.options.y*this.options.scale),[2,this.canvas]}})})},Vs);function Vs(A,e){A=vs.call(this,A,e)||this;return A.canvas=e.canvas||document.createElement("canvas"),A.ctx=A.canvas.getContext("2d"),A.options=e,A.canvas.width=Math.floor(e.width*e.scale),A.canvas.height=Math.floor(e.height*e.scale),A.canvas.style.width=e.width+"px",A.canvas.style.height=e.height+"px",A.ctx.scale(A.options.scale,A.options.scale),A.ctx.translate(-e.x,-e.y),A.context.logger.debug("EXPERIMENTAL ForeignObject renderer initialized ("+e.width+"x"+e.height+" at "+e.x+","+e.y+") with scale "+e.scale),A}var ks=function(r){return new Promise(function(A,e){var t=new Image;t.onload=function(){A(t)},t.onerror=e,t.src="data:image/svg+xml;charset=utf-8,"+encodeURIComponent((new XMLSerializer).serializeToString(r))})},Rs=(Ns.prototype.debug=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];this.enabled&&("undefined"!=typeof window&&window.console&&"function"==typeof console.debug?console.debug.apply(console,t([this.id,this.getTime()+"ms"],A)):this.info.apply(this,A))},Ns.prototype.getTime=function(){return Date.now()-this.start},Ns.prototype.info=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];this.enabled&&"undefined"!=typeof window&&window.console&&"function"==typeof console.info&&console.info.apply(console,t([this.id,this.getTime()+"ms"],A))},Ns.prototype.warn=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];this.enabled&&("undefined"!=typeof window&&window.console&&"function"==typeof console.warn?console.warn.apply(console,t([this.id,this.getTime()+"ms"],A)):this.info.apply(this,A))},Ns.prototype.error=function(){for(var A=[],e=0;e<arguments.length;e++)A[e]=arguments[e];this.enabled&&("undefined"!=typeof window&&window.console&&"function"==typeof console.error?console.error.apply(console,t([this.id,this.getTime()+"ms"],A)):this.info.apply(this,A))},Ns.instances={},Ns);function Ns(A){var e=A.id,A=A.enabled;this.id=e,this.enabled=A,this.start=Date.now()}var Ps=(Xs.instanceCount=1,Xs);function Xs(A,e){this.windowBounds=e,this.instanceName="#"+Xs.instanceCount++,this.logger=new Rs({id:this.instanceName,enabled:A.logging}),this.cache=null!==(e=A.cache)&&void 0!==e?e:new On(this,A)}"undefined"!=typeof window&&Tn.setContext(window);var Js=function(u,F){return a(void 0,void 0,void 0,function(){var e,t,r,B,n,s,o,i,Q,c,a,g,w,U,l,C;return H(this,function(A){switch(A.label){case 0:if(!u||"object"!=typeof u)return[2,Promise.reject("Invalid element provided as first argument")];if(!(e=u.ownerDocument))throw new Error("Element is not attached to a Document");if(!(t=e.defaultView))throw new Error("Document is not attached to a Window");return w={allowTaint:null!==(U=F.allowTaint)&&void 0!==U&&U,imageTimeout:null!==(c=F.imageTimeout)&&void 0!==c?c:15e3,proxy:F.proxy,useCORS:null!==(a=F.useCORS)&&void 0!==a&&a},U=h({logging:null===(g=F.logging)||void 0===g||g,cache:F.cache},w),c={windowWidth:null!==(c=F.windowWidth)&&void 0!==c?c:t.innerWidth,windowHeight:null!==(a=F.windowHeight)&&void 0!==a?a:t.innerHeight,scrollX:null!==(g=F.scrollX)&&void 0!==g?g:t.pageXOffset,scrollY:null!==(w=F.scrollY)&&void 0!==w?w:t.pageYOffset},a=new d(c.scrollX,c.scrollY,c.windowWidth,c.windowHeight),g=new Ps(U,a),c=null!==(w=F.foreignObjectRendering)&&void 0!==w&&w,w={allowTaint:null!==(U=F.allowTaint)&&void 0!==U&&U,onclone:F.onclone,ignoreElements:F.ignoreElements,inlineImages:c,copyStyles:c},g.logger.debug("Starting document clone with size "+a.width+"x"+a.height+" scrolled to "+-a.left+","+-a.top),U=new dn(g,u,w),(w=U.clonedReferenceElement)?[4,U.toIFrame(e,a)]:[2,Promise.reject("Unable to find element in cloned iframe")];case 1:return(r=A.sent(),l=jB(w)||"HTML"===w.tagName?function(A){var e=A.body,t=A.documentElement;if(!e||!t)throw new Error("Unable to get document size");A=Math.max(Math.max(e.scrollWidth,t.scrollWidth),Math.max(e.offsetWidth,t.offsetWidth),Math.max(e.clientWidth,t.clientWidth)),t=Math.max(Math.max(e.scrollHeight,t.scrollHeight),Math.max(e.offsetHeight,t.offsetHeight),Math.max(e.clientHeight,t.clientHeight));return new d(0,0,A,t)}(w.ownerDocument):f(g,w),B=l.width,n=l.height,s=l.left,o=l.top,i=Ys(g,w,F.backgroundColor),l={canvas:F.canvas,backgroundColor:i,scale:null!==(l=null!==(l=F.scale)&&void 0!==l?l:t.devicePixelRatio)&&void 0!==l?l:1,x:(null!==(l=F.x)&&void 0!==l?l:0)+s,y:(null!==(l=F.y)&&void 0!==l?l:0)+o,width:null!==(l=F.width)&&void 0!==l?l:Math.ceil(B),height:null!==(l=F.height)&&void 0!==l?l:Math.ceil(n)},c)?(g.logger.debug("Document cloned, using foreign object rendering"),[4,new Os(g,l).render(w)]):[3,3];case 2:return Q=A.sent(),[3,5];case 3:return g.logger.debug("Document cloned, element located at "+s+","+o+" with size "+B+"x"+n+" using computed rendering"),g.logger.debug("Starting DOM parsing"),C=kB(g,w),i===C.styles.backgroundColor&&(C.styles.backgroundColor=Le.TRANSPARENT),g.logger.debug("Starting renderer for element at "+l.x+","+l.y+" with size "+l.width+"x"+l.height),[4,new bs(g,l).render(C)];case 4:Q=A.sent(),A.label=5;case 5:return null!==(C=F.removeContainer)&&void 0!==C&&!C||dn.destroy(r)||g.logger.error("Cannot detach cloned iframe as it is not in the DOM anymore"),g.logger.debug("Finished rendering"),[2,Q]}})})},Ys=function(A,e,t){var r=e.ownerDocument,B=r.documentElement?fe(A,getComputedStyle(r.documentElement).backgroundColor):Le.TRANSPARENT,n=r.body?fe(A,getComputedStyle(r.body).backgroundColor):Le.TRANSPARENT,t="string"==typeof t?fe(A,t):null===t?Le.TRANSPARENT:4294967295;return e===r.documentElement?oe(B)?oe(n)?t:n:B:t};return function(A,e){return Js(A,e=void 0===e?{}:e)}});
</script>
<script>
const $=id=>document.getElementById(id);
let MASTERS=[],CUR=null,DATA=null,view='msg',shown=0,ME_GUESS=null,ME_OPTS='',ZBLOBS={};
const jget=u=>fetch(u).then(r=>r.json());
const jgetQ=u=>fetch(u).then(r=>r.ok?r.json():null).catch(()=>null);
const esc=s=>(s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYPE_ICON={2:'🖼',3:'🎨',4:'📷',6:'📎',7:'📎',18:'📎',19:'🔗',22:'📎',24:'🔗',28:'🎙',31:'🎬',32:'🎙',37:'🎬'};
function prettyText(m){
  const mt=+m.msgType||0;const raw=m.text||'';
  if(mt===2&&raw.trim().startsWith('{')){
    try{const j=JSON.parse(raw);const p=JSON.parse(j.params||'{}');
      return '🖼 Ảnh '+(p.width||'?')+'×'+(p.height||'?')+' — link CDN đã hết hạn';}catch(e){}
  }
  if(mt===18&&raw.trim().startsWith('{')){
    try{const j=JSON.parse(raw);const p=JSON.parse(j.params||'{}');
      const dur=+p.duration||0;const ds=dur?Math.round(dur/1000)+'s':'';
      const sz=+p.fileSize?fmtB(+p.fileSize):'';
      const dim=(p.video_original_width&&p.video_original_height)?p.video_original_width+'×'+p.video_original_height:'';
      const grp=(+p.total_item_in_group>1)?' (album '+(+p.id_in_group+1)+'/'+p.total_item_in_group+')':'';
      return '🎬 Video'+(ds?' '+ds:'')+(sz?' · '+sz:'')+(dim?' · '+dim:'')+grp+' — link CDN đã hết hạn';}catch(e){}
  }
  if(raw.trim().startsWith('{')){
    try{const j=JSON.parse(raw);const t=j.title||j.mediaTitle||'';const h=j.href||'';const act=j.action||'';
      if(act==='recommened.misscall')return '📞 Cuộc gọi nhỡ';
      if(act==='recommened.user')return '👤 Liên hệ: '+(t||'');
      if(mt===6)return '📎 '+(t||'File đính kèm');
      if(mt===19||mt===24)return '🔗 '+(t||'Link')+(h&&h!==t?' — '+h:'');
      if(t)return t;
    }catch(e){}
  }
  return raw;
}
function fmtB(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB'}
let MSTAT=null,MSTAT_POLL=null;
function fmtPct(a,b){return b?Math.round(100*a/b)+'%':'—'}
function renderMstat(s){
  const box=document.getElementById('mstat');if(!box)return;
  if(!s){box.style.display='none';return}
  const total=s.mediaMsgs||0;
  const seg=(n,cls,label)=>n?`<span class="ms-seg ${cls}">${label}: <b>${n.toLocaleString('vi-VN')}</b> (${fmtPct(n,total)})</span>`:'';
  const audit=s.auditing?`<span class="ms-audit">⏳ đang kiểm tra link… ${Math.round(100*(s.auditDone||0)/(s.auditTotal||1))}%</span>`:'';
  box.style.display='flex';
  box.innerHTML=`<span class="ms-title">🖼 ${total.toLocaleString('vi-VN')} ảnh+video:</span>`
    +seg(s.saved,'ms-ok','💾 đã lưu local')
    +seg(s.linkAlive,'ms-alive','🔗 link sống')
    +seg(s.linkDead,'ms-dead','💀 đã mất vĩnh viễn')
    +(s.unknown?`<span class="ms-seg ms-unk">❔ chưa kiểm tra: <b>${s.unknown.toLocaleString('vi-VN')}</b> (${fmtPct(s.unknown,total)})</span>`:'')
    +audit;
}
async function pollMstat(){
  if(!CUR)return;
  const s=await jgetQ('/api/mediastats?uid='+encodeURIComponent(CUR.uid));
  if(s&&!s.error){MSTAT=s;renderMstat(s);}
  if(MSTAT&&MSTAT.auditing){MSTAT_POLL=setTimeout(pollMstat,1500);}
  else{MSTAT_POLL=null;}
}
function startMstat(){
  if(MSTAT_POLL){clearTimeout(MSTAT_POLL);MSTAT_POLL=null;}
  MSTAT=null;
  pollMstat();
}

async function init(){
  const j=await jget('/api/masters'); MASTERS=j.masters||[];
  if(!MASTERS.length){$('mlist').innerHTML='<div class="empty">Chưa có master nào.<br>Chạy xuất + Gộp bản trùng lặp trước.</div>';return}
  $('mlist').innerHTML=MASTERS.map((m,i)=>{
    const md=m.media;
    return `<div class="mcard" data-i="${i}"><div class="nm">${esc(m.conversation)}</div>
    <div class="meta">${m.count.toLocaleString('vi-VN')} tin · ${esc((m.first||'').slice(0,10))} → ${esc((m.last||'').slice(0,10))}</div>
    <div>${m.recalled?`<span class="badge warn">⚠ ${m.recalled} nghi thu hồi</span>`:''}
    <span class="badge acc">💬 master</span>${md?`<span class="badge">🖼 ${md.photos+md.videos} file · ${fmtB(md.bytes)}</span>`:''}</div></div>`}).join('');
  document.querySelectorAll('.mcard').forEach(c=>c.onclick=()=>select(+c.dataset.i));
  select(0);
}

async function select(i){
  CUR=MASTERS[i];
  document.querySelectorAll('.mcard').forEach((c,k)=>c.classList.toggle('sel',k===i));
  $('d1').value='';$('d2').value='';$('q').value='';$('or').checked=false;document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',c.dataset.r==='all'));
  await load();
}
function rangeFor(r){
  if(r==='all')return ['',''];
  const d=new Date();const to=d.toISOString().slice(0,10);
  if(r==='y')return [d.toISOString().slice(0,4)+'-01-01',to];
  d.setDate(d.getDate()-(+r));return [d.toISOString().slice(0,10),to];
}
async function load(){
  if(!CUR)return;
  $('content').innerHTML='<div class="empty">Đang tải…</div>';
  startMstat();
  const f=$('d1').value,t=$('d2').value;
  DATA=await jget(`/api/master?uid=${encodeURIComponent(CUR.uid)}&from=${f}&to=${t}`);
  if(DATA.error){$('content').innerHTML='<div class="empty">'+esc(DATA.error)+'</div>';return}
  zmode=false;$('tzalo').classList.remove('on');$('tmsg').classList.add('on');$('tmed').classList.remove('on');view='msg';
  ZBLOBS={};(DATA.media&&DATA.media.files||[]).forEach((f,k)=>ZBLOBS[k]=f.file.split('/').pop());
  ME_GUESS=localStorage.getItem('zme_'+DATA.uid)||null;
  const senders=[...new Set(DATA.messages.map(m=>m.sender).filter(Boolean))];
  ME_OPTS=senders.map(s=>'<option value="'+esc(s)+'"'+(ME_GUESS===s?' selected':'')+'>'+esc(s)+'</option>').join('');
  if(!ME_GUESS){
    const low=senders.map(s=>s.toLowerCase());
    const nonToi=senders.filter((s,i)=>low[i]!=='tôi'&&low[i]!=='toi');
    if(senders.length===2)ME_GUESS=senders[0];
    else if(nonToi.length&&nonToi.length<senders.length)ME_GUESS=nonToi[0];
    else if(senders.length)ME_GUESS=senders[0];
    if(ME_GUESS)try{localStorage.setItem('zme_'+DATA.uid,ME_GUESS)}catch(e){}
  }
  $('tmed').textContent='🖼 Media'+(DATA.media?` (${DATA.media.photos+DATA.media.videos})`:'');
  shown=0;render();
}
function drawMsgs(){
  const q=$('q').value.trim().toLowerCase(),or=$('or').checked;
  let msgs=DATA.messages;
  if(or)msgs=msgs.filter(m=>m.missingSince);
  if(q)msgs=msgs.filter(m=>(m.text||'').toLowerCase().includes(q)||(m.sender||'').toLowerCase().includes(q));
  const CHUNK=800,part=msgs.slice(0,shown+CHUNK);shown=part.length;
  let html='',day='';
  for(const m of part){
    const d=(m.time||'').slice(0,10);
    if(d!==day){day=d;html+=`<div class="dayhdr">${esc(d)}</div>`}
    const mt=+m.msgType||0,icon=TYPE_ICON[mt]||'';
    const pt=prettyText(m);
    const txt=esc(zEscapeBlobs(pt));
    const cp=m.copies>1?`<span class="cp">· ${m.copies} bản (gộp 2 account)</span>`:'';
    const rec=m.missingSince?`<div class="rec">⚠ Nghi bị thu hồi — lastSeen ${esc(m.lastSeen||'')}, thiếu từ ${esc(m.missingSince||'')}${cp}</div>`:cp?`<div class="cp">${cp}</div>`:'';
    html+=`<div class="msg ${mt!==1?'media':''} ${m.missingSince?'recalled':''}" title="msgId ${esc(m.msgId||'')} · firstSeen ${esc(m.firstSeen||'')} · lastSeen ${esc(m.lastSeen||'')}">
      <div class="t">${esc((m.time||'').slice(11))}</div>
      <div class="w"><div class="s">${esc(m.sender||'?')}</div>${icon?`<div style="font-size:10.5px">${icon}</div>`:''}</div>
      <div class="x">${txt||'<i>(không có nội dung)</i>'}${rec}</div></div>`;
  }
  const rest=msgs.length-part.length;
  if(rest>0)html+=`<button class="more" onclick="shown=${part.length};drawMsgs()">Hiện thêm ${Math.min(CHUNK,rest)} / còn ${rest}</button>`;
  if(!part.length)html='<div class="empty">Không có tin nào khớp bộ lọc.</div>';
  $('content').innerHTML=html;
  $('statchip').textContent=`${part.length.toLocaleString('vi-VN')}/${msgs.length.toLocaleString('vi-VN')} tin đang hiện · tổng ${DATA.total.toLocaleString('vi-VN')}`;
}
function drawMedia(){
  const md=DATA.media;
  if(!md||!(md.files||[]).length){$('content').innerHTML='<div class="empty">Chưa có media master cho hội thoại này.<br>Chạy "Tải tất cả ảnh &amp; video" rồi "Gộp bản trùng lặp".</div>';return}
  let html=`<div class="dayhdr" style="margin-top:14px">${md.photos} ảnh · ${md.videos} video · ${fmtB(md.bytes)} — mới nhất trước</div><div id="grid">`;
  for(const f of md.files){
    const mt=f.msgType||0;
    const u=`/api/mediafile?uid=${encodeURIComponent(DATA.uid)}&f=${encodeURIComponent(f.file)}`;
    const inner=(mt===18&&m.f)
      ?`<video class="zmsgvid" controls preload="metadata" src="${urlFor(m.f)}"></video>`
      :(mt===2&&m.f)
      ?`<img class="zmsgimg" loading="lazy" src="${urlFor(m.f)}" alt="" onclick="zLb('${urlFor(m.f)}')">`
      :f.file.endsWith('.mp4')
      ?`<video controls preload="metadata" src="${u}"></video>`
      :`<img loading="lazy" src="${u}" alt="">`;
    html+=`<div class="cell">${inner}<div class="cap"><b>📅 ${esc(f.sentAt||'?')}</b> · ${fmtB(f.bytes)}${f.alsoAs?` · ${f.alsoAs} tên khác`:''}<br>${esc(f.file.split('/').pop())}</div></div>`;
  }
  $('content').innerHTML=html+'</div>';
  $('statchip').textContent=`${md.photos+md.videos} file · ${fmtB(md.bytes)}`;
}
function render(){view==='zalo'?drawZalo():(view==='msg'?drawMsgs():drawMedia())}

$('d1').onchange=$('d2').onchange=load;
$('or').onchange=()=>{shown=0;drawMsgs()};
let qt=null;$('q').oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{shown=0;drawMsgs()},250)};
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));c.classList.add('on');
  const[a,b]=rangeFor(c.dataset.r);$('d1').value=a;$('d2').value=b;load();
});
$('tmsg').onclick=()=>{view='msg';$('tmsg').classList.add('on');$('tmed').classList.remove('on');render()};
$('tmed').onclick=()=>{view='med';$('tmed').classList.add('on');$('tmsg').classList.remove('on');render()};
let zmode=false;
function urlFor(f){return '/api/mediafile?uid='+encodeURIComponent(DATA.uid)+'&f='+encodeURIComponent(f)}
function zEscapeBlobs(s){return s.replace(/#blob-([0-9]+)/g,(m,n)=>ZBLOBS[+n]||'')}
function zDayLabel(d){const t=new Date(d+'T00:00:00'),n=new Date();const D=x=>new Date(x.getFullYear(),x.getMonth(),x.getDate());if(+D(t)===+D(n))return 'Hôm nay';const y=new Date(n);y.setDate(y.getDate()-1);if(+D(t)===+D(y))return 'Hôm qua';return d.split('-').reverse().join('/')}
function drawZalo(){
  const q=$('q').value.trim().toLowerCase(),or=$('or').checked;
  let msgs=DATA.messages;
  if(or)msgs=msgs.filter(m=>m.missingSince);
  if(q)msgs=msgs.filter(m=>(m.text||'').toLowerCase().includes(q)||(m.sender||'').toLowerCase().includes(q));
  const CHUNK=400,part=msgs.slice(0,shown+CHUNK);shown=part.length;
  let html='',day='';
  for(const m of part){
    const d=(m.time||'').slice(0,10);
    if(d!==day){day=d;html+='<div class="zday"><span>'+esc(zDayLabel(d))+'</span></div>'}
    const me=ME_GUESS&&m.sender===ME_GUESS;
    const mt=+m.msgType||0;
    let av='';if(!me)av='<div class="zav">'+esc((m.sender||'?').trim().charAt(0).toUpperCase())+'</div>';
    let inner='';    if((mt===2||mt===18)&&m.f){
      const u=urlFor(m.f);
      inner=mt===18
        ?'<video class="zmsgvid" controls preload="metadata" src="'+u+'"></video><span class="zmedia-meta">📅 '+esc(m.time||'')+'</span>'
        :'<img class="zmsgimg" loading="lazy" src="'+u+'" alt="" onclick="zLb(this.src)"><span class="zmedia-meta">📅 '+esc(m.time||'')+'</span>';
    }else{
      const pt=prettyText(m);
      inner=esc(zEscapeBlobs(pt))||'<i>(không có nội dung)</i>';
    }
    const rec=m.missingSince?'<span class="zrec">⚠ Nghi bị thu hồi — lastSeen '+esc(m.lastSeen||'')+', thiếu từ '+esc(m.missingSince||'')+'</span>':'';
    html+='<div class="zrow'+(me?' me':'')+'">'+av+'<div class="zbubble'+(m.f?' zmedia':'')+(m.missingSince?' zrecalled':'')+'" title="'+esc((m.sender||'?'))+' · '+esc(m.time||'')+' · msgId '+esc(m.msgId||'')+'">'+inner+'<span class="ztime">'+esc((m.time||'').slice(11,16))+(m.copies>1?' · 2 bản':'')+'</span>'+rec+'</div></div>';
  }
  const rest=msgs.length-part.length;
  if(rest>0)html+='<button class="more" onclick="shown='+part.length+';'+(zmode?'drawZalo()':'drawMsgs()')+'">Hiện thêm '+Math.min(CHUNK,rest)+' / còn '+rest+'</button>';
  if(!part.length)html='<div class="zempty">Không có tin nào khớp bộ lọc.</div>';
  $('content').innerHTML=(zmode&&part.length?'<div class="zwrap"><div class="zbar"><button class="zback" title="Về giao diện dữ liệu">←</button>💬 '+esc(CUR.conversation)+'<select class="zme" id="zme" title="Chọn account nào là &quot;tôi&quot;">'+(ME_OPTS||'')+'</select></div><div class="zscroll">'+html+'</div></div>':html);
  if(zmode&&part.length){
    document.querySelector('.zbar .zback').onclick=()=>{$('tzalo').classList.remove('on');view='msg';$('tmsg').classList.add('on');render()};
    const sel=document.getElementById('zme');
    if(sel){sel.onchange=()=>{ME_GUESS=sel.value;try{localStorage.setItem('zme_'+DATA.uid,ME_GUESS)}catch(e){}shown=0;drawZalo()};
      if(ME_GUESS)sel.value=ME_GUESS;}
  }
  $('statchip').textContent=part.length.toLocaleString('vi-VN')+'/'+msgs.length.toLocaleString('vi-VN')+' tin đang hiện · tổng '+DATA.total.toLocaleString('vi-VN');
  if(window.scrollTo&&$('content').scrollTop===0){const sc=$('content');sc.scrollTop=sc.scrollHeight}
}
function zLb(src){const lb=document.getElementById('zlightbox');lb.querySelector('img').src=src;lb.style.display='flex'}
document.getElementById('zlightbox').onclick=function(){this.style.display='none'};
$('tzalo').onclick=()=>{zmode=true;view='zalo';shown=0;$('tzalo').classList.add('on');$('tmsg').classList.remove('on');$('tmed').classList.remove('on');drawZalo()};
$('tcap').onclick=async()=>{
  try{$('tcap').textContent='⏳ Đang chụp…';
    const node=zmode?document.querySelector('.zwrap'):$('content');
    const cv=await html2canvas(node,{backgroundColor:'#0f172a',useCORS:true,scale:window.devicePixelRatio||1.5});
    const a=document.createElement('a');
    a.download=(CUR?CUR.conversation:'viewer')+'_'+(zmode?'zalo_':'')+new Date().toISOString().slice(0,19).replace(/[:T]/g,'-')+'.png';
    a.href=cv.toDataURL('image/png');a.click();
  }catch(e){alert('Chụp lỗi: '+e)}finally{$('tcap').textContent='📷 Chụp màn hình (PNG)'}
};
init();
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/" or p == "/index.html":
            self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/status":
            try:
                ok = cdp_ready()
                st = {"cdp": ok, "uid": None, "name": None, "convCount": 0}
                if ok:
                    c = Cdp(find_page())
                    me = json.loads(c.evaluate(JS["me"]))
                    st["uid"], st["name"] = me.get("uid"), me.get("name")
                    try:
                        convs = json.loads(c.evaluate(JS["convs"]))
                        st["convCount"] = len(convs)
                    except Exception:
                        pass
                self._json(st)
            except Exception as e:
                self._json({"cdp": False, "error": str(e)[:200]})
        elif p == "/api/conversations":
            try:
                convs = json.loads(Cdp(find_page()).evaluate(JS["convs"]))
                convs.sort(key=lambda c: c.get("lastTime") or 0, reverse=True)
                self._json({"convs": convs})
            except Exception as e:
                self._json({"convs": [], "error": str(e)[:200]}, 500)
        elif p == "/viewer":
            self._send(200, VIEWER_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif p == "/api/masters":
            try:
                self._json(_viewer_api_masters())
            except Exception as e:
                self._json({"error": str(e)[:200]}, 500)
        elif p == "/api/master":
            try:
                qs = parse_qs(urlparse(self.path).query)
                self._json(_viewer_api_master(
                    qs.get("uid", [""])[0], qs.get("from", [""])[0], qs.get("to", [""])[0]))
            except Exception as e:
                self._json({"error": str(e)[:200]}, 500)
        elif p == "/api/mediastats":
            try:
                qs = parse_qs(urlparse(self.path).query)
                self._json(_viewer_media_stats(qs.get("uid", [""])[0]))
            except Exception as e:
                self._json({"error": str(e)[:200]}, 500)
        elif p == "/api/mediafile":
            try:
                _viewer_mediafile(self, parse_qs(urlparse(self.path).query))
            except (ConnectionAbortedError, BrokenPipeError):
                pass
            except Exception as e:
                try:
                    self._json({"error": str(e)[:200]}, 500)
                except Exception:
                    pass
        elif p == "/api/progress":
            job = JOBS.get(self.path.split("job=")[-1])
            if not job:
                self._json({"status": "error", "error": "unknown job"}); return
            out = {k: job.get(k) for k in ("status", "done", "total", "error",
                                            "found", "okCount", "failCount",
                                            "current", "summary", "extra")}
            out["count"] = job.get("total") or job.get("done")
            out["filename"] = os.path.basename(job["file"]) if job.get("file") else None
            self._json(out)
        elif p == "/api/download":
            job = JOBS.get(self.path.split("job=")[-1])
            if not job or not job.get("file") or not os.path.exists(job["file"]):
                self._json({"error": "file not ready"}, 404); return
            with open(job["file"], "rb") as f:
                data = f.read()
            fn = os.path.basename(job["file"])
            # RFC 5987: ASCII fallback + UTF-8 encoded filename* for Vietnamese names
            fn_ascii = fn.encode("ascii", "ignore").decode() or "backup"
            self._send(200, data, "application/octet-stream", {
                "Content-Disposition":
                    f"attachment; filename=\"{fn_ascii}\"; filename*=UTF-8''{urlquote(fn)}",
            })
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?")[0]
        if p == "/api/export":
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            uid, fmt = body.get("uid"), body.get("fmt", "json")
            friendly = not bool(body.get("raw"))
            if not uid:
                self._json({"error": "missing uid"}, 400); return
            jid = f"{int(time.time()*1000):x}"
            JOBS[jid] = {"status": "running", "uid": uid, "fmt": fmt, "done": 0,
                         "total": 0, "file": None, "error": None, "name": uid}
            threading.Thread(target=run_job, args=(jid, uid, fmt, friendly), daemon=True).start()
            self._json({"job": jid})
        elif p == "/api/exportmedia":
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            uid = body.get("uid")
            if not uid:
                self._json({"error": "missing uid"}, 400); return
            jid = f"{int(time.time()*1000):x}"
            JOBS[jid] = {"status": "running", "uid": uid, "fmt": "media", "done": 0,
                         "total": 0, "file": None, "error": None, "name": uid}
            threading.Thread(target=run_media_job, args=(jid, uid), daemon=True).start()
            self._json({"job": jid})
        elif p == "/api/cancel":
            CANCEL.set()
            for j in JOBS.values():
                if j.get("status") in ("running", "scan", "downloading", "saving"):
                    j["canceling"] = True
            self._json({"ok": True})
        elif p == "/api/merge":
            jid = f"{int(time.time()*1000):x}"
            JOBS[jid] = {"status": "running", "uid": "*", "fmt": "merge", "done": 0,
                         "total": 0, "file": None, "error": None, "name": "MERGE",
                         "current": None, "summary": []}
            CANCEL.clear()
            threading.Thread(target=run_merge_job, args=(jid,), daemon=True).start()
            self._json({"job": jid})
        elif p == "/api/exportall":
            ln = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(ln) or b"{}")
            fmt = body.get("fmt", "md")
            if fmt not in EXT:
                fmt = "md"
            jid = f"{int(time.time()*1000):x}"
            JOBS[jid] = {"status": "running", "uid": "*", "fmt": fmt, "done": 0,
                         "total": 0, "file": None, "error": None, "name": "TẤT CẢ",
                         "current": None, "summary": []}
            CANCEL.clear()
            threading.Thread(target=run_all_job, args=(jid, fmt), daemon=True).start()
            self._json({"job": jid})
        elif p == "/api/restart":
            ok = restart_zalo_debug()
            self._json({"ok": bool(ok)})
        else:
            self._json({"error": "not found"}, 404)


def _free_port(start=8320, tries=20):
    for p in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _someone_listening(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    port = 8320
    # already running? just open the browser to it
    try:
        if _someone_listening(port):
            try:
                st = json.loads(urllib.request.urlopen(
                    f"http://localhost:{port}/api/status", timeout=4).read())
            except Exception:
                st = {}
            if st.get("cdp") is not None:  # it's our app
                print(f"WebUI already running at http://localhost:{port}")
                webbrowser.open(f"http://localhost:{port}")
                return
    except Exception:
        pass
    port = _free_port(port)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://localhost:{port}"
    print(f"Zalo Backup WebUI: {url}  (Ctrl+C to stop)")
    if not os.environ.get("ZALO_NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
