# Kết quả kiểm tra History Extension

**Phạm vi tài liệu:** các số liệu, bảng PASS/FAIL/SKIPPED và mô tả runner bên dưới
ghi riêng từng vòng. Mục state holder đối chiếu kết quả lần chạy trước từ log
terminal có sẵn trong phiên; các mục lịch sử cũ không phải kết quả chạy lại.
Lượt sửa tài liệu này chỉ đọc implementation, không chạy test, khởi động server,
truy cập DB hoặc thiết bị. Không có artifact CI/JUnit/log độc lập kèm manifest
hash trong repository để tái xác minh mọi kết quả cũ ngoài phiên làm việc này.

## Contract đối chiếu từ mã nguồn, không phải kết quả test mới

- `protection.audit_view/encode_backup` xuất public envelope 1.2: backup_version,
  backup_kind, exported_at, timezone, key_scope, audit_trail summary, history_state
  và encrypted_history.
  Public không có topology_id hoặc cấu hình gốc. `HistoryManager.export_backup_file`
  mã hóa archive schema nội bộ 1.0 với topology, snapshot/metadata gốc và stacks.
  Chỉ số stack bắt đầu từ 0; public audit có field snapshot là summary, không
  dùng summary đó để Restore. Reader 1.1 cần cùng khóa/projection cũ.
- Editor pending standalone/batch đã được tích hợp. Flask test client/controller
  harness qua pipe có DOM/Alpine test doubles; browser/topology thực tế: UNKNOWN.
- `HistoryState` giữ history/tracking/revision trong cùng generation. Writer chuẩn
  bị candidate copy-on-write; `_install_state()` publish payload duy nhất bằng
  `self._state = target`. Operation đồng bộ giữ khóa để đọc cùng generation.
  `_publish()` giữ identity manager/cipher/lock; receipt retry dùng cùng target.
- `PendingWorkspace.move/editor/edit` check recovery_required, chặn
  Undo/Redo/Restore, mở editor và PATCH pending. Recovery hiệu lực là OR của cờ
  riêng, receipt chưa acknowledge và needs_reconcile trong holder. `status()`
  synchronize DB, kiểm tra receipt/dirty state rồi xóa cờ riêng. GET status,
  backup và audit đều gọi status(); cờ có thể đã xóa trước
  export/HTTP response hoàn tất. Synchronize lỗi thì chưa đến bước xóa cờ.
  Reconcile có thể checkpoint/xóa Redo khi canonical DB khác journal.
- Core create/cancel không check cờ: capture/record hooks bắt lỗi bên trong,
  log tên loại lỗi rồi cho core tiếp tục. Capture còn có thể synchronize/
  checkpoint. Không tuyên bố mọi core writer bị fence hoặc journal luôn giữ
  nguyên khi hook đã đổi RAM một phần. Core-create sau DB commit/RAM push nhưng
  chưa cập nhật expected được regression hiện có kiểm tra; lỗi tùy ý khác vẫn
  không có cam kết phục hồi đầy đủ. Xem ma trận coverage state holder bên dưới.
- `protection._PROCESS_CIPHER` dùng khóa mặc định chỉ ở RAM. Process mới sau
  restart có khóa mới, không giải mã backup cũ nếu thiếu đúng khóa cũ. Pending
  API chưa cấp phát/luân chuyển khóa bền vững; không phải disaster recovery.
  Caller key của HistoryManager không phải tính năng provisioning của pending API.
- Request lock riêng từng Flask app, không có timeout; RAM history/revision/
  recovery không chia sẻ worker. Pending history writes chỉ chấp nhận SQLite;
  caller ngoài HTTP không tự có request lock. Multi-process cùng DB, concurrency
  stress, deadlock/fairness, latency thực và durable crash recovery: UNKNOWN.
- Public History audit/diff dùng allowlist/masking; private archive mã hóa phục
  vụ Restore đầy đủ. Không suy ra mọi metadata/log/response core đều an toàn.

Chi tiết hiện tại: [history-extension-integration.md](history-extension-integration.md)
và [pending-history-integration.md](pending-history-integration.md).

## Verification state holder trước tách từ log có sẵn — không chạy lại trong lượt tài liệu

Nguồn bằng chứng là log terminal của lượt implementation và command runner đã
lưu trong phiên, không chỉ dòng tổng kết. Final read-only review trước lượt này
đã đối chiếu hash của 34 file với snapshot cuối của lần chạy đó. Lượt tài liệu
này đọc lại hai implementation và test publication/recovery, khớp nội dung đã
review; không chạy lại test hoặc tạo artifact log. Thiếu artifact độc lập trong
repository nên khả năng tái xác minh kết quả này ngoài phiên hiện tại là UNKNOWN.
Không dùng các số liệu 63/242/324/463 ở mục lịch sử để thay thế bằng chứng này.

Command được ghi nhận chạy từ `backend`, dùng virtualenv đã tồn tại, Python
`-B` với guard nạp qua stdin. Lời gọi pytest bên trong runner là:

```python
pytest.main([
    '-q', '--tb=no', '--capture=sys', '--show-capture=no', '-p', 'no:cacheprovider',
    'tests/history', 'extension_tests/test_history_manager.py',
], plugins=[Safety()])
```

Đây là trích command lịch sử, không phải lệnh chạy độc lập an toàn: `Safety()`
và guard trước collection phải có đầy đủ. Runner chỉ cho phép TestConfig/SQLite
`:memory:`/monitoring=false, chặn mở DB trên đĩa, file writes/cache, socket/DNS/
server bind, SSH/Apply/device verification; child process được review riêng.
Log ghi `392 passed in 332.90s (0:05:32)`, exit code 0, `safe_test_checks=392`,
không ghi nhận guard violation; FAIL=0, SKIPPED=0 trong đúng phạm vi sau:

| File trong lần chạy sau state holder | PASS | FAIL | SKIPPED |
| --- | ---: | ---: | ---: |
| `extension_tests/test_history_manager.py` | 12 | 0 | 0 |
| `tests/history/test_history_boundaries.py` | 18 | 0 | 0 |
| `tests/history/test_history_final_regressions.py` | 82 | 0 | 0 |
| `tests/history/test_history_hardening.py` | 43 | 0 | 0 |
| `tests/history/test_history_publication.py` | 94 | 0 | 0 |
| `tests/history/test_history_recovery_hooks.py` | 113 | 0 | 0 |
| `tests/history/test_history_routes.py` | 7 | 0 | 0 |
| `tests/history/test_history_ui_roundtrip.py` | 1 | 0 | 0 |
| `tests/history/test_pending_history.py` | 21 | 0 | 0 |
| `tests/history/test_pending_ui.py` | 1 | 0 | 0 |
| Tổng của command này | 392 | 0 | 0 |

**Tách phạm vi test sau lần chạy trên:** chuyển nguyên hai hàm audit core (7
parameter cases redaction và 1 case Preview audit) từ test_history_hardening.py
sang tests/audit/test_audit_hardening.py. File mới có helper Preview/guard cục bộ;
opened/journal và các test History không đổi. Bảng 43/392 phía trên giữ nguyên vì
đó là log trước tách, khi audit/service.py đã có modification. Lần chạy sau tách
đã xác nhận 35 case History hardening, 384 History/manager và 8 audit mới đều PASS,
được ghi riêng dưới đây; không thay số liệu hoặc viết lại log lịch sử phía trên.
Audit hardening dự kiến commit riêng sau History, dùng helper protection đã có.
Snapshot History-only với audit bản HEAD vẫn chưa được kiểm chứng. Runner sau
tách phải giữ guard trước collection và chỉ chọn file/node audit an toàn;
không chạy nguyên thư mục audit có các test Apply.

### Verification sau tách — lần chạy mới nhất từ worktree hiện tại

Đã dùng lại runner Safety lưu trong phiên và virtualenv cũ; chỉ bổ sung selection
`tests/audit/test_audit_hardening.py` bên cạnh `tests/history` và
`extension_tests/test_history_manager.py`, không thay cơ chế guard. Guard được
kích hoạt trước collection, chỉ cho TestConfig/SQLite :memory:, chặn DB trên
đĩa, network/server bind, SSH và các đường Apply/Verify/thiết bị. Child process
được đọc lại; UI dùng pipe với Flask test client, không phải browser/server.

Log mới ghi `392 passed in 172.12s (0:02:52)`, exit code 0,
`safe_test_checks=392`, không có guard violation; 0 FAIL và 0 SKIPPED.

| File sau tách trong lần chạy mới | PASS | FAIL | SKIPPED |
| --- | ---: | ---: | ---: |
| `extension_tests/test_history_manager.py` | 12 | 0 | 0 |
| `tests/history/test_history_boundaries.py` | 18 | 0 | 0 |
| `tests/history/test_history_final_regressions.py` | 82 | 0 | 0 |
| `tests/history/test_history_hardening.py` | 35 | 0 | 0 |
| `tests/history/test_history_publication.py` | 94 | 0 | 0 |
| `tests/history/test_history_recovery_hooks.py` | 113 | 0 | 0 |
| `tests/history/test_history_routes.py` | 7 | 0 | 0 |
| `tests/history/test_history_ui_roundtrip.py` | 1 | 0 | 0 |
| `tests/history/test_pending_history.py` | 21 | 0 | 0 |
| `tests/history/test_pending_ui.py` | 1 | 0 | 0 |
| `tests/audit/test_audit_hardening.py` | 8 | 0 | 0 |
| Tổng của command sau tách | 392 | 0 | 0 |

Kết quả gồm 384 case History/manager và 8 case audit trên worktree hiện tại,
bao gồm audit core đang sửa đổi. Không tuyên bố snapshot History-only với
audit bản HEAD đã được xác minh. Commit History dự kiến giữ 33 file, loại
`backend/src/network_copilot/audit/service.py` và
`backend/tests/audit/test_audit_hardening.py`; không dùng việc loại hai file khỏi
danh sách để biến kết quả worktree thành bằng chứng cho snapshot đó. Không chạy
suite audit rộng, test_security.py hoặc test Apply/thiết bị. Không có test chạy
trong lượt cập nhật tài liệu; log mới vẫn chỉ có trong phiên, chưa có artifact
CI/JUnit độc lập trong repository. Browser/topology thật vẫn chưa kiểm thử.

`test_history_publication.py` có 94 cases: standalone/batch; lỗi chuẩn bị history,
fingerprints, revision/generation; lỗi trước/sau install và acknowledgement;
callback edit/Undo/Redo/Restore fail một lần hoặc liên tiếp; retry cùng receipt;
DB đổi sau commit đã biết; packet preparation trước commit; fork/alias/identity;
standalone manager push/checkpoint/Undo/Redo/Restore. Assertion kiểm tra DB,
journal/stacks, expected, epoch/generation, recovery/receipt và identity, không
chỉ xác nhận fault được kích hoạt. GET lặp giữ target không tạo checkpoint/event
trùng; PATCH revision cũ conflict và revision mới cập nhật dữ liệu canonical.

113 recovery cases bao phủ entry-point fence, ba GET reconcile/read failure,
export failure sau reconcile, core create/cancel fail-soft, actor/owner khác,
thiếu active tracking, cancelled keys/Redo, fork/checkpoint/fingerprint failure,
key reader và lock release/wait. Hai regression core-create lỗi sau RAM push
không sửa assertion hoặc bỏ qua; file recovery giữ nguyên qua lượt state holder.
Core recording vẫn có thể để lại dirty generation trước GET, không all-or-nothing.

Hai Python UI roundtrip gọi harness JavaScript qua pipe và DOM/Alpine test
doubles; không phải browser. Hai harness toolbar/panel độc lập không được chạy
lại riêng trong command 392; kết quả của chúng ở các vòng cũ vẫn là lịch sử.
Suite `tests/audit/test_audit.py` không nằm trong command này. Ở lần trước tách,
History hardening có test redaction core/preview, nhưng chưa chứng minh toàn bộ audit core tương
thích với thay đổi `audit/service.py`; cần review/regression chọn lọc riêng.

Publication exception safety và callback idempotence trong ma trận trên không
chứng minh transaction nguyên tử SQLite/RAM, multi-process safety hoặc durable
crash recovery. Browser thật, topology thật, concurrency stress, multi-process,
deadlock/fairness và latency thực: UNKNOWN. Backup dùng khóa RAM mặc định vẫn
không có provisioning bền vững của pending API.

## Kết quả lịch sử — luồng API JSON generic trước tích hợp pending

1. **Tạo snapshot:** caller phải cung cấp toàn bộ dict JSON qua
   `historyPanel.push(snapshot, actionType, description)` hoặc POST
   `/api/draft-history/states`. Không có baseline tự động.
2. **Lưu:** route lấy actor từ JWT, gọi `HistoryManager.push_state()`, copy sâu,
   ghi event với timestamp UTC+07:00, thêm chỉ số event vào undo stack và xóa redo.
   Các event của nhánh bị bỏ vẫn còn trong journal.
3. **Undo:** nếu undo stack có ít nhất hai trạng thái, chuyển trạng thái hiện tại
   sang redo và trả snapshot trước; ghi event UNDO. Không còn trạng thái thì API
   trả 409 và không ghi event giả.
4. **Redo:** lấy phần tử trên cùng của redo, chuyển vào undo và trả snapshot;
   ghi event REDO. Redo rỗng trả 409 và không ghi event giả.
5. **Backup:** GET `/api/draft-history/backup` trả JSON attachment có audit_trail
   và history_state giữ vị trí hai stack. Export không sửa cấu hình hay ghi event
   thao tác mới; exported_at là metadata lần xuất.
6. **Restore:** POST `/api/draft-history/restore` kiểm tra dữ liệu trước cập nhật,
   phục hồi stack/snapshot, nối journal đang có với journal import và ghi RESTORE
   của operator hiện tại. JSON sai không thay đổi lịch sử.
7. **Đồng bộ UI:** response API cập nhật can_undo/can_redo và gọi callback gán
   `draftSnapshot`, phát event `nc-history-snapshot`. Timer GET status mỗi 15s
   chỉ đọc history; không tạo snapshot. Logout/đổi user hủy controller và timer.

## Kết quả lịch sử — điểm còn thiếu ở vòng generic

Trong vòng generic được báo cáo, chỉ route `/states` gọi `push_state()`;
Preview/Approve/Apply, AI/chat và inventory chưa gọi module history. Test
`test_preview_does_not_automatically_create_history` xác nhận tạo Preview không
làm xuất hiện snapshot. Không coi monitoring snapshots hoặc các hàng pending
là trạng thái nháp.

`draftSnapshot` và event của extension chưa được một editor/vùng hiển thị nháp
trong ứng dụng sử dụng. Vì vậy Undo/Redo không làm danh sách pending đổi nội dung.
Muốn tích hợp tiếp cần định nghĩa schema/caller sở hữu nháp và baseline thực,
push sau mỗi chỉnh sửa nháp thành công, rồi editor nhận callback/event để render.
Nếu muốn Apply nháp đã restore, phải tạo Preview mới và giữ toàn bộ bước duyệt.

Lần kiểm tra này không sửa `pendingChanges`, `pendingBatches`, `changesById`,
`batchesById`, service core, database hoặc các điểm chạm app/template đã có.

## Kết quả lịch sử — lỗi đã tái hiện và sửa ở vòng generic

- Test JavaScript kích hoạt click bằng mã trên nút disabled vẫn gửi request.
  Handler nay kiểm tra trạng thái enabled và busy trước khi POST.
- Adapter phiên cũ chỉ kiểm tra token sau request; test xác nhận request đã được
  gửi bằng phiên mới trước khi ném lỗi. Nay kiểm tra token/quyền ADMIN trước và
  sau request.
- `exportBackup()` không kiểm tra controller đã hủy. Nay kiểm tra trước/sau
  request; destroy có tính idempotent và xóa thông báo lỗi cũ.

Chỉ sửa logic runtime trong `static/js/history-toolbar.js` và
`static/js/history-panel.js`. Bổ sung test và tài liệu ở các file riêng.

## Kết quả lịch sử — bằng chứng tự động ở vòng generic

| Phạm vi | Kết quả |
| --- | --- |
| `tests/history` | 26 passed, gồm API/controller roundtrip |
| `tests/chat/test_batch_ui.py`, `tests/test_health.py`, `tests/test_security.py` | 37 passed |
| `extension_tests/test_history_manager.py` | 12 passed |
| `extension_tests/history_toolbar_test.cjs` | passed |
| `extension_tests/history_panel_test.cjs` | passed |
| Kiểm tra cú pháp Python/JavaScript và `git diff --check` | passed |

Không dùng lại lệnh regression rộng của vòng cũ: test_security.py và một số test
audit/lifecycle gọi endpoint Apply. Chỉ chọn test đã review dưới runner có guard
RAM/network/SSH/Apply/Verify; nếu guard chưa được kiểm tra thì UNKNOWN/chưa chạy.
Phạm vi và điều kiện chạy an toàn nằm ở mục kiểm thử trong tài liệu integration.

Test UI roundtrip chạy `history_api_ui_harness.cjs` qua subprocess, trao đổi JSON
qua stdin/stdout với Flask test client và dùng HistoryManager thật. Snapshot
không được giả lập ở tầng API: được push, undo, redo, export và restore qua route
thật. DOM/đăng ký Alpine là test doubles. Không có browser hay socket mạng trong
test này. Test fixture tạo dữ liệu trong SQLite RAM, không seed hoặc sửa local DB
đang chạy. Không tạo thiết bị trong test roundtrip; các test Preview dùng fixture
thiết bị của suite gốc trong DB RAM.

Các test mới kiểm tra JWT thiếu/sai/hết hạn, ADMIN bị hạ quyền trong database,
user không tồn tại, cô lập thao tác theo user/topology, giới hạn payload, backup
lỗi, audit no-op, actor/timestamp, restore giữ redo và nhánh sau undo. Chặn các
hàm SSH/Apply/execute-readonly/capture_backup bằng monkeypatch để test thất bại
nếu history gọi chúng. Controller cũng chỉ được phép gọi `/api/draft-history`.

## Kết quả lịch sử — giới hạn của vòng generic

- Chưa xác nhận thao tác với dữ liệu thật trong trình duyệt localhost đang mở;
  các kết quả trên là từ test client/controller test.
- Ở vòng generic đó chưa có luồng tự ghi snapshot/editor pending; điểm thiếu
  này đã được tích hợp ở các vòng sau, không phải trạng thái hiện tại.
- Journal lưu RAM/JSON của extension, không phải bảng AuditLog core hoặc màn
  hình audit database. Journal mất khi restart và không chia sẻ nhiều worker.
- Actor/timestamp của event import là dữ liệu người dùng cung cấp, không có chữ
  ký xác thực. Event RESTORE ghi operator hiện tại, diff được tính lại.
- Export không ghi event audit EXPORT. Nếu yêu cầu kiểm toán cả tải backup,
  cần quy định riêng và bổ sung trong extension.

Không commit, push, merge hoặc deploy trong lần kiểm tra này.

## Kết quả lịch sử — vòng hardening 2026-10-13

Contract ở vòng hardening đã thay đổi so với vòng generic phía trên: editor
Pending đọc revision và bắt buộc gửi khi lưu; backup 1.1 chứa ciphertext đầy đủ
và audit đã che. /api/pending-history/audit là audit-only, không thể Restore.
Thiết kế, điểm chạm core và phân tích khóa được ghi trong
[pending-history-integration.md](pending-history-integration.md).

Vòng đầu: 91 passed, 6 failed. Hai assertion mới dùng lệnh chưa canonical hóa
(backend thêm configure terminal/end); sửa test để so với cấu hình canonical
đã lưu và kiểm tra secret còn chính xác. Bốn lỗi khác phát hiện guard legacy
đang từ chối mô tả audit đã che dù snapshot còn nguyên; sửa guard để kiểm tra
marker trong snapshot cần Restore. Không bỏ kiểm tra partial batch/identity/stack
hoặc hạ xác thực backup 1.1. Guard vòng đầu: network/disk_db/Apply/SSH đều 0.

Vòng cuối: 242 Python passed, 0 failed, 3 skipped; 2 harness JavaScript độc lập
passed. History gồm 12 manager, 48 API/pending/UI có sẵn và 43 hardening mới.
Regression gồm 69 Preview/batch/model, 34 audit và 36 chat UI/health/security.
Ba test có endpoint Apply bị bỏ theo phạm vi an toàn. Syntax và diff-check đạt.

Dùng virtualenv đã tồn tại, Python -B, pytest không cache/plugin tự nạp. Runner
chặn socket/DNS, mọi SQLite ngoài :memory:, Apply và SSH thật; mọi bộ đếm guard
đều 0. Hai test roundtrip UI/API chạy JavaScript qua pipe và app.js thật, với
DOM/Alpine test doubles; chưa kiểm thử browser/E2E hoặc topology thật. Không cài
package, chạm database local, seed, khởi động server hoặc thao tác Git xuất bản.

## Kết quả lịch sử — vòng sửa final review 2026-10-13

Kết quả 242 PASS ở trên thuộc vòng hardening. Báo cáo final review dưới đây cũng
là lịch sử, không phải test mới của lượt sửa tài liệu. Ở vòng đó đã giữ tracked
diff ở app factory,
audit core, hai changes services và template; sửa các file extension/test/docs
được liệt kê trong pending-history-integration.md.

| Nhóm được báo cáo trong vòng lịch sử final review | PASS | FAIL | SKIPPED |
| --- | ---: | ---: | ---: |
| test_history_final_regressions.py (mới) | 82 | 0 | 0 |
| HistoryManager, History/API/pending/UI có sẵn | 103 | 0 | 0 |
| Preview, batch-preview, batch-model | 69 | 0 | 0 |
| Audit | 34 | 0 | 2 |
| Chat UI, health, security | 36 | 0 | 1 |
| Tổng Python | 324 | 0 | 3 |
| history_toolbar_test.cjs và history_panel_test.cjs | 2 | 0 | 0 |

Ba SKIPPED: test_change_lifecycle_is_audited,
test_failed_apply_is_audited_as_failure, test_invalid_state_keeps_its_own_error_code,
vì gọi endpoint Apply. Chưa chạy E2E, seed, demo/smoke, server, device API,
SSH/Telnet hoặc device verification. Fixture tạo/xóa bảng chỉ ở :memory:.

Runner dùng virtualenv đã tồn tại, -B, không pytest cache/plugin tự nạp; bọc cả
sqlite3.connect và sqlite3.dbapi2.connect để chỉ nhận :memory:, chặn socket/DNS,
SSHClient._connect, Apply và run_verification. Trước từng test xác nhận TestConfig
RAM/monitoring=false và guard vẫn được cài. Bộ đếm được báo cáo ở vòng đó: 324 safety checks,
network=0, disk_db=0, device_or_apply_verify=0. Child process kiểm tra khóa dùng
pipe, -B và tự chặn network/database; không ghi khóa hoặc snapshot ra output.

Đối chiếu từng phát hiện theo kết quả lịch sử, không phải chạy lại trong lượt này:

- P1 concurrency: PASS cho writer chen ở staging, sau staging, trước CAS,
  writer thread khác và Device đổi. Response 409; không đổi live journal/stack;
  dữ liệu writer ngoài khóa được giữ. SQLite BUSY và dialect chưa kiểm chứng
  bị từ chối. Shared DB hai process và dialect khác SQLite: UNKNOWN/chưa hỗ trợ.
- P1 commit: PASS cho edit/Undo/Redo/Restore standalone/batch, lỗi response
  preparation, đọc trước commit, commit chưa thực hiện, postcommit read không
  được gọi, after_commit listener và wrapper lỗi. Lỗi trước commit giữ nguyên
  DB/history; đã commit trả kết quả thành công và history khớp DB.
- Driver commit vật lý rồi ném lỗi: PASS, xác nhận lại dữ liệu và publish.
  Khi query xác nhận bị lỗi: test báo cáo PASS về reason commit_outcome_unknown,
  chặn Undo/PATCH và reconcile qua GET status. Phạm vi guard các entry points
  và GET backup/audit được mô tả từ source ở đầu tài liệu; chưa suy rộng test
  này sang tất cả core writers hoặc mọi fault case của hook. Không coi đó là
  đã biết kết quả
  commit; crash process/disconnect thật: UNKNOWN, RAM không có durable recovery.
- P2 Restore: PASS, malformed/key/cipher/metadata/audit-only lỗi và external
  conflict không checkpoint/xóa Redo; cả workspace chưa có baseline và bản ghi
  có sẵn nhưng chưa tracked đều không làm đổi history khi bị từ chối.
- P2 key: PASS về lifecycle/reader compatibility; 1.1 cùng khóa vẫn Restore đầy
  đủ. Process mới không có khóa default bị từ chối, có cùng caller key thành công.
  Provisioning/rotation khóa bền vững của pending routes: UNKNOWN/chưa triển khai.
- P2 secret: PASS với metadata, actor/action, topology, khóa dict, header và
  stack metadata opaque giả. Public 1.2 là allowlist; private ciphertext Restore
  nguyên dữ liệu. Không kết luận mọi log/response core cũ đã an toàn.
- P2 size: PASS với ciphertext thực sát ngưỡng 2 MiB, thêm một byte, UTF-8 và
  HTTP error 422 backup_too_large; không trả export quá ngưỡng/không truncate.
- Giữ nguyên kiến trúc khóa: PASS qua đối chiếu source; latency/deadlock stress:
  UNKNOWN. Browser/topology thật: UNKNOWN; harness không thay thế browser.

Vòng mục tiêu đầu có 170 PASS/1 FAIL do contract REDACTED cũ; đã giải thích và
đổi assertion sang schema allowlist + diff redacted + exact Restore, không sửa
runtime để khớp assertion cũ. Vòng trung gian 313 PASS/3 SKIPPED. Không còn FAIL
trong lệnh cuối. Python AST, JavaScript syntax, scoped whitespace và
git diff --check đạt. Git còn 5 tracked modified và 27 untracked files; stat
chỉ tính tracked, cần review cả untracked trước PR.

Điều kiện còn thiếu trước commit/push/PR: review public contract 1.2 và actor
alias, giới hạn một worker/SQLite, vòng đời khóa và khả năng từ chối export lớn;
chốt chính sách API/log core ngoài phạm vi extension; kiểm thử browser trên
topology test cô lập. Nếu yêu cầu backup dài hạn/multi-worker phải có thiết kế
provisioning và storage được kiểm chứng trước khi quảng bá hỗ trợ. Chưa commit,
push, merge hoặc tạo PR trong vòng lịch sử đó.

## Trạng thái ma trận từng còn thiếu và giới hạn kiểm thử tiếp theo

Ở lượt sửa tài liệu trước, ma trận recovery cho editor/PATCH/Undo/Redo/Restore,
ba GET lỗi/reconcile/export, core hooks fail-soft và lock release còn là kế hoạch.
Hiện các tình huống cụ thể đó đã có trong test recovery/publication và command
392 được ghi riêng phía trên. Không suy rộng thành mọi writer/fault đều được
bao phủ hoặc mọi hook lỗi đều giữ nguyên RAM. Test mới về sau vẫn phải kiểm tra
DB, journal/stacks, tracking/revision và recovery theo từng fault point.

Regression audit core ngoài nhóm History, browser/topology cô lập, concurrency
stress, multi-process và durable crash recovery chưa được xác minh bởi command
392. Không chạy suite audit/lifecycle rộng có Apply để lấp khoảng coverage đó.

Các test khóa/reader hiện có là fixture và child process qua pipe, không chứng
minh backup thực tế trước shutdown còn đúng khóa. Khả năng đó: UNKNOWN. Tất cả
test mới chỉ được chạy sau xác nhận fixture/engine SQLite :memory:, monitoring
tắt và guard disk DB/network/device/server trước collection và từng test.
Không chạy lệnh rộng chứa test_security.py, Apply, SSH/Telnet/device verification,
seed/demo/smoke hoặc E2E chưa chứng minh cô lập. Lượt sửa tài liệu này không chạy test.
