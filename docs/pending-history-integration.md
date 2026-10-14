# History của pending previews thực

**Phạm vi lượt sửa tài liệu:** chỉ đối chiếu implementation, không chạy test,
khởi động server hoặc truy cập DB/thiết bị. Giữ số liệu các vòng cũ dưới nhãn
lịch sử; lần sau tách 392 PASS, 0 FAIL, 0 SKIPPED có nguồn là log terminal đã có
trong phiên, không phải test chạy lại ở lượt này. Giới hạn bằng chứng được
ghi riêng trong history-extension-verification.md.

## Hành vi và cách thử

Hai nút Undo/Redo của cột Thay đổi đang chờ nay dùng `/api/pending-history`.
Trạng thái hiện tại: backup 1.2, audit allowlist, state holder và commit recovery;
kết quả các vòng trước bên dưới là lịch sử kiểm thử, không phải kết quả vòng mới.
Kế hoạch thử UI dưới đây chưa thực hiện trên browser/topology; chỉ áp dụng khi
được phép dùng môi trường nháp test cô lập, không dùng server/thiết bị thật.
Đăng nhập ADMIN, tạo Preview hoặc chọn một Preview/batch đang chờ duyệt
của chính tài khoản đó, bấm **Sửa nháp**, sửa lệnh/mô tả rồi **Lưu nháp**.
Undo phục hồi nội dung trước; Redo phục hồi nội dung sau. Batch chỉnh toàn bộ con
trong một transaction. Không có lệnh SSH khi lưu/Undo/Redo/Restore.

Khi hook thành công, tạo Preview mới ghi CREATE_PENDING hoặc CREATE_PENDING_BATCH.
Hủy Preview/batch
chưa được duyệt tự ghi CANCEL_PENDING hoặc CANCEL_PENDING_BATCH. Undo tạo mới
đổi trạng thái bản ghi thành cancelled, không xóa. Redo đưa cùng ID về
pending_approval nếu đủ điều kiện. Undo hủy nháp đưa lại về pending_approval.
Nháp sau chỉnh sửa luôn cần Approve trước Apply và giữ Verify của hệ thống gốc.

History rỗng có baseline từ dữ liệu hiện tại, hai nút disabled. Preview có sẵn
từ trước khi server khởi động là baseline, không giả vờ có lịch sử tạo trước đó.
Sửa/hủy nháp đó sẽ tạo bước Undo mới. Không có thiết bị hoặc snapshot mẫu được
thêm vào môi trường local.

## Điểm chạm và quan hệ dữ liệu

- `changes/service.py`: create_preview/cancel thêm capture_before trước mutation
  và record_after sau commit; mỗi hàm ba dòng hook.
- `changes/batch_service.py`: tương tự ở create_batch_preview/cancel_batch;
  hook bắt cả batch do AI tạo vì AI dùng cùng service.
- `history_extension/routes.py`: đăng ký pending blueprint idempotent bên cạnh
  API draft-history cũ.
- `index.html`: include editor riêng và thêm nút mở editor cho owner của bản nháp.
- Logic mới nằm trong pending_service.py, pending_routes.py, history-panel.js,
  history-toolbar.js, pending_editor.html và CSS extension.

Ranh giới commit dự kiến: giữ implementation/helpers/tests History và protection
trong History commit. Hai hàm test core redaction/Preview audit (7+1 cases) đã
chuyển nguyên vẹn sang tests/audit/test_audit_hardening.py; file audit mới có
helper Preview và guard riêng, không phụ thuộc helper trong History test module.
Audit/service.py và các case này cần review/commit audit riêng sau History vì
audit dùng protection.redact_sensitive_data. Không sửa implementation trong
bước tách. Lần chạy sau tách đã đạt 392 PASS, 0 FAIL, 0 SKIPPED trong 172,12 giây,
gồm 384 case History/manager và 8 audit mới trên worktree hiện tại có audit core
đang sửa đổi. Số liệu 392/43 trong log trước tách giữ nguyên; không tuyên bố
snapshot History-only với audit bản HEAD đã được xác minh. Audit/service.py và
file test audit mới không thuộc danh sách 33 file dự kiến commit History.

### HistoryState và publication một generation

`HistoryManager._state` giữ `HistoryState(histories, expected, revision_epoch,
generation, needs_reconcile)`. `histories` chứa các topology với snapshot,
journal riêng/công khai, actor aliases và stacks. `expected` là fingerprints
raw của các hàng được theo dõi, bao gồm cancelled keys cần cho Redo. Manager
standalone dùng expected=None; pending workspace dùng dict và topology default.
`generation` đếm publication RAM; HMAC editor dùng `revision_epoch`, không dùng
trực tiếp generation. Hai bộ đếm không đồng nghĩa và không phải DB version.

Push/checkpoint/Undo/Redo/Restore chuẩn bị candidate copy-on-write. `fork()` copy
sâu state, có lock mới và giữ cipher/key scope; không deepcopy cipher hoặc lock.
`_prepare_publication()` tạo target với history/tracking copies, epoch đã chuẩn
bị, generation=base.generation+1 và needs_reconcile=False. `_publish()` giữ nguyên
manager/cipher/lock, gọi `_install_state(base, target)` với điểm publish payload
duy nhất `self._state = target`. `_install_state()` từ chối base không còn đúng;
nếu target đã là live state thì retry không đổi generation hay tạo event.

Operation pending đồng bộ giữ workspace RLock và manager RLock; phương thức
manager giữ manager RLock. Các đọc liên quan vì thế thấy cùng generation trong
operation. Holder frozen không làm các dict bên trong bất biến; private views
`_histories`/`expected` không bảo vệ caller sửa trực tiếp hoặc đọc ngoài protocol.
Giới hạn này khác với việc candidate/fork không alias mutable live state.

`synchronize()` đọc DB, ghim base, nhận diện snapshot/fingerprint lệch, thiếu
active keys hoặc dirty state, rồi chuẩn bị candidate trước publish. Điều kiện
active keys là subset của expected, không phải equality: extra cancelled keys
hợp lệ không làm mất Redo. Workspace mới tạo baseline; GET bình thường không
checkpoint. Reconcile lập checkpoint giữ journal, đặt baseline mới và xóa Redo.

Receipt `PublicationAttempt(base, target, source)` và cờ `_recovery_flag` ở
workspace, không ở holder hoặc backup. Recovery hiệu lực là OR của cờ này,
receipt chưa acknowledge và state.needs_reconcile. `_acknowledge()` chỉ gỡ
receipt tương ứng khi live state chính là target; `status()` synchronize thành
công, kiểm tra hết receipt/dirty state rồi mới xóa cờ riêng. Không đòi export
hoặc HTTP response đã thành công mới xóa cờ.

| Fault/trạng thái | Live payload và lần GET tiếp theo |
| --- | --- |
| Lỗi đọc DB, fork, fingerprint, history hoặc packet preparation trước publish | Giữ base, không có candidate publication; recovery đã bật không bị xóa. GET sau thử reconcile lại. |
| Lỗi install trước đổi reference | Giữ base và receipt. Nếu target khớp DB, GET retry đúng target. |
| Lỗi sau đổi reference hoặc trước acknowledgement | Giữ target hoàn chỉnh và receipt. GET xác nhận DB rồi acknowledge; không tăng epoch hoặc ghi checkpoint trùng. |
| Callback sau commit fail liên tiếp | DB đã commit không bị rollback; giữ receipt, bật recovery và trả 409 commit_outcome_unknown. GET xác nhận target trước retry. |
| DB đổi sau commit đã biết, target không còn khớp | Giữ event đã commit của receipt source=committed_db, reconcile dữ liệu DB hiện tại bằng barrier mới. |
| GET bình thường hoặc GET lặp sau recovery thành công | Giữ cùng state nếu DB/tracking không đổi, không checkpoint/event mới. |

Nếu live state không còn là base hoặc target của receipt, code trả conflict,
không coi publication đó đã thành công. Exception safety của packet RAM không
là transaction nguyên tử giữa SQLite và RAM; cũng không giải quyết crash process
hoặc nhiều worker. Standalone manager dùng cùng copy-on-write/install nhưng không
có pending receipt/recovery. Backup/export giữ nguyên projection public 1.2 và
archive 1.0; không serialize holder/generation/receipt hoặc revision key.

Snapshot pending-v1 chứa ID, danh tính đích frozen, lệnh, execution_mode,
verification và mô tả của các ChangeRequest thuộc owner; ChangeBatch chứa ID
và mô tả. Membership batch lấy từ batch_id, phải gồm toàn bộ con. Snapshot
chỉ chứa trạng thái nháp đang hoạt động; các bản ghi bị Undo/hủy vẫn tồn tại.

Trước một thao tác lịch sử, backend đối chiếu tất cả cột của các bản ghi đã
theo dõi với fingerprint kỳ vọng. UPDATE có điều kiện compare-and-swap trên các
cột đó; parent/children cùng transaction. Backend kiểm tra snapshot kết quả
**trước commit**. Stack được stage bằng HistoryManager.fork và chỉ publish sau
commit được xác nhận thành công. Lỗi validation/CAS hoặc commit được xác nhận
chưa hoàn tất không dịch chuyển stack live của mutation extension. Outcome
commit chưa rõ được xử lý riêng ở mục recovery; không coi rollback là đảo ngược
transaction đã commit.

Những field về risk, confirmation, capability, verification plan, rollback và
warnings được sinh lại bằng prepare_change; JSON import không quyết định quyền
phê duyệt. Verification nội bộ trong snapshot được sinh lại và so khớp, không
gửi trở lại qua validation dành cho lệnh client. Verification mới do client
yêu cầu vẫn qua policy gốc; show running-config tùy ý bị từ chối.

Khi status/synchronize nhận thay đổi DB ngoài journal, checkpoint có thể giữ
audit cũ nhưng đặt baseline mới và xóa redo. Undo/Redo/Restore stage history;
conflict trước commit không tự publish checkpoint của clone vào history live.
Bản ghi approved/running/success/failed, hoặc có approved_at,
applied_at, backup/output đều không được mở lại thành pending. Bản đã duyệt
rồi cancelled cũng không thể phục hồi. Frozen target thay đổi yêu cầu Preview
mới; child bị thiếu hoặc batch không nhất quán bị từ chối.

## Đồng bộ UI và concurrency

Không gán snapshot vào pendingChanges/pendingBatches/changesById/batchesById.
Controller nhận snapshot xong tăng các generation/request token hiện có để
loại response GET cũ, rồi gọi refreshChanges và refreshBatches trong app.js gốc.
Các getter/filter pending và card chat tiếp tục dùng dữ liệu canonical từ DB.

Watcher revision của hai map cập nhật trạng thái history sau khi Preview/cancel
hoàn tất; các callback trùng được gộp thành một request. Timer 15s là dự phòng.
Lỗi refresh hiển thị và khóa nút tới lần refresh thành công. Logout hủy timer,
controller và editor. Editor lỗi giữ dữ liệu nhập để operator sửa lại.

Extension tạo request lock riêng cho từng Flask app trong process, khóa các HTTP
request pending/lifecycle/AI thuộc phạm vi middleware để edit
không xen vào giữa read và commit của Approve gốc. Khóa được release cả khi JWT
hoặc thao tác thất bại. Batch action đang chạy ở UI chặn mutation history mới.

## API

Tất cả cần JWT ADMIN đang hoạt động; owner lấy từ database, không từ body.
Pending schema hiện chưa có trường topology nên API chỉ nhận topology_id=default;
không giả lập nhiều topology trên cùng bản ghi. API JSON generic vẫn hỗ trợ
topology độc lập và không cập nhật DB.

| Method | Endpoint | Payload |
| --- | --- | --- |
| GET | /api/pending-history | snapshot, can_undo, can_redo, diff |
| POST | /api/pending-history/undo | không cần body |
| POST | /api/pending-history/redo | không cần body |
| GET | /api/pending-history/changes/id/editor | draft canonical và revision trong cùng một response |
| PATCH | /api/pending-history/changes/id | revision bắt buộc; commands?, description?, verification_commands?, execution_mode? |
| GET | /api/pending-history/batches/id/editor | toàn bộ batch và revision |
| PATCH | /api/pending-history/batches/id | revision, description và changes gồm mỗi child đúng một lần |
| GET | /api/pending-history/audit | audit_only, restore_supported=false |
| GET | /api/pending-history/backup | JSON attachment |
| POST | /api/pending-history/restore | backup JSON |

Public envelope `backup_version="1.2"` chỉ có `backup_kind`, `exported_at`,
`timezone`, `key_scope`, `audit_trail` allowlist, `history_state` và
`encrypted_history` bên cạnh version. Field snapshot trong public audit là
summary/counts; actor là bí danh, mô tả cố định, action là enum hoặc OTHER.
Không có topology_id, cấu hình hoặc metadata gốc ở lớp public.

Archive Fernet bên trong encrypted_history hiện dùng schema nội bộ
`backup_version="1.0"`, chứa exported_at/timezone/topology_id/key_scope,
audit với snapshot/metadata gốc và history_state. Các stack là chỉ số bắt đầu
từ 0 của event, tương ứng public audit summary; Restore lấy cấu hình từ archive,
không lấy summary làm snapshot. Reader 1.1 vẫn cần cùng khóa/projection cũ.
Restore giải mã và xác thực cả ciphertext lẫn projection metadata; audit-only không Restore
được. JSON 1.0 chỉ chấp nhận khi không phát hiện credential plaintext và snapshot
không chứa marker đã che. Mô tả audit đã che không làm mất dữ liệu snapshot.
Khóa mặc định chỉ ở RAM process, không xuất trong backup/log/response. Process
mới sau restart tạo khóa mặc định mới, không giải mã backup dùng khóa cũ nếu
thiếu đúng khóa đó. Caller có thể truyền backup_key vào HistoryManager, nhưng
pending API chưa có provisioning/rotation/retention khóa bền vững. Không gọi
tính năng này là disaster recovery. Cùng khóa chỉ đáp ứng điều kiện giải mã;
Restore chỉ chấp nhận ID đã được theo dõi
trong workspace của owner hiện tại. Các snapshot mà undo/redo có thể truy cập
được kiểm tra lại trước write; không nhập row mới/owner/device/batch membership.
Archive audit nằm sau checkpoint vẫn được giữ nhưng không trở thành trạng thái
có thể Undo. Restore không phục hồi thiết bị hoặc tự phê duyệt.

## Phạm vi kiểm thử và kết quả lịch sử

Các test mới ở tests/history/test_pending_history.py và test_pending_ui.py.
Test UI dùng app.js thật cho refreshChanges/refreshBatches và getter pending,
controller thật, API Flask thật, DB SQLite test trong RAM. DOM và đăng ký Alpine
là test double; không tuyên bố đã kiểm tra trực tiếp trình duyệt.

Bao phủ tạo/sửa/hủy → Undo → Redo, nhiều bước, nhánh mới, batch nhiều con,
Backup/Restore và redo còn lại, history trống, payload lỗi, partial batch,
identity đổi, approved/running/terminal và approved-then-cancelled, rollback CAS
giữa batch, commit lỗi, quyền/owner, refresh cũ đến muộn và request lock.
Test history chặn SSH/Apply/execute-readonly/capture_backup; không seed hay chạm
database local. Device fixtures chỉ nằm trong DB RAM của test suite.

**Kết quả lịch sử của vòng tích hợp pending:** **463 passed** cho lệnh regression
của vòng đó (48 test History,
415 test changes/AI/chat/health/security); **12 passed** cho unittest
HistoryManager; hai harness history_toolbar_test.cjs/history_panel_test.cjs pass.
Kiểm tra cú pháp Python/JavaScript và `git diff --check` pass. Vòng pending đầu
có 7 fail do đưa verification nội bộ vào validation client; đã sửa và chạy lại.
Test UI từng fail ở so sánh thứ tự ID API giảm dần với getter map tăng dần;
đã so tập ID vì thứ tự đó không phải quan hệ dữ liệu cần bảo vệ. Không còn test
fail trong các lệnh cuối.

Các file runtime đã thay đổi/bổ sung trong vòng tiếp tục:

- `backend/src/network_copilot/history_extension/history_manager.py`
- `backend/src/network_copilot/history_extension/pending_service.py`
- `backend/src/network_copilot/history_extension/pending_routes.py`
- `backend/src/network_copilot/history_extension/routes.py`
- `backend/src/network_copilot/changes/service.py`
- `backend/src/network_copilot/changes/batch_service.py`
- `backend/src/network_copilot/static/js/history-toolbar.js`
- `backend/src/network_copilot/static/js/history-panel.js`
- `backend/src/network_copilot/static/css/history-toolbar.css`
- `backend/src/network_copilot/templates/index.html`
- `backend/src/network_copilot/templates/history_extension/pending_editor.html`

Test/harness: thêm `backend/tests/history/test_pending_history.py`,
`backend/tests/history/test_pending_ui.py`,
`backend/extension_tests/pending_api_ui_harness.cjs`; cập nhật
`backend/extension_tests/history_api_ui_harness.cjs` để kiểm tra generic API như
trước và `backend/extension_tests/history_panel_test.cjs` để bảo vệ watcher.
Tài liệu: cập nhật `docs/history-extension-integration.md`,
`docs/history-extension-verification.md`; thêm tài liệu này. `app.py` vẫn giữ
đúng các điểm chạm từ vòng trước, không sửa lại trong vòng tiếp tục.

Không dùng lại lệnh regression rộng của vòng cũ: các suite changes/AI/audit/
security có thể chứa Apply hoặc thao tác thiết bị. Phạm vi History mục tiêu đã
được review là tests/history và extension_tests/test_history_manager.py, nhưng
chỉ chạy sau khi xác nhận runner/fixture/child-process guards còn đúng.
Runner phải chỉ tạo app với TestConfig, xác nhận engine SQLite :memory: và
monitoring=false, chặn disk DB/socket/DNS/server/SSH/Apply/device verification
trước collection và từng test. Dùng virtualenv đã có, -B, tắt bytecode/cache/
plugin tự nạp/coverage/report. Nếu chưa chứng minh được guard thì UNKNOWN/chưa
chạy, không thay bằng lệnh pytest trực tiếp. Hướng dẫn nhóm độc lập nằm trong
[history-extension-integration.md](history-extension-integration.md).

Giới hạn: history/revision/recovery nằm trong RAM; request lock riêng từng Flask
app trong process. Chưa hỗ trợ
nhiều worker, lifecycle gọi trực tiếp ngoài request hoặc process khác chỉnh DB.
CAS ngăn ghi đè khi dữ liệu
đã thay đổi; các thay đổi ngoài journal làm history bị chặn/checkpoint. Khi
restart chỉ baseline pending hiện tại được khởi tạo; backup tham chiếu ID đã
mất/không còn được theo dõi bị từ chối. Cơ chế CAS được kiểm chứng trên SQLite;
chưa kiểm chứng các database khác. JSON audit không có chữ ký chống giả mạo và
không được đồng bộ vào bảng AuditLog gốc. Chưa có UI chọn file Backup/Restore;
API/controller hỗ trợ exportBackup/restore, hai nút Undo/Redo và editor đã nhúng.

Không commit, push, merge, deploy hoặc khởi động lại ứng dụng local trong lần này.

## Bảo vệ editor, secret và child ID — 2026-10-13

Mục này lưu contract/kết quả của vòng hardening trước state holder. Các mô tả
backup 1.1 và lỗi commit phía dưới cần đọc cùng contract final review và mục
publication/receipt hiện tại phía trên; không thay thế public envelope 1.2.

Đây là báo cáo vòng hardening trước. Kết luận về public schema và atomicity ở
phần này được thay thế bởi mục “Sửa các phát hiện final review” phía dưới.

Editor đọc GET .../editor, giữ revision cùng nội dung và gửi revision khi PATCH.
Revision là HMAC nội dung hàng DB, các con batch, danh tính thiết bị và generation
của workspace. Không trả hash trực tiếp của secret. Hai editor cùng mở: lần lưu
thứ nhất thành công; lần sau trả 409 với reason=stale_revision, reload_required=true.
UI giữ dữ liệu chưa lưu và yêu cầu mở lại editor. Undo/Redo/Restore, tạo/hủy nháp
tăng generation, kể cả khi nội dung quay về giống trước (ABA). Revision thiếu
hoặc sai định dạng trả 422. GET editor không tự ghi baseline hoặc journal.

PATCH kiểm tra trước synchronize. History/expected được stage riêng và chỉ publish
sau commit DB. Lỗi revision, validation, policy, CAS hoặc commit không di chuyển
journal/stack live. Thay đổi bên ngoài vẫn dùng checkpoint trong status/Undo;
lần lưu stale không tự tạo checkpoint. Child ID phải là int dương, không nhận
bool/string/float/list/object/null; lỗi 422 có trường changes.N.id.

Snapshot gốc giữ riêng trong RAM để Undo/Redo/Restore. Journal audit che toàn bộ
commands và mô tả tự do; diff che giá trị commands/description/credential. Backup
1.1 giữ ciphertext đầy đủ, Restore ghi cấu hình canonical gốc, không ghi marker
đã che vào DB. /audit không chứa ciphertext và có restore_supported=false.
Che mật khẩu IOS type 0/7, secret, token/API key, community, pre-shared-key,
key-string và các trường credential. Không coi regex là bộ nhận diện mọi secret
tùy ý: không đưa secret vào ID/topology/actor/action_type hoặc metadata khác không
được định nghĩa là credential. API editor/status ADMIN vẫn chứa nội dung cần sửa.

Điểm chạm core mới: audit/service.py che trọn suffix secret gồm encoding type,
che mọi danh sách lệnh cấu hình kể cả cú pháp vendor chưa biết, che mô tả Preview,
bỏ traceback/SQL parameters khi audit thất bại. Single readonly-command audit
giữ contract cũ. Không đổi schema/database/quy trình duyệt. Endpoint History dùng
thông báo lỗi cố định; log chỉ ghi tên loại lỗi, không echo lệnh hoặc exception.

### Request lock theo Flask app: chỉ phân tích, không sửa

Mỗi Flask app tạo một request RLock riêng. App before_request lấy lock đó cho
mọi /api/pending-history, POST/PATCH
/api/changes*, /api/change-batches* và POST /api/ai/chat. Teardown nhả khóa cả khi
JWT/endpoint lỗi. Thứ tự hiện tại: request_lock → registry lock ngắn (nhả trước
xử lý workspace) → workspace RLock → HistoryManager RLock. RLock cho phép
status/synchronize/current_snapshot lồng nhau trong cùng thread. Test probe từ
thread khác xác nhận Undo/Approve cùng giữ khóa và nhả sau lỗi xác thực. Chưa đo
latency hoặc chứng minh không deadlock ở mọi đường gọi. Không thấy thứ tự ngược
trong luồng đã đọc; caller tương lai giữ workspace rồi chờ request lock có thể
tạo thứ tự ngược. RLock không giải quyết chu trình chờ giữa nhiều thread.

Khóa bao toàn request, gồm AI provider, Apply/SSH/Verify, đọc DB, copy lịch sử,
mã hóa backup và validation Restore. Một request dài chặn người dùng khác và
request polling thuộc phạm vi khóa; acquire không timeout, không bảo đảm fairness.
Đây không phải khóa OS cho mọi app/job trong process. Không bảo vệ nhiều worker
hoặc service gọi ngoài HTTP. Không chạy AI provider/Apply hoặc benchmark thiết bị.

### Kết quả lịch sử — bằng chứng vòng hardening

| Phạm vi | PASS | FAIL | SKIPPED |
| --- | ---: | ---: | ---: |
| HistoryManager | 12 | 0 | 0 |
| History/API/pending trước vòng này | 48 | 0 | 0 |
| Hardening concurrency/secret/ID | 43 | 0 | 0 |
| Preview/batch preview/model | 69 | 0 | 0 |
| Audit core | 34 | 0 | 2 |
| Chat UI, health, security | 36 | 0 | 1 |
| Tổng Python | 242 | 0 | 3 |
| Hai harness JavaScript độc lập | 2 | 0 | 0 |

Hai test Python roundtrip còn chạy harness API/UI bằng pipe với app.js thật,
DB RAM và DOM/Alpine test doubles; không phải browser/E2E. Ba test bị bỏ:
test_change_lifecycle_is_audited, test_failed_apply_is_audited_as_failure,
test_invalid_state_keeps_its_own_error_code vì gọi endpoint Apply. Runner chặn
socket/DNS, SQLite ngoài :memory:, SSH thật và Apply. Số lần chạm guard đều 0.
Không cài package, không đọc database local hoặc khởi động server. Cú pháp
Python/JavaScript và git diff --check đạt ở vòng xác minh cuối.

Trước PR: kiểm tra browser trong môi trường nháp cô lập khi có topology test;
kiểm tra cache JS và hai editor; thống nhất format 1.1 và lifecycle khóa backup;
giữ phạm vi một worker, rồi thiết kế/kiểm chứng thay thế khóa nếu cần mở rộng.
Chưa có bằng chứng kiểm thử trình duyệt hoặc topology thật trong vòng này.

### File thay đổi trong vòng hardening

- `history_extension/history_manager.py`: tách snapshot gốc khỏi journal đã che,
  export/import mã hóa, audit-only và diff được bảo vệ.
- `history_extension/protection.py` (mới): redaction và envelope Fernet.
- `history_extension/pending_service.py`: revision HMAC/generation, editor DTO,
  validation ID, stage history trước DB commit và log không chứa exception text.
- `history_extension/pending_routes.py`: editor/audit endpoints và lỗi JSON an toàn.
- `history_extension/routes.py`: thông báo backup/parser lỗi không echo input.
- `static/js/history-panel.js`: lấy nội dung cùng revision, gửi revision, giữ
  editor khi conflict và bỏ response mở editor thuộc phiên cũ.
- `audit/service.py`: tăng bảo vệ audit core; không đổi schema/luồng Apply.
- `tests/history/test_history_hardening.py` (mới): 43 trường hợp bảo vệ.
- `tests/history/test_pending_history.py`: dùng editor-open/revision và fixture
  legacy không secret để giữ kiểm thử semantic Restore.
- `extension_tests/test_history_manager.py`: format mã hóa, legacy và kiểm tra
  atomicity theo trạng thái/audit thay vì ciphertext ngẫu nhiên; 12 test PASS.
- `extension_tests/pending_api_ui_harness.cjs`: await editor-open, hai editor,
  kiểm tra conflict giữ input và DB không bị ghi đè.
- `extension_tests/history_api_ui_harness.cjs`: audit description đã che và lỗi
  generic không echo input; Restore vẫn kiểm tra snapshot gốc.
- Ba tài liệu `history-extension-integration.md`,
  `history-extension-verification.md`, `pending-history-integration.md`.

Các thay đổi có sẵn ở app.py, changes/service.py, changes/batch_service.py và
templates/index.html được giữ nguyên trong vòng này. git diff --stat chỉ đếm
tracked files; phần lớn extension vẫn untracked, cần review danh sách đó khi PR.
Giới hạn payload Restore hiện vẫn 2 MiB; lịch sử export tăng lớn có thể vượt
giới hạn này. Chưa thêm retention, pagination hoặc bộ lọc kích thước backup.

## Contract sau sửa final review và kết quả lịch sử — 2026-10-13

Phần này thay thế các kết luận hardening trước về atomicity, audit công khai và
kích thước export. Không đổi kiến trúc request/workspace/manager RLock, không sửa
core Apply/Approve, schema database, môi trường hoặc file khóa.

### Revision và phạm vi writer

PATCH chụp bản sao tất cả hàng của owner lúc kiểm tra revision. Sau staging phải
so snapshot và raw rows với bản sao đó; nếu khác trả 409, không checkpoint live.
Điều kiện CAS vẫn dùng bản đọc đã ghim, không nhận expected mới từ ORM refresh.
UPDATE ChangeRequest kiểm tra thêm danh tính Device bằng EXISTS trong cùng câu
SQL, nên Device cache cũ không cho phép ghi nháp vào đích đã đổi.

Trong cùng process, request lock và workspace RLock vẫn tuần tự hóa các luồng
được bảo vệ. Test có writer thực trong thread khác dùng Session riêng và DB RAM;
writer ngoài lock bị phát hiện, không ghi đè. Với SQLite, conditional UPDATE và
write transaction giữ điều kiện đến commit; BUSY/LOCKED trả conflict. Writer đến
sau commit là thao tác mới, được phát hiện ở lần đọc tiếp theo. Không thể buộc một
writer SQL tùy ý, ghi mù ngoài extension, tuân theo revision của extension.

History/revision key/request lock chưa chia sẻ giữa process. Token editor của
worker khác bị từ chối; không tuyên bố hỗ trợ multi-worker history. Chưa chạy test
hai process cùng ghi một database: phép thử đó không thể chia sẻ SQLite :memory:
độc lập trên Windows theo cách đang dùng. Các interleaving trước CAS được tái
hiện bằng writer RAM và thread. Mutation ở dialect khác SQLite bị từ chối 409
cho đến khi có kiểm chứng transaction tương ứng; không tự truy cập backend khác.

### Commit và recovery

Response JSON và expected hậu cập nhật được tính trước commit. Listener
after_commit riêng cho Session được chèn đầu hàng đợi; khi DB commit thành công,
listener publish cùng receipt/target đã chuẩn bị qua một lần đổi reference state,
không thay manager. Callback `publish` nội bộ ở apply hiện là factory tạo receipt
trước commit; các caller move/edit dùng contract này. Đường callback thành công
không đọc DB hoặc synchronize thêm sau commit. Listener được gỡ trong finally.

Nếu callback lỗi trước/sau install, `_apply()` retry bằng cùng receipt một lần;
không chạy lại thao tác tạo journal hoặc Undo/Redo/Restore. Nếu vẫn lỗi, giữ
receipt, bật recovery và trả 409. GET đọc DB để xác nhận packet trước retry,
không phải publish lại một candidate mới nếu packet cũ còn khớp. Idempotence này
áp dụng cho callback retry cùng packet, không cho mọi lần gửi lại HTTP mutation.

Lỗi response preparation, CAS, đọc trước commit hoặc commit chưa thực hiện:
rollback transaction và giữ live journal/stack. Lỗi listener khác hoặc wrapper
phát sinh sau callback commit: giữ trạng thái đã publish, đóng Session để cleanup
và trả response thành công đã chuẩn bị. Không coi rollback là đảo ngược commit.

Nếu driver ném lỗi sau commit vật lý nhưng trước after_commit, chỉ cleanup
Session rồi đọc lại để xác định kết quả. Nếu toàn bộ raw rows khớp target đã chuẩn
bị, publish và trả thành công. Nếu DB còn nguyên bản trước thao tác, trả lỗi và
giữ history. Nếu không xác định được, trả 409 reason=commit_outcome_unknown,
reload_required=true, đặt recovery_required và chặn editor/PATCH/Undo/Redo/Restore.
`PendingWorkspace.status()` synchronize DB, kiểm tra không còn receipt hoặc
needs_reconcile rồi xóa cờ riêng. GET
`/api/pending-history`, `/backup` và `/audit` đều gọi method này, có thể reconcile/
checkpoint và mở lại các entry points bị chặn. Cờ có thể đã được xóa sau
synchronize thành công, trước khi export hoặc HTTP response hoàn tất; không
đòi toàn bộ GET phải trả HTTP 200 mới xóa cờ. Nếu synchronize lỗi thì chưa đến
bước xóa cờ. History RAM trước reconcile không phải trạng thái DB đã xác nhận.

Cờ chỉ check trong move/editor/edit của pending extension, không fence tất cả
core writers, không check trong method apply nội bộ nếu caller gọi trực tiếp,
và không áp dụng cho API generic draft-history độc lập. Core create/cancel có
thể tiếp tục: capture_before không check cờ, có thể synchronize/checkpoint;
capture lỗi trong hook trả None, record_after lỗi sau core commit chỉ log tên
loại lỗi rồi trả về. Hai hook dùng user_id người thực hiện, không tự fence owner
của mọi bản ghi core được thao tác. `record()` có thể publish dirty generation
qua cập nhật epoch/history trước khi expected hoàn tất. Lỗi sau core create đã
commit và push_state đã cập nhật RAM không rollback DB hoặc xóa event đã ghi;
GET nhận diện needs_reconcile/thiếu active tracking rồi checkpoint và cập nhật
tracking trên candidate hoàn chỉnh. Hai regression core-create lỗi sau RAM push
giữ nguyên và có trong lần chạy 392. Không khẳng định mọi core writer hay mọi
hook mutation đều all-or-nothing hoặc mọi fault tùy ý phục hồi đầy đủ. Đây không
phải transaction chung của core và history RAM.

Crash process, mất kết nối khiến không thể đọc lại, hoặc writer khác ghi tiếp
khi outcome đang mơ hồ vẫn là giới hạn của RAM history. Không tuyên bố có durable
transaction chung giữa SQLite và RAM. Không thử gây crash/mất kết nối thiết bị.

### Restore lỗi và checkpoint

Backup được kiểm tra trong manager clone trước đọc/synchronize workspace. Undo,
Redo và Restore stage toàn bộ thay đổi lịch sử; lỗi malformed/key/ciphertext,
validation hoặc revision/data conflict trước commit không đổi live journal,
undo/redo hoặc epoch. Restore không tự checkpoint/xóa Redo khi phát hiện dữ liệu
bên ngoài. GET status vẫn được phép checkpoint để invalidation lịch sử cũ, và
GET phục hồi commit outcome mơ hồ có thể tạo checkpoint được mô tả phía trên.

### Backup, khóa và allowlist

Export mới: backup_version=1.2. Public envelope chỉ gồm backup_version/backup_kind,
exported_at, timezone cố định, key_scope, audit_trail allowlist, history_state
và encrypted_history. Archive bên trong dùng backup_version=1.0, chứa
exported_at/timezone/topology_id/key_scope, snapshot/metadata gốc và stacks.
Chỉ số stack bắt đầu từ 0 của event; public audit chỉ giữ summary tương ứng,
không dùng summary để Restore cấu hình.
Lớp public không có topology/user/custom metadata dạng rõ. Audit giữ step/timestamp hợp lệ,
actor-N, action_type thuộc enum hoặc OTHER, mô tả cố định, số lượng thuộc tính
hoặc pending/batch, số add/remove/replace. Không copy ID, command, description,
hostname, khóa dict tùy ý hoặc giá trị cấu hình vào public audit. Diff che mọi
before/after; chỉ tên path thuộc allowlist được giữ, segment khác cũng bị che.

Snapshot, actor, action/mô tả gốc và metadata cần Restore nằm trong archive riêng
trong RAM rồi mã hóa bằng Fernet. API editor/status ADMIN có chủ ý trả snapshot
gốc để chỉnh sửa; không phải API audit. Không mở rộng chính sách redaction của
API core cũ trong vòng này. Những response/log core ngoài History vẫn cần review
chính sách riêng, không coi regex hiện có là bảo vệ mọi secret trong project.

Reader giữ 1.1 với projection xác thực cũ và cùng khóa, rồi xuất lại bằng 1.2 nếu
caller yêu cầu export; không tự đổi file cũ. JSON 1.0 không chứa credential đã
nhận diện/marker redacted vẫn được kiểm tra và chấp nhận. Không có nhận diện
toàn bộ secret trong dữ liệu legacy tùy ý; output mới dùng allowlist để tránh
phụ thuộc vào khả năng đó. Actor/thời gian import vẫn là khai báo từ archive,
không phải chứng cứ xác thực danh tính; event RESTORE lấy actor hiện tại.

Khóa mặc định không thay đổi thiết kế: tạo một lần khi import, ở RAM process,
không ghi file/log/audit/response. Nó mất khi restart; process Python mới không
đọc backup default đã dùng khóa cũ nếu không còn đúng khóa đó.
CREDENTIAL_ENCRYPTION_KEY chỉ phục vụ credential hiện hữu;
không tái sử dụng hay thay cấu hình khóa đó ở đây. HistoryManager(backup_key=...)
cho phép caller quản lý cùng khóa; báo cáo lịch sử ghi kiểm thử đọc ở process
mới qua pipe, nhưng
pending routes chưa có provisioning/rotation/retention khóa bền vững. Reader 1.1
không cứu được backup đã mất khóa. Không quảng bá backup pending là dài hạn
hoặc disaster recovery. Cùng khóa không bỏ qua validation owner/ID/eligibility/
frozen target của pending Restore.

MAX_BACKUP_BYTES=2*1024*1024 dùng chung ở manager và cả hai API. Export kiểm tra
bytes UTF-8 của toàn JSON, bao gồm ciphertext và audit; vượt ngưỡng trả 422
backup_too_large, không trả file/ciphertext một phần và không truncate lịch sử.
Import nhận đúng ngưỡng, từ chối thêm một byte. Archive lớn vẫn giữ trong RAM
nhưng không export được; retention/phân đoạn chưa có và cần thiết kế riêng.

### Kết quả lịch sử — kiểm thử final review và điều kiện trước PR

File mới tests/history/test_history_final_regressions.py có 82 trường hợp:
writer giữa check/staging/CAS, thread khác, Device đổi; edit/Undo/Redo/Restore
standalone và batch tại lỗi trước/sau commit, driver commit mơ hồ; Restore lỗi
khi còn Redo; backend chưa kiểm chứng; audit metadata/keys opaque; reader 1.1;
process mới có/không cùng caller key; threshold export/import thực 2 MiB và UTF-8.
Trong vòng kiểm thử lịch sử được báo cáo, test dùng fixture RAM, runner chặn
SQLite ngoài :memory:, socket/DNS,
SSH/Apply và device verification; guard xác nhận trước từng test.

Các số liệu sau là lịch sử, không phải test chạy trong lượt sửa tài liệu.
Vòng mục tiêu đầu 170 PASS, 1 FAIL vì assertion cũ đòi marker REDACTED trong
backup. Audit allowlist đã bỏ cấu hình, nên đổi assertion sang schema tóm tắt,
diff có marker và Restore vẫn giữ nguyên dữ liệu. Vòng regression kế tiếp
313 PASS/3 SKIPPED trước khi thêm recovery driver; vòng mục tiêu tiếp theo 80
regression mới sau recovery đã PASS. Vòng cuối có thêm guard history rỗng và
header allowlist: 324 PASS/3 SKIPPED, trong đó 82 regression mới PASS. Hai harness
JavaScript độc lập PASS; syntax/whitespace/diff-check PASS. Kết quả chi tiết được
ghi trong history-extension-verification.md.

File runtime sửa trong vòng này: pending_service.py, history_manager.py,
protection.py, pending_routes.py, routes.py. Test/harness cập nhật để kiểm tra
private restore và public schema: test_history_manager.py, test_history_routes.py,
test_history_boundaries.py, test_history_hardening.py, test_pending_history.py,
history_api_ui_harness.cjs. Ba tài liệu được cập nhật. App factory, hooks core,
audit/service.py, app.js và UI runtime được giữ nguyên trong vòng này.

Trước commit/push/PR: review toàn bộ untracked; thống nhất thay đổi contract 1.2,
actor alias/public snapshot summary, size error 422 và phạm vi một worker;
quyết định provisioning khóa/retention nếu cần backup dài hạn; kiểm thử browser
hai tab, batch, Undo/Redo và Restore trên topology test cô lập. Chưa có bằng chứng
browser, topology thật, shared DB multi-process hoặc dialect khác SQLite.

## Các giới hạn chưa được kiểm chứng

Multi-process cùng DB, concurrency stress, deadlock/fairness, latency thực,
browser/topology và durable crash recovery: UNKNOWN. Probe RAM/thread và lỗi
driver/listener giả không chứng minh durable recovery khi process/máy bị crash.
Không thay đổi kiến trúc lock hoặc tự cấp phát khóa bền vững trong lượt tài liệu.

Ma trận từng còn thiếu về pending entry points, ba GET reconcile/read/export
lỗi, core hooks và lock release nay có các cases cụ thể trong 113 recovery và
94 publication tests. Lần sau tách ghi tổng 392 PASS, 0 FAIL, 0 SKIPPED, gồm
384 case History/manager và 8 audit mới trên worktree có audit core đang sửa đổi;
không chứng minh snapshot History-only dùng audit bản HEAD đã được xác minh.
không có test chạy trong lượt tài liệu. Command/phạm vi và giới hạn nguồn log ở
[history-extension-verification.md](history-extension-verification.md). Không
cộng các kết quả này với 324/463 của vòng lịch sử hoặc suy ra mọi writer/fault
đã được bao phủ. Hai UI roundtrip dùng pipe/test doubles, không phải browser.
Suite `tests/audit/test_audit.py` không nằm trong command sau tách; file audit
hardening mới có 8 PASS. Audit core vẫn cần review/regression an toàn chọn lọc;
không khẳng định mọi log/response core được bảo vệ.

Các test về sau phải kiểm tra DB, journal/stacks, fingerprints, epoch/generation,
receipt và recovery theo từng fault; không ép mọi core hook lỗi giữ nguyên RAM.
Khả năng giải mã một backup thực tế trước
restart khi chưa xác minh đúng khóa: UNKNOWN, không tự đọc backup/DB để suy đoán.
