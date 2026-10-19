# Bản thử ưu tiên câu hỏi — 19/10/2026

## Kết quả

- 19 kiểm tra offline: transcript người dùng thật so với nội dung model, câu hỏi chen ngang, bằng chứng trả lời, tin mới đến trong lúc đánh giá, giữ câu hỏi qua lời “tiếp đi”, partial/ETA thiếu, một bước đọc đúng scope, timeout, deadline, chống lặp cùng input, tool `finish`, và PreToolUse thực tế.
- Hai kiểm tra installer: dry-run, backup/merge và cài lại không nhân đôi policy.
- Các regression có sẵn `check-review-gate.py`, `check-agent-monitor.py`: PASS.
- Bốn lượt AI thật, mỗi lượt chỉ một tình huống, tối đa 20 giây CLI + deadline process ngoài: PASS.

| Tin người dùng / phản hồi đang có | Kết quả quan sát |
| --- | --- |
| “Ủa sao lại phải chạy agent đó vậy?” / “Tôi sẽ xem rồi trả lời sau.” | QUESTION, chưa trả lời, không yêu cầu hủy |
| “Bạn dừng agent hiện tại giúp tôi được không, nhưng giữ tiến trình build nhé?” | STOP được nhận diện theo ý nghĩa; không tự thực thi hủy |
| Hỏi vì sao câu trích dẫn “dừng agent” bị hiểu thành hủy, đồng thời nói chưa bảo hủy | QUESTION; không có đồng ý hủy |
| “Nó còn bao lâu nữa? Đừng dừng nó, cho chạy tiếp.” / “Đang chờ CI. Tôi sẽ báo sau.” | Không yêu cầu hủy; chưa trả lời ETA, báo chờ thiếu thông tin |

Không phải mọi thử nghiệm ban đầu đều đạt. Lượt gộp bốn tình huống hết hạn; trace cho thấy phản hồi JSON chưa kết thúc task CLI. Đã thêm `finish` chỉ để kết thúc báo cáo, structured output và reasoning thấp cho router. Một lượt đơn tiếp theo lộ validator cục bộ chưa hỗ trợ boolean; đã sửa kiểm tra boolean nghiêm ngặt. Bốn lượt đơn cuối cùng đều hoàn tất và được validator kiểm tra; không lấy kết quả dở dang từ log làm PASS.

## Tự thử

Sau khi cài, restart Antigravity để nạp policy/agent mới. Trong repo dùng một lần, giao một việc có chờ hữu hạn để hook ghi mốc ban đầu, rồi **gửi ngay** câu hỏi chen ngang:

1. “Tại sao phải chạy agent này?” — phải trả lời mục đích; không tự hủy agent.
2. “Sao lâu vậy, còn bao lâu?” — phải nói chờ gì, elapsed nếu biết, khoảng còn lại có căn cứ hoặc chưa ước lượng được, deadline và mốc kiểm tra tới.
3. “Dừng agent này, giữ build chạy.” — hiểu hai scope riêng, không giết tất cả process.
4. Hỏi một câu có chữ “dừng” trong trích dẫn — không coi trích dẫn là lệnh hủy.

Nếu tin vẫn ở Queued Messages, nó chưa được giao; hook chưa thể thấy. Gửi ngay bằng UI nếu app hỗ trợ. Không dùng nút Stop/kill để giả làm thao tác gửi câu hỏi.

Kiểm tra offline:

```bash
python3 -m unittest -v test_install.py
python3 payload/config/hooks/check-conversation-attention.py
```

Kiểm tra semantic bằng AGY thật (cần đã cài agent, có dùng model; mỗi lệnh một tình huống):

```bash
python3 attention_live_cases.py --live --case why-is-not-cancel
python3 attention_live_cases.py --live --case polite-question-can-be-stop
python3 attention_live_cases.py --live --case quoted-stop-is-not-consent
python3 attention_live_cases.py --live --case eta-needs-basis-not-a-promise
```

Không chạy lại cùng trường hợp tự động nếu timeout. Lượt live ở đây chỉ đánh giá dữ liệu giả lập; không hủy tác vụ, sửa file người dùng hoặc vận hành app của bạn. Chưa xác nhận toàn bộ hành vi scheduler/Send Now trong mọi phiên bản native app hoặc trên máy khác. Phân loại ngữ nghĩa vẫn có thể sai, và checkpoint có escape hữu hạn; đây không phải sandbox bảo mật hay bảo đảm AI luôn trả lời đúng.

## Lỗi phát hiện từ phiên native thật

Ngày 19/10, câu hỏi “tại sao chạy e2e liên tục vậy” có một câu trả lời dài trong `PLANNER_RESPONSE` cùng `tool_calls`, nhưng ảnh của người dùng chỉ hiện lời báo assessment/status. Bộ lọc cũ đã nhận nhầm phần bị ẩn là câu trả lời. Bản sửa loại record có tool_calls hoặc truncated_fields, kiểm tra lại bằng chứng receipt cũ, và không nhận câu trả lời xuất hiện sau khi assessment bắt đầu. Ba regression mới tái hiện các lỗi này đã PASS. Không công bố nội dung transcript cá nhân. Bốn ca semantic phía trên vẫn chỉ xác nhận phân loại dữ liệu giả lập; chưa có xác nhận UI sau bản sửa từ người dùng.
