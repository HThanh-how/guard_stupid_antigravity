# Nguồn và phạm vi bản xuất

Snapshot ngày 19/10/2026 từ cấu hình cá nhân đang cài của chủ repo:

- `~/.gemini/GEMINI.md`
- `~/.gemini/config/hooks.json`
- Chỉ các file `.py`, `.sh`, `command-limits.json` ở cấp đầu của `~/.gemini/config/hooks/`.
- Bốn thư mục agent trong `~/.gemini/config/agents/`.
- Mười thư mục skill trong `~/.gemini/config/skills/`.

Các source được giữ nguyên, ngoại trừ một đường dẫn home tuyệt đối trong hướng dẫn agent `ci-status-monitor` đã đổi sang `~/.gemini/`. Installer, README và bài kiểm tra installer được thêm riêng cho repo này. Không sửa cấu hình đang chạy trên máy gốc.

Một số skill được cài/tùy biến từ bộ skill khác (Ponytail, systematic-debugging, verification-before-completion, GenOffice). Giữ metadata/ghi chú nguồn trong từng file. Repo này không gán lại một giấy phép chung cho các nội dung bên thứ ba; kiểm tra nguồn và quyền sử dụng tương ứng trước khi tái phân phối chúng tiếp.

Không chứa thông tin đăng nhập, mã nguồn/bản cài Antigravity, lịch sử hội thoại, state review hoặc quyền đã cấp. Không bảo đảm tương thích mọi phiên bản Antigravity hay mọi hệ điều hành.

Kiểm tra offline installer:

```bash
python3 -m unittest -v test_install.py
```

Bài kiểm tra dùng home tạm và `true` (tìm qua PATH) làm executable giả; không chạy AGY, không gọi mạng, không cài vào home thật. Chúng xác nhận dry-run không ghi file, cấu hình cũ được giữ/sao lưu và cài lại không nhân đôi policy. Đây không phải kiểm thử end-to-end Antigravity trên máy mới.
