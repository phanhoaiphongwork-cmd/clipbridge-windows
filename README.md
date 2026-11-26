# ClipBridge cho Windows

App Windows đồng bộ clipboard (chữ + ảnh) giữa PC và iPhone qua Internet: copy trên PC thì iPhone nhận được, iPhone gửi sang thì PC nhận được.
Nội dung được **mã hoá đầu-cuối (AES-256-GCM)**. Server chỉ thấy dữ liệu đã mã hoá và tự xoá sau 4 giờ.

Phía iPhone: https://clipbridge-weld.vercel.app

## Cài đặt (~2 phút)
1. Cài **Python 3** từ https://www.python.org/downloads/. Khi cài, nhớ tick **Add python.exe to PATH**.
2. Vào mục **[Releases](../../releases/latest)** của repo này và tải **một file duy nhất**: `ClipBridge.pyw`.
   Nếu trình duyệt hỏi có giữ file không, chọn **Giữ lại (Keep)**.
3. Nhấp đúp `ClipBridge.pyw`. Lần đầu app sẽ:
   - tự cài thư viện giao diện `customtkinter` và `qrcode` (cần mạng, mất vài giây);
   - tự cài vào `%LOCALAPPDATA%\ClipBridge`, tạo lối tắt **ClipBridge** trên Desktop và Start Menu, bật **tự chạy ngầm khi đăng nhập Windows**;
   - hiện **mã QR**. Mở Camera iPhone và quét mã đó để ghép đôi.
4. Xong. File `ClipBridge.pyw` vừa tải về có thể xoá đi; từ giờ mở app bằng lối tắt.

Không cần giải nén, không cần **Unblock**: bản cài trong `%LOCALAPPDATA%` không mang dấu "tải từ Internet". Muốn tắt tự khởi động: **Cài đặt** (bánh răng) → bỏ tick **Tự chạy ngầm khi đăng nhập Windows**.

<details>
<summary>Cách cũ: tải cả thư mục (ZIP)</summary>

1. Tải repo: nút **Code → Download ZIP**. Chuột phải file ZIP → **Properties** → tick **Unblock** → **OK**.
2. Giải nén ra một thư mục cố định, ví dụ `C:\Users\<tên bạn>\ClipBridge`. **Đừng chuyển thư mục** sau khi cài.
3. Nhấp đúp **`ClipBridge.bat`** (hoặc `clipbridge_app.pyw`), quét mã QR, rồi trong **Cài đặt** tick **Tự chạy ngầm…** và chọn **Tạo lối tắt**.
</details>

## Sử dụng
| Mục | Công dụng |
|---|---|
| Công tắc **Tự gửi** | Bật: copy trên PC tự gửi sang iPhone. Tắt khi copy thông tin nhạy cảm. |
| **Tìm trong lịch sử** (Ctrl+F) và lọc **Tất cả / PC / iPhone** | Tìm nhanh các mục đã copy trong 4 giờ qua |
| Dòng trong lịch sử | Trỏ chuột vào để hiện nút **Copy**, hoặc nhấp đúp dòng để copy lại |
| **Ghép đôi** | Mã QR để quét bằng iPhone. Mã ghép đôi được ẩn, chỉ hiện 5 ký tự cuối; bấm **Copy mã** để dán vào iPhone (clipboard tự xoá mã sau 60 giây). **Tạo mã mới…** nếu lỡ lộ mã. |
| Dòng trạng thái ở cuối | Việc gửi/nhận gần nhất. Bấm vào để xem nhật ký. |
| **Cài đặt** (bánh răng) | Tự khởi động, tạo lối tắt, xem nhật ký, mở thư mục, **Gỡ cài đặt…**, **Thoát hẳn** |

- Giao diện theo chế độ sáng/tối của Windows.
- Bấm **X** để đóng cửa sổ thì ClipBridge **vẫn chạy ngầm**. Mở lại lối tắt để hiện cửa sổ.
- App tự bỏ qua nội dung từ trình quản lý mật khẩu (1Password, Bitwarden, KeePass…).

## Gỡ cài đặt
**Cài đặt → Gỡ cài đặt…**: app tắt tự khởi động, xoá lối tắt, mã ghép đôi, nhật ký và **xoá luôn thư mục ClipBridge** (`%LOCALAPPDATA%\ClipBridge`) ngay sau khi app đóng.

Để an toàn, nếu thư mục còn chứa file khác không phải của ClipBridge (ví dụ bạn giải nén thẳng vào Downloads), app chỉ xoá các file của ClipBridge và giữ nguyên thư mục.

## Smart App Control (Windows 11)
Chạy bằng Python như trên thì **không bị chặn**:
- `pythonw.exe` và các DLL của Python đều do Python Software Foundation ký số.
- File `.pyw` là script, Smart App Control không kiểm tra.
- `customtkinter` và `qrcode` viết thuần Python, không có DLL.
- App không dùng PowerShell hay VBScript.

Riêng `ClipBridge.exe` tự build bằng `build_exe.bat` thì **sẽ bị chặn** vì chưa ký số. Đừng dùng file .exe này nếu máy bật Smart App Control.

## Cập nhật
Tải `ClipBridge.pyw` mới từ **Releases** rồi nhấp đúp. App hỏi để đóng bản đang chạy, thay bằng bản mới và mở lại. Mã ghép đôi được giữ nguyên, không cần ghép lại iPhone.

**Đang dùng bản ZIP cũ?** Cứ nhấp đúp `ClipBridge.pyw` mới: app tự lấy mã ghép đôi từ thư mục cũ (nếu bản cũ đang bật tự khởi động, hoặc đặt file `.pyw` vào chính thư mục cũ rồi chạy), chuyển tự khởi động và lối tắt sang bản mới. Sau đó có thể xoá thư mục cũ.

## Bản dòng lệnh (tuỳ chọn)
```
py -m pip install qrcode customtkinter
py clipbridge_cloud.py              # chạy trong cửa sổ terminal
py clipbridge_cloud.py --history    # xem lịch sử và copy lại
py clipbridge_cloud.py --install    # tự chạy ngầm cùng Windows
py clipbridge_cloud.py --stop       # dừng chạy ngầm
py clipbridge_cloud.py --uninstall  # gỡ tự khởi động
```
Mỗi lúc chỉ nên chạy một bản: app hoặc bản dòng lệnh.

## Các file
| File | Nội dung |
|---|---|
| `ClipBridge.bat` | Mở app (nhấp đúp) |
| `clipbridge_app.pyw` | App có giao diện |
| `clipbridge_cloud.py` | Lõi đồng bộ + mã hoá, bản dòng lệnh |
| `clipbridge.py` | Clipboard Windows + xử lý ảnh |
| `build_single.py` | Gộp mọi thứ thành một file `ClipBridge.pyw` (`py build_single.py` → `dist/ClipBridge.pyw`) |
| `build_exe.bat` | Build `ClipBridge.exe` (không cần nếu chạy bằng Python) |

### Phát hành bản một file
GitHub Action `.github/workflows/release.yml` tự chạy `build_single.py`:
- Mỗi lần push lên `main`: `ClipBridge.pyw` nằm trong mục **Artifacts** của lần chạy (để thử).
- Khi tạo **Release** mới trên GitHub (Releases → Draft a new release → đặt tag, ví dụ `v1.1` → Publish): `ClipBridge.pyw` tự được đính kèm vào Release.

## Bảo mật
- **Mã ghép đôi là chìa khoá duy nhất.** Ai có mã (hoặc ảnh chụp QR) đều đọc/ghi được clipboard của bạn. Nếu lỡ lộ, bấm **Ghép đôi iPhone (QR) → Tạo mã mới**.
- Mã nằm trong `clipbridge_config.json`. Đừng chia sẻ hay commit file này.
