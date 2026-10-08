# Đề xuất tối ưu và tính năng tiếp theo

Kết quả nghiên cứu sau khi có project, sơ đồ mạng và quản trị user. Xếp theo giá trị
trên công sức; mục đã làm được đánh dấu ✅.

## Đã làm

- ✅ Thêm thiết bị kiểu Packet Tracer/PNETLab: kéo thả từ bảng thiết bị, tên (`R1`,
  `SW2`…) và IP quản trị tự cấp, cổng tự chọn khi nối dây, phím `Delete`.
- ✅ Chat: gợi ý câu hỏi theo thiết bị thật của project, Enter gửi / Shift+Enter xuống
  dòng (không gửi nhầm khi đang gõ Telex/VNI), `↑` nhắc lại tin trước, nút "Tin mới",
  chỉ báo AI đang xử lý, bấm thiết bị để chèn hostname, sao chép output.
- ✅ Bớt truy vấn thừa: danh sách thiết bị và danh sách project không còn N+1.

## Nên làm tiếp (ưu tiên cao)

| # | Việc | Vì sao |
|---|---|---|
| 1 | **Nhập thiết bị từ PNETLab** qua API của PNETLab (liệt kê node của một lab) | Hiện vẫn phải thêm tay từng thiết bị; PNETLab đã biết tên, loại và console của chúng |
| 2 | **Tự khám phá topology** bằng `show cdp neighbors detail` / `show lldp neighbors` trên thiết bị thật, rồi vẽ link | Dựng sơ đồ từ mạng thật thay vì vẽ tay; cũng là cơ sở cho mục 3 |
| 3 | **So sánh thiết kế với thực tế**: tô đỏ link có trong sơ đồ nhưng không có trên thiết bị (và ngược lại) | Phát hiện lệch cấu hình, đúng nhu cầu "thiết kế rồi cấu hình" |
| 4 | **Nhập/xuất sơ đồ** (JSON, CSV thiết bị, ảnh PNG/SVG) và **nhân bản project/mẫu** | Chia sẻ bài lab, dựng lại nhanh |
| 5 | **Hoàn tác / làm lại** trên canvas, chọn nhiều thiết bị, căn lề | Thao tác sơ đồ an tâm như Packet Tracer |
| 6 | **Cấu hình L2/L3 phong phú hơn từ sơ đồ**: VLAN, cổng access, SVI, định tuyến tĩnh/OSPF theo link | Hiện chỉ sinh IP, trunk, mô tả; ASA mới bị bỏ qua |

## Hiệu năng và độ tin cậy

- **Cập nhật chat bằng push thay vì thăm dò.** Trang chat gọi 4 API mỗi 7–15 giây cho
  mỗi tab đang mở. Dùng SSE (một kết nối, server gửi khi có tin/thay đổi) hoặc ít nhất
  `ETag`/`If-None-Match` để các lần thăm dò không đổi trả `304`.
- **Monitoring theo project, chạy song song có giới hạn.** Vòng poll hiện tuần tự
  qua mọi thiết bị của mọi project (`max_instances=1`); nhiều project sẽ làm một vòng
  kéo dài hơn chu kỳ. Dùng thread pool nhỏ và lưu thời lượng vòng để cảnh báo.
- **Giữ lại snapshot có hạn.** `device_snapshots` và `command_executions` tăng mãi;
  cần job dọn (giữ N bản gần nhất hoặc X ngày) và phân trang các API danh sách.
- **Tái sử dụng phiên SSH** trong một lô thay đổi nhiều thiết bị / một vòng poll thay
  vì mở lại mỗi lệnh.
- **Đồng thời trên canvas**: hai người cùng sửa một sơ đồ đang ghi đè nhau (last write
  wins). Thêm `updated_at`/phiên bản để cảnh báo, hoặc khóa theo phiên.

## Giao diện chat

- Hiển thị Markdown cơ bản và đánh dấu cú pháp cho khối lệnh trong câu trả lời.
- Đổi tên / ghim / xóa phiên chat; tìm kiếm trong lịch sử.
- Nút "Chạy lại", "Hỏi tiếp về kết quả này" ngay trên mỗi kết quả lệnh.
- Thông báo (toast) khi một thay đổi được duyệt/áp dụng bởi người khác.

## Bảo mật và vận hành

- Khóa tạm thời tài khoản sau nhiều lần đăng nhập sai (hiện chỉ giới hạn theo IP).
- Xác thực hai bước cho ADMIN; chính sách mật khẩu có kiểm tra mật khẩu phổ biến.
- Cho phép chủ project tự duyệt/áp dụng thay đổi *trong project của mình* (cấu hình
  theo project) thay vì chỉ ADMIN toàn cục — cần quyết định chính sách.
- Sao lưu định kỳ file SQLite và thử khôi phục; cân nhắc PostgreSQL nếu có nhiều
  người dùng đồng thời.
- Giới hạn tần suất cho thao tác tạo project/thiết bị.
