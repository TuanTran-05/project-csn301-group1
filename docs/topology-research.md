# Nghiên cứu: đa dạng topology và giao diện tạo topology

Kết quả khảo sát ngày 2026-10-11, dựa trên việc chạy ứng dụng thật (đóng vai một user
`OPERATOR` dựng topology từ đầu, và một project 120 thiết bị / 179 dây) cùng đọc mã.

## 1. Đo được gì

| Phép đo | Kết quả | Ý nghĩa |
|---|---|---|
| Kéo một node trong sơ đồ 120 node / 179 dây | 30 lần di chuyển mất ≈ 1,7 s (≈ 57 ms/lần, ~17 fps) | Giật rõ. Mỗi lần kéo, toàn bộ SVG (122 KB) bị dựng lại và gán `innerHTML` (≈ 14 ms riêng phần này) |
| Bố trí mặc định của 120 node | Lưới 5 cột, sắp theo tên (`N1, N10, N100, N101…`), canvas cao 3644 px | Không liên quan tới kết nối nên dây chằng chịt; sắp tên theo chữ chứ không theo số |
| Cuộn chuột / nhấp đúp trên canvas | Không phóng to, không kéo nền (pan), nhấp đúp không làm gì | Sơ đồ quá nhỏ hoặc quá lớn đều không xem được trọn vẹn |
| User `OPERATOR` dựng xong sơ đồ | Nút "Tạo bản xem trước" bị ẩn, chỉ có dòng "Chỉ ADMIN…" | Người tạo topology không có cách gửi cấu hình cho ADMIN duyệt |
| Thiết bị vừa thả vào canvas | Trạng thái `unknown`, IP tự cấp, chưa có SSH | Không có khái niệm thiết bị "chỉ để thiết kế" |
| Loại thiết bị có thể vẽ | 4 mẫu: Router, Switch L3, Switch L2, Firewall ASA | Không có PC, server, cloud/Internet, AP, hub… |

## 2. Thiếu gì để topology "đa dạng"

### 2.1 Mô hình dữ liệu
1. **Thiết bị không quản lý được (unmanaged).** Mọi thiết bị bắt buộc có IP quản trị, loại
   `cisco_ios|cisco_asa` và vai trò mạng (CHECK trong DB). Packet Tracer/PNETLab có PC,
   server, laptop, điện thoại, cloud, AP, hub. Cần `kind` (router, l3_switch, switch,
   firewall, pc, server, cloud, ap…) và cờ `managed`; thiết bị không quản lý thì không cần
   IP quản trị/SSH, không bị poll, không nhận cấu hình, nhưng vẫn được nối và có địa chỉ
   (IP/gateway) để vẽ và tính toán.
2. **Chế độ thiết kế (planned) so với đã triển khai.** Trạng thái vòng đời của thiết bị:
   `planned` → `provisioned`. Thiết bị planned không cần IP thật; "Triển khai" mới yêu cầu IP
   và SSH. Đây là điều làm Packet Tracer dễ dùng: vẽ trước, cấu hình sau.
3. **Nhiều hình thức kết nối.** Hiện chỉ 2 đầu, 3 kiểu (`physical|routed|trunk`). Thiếu:
   - đoạn mạng dùng chung (hub/cloud/LAN segment) nối ≥ 3 thiết bị;
   - **port-channel/LAG** (nhiều dây song song giữa hai thiết bị);
   - **router-on-a-stick** (cổng trunk của router + subinterface theo VLAN);
   - loopback, cổng serial/WAN, tunnel;
   - cáp access (một VLAN) khai báo ngay trên dây thay vì ở tab riêng.
4. **Nhiều topology trong một project.** Hiện một project = một sơ đồ. Người dùng thực tế cần
   "hiện tại" và "đề xuất", hoặc nhiều kịch bản. Cần **phiên bản/ảnh chụp (snapshot)** của
   sơ đồ: lưu, so sánh (diff), khôi phục, đặt tên.
5. **Chú thích và nhóm.** Ghi chú văn bản, hộp nhóm có tên (site, tầng, VLAN, DMZ), màu theo
   vùng, nhãn cho dây. Không có chỗ lưu những thứ này.
6. **Ngăn chứa cho nhiều site.** Sơ đồ lớn cần gom thành nhóm có thể thu gọn.

### 2.2 Tạo nhanh topology có sẵn mẫu
- **Thư viện mẫu / trình sinh**: star, ring, full-mesh, hub-and-spoke, cây 3 lớp
  (core–distribution–access), campus có DMZ, 2 site nối WAN. Người dùng chọn mẫu, nhập số
  lượng (ví dụ "1 core, 2 distribution, 4 access"), hệ thống tạo thiết bị, dây, IP điểm-điểm,
  VLAN mẫu. Đây là cách ngắn nhất để "đa dạng topology" mà không phải kéo từng thiết bị.
- **Lưu project hiện tại thành mẫu** (đã có xuất/nhân bản; thiếu thư viện mẫu dùng chung và
  mẫu công khai trong nhóm).
- **Kế hoạch địa chỉ tự động (IPAM)**: chia subnet /30 cho dây routed, /24 cho VLAN từ một
  khối lớn (ví dụ `10.0.0.0/16`), tránh trùng; hiện phải gõ từng subnet.

## 3. Giao diện tạo topology: những chỗ vướng

### Cấp thiết (làm sơ đồ dùng được)
1. **Phóng to / thu nhỏ / kéo nền / "vừa khung"** (con lăn, Ctrl+con lăn, nút +/−/fit, hai
   ngón trên màn cảm ứng) và **minimap** cho sơ đồ lớn.
2. **Hiệu năng kéo**: cập nhật `transform` của node đang kéo và chỉ các dây của nó, thay vì
   dựng lại toàn bộ SVG; áp dụng cho cả chọn vùng và hoạt ảnh. Mục tiêu ≥ 60 fps ở 300 node.
3. **Tự bố trí thông minh**: phân tầng theo vai trò (core trên, access dưới), vòng tròn, hình
   sao, lực đẩy; chạy trên vùng đã chọn; sắp xếp tự nhiên (`N2` trước `N10`). Hiện "Sắp xếp
   lại" chỉ xếp lưới theo tên.
4. **Chỉnh nhanh tại chỗ**: nhấp đúp để đổi tên, panel thuộc tính cố định cho thiết bị đang
   chọn (hostname, IP, vai trò, môi trường, cổng), menu chuột phải (xóa, nhân bản, nối tới…).
5. **Sao chép/dán và nhân bản** thiết bị hoặc cả vùng chọn (Ctrl+C/V/D), giữ cấu trúc dây
   bên trong vùng.
6. **Nối dây bằng cách kéo từ cổng**, chọn cổng từ danh sách cổng thật của thiết bị (đã dùng /
   còn trống) thay vì gõ tên; hiện cổng dạng chữ tự do.
7. **Bắt dính lưới / đường gióng** khi kéo, để sơ đồ gọn.

### Quan trọng (trải nghiệm người mới)
8. **Hướng dẫn lần đầu**: trạng thái trống có nút "Chọn mẫu", "Nhập từ PNETLab", "Kéo thiết
   bị đầu tiên"; chú giải phím tắt (phím `?`); trình hướng dẫn 4 bước.
9. **Bảng kiểm tra (lint) thời gian thực**: thiết bị đứng một mình, hai thiết bị trùng IP/
   hostname, subnet trùng, VLAN dùng nhưng chưa khai báo, trunk không cho VLAN đang dùng,
   thiết bị chưa có SSH, dây routed thiếu địa chỉ. Hiện lỗi chỉ hiện khi bấm lưu.
10. **Xem trước cấu hình theo thiết bị** ngay khi chọn thiết bị (tab "Cấu hình sinh ra" bên
    cạnh thuộc tính), và so sánh với `show running-config` gần nhất.
11. **Tìm kiếm và lọc** (theo tên, vai trò, VLAN, trạng thái), làm nổi bật đường đi giữa hai
    thiết bị, hiển thị/ẩn nhãn cổng và IP theo lớp (L1/L2/L3).
12. **Lưu tự động có trạng thái rõ** ("Đã lưu", "Đang lưu…"), và lịch sử phiên bản.
13. **Khả năng tiếp cận**: điều hướng bàn phím giữa các node, nhãn ARIA cho node/dây, tương
    phản tốt cho chế độ nối dây; giao diện trên máy tính bảng.

## 4. Quy trình người dùng (quyền hạn)

- **Gửi duyệt cho người tạo topology.** User `OPERATOR` hay editor hiện không tạo được bản
  xem trước cấu hình, nên không thể đề xuất gì cho ADMIN. Bản xem trước chỉ là *đề xuất*
  (`pending_approval`), việc duyệt và áp dụng vẫn là ADMIN, vì vậy cho phép editor tạo bản
  xem trước **không làm yếu** quy tắc an toàn hiện có. Thêm: ghi chú gửi kèm, thông báo cho
  ADMIN, trạng thái "đang chờ duyệt" hiện trên project.
- **Hạn mức**: hiện không giới hạn số project/thiết bị mỗi user, và tạo hàng loạt không bị
  giới hạn tần suất. Cần quota theo user (ADMIN chỉnh được) và giới hạn tần suất.
- **Lịch sử thay đổi sơ đồ** (ai sửa gì, khi nào) để hai người cùng sửa không ghi đè nhau;
  hiện là "người ghi sau thắng".
- **Thành viên project**: tìm user khi chia sẻ (hiện phải gõ đúng tên), vai trò mô tả rõ hơn,
  người được chia sẻ có thể rời project.

## 5. Đề xuất thực hiện

| Giai đoạn | Nội dung | Vì sao trước |
|---|---|---|
| **A. Làm sơ đồ dùng được** (nhanh, ít rủi ro) | Phóng to/kéo nền/vừa khung + minimap; cập nhật kéo theo từng node; tự bố trí (phân tầng, vòng, sao) và sắp tự nhiên; nhấp đúp đổi tên; sao chép/dán; chọn cổng từ danh sách; lưới dính; chú giải phím tắt | Sửa đúng các điểm giật/khó dùng đã đo được, không đổi dữ liệu |
| **B. Tạo nhanh và đa dạng** | Thư viện mẫu topology + trình sinh theo số lượng; IPAM tự chia subnet; thiết bị unmanaged (PC, server, cloud, AP); trạng thái planned/provisioned; bảng kiểm tra; ghi chú và nhóm | Đáp ứng trực tiếp "đa dạng topology" và "dễ tạo" |
| **C. Kết nối nâng cao và cộng tác** | Đoạn mạng dùng chung, port-channel, router-on-a-stick, loopback; snapshot/phiên bản và diff; gửi duyệt cho editor; quota; lịch sử thay đổi | Cần đổi mô hình dữ liệu và quy trình, nên làm sau khi A, B ổn định |

Rủi ro chính: B và C đổi lược đồ (cột `kind`, `managed`, `lifecycle` trên thiết bị; bảng
mẫu, nhóm, ghi chú, phiên bản, đoạn mạng). Các CHECK trong DB và logic sinh cấu hình phải
được cập nhật cùng lúc: thiết bị unmanaged phải bị loại khỏi poll, khám phá, sinh cấu hình
và nhập/xuất CSV.
