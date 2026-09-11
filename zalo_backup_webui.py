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
    return arr, total


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
    if t == "2":
        return "[Ảnh] " + (v.get("oriUrl") or v.get("normalUrl") or "")
    if t == "18":
        return "[Video] " + (v.get("oriUrl") or "")
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


def _viewer_api_master(uid, d1, d2):
    fp = _viewer_master_path(uid)
    if not fp:
        return {"error": "master not found for uid " + str(uid)}
    d = _viewer_load_master(fp)
    msgs = d.get("messages") or []
    keys = ("time", "sender", "msgType", "text", "msgId", "firstSeen",
            "lastSeen", "missingSince", "copies", "sources")
    out = []
    for m in msgs:
        t = m.get("time") or ""
        if d1 and t[:10] < d1:
            continue
        if d2 and t[:10] > d2:
            continue
        out.append({k: m.get(k) for k in keys})
    return {"conversation": d.get("conversation"), "uid": str(d.get("uid") or ""),
            "masterFile": os.path.basename(fp), "total": len(msgs), "count": len(out),
            "messages": out, "media": _viewer_media_info(uid, with_files=True)}


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
  <div id="tabs"><button id="tmsg" class="on">💬 Tin nhắn</button><button id="tmed">🖼 Media</button></div>
  <div id="content"></div>
 </div>
</div>
<script>
const $=id=>document.getElementById(id);
let MASTERS=[],CUR=null,DATA=null,view='msg',shown=0;
const jget=u=>fetch(u).then(r=>r.json());
const esc=s=>(s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYPE_ICON={2:'🖼',3:'🎨',4:'📷',6:'📎',7:'📎',18:'📎',19:'🔗',22:'📎',24:'🔗',28:'🎙',31:'🎬',32:'🎙',37:'🎬'};
function fmtB(n){if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(1)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB'}

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
  const f=$('d1').value,t=$('d2').value;
  DATA=await jget(`/api/master?uid=${encodeURIComponent(CUR.uid)}&from=${f}&to=${t}`);
  if(DATA.error){$('content').innerHTML='<div class="empty">'+esc(DATA.error)+'</div>';return}
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
    let txt=esc(m.text||'');
    if(mt===2&&m.text&&m.text.trim().startsWith('{')){
      try{const j=JSON.parse(m.text);const p=JSON.parse(j.params||'{}');
        txt=`🖼 Ảnh ${p.width||'?'}×${p.height||'?'} — link CDN ${new URL(j.oriUrl||j.thumbUrl||'http://x').hostname}`;}
      catch(e){}
    } else if(txt.startsWith('{')){
      try{const j=JSON.parse(txt);const t=j.title||j.mediaTitle||'';const h=j.href||'';
        if(mt===6)txt='📎 '+(t?esc(t):'File đính kèm');
        else if(mt===19||mt===24)txt='🔗 '+(t?esc(t):'Link')+(h&&h!==t?' — '+esc(h):'');
        else if(t)txt=esc(t);
      }catch(e){}
    }
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
    const u=`/api/mediafile?uid=${encodeURIComponent(DATA.uid)}&f=${encodeURIComponent(f.file)}`;
    const inner=f.file.endsWith('.mp4')
      ?`<video controls preload="metadata" src="${u}"></video>`
      :`<img loading="lazy" src="${u}" alt="">`;
    html+=`<div class="cell">${inner}<div class="cap"><b>📅 ${esc(f.sentAt||'?')}</b> · ${fmtB(f.bytes)}${f.alsoAs?` · ${f.alsoAs} tên khác`:''}<br>${esc(f.file.split('/').pop())}</div></div>`;
  }
  $('content').innerHTML=html+'</div>';
  $('statchip').textContent=`${md.photos+md.videos} file · ${fmtB(md.bytes)}`;
}
function render(){view==='msg'?drawMsgs():drawMedia()}

$('d1').onchange=$('d2').onchange=load;
$('or').onchange=()=>{shown=0;drawMsgs()};
let qt=null;$('q').oninput=()=>{clearTimeout(qt);qt=setTimeout(()=>{shown=0;drawMsgs()},250)};
document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('.chip').forEach(x=>x.classList.remove('on'));c.classList.add('on');
  const[a,b]=rangeFor(c.dataset.r);$('d1').value=a;$('d2').value=b;load();
});
$('tmsg').onclick=()=>{view='msg';$('tmsg').classList.add('on');$('tmed').classList.remove('on');render()};
$('tmed').onclick=()=>{view='med';$('tmed').classList.add('on');$('tmsg').classList.remove('on');render()};
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
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
