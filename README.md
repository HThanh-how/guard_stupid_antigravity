# Antigravity control kit

Bản xuất bộ kiểm soát đang dùng trên máy của HThanh, ngày **19/10/2026**. Mục tiêu: giảm bỏ sót yêu cầu, sửa triệu chứng, làm yếu test, báo hoàn thành thiếu bằng chứng và chờ agent/lệnh vô hạn. Đây là cấu hình cá nhân, không phải tính năng bảo đảm của Antigravity.

## Cài trên máy khác

Hiện hỗ trợ **macOS/Linux**. Bộ hook dùng Bash, `fcntl`, process groups và POSIX signals; chưa hỗ trợ Windows native. WSL không đồng nghĩa hook sẽ được app Windows nạp.

1. Cài Antigravity và **AGY CLI chính thức**, đăng nhập riêng trên máy mới. Cần Python 3.9+, Bash, Git. Theo dõi GitHub CI cần `gh` và đăng nhập GitHub. Không chuyển token từ máy cũ.
2. Tải repo và xem trước cấu hình:

```bash
git clone https://github.com/HThanh-how/guard_stupid_antigravity.git
cd guard_stupid_antigravity
python3 install.py --dry-run --agy "$(command -v agy)"
```

3. Cài và khởi động lại Antigravity:

```bash
python3 install.py --agy "$(command -v agy)"
```

Nếu `command -v agy` không có kết quả, cung cấp đường dẫn **CLI thật** bằng `--agy /duong/dan/agy`. Không trỏ vào file `.app` hoặc tải CLI từ nguồn không xác định. Installer không tải phần mềm, không đăng nhập, không chạy AGY và không thay credential.

Installer sao lưu file sẽ thay, gộp hai nhóm hook của kit vào `~/.gemini/config/hooks.json`, giữ nhóm hook khác, thêm policy bằng block có đánh dấu vào `~/.gemini/GEMINI.md`, và cài agents/skills. Nếu đã có file cùng tên, bản trong kit sẽ thay file đó sau khi sao lưu. Nó tạo link `~/.local/bin/agy` đến CLI đã chọn nếu link/file chưa tồn tại; không thay CLI hiện có. Nên kiểm tra rule cũ vì các rule mâu thuẫn vẫn có thể gây chặn.

Kiểm tra các thành phần được nạp trong app sau khi restart. Schema hook/tool phụ thuộc bản Antigravity: kiểm tra một tác vụ thử trong **repo dùng một lần**, không lấy repo thật làm nơi thử quyền. Chưa có xác nhận chạy end-to-end trên máy mới.

## Có gì trong gói

| Thành phần | Vai trò |
| --- | --- |
| `payload/GEMINI.md` | Policy kỹ thuật toàn cục, tiếp tục yêu cầu gốc qua compaction, báo bằng chứng và giới hạn |
| `payload/config/hooks.json` | PreToolUse, Pre/PostInvocation, PostToolUse, Stop |
| `review-gate.py` và hai `.sh` | Gate test/Git, lưu trạng thái review, khởi tạo yêu cầu reviewer |
| `independent-agent.py` | Chạy reviewer/test-author/classifier/CI agent qua CLI có deadline |
| `bounded-command.py`, `command-limits.json` | Giới hạn tổng thời gian, thời gian không có đầu ra và dọn process con |
| `test-command.py` | Nhận diện lệnh thực sự chạy test; tránh coi việc đọc tên file test là chạy test |
| `user-priority.py`, `authorize-git.py`, `interactive-permission.py` | Diễn giải quyền Git từ lời người dùng thật, không đòi câu thần chú |
| `git-branch-policy.py`, `merge-readiness.py` | Kiểm tra nhánh/đích push và bằng chứng sẵn sàng merge |
| `consequence-review.py` | Thu yêu cầu gốc, kiểm tra hệ quả dữ liệu và thứ tự async |
| `ci-monitor.py` | Theo dõi CI hữu hạn theo đúng repo/SHA/run ID |
| `check-*.py` | Các bài kiểm tra hồi quy của bộ hook, không phải trạng thái đã duyệt |
| `config/agents/*/agent.md` | 4 agent chuyên trách bên dưới |
| `config/skills/*/SKILL.md` | 10 skill đang dùng, gồm debugging, verification, consequences, GenOffice và Ponytail |

Đường dẫn thực tế sau cài là `~/.gemini/config/hooks/<tên file>`. Tên `genoffice` được giữ để khớp agent/hook hiện có; phần guard chung không chỉ áp dụng repo GenOffice. Skill GenOffice cần cài GenOffice riêng; gói này không chứa bộ cài ứng dụng.

## Bốn agent

- **genoffice-test-author**: đọc yêu cầu/baseline, đề xuất diff test; không sửa file hoặc chạy lệnh. Parent áp dụng diff rồi đưa reviewer độc lập xem.
- **genoffice-reviewer**: chỉ có công cụ đọc/tìm kiếm; kiểm tra toàn bộ thay đổi liên quan, edge case, độ trung thực test và bằng chứng UI. Không giả vờ đã chạy test.
- **git-intent-classifier**: phân loại lời cấp quyền thật bằng JSON, không công cụ hành động. Cho commit không tự suy ra cho push.
- **ci-status-monitor**: gọi worker CI chỉ đọc, hữu hạn một lần; không sửa code, restart CI, commit hoặc push.

## Các hành vi được kiểm soát

1. **Yêu cầu người dùng**: yêu cầu/corrections mới có ưu tiên hơn các mặc định cá nhân này. Không tự bỏ yêu cầu cũ chỉ vì tin nhắn mới ngắn hoặc context bị rút gọn. Phải báo việc chưa làm/blocked/unverified. Điều này không ghi đè chính sách hệ thống hoặc giới hạn công cụ của app.
2. **Tìm hiểu luồng chạy**: ghi symptom → contract → entrypoint/caller/callee → thay đổi trạng thái/IPC/lưu trữ → kết quả thực tế. Grep dùng để định vị; đọc vài dòng không đủ để xác nhận nguyên nhân.
3. **Giải pháp gốc**: tái dùng luồng/thư viện/chuẩn sẵn có, đọc caller và nhánh lỗi trước khi sửa. Không thêm kiến trúc, phụ thuộc, hotfix hoặc tính năng ngoài phạm vi chỉ để làm test xanh. Chưa có cơ chế máy chứng minh mọi giải pháp đã được nghiên cứu trên mạng.
4. **Test**: được sửa test khi đúng yêu cầu; phải giải thích thay đổi assertion/fixture/snapshot so với gốc. Reviewer xem skip/only, mock né production, giảm threshold, mất coverage và edge case. Không coi agent đang chạy, timeout, phản hồi rỗng là PASS.
5. **Dữ liệu/async**: xác định invariant, ownership, commit point; kiểm tra create/import/persist/replace/delete/rollback, asset dùng chung; fault injection và kết quả đến sai thứ tự khi phù hợp. Kiểm tra cả success/failure, dispose/switch và cleanup muộn.
6. **Git**: bình thường cần comprehensive review trước add/commit/push. Lời cấp quyền trực tiếp có thể được classifier diễn giải một lần, có scope và cache, không cần đúng một câu mẫu. Quyền không biến test chưa chạy thành PASS và không tự miễn kiểm tra readiness khi merge.
7. **Nhánh quan trọng**: snapshot hiện chặn commit/push tự động trên `main`, `master`, `develop`, `dev`, `production`, `prod`, `staging`, `release`, `release/*`, `releases/*` và remote default branch. Push bình thường chỉ cùng nhánh làm việc; không force/delete/mirror/all/tags. Đây vẫn là chặn cứng của cấu hình hiện tại, không có cam kết mọi lời cho phép sẽ mở hết.
8. **UI**: chạy màn hình thật, chụp và **mở xem** ảnh; desktop/narrow khi liên quan. Báo clipping, overlap, loading/error, viewport và đường dẫn bằng chứng. Không gọi có ảnh là đã xác minh.
9. **Dọn test**: finally/teardown đóng đúng app/process mình tạo; xóa fixture của tác vụ, giữ ảnh/log cần báo cáo. Không đóng app thường của người dùng hoặc xóa thư mục tạm không thuộc tác vụ.
10. **CI/agent**: một monitor độc lập hữu hạn; parent làm việc khác không đổi scope/SHA. Nếu chỉ còn chờ thì báo PENDING; không tạo vòng polling hoặc agent lồng vô hạn.

Hook là cơ chế hỗ trợ, không phải sandbox an ninh hoàn chỉnh hay bằng chứng rằng AI không thể bỏ sót prompt. Cần xem báo cáo, diff, log và kết quả thật.

## Giới hạn thời gian hiện tại

- Lệnh: `command-limits.json` đặt **600 giây tổng / 180 giây không có đầu ra**. Budget hợp lệ tối đa 3600 giây; `idle=0` tắt kiểm tra idle. Chỉ đổi khi có lý do và được người dùng cho phép.
- Reviewer CLI: thường 300 giây, có deadline process ngoài; trạng thái gate có deadline khoảng 6 phút, tối đa 2 lần thử. Timeout/incomplete phải chẩn đoán và báo BLOCKED, không tự lặp cùng yêu cầu.
- Classifier Git: 20 giây CLI / khoảng 30 giây process ngoài; không retry mù hoặc biến UNCLEAR thành cho phép.
- CI: worker thường 480 giây, caller budget 600 giây. PASS chỉ bao phủ run ID/SHA được báo, không suy ra mọi workflow đều xanh.
- Hook lifecycle chỉ chuẩn bị trạng thái; **không inject tool/message steps**. Đây là thay đổi tránh lỗi `unknown injected step type: <nil>` từng gặp.

Chi tiết có thể được xem trực tiếp trong source tương ứng; đây là snapshot cấu hình, không tự đồng bộ từ máy cũ.

## Lệnh vận hành

Khi hook chặn, dùng **đúng key và cwd trong thông báo**, không sao chép key của chat khác:

```bash
python3 ~/.gemini/config/hooks/independent-agent.py review KEY_TU_THONG_BAO
python3 ~/.gemini/config/hooks/independent-agent.py author /absolute/request.txt --cwd /absolute/repo
python3 ~/.gemini/config/hooks/authorize-git.py KEY_TU_THONG_BAO
python3 ~/.gemini/config/hooks/independent-agent.py ci-monitor --repo owner/repo --sha FULL_SHA --run-id RUN_ID --cwd /absolute/repo --seconds 600
```

File yêu cầu test-author phải có yêu cầu gốc, acceptance contract, baseline, lỗi quan sát và phạm vi test. Lệnh reviewer cần chạy từ cwd được hook cung cấp. Không chạy các ví dụ với placeholder nguyên văn.

Nếu agent bị treo: xem trạng thái/deadline/log một lần, tôn trọng deadline, báo lỗi thật; không coi task ID là hoàn thành. Nếu hook yêu cầu cú pháp quyền cứng: xem classifier có chạy và kết quả gì, không buộc người dùng lặp một câu thần chú. Nếu ảnh không đọc được: báo UNVERIFIED. Đừng tự tắt guard để biến blocked thành pass.

## Giao việc cho Antigravity từ một AI khác

Mẫu ngắn:

> Mục tiêu: … Repo/cwd: … Baseline/SHA: … Yêu cầu gốc: … File được phép: … Không được phép: … Tiêu chí chấp nhận: … Tổng thời gian: … Deadline từng lệnh/agent: … Bằng chứng: … Báo PASS/FAIL/BLOCKED/UNVERIFIED. Không retry vô hạn. Cuối cùng đóng đúng app/process test và dọn fixture của mình.

Tách chủ sở hữu production, người thiết kế test và reviewer. Chỉ chia song song khi file/phạm vi độc lập; một integrator giữ giao diện chung và Git. UI thay đổi phải có ảnh đã xem. Với native app, gửi qua UI/connector được hỗ trợ rồi xác nhận tin nhắn thật xuất hiện và tác vụ bắt đầu; đừng tin thao tác click nếu chưa quan sát kết quả. Không mang index UI, task ID, conversation ID, đường dẫn log hay quyền của máy cũ sang máy mới.

## Sao lưu và gỡ

Installer in đường dẫn `~/.gemini/control-kit-backups/<timestamp>/`. `receipt.json` ghi file nào đã thay, file nào mới tạo, có tạo link CLI hay không. Bản gốc trước mỗi lần cài nằm tại cùng đường dẫn tương đối trong thư mục backup.

Để hoàn tác: đóng Antigravity, phục hồi các file `replaced` từ backup; chỉ xóa file `created` của kit nếu chưa chứa thay đổi mới cần giữ. Gỡ link CLI **chỉ khi receipt ghi kit đã tạo nó và link vẫn trỏ đúng CLI đã chọn**. Không xóa toàn bộ `~/.gemini`. Khởi động lại app. Khi restore `hooks.json`/`GEMINI.md`, giữ lại những thay đổi bạn đã thực hiện sau lúc cài.

## Không được mang sang máy khác

Gói đã loại `config.json`, MCP/server credentials, OAuth/token, lịch sử chat/brain, project registry, state review, authorization receipts, task-evidence, CI cache, node_modules và backup cá nhân. Máy mới phải có trạng thái review/quyền mới; không sao chép PASS hay quyền cũ.

`SHA256SUMS.json` ghi checksum payload; installer kiểm tra trước khi ghi. Checksum kiểm tra tính nhất quán gói, không phải chữ ký xác thực tác giả. Snapshot đã đổi đường dẫn tuyệt đối của agent CI sang `~/.gemini/...`; các tên agent/nhóm hook còn giữ nguyên để tương thích.
