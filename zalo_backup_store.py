#!/usr/bin/env python3
"""Zalo full-conversation backup via the app's own local store API (CDP).

Works ONLY while the Zalo PC session whose DBs contain the conversation is logged in.
Paginates Core.Message by (userId, sendDttm, msgId) keyset, 500 msgs/batch, and saves
deduped, time-sorted JSON. Falls back to decrypting `msg` fields that arrive encrypted.

Usage:
  python zalo_backup_store.py check     # is the target conversation present?
  python zalo_backup_store.py full [N]  # dump entire history (N = max batches, optional)
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import websocket

CDP_HTTP = "http://localhost:8315"
TARGET_UID = "2559848092105805671"
OUT_FILE = Path(r"H:\zalo-backup") / f"messages_{TARGET_UID}.json"
BATCH = 500


def find_page() -> str:
    with urllib.request.urlopen(CDP_HTTP + "/json/list", timeout=10) as r:
        pages = json.load(r)
    for p in pages:
        if p.get("type") == "page" and "index.html" in p.get("url", ""):
            return p["webSocketDebuggerUrl"]
    raise RuntimeError(f"Zalo main page not found: {[(p.get('type'), p.get('url','')[:80]) for p in pages]}")


class Cdp:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=180, origin="http://localhost:8315")
        self._id = 0

    def evaluate(self, expr: str, timeout: int = 150):
        self._id += 1
        rid = self._id
        self.ws.send(json.dumps({
            "id": rid, "method": "Runtime.evaluate",
            "params": {"expression": expr, "awaitPromise": True, "returnByValue": True,
                       "timeout": timeout * 1000},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(f"CDP error: {msg['error']}")
                res = msg.get("result", {})
                if res.get("exceptionDetails"):
                    raise RuntimeError(f"Page exception: {json.dumps(res['exceptionDetails'])[:1500]}")
                return res.get("result", {}).get("value")
        raise TimeoutError("CDP evaluate timed out")


JS_CHECK = r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  const out = { me: null, uid: null, conv: null, lastBatch: null };
  try { out.uid = store.getUserId(); } catch (e) { out.uid = 'ERR ' + String(e).slice(0,120); }
  try { const me = await store.getMe(); out.me = me && { userId: me.userId, displayName: me.displayName }; }
  catch (e) { out.me = 'ERR ' + String(e).slice(0,120); }
  try {
    const r = await store.getMessageFromConversationByLimit('UID', 5, { count: 5 });
    const items = Array.isArray(r) ? r : ((r && r.items) || []);
    out.conv = items.length ? { count: items.length, sample: JSON.stringify(items[0]).slice(0, 300) } : 'empty';
  } catch (e) { out.conv = 'ERR ' + String(e).slice(0,200); }
  return JSON.stringify(out);
})()
""".replace("UID", TARGET_UID)


def check(cdp: Cdp):
    out = json.loads(cdp.evaluate(JS_CHECK))
    print("me          :", out.get("me"))
    print("store userId:", out.get("uid"))
    print("target conv :", out.get("conv"))
    if isinstance(out.get("conv"), str) and out["conv"] == "empty":
        print("!! Target conversation is NOT in this account's store.")
        print("!! Log into the account whose Core/Message/<UID>.db is 14 MB, then re-run.")


JS_PAGE = r"""
(async () => {
  const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;
  const z0WU = window.webpackJsonp.push([[Math.random()],{},[["z0WU"]]]).default;
  const uid = UID;
  const count = COUNT;
  const before = { sendDttm: SDTTM, msgId: MSGID };
  let items = [];
  try {
    const r = await store.getMessageFromConversationByLimit(uid, count, {
      count: count,
      fromMsg: before.sendDttm || before.msgId ? before : undefined,
      include: false,
    });
    items = Array.isArray(r) ? r : ((r && r.items) || []);
  } catch (e) {
    return JSON.stringify({ __err: String(e).slice(0, 300) });
  }
  // normalize + best-effort decrypt of msg fields that arrive encrypted
  const out = [];
  for (const m of items) {
    const o = {};
    for (const k in m) {
      const v = m[k];
      o[k] = (v && typeof v === 'object') ? JSON.parse(JSON.stringify(v)) : v;
    }
    if (typeof o.msg === 'string' && o.msg.length > 24 && !o.msg.startsWith('{')) {
      try { o.msg = await z0WU.decodeAES(o.msg); } catch (e) { /* keep as-is */ }
    }
    out.push(o);
  }
  return JSON.stringify(out);
})()
"""


def page(cdp: Cdp, sdttm=None, msgid=None):
    expr = (JS_PAGE
            .replace("UID", json.dumps(TARGET_UID))
            .replace("COUNT", str(BATCH))
            .replace("SDTTM", json.dumps(str(sdttm)) if sdttm is not None else "undefined")
            .replace("MSGID", json.dumps(str(msgid)) if msgid is not None else "undefined"))
    return json.loads(cdp.evaluate(expr))


def _key(m):
    return (int(str(m.get("sendDttm") or m.get("serverTime") or 0) or 0),
            int(str(m.get("msgId") or m.get("cliMsgId") or 0) or 0))


def full(max_batches=None):
    cdp = Cdp(find_page())
    msgs = {}          # msgId -> message
    # resume from previous checkpoint if present
    for cand in (OUT_FILE, OUT_FILE.with_suffix(".partial.json")):
        if cand.exists():
            try:
                prev = json.loads(cand.read_text(encoding="utf-8")).get("messages", [])
                for m in prev:
                    mid = str(m.get("msgId") or m.get("cliMsgId") or "")
                    if mid:
                        msgs[mid] = m
                if msgs:
                    print(f"[*] resumed with {len(msgs)} messages from {cand.name}")
                break
            except Exception as e:
                print(f"[!] could not resume from {cand.name}: {e}")
    oldest = min(msgs.values(), key=_key) if msgs else None
    sdttm = oldest.get("sendDttm") or oldest.get("serverTime") if oldest else None
    msgid = oldest.get("msgId") or oldest.get("cliMsgId") if oldest else None
    n = 0
    t0 = time.time()
    while True:
        if max_batches and n >= max_batches:
            print(f"[*] reached batch limit {max_batches}")
            break
        for attempt in range(6):
            try:
                items = page(cdp, sdttm, msgid)
                break
            except Exception as e:
                print(f"[!] batch {n} attempt {attempt+1}: {e}", flush=True)
                time.sleep(2 ** attempt)
                try:
                    cdp = Cdp(find_page())  # fresh connection each retry
                except Exception as ce:
                    print(f"[!] reconnect failed: {ce}", flush=True)
        else:
            print("[!] retries exhausted", flush=True); break
        if isinstance(items, dict) and items.get("__err"):
            print(f"[!] {items['__err']}"); break
        arr = items if isinstance(items, list) else []
        if not arr:
            print(f"[*] batch {n}: empty — done."); break
        new = 0
        for m in arr:
            mid = str(m.get("msgId") or m.get("cliMsgId") or "")
            if mid and mid not in msgs:
                msgs[mid] = m; new += 1
        print(f"[+] batch {n}: {len(arr)} got, {new} new, total {len(msgs)} ({time.time()-t0:.0f}s)", flush=True)
        if len(arr) < BATCH:
            print("[*] short batch — last page.", flush=True); break
        # keyset: oldest item of this batch
        tail = min(arr, key=_key)
        sdttm = tail.get("sendDttm") or tail.get("serverTime") or ""
        msgid = tail.get("msgId") or tail.get("cliMsgId") or ""
        n += 1
        time.sleep(0.3)
        if n % 10 == 0:
            save(msgs, ".partial")
            print("    [checkpoint]", flush=True)
    save(msgs)


def save(msgs: dict, suffix: str = ""):
    def sort_key(m):
        return str(m.get("sendDttm") or m.get("ts") or m.get("serverTime") or "")
    arr = sorted(msgs.values(), key=sort_key)
    payload = {
        "uid": TARGET_UID,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(arr),
        "messages": arr,
    }
    OUT_FILE.with_suffix(suffix + ".json" if suffix else ".json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    if not suffix and arr:
        print(f"[*] saved {len(arr)} messages -> {OUT_FILE}")
        print(f"    oldest: {sort_key(arr[0])}  newest: {sort_key(arr[-1])}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    cdp = Cdp(find_page())
    if mode == "check":
        check(cdp)
    elif mode == "full":
        full(int(sys.argv[2]) if len(sys.argv) > 2 else None)
    else:
        print(__doc__)
