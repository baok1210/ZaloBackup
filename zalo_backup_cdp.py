#!/usr/bin/env python3
"""Zalo message history backup via Chrome DevTools Protocol.

Usage:
  python zalo_backup_cdp.py probe           # fetch one batch, print structure
  python zalo_backup_cdp.py full [N]        # paginate until exhausted (N = max batches, optional)
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://localhost:8315"
TARGET_UID = ""  # <- dán UID hội thoại cần backup vào đây (xem tab viewer / export)
PAGE_COUNT = 50
OUT_FILE = Path(__file__).resolve().parent / f"messages_{TARGET_UID}.json"
RAW_DIR = Path(r"H:\zalo-backup\raw_batches")


def find_page() -> str:
    with urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=10) as r:
        pages = json.load(r)
    for p in pages:
        if p.get("type") == "page" and "index.html" in p.get("url", ""):
            return p["webSocketDebuggerUrl"]
    raise RuntimeError(f"Zalo main page not found. Pages: {[(p.get('type'), p.get('url','')[:80]) for p in pages]}")


class Cdp:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=120, origin="http://localhost:8315")
        self._id = 0

    def evaluate(self, expr: str, timeout: int = 90):
        self._id += 1
        req_id = self._id
        self.ws.send(json.dumps({
            "id": req_id,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "awaitPromise": True,
                "returnByValue": True,
                "timeout": timeout * 1000,
            },
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                res = msg.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError(f"Page exception: {json.dumps(res['exceptionDetails'])[:2000]}")
                return res.get("result", {}).get("value")
        raise TimeoutError("CDP evaluate timed out")


# One JS call = fetch a batch AND decrypt it inside the page (Zalo's own AES).
JS_FETCH = """
(async () => {{
  const fBUP = window.webpackJsonp.push([[Math.random()],{{}},[["fBUP"]]]).default;
  const z0WU = window.webpackJsonp.push([[Math.random()],{{}},[["z0WU"]]]).default;
  const uid  = {uid};
  const cnt  = {count};
  const off  = {offset};
  let res;
  try {{
    res = await fBUP.getHistoryMessage(uid, cnt, off);
  }} catch (e) {{
    return JSON.stringify({{ __err: 'call: ' + String(e) }});
  }}
  const out = {{ __err: null }};
  out.rawKeys = res && typeof res === 'object' ? Object.keys(res) : ['<' + typeof res + '>'];
  if (res && res.data) {{
    out.dataKeys = Object.keys(res.data);
    if (typeof res.data.data === 'string') {{
      try {{ out.dec = await z0WU.decodeAES(res.data.data); }} catch (e) {{ out.decErr = String(e); }}
    }}
    if (res.data.msgs) out.msgsCount = res.data.msgs.length;
  }}
  return JSON.stringify(out);
}})()
"""


def fetch_batch(cdp: Cdp, offset_id=None, count=PAGE_COUNT):
    off = json.dumps(offset_id) if offset_id is not None else "undefined"
    expr = JS_FETCH.format(uid=json.dumps(TARGET_UID), count=count, offset=off)
    raw = cdp.evaluate(expr)
    return json.loads(raw)


def probe():
    url = find_page()
    print(f"[*] Connecting: {url}")
    cdp = Cdp(url)
    print("[*] Fetching probe batch (50 messages)...")
    batch = fetch_batch(cdp)
    if batch.get("__err"):
        print(f"[!] Call error: {batch['__err']}")
        return
    print(f"    rawKeys   = {batch.get('rawKeys')}")
    print(f"    dataKeys  = {batch.get('dataKeys')}")
    print(f"    msgsCount = {batch.get('msgsCount')}")
    if batch.get("decErr"):
        print(f"[!] decrypt error: {batch['decErr']}")
    dec = batch.get("dec")
    if dec:
        print(f"    decrypted length = {len(dec)}")
        try:
            obj = json.loads(dec)
            if isinstance(obj, dict):
                print(f"    decrypted top keys = {list(obj.keys())}")
                for k, v in obj.items():
                    if isinstance(v, list):
                        print(f"      '{k}': list[{len(v)}]")
                        if v:
                            print(f"        first item keys: {list(v[0].keys()) if isinstance(v[0], dict) else v[0]}")
                            print(f"        first item sample: {json.dumps(v[0], ensure_ascii=False)[:1500]}")
                            print(f"        last item sample:  {json.dumps(v[-1], ensure_ascii=False)[:600]}")
                    else:
                        print(f"      '{k}': {json.dumps(v, ensure_ascii=False)[:300]}")
            else:
                print(f"    decrypted is {type(obj).__name__}, preview: {str(dec)[:800]}")
        except json.JSONDecodeError:
            print(f"    decrypted NOT json. preview: {dec[:800]}")
    RAW_DIR.mkdir(exist_ok=True)
    (RAW_DIR / "probe.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[*] Full probe output saved to {RAW_DIR / 'probe.json'}")


def full(max_batches=None):
    url = find_page()
    print(f"[*] Connecting: {url}")
    cdp = Cdp(url)
    RAW_DIR.mkdir(exist_ok=True)
    all_msgs = {}          # msgId -> message object (dedup)
    raw_blobs = []         # keep every decrypted batch for forensics
    offset_id = None
    batch_no = 0
    last_sizes = []
    t0 = time.time()

    while True:
        if max_batches and batch_no >= max_batches:
            print(f"[*] Stopping: reached max batch limit {max_batches}")
            break
        batch = None
        for attempt in range(4):
            try:
                batch = fetch_batch(cdp, offset_id)
                break
            except Exception as e:
                wait = 2 ** attempt
                print(f"[!] batch {batch_no} attempt {attempt+1} failed: {e} — retry in {wait}s")
                time.sleep(wait)
        if batch is None:
            print("[!] giving up after retries")
            break
        if batch.get("__err"):
            print(f"[!] API error at offset {offset_id}: {batch['__err']}")
            break

        dec = batch.get("dec")
        if not dec:
            print(f"[!] no decrypted payload at offset {offset_id}. batch keys: {batch}")
            break
        try:
            payload = json.loads(dec)
        except json.JSONDecodeError:
            print("[!] decrypted payload is not JSON; dump and stop")
            (RAW_DIR / f"badjson_batch{batch_no}.txt").write_text(dec[:100000], encoding="utf-8")
            break

        msgs = payload.get("msgs") if isinstance(payload, dict) else None
        if msgs is None and isinstance(payload, list):
            msgs = payload
        n_new = 0
        for m in msgs or []:
            mid = m.get("msgId") or m.get("cliMsgId")
            if mid and mid not in all_msgs:
                all_msgs[mid] = m
                n_new += 1

        raw_blobs.append(payload)
        (RAW_DIR / f"batch_{batch_no:05d}.json").write_text(dec, encoding="utf-8")

        # pick next offset: oldest message id of this batch
        if not msgs:
            print(f"[*] batch {batch_no}: empty batch — done.")
            break
        oldest = msgs[-1] if isinstance(msgs[-1], dict) else None
        next_off = (oldest or {}).get("msgId")
        sizes = len(msgs)
        last_sizes.append(sizes)
        total_seen = len(all_msgs)
        print(f"[+] batch {batch_no}: got {sizes} msgs, {n_new} new, total {total_seen}, "
              f"next offset {str(next_off)[:40]} ({time.time()-t0:.0f}s)")

        if sizes < PAGE_COUNT:
            print("[*] short batch (<50) — considered last page.")
            break
        if not next_off or next_off == offset_id:
            print("[*] no progress in offset — stopping to avoid loop.")
            break
        offset_id = next_off
        batch_no += 1
        time.sleep(0.4)  # be gentle with the API

        # checkpoint every 50 batches
        if batch_no % 50 == 0:
            save(all_msgs, suffix=".partial")
            print("    [checkpoint saved]")

    save(all_msgs)
    (RAW_DIR.parent / f"raw_batches_{TARGET_UID}.json").write_text(
        json.dumps(raw_blobs, ensure_ascii=False), encoding="utf-8")
    print(f"[*] DONE. total unique messages: {len(all_msgs)} in {time.time()-t0:.0f}s")


def save(all_msgs: dict, suffix: str = ""):
    msgs_sorted = sorted(
        all_msgs.values(),
        key=lambda m: (str(m.get("serverTime") or m.get("sendDttm") or m.get("cliMsgId") or "")),
    )
    out = {
        "uid": TARGET_UID,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(msgs_sorted),
        "messages": msgs_sorted,
    }
    OUT_FILE.with_suffix(suffix + ".json" if suffix else ".json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    if not suffix:
        first = msgs_sorted[0] if msgs_sorted else {}
        last = msgs_sorted[-1] if msgs_sorted else {}
        def ts(m):
            for k in ("serverTime", "sendDttm", "localDttm"):
                if m.get(k):
                    return m[k]
            return "?"
        print(f"[*] Saved {len(msgs_sorted)} messages -> {OUT_FILE}")
        print(f"    first: {ts(first)} | last: {ts(last)}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if mode == "probe":
        probe()
    elif mode == "full":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        full(lim)
    else:
        print(__doc__)
