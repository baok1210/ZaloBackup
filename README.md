# Zalo Backup — full-conversation & media backup for Zalo PC (local, offline, no server)

A local backup tool that reads Zalo PC's own conversation store directly via the app's
Chrome DevTools Protocol (CDP), then lets you **browse, export, deduplicate, and view** everything
offline — no Zalo server, no cloud, no plaintext credentials ever leave your machine.

**What it does (one picture):**
1. Talks to your running Zalo PC instance over CDP (`localhost:8315`) and pulls every message
   in a conversation straight from the store API (`XS0u`), paginating by keyset.
2. Exports messages in 4 formats (JSON / TXT / CSV / Markdown), with a clean **friendly** layout
   (`time / sender / type / text`) and a raw full-fields option for forensics.
3. Downloads all photos & videos in parallel, converts JPEG-XL→JPEG (so Google Photos can read them),
   and stamps the actual send date into EXIF (photos) / the mp4 header + file mtime (videos) so
   Google Photos shows them on the right day.
4. **Merges duplicate self-chat exports from multiple accounts** into one deduplicated master file,
   using content + time-window matching (Zalo gives each account a *different* msgId for the same
   message, so msgId-based dedup doesn't work across accounts — this engine handles it).
5. Supports a **snapshot→master** workflow: crawl on the 1st, crawl again on the 6th, the later
   crawl folds into one accumulating `master_<name>_<uid>.json` (new messages added, old ones updated,
   recalled messages flagged but preserved forever).
6. **Media master** (`media_master_<name>_<uid>/`) deduplicates downloaded photos/videos across crawls
   by SHA-256 content hash — same image under different CDN names is only stored once.
7. A **built-in Master Viewer** page (`/viewer`) lets you browse every master, filter by date range,
   search by text, show only recalled messages, and preview the media inline — works fully offline
   from the exports folder, Zalo not required.

## Quickstart

**Prerequisites**
- Zalo PC installed and logged into the account whose conversations you want to back up.
  (The tool reads Zalo's own local store; it only has access to what that Zalo session holds.)
- Python 3.14+ (or just use the prebuilt EXE below — no Python needed).

**One-click (no Python, no install)**

1. Download `ZaloBackup.exe` (or build it — see "Build the EXE").
2. Double-click `ZaloBackup.exe`. Your browser opens `http://localhost:8320` automatically.
   If port 8320 is already taken by a running instance, the new one just opens that and exits.
3. If Zalo isn't in debug mode yet, click **Restart Zalo (debug mode)** in the UI.
   That restarts Zalo with `--remote-debugging-port=8315` so the tool can talk to it.
4. Pick a conversation, choose a format (JSON / TXT / CSV / Markdown), hit **Xuất**, and download.
5. Repeat for other conversations. When you're done, hit **🔗 Gộp bản trùng lặp** to merge any
   conversations that were exported from more than one account into one master file.

**From source (developers)**

```bash
git clone https://github.com/baok1210/ZaloBackup.git
cd ZaloBackup
pip install websocket-client piexif pillow pillow-jxl-plugin
python zalo_backup_webui.py            # starts on http://localhost:8320
```

Then open `http://localhost:8320` in your browser.

**Build the EXE (one command)**

```bash
pip install pyinstaller websocket-client piexif pillow pillow-jxl-plugin
python -m PyInstaller --noconfirm --onefile --console --name ZaloBackup \
       --hidden-import websocket --hidden-import zalo_merge zalo_backup_webui.py
```

The EXE appears in `dist/ZaloBackup.exe`. Copy it next to an `exports/` folder if you want.
On Windows, first-run SmartScreen may ask "More info → Run anyway" — that's normal for a
self-built binary with no code-signing certificate.

## How it works (short)

Zalo PC stores conversations locally in an encrypted (SQLCipher) database. The app itself is a
Chromium-based Electron app, so it exposes Chrome DevTools Protocol on a local port.
`zalo_backup_webui.py` attaches to that page, finds the Zalo store module, and calls its own
API to enumerate conversations and paginate `Core.Message` records by `(userId, sendDttm, msgId)`.
Exports are pure-read — no Zalo DB is opened or modified directly by the tool.

`zalo_merge.py` is the dedup engine:
- Same conversation from two different Zalo accounts → merge by (type, time window, content),
  because msgId differs per account.
- Same conversation, different crawl dates → fold into one master JSON that accumulates over time,
  tracking `firstSeen` / `lastSeen` / `missingSince` per message, so you can see what was recalled.
- Downloaded media ZIPs → content-hash-dedup into a per-conversation `media_master_*` folder.

The Viewer reads the exported JSON/Media folder directly — no CDP, no Zalo running needed.

## Features

### Conversation export
- All conversations attached to the logged-in Zalo PC session, listed with last-message time.
- Search conversations by name or UID.
- **JSON** — friendly, readable, includes msgId (for future dedup/crawls), firstSeen/lastSeen.
- **TXT** — chat-style, one line per message.
- **CSV** — opens in Excel.
- **Markdown** — pretty, grouped by day.
- Raw full-fields JSON mode for forensic use.

### Media export
- One-click download of every photo/video in a conversation (parallel).
- JPEG-XL → JPEG conversion (Google Photos cannot ingest `.jxl`).
- Send date stamped into files: EXIF `DateTimeOriginal` for photos, mp4 `mvhd` date + mtime for videos.
- Packaged into a dated ZIP, same folder as text exports.

### Merge / dedup
- Merges self-chat duplicates from multiple accounts into one master JSON + Markdown.
- Smart matching: text within 2s + identical content; media within 10s; never merges two lines
  from the same source file (each export is already unique by msgId).
- Handles edge cases safely: photo albums (rapid images), repeated short texts ("ok/ok"), small
  clock offsets between the two views. Worst case is keeping an extra line, never losing one.

### Snapshot → master (multi-crawl)
- Each crawl folds into one accumulating master per conversation, never replaces it.
- Tracks `firstSeen` / `lastSeen`; flags `missingSince` for messages that disappear (likely recalled),
  but keeps their content forever.
- Re-crawls are incremental: only new/changed lines do work.
- Idempotent: pressing merge multiple times does not duplicate.

### Media master
- One folder per conversation: `media_master_<name>_<uid>/`, with `index.json` + `photos/` + `videos/`.
- Deduped by SHA-256 of the *converted* file (so the same photo under different CDN URLs is stored once).
- `alsoAs` tracks every CDN name an image was seen under.
- Empty/half-written ZIPs are ignored.

### Master Viewer (`/viewer`)
- Lists every `master_*.json` from the exports folder.
- Messages: grouped by day, date-range presets + custom from/to, text search, "only recalled" filter.
- Per-message tooltip shows msgId / firstSeen / lastSeen for forensics.
- Non-text messages are prettified (photo dims + CDN host, file/link title instead of raw JSON).
- Media tab: inline preview of photos and **playable videos**, with the stamped send date in the caption.
- Fully offline — reads the exports folder. Zalo does not need to be running.

### Zalo-style chat mode + screenshot export
- Tab **💬 Dạng Zalo** re-renders the conversation like the real Zalo chat window: green "me" bubbles
  on the right, white bubbles with avatar initial on the left, day separators (Hôm nay / Hôm qua / dd/mm/yyyy),
  a blue header bar with the conversation name and a "who is me" selector (remembered per master).
- Photos that were saved into the media master are shown **inline inside the bubble** (mapped by send
  time, ±2 s tolerance, album-safe); click to zoom. Dead-CDN photos fall back to a readable caption.
- Tab **📷 Chụp màn hình (PNG)** renders the current view (Zalo style included) into a single PNG via
  the bundled html2canvas 1.4.1 (vendored, MIT) — works fully offline; the download carries the
  conversation name and timestamp in the filename. Ctrl+P / print gives a clean white PDF of the chat.
- Recalled messages keep the red strikethrough treatment in Zalo mode.
- **Videos play inline** in Zalo mode and the media grid (mp4 from the media master, HTTP Range
  streaming); messages whose CDN link has expired render as a readable one-liner instead of raw
  JSON: `[Video 2s · 417 KB (album 10/12)]`, `[Ảnh 896×2030]`. Photo/video payloads are prettified
  the same way in master `.md` files, friendly JSON/CSV/TXT exports, and the viewer.
- **Media stats bar** (top of the master view) audits every photo/video message and shows how much
  is 💾 saved locally, 🔗 still downloadable (live CDN link), 💀 lost forever (no local file + dead
  link), and ❔ not yet checked. Link probing runs in a background thread (~1,600 links/min, 12
  parallel), results are cached next to the master JSON (`master_*.json.mediastats`), and the bar
  updates live while the audit runs. Zalo PC does not need to be open — the probe goes straight to
  the CDN URLs stored in the master.

### Daily auto-crawl (Windows Task Scheduler)

One-time setup (PowerShell, in the project folder):

```powershell
powershell -ExecutionPolicy Bypass -File register_task.ps1   # daily 07:30
```

Every morning the machine runs `ZaloAutoCrawl.exe` (invisible, via `run_auto_crawl.vbs`), which:

1. Starts the WebUI server if it is not running (no browser window opens).
2. Restarts Zalo PC in debug mode if CDP is down (login persists, no interaction needed).
3. Runs **Backup ALL** (text + media, skipping what was exported in the last 24 h) and then
   **Merge** (fold snapshots into the masters).

Results append to `logs/auto_crawl.log`; Task Scheduler shows the last-run result.
Catch-up: if the PC was off at 07:30, the run happens as soon as it boots. Overlapping runs are
skipped, never doubled. Remove the schedule any time with
`powershell -ExecutionPolicy Bypass -File unregister_task.ps1`.

### Account model awareness
- The tool understands that Zalo gives each account a *different* msgId for the same message,
  and that a self-chat exists twice (once from each side), each with its own UID.
- Cross-account merges use content+time, not msgId, so they work correctly.

## Folder layout (what you get)

```
H:\zalo-backup\
├── zalo_backup_webui.py      # main WebUI (exports + viewer)
├── zalo_merge.py             # merge/dedupe engine (snapshot→master, media master)
├── ZALO_DECRYPTION_KNOWLEDGE.md  # how the store/CDP/crypto/account model actually works
├── exports\                  # all exported data lives here
│   ├── master_Bảo_2559848092105805671.json
│   ├── master_Bảo_2559848092105805671.md
│   ├── media_master_Bảo_2559848092105805671\
│   │   ├── index.json
│   │   ├── photos\
│   │   └── videos\
│   ├── Bảo_2559848092105805671.json   (per-crawl friendly export)
│   └── ...other conversations...
└── ZaloBackup.exe            (optional, built by you — not committed)
```

## Workflow

**First time, single account**
1. Open Zalo and log in.
2. Run the tool. Let it restart Zalo in debug mode if needed.
3. Export the conversations you care about (or use **Backup TẤT CẢ** to do all of them in one go,
   skipping anything already backed up in the last 24h).
4. For conversations with photos/videos: hit **Tải tất cả ảnh & video** *while the links are still alive*
   (Zalo CDN links expire in roughly 1–2 years; old links return 404 forever).

**Two accounts, same self-chat**
1. Log in account A in Zalo, run the tool, export the self-chat.
2. Log in account B in Zalo, restart debug mode, export the same self-chat.
3. Hit **🔗 Gộp bản trùng lặp**. The tool finds both exports for "Bảo", deduplicates, and writes
   `master_Bảo_<uid>.json` + `.md`.

**Ongoing, multi-crawl**
1. Each time you want an up-to-date backup, run the tool, hit **Backup TẤT CẤNG**, then **🔗 Gộp bản trùng lặp**.
2. The master grows: new messages get `firstSeen` today, old ones just update `lastSeen`,
   recalled ones get `missingSince` flagged. Next crawl is fast.

## Notice on what Zalo stores locally

This tool backs up exactly what Zalo PC has synced/stored locally for the session you're logged into.
A conversation segment that was never synced to any PC on any account isn't available here —
only what the local store holds. For the most complete copy, export from whichever account holds the
most complete local sync, and merge across accounts when available. Old media links expire from Zalo's
CDN, so photos/videos should be downloaded while fresh.

## Troubleshooting

- **"Zalo not detected" / CDP not ready**: Zalo isn't running in debug mode. Click **Restart Zalo
  (debug mode)** in the UI. If that fails, close Zalo and run:
  `Zalo.exe --remote-debugging-port=8315 --remote-allow-origins=*`
- **Port 8320 busy**: the tool auto-picks the next free port, or if a *healthy instance of itself*
  is already on 8320, it just opens that page and exits — no error.
- **Media download shows lots of failures**: that's Zalo's CDN link expiry, not a bug. Old links are
  gone for good; download media from conversations you care about sooner rather than later.
- **Export looks empty or wrong**: make sure Zalo is logged into the account whose conversations you
  expect. The store only has what that session synced.
- ** Vietnamese filenames download broken**: the tool sends `Content-Disposition` with both an ASCII
  fallback and a UTF-8 `filename*` per RFC 5987, so browsers save the real Vietnamese name.

## Self-contained, local-only

Everything runs on your machine. The WebUI is a plain Python HTTP server; the only network call outside
localhost is downloading images/videos from Zalo's CDN URLs that are already embedded in your stored
messages. No API keys, no account password, no cloud — CDP talks to your own Zalo process.

## License

This project is provided as-is for personal local backup of your own Zalo conversations.
It reads Zalo's own local store via the app's own CDP interface; it does not connect to Zalo's servers
directly (only to download media URLs that Zalo already gives your local store).

Use responsibly and only on conversations you have the right to back up.

— Built around Zalo PC v26.8.20 on Windows, Python 3.14.
