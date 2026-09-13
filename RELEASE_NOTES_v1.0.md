# ZaloBackup v1.0 — first public release 🎉

**ZaloBackup** is a 100% local, offline tool that backs up your Zalo PC conversations — messages, photos and videos — merges multiple accounts into one deduplicated archive, and lets you re-read everything in a built-in viewer that looks like the real Zalo app. No cloud, no server: your data never leaves your machine.

---

# 🇻🇳 Bản tiếng Việt (dưới cùng) — English first

## ✨ Highlights

- **One-click run** — download `ZaloBackup.exe`, double-click, done. No Python, no install.
- **Full-conversation export** — every message from today back to the very first one (keyset pagination, no message-count caps; ~5,400 messages in 11 s on a 3-year-old chat).
- **⚡ Backup ALL conversations** — one button for the whole account; re-runs skip anything backed up in the last 24 h.
- **Media downloader** — all photos & videos in parallel, packed into date-stamped ZIPs; JPEG-XL → JPEG conversion; the send date is written into EXIF (photos) and the MP4 header (videos) so Google Photos files them on the right day.
- **True dedup** — Zalo stores each own-device message twice (a PC copy + a phone-sync copy with different `msgId`s); the tool collapses them by `cliMsgId`. The same chat exported from two different accounts is merged by content + time matching.
- **Masters that accumulate** — daily crawls fold into one `master_<name>_<uid>.json`: new messages are added, known ones just get `firstSeen`/`lastSeen` bumped, recalled ones are flagged `missingSince` but their content is kept forever.
- **Media master** — photos/videos are deduplicated across crawls by SHA-256 content hash, so re-downloading the same file costs nothing.
- **Media health stats** — the viewer audits every CDN link in the background: 💾 saved locally / 🔗 still alive / 💀 permanently lost — so you know exactly what to rescue before it disappears.
- **Master Viewer (`/viewer`)** — browse by day, full-text search, "only recalled" filter, **Zalo-style chat mode** with inline photo/video playback, one-click PNG screenshot and print-to-PDF. It remembers the last conversation, the active tab and your scroll position.
- **Daily auto-backup (optional)** — Task Scheduler scripts run the whole pipeline at 07:30, catch up if the PC was off, fully silent, logged to `logs/auto_crawl.log`.

## 🚀 Quick start (no coding needed)

1. Download `ZaloBackup.exe` below and put it in an empty folder.
2. Double-click it — your browser opens `http://localhost:8320`.
3. Click **⟳ Restart Zalo (debug mode)** and wait for **✅ Zalo OK**.
4. Click **⚡ Backup TẤT CẢ hội thoại**, then open **📖 Xem & đọc master** and read everything.

> **Windows SmartScreen warning?** Click **More info → Run anyway**. That's normal for unsigned, self-built binaries.

The complete zero-code guide is **[HƯỚNG-DẪN-SỬ-DỤNG.md](HƯỚNG-DẪN-SỬ-DỤNG.md)** in the repository (Vietnamese).

## ⚠️ Known limitations — read before relying on it

- Only what your logged-in Zalo PC session has synced can be exported. Messages recalled **before** your first crawl cannot be recovered by any tool — Zalo does not serve them to anyone.
- Photo/video CDN links expire (often within 1–2 years). Once dead, the file is gone; the media stats panel shows exactly what is still rescuable, and the daily schedule exists to shrink that risk window.
- End-to-end encrypted secret chats are not accessible.
- Windows only (the tool drives Zalo PC).

---

# 🇻🇳 TIẾNG VIỆT

## ZaloBackup v1.0 — bản phát hành công khai đầu tiên 🎉

**ZaloBackup** là công cụ backup **100% chạy local, offline**: sao lưu toàn bộ tin nhắn, ảnh, video từ Zalo PC, gộp nhiều account thành một kho dữ liệu đã khử trùng lặp, và xem lại mọi thứ bằng trình đọc tích hợp giống hệt giao diện Zalo. Không cloud, không server — dữ liệu không bao giờ rời khỏi máy bạn.

## ✨ Điểm nổi bật

- **Chạy 1 cú nhấp đúp** — tải `ZaloBackup.exe`, double-click là xong. Không cần Python, không cần cài đặt.
- **Xuất trọn hội thoại** — lấy được mọi tin nhắn từ hôm nay về đúng tin đầu tiên (không giới hạn số tin; hội thoại 3 năm ~5.400 tin lấy trong 11 giây).
- **⚡ Backup TẤT CẢ hội thoại** — 1 nút cho cả account; lần chạy sau tự bỏ qua phần đã backup trong 24h.
- **Tải ảnh & video** — tải song song, đóng gói ZIP kèm ngày gửi; chuyển JPEG-XL → JPEG; ghi ngày gửi vào EXIF (ảnh) và header mp4 (video) để **Google Photos tự nhận đúng ngày**.
- **Khử trùng lặp thật** — Zalo lưu tin tự gửi 2 lần (bản PC + bản sync từ điện thoại, khác `msgId`); tool gộp theo `cliMsgId`. Cùng một hội thoại export từ 2 account khác nhau cũng được gộp bằng khớp nội dung + thời gian.
- **Master tích lũy** — các lần crawl hằng ngày gộp vào một file `master_<tên>_<uid>.json`: tin mới được thêm, tin cũ chỉ cập nhật `firstSeen`/`lastSeen`, tin bị **thu hồi** bị gắn cờ `missingSince` nhưng **vẫn giữ nguyên nội dung vĩnh viễn**.
- **Media master** — ảnh/video khử trùng lặp giữa các lần crawl bằng SHA-256: tải lại file cũ tốn 0 công sức.
- **Thống kê sức khoẻ media** — viewer tự quét từng link CDN: 💾 đã lưu local / 🔗 còn sống / 💀 đã mất vĩnh viễn — biết chính xác phải cứu gì trước khi mất.
- **Master Viewer (`/viewer`)** — xem theo ngày, tìm kiếm, lọc "chỉ tin bị thu hồi", **chế độ 💬 Dạng Zalo** với ảnh/video phát ngay trong bong bóng chat, chụp màn hình PNG 1 nút, in ra PDF. Nhớ hội thoại đang đọc, tab đang mở và vị trí cuộn.
- **Tự động backup mỗi sáng (tuỳ chọn)** — script hẹn giờ Windows chạy toàn bộ lúc 07:30, máy tắt đúng giờ thì bắt kịp khi bật máy, chạy ẩn hoàn toàn, có log.

## 🚀 Bắt đầu nhanh (không cần biết code)

1. Tải `ZaloBackup.exe` bên dưới, chép vào một thư mục trống.
2. Nhấp đúp — trình duyệt tự mở `http://localhost:8320`.
3. Bấm **⟳ Restart Zalo (debug mode)**, chờ dòng **✅ Zalo OK**.
4. Bấm **⚡ Backup TẤT CẢ hội thoại**, rồi mở **📖 Xem & đọc master**.

> **Windows hiện màn hình xanh SmartScreen?** Bấm **More info → Run anyway** — chuyện bình thường với phần mềm tự đóng gói chưa có chữ ký số.

Hướng dẫn đầy đủ bằng tiếng Việt, dành cho người không biết code: **[HƯỚNG-DẪN-SỬ-DỤNG.md](HƯỚNG-DẪN-SỬ-DỤNG.md)**.

## ⚠️ Giới hạn cần biết trước khi tin cậy

- Chỉ backup được những gì phiên Zalo PC đang đăng nhập đã đồng bộ. Tin bị thu hồi **trước** lần crawl đầu tiên thì không công cụ nào lấy lại được — Zalo không trả chúng cho bất kỳ ai.
- Link CDN ảnh/video của Zalo có hạn (thường 1–2 năm). Link chết thì file mất thật; thanh thống kê cho biết chính xác cái nào còn cứu được, và lịch crawl tự động tồn tại để thu hẹp cửa sổ rủi ro đó.
- Chat bí mật mã hoá E2EE không truy cập được.
- Chỉ hỗ trợ Windows (vì điều khiển Zalo PC).

---

*Free & open source — dùng cục bộ, chỉ backup hội thoại của chính bạn. Zalo is a trademark of VNG Corporation; this project is not affiliated with VNG.*
