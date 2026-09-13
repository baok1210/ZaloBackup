# Zalo PC Encryption & Backup — Knowledge Base (v2, verified 2026-09-11)

Target: Zalo PC v26.8.20 (Electron 22.3.9, Chromium 108), Windows, machine of user "Admin".
Everything below was **verified live** against a running Zalo PC on 2026-09-11 unless marked
UNVERIFIED.

---

## 0. TL;DR — the method that actually works

**Do NOT use `getHistoryMessage` for personal chats** (it is group-only, see §2).
Back up through the renderer's local store instead:

```js
// 1) resolve a webpack module (works in main page index.html over CDP)
const store = window.webpackJsonp.push([[Math.random()],{},[["XS0u"]]]).default;

// 2) count
await store.countTotalMessageOfConversation(uid);            // e.g. 4028

// 3) page backwards with keyset pagination (500/batch works well)
let items = await store.getMessageFromConversationByLimit(uid, 500, {
  count: 500,
  fromMsg: { sendDttm: "<oldest sendDttm so far>", msgId: "<oldest msgId so far>" },
  include: false,
});
// NOTE: returns the message ARRAY directly (not {items: ...})
// each message arrives FULLY DECRYPTED (plaintext) — no AES work needed

// 4) stop when batch < limit; the keyset for the next call is the oldest
//    (sendDttm, msgId) pair of the batch you just received
```

Sort/dedupe by `msgId`. `sendDttm`/`serverTime` are epoch-ms strings.

---

## 1. Runtime access (CDP)

```
taskkill /F /IM Zalo.exe
& "$env:LOCALAPPDATA\Programs\Zalo\Zalo.exe" --remote-debugging-port=8315 --remote-allow-origins=*
```
- `http://localhost:8315/json/list` → pick the page whose URL contains `index.html`
- `Runtime.evaluate` with `awaitPromise:true, returnByValue:true` over the page WebSocket
- CDP origin header `http://localhost:8315` required by `--remote-allow-origins=*`
- Login state persists across restarts; Zalo auto-logs-in to the last account
- **Multi-account:** several accounts stay remembered on one machine. Each has its own
  storage directory (§3). Switching accounts in the UI switches which storage is live.

### Useful webpack modules (chunk `compact-app-pc.96f2410660e9f7b98dfd.js`)
| Module | Contents |
|---|---|
| `XS0u` | Local message store (DAL facade): conversations, messages, media, friends, E2EE stores |
| `fBUP` | REST API layer (`getMe`, `getFriendsList`, `sendZText`, `getHistoryMessage`, …) |
| `z0WU` | Utils incl. `encodeAES`/`decodeAES` (transport crypto) |
| `Gm1y` | Group info cache |
| `NDmK` | Remote config: `enableFetchHistoryMessage:false`, `enableHistoryMessage:true`, `e2ee`, domains |

### XS0u API highlights (verified)
```js
store.getUserId()                        // current account uid (also store.getMe())
store.getConversations()                 // array of ~1100 convs {userId, displayName, infoCheckSearch.lastMessageTime, ...}
store.getConversation(uid)               // conv meta incl. peer displayName/zaloName/globalId
store.countTotalMessageOfConversation(uid)
store.getMessageFromConversationByLimit(uid, n, {count, fromMsg, include})
store.getAllMessagesFromConversationSingle(uid)   // returns {0:{children:[...]}} day-grouped
store.getMe()                            // profile of current account
```

---

## 2. History APIs — what works and what doesn't

### ❌ `getHistoryMessage(uid, count)` — GROUPS ONLY
- `fBUP.getHistoryMessage` → HTTP GET `https://tt-group-wpa.chat.zalo.me/api/group/history?...&params=<AES({grid,count})>`
- For a **personal** uid the server returns **HTTP 404** ("404 Not Found" HTML body)
- Config flag `enableFetchHistoryMessage` is **false** → app itself doesn't use it
- There is **no** personal-history REST endpoint in the command table (checked all
  `/api/...` entries incl. dynamic `[o.a]` = `/api/subtab/seen`). Personal history is
  local-first; the client never bulk-pulls it from the server.
- Older web revivals (zalo-web revival scripts) used this endpoint when it accepted
  personal ids — that era is gone in PC v26.8.20.

### ✅ Local store path (the working way, see §0)
- Reads the SQLCipher-encrypted per-conversation DBs **through the app itself**
  (key never leaves the app; we just ask the app's own DAL facade)
- Contains everything that has been synced to this machine (see §3 limits)

### Server-side limits (practical)
- Zalo does **not** re-download full history into a fresh client; a newly logged-in
  machine only has what the app has synced (scroll in UI pulls more, within server retention)
- So: back up from the machine/account that has lived with that conversation

---

## 3. Account / storage / UID model (important, was misunderstood before)

- Storage root: `%APPDATA%\ZaloData\Database\_production\<STORAGE_UID>\`
  - `<STORAGE_UID>\Core\Message\<CONV_UID>.db` — one SQLCipher DB per conversation
  - `<STORAGE_UID>\Core\Index.db`, plus `Media.db`, `MsgInfo.db`, `Meta.db`,
    `Reaction.db`, `Res.db`, `Sync.db`, `E2ee.db` (all SQLCipher-encrypted)
  - `%APPDATA%\ZaloData\Database\_production\` shared, **plaintext SQLite**:
    `Qos.db`, `Storage.db`, `SecureLocalstorage.db` (+wal/shm), `Trust.db`
    → profile display names are readable in `Storage.db` table `info-cache`
    (key `0_<storageUid>` → `{"zName": ...}`, `1_<storageUid>` = second account)
- `<STORAGE_UID>` = uid of the **account whose data this is** (its own id in its own space)
- `<CONV_UID>` = id of the peer **as seen from the logged-in account** — Zalo assigns
  different numeric UIDs for the same human depending on which account looks at them.
  Practical mapping discovered on this machine:
  | Human | As themselves (storage dir) | As seen from the other account |
  |---|---|---|
  | "Đoàn Bảo" (+84906217596, username `t_m7e0aa6ifc`, globalId `TOS7N2H5J6MHU9RVF0ORK9J6N74T9JG0`) | `1558295982362900914` | `415420463646174242` |
  | "Bảo Bv Stamford Guangzhou" (same human, 2nd account) | `415420463646174242` | `3739565505259494135` (and `2559848092105805671` in older cache rows) |
- Self-chat ("notebook") conversations therefore exist **twice**, once per account,
  with different conv UIDs. Both sides hold the full thread content.
- In message rows, `fromUid: "0"` means **sent by the account that owns the storage**
  (i.e. "me"). `toUid` is always the conv id. `dName` carries display name of the sender.

Example verified: conv `2559848092105805671` inside storage `415420463646174242`
= the self-chat between these two accounts; 3,892 msgs (2023-07-02 → 2026-09-11),
2,628 from `fromUid:"0"` (sent from Stamford side) + 1,264 from `2559848092105805671`.
The same conv in storage `1558295982362900914` is `3739565505259494135`.

---

## 4. Crypto layers (unchanged facts + verified behavior)

### 4.1 Transport (REST) — AES-CBC
- Key (Base64): `8OF9jccA7NHgjVqvDNbRKg==`, IV = 16 zero bytes, PKCS7
- `z0WU.encodeAES/decodeAES`; requests: `?...&params=` + `encodeURIComponent(encodeAES(JSON.stringify(payload)))`
- REST responses: `{error_code, error_message, data: "<AES string>"}` — decode `data` with `decodeAES`
- Verified live by decoding `getMe` response

### 4.2 Socket layer — AES-GCM
- Session key delivered in AUTHEN frame (`r.key`), AES-GCM with iv||additionalData||
  ciphertext framing (`48`-char base64 head split 16/16/rest). Not needed for backup.

### 4.3 Local DBs — SQLCipher 4 (UNVERIFIED exact key, flow verified)
- All `Core/**` and per-account DBs are SQLCipher-encrypted (no plaintext header,
  sizes are multiples of 4096; salt = first 16 bytes of file)
- Flow: renderer's DB config manager holds `partition.cipherKey` per (db, partition) and
  sends it **with every DAL IPC request**; the sqlite utility process passes it to
  `PRAGMA key` (`open(name, version, rawCipherKey, mode, sessional)` →
  `sqlCipherKey()` checks `database-config.json` progress flags)
- Key origin (from code): `partitionConfig = createPartitionConfig(sessionUserId, dbName,
  scopeKey)` where `scopeKey = getUserScopeKey()` for sessional DBs (= session **UIN**)
  or `getSharedKey()` (= empty string → shared DBs are plaintext, confirmed §3)
- Standard SQLCipher 4 params assumed (default kdf_iter 256000, PBKDF2-HMAC-SHA512,
  4096 pages, 80-byte page reserve). Offline trial with passphrase = storage uid string
  **failed** → the actual passphrase is not the raw uid string (probably the true UIN
  or a derived value). To finish offline decryption: hook the renderer (CDP) at DAL
  message send (`connection:{fullname, cipherKey, ...}` in sync-v2-sub-worker module)
  or read the session's real UIN and retry §4.3 params. UNVERIFIED — do not trust any
  hardcoded key from the asar for DB decryption.

### 4.4 Field-level encryption (inside DBs) — AES-CBC
- Sensitive fields (`msg`, `attach`; also `content`, `parsedInfo`, `localPath` in some
  tables) get an extra AES-CBC layer before storage
- KDF: PBKDF2-SHA256, **1000 iterations**, 32-byte key
- Salt = IV = `[221,179,255,0,79,11,0,70,61,94,221,189,11,233,96,177]`
- Root key: `d9c07b9de9915af401607d3f360211ac` (utf-8)
- Irrelevant when extracting via the store API (§0) — fields arrive decrypted

### 4.5 Other secrets in app.asar (unchanged)
```
passwordCipherKey = "9fc5vEW3"
paramCipherKey    = "3FC4F0D2AB50057BCE0D90D9187A22B1"
zsafeStorageKey   = "afe75eea0d3c4bd9a65439a54eabc988"
zsafe-storage.json: {"zsskv":"2","zsskd":{"zshk":"..."}}  (v2 = Electron safeStorage, runtime-only)
```

---

## 5. Tooling built (all in `H:\zalo-backup`)

| File | What it does | Status |
|---|---|---|
| `zalo_backup_store.py` | CLI backup via store API. `check` (account + conv presence), `full [N]` (keyset pagination, dedupe, resume from partial, auto-reconnect), saves sorted JSON | ✅ used for the 3,892-msg backup |
| `zalo_merge.py` | **Two merge modes.** (1) *Cross-account merge* (`merge_group`, `merged_<name>_<ts>.json`): same chat seen from 2 accounts has different UIDs AND msgIds — matched by (type, Δt, content), source-file guard; union-safe. (2) **Snapshot→master** (`fold_snapshot`, `master_<name>_<uid>.json`) — the auto-crawl data layer: crawl #1 (mùng 1) and crawl #2 (mùng 6) are snapshots; the later is the superset, so folding ACCUMULATES: new msgs appended, known ones get `firstSeen/lastSeen`, a message absent from a same-account full crawl that provably covered its position (a newer msg exists) is flagged `missingSince` (likely recalled) with content kept forever; seen again → un-flagged. msgId exact-match fast path (exports now carry msgId in friendly JSON); fuzzy fallback for legacy exports. Same-crawledAt fold is idempotent. WebUI "🔗 Gộp bản trùng lặp" now builds/updates masters. **Device-copy dedup (2026-09-14):** when a message is sent/synced across devices the store returns it TWICE — a PC copy (`src=10`, plain `msgId`) and a phone-sync copy (`src=7`, `msgId` suffixed `_NNN`) — sharing `cliMsgId` but with DIFFERENT `msgId`, ~50 ms apart; dedup by msgId alone kept both and exports showed every own message duplicated. Fix at 3 layers: `fetch_all` collapses copies per `cliMsgId` (keeps the later PC copy = what Zalo displays, live-verified 105→81 rows, 0 dups left); `fold_snapshot` gets a `cliMsgId` fast path + `_heal_device_copies()` self-heals old masters on every fold (cli groups → cli-less same-text ±2 s join → legacy same-second same-text; later row wins, ids/raw merged onto it); `merge_sources` pre-collapses same-file cli copies before cross-file matching. Friendly JSON exports now carry `cliMsgId`. Verified end-to-end: Blue Roses master 105→88, self-chat master 6,452→6,154 (298 device copies), recall/idempotency/cross-account behaviors preserved. **Sender backfill (2026-09-14):** raw store rows frequently carry NO `dName`, so masters folded from raw exports had ~half the rows with an empty sender (viewer showed '?' as the name) and 2-view merges mixed sender labels (own msgs = 'Tôi' in one view, 'Đoàn Bảo' in the other). `save_master` now runs `_backfill_senders()`: evidence = the row's own dName → a fromUid→name map built from rows that DO have dName → the master account's name for fromUid=0/uid rows; ambiguous uids skipped. One-time healing on the real master: 3,244 senders filled from dName evidence (fromUid 0 ↔ 'Bảo Bv Stamford Guangzhou', 2559848092105805671 ↔ 'Đoàn Bảo'), then cross-view label unification → exactly 2 sender names remain (2,210 + 3,944) | ✅ verified: recall cycle (mark→un-mark), permanent recall kept, legacy fuzzy fold, idempotency; real self-chat 2 views → master deduped 6,154 msgs, 0 false recalls (cross-account views never mark missing) |
| `zalo_backup_webui.py` | Local WebUI on **http://localhost:8320** (localhost-only). Lists all conversations of the logged-in account (searchable), format picker **JSON / TXT / CSV / MD**, live progress, in-browser download, "Restart Zalo (debug mode)" button, **Media export button** (see below), JSON friendly-vs-raw checkbox. Endpoints: `/api/status`, `/api/conversations`, `/api/export` (POST uid+fmt+friendly), `/api/progress?job=`, `/api/download?job=`, `/api/restart`, `/api/media` (POST uid), `/api/media/download?job=` | ✅ verified end-to-end |
| `ZaloBackup.exe` | PyInstaller onefile build of the WebUI (8.7 MB → larger after media deps). Auto-opens browser, picks a free port if 8320 busy, reuses/focuses an already-running instance, exports land in `exports\` next to the exe. New-user friendly: just double-click. SmartScreen "Run anyway" expected (unsigned) | ✅ shipped in `H:\zalo-backup` |
| Media pipeline (inside webui) | Scans conv messages for photo/video CDN URLs → downloads in parallel (10 threads) → **JXL→JPEG conversion** (Google Photos can't read JPEG XL) → date stamps: EXIF DateTimeOriginal for JPEGs, mp4 `mvhd` creation-time patch + file mtime for videos → incremental ZIP | ✅ |
| **Backup-ALL mode** (inside webui) | One click (`/api/exportall`): walks **every** conversation of the logged-in account, per conv = text export (chosen fmt) + media ZIP; skips items already exported in the last 24 h (per-file mtime check, media recognized by `media_<name>_<uid12>_*.zip` prefix); live progress shows `(i/N) <name>` + last 8 summary lines; **cancelable** via `/api/cancel` (shared `CANCEL` event, checked between convs and between download batches). First full run can take hours (media downloads dominate); later runs are minutes (skips) | ✅ verified end-to-end |
| **Media master** (`fold_media_zips`) | Per-conversation folder `media_master_<name>_<uid>\` (photos/ videos/ + `index.json`): folds every media ZIP of that conv across crawls, dedupe by **SHA-256 of content** — the same photo re-downloaded in a later crawl (same or different CDN filename) costs nothing; new content stored under its date-stamped name; per-file `firstSeen/lastSeen/alsoAs` provenance; 0-byte and corrupt ZIPs tolerated; refold-idempotent. Wired into the WebUI merge job (after messages, per conversation) | ✅ verified: real 74 MB ZIP → 16 files/71 MB stored; refold → 0 new/16 dup |
| JSON friendly mode | Default export shape is the raw store row (~80 fields). Friendly mode (default, checkbox to disable) = `{time, sender, msgType, text}` — human-readable | ✅ |
| `zalo_backup_cdp.py` | First-gen probe/extractor around `getHistoryMessage` (kept for group convs / history reference) | ⚠ group-only |
| `zalo_decryption_utils.js/.py` | Field-level crypto helpers (PBKDF2/AES-CBC) | valid, not needed via store API |
| `ZALO_DECRYPTION_KNOWLEDGE.md` | This file | v2 |
| `messages_2559848092105805671.json` | Full backup, 3,892 msgs, all fields | ✅ 2026-09-11 |
| `messages_2559848092105805671_readable.json` | Same, trimmed (time/from/type/text) | ✅ |
| `exports\` | WebUI output dir (e.g. `Bảo_2559848092105805671.md`, 3.1 MB) | ✅ |
| Run doc for preview: `C:\Users\Admin\Downloads\.freebuff\run.md` | how to start the WebUI detached | ✅ |

WebUI usage pattern after switching accounts: log into the account in Zalo PC →
click **Restart Zalo (debug mode)** in the WebUI (or the taskkill/start command in §1)
→ wait for status bar to show the new account → pick conversation → pick format → Export.

---

## 6. Gotchas learned the hard way

- `getMessageFromConversationByLimit` returns a **bare array**; don't expect `{items}`
- Windows console: call `sys.stdout.reconfigure(encoding="utf-8")` in Python before
  printing Vietnamese names, or you get UnicodeEncodeError (cp1252)
- In bash-on-Windows, `powershell -Command '...$env:LOCALAPPDATA...'` — single quotes
  keep `$env:` intact; double quotes make bash eat it
- PowerShell `Start-Process`: stdout and stderr must go to **different** files
- `fromMsg` keyset: use the **oldest** message of the previous batch (`min` by
  (sendDttm, msgId)); `include:false` to avoid re-fetching it
- Web UI shows sender names: for peer rows `dName` exists; for own rows `fromUid:"0"`
  — map own rows to "Tôi" and peer rows to the conv display name
- Zalo writes `InfoCheckSearch.lastMessageTime` (int ms) on convs — good for sorting
- Don't read the live `.db` files with sqlite3 while Zalo runs (they're encrypted anyway);
  shared plaintext DBs are readable but WAL may be hot — copy before opening
- Preview/live servers: check `netstat -ano | findstr :8320` before starting another
  instance; reuse if healthy
- **Zalo CDN links expire** (photo-stal-XX.zdn.vn / zdn.vn video). Empirical: 2023 media
  links are **all dead** (~2 yr TTL); ~3 yr old links dead too; recent months fine. A
  conversation export from today only rescues recent media — download media EARLY, do
  not wait. Fallback URL variants (hd/normal/thumb) all die together (same asset).
- Photos arrive as **JPEG XL (`image/jxl`, `.jxl`)** — Google Photos and most software
  can't ingest it; convert to JPEG. `pillow-jxl-plugin` + Pillow works, keeps resolution.
- piexif: `piexif.insert(exif_bytes, jpeg_bytes)` **needs an output buffer arg for bytes
  input** (`piexif.insert(tiff, data, new_data)`); and `dump()` needs all IFD sections.
- mp4 date stamp: patch `mvhd` box creation/modification time (offset after 'mvhd'+
  version+flags; seconds since 1904-01-01) AND set file mtime — Google Photos uses mtime
  for videos, EXIF for photos.
- Media job pacing: sequential download ~9 s/item is too slow for thousands of items —
  parallelize (ThreadPoolExecutor, 10 workers) and write the ZIP incrementally (don't
  buffer everything in RAM; videos are tens of MB each).
- BaseHTTPRequestHandler routing: `do_GET` and `do_POST` are separate `elif` chains — an
  endpoint added to the wrong chain silently returns `{"error":"not found"}` (cost us the
  first `/api/cancel`).
- Incremental tasks should be idempotent + skippable: for mass jobs, key progress off
  files already on disk (mtime < 24 h = skip) so a canceled/interrupted run resumes
  cheaply and re-runs only do deltas.
- Writing JSON with `json.dumps(...ensure_ascii=False)` then serving it via HTTP can
  emit lone surrogates for malformed source strings; clients must decode with
  `errors="replace"` or the server must sanitize (known cosmetic issue with old Zalo rows).
- Cross-account merge empirical Δt for the same physical message: text ≤2 s in ~99%
  of pairs, tail to ~8 s; media rows can differ ~5 s between views (per-viewer CDN URL
  rewrite happens at different times) → windows 2 s (text) / 10 s (other).
- `crawledAt` priority chain: JSON `exportedAt` > filename-encoded ts
  (`media_<name>_<uid12>_YYYYMMDD_HHMMSS.zip`) > mtime. Never rely on mtime alone —
  copies/moves destroy it and parallel test files share the same second.
- Media dedupe must key on **content hash**, not filename: the same photo comes back
  under different CDN names across accounts/crawls (and JXL→JPEG re-encodes bytes, so
  hash after conversion — the media ZIPs already contain converted JPEGs).
- A conversation's raw CLI backup (`messages_<uid>.json` in the backup root) won't group
  with its WebUI exports (`<name>_<uid>.json` in exports\) — scan BOTH dirs and keep
  copies named `<name>_<uid>.json` in exports\ for the merge to see them.

---

## 7. What's still open

- Exact SQLCipher passphrase for offline DB decryption (§4.3). Next step if needed:
  CDP-hook the DAL request payload (`cipherKey` field) in the page/worker and log it.
- ~~Media/blob download pipeline~~ **DONE 2026-09-11** (WebUI media button). Remaining
  nuance: CDN TTL means old media is unrecoverable via links — if local cached copies
  (Media.db paths / `ZaloData\media\<uid>\...`) turn out to hold originals, an offline
  cache-lookup before download would rescue more items (untested).
- Server-side retention for fresh-machine sync (empirically limited; not quantified).

### Master Viewer (added 2026-09-11)

- `/viewer` page on the WebUI (exe included): browse all `master_<name>_<uid>.json` —
  messages grouped by day, date-range presets + custom from/to, text search, a filter
  for recalled flags (`missingSince` shown with strikethrough + lastSeen/missingSince
  tooltip), `copies`/`sources` provenance badges, and a Media tab previewing the
  `media_master_*` photos/videos inline (EXIF/mp4 dates shown in captions).
- Media is served by `/api/mediafile?uid=<uid>&f=<relative>` with HTTP **Range** support
  (206) so `<video>` seeks work; path traversal is blocked by realpath-prefix check;
  MIME is inferred from extension (mp4/jpg/png/webp/webm/gif).
- Master JSON is cached per (mtime,size) so browsing is instant even on 17 MB masters;
  the viewer reads exports directly — Zalo/CDP does NOT need to be running.
- Non-text msgTypes are prettified: photos → dims + CDN host, files/links → 📎/🔗 +
  title; tooltip always carries msgId/firstSeen/lastSeen for forensics.
- **Zalo-style mode (tab "Dạng Zalo")**: renders bubbles like the real Zalo window —
  me=green right / others=white left with avatar initial, day pills (Hôm nay/Hôm qua/dd/mm/yyyy),
  blue header bar + "who is me" `<select>` persisted in localStorage per master uid
  (auto-pick: 2 senders → first, >2 → the non-"Tôi" sender). Photo msgs of msgType 2 are
  matched to media-master `photos/` by send time ±2 s (bisect + whole-window scan so
  multi-photo albums all match; globally once each) and displayed inline, click-to-zoom
  lightbox. Dead-CDN photos keep the readable caption fallback.
- **Screenshot export (📷 button)**: bundles html2canvas 1.4.1 (MIT, vendored into the
  VIEWER_HTML string — no CDN, works offline) and renders the active view to a PNG
  download; print CSS yields a clean PDF. `ZALO_NO_BROWSER=1` env suppresses the
  auto-open-browser behavior of `main()` (used when starting the exe from scripts).

— Last updated 2026-09-13, after Zalo-style chat mode + offline PNG screenshot export + video
inline rendering (`msgType 18`), pretty media one-liners (`zalo_merge.pretty_media_text`) shared by
exports / master MD / viewer, and msgType-tagged media payloads for the correct renderer per file.

## Media stats audit (`/api/mediastats`)

- Classifies every photo/video message in a master: **saved** (file in media master), **alive**
  (CDN link still downloadable — GET `Range: bytes=0-1` with the ZaloPC UA), **dead** (= lost
  forever), **unknown** (not probed yet). `saved + alive + dead + unknown = mediaMsgs` always.
- Probing runs in a **background daemon thread** (12 parallel workers, ~1,600 links/min), writes
  the cache atomically every 40 probes to `master_<name>_<uid>.json.mediastats`, and the API
  returns immediately (`auditing: true`, `auditDone/auditTotal` progress). The viewer polls it
  every 1.5 s while auditing. A dead link is only ever counted after an actual probe — never
  guessed from age.
- One link per message is probed (the first of oriUrl/normalUrl/hdUrl/thumbUrl/hd); messages
  already saved locally are never probed. Cache survives server restarts.

## Daily auto-crawl (Task Scheduler)

- Chain: Task `ZaloBackup AutoCrawl` (07:30 daily, StartWhenAvailable catch-up, 8 h limit,
  IgnoreNew) → `run_auto_crawl.vbs` (windowless) → `ZaloAutoCrawl.exe` → HTTP on `127.0.0.1:8320`:
  ensure server (starts `ZaloBackup.exe` with `ZALO_NO_BROWSER=1` if down), ensure CDP
  (`/api/restart` if `/api/status` says `cdp:false`), then `/api/exportall` + `/api/merge`,
  polling `/api/progress?job=` every 5 s. Exit code feeds Task Scheduler's Last Result.
- Gotcha encoded in the runner: the **first** `/api/status` after a server boot can take ~6 s
  (cold CDP walk), so the runner probes `GET /` for liveness (cheap, no CDP) and uses a 15 s
  timeout for the CDP status check. Logs: `logs/auto_crawl.log`.
- Zalo must be logged in once interactively; afterwards the login persists and runs are
  unattended. Media already dead stays dead — the schedule's value is shrinking the future
  loss rate, not recovering the past.

## Completeness of a full-conversation crawl (verified 2026-09-13)

- `fetch_all` keyset pagination is **exhaustive over the retrievable set**: on the largest
  self-chat view (uid ...4135) it walked 11 × 500-message batches in ~11 s with **zero
  duplicate msgIds** and terminated on the short batch, reaching the conversation's very
  first message (2023-07-02). Nothing more exists server-side for the API to return.
- `countTotalMessageOfConversation` is **unreliable as a completeness oracle**: it reported
  5,423 for a conversation where the exhaustive walk yields 5,397 (likely counting recalled
  messages), and **0** for another conversation with 6,452 messages (that conv belongs to the
  other account's view — a limitation, not a bug: you can only crawl conversations visible to
  the logged-in account; log in the other account and fold via master merge).
- Practical completeness model: every message the logged-in account can retrieve is captured;
  messages recalled *before* the first crawl are unrecoverable by any tool; messages recalled
  *after* a crawl stay in the master with `missingSince`; the daily schedule minimizes the
  recalled-before-first-crawl window and downloads media links while alive.

## Stickers (verified 2026-09-13)

- Two sticker kinds in the store:
  - `msgType=7` — sent sticker / GIF (`originMsgType=chat.gif` etc.): `message` carries
    `oriUrl/thumbUrl/hdUrl` + `params.{hd,small,thumb}` on the **old gif CDN**
    (`zalo-gif.zadn.vn`). Probed 17/17: 16 still alive after 5 years — this CDN
    expires far slower than the photo CDN. `previewThumb` is **null** for stickers
    (unlike photos).
  - `msgType=4` — catalog sticker: `message = {type:"7", catId, id, extInfo}` with
    **no URL at all** (`extInfo.params.thumbUrl` empty in practice; `previewThumb` null).
    Zalo PC renders them from a local cache:
    `%APPDATA%/ZaloData/media/<accountUid>/sticker/<catId>/<stickerId>/<md5>.png|gif`.
    Coverage is partial (cached only when the sticker pack was downloaded/used):
    measured 8,174/10,059 messages = 900 distinct (catId,id), 589 in cache (65%).
- Display pipeline (`/api/stickerthumb?uid=&msgid=`), in priority order:
  1. downloaded file in the media master (`stickers/` folder — media downloader now
     treats msgType 7 as a sticker, keeps the real extension, no JXL re-encode);
  2. Zalo PC's local sticker cache (type 4, index rebuilt every 60s);
  3. proxy of the row's CDN gif URL (Zalo UA, small `params.small` variant first —
     65 KB vs 2.9 MB for `_l.gif`).
- Masters store `[Sticker]` as the text for types 4/7 (legacy JSON dumps self-heal on
  every fold). Viewer renders an inline image when the payload exposes `st:true`.
- `fold_snapshot` perf: the per-type fuzzy index is now incrementally sorted
  (bisect insert) — folding 108k rows takes ~0.7s; the previous re-sort-per-row was
  O(n²) and hung >7 min on the 108k "Mẹ Bun" conversation (would have hit the daily
  auto-crawl on big chats).

## Non-photo JSON payloads made human-readable (2026-09-13, part 2)

Beyond stickers, four more payload types used to dump raw JSON into masters:
- `msgType=3` voice messages: `params.{m4a, duration}` on `voice-aac-dl.zdn.vn`
  → `[🎙️ Tin nhắn thoại 12s]` (384 rows in "Mẹ Bun"; 99 lack duration).
- `msgType=5` doodles (`chat.doodle`): `params.{width,height}`, `oriUrl` png
  → `[🖼️ Doodle 815×1280]` (8 rows).
- `msgType=17` location shares (`chat.location.new`): `message.desc` already reads
  "Tọa độ (lat, lng)" → `📍 Tọa độ (…)` (1 row).
- `msgType=52` embedded web content (`chat.webcontent`, e.g. bank-account cards):
  label in `params.customMsg.msg.vi/en` → `🧩 Tài khoản ngân hàng` (4 rows).
- `msgType=1` with `action:"rtf"`: rich text; the real text is in `message.title`.
`fold_snapshot` self-heals all of these on every fold (raw rows re-derive text via
`_text_of`; legacy friendly rows fall back to `[Sticker]` for 4/7). Master "Mẹ Bun"
verified: 0 JSON rows out of 108,094.

## Voice notes (verified 2026-09-13)

- `msgType=3` carries `params.{m4a, duration}` pointing at `voice-aac-dl.zdn.vn/<id>/<hash>.aac`.
- **CDN links die fast**: 9/9 links probed (2021) return 404 — unlike sticker GIFs
  (zalo-gif.zadn.vn still alive after 5 years), voice CDN expiry is photo-CDN-like.
- **Zalo PC caches played voice notes on disk**:
  `%APPDATA%/ZaloData/media/<accountUid>/ZaloDownloads/voice/<msgId>` — a raw
  ADTS AAC file (no extension, "MPEG ADTS, AAC, v4 LC, 16 kHz, stereo"), one per
  message actually played on this PC. Measured: 268 cached entries, 100/384 of the
  "Mẹ Bun" voice notes recoverable (the other 284 were never played locally).
- Viewer: `/api/voice?uid=&msgid=` serves local cache first, then proxies the CDN
  with Range pass-through (seekable). Rows expose `vo:true` **only when actually
  playable** (cache hit) so users never see a broken player; other rows keep the
  readable `[🎙️ Tin nhắn thoại 12s]` text.
- Playback verified in browser: readyState 4, correct duration, no MediaError;
  works in both Messages and Zalo-style tabs (12 players rendered per chunk).

## Type-6 "recommended" messages = call logs, links, contacts (2026-09-13)

`msgType=6` looked like links ("[Link] 13 phút 28 giây") but is 4 different things,
split by `message.action`:
- `recommened.misscall` (1,102 rows) → `[📞 Cuộc gọi nhỡ]` / `[📹 Cuộc gọi video nhỡ]`
  (`params.calltype`: 0=voice, 1=video; the "0 phút 0 giây" title is ignored).
- `recommened.calltime` (1,055) → `[📞 Cuộc gọi thoại 13 phút 28 giây]` /
  `[📹 Cuộc gọi video …]` from `params.duration` (seconds).
- `recommened.link` (379) → stays `[Link] <title>` (real shared links).
- `recommened.user` (67) → `[👤 <title>]` (shared contact cards, desc holds phone).
Also: many legacy call rows carried `title="sendBubbleMessage"` or `href=""`.
Self-heal rewrites `[Link] …` rows on every fold via raw payload. Exports
(JSON/TXT/CSV `_friendly`) share the same rendering through `pretty_media_text`.

## Sender alias merge — one person, several names (2026-09-13)

Multi-account merges gave one fromUid several display names: account A's export
labels itself "Tôi" while account B's export shows the same uid as "Đoàn Bảo";
the peer appears as "Mẹ Bun" (46,386 rows) AND "Hồng Thắm" (1,362) because each
account had a different contact name. With 4 "senders" for 2 real people the
viewer's bubble side-guessing broke (own messages rendered on the peer side).
Fix in `_backfill_senders` (runs on every master save): count (fromUid → sender)
pairs, and when a uid has several names pick the canonical one (most frequent;
"Tôi" never wins) and relabel ALL of that uid's rows. Mẹ Bun master went from 4
labels to exactly 2 senders: Đoàn Bảo (60,346) + Mẹ Bun (47,748).
Viewer ME-guess upgrade: if exactly one sender is NOT named like any master
conversation, that side is "me" (own name equals the self-chat conversation name
in masters merged across both accounts).

## Auto-detecting "me" — no manual override needed (2026-09-13)

Root cause of wrong bubble sides was never missing knowledge: raw rows carry
fromUid, and fromUid=0 literally means "the logged-in account of the export".
Two fixes make the pipeline self-identifying:
1. `fold_snapshot` records the crawling account into `master.me_uids` (from a
   fromUid=0 row's toUid) so multi-crawler masters keep provenance.
2. `_backfill_senders` merges "Tôi" into the same uid's other name (real
   display name wins). Masters now always show one label per real person.
Viewer: ME-guess prefers the sender not named like any master conversation;
guess key bumped to `zme2_<uid>` so stale wrong guesses are discarded once.
Verified: Mẹ Bun auto-guess = Đoàn Bảo, 215 own bubbles, persisted.

## Smoke test (`smoke_test.mjs`, 2026-09-13)

Playwright script opening every master in the live viewer and asserting:
1. **no-json-dumps** — zero rows whose text starts with `{`;
2. **no-missing-senders** — zero empty/'?' senders;
3. **sender-count-sane** — ≤4 distinct senders (1-1/self chats);
4. **one-me-sender + me-guess-matches-bubbles** — Zalo-style "me" side consistent
   with the auto-guess;
5. **voice-plays / sticker-renders** — flagged rows actually render inline media
   that loads (audio readyState>0, image naturalWidth>0).
First run caught 5,382 leftover JSON rows in the Bảo/Blue Roses masters (legacy
photo/video/URL payloads that predate the self-heal rules) — healed via
pretty_media_text; all masters now pass.
Run: `npm run smoke` (server must be running on :8320).

## sendBubbleMessage — không phải link, là bong bóng cuộc gọi (13/09/2026)

Zalo đặt `message.title = "sendBubbleMessage"` (kèm `action = recommened.misscall/calltime`)
cho **bong bóng log cuộc gọi** — exporter friendly cũ dán nhầm `[Link] sendBubbleMessage`.
Bằng chứng: quét mọi raw export, 100% raw row có title này mang action cuộc gọi; 176 link
thật đều mang title = tiêu đề trang web. Đã sửa 3 lớp:
1. `pretty_media_text` (zalo_merge) + `_friendly` (webui): title sentinel → `[📞 Cuộc gọi thoại]` / `[📹 Cuộc gọi video]`.
2. `heal_master_texts()` — hàm heal tách riêng, chạy tự động trước mỗi lần fold; raw rows heal theo action thật.
3. Legacy friendly rows (raw=null): chỉ đổi khi title nhận diện được (`sendBubbleMessage`),
   KHÔNG đụng `[Link]` trống hoặc `[Link] https://…` (link thật).
Healed: master_Blue Roses 6/6 dòng; smoke test PASS toàn bộ.

## UI redesign theo taste-skill (2026-09-13, commit 74afc61)

Audit-first theo skill `redesign-existing-projects` (github.com/Leonxlnx/taste-skill):
Scan → Diagnose → Fix theo độ ưu tiên font → màu → states → layout → polish.
Các quyết định thiết kế cần GIỮ khi sửa UI về sau:

1. **Một hệ token chung cho cả 2 trang** (HTML + VIEWER_HTML trong zalo_backup_webui.py):
   `--bg:#0b1220 --panel:#121a2c --panel2:#0d1424 --line:#223049 --tx:#e9eef7 --dim:#93a5c4
   --acc:#5aa9ff --ok:#43d17c --warn:#ffb454 --err:#ff6b6b` + `--r-lg:14px --r-md:10px`.
   Đổi màu/radius phải sửa ở CẢ HAI khối `:root`.
2. **Font — KHÔNG tải font ngoài** (đúng lời hứa "dữ liệu không rời khỏi máy", localhost thuần):
   `--font-ui: "Segoe UI Variable Text","Segoe UI",system-ui` · `--font-disp: "Segoe UI Variable
   Display","Bahnschrift"` (tiêu đề, tracking -0.02em) · `--font-mono: "Cascadia Mono",Consolas`
   cho UID/timestamp. Số liệu dạng bảng luôn `font-variant-numeric: tabular-nums`.
3. **Nút hành động = tinted translucent, không màu nền đặc**: `.tint-ok` (media), `.tint-warn`
   (backup-all), `.tint-acc2` (merge), `.tint-err` (hủy). Chỉ nút chính dùng nền accent đặc.
   Cấm inline `style="background:..."` trên nút — đã từng tạo hàng "cầu vồng" 4 màu.
4. **States bắt buộc** trên mọi element tương tác: hover (brightness/border), `:active`
   (`scale(.985)`/`translateY(1px)`), `:focus-visible` (outline 2px accent), transition 150ms.
   `prefers-reduced-motion: reduce` phải tắt hết animation.
5. **Bề mặt**: shadow tint theo nền (`rgba(2,8,22,.35)`, không đen thuần), noise overlay 2.5%
   chống flat, scrollbar + `::selection` theo theme, `100dvh` thay `100vh`, sticky day-header
   frosted (`backdrop-filter: blur(8px)`).
6. **Loading state**: bảng hội thoại hiện 5 dòng skeleton (`tr.skl` + `.skbar` shimmer) trong lúc
   chờ `/api/conversations` — không bao giờ để bảng trắng trơn.

Tham khảo đầy đủ quy trình audit: mục "Design Audit" trong SKILL.md của taste-skill
(font/palette/layout/states/content/icons/code-quality).
