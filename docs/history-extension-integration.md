# Tích hợp Lịch sử Cấu hình, Undo/Redo và Backup/Restore

**Phạm vi cập nhật tài liệu:** chỉ đối chiếu mã nguồn, không chạy test trong lượt
sửa tài liệu này. Các số liệu cũ được giữ dưới nhãn kết quả lịch sử. Kết quả
Các lần 392 PASS trước và sau tách được ghi riêng từ log terminal có sẵn trong
phiên, không phải test chạy trong lượt này; nguồn và giới hạn bằng
chứng nằm trong [history-extension-verification.md](history-extension-verification.md).

**Đã tích hợp pending thực:** hai nút trên trang hiện dùng `/api/pending-history`.
**Vòng sửa final review:** backup xuất mới là 1.2 với audit allowlist; vẫn đọc 1.1
khi có cùng khóa. Xem mục “Sửa các phát hiện final review” trong tài liệu pending.
Hướng dẫn mới: [pending-history-integration.md](pending-history-integration.md).
Các ví dụ `/api/draft-history` phía dưới là API JSON độc lập; contract backup/audit
1.2 thay đổi public schema, reader vẫn giữ 1.1 khi còn cùng khóa;
không dùng `historyPanel.push()` của API đó để cập nhật pending database.

**Ranh giới commit sau tách test:** `tests/history/test_history_hardening.py`
giữ các test/helpers History; 7 parameter cases core redaction và 1 case Preview
audit được chuyển nguyên vẹn sang `tests/audit/test_audit_hardening.py`, có helper
Preview và guard cục bộ, không import helper test History. Thay đổi audit core
ở `audit/service.py` dự kiến review/commit riêng sau History. `protection.py`
vẫn thuộc History vì backup/diff/legacy reader cần nó. Lần chạy sau tách đã đạt
392 PASS, 0 FAIL, 0 SKIPPED trong 172,12 giây: 384 case History/manager và 8 case
audit mới. Kết quả đến từ worktree hiện tại, bao gồm audit core đang sửa đổi;
không chứng minh snapshot History-only dùng audit bản HEAD đã được xác minh.

## Cấu trúc hiện tại và phạm vi

Backend là package `network_copilot` dưới `backend/src`, Flask application factory
ở `app.py`. Các module chia theo nghiệp vụ: auth, devices, credentials, SSH,
commands, parsers, monitoring, changes, backups, audit, ai, chat, dashboard,
evaluation. SQLAlchemy/Flask-Migrate quản lý database. Scripts phục vụ seed,
triển khai và đánh giá; tests dùng fake SSH/AI.

Trang `/` dùng `templates/index.html`, `static/js/app.js`, Alpine vendored và
`static/css/app.css`. Trang `/dashboard` có template/controller riêng.
Thanh bên phải là `aside.sidebar.changes-panel`; tiêu đề dùng include extension.
`pendingChanges` lọc `pending_approval`; `pendingBatches` gồm cả `approved`.
Các map `changesById`/`batchesById` được cập nhật từ API và polling.

`changes/service.py` và `changes/batch_service.py` giữ quy trình
Preview → Approve → Apply → Verify, backup running-config trước Apply và audit
đã redact. `changes/rollback.py` cung cấp hướng dẫn rollback.

API JSON độc lập quản lý **cấu hình nháp JSON**. API pending mới cập nhật
ChangeRequest/ChangeBatch chưa từng được duyệt, không sửa inventory, credentials,
running-config hay audit database. Undo không rollback thiết bị hoặc yêu cầu đã phê duyệt.
Không được gán snapshot vào `changesById`/`batchesById`: polling sẽ ghi đè, còn
Approve/Apply vẫn dùng bản ghi gốc. Muốn gửi cấu hình đã phục hồi ra thiết bị,
nháp được kiểm tra lại bởi prepare_change và vẫn phải được duyệt/Apply qua API gốc.

Đã tích hợp vào `app.py` và `index.html` bằng các điểm chạm tối thiểu bên dưới.
`app.js`, `app.css` được giữ nguyên. Hai service changes có thêm hook nhỏ chụp
snapshot trước tạo/hủy và ghi sau commit. Không có dependency mới, migration
hoặc sửa `.env`/`wsgi.py`; mọi thay đổi chưa commit trước đó được giữ lại.

**Hiện tại:** có nút Sửa nháp cho Preview/batch thuộc user đang đăng nhập và còn
pending_approval. History cập nhật database, rồi gọi refreshChanges/refreshBatches;
không gán snapshot vào map/list pending. History RAM chỉ hỗ trợ một process và
pending topology mặc định. Cấu hình đã duyệt/Apply nằm ngoài phạm vi Undo/Redo.

## Các file mới

| File | Vai trò |
| --- | --- |
| `backend/src/network_copilot/history_extension/history_manager.py` | Logic Python, thread lock, JSON validation, diff, journal; dùng cryptography đã có |
| `backend/src/network_copilot/history_extension/routes.py` | API opt-in, ADMIN đang hoạt động, lịch sử riêng từng user |
| `backend/src/network_copilot/templates/history_extension/toolbar.html` | Tiêu đề cùng hàng với hai nút SVG và thông báo lỗi |
| `backend/src/network_copilot/static/css/history-toolbar.css` | Dark theme, tooltip kể cả khi disabled, focus bàn phím |
| `backend/src/network_copilot/static/js/history-toolbar.js` | Controller độc lập có callback nhận snapshot |
| `backend/src/network_copilot/static/js/history-panel.js` | Component Alpine con, sở hữu `draftSnapshot` riêng |
| `backend/extension_tests/` | Kiểm tra độc lập, không truy cập lab/database |
| `backend/tests/history/test_history_routes.py` | Kiểm thử API Flask thật bằng database test trong RAM |
| `backend/tests/history/test_history_boundaries.py` | JWT, đổi quyền, cô lập, input lỗi và chặn SSH/Apply |
| `backend/tests/history/test_history_ui_roundtrip.py` | Nối controller JavaScript với API thật bằng pipe, không dùng socket |

## 1. Dùng logic độc lập

```python
from network_copilot.history_extension import HistoryManager

history = HistoryManager()  # giữ instance trong suốt vòng đời workspace/user
history.push_state({"nodes": []}, "admin", "BASELINE", "Trạng thái ban đầu")
history.push_state({"nodes": [{"id": "R1"}]}, "admin", "ADD_NODE", "Thêm R1")
assert history.can_undo()
diff = history.get_diff()
previous = history.undo(actor="admin")
following = history.redo(actor="admin")
backup_json = history.export_backup_file()
restored = history.restore_from_backup(backup_json, actor="admin")
```

Mọi method nhận `topology_id="default"`. Ví dụ
`history.undo(topology_id="lab-2", actor="operator")`. Các topology tách biệt;
topology đích restore do caller chọn, không lấy quyền truy cập từ JSON import.

Snapshot đầu là baseline: chưa Undo được. Phải push baseline trước chỉnh sửa
đầu tiên. Push mới xóa redo stack nhưng giữ audit của nhánh cũ. Không còn trạng
thái để Undo/Redo thì trả `None`, không ghi event giả. Snapshot phải là dict JSON
với khóa chuỗi, không NaN/Infinity/tuple; mọi đầu vào/đầu ra được copy sâu.

`get_diff()` trả danh sách `{op, path, before?, after?}`: `add`, `remove`,
`replace`. Path theo JSON Pointer; list được so sánh/thay toàn bộ. Diff này so
snapshot hiện tại với snapshot trước trên nhánh undo đang hoạt động.
`diff_from_previous` trong audit so với event trước, gồm cả Undo/Redo/Restore.

Backup có hai lớp, theo `protection.audit_view/encode_backup` và
`HistoryManager.export_backup_file`:

| Lớp | Nội dung theo code hiện tại |
| --- | --- |
| Public envelope `backup_version="1.2"` | `backup_kind="restorable_encrypted"`, `exported_at`, `timezone`, `key_scope`, `audit_trail` theo allowlist, `history_state` và `encrypted_history` |
| Archive trong `encrypted_history`, schema nội bộ `backup_version="1.0"` | `exported_at`, `timezone`, `topology_id`, `key_scope`, audit với snapshot/metadata gốc và `history_state` phục vụ Restore |

Audit công khai giữ step/timestamp, bí danh actor, action thuộc enum hoặc OTHER,
mô tả cố định, số lượng thuộc tính/pending/batch và số add/remove/replace. Field
`snapshot` trong public audit là summary, không phải cấu hình để Restore.
Snapshot, actor/action/description gốc và diff gốc nằm trong archive mã hóa.
`topology_id` không xuất hiện ở lớp public. Public `history_state` có các chỉ số
stack bắt đầu từ 0; chúng tương ứng các event trong archive và public audit,
nhưng không chứa nội dung snapshot. Restore giải mã và xác thực projection
public trước khi dùng snapshot/stack bên trong archive.

`export_audit_log()` trả `backup_kind="audit_only"`, `restore_supported=false`
và audit summary, không có ciphertext và không thể Restore. Reader 1.1 vẫn cần
cùng khóa và projection xác thực cũ; không tự chuyển đổi file backup cũ.
Export và Restore cùng giới hạn 2 MiB UTF-8; API trả `backup_too_large` khi vượt
ngưỡng, không cắt dữ liệu. Method export trả JSON text; caller tự tải xuống hoặc
ghi vào **file backup mới**, không ghi đè cấu hình gốc.

Khóa mặc định được tạo khi module protection được import, chỉ tồn tại trong RAM
process và không được xuất ra backup/log/response. Sau restart ứng dụng bằng
process mới, khóa mặc định mới không giải mã được backup đã dùng khóa cũ nếu
không còn đúng khóa đó. `HistoryManager(backup_key=...)` cho phép caller truyền
khóa; pending API chưa có provisioning/rotation/retention khóa bền vững và
không tự dùng `CREDENTIAL_ENCRYPTION_KEY`. Đây không phải disaster recovery.
Cùng khóa chỉ đáp ứng điều kiện giải mã; Restore pending vẫn cần quyền, ID đã
tracked, trạng thái chưa duyệt và danh tính đích hợp lệ.

Restore kiểm tra toàn bộ trước khi cập nhật. Nhận dict hoặc JSON string/bytes;
phiên bản không hỗ trợ/dữ liệu sai ném `ValueError`. Bản 1.0 tối giản chỉ có các
field yêu cầu được coi là lịch sử tuyến tính nếu không phát hiện credential
plaintext hoặc snapshot đã che. Restore giữ journal hiện có,
nối audit nhập và ghi event RESTORE với actor hiện tại; stack chuyển sang lịch
sử được nhập. Undo không hoàn tác thao tác import về workspace trước import.
Timestamp luôn UTC+07:00 với định dạng `YYYY-MM-DD HH:mm:ss`.

### State holder và publication hiện tại

`HistoryManager` giữ một reference `_state` tới `HistoryState`, gồm `histories`,
`expected`, `revision_epoch`, `generation` và `needs_reconcile`. `histories` giữ
snapshot, journal, actor aliases và Undo/Redo theo topology. Manager độc lập dùng
`expected=None`; pending workspace khởi tạo tracking bằng dict. `generation` là
bộ đếm publication RAM, khác `revision_epoch` dùng trong HMAC revision của editor.
Không xuất các trường holder này vào backup; schema hai lớp phía trên giữ nguyên.

Các writer push/checkpoint/Undo/Redo/Restore chuẩn bị candidate bằng copy-on-write.
`fork()` copy sâu payload và có lock riêng, dùng cùng cipher; không deepcopy
cipher hoặc lock. Candidate và live generation không alias history/tracking
maps. `_install_state(base, target)` kiểm tra identity rồi publish payload bằng
một phép gán `self._state = target`; retry khi target đã được cài là no-op.
`PendingWorkspace._publish()` không thay HistoryManager, cipher hoặc lock.

Các operation pending đồng bộ giữ workspace RLock và manager RLock; phương thức
HistoryManager giữ manager RLock. Generation được giữ ổn định trong phạm vi
operation bằng khóa, không chỉ nhờ phép đổi reference. `frozen=True` chỉ khóa
field của dataclass: các dict bên trong và private compatibility views vẫn có
thể bị sửa nếu caller bỏ qua protocol. Không có bảo đảm cho writer tùy ý sửa
trực tiếp `_histories`/`expected` hoặc reader ghép các field ngoài khóa.

`synchronize()` đọc DB, ghim base và chuẩn bị đầy đủ history, fingerprints,
revision/generation trên candidate trước publish. GET bình thường giữ nguyên
state, không checkpoint. Snapshot/fingerprint lệch, thiếu active tracking hoặc
`needs_reconcile` mới yêu cầu reconcile; extra cancelled keys không tự gây
checkpoint vì chúng có thể cần cho Redo. Checkpoint giữ journal, đặt baseline
mới và xóa Redo. Lỗi chuẩn bị không publish candidate hoặc xóa recovery đã bật.

Receipt `PublicationAttempt(base, target, source)` ở workspace, ngoài holder.
Lỗi trước install giữ base; lỗi sau install giữ target đã hoàn chỉnh cùng receipt
chưa acknowledge. GET recovery kiểm tra target với snapshot/fingerprints DB;
nếu khớp, retry cùng receipt, không tạo event/checkpoint hoặc tăng revision lần
nữa. Nếu DB đã đổi sau một commit được biết, reconcile giữ event đã commit rồi
lập barrier mới. Receipt được acknowledge khi live state chính là target.

Callback sau DB commit dùng packet đã chuẩn bị trước commit; retry nội bộ dùng
cùng receipt, không chạy lại Undo/Redo/Restore. Đây không phải cam kết retry một
HTTP mutation bất kỳ là idempotent. Standalone manager dùng copy-on-write nhưng
không có pending receipt/recovery. Publication một generation trong RAM không
tạo transaction nguyên tử với SQLite, multi-process safety hoặc crash recovery
bền vững. Core-create fail-soft được phân biệt ở mục tiếp theo.

## 2. API đã đăng ký trong application factory

Trong `backend/src/network_copilot/app.py`, `_register_blueprints()` đã import
initializer extension và gọi sau các blueprint hiện tại:

```python
from .history_extension.routes import init_app as init_history_extension
init_history_extension(app)
```

Initializer kiểm tra blueprint `draft_history` để không đăng ký lặp hoặc xóa
history nếu gọi lại trên cùng app. Các app factory khác nhau có storage riêng.
Không gọi ở mỗi request. Không sửa extensions/models/migrations/config.

| Method | Path | Nội dung |
| --- | --- | --- |
| GET | `/api/draft-history` | snapshot, diff, can_undo, can_redo |
| POST | `/api/draft-history/states` | `{snapshot, action_type, description}` |
| POST | `/api/draft-history/undo` | không cần body |
| POST | `/api/draft-history/redo` | không cần body |
| GET | `/api/draft-history/backup` | JSON attachment |
| POST | `/api/draft-history/restore` | object backup đầy đủ |

Tất cả cần Bearer JWT và ADMIN đang hoạt động. Actor lấy từ JWT, không tin actor
client gửi. Mọi URL nhận `?topology_id=...`. History được chia theo user JWT và
topology; VIEWER không đọc được cấu hình nháp. JSON POST giới hạn 2 MiB.
Undo/Redo hết stack trả 409; JSON/backup sai trả 400.

## 3. Hai nút đã nhúng vào đúng tiêu đề

Trong `<head>` của `templates/index.html` đã thêm một dòng sau CSS hiện tại:

```html
<link rel="stylesheet" href="{{ url_for('static', filename='css/history-toolbar.css') }}">
```

Tại `aside.sidebar.changes-panel` đã thêm `x-data="historyDraft"` và thay duy nhất
dòng `<h2>Thay đổi đang chờ</h2>` bằng include. Phần đầu hiện là:

```html
<aside class="sidebar changes-panel" x-data="historyDraft">
  {% include 'history_extension/toolbar.html' %}
  <!-- Giữ nguyên mọi nội dung bên dưới -->
```

Trước script `vendor/alpine.min.js` ở cuối template đã thêm hai script có defer:

```html
<script src="{{ url_for('static', filename='js/history-toolbar.js') }}" defer></script>
<script src="{{ url_for('static', filename='js/history-panel.js') }}" defer></script>
```

Không sửa `app.js`/`app.css`. Component Alpine con kế thừa `token`, `currentUser`,
`authFetch`; có state riêng `draftSnapshot`/`historyPanel`. Khi đổi tài khoản
hoặc logout, controller cũ được hủy, snapshot xóa và trạng thái được tải lại.
Nút luôn disabled trước khi lấy được status; khi request đang chạy không cho
click lần hai. Lỗi hiển thị qua vùng aria-live và khóa nút cho tới refresh.
Guard click kiểm tra cả trạng thái history để sự kiện kích hoạt bằng mã trên
nút disabled không gửi request. Export bị từ chối khi controller đã hủy. Adapter
kiểm tra token/quyền ADMIN trước lẫn sau request để chặn phiên cũ.
Component tải lại status mỗi 15 giây, không push snapshot khi polling. Timer
riêng được dừng khi logout/đổi user/unmount. Nút tròn 28px; CSS extension xử lý
margin của tiêu đề để căn cùng hàng mà không sửa CSS gốc.

## 4. Nối editor cấu hình nháp của nhóm

Mục này mô tả adapter JSON generic cho editor khác. Editor pending thực đã được
nhúng; dùng PATCH `/api/pending-history/changes/<id>` hoặc `/batches/<id>` và
`historyPanel.edit(kind, id, payload)` với revision từ GET .../editor, không push
full snapshot generic. Thiếu revision trả 422; editor stale trả 409.

Khi `recovery_required` bật, pending extension từ chối mở editor và
PATCH/Undo/Redo/Restore với conflict. Đây không phải fence cho tất cả core writers.
Trạng thái này là OR của `_recovery_flag`, receipt `_attempt` chưa acknowledge
và `history._state.needs_reconcile`. `status()` synchronize DB, kiểm tra không
còn receipt/dirty state rồi mới xóa `_recovery_flag`; GET
`/api/pending-history`, `/backup` và `/audit` đều gọi method này. Cờ có thể đã
được xóa sau synchronize thành công, trước khi export hoặc HTTP response hoàn
tất; lỗi export sau đó không chứng minh cờ vẫn bật. Nếu synchronize lỗi, chưa
đến bước xóa cờ. Reconcile có thể checkpoint và xóa Redo khi dữ liệu ngoài
journal thay đổi.

Core create/cancel vẫn có thể tiếp tục qua hook fail-soft: capture lỗi trả
`None`, record lỗi sau core commit chỉ log tên loại lỗi. Hook không kiểm tra cờ
recovery; capture thành công còn có thể synchronize/checkpoint. `record()` có
thể publish revision/history trước tracking; nếu hook lỗi sau RAM push, state
dirty còn lại được GET reconcile. Không rollback core create đã commit và không
khẳng định mọi hook mutation all-or-nothing. Hai regression core-create lỗi sau
RAM push giữ nguyên; coverage cụ thể ở tài liệu verification không chứng minh
mọi writer/fault tùy ý đều phục hồi được.
API JSON generic độc lập không dùng cờ của pending workspace.

Trong API JSON generic, include hai nút không tự tạo lịch sử từ các bản ghi
pending. Trong **component editor khác** nằm trong
scope của aside, dùng `historyPanel` do component con cung cấp:

```javascript
// Chỉ khởi tạo baseline một lần khi workspace mới chưa có snapshot.
const status = await historyPanel.refresh();
if (status.snapshot === null) {
  await historyPanel.push({ nodes: [], interfaces: {} }, "BASELINE", "Nháp ban đầu");
}

// Sau mỗi thao tác chỉnh sửa thành công; gửi full snapshot mới.
await historyPanel.push(nextDraft, "CONFIG_INT", "Đổi mô tả interface R1 Gi0/0");

// Controller tự gán draftSnapshot sau push, undo, redo hoặc restore.
const backup = await historyPanel.exportBackup(); // object do authFetch parse
await historyPanel.restore(backup);              // hoặc chuỗi JSON từ file
```

Không push mỗi lần poll hoặc mỗi render. Với editor nằm ngoài aside, lắng nghe
event `nc-history-snapshot` để render bản nháp được phục hồi. Nếu dùng controller
không Alpine, gọi `NetAIHistory.mount(root, {request, onSnapshot, topologyId})`;
`request` phải trả Promise của object JSON và ném lỗi khi HTTP không thành công,
`onSnapshot` chỉ cập nhật editor mới. Gọi `destroy()` khi đổi user/topology hoặc
unmount, rồi tạo controller mới. `onSnapshot` không gọi API Apply.

Để lưu backup qua trình duyệt từ object API:

```javascript
const backup = await historyPanel.exportBackup();
const url = URL.createObjectURL(new Blob([JSON.stringify(backup, null, 2)],
  { type: "application/json" }));
const link = document.createElement("a");
link.href = url;
link.download = "draft-history.json";
link.click();
setTimeout(() => URL.revokeObjectURL(url), 1000);
```

Restore file: `await historyPanel.restore(await selectedFile.text())` trong
component mới. Bắt lỗi và báo cho operator; không tự chuyển restore thành Apply.

## 5. Kiểm tra và giới hạn triển khai

Các lệnh dưới đây là hướng dẫn cho một lượt kiểm thử được phép sau này, không
được chạy trong lượt sửa tài liệu. Dùng `$testPython` trỏ tới virtualenv đã có;
không cài dependency mới. Nhóm HistoryManager/harness độc lập đã được review
không mở DB/server hay kết nối thiết bị; cần kiểm tra lại nếu các file đó đổi:

```powershell
$env:PYTHONPATH = (Resolve-Path backend/src).Path
$env:PYTHONDONTWRITEBYTECODE = '1'
& $testPython -B -m unittest discover -s backend/extension_tests -p "test_*.py" -v
node backend/extension_tests/history_toolbar_test.cjs
node backend/extension_tests/history_panel_test.cjs
node --check backend/src/network_copilot/static/js/history-panel.js
git --no-optional-locks diff --check
git --no-optional-locks diff --stat
git --no-optional-locks status --short
```

Nhóm API/pending mục tiêu đã được review là `backend/tests/history/`, dùng Flask
test client và fixture SQLite `:memory:`. Trước khi chạy phải xác nhận runner
chỉ tạo app với TestConfig, engine thực sự ở RAM, monitoring tắt, chặn cả hai
sqlite3 connect ngoài `:memory:`, socket/DNS/server bind, SSH, Apply và device
verification trước collection và trước từng test. Dùng `-B`, tắt bytecode,
pytest cache/plugin tự nạp và không tạo coverage/report. Nếu chưa kiểm tra được
runner/fixture/child-process guard, ghi UNKNOWN và chưa chạy; không chạy pytest
trực tiếp chỉ dựa vào tên thư mục test.

Không khuyến nghị lệnh regression rộng chứa `backend/tests/test_security.py`
hoặc test audit/lifecycle có thể gọi Apply. Chọn node test an toàn riêng sau
review; không chạy seed, demo/smoke hoặc E2E chưa chứng minh cô lập.

**Kết quả lịch sử của vòng kiểm tra bổ sung:** 63 kiểm thử Flask/regression trong môi trường
ảo riêng ở thư mục tạm, 12 kiểm thử HistoryManager, hai harness JavaScript cho
toolbar và lifecycle/logout. Kiểm tra cú pháp Python/JavaScript và
`git diff --check` đạt. Các kiểm thử API lưu snapshot thật qua Flask test client,
Undo/Redo, export/import, giữ nguyên pending preview và không gọi SSH.
Editor pending standalone/batch hiện đã được tích hợp. Browser testing và
topology thực tế vẫn UNKNOWN; các test lịch sử dùng dữ liệu/DB RAM, không dùng
database local hoặc thiết bị mạng thực.
Test roundtrip còn chạy controller JavaScript và nhận snapshot từ API Flask thật
qua pipe, xác nhận click Redo, trạng thái busy/disabled, event cập nhật nháp,
restore thất bại và giữ nguyên bốn map/list pending. DOM và đăng ký Alpine dùng
test double, nên kết quả này không thay thế kiểm thử trực tiếp trong trình duyệt.

Chi tiết luồng, lỗi đã sửa và phần tích hợp còn thiếu:
[`history-extension-verification.md`](history-extension-verification.md).
Tài liệu đó giữ riêng log 392 PASS trước tách và log sau tách 392 PASS, 0 FAIL,
0 SKIPPED (384 History/manager + 8 audit mới), gồm 94 publication và 113 recovery
cases. Không cộng hai lần chạy hoặc các số liệu lịch sử phía trên. Suite
`tests/audit/test_audit.py` không được chạy; file audit hardening mới có 8 PASS.

Kế hoạch browser, chưa thực hiện: chỉ dùng dữ liệu nháp/topology test cô lập khi
được phép; nếu chưa có môi trường đó thì giữ UNKNOWN. Baseline chỉ có disabled; sửa lần đầu
bật Undo; Undo bật Redo; sửa sau Undo làm Redo disabled; reload/restore cập nhật
editor; logout không giữ nháp user trước; Viewer bị từ chối; pending và polling
đồng bộ qua refresh gốc. Không gọi Apply/Verify hoặc thiết bị. Thử backup khi còn redo và import
JSON lỗi để xác nhận nháp hiện tại không bị thay đổi.

History, revision key và recovery flag hiện ở RAM, mất khi restart và không
chia sẻ giữa worker. Request RLock tạo riêng cho từng Flask app trong process;
nó bao các pending requests và POST/PATCH lifecycle/AI thuộc phạm vi middleware,
không bảo vệ mọi app/job hoặc caller ngoài HTTP. Acquire không timeout; request
dài có thể chặn request khác cùng thuộc phạm vi khóa. Mutation pending History
từ chối dialect ngoài SQLite; không suy rộng thành giới hạn mọi API core.

Multi-process cùng DB, concurrency stress, deadlock/fairness, latency thực và
durable crash recovery: UNKNOWN. RAM history không có transaction bền vững
chung với DB. Nếu dùng nhiều worker, cần storage extension riêng có giao dịch/
coordination liên process được kiểm chứng trước khi triển khai. Không quảng bá journal
RAM/JSON này như audit bất biến có chữ ký: JSON import có thể chứa actor/thời
gian giả; chỉ event RESTORE xác nhận operator hiện tại thực hiện import.

Backup mã hóa snapshot gốc; journal audit và diff công khai che lệnh, mô tả tự do
và credential. Editor/status ADMIN vẫn trả nội dung cần sửa. Audit core có thay
đổi redaction tối thiểu trong audit/service.py để che secret IOS có encoding type,
danh sách lệnh cấu hình và mô tả Preview. Không thêm dependency hoặc đọc/ghi file
khóa. Xem bằng chứng hardening, giới hạn format/khóa và checklist trước PR trong
[pending-history-integration.md](pending-history-integration.md).

Hoàn tác việc tích hợp: gỡ đúng các dòng import/include/link/script/x-data đã
thêm. Không có migration hoặc dữ liệu core nào phải rollback.
