# Backend Admin – Teacher – Student

Backend Flask bổ sung API quản trị, quyền theo phạm vi, bài thực hành và quản lý lab. Các quyền **tạo config, duyệt config, triển khai config và phục hồi** độc lập với nhau.

## Chạy và nâng cấp

Yêu cầu Python 3.11 trở lên. Môi trường `.venv` cũ của máy này dùng Python 3.10; môi trường kiểm thử riêng là `.venv-governance` (Python 3.12).

```powershell
# Từ thư mục backend, trong môi trường đã cài backend[dev]
python -m flask --app wsgi db upgrade
python scripts/seed_lab.py
python -m flask --app wsgi run
```

Trước khi seed, cấu hình `SEED_ADMIN_PASSWORD`, `JWT_SECRET_KEY` và `CREDENTIAL_ENCRYPTION_KEY` theo README. Không có đăng ký Admin công khai. Seed chỉ khởi tạo Admin khi hệ thống chưa có Admin. Không ghi mật khẩu vào mã nguồn.

Migration `9e54c2bd3d7e`:

- Hỗ trợ SQLite và PostgreSQL; kiểm thử migration được thực hiện trên SQLite.
- Dừng trước khi thay đổi schema nếu có nhiều Admin hoặc triển khai đang chạy. Cần xử lý tài khoản trùng và đối soát tác vụ trước khi nâng cấp.
- Giữ dữ liệu cũ, đổi `VIEWER`/`OPERATOR` thành `STUDENT` và vô hiệu hóa token cũ.
- Hủy preview/approval cũ chưa chạy vì chúng chưa ràng buộc đầy đủ nội dung, phạm vi và trạng thái nền. Lưu một sự kiện migration trong audit.
- Teacher hiện có và Teacher mới mặc định được tạo nhóm và tài khoản Student, không cần grant của Admin. Các quyền thao tác thiết bị/config của Teacher và Student vẫn cần cấp rõ ràng.
- Thiết bị chưa khai báo môi trường được xếp `physical_lab`, không cô lập. Admin cần phân loại chính xác `virtual`, `physical_lab` hoặc `production`.

Các kiểm tra migration trong quá trình phát triển dùng cơ sở dữ liệu thử riêng, không nâng cấp cơ sở dữ liệu đang vận hành.

## Đăng nhập Admin

`POST /api/auth/login` chỉ cần `username` và `password`. Admin có thể đăng nhập đồng thời trên nhiều máy; đăng nhập mới không thu hồi phiên trước đó.

Phản hồi chứa token đầy đủ và `mfa_setup_required: false` để tương thích với ứng dụng cũ. Không còn màn hình thiết lập, yêu cầu mã TOTP hay mã khôi phục. Các API `/api/auth/mfa/setup`, `/api/auth/mfa/enable` và `/api/auth/mfa/reset` đã được gỡ bỏ.

Đổi mật khẩu, đổi quyền hoặc thay đổi trạng thái tài khoản vẫn làm token cũ mất hiệu lực trên mọi máy. Các cột MFA trong migration cũ được giữ nguyên để không sửa lịch sử nâng cấp; ứng dụng không sử dụng chúng.

`POST /api/admin/transfer` nhận `user_id` và `password` của Admin hiện tại. Người nhận phải là Teacher đang hoạt động. Chuyển giao làm token cũ của cả hai tài khoản mất hiệu lực. Các grant gốc do Admin cấp được chuyển quyền sở hữu cho Admin mới.

## Lab dùng chung, project và chat topology

Tài khoản Operator và Viewer đều thấy project lab mặc định (project seed `PNETLab`, hoặc tên đặt trong `SEED_PROJECT_NAME`) ở chế độ xem. Khung chat dùng được trên lab này; Viewer và Operator nhận tư vấn AI dựa trên topology của project. AI giải thích yêu cầu bằng thuật ngữ mạng và đề xuất cách kết nối, không chạy cấu hình.

Operator có thể nhập “Tạo project mới” trong chat. Hệ thống tạo project và hỏi số thiết bị mạng (2–25), sau đó hiển thị các sơ đồ hình sao, vòng, campus hoặc hub and spoke phù hợp với số lượng. Chọn một phương án sẽ tạo các thiết bị ở dạng kế hoạch và mở canvas topology để Operator thêm, sửa hoặc xóa. Thiết bị phải được cấp IP và thông tin truy cập trước khi có thể cấu hình thật.

Project là phạm vi chia sẻ của nhóm: Operator thêm tài khoản Viewer từ tab “Nhóm / chia sẻ”. Viewer xem topology và dùng chat tư vấn; chỉ người có quyền sửa project mới thay đổi sơ đồ.

### Phiên chat là riêng của từng người

Thành viên dùng chung thiết bị và topology của project, nhưng **mỗi người có phiên chat riêng**. Phiên thuộc về người tạo ra nó (`chat_sessions.created_by_id`):

- `GET /api/chat/sessions` chỉ trả về phiên của chính người gọi, kể cả Admin.
- `GET /api/chat/messages` và `POST /api/ai/chat` không có `session_id`, hoặc với `session_id` của người khác, sẽ dùng phiên gần nhất của chính người gọi (tạo mới nếu chưa có). Viewer không thể đọc hay ghi vào phiên của Operator và ngược lại.
- Admin được đọc mọi phiên trong project theo `session_id` để kiểm tra (audit), nhưng không ghi vào phiên của người khác.
- Chỉ áp dụng / tải cấu hình từ gợi ý AI nằm trong phiên của chính mình.
- Phiên cũ không có người tạo (`created_by_id` NULL, từ trước thay đổi này) chỉ Admin còn đọc được.

### Bảng quyền theo vai trò

Quyền thực tế = vai trò tài khoản ∩ quyền trên project (`owner` / `editor` / `viewer`). Tài khoản VIEWER luôn bị hạ xuống `viewer` trên mọi project, kể cả project được chia sẻ quyền sửa. Lab mặc định là `viewer` với mọi tài khoản trừ Admin.

| Hành động | ADMIN | OPERATOR | VIEWER |
|---|---|---|---|
| Xem mọi project | ✓ | chỉ project của mình / được chia sẻ / lab mặc định | chỉ project được chia sẻ / lab mặc định |
| Tạo project (kể cả “Tạo project mới” trong chat) | ✓ | ✓ | ✗ |
| Chia sẻ project | ✓ | chỉ thêm tài khoản Viewer, trên project mình sở hữu | ✗ |
| Sửa topology / thiết bị / áp thiết kế AI | ✓ | khi là owner/editor | ✗ |
| Chat tư vấn AI (không chạy lệnh) | — | ✓ | ✓ |
| Chat AI chạy lệnh / tạo Preview → Approve → Apply | ✓ | ✗ | ✗ |
| Duyệt và áp cấu hình AI lên PNETLab (AI Management) | ✗ (dùng pipeline Preview/Approve/Apply) | khi là owner/editor và project bật AI Management | ✗ |
| Tải file cấu hình từ gợi ý AI | ✓ | ✓ (gợi ý trong phiên của mình) | ✓ (gợi ý trong phiên của mình) |
| Đọc phiên chat | mọi phiên (audit) | phiên của mình | phiên của mình |
| Quản lý người dùng | ✓ | ✗ | ✗ |

## Cấp quyền ban đầu

Một luồng thiết lập điển hình:

1. Admin tạo Teacher/Student bằng `POST /api/users` (`username`, `password` ít nhất 12 ký tự, `role`).
2. Admin tạo nhóm qua `POST /api/groups` (`name`), giao nhóm bằng `POST /api/groups/{id}/transfer` (`teacher_id`). Teacher cũng có thể tự tạo nhóm mà không cần Admin cấp quyền; Teacher tạo nhóm tự động trở thành người quản lý nhóm đó.
3. Admin đăng ký thiết bị qua API `/api/devices`, đặt môi trường và hướng dẫn phục hồi; phân bổ bằng `PUT /api/groups/{group_id}/devices/{device_id}`.
4. Teacher tự tạo tài khoản Student qua `POST /api/users` (role `STUDENT` hoặc bỏ trống), không cần grant `student.create`. Teacher thêm Student bằng `PUT /api/groups/{group_id}/members/{user_id}` hoặc duyệt yêu cầu tham gia.
5. Admin cấp từng quyền cho Teacher; đánh dấu `delegable` cho các quyền Teacher được phép cấp lại. Teacher cấp các quyền đó cho Student trong nhóm.

Admin đặt thông tin SSH bằng `PUT /api/devices/{id}/credentials` với `username`, `password`, tùy chọn `enable_secret`. Backend mã hóa bằng credential cipher hiện có; API chỉ trả trạng thái đã cấu hình và không có endpoint đọc mật khẩu/khóa cho Student.

Ví dụ request `POST /api/permissions` của Admin:

```json
{
  "user_id": 2,
  "action": "config.deploy",
  "group_id": 1,
  "device_id": 3,
  "environment": "virtual",
  "expires_at": "2026-12-31T17:00:00Z",
  "delegable": true
}
```

Teacher dùng cùng endpoint, đổi `user_id` thành Student và đặt `delegable: false`. Thời hạn, nhóm, thiết bị và môi trường không được rộng hơn grant gốc. Không khai báo thiết bị/môi trường nghĩa là grant rộng trong các chiều đó; quyền vẫn cần membership và phân bổ thiết bị hợp lệ. Student không được cấp `config.approve`, quyền quản trị tài khoản/nhóm, quản lý bài tập hoặc xóa lab.

Các action hỗ trợ:

| Mục đích | Action |
|---|---|
| Tài khoản/nhóm | `student.create`, `group.create` — quyền mặc định của Teacher, không phụ thuộc grant |
| Quan sát | `device.read` (đọc API/trạng thái), `device.observe` (chạy lệnh đọc/refresh) |
| Config | `config.create`, `config.validate`, `config.simulate`, `config.approve`, `config.deploy`, `config.restore` |
| AI | `ai.use` |
| Bài tập/lịch | `assignment.manage`, `device.reserve` |
| Lab | `lab.template`, `lab.create`, `lab.start`, `lab.stop`, `lab.reset`, `lab.delete` |

Grant được kiểm tra theo **một phạm vi hoàn chỉnh**, không cộng nhóm A với thiết bị nhóm B. Grant con mất hiệu lực khi grant cha hết hạn/thu hồi, người cấp bị khóa hoặc không còn quản lý nhóm. Xóa Student khỏi nhóm thu hồi grant nhóm để việc tham gia lại không khôi phục quyền cũ. Đình chỉ nhóm không khóa tài khoản toàn hệ thống.

`DELETE /api/permissions/{id}` thu hồi quyền. `GET /api/permissions` trả trạng thái `active` được tính từ toàn bộ chuỗi quyền. Timestamp đầu vào phải có timezone, ví dụ `Z` hoặc `+07:00`; backend lưu UTC.

## Quy trình cấu hình

1. Chọn nhóm và thiết bị. `POST /api/ai/chat` hỗ trợ thêm `group_id`; AI chỉ nhận inventory/topology được phép. Wildcard chỉ giải quyết thiết bị trong phạm vi được cấp.
2. `POST /api/changes/preview` nhận `group_id`, `device_id`, `commands`, `description`, tùy chọn `assignment_id` và `execution_mode`. Nếu thiết bị thuộc nhiều nhóm được phép, phải chọn rõ nhóm.
3. Với thiết bị thật hoặc lab ảo dùng chung, Teacher/Admin chụp baseline qua `POST /api/devices/{id}/baseline` **trước khi tạo preview**. Endpoint trả metadata backup, không trả config thô.
4. Backend xác định `low`/`high`. Student phải nêu mục đích trong `description` khi yêu cầu High Risk. Lệnh bị cấm bị từ chối ngay; không được chuyển thành một yêu cầu duyệt hợp lệ.
5. Teacher có `config.approve` hoặc Admin duyệt. Mặc định Teacher không tự duyệt config mình tạo; thay đổi production chỉ Admin duyệt. `/reject` nhận `reason`; `/escalate` thông báo Admin.
6. Student có thể chạy Low Risk qua Smart Approval của đúng nhóm, hoặc triển khai **config của mình đã được Teacher/Admin duyệt**, nếu có `config.deploy`. Teacher không có quyền triển khai chỉ nhờ có quyền duyệt.
7. Trước SSH và sau khi backup, kiểm tra lại quyền, scope, lịch, thời hạn, approval digest, revision và baseline. Teacher/Student sử dụng thiết bị thật phải có reservation còn hiệu lực của đúng nhóm. Mọi role bị chặn trong thời gian bảo trì hoặc khi thiết bị offline.
8. Lease có khóa duy nhất theo thiết bị và cập nhật trạng thái có điều kiện ngăn hai worker triển khai đồng thời. Ghi audit bắt đầu trước khi gửi thay đổi; sau đó triển khai, xác minh và thông báo kết quả.

Approval mặc định hết hạn sau 60 phút. Digest ràng buộc commands, verification, thiết bị, nhóm, bài tập, môi trường, baseline và chính sách liên quan. Khi những thông tin này đổi, tạo preview và duyệt lại. Config nền thật được đối chiếu với backup ngay trước khi ghi. Thay đổi ngoài hệ thống giữa lần kiểm tra cuối và lần SSH ghi vẫn phụ thuộc khả năng giao dịch/khóa của thiết bị.

Thiết bị production, core/distribution/firewall/ISP, lab không cô lập, routing/ACL/IP quản trị và lệnh chưa thuộc tập Low Risk đều mặc định High Risk. Môi trường `physical_lab` chỉ xét Low Risk khi Admin khai báo prefix được phép và lệnh vẫn qua bộ kiểm tra an toàn. `PATCH /api/policy` chỉ dành cho Admin; `GET` công khai cho người đã đăng nhập.

Phân loại rủi ro là tự động ở backend, không lấy mức rủi ro do người dùng hay AI tự khai báo. Chỉ cần một lệnh High Risk thì cả cấu hình là High Risk.

| Nhóm cấu hình | Mức tự động |
|---|---|
| Đổi hostname, mô tả cổng, tạo/đặt tên VLAN, đặt cổng Ethernet ở access mode và gán access VLAN | Low trong lab ảo cô lập, trên thiết bị không thuộc nhóm quan trọng |
| Routing/static route/OSPF, ACL, IP interface hoặc IP quản trị | High |
| Tài khoản, mật khẩu, AAA, SSH/crypto, SNMP | High |
| Xóa cấu hình, shutdown, trunk, lệnh chưa thuộc tập Low được hỗ trợ | High |
| Bất kỳ thay đổi nào trên production, core/distribution/ISP/firewall hoặc lab ảo không cô lập | High |
| Lab vật lý | High mặc định; chỉ xét Low với tập lệnh an toàn đã được policy cho phép |
| Phục hồi cấu hình | High, cần duyệt lại |

Admin có thể bổ sung lệnh cấm hoặc nâng mức rủi ro qua policy; không cần cấu hình policy trước để bộ phân loại tự động hoạt động. Low Risk không tự cấp quyền triển khai và không bỏ qua lịch thiết bị hay các kiểm tra an toàn.

Batch API hiện có tiếp tục sử dụng cùng kiểm tra quyền cho từng thiết bị. Toàn bộ batch được kiểm tra quyền trước khi bắt đầu; từng child được kiểm tra lại khi đến lượt chạy. Các lỗi xảy ra sau khi đã chạy một phần vẫn trả kết quả từng thiết bị.

## Các nhóm API mới

| Nhóm | Endpoint chính |
|---|---|
| Tài khoản | `GET/POST /users`, `PATCH /users/{id}`, `PATCH /auth/profile`, `POST /auth/password` |
| Nhóm | `GET/POST /groups`, `PATCH /groups/{id}`, `/transfer`, `/members/{id}`, `/devices/{id}` |
| Lời mời | `POST /groups/{id}/invitations`, `DELETE /invitations/{id}`, `POST /groups/join`, `GET /groups/{id}/join-requests`, `POST /join-requests/{id}/review` |
| Bản nháp | `GET/POST /drafts`, `GET/POST /drafts/{id}/versions`, `GET /drafts/{id}/diff?from=…&to=…`, `POST /drafts/{id}/validate`, `/preview`, `/simulate` |
| Bài tập | `GET/POST /groups/{id}/assignments`, `GET/POST /assignments/{id}/submissions`, `PATCH /submissions/{id}/grade` |
| Lịch | `GET/POST /reservations`, `DELETE /reservations/{id}` |
| Lab | `GET/POST /lab-templates`, `PATCH /lab-templates/{id}`, `GET/POST /labs`, `POST /labs/{id}/{start,stop,reset,delete}` |
| AI template | `GET/POST /ai-templates`, `PATCH /ai-templates/{id}` |
| Thông báo | `GET /notifications`, `POST /notifications/{id}/read` |
| Phục hồi | `POST /changes/{id}/restore-request`, `POST /changes/{id}/restore-preview` |
| Vận hành | `GET /operations/leases`, `POST /operations/leases/{device_id}/release` |

Mọi đường dẫn trong bảng có tiền tố `/api`. `members` dùng PUT để thêm, DELETE để rời/loại, PATCH với `suspended` để đình chỉ. `join` nhận `code`; `/review` nhận `accepted`. Lời mời nhóm mặc định hết hạn sau 7 ngày, được lưu bằng hash.

Draft version không sửa tại chỗ; tạo version mới rồi chọn `version_id` để kiểm tra/nộp/preview. Assignment nhận `device_id`, `title`, `instructions`, `starts_at`, `due_at`, `allowed_prefixes`. Nộp bài phải đúng nhóm, thiết bị, thời gian và prefix được phép. Preview có `assignment_id` được kiểm tra lại các giới hạn khi triển khai. Chấm bài nhận `score` từ 0–100 và `feedback`.

AI template nhận `name`, `instructions`, `commands`; chỉ Admin được quản lý. Template đang bật được đưa vào context AI, nhưng không thay thế kiểm tra quyền/chính sách. Lab template có `allowed_teacher_ids` để Admin chọn Teacher được sử dụng.

Lịch reservation không chồng nhau trên một thiết bị; `maintenance: true` chỉ Admin được đặt. Thông báo nằm trong platform; chưa có gửi email hoặc push. Danh sách thông báo kèm `expiring_permissions` trong 24 giờ tới. Policy đặt hạn mức AI/ngày, số lab, thời gian lab và số change đang chờ/chạy.

## Phần cần kết nối hạ tầng thật

Backend đã có driver SSH Cisco IOS/ASA cho thiết bị đăng ký. Không tự mở SSH, tạo lab hay áp dụng migration vào hệ thống thật khi chạy bộ kiểm thử.

Quản lý vòng đời lab cần gắn `app.config["LAB_DRIVER"]` bằng adapter của hạ tầng được sử dụng:

```python
class LabDriver:
    def provision(self, topology, *, deadline, idempotency_key) -> str:
        """Trả external lab ID; tạo lab cô lập và cấu hình tự dừng tại deadline UTC."""

    def operate(self, external_id, action, *, deadline) -> None:
        """Thực hiện start/stop/reset/delete; chỉ trả về khi thao tác thành công."""
```

Adapter phải tự thực thi giới hạn tài nguyên và deadline, kể cả khi Flask dừng, đồng thời xử lý idempotency. Nếu provider lỗi, backend lưu trạng thái `failed`; cần đối soát với provider trước khi thử lại. Không có adapter PNETLab/EVE-NG chạy thật được cấu hình sẵn trong thay đổi này.

Mô phỏng cần `app.config["ISOLATED_SIMULATOR"]`, callable nhận `device_type`, `commands` và trả kết quả JSON. Nó không nhận địa chỉ quản trị hoặc credential của thiết bị mục tiêu; adapter phải chạy trong môi trường cô lập. Nếu chưa cấu hình provider, API trả **503**, không báo đã tạo/chạy/mô phỏng thành công. `/validate` chỉ kiểm tra chính sách và các cấu trúc backend hỗ trợ, không thay thế parser cú pháp đầy đủ của hệ điều hành mạng.

Không giả định mọi config có thể tự rollback. `/restore-request` gửi yêu cầu; `/restore-preview` nhận `commands`, `description`, tùy chọn `execution_mode`, yêu cầu hướng dẫn phục hồi của thiết bị và quyền `config.restore`. Restoration tạo một preview High Risk mới, vẫn phải duyệt và có quyền triển khai. Lab riêng có thể reset nếu được cấp quyền. Không tự thực thi danh sách `rollback_commands` suy đoán.

Triển khai SSH hiện chạy đồng bộ và không hỗ trợ ngắt an toàn giữa các lệnh; API cancel hiện có chỉ hủy tác vụ chưa chạy. Lease không tự hết hạn để tránh worker cũ vẫn ghi lên thiết bị. Sau khi xác minh worker đã dừng, Admin có thể gọi `/operations/leases/{id}/release` với `confirm_worker_stopped: true` và `reason`; hành động này được ghi audit.

## Kiểm thử

```powershell
python -m pytest -q
```

Các bài kiểm thử governance kiểm tra chuỗi ủy quyền, sai nhóm/thiết bị, thu hồi/hết hạn/rời nhóm, approval bị sửa, lease, MFA/recovery/chuyển giao Admin, cô lập lịch sử, lịch bảo trì, draft/nộp/chấm bài, quota AI, lab adapter và drift cấu hình. SSH/AI/lab provider đều dùng mô phỏng trong test; cần kiểm thử tích hợp riêng với hạ tầng được cấu hình trước khi đưa vào vận hành.
