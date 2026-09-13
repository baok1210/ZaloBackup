#!/usr/bin/env python3
"""Instagram DYI (Download Your Information) -> ZaloBackup master converter.

Instagram has no live API for us to crawl, but it DOES offer an official full
export: Instagram app/site -> Your activity -> Download your information
(JSON format). This module converts that archive into the same master_*.json /
media_master_* layout the Zalo tool uses, so the entire viewer (messages by
day, recalled flags, media grid, Zalo-style bubbles) works on Instagram chats
with zero new UI.

Usage:
  python ig_import.py <extracted-DYI-folder> [out_dir]
  (out_dir defaults to ./exports next to this script)

Notes on the DYI format (verified against a real 2026 export):
  messages/inbox/<Title>_<threadid>/message_1.json ... message_N.json
    -> {"participants": [{"name": ...}], "messages": [{...}], "title": ...}
  Every string is double-encoded (UTF-8 read as Latin-1) and must be repaired.
  Message fields used: sender_name, timestamp_ms, content, photos, videos,
  audio_files, gifs, shares, call_duration, is_geoblocked_for_viewer.
  Message text content is NOT in the JSON media entries — media carry a "uri"
  pointing at the image/video file inside the same archive.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sys
from datetime import datetime

import zalo_merge as ZMERGE


# --------------------------------------------------------------------------
# Mojibake repair: Instagram writes UTF-8 bytes decoded as Latin-1.
# "T\x01\x00ti" style artifacts and broken Vietnamese diacritics both fix by
# re-encoding Latin-1 -> decoding UTF-8. Strings that fail are returned as-is.
def fix_ig(s):
    if not isinstance(s, str) or not s:
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def _thread_id_from_folder(folder_name):
    """DYI folders end with '-<digits>' = stable thread id; else hash the name."""
    m = re.search(r"-(\d{6,})$", folder_name or "")
    if m:
        return "ig" + m.group(1)[-18:]
    return "ig" + hashlib.sha1((folder_name or "").encode("utf-8")).hexdigest()[:16]


def _content_text(m):
    parts = [fix_ig(c) for c in (m.get("content") or []) if isinstance(c, str)]
    if parts:
        return "\n".join(parts)
    if m.get("call_duration"):
        return ""
    return ""


def _kind_of_ig(m):
    """Map an IG message dict to Zalo msgType (str) + pretty text fallback."""
    if m.get("call_duration"):
        secs = int(m["call_duration"])
        mins, s = divmod(secs, 60)
        dur = f" {mins} phút {s} giây" if mins else f" {s} giây"
        return "6", f"[📞 Cuộc gọi thoại{dur}]"
    if m.get("videos"):
        return "18", "[Video]"
    if m.get("photos"):
        return "2", "[Ảnh]"
    if m.get("audio_files"):
        return "3", "[🎙️ Tin nhắn thoại]"
    if m.get("gifs"):
        return "7", "[Sticker GIF]"
    sh = m.get("shares") or []
    if sh and isinstance(sh[0], dict):
        link = sh[0].get("link") or sh[0].get("share_text") or ""
        return "6", f"[Link] {fix_ig(link)}".strip()
    return "1", ""


def _ig_rows_for_conv(conv_dir, base_dir, uid):
    """Yield deduped, time-sorted rows in fold_snapshot's row contract
    (ts/type/text/sender/msgId/cliMsgId/raw). msgId is a deterministic hash of
    (thread, ts, sender, type, text) so re-imports match exactly by id — no
    fuzzy matching, no duplicates."""
    rows = {}
    title = None
    for fp in sorted(glob.glob(os.path.join(conv_dir, "message_*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if title is None:
            title = fix_ig(data.get("title") or os.path.basename(conv_dir))
        for m in data.get("messages") or []:
            ts = int(m.get("timestamp_ms") or 0)
            if not ts:
                continue
            sender = fix_ig(m.get("sender_name") or "") or "?"
            mt, pretty = _kind_of_ig(m)
            text = _content_text(m)
            if mt != "1" and not text:
                text = pretty
            mid = "ig" + hashlib.sha1(
                f"{uid}|{ts}|{sender}|{mt}|{text}".encode("utf-8")).hexdigest()[:16]
            if mid in rows:
                continue  # identical retry inside DYI
            rows[mid] = {
                "ts": ts,
                "msgId": mid,
                "cliMsgId": "",
                "raw": None,
                "type": mt,
                "time": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S"),
                "sender": sender,
                "text": text or "(tin trống)",
            }
    return title, sorted(rows.values(), key=lambda r: r["ts"])


# --------------------------------------------------------------------------
def import_folder(dyi_dir, out_dir, progress=None):
    """Convert one extracted DYI folder. Returns (n_convs, [master names]).

    Creates/updates master_<name>_<uid>.json + .md via fold_snapshot (so a
    second DYI from a later date folds into the same master) and builds a
    media_master_<name>_<uid>/ with photos/ + videos/ + index.json (same shape
    the Zalo media job writes, so the viewer serves and matches them).
    """
    dyi_dir = os.path.abspath(dyi_dir)
    out_dir = os.path.abspath(out_dir or os.path.join(os.path.dirname(os.path.abspath(__file__)), "exports"))
    os.makedirs(out_dir, exist_ok=True)
    inbox = None
    for cand in (os.path.join(dyi_dir, "messages", "inbox"),
                 os.path.join(dyi_dir, "inbox")):
        if os.path.isdir(cand):
            inbox = cand
            break
    if not inbox:
        for p in glob.glob(os.path.join(dyi_dir, "**", "inbox"), recursive=True):
            if os.path.isdir(p):
                inbox = p
                break
    if not inbox:
        raise FileNotFoundError(
            "Không tìm thấy messages/inbox trong thư mục DYI — hãy giải nén trọn bộ ZIP Instagram (định dạng JSON).")

    convs = sorted(d for d in glob.glob(os.path.join(inbox, "*")) if os.path.isdir(d))
    done = 0
    names = []
    for conv_dir in convs:
        if progress:
            progress(done, len(convs), os.path.basename(conv_dir))
        uid = _thread_id_from_folder(os.path.basename(conv_dir))
        title, rows = _ig_rows_for_conv(conv_dir, dyi_dir, uid)
        if not rows:
            continue
        name = title or os.path.basename(conv_dir)
        master = None
        mp = ZMERGE.master_path_for(out_dir, name, uid)
        if os.path.exists(mp):
            with open(mp, encoding="utf-8") as f:
                master = json.load(f)
        if master is None:
            master = ZMERGE.empty_master(name, uid)
        ZMERGE.fold_snapshot(master, rows, crawled_at=ZMERGE._now_str(), complete=True)
        ZMERGE.save_master(master, out_dir)
        _build_media_master(conv_dir, dyi_dir, out_dir, name, uid, rows)
        names.append(name)
        done += 1
        if progress:
            progress(done, len(convs), name)
    return done, names


def _sha1(b):
    return hashlib.sha1(b).hexdigest()


def _build_media_master(conv_dir, dyi_dir, out_dir, name, uid, rows):
    """Copy photo/video files from the DYI into media_master_<name>_<uid>/.

    Files are renamed photos/<ts>_<i>.<ext> / videos/<ts>_<i>.<ext> (the send
    time is also stamped into JPEG EXIF and the video's mtime so Google Photos
    shows the right day), deduped by content hash (alsoAs), and indexed in
    index.json exactly like the Zalo media job's format.
    """
    md = os.path.join(out_dir, f"media_master_{name}_{uid}")
    if not os.path.isdir(md):
        os.makedirs(os.path.join(md, "photos"), exist_ok=True)
        os.makedirs(os.path.join(md, "videos"), exist_ok=True)
    idx_path = os.path.join(md, "index.json")
    idx = {"files": {}}
    if os.path.isfile(idx_path):
        try:
            idx = json.load(open(idx_path, encoding="utf-8"))
        except Exception:
            idx = {"files": {}}
    idx.setdefault("files", {})

    def stamp(path, is_video, dt):
        try:
            if is_video:
                os.utime(path, (dt.timestamp(), dt.timestamp()))
                return
            import piexif
            exif_dict = {"Exif": {piexif.ExifIFD.DateTimeOriginal:
                                  dt.strftime("%Y:%m:%d %H:%M:%S").encode()}}
            piexif.insert(piexif.dump(exif_dict), path)
        except Exception:
            pass

    # collect (ts, kind, uri) from raw message jsons (rows no longer carry uris)
    items = []
    for fp in sorted(glob.glob(os.path.join(conv_dir, "message_*.json"))):
        try:
            data = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        for m in data.get("messages") or []:
            ts = int(m.get("timestamp_ms") or 0)
            for kind, key in (("photos", "photos"), ("videos", "videos")):
                for med in m.get(key) or []:
                    uri = med.get("uri") if isinstance(med, dict) else None
                    if uri:
                        items.append((ts, kind, uri))
    n_new = 0
    for ts, kind, uri in items:
        src = os.path.join(dyi_dir, uri.replace("/", os.sep)) if not os.path.isabs(uri) else uri
        if not os.path.isfile(src):
            continue  # media missing from this DYI (geoblocked / pruned) — skip
        with open(src, "rb") as f:
            blob = f.read()
        sha = _sha1(blob)
        if sha in idx["files"]:
            continue  # already imported from an earlier DYI
        dt = datetime.fromtimestamp(ts / 1000) if ts else datetime.now()
        sub = "videos" if kind == "videos" else "photos"
        ext = os.path.splitext(src)[1].lower() or (".mp4" if kind == "videos" else ".jpg")
        i = 0
        while True:
            rel = f"{sub}/{dt.strftime('%Y%m%d_%H%M%S')}_{i}{ext}"
            dst = os.path.join(md, rel)
            if not os.path.exists(dst):
                break
            i += 1
        with open(dst, "wb") as f:
            f.write(blob)
        stamp(dst, kind == "videos", dt)
        idx["files"][sha] = {"file": rel, "bytes": len(blob),
                             "sentAt": [dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second],
                             "alsoAs": []}
        n_new += 1
    if n_new or not os.path.isfile(idx_path):
        tmp = idx_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False, indent=1)
        os.replace(tmp, idx_path)
    return n_new


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else ""
    out = sys.argv[2] if len(sys.argv) > 2 else None
    if not src or not os.path.isdir(src):
        print("Usage: python ig_import.py <extracted-DYI-folder> [out_dir]")
        sys.exit(1)
    n, names = import_folder(src, out, progress=lambda d, t, c: print(f"[{d}/{t}] {c}", flush=True))
    print(f"Imported {n} conversations: {', '.join(names)}")
