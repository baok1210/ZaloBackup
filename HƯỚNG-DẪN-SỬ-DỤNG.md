# 🟦 HƯỚNG DẪN SỬ DỤNG ZALO BACKUP
### (Dành cho người KHÔNG biết lập trình — chỉ cần biết bấm chuột)

---

## 1. Phần mềm này làm được gì?

- **Sao lưu toàn bộ tin nhắn** trong Zalo PC của bạn ra máy tính (kể cả hội thoại dài vài năm).
- **Tải về toàn bộ ảnh & video** trong hội thoại, dán ngày gửi vào file để Google Photos tự nhận đúng ngày.
- **Xem lại tin nhắn + ảnh/video** bằng một trang web chạy ngay trên máy (không cần mạng, không cần mở Zalo).
- **Tự động backup mỗi sáng** nếu bạn muốn (hẹn giờ sẵn, máy bật là tự chạy).
- **Hoàn toàn riêng tư**: mọi dữ liệu nằm trong máy bạn, không gửi đi đâu cả.

---

## 2. Cài lần đầu (chỉ làm 1 lần)

1. Vào trang dự án: **https://github.com/baok1210/ZaloBackup**
2. Mục **Releases** (bên phải) → tải file **`ZaloBackup.exe`** về máy.
   > ⚠️ Windows có thể hiện màn hình xanh "Windows protected your PC".
   > Đây là chuyện bình thường với phần mềm tự đóng gói. Bấm **More info** → **Run anyway**.
3. Tạo một thư mục để chứa dữ liệu, ví dụ `D:\ZaloBackup`, rồi **chép `ZaloBackup.exe` vào đó**.
4. Đảm bảo **Zalo PC đã đăng nhập** account bạn muốn backup.
5. **Nhấp đúp** `ZaloBackup.exe` → trình duyệt tự mở trang `http://localhost:8320`.
   > Nếu trình duyệt không tự mở, tự vào địa chỉ: `http://localhost:8320`

Xong phần cài! Các lần sau chỉ cần **nhấp đúp file .exe** là dùng.

---

## 3. Backup thường ngày (4 bước)

Làm đúng theo bảng **"🚀 Bắt đầu ở đây — 4 bước"** ngay đầu trang phần mềm:

| Bước | Bấm gì | Ghi chú |
|---|---|---|
| **1** | **⟳ Restart Zalo (debug mode)** | Chờ 10–40 giây. Thấy dòng xanh **✅ Zalo OK** là được. Chỉ cần làm 1 lần mỗi khi mở máy. |
| **2** | Chọn hội thoại → **⬇ Xuất hội thoại đã chọn** | Hoặc bấm thẳng **⚡ Backup TẤT CẢ hội thoại** để lấy hết một thể. |
| **3** | **🖼 Tải tất cả ảnh & video** | Ảnh/video gói thành file ZIP. Chạy lại thì chỉ tải phần mới, rất nhanh. |
| **4** | **📖 Xem & đọc master** | Trang đọc tin nhắn + ảnh/video, giống hệt Zalo. |

💾 File lưu ở **thư mục `exports`** cạnh file .exe (địa chỉ hiển thị ngay trên trang phần mềm).
File ZIP ảnh/video: tải về từ link hiện sau khi chạy xong, rồi chép vào thư mục `exports` cũng được.

---

## 4. Xem lại tin nhắn (Viewer)

1. Bấm **📖 Xem & đọc master** (góc trên trang chính).
2. Bấm vào hội thoại cần xem → có 3 kiểu xem:
   - **💬 Tin nhắn** — dạng danh sách, lọc theo ngày, tìm chữ, lọc "chỉ tin bị thu hồi".
   - **🖼 Media** — lưới ảnh + video, bấm để phóng to, phát video luôn trong trang.
   - **💬 Dạng Zalo** — bong bóng chat giống hệt điện thoại, đẹp nhất để chụp màn hình.
3. Phần mềm **nhớ** bạn đang đọc hội thoại nào, đang xem tab nào và cuộn tới đâu — mở lại là thấy ngay chỗ cũ.

### Thanh thống kê ảnh/video (nổi ở đầu trang)
- 💾 **Đã lưu local** — file đã tải về máy, an toàn vĩnh viễn.
- 🔗 **Link sống** — Zalo còn cho tải → nên bấm **Tải tất cả ảnh & video** sớm để giữ lại.
- 💀 **Đã mất vĩnh viễn** — link chết + chưa kịp tải. Không cách nào lấy lại được, đừng chần chừ!
- ❔ **Chưa kiểm tra** — đang quét, chờ vài phút là xong.

### Chụp màn hình / in PDF
- **📷 Chụp màn hình (PNG)** — lưu nguyên khung chat thành file ảnh.
- Hoặc bấm **Ctrl + P** để in ra PDF (nền trắng, gọn gàng).

---

## 5. Backup HAI account Zalo (gộp về một)

Nếu bạn từng đổi account và muốn gộp lịch sử của cả hai:

1. Đăng nhập account A trên Zalo PC → chạy **⚡ Backup TẤT CẢ**.
2. Đăng xuất, đăng nhập account B → bấm **⟳ Restart Zalo (debug mode)** → chạy **⚡ Backup TẤT CẢ** lần nữa.
3. Bấm **🔗 Gộp bản trùng lặp** → phần mềm tự nhận biết các hội thoại trùng giữa 2 account và gộp thành 1 file "master" sạch, không còn tin lặp.

---

## 6. Hẹn giờ backup tự động mỗi sáng (tuỳ chọn)

Phần mềm có sẵn script hẹn giờ. Làm 1 lần duy nhất:

1. Mở thư mục chứa phần mềm.
2. Chuột phải vào `register_task.ps1` → **Run with PowerShell**.
   > Nếu bị chặn: mở PowerShell, gõ:
   > `powershell -ExecutionPolicy Bypass -File "đường-dẫn-đến\register_task.ps1"`
3. Xong! Mỗi **7:30 sáng** (khi máy đang bật) phần mềm tự backup + tự gộp, không cần bấm gì.

- Đổi giờ: sửa số `07:30` trong file `register_task.ps1` rồi chạy lại.
- Gỡ lịch: chạy `unregister_task.ps1`.
- Xem lịch sử chạy: mở file `logs\auto_crawl.log`.

---

## 7. Câu hỏi thường gặp

**❓ Bấm Restart Zalo xong bị đăng xuất / phải quét QR lại?**
Zalo PC restart lại app — thường vẫn giữ đăng nhập. Nếu bị, đăng nhập lại 1 lần là các lần sau ổn.

**❓ Bấm Xuất nhưng báo lỗi / không thấy hội thoại nào?**
- Zalo chưa ở chế độ debug → bấm **⟳ Restart Zalo (debug mode)** rồi chờ dòng **✅ Zalo OK**.
- Cổng bị phần mềm khác chiếm → tắt các ZaloBackup.exe cũ (Task Manager), mở lại.

**❓ Windows chặn file .exe?**
**More info → Run anyway**. Phần mềm tự đóng gói không có chữ ký số nên Windows hỏi cho chắc.

**❓ Ảnh/video báo "link CDN đã hết hạn"?**
Link của Zalo có hạn (1–2 năm, có khi ngắn hơn). Chết rồi thì không cứu được — vì vậy nên backup sớm và đều (hẹn giờ tự động giúp việc này).

**❓ Tin nhắn bị thu hồi (unsend) có lưu được không?**
- Thu hồi **sau khi** đã backup → vẫn còn nguyên trong master, gạch đỏ đánh dấu.
- Thu hồi **trước khi** backup lần đầu → không phần mềm nào lấy lại được.

**❓ Muốn backup máy khác / account khác?**
Cứ cài như mục 2 trên máy đó, đăng nhập account cần backup rồi chạy bình thường. File master của mỗi máy độc lập.

**❓ Gỡ cài đặt thế nào?**
Xóa thư mục phần mềm là xong. Trước đó chạy `unregister_task.ps1` nếu đã bật lịch tự động.

---

## 8. Tóm tắt siêu ngắn (dán lên tường)

> 1. Nhấp đúp `ZaloBackup.exe`
> 2. Bấm **⟳ Restart Zalo (debug mode)** → chờ ✅
> 3. Bấm **⚡ Backup TẤT CẢ**
> 4. Bấm **🔗 Gộp bản trùng lặp**
> 5. Mở **📖 Xem & đọc master** → đọc thoải mái 🎉
