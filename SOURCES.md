# Nguồn và phạm vi bản xuất

Snapshot ngày 19/10/2026 từ cấu hình cá nhân đang cài của chủ repo:

- `~/.gemini/GEMINI.md`
- `~/.gemini/config/hooks.json`
- Chỉ các file `.py`, `.sh`, `command-limits.json` ở cấp đầu của `~/.gemini/config/hooks/`.
- Bốn thư mục agent gốc trong `~/.gemini/config/agents/`, cùng agent `conversation-attention` thêm cho bản thử ưu tiên câu hỏi.
- Mười thư mục skill trong `~/.gemini/config/skills/`.

Snapshot ban đầu giữ nguyên source, ngoại trừ một đường dẫn home tuyệt đối trong hướng dẫn agent CI đổi sang `~/.gemini/`. Bản thử tiếp theo bổ sung semantic attention checkpoint, nối PreToolUse và thêm policy trả lời câu chen ngang/báo ETA trung thực. Source đang chạy trên máy gốc chỉ được cập nhật sau kiểm tra và có backup, theo yêu cầu thử của chủ repo.

Một số skill được cài/tùy biến từ bộ skill khác (Ponytail, systematic-debugging, verification-before-completion, GenOffice). Giữ metadata/ghi chú nguồn trong từng file. Repo này không gán lại một giấy phép chung cho các nội dung bên thứ ba; kiểm tra nguồn và quyền sử dụng tương ứng trước khi tái phân phối chúng tiếp.

Không chứa thông tin đăng nhập, mã nguồn/bản cài Antigravity, lịch sử hội thoại, state review hoặc quyền đã cấp. Không bảo đảm tương thích mọi phiên bản Antigravity hay mọi hệ điều hành.

Kiểm tra offline installer:

```bash
python3 -m unittest -v test_install.py
```

Bài kiểm tra dùng home tạm và `true` (tìm qua PATH) làm executable giả; không chạy AGY, không gọi mạng, không cài vào home thật. Chúng xác nhận dry-run không ghi file, cấu hình cũ được giữ/sao lưu và cài lại không nhân đôi policy. Đây không phải kiểm thử end-to-end Antigravity trên máy mới.
