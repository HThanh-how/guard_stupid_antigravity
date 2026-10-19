# Bản chuyển đổi Windows native

Bản sửa cục bộ của repo HThanh-how/guard_stupid_antigravity, thực hiện theo yêu cầu của chủ laptop. Bản upstream tại thời điểm tải chỉ hỗ trợ macOS/Linux.

## Cài đặt

Dùng Python đã có trên máy và đường dẫn AGY CLI **hiện có**:

```powershell
py -3.13 -X utf8 install.py --dry-run --agy "$env:LOCALAPPDATA\agy\bin\agy.exe"
py -3.13 -X utf8 install.py --agy "$env:LOCALAPPDATA\agy\bin\agy.exe"
```

Installer không cài/cập nhật/chạy hay đăng nhập CLI. Nó ghi các hook, agents, skills và block policy vào `.gemini` của người dùng Windows; sao lưu trước khi ghi. Đường dẫn Python và CLI được lưu trong `config/hooks/runtime.json`. Không tạo symlink hay thay PATH trên Windows. Sau khi cài, khởi động lại Antigravity khi đã lưu công việc đang mở.

## Phần đã chuyển

- Hook gọi Python trực tiếp qua PowerShell, có deadline ngoài 8 giây; không cần Bash hoặc WSL.
- Khóa trạng thái bằng `msvcrt.locking`; giữ nguyên cơ chế khóa POSIX cho macOS/Linux.
- Mỗi lệnh/reviewer/worker CI có Windows Job riêng, gán trước khi process chạy. Khi quá hạn, hủy hoặc process chủ thoát, Windows dọn các process con trong Job đó.
- Đọc stdout/stderr bằng luồng đọc trên Windows, giữ mã thoát và deadline tổng/không có đầu ra.
- Lệnh gốc được mã hóa khi đi qua wrapper để giữ tiếng Việt, dấu nháy và khoảng trắng. Nhận diện executable `.exe`, `.cmd`, đường dẫn Windows và lệnh PowerShell.
- Yêu cầu reviewer dài được lưu trong file tạm thay vì vượt giới hạn dòng lệnh Windows; file được dọn sau khi kết thúc. Reviewer vẫn dùng CLI hiện có theo thiết kế gốc của kit.

## Kiểm tra cục bộ

```powershell
py -3.13 -X utf8 -m unittest -v test_install test_windows
py -3.13 -X utf8 payload/config/hooks/check-test-command.py
```

Các kiểm tra chạy offline trong thư mục tạm: giữ cấu hình/sao lưu/cài lại, đường dẫn có dấu và khoảng trắng, quoting, mã thoát, deadline, khóa đồng thời, dọn process con và bảo toàn process không liên quan, fail-closed, gate review/Git, role hạn chế, đầu vào reviewer dài và loại báo cáo chưa hoàn chỉnh. Không mở phiên AGY thật và không gọi GitHub.

Chúng xác nhận phần chuyển đổi và lệnh hook đã sinh, chưa xác nhận một phiên review AI/CI thật hoặc vòng đời hook trong giao diện Antigravity sau restart. macOS/Linux chưa được kiểm tra lại trên máy này. Hook vẫn là công cụ hỗ trợ, không phải sandbox bảo mật.

## Hoàn tác

Đường dẫn backup được installer in ra; `receipt.json` liệt kê file `created` và `replaced`. Đóng Antigravity, phục hồi các file `replaced` từ backup, chỉ xóa file `created` nếu chưa có thay đổi mới cần giữ. Không xóa cả `.gemini`. Khởi động lại ứng dụng sau khi phục hồi.
