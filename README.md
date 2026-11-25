# ClipBridge cho Windows

App Windows đồng bộ clipboard (chữ + ảnh) giữa PC và iPhone qua Internet: copy trên PC thì iPhone nhận được, iPhone gửi sang thì PC nhận được.
Nội dung được **mã hoá đầu-cuối (AES-256-GCM)**. Server chỉ thấy dữ liệu đã mã hoá và tự xoá sau 4 giờ.

Phía iPhone: https://clipbridge-weld.vercel.app

## Cài đặt (~3 phút)
1. Cài **Python 3** từ https://www.python.org/downloads/. Khi cài, nhớ tick **Add python.exe to PATH**.
2. Tải repo này về: nút **Code → Download ZIP**.
3. Chuột phải file ZIP → **Properties** → tick **Unblock** → **OK**. Bước này bỏ dấu "tải từ Internet", để Windows không chặn hay hỏi lại khi chạy.
4. Giải nén ra một thư mục cố định, ví dụ `C:\Users\<tên bạn>\ClipBridge`. **Đừng chuyển thư mục** sau khi cài.
5. Nhấp đúp **`ClipBridge.bat`** để mở app. Nếu không mở được, nhấp đúp `clipbridge_app.pyw`.
6. Lần đầu app hiện **mã QR**. Mở Camera iPhone và quét mã đó để ghép đôi.
7. Trong app:
   - Tick **Tự chạy ngầm khi đăng nhập Windows**.
   - Bấm **Tạo lối tắt** để có biểu tượng ClipBridge trên Desktop và Start Menu.

## Sử dụng
| Mục | Công dụng |
|---|---|
| **Lịch sử** (▼/▶) | Các mục đã copy trên PC và iPhone trong 4 giờ qua. Bấm **Copy** để copy lại. |
| **Tạm dừng tự gửi sang iPhone** | Tạm ngưng gửi, ví dụ khi copy thông tin nhạy cảm |
| **Ghép đôi iPhone (QR)** | Hiện lại mã QR, hoặc **Tạo mã mới** nếu lỡ lộ mã |
| **Hoạt động** | Nhật ký gửi/nhận (cũng ghi vào `clipbridge.log`) |

- Bấm **X** để đóng cửa sổ thì ClipBridge **vẫn chạy ngầm**. Mở lại lối tắt để hiện cửa sổ.
- Muốn tắt hẳn: bấm **Thoát hẳn**.
- App tự bỏ qua nội dung từ trình quản lý mật khẩu (1Password, Bitwarden, KeePass…).

## Smart App Control (Windows 11)
Chạy bằng Python như trên thì **không bị chặn**:
- `pythonw.exe` và các DLL của Python đều do Python Software Foundation ký số.
- File `.pyw` là script, Smart App Control không kiểm tra.
- App không dùng PowerShell hay VBScript.

Riêng `ClipBridge.exe` tự build bằng `build_exe.bat` thì **sẽ bị chặn** vì chưa ký số. Đừng dùng file .exe này nếu máy bật Smart App Control.

## Cập nhật
Tải ZIP mới, rồi giải nén **đè** vào cùng thư mục. File `clipbridge_config.json` chứa mã ghép đôi vẫn được giữ nguyên, nên không cần ghép lại iPhone.

## Bản dòng lệnh (tuỳ chọn)
```
py -m pip install qrcode
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
| `build_exe.bat` | Build `ClipBridge.exe` (không cần nếu chạy bằng Python) |

## Bảo mật
- **Mã ghép đôi là chìa khoá duy nhất.** Ai có mã (hoặc ảnh chụp QR) đều đọc/ghi được clipboard của bạn. Nếu lỡ lộ, bấm **Ghép đôi iPhone (QR) → Tạo mã mới**.
- Mã nằm trong `clipbridge_config.json`. Đừng chia sẻ hay commit file này.
