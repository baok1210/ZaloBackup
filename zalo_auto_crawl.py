#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Zalo Backup — daily auto-crawl runner (for Windows Task Scheduler).

Flow (each run):
  1. Ensure the WebUI server is up on :8320 (start ZaloBackup.exe if not).
  2. Ensure Zalo PC runs with the debug port (POST /api/restart if CDP is down).
  3. POST /api/exportall + /api/merge, wait for both jobs to finish.
  4. Append a log line to logs/auto_crawl.log and exit 0/1 for Task Scheduler.

Zalo PC must be logged in to the account you want backed up; the login
persists across restarts, so an unattended run just works.

Usage:
  python zalo_auto_crawl.py             # exportall(md) + merge
  ZaloAutoCrawl.exe                     # frozen build, same behavior
"""
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = sys.stdout

if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
elif __name__ == "__main__":
    HERE = os.path.dirname(os.path.abspath(__file__))

BASE = "http://127.0.0.1:8320"
EXE = os.path.join(HERE, "ZaloBackup.exe")
LOG_DIR = os.path.join(HERE, "logs")
LOG_FILE = os.path.join(LOG_DIR, "auto_crawl.log")

JOB_TIMEOUT = 6 * 3600          # hard cap for exportall+merge
CDP_WAIT = 120                  # seconds to wait for Zalo CDP after restart
SERVER_WAIT = 180               # seconds to wait for the WebUI server to answer
                                # (first boot of a fresh exe can be slow: AV scan)


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def http(url, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _status_ok():
    try:
        st = http(BASE + "/api/status", timeout=15)  # first CDP call can be slow
        return bool(st.get("cdp"))
    except Exception:
        return False


def ensure_server():
    """Make sure the WebUI answers on :8320. Returns True when reachable."""
    try:
        urllib.request.urlopen(BASE + "/", timeout=5).read()  # cheap: no CDP
        return True
    except Exception:
        pass
    log("server not running — starting %s" % EXE)
    if not os.path.isfile(EXE):
        log("FATAL: %s not found" % EXE)
        return False
    env = dict(os.environ, ZALO_NO_BROWSER="1")  # never open a browser at 7am
    subprocess.Popen([EXE], creationflags=0x00000008,  # DETACHED_PROCESS
                     cwd=HERE, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     stdin=subprocess.DEVNULL)
    for _ in range(SERVER_WAIT // 3):
        time.sleep(3)
        try:
            urllib.request.urlopen(BASE + "/", timeout=5).read()
            return True
        except Exception:
            continue
    log("FATAL: server did not come up within %ds" % SERVER_WAIT)
    return False


def ensure_zalo_debug():
    """CDP reachable? If not, ask the server to restart Zalo in debug mode."""
    if _status_ok():
        return True
    log("CDP down — restarting Zalo in debug mode (login persists)…")
    try:
        http(BASE + "/api/restart", payload={}, timeout=60)
    except Exception as e:
        log("restart request failed: %s" % e)
    for _ in range(CDP_WAIT // 5):
        time.sleep(5)
        if _status_ok():
            log("Zalo debug mode is up")
            return True
    log("FATAL: Zalo CDP not reachable after %ds (is Zalo logged in?)" % CDP_WAIT)
    return False


def start_job(path, payload=None):
    j = http(BASE + path, payload=payload or {}, timeout=30)
    jid = j.get("job")
    if not jid:
        raise RuntimeError("no job id from %s: %s" % (path, j))
    log("started %s -> job %s" % (path, jid))
    return jid


def wait_job(jid, label):
    t0 = time.time()
    last = ""
    while time.time() - t0 < JOB_TIMEOUT:
        time.sleep(5)
        try:
            j = http(BASE + "/api/progress?job=" + jid, timeout=15)
        except Exception as e:
            log("%s: progress poll failed: %s" % (label, e))
            continue
        st = j.get("status")
        cur = j.get("current") or ""
        line = "%s: %s %s/%s %s" % (label, st, j.get("done", 0), j.get("total", 0), cur)
        if line != last:
            last = line
            log(line)
        if st == "done":
            log("%s: DONE — %s/%s ok=%s fail=%s" %
                (label, j.get("done", 0), j.get("total", 0),
                 j.get("okCount", ""), j.get("failCount", "")))
            return True
        if st == "error":
            log("%s: ERROR — %s" % (label, j.get("error")))
            return False
    log("%s: TIMEOUT after %ds" % (label, JOB_TIMEOUT))
    return False


def main():
    log("=== auto-crawl run start ===")
    ok = True
    if not ensure_server():
        return 1
    if not ensure_zalo_debug():
        return 1
    # merge first? No — export first so the freshest data is on disk, then fold.
    try:
        j1 = start_job("/api/exportall", {"fmt": "md"})
    except Exception as e:
        log("FATAL: cannot start exportall: %s" % e)
        return 1
    ok = wait_job(j1, "exportall") and ok
    try:
        j2 = start_job("/api/merge")
    except Exception as e:
        log("WARN: cannot start merge: %s" % e)
        j2 = None
    if j2:
        ok = wait_job(j2, "merge") and ok
    log("=== auto-crawl run %s ===" % ("OK" if ok else "WITH ERRORS"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
