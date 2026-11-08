# Kế hoạch viết lại report EIUSC2026 (ID131): AI-Assisted Network Management System

> Đối chiếu bản report cũ (`EIUSC2026-ID131-AI_Assisted_System.docx`) với mã nguồn hiện tại
> trên `main` (commit `c6acfda`, 08-11-2026). Mọi tính năng được nêu dưới đây đều có mã nguồn
> và test tương ứng trong `backend/`; mục nào chưa kiểm chứng trên lab thật được ghi rõ.

---

## 1. Hệ thống hiện tại so với bản report cũ

### 1.1 Số liệu tổng quan (đo lại ngày 08-11-2026)

| Chỉ số | Bản report cũ (ngầm định) | Hiện tại |
|---|---|---|
| Mã Python backend | ~ vài nghìn dòng, 14 module | **≈ 23.900 dòng**, 25 package (`ai`, `changes`, `topology`, `governance`, `checkpoints`, `integrations`, `history_extension`, `realtime`, `projects`, `groups`…) |
| Giao diện | 1 trang chat + dashboard | 4 trang (Chat/Copilot, Projects + Canvas sơ đồ mạng, Users, Dashboard), ≈ 7.600 dòng JS, song ngữ **VI/EN** |
| API | ~ 20 endpoint | **176 route** REST + SSE |
| Bảng CSDL | ~ 10 bảng | **≈ 45 bảng**, 30 migration Alembic, CHECK/FK/UNIQUE đầy đủ |
| Kiểm thử | 697 test (08-2026) | **1.930 test pass** trong 483 s (`pytest -q`, đã chạy lại) |
| Vendor | Cisco IOS (đọc ASA) | Cisco IOS/IOSv-L2, Cisco ASA, **Palo Alto PAN-OS** (Netmiko), thiết bị không quản trị (PC, server, cloud, AP, segment) |
| Vai trò | ADMIN duy nhất được cấu hình | **ADMIN / TEACHER / STUDENT** (+ OPERATOR / VIEWER theo project), quyền cấp theo phạm vi, ủy quyền, hết hạn |

### 1.2 Những gì báo cáo cũ **chưa có** (cần đưa vào)

| Nhóm | Tính năng mới | Mã nguồn chính |
|---|---|---|
| **AI** | *Intent router* dùng LLM (design / read / configure / chat) với lịch sử hội thoại và tên thiết bị; luật từ khóa làm dự phòng | `ai/intent.py`, `scripts/eval_intent_router.py` |
| | *AI Design Advisor*: từ câu tự nhiên → thiết kế L2/L3 đầy đủ (VLAN, access port, SVI, loopback, subinterface, static route, OSPF, addressing pool); thử áp dụng rồi rollback để kiểm tra, tối đa 2 lần sinh lại | `ai/design_assistant.py` |
| | Ngữ cảnh topology an toàn: VLAN↔subnet↔gateway suy ra từ snapshot, **không bao giờ** gửi IP quản trị, credential, full running-config | `ai/topology.py`, `ai/service.py::build_context` |
| | Tạo project/topology bằng hội thoại ("xây 1 hệ thống mạng gồm 1 router, 1 switch, 6 PC") → chọn mẫu → nhập sơ đồ vào canvas | `chat/onboarding.py`, `topology/chat_import.py` |
| | Hai provider: Gemini (mặc định, `gemini-2.5-flash`) và Anthropic Claude; `AI_ROUTER_MODEL` riêng cho phân loại | `ai/provider.py` |
| **Thiết kế mạng** | Canvas kiểu Packet Tracer: kéo thả, nối dây chọn cổng tự động, zoom/pan/minimap, auto-layout, undo/redo, copy/paste | `static/js/canvas.js`, `network_diagram.js` |
| | Mẫu topology (star, branch, campus, DMZ, ring, hub-and-spoke), IPAM tự động (`auto`), lint thiết kế | `topology/blueprints.py`, `topology/lint.py` |
| | Sinh cấu hình từ sơ đồ (IOS + ASA), port-channel, router-on-a-stick, loopback | `topology/configgen.py`, `topology/design.py` |
| | Phiên bản sơ đồ (lưu/so sánh/khôi phục), nhập/xuất JSON, CSV, SVG/PNG | `topology/versions.py`, `projects/exchange.py` |
| | Khám phá CDP/LLDP và so sánh **thiết kế ↔ thực tế** | `topology/discovery.py` |
| **Tích hợp PNETLab** | Nhập lab (session API PNETLab 4/5, fallback EVE-NG), chống SSRF | `topology/pnetlab_import.py`, `integrations/pnetlab.py` |
| | **Xuất project thành lab PNETLab mới** (node, link, Cloud0), tự xóa nếu lỗi giữa chừng | `topology/pnetlab_export.py`, `integrations/pnetlab_writer.py` |
| | **Bootstrap SSH qua console telnet** (IOS và PA-VM): đặt hostname, IP quản trị, user, RSA, SSH v2 — chạy nền, khởi động theo thứ tự | `topology/bootstrap.py`, `bootstrap_panos.py`, `bootstrap_jobs.py`, `integrations/console.py` |
| **Governance / giáo dục** | Nhóm lớp, lời mời/duyệt tham gia, grant theo phạm vi (nhóm × thiết bị × môi trường × thời hạn), ủy quyền Teacher→Student | `governance/access.py`, `groups/` |
| | Phân loại rủi ro tự động Low/High theo lệnh, vai trò thiết bị, môi trường; *Smart Approval* cho Low Risk | `governance/workflow.py`, `changes/capabilities.py` |
| | Approval digest (ràng buộc lệnh, thiết bị, baseline, policy; hết hạn 60 phút), baseline drift check, device lease chống ghi đồng thời, cửa sổ bảo trì, reservation | `governance/workflow.py` |
| | Bài tập, bản nháp có phiên bản + diff, nộp bài, chấm điểm, lab template, quota AI/ngày, thông báo | `governance/learning.py` |
| **An toàn thay đổi** | **Checkpoint Undo/Redo cấu hình đã áp dụng**: so sánh cấu trúc running-config → sinh lệnh khôi phục → đi lại Preview→Approve→Apply→Verify (không ghi thẳng thiết bị) | `checkpoints/service.py`, `checkpoints/diff.py` |
| | Lịch sử/undo cho preview đang chờ, backup mã hóa có xác thực, audit allowlist | `history_extension/` |
| | Thùng rác thiết bị: token khôi phục được niêm phong bằng khóa credential | `devices/trash.py` |
| **Vận hành** | SSE realtime (trễ ≈ 2 s thay vì 7–15 s polling), monitoring song song có giới hạn | `realtime/`, `monitoring/service.py` |
| | Parser PAN-OS (system info, interface, route theo virtual router) | `parsers/panos.py` |

### 1.3 Chỗ sai / lỗi thời trong bản cũ phải sửa

1. **Abstract & Methodology**: "configuration changes must be reviewed by the admin user" → nay là quy trình nhiều vai trò (Teacher duyệt, Student triển khai khi có grant, Smart Approval cho Low Risk).
2. **Phân loại yêu cầu**: bản cũ chỉ nói `monitor/configure/troubleshoot/chat`. Hiện có **2 tầng**: (i) router chọn pipeline `design/read/configure/chat`; (ii) `AIAction` có cấu trúc `monitor/configure/troubleshoot/chat`. Phải mô tả cả hai.
3. **"If the process fails, the backup is activated"** — sai. Hệ thống **không tự rollback**. Đúng là: backup trước apply; verify sau apply; lỗi → `failed/partial_success`; khôi phục bằng *checkpoint* tạo preview mới và phải duyệt lại.
4. **Conclusion**: "mainly designed for Cisco… future: multi-vendor" → đã hỗ trợ PAN-OS; viết lại phần hạn chế.
5. **Table 1**: ô Frontend ghi "Alpine.jsChat interface" (lỗi dính chữ); thiếu Netmiko, requests, SSE, Gunicorn gthread, i18n; "Gemini/ Anthropic" để ô trống.
6. **Test-bed**: "Based on Table 1, the R1, S1…" → phải là **Table 2**. Bảng IP cần cập nhật theo lab hiện tại (thêm firewall PA-VM nếu dùng, folder PNETLab `/Courses/CSN301-StudentLab/Group-1`).
7. Câu lặp: "Output parsers convert raw CLI results…" xuất hiện hai lần.
8. Introduction: câu "Their study reported improvements in student learning…" không rõ chủ ngữ (đứng sau Kim et al. về multi-agent) — chuyển lên sau Md Enzai / Soto et al.
9. Evaluation chỉ có 3 ví dụ định tính (show IP, show VLAN, xóa VLAN) — **không có số liệu**. Cần thêm đánh giá định lượng (mục 4).
10. Hình 1 (kiến trúc) và Hình 3 (workflow) đã lỗi thời: thiếu intent router, design advisor, governance, PNETLab, checkpoint.

---

## 2. Cấu trúc report mới (đề xuất)

Giữ khuôn 4 phần của hội nghị nhưng tách Methodology thành các tiểu mục rõ ràng. Nếu giới hạn trang
chặt (thường 6–8 trang), ưu tiên theo nhãn **[Bắt buộc] / [Nên có] / [Nếu còn chỗ]**.

### Title (đề xuất)
*"NetAI: A Safety-Governed AI Copilot for Designing, Deploying and Managing Networking Labs in Education"*
(hoặc giữ tên cũ, thêm "with Human-in-the-Loop Change Control").

### Abstract (≈ 200 từ) [Bắt buộc]
Viết lại theo 5 ý:
1. Vấn đề: sinh viên khó cấu hình/khắc phục sự cố; trợ lý AI tổng quát không biết trạng thái mạng thật và nguy hiểm nếu được tự chạy lệnh.
2. Giải pháp: hệ thống web nối LLM với thiết bị thật (Cisco IOS/ASA, Palo Alto) qua SSH và PNETLab, bao phủ cả vòng đời **design → deploy → monitor → configure → undo**.
3. Điểm mới: intent router dùng LLM; AI design advisor sinh thiết kế L2/L3 kiểm chứng được; xuất lab sang PNETLab và bootstrap SSH qua console; quản trị theo vai trò Admin/Teacher/Student với phân loại rủi ro tự động, approval digest, backup, verify và checkpoint undo.
4. Nguyên tắc: AI chỉ *đề xuất*, backend *quyết định*; AI không bao giờ thấy credential/IP quản trị.
5. Kết quả: 1.930 test tự động; độ chính xác router X%, tỷ lệ thiết kế hợp lệ Y%, 0 lệnh SSH không an toàn trên Z kịch bản; demo trên lab PNETLab CSN301.

**Keywords**: Large Language Models, Network Management, Networking Education, Human-in-the-Loop, Intent-Based Networking, PNETLab.

### I. INTRODUCTION [Bắt buộc]
- **Đoạn 1 – bối cảnh** (giữ nguyên ý cũ, rút gọn).
- **Đoạn 2 – related work giáo dục** (Budianto, Verdú, Ahmed & Hasnine, Md Enzai, Soto) + câu "Their study reported…" đặt đúng chỗ.
- **Đoạn 3 – related work LLM cho cấu hình mạng** (NetConfEval, LLM-NetCFG, Intent-LLM, NAPALM, Jiang, Kim). Thêm nhận xét: các hệ này nhắm tới *tự động hóa tối đa*, trong khi môi trường lớp học cần *kiểm soát và phân quyền*.
- **Đoạn 4 – khoảng trống**: (a) AI không có ngữ cảnh mạng thật; (b) rủi ro khi AI chạy lệnh; (c) **chưa có hệ nào gắn AI với vòng đời lab giáo dục** (thiết kế sơ đồ → dựng lab → cấu hình → chấm bài) và **phân quyền theo vai trò lớp học**.
- **Đoạn 5 – đóng góp** (viết dạng liệt kê, mới):
  1. Kiến trúc "AI proposes, backend disposes" với hai tầng phân loại ý định và chính sách kiểm tra ở server.
  2. AI design advisor + canvas sinh cấu hình, nối với PNETLab (import/export/bootstrap) để đi từ câu mô tả đến lab chạy được.
  3. Mô hình governance cho giáo dục: grant theo phạm vi, rủi ro tự động, Smart Approval, approval digest, lease, baseline, checkpoint undo.
  4. Đánh giá định lượng (test, router, design, an toàn) và minh họa trên lab thật.
- **Đoạn 6 – bố cục bài**.

### II. SYSTEM DESIGN (thay cho "Methodology")

#### A. Design Principles [Bắt buộc] — *mới*
4–5 nguyên tắc, mỗi nguyên tắc 1–2 câu và trỏ đến cơ chế thực thi:
- P1 *AI proposes, backend disposes* — model không mở SSH, không quyết định quyền.
- P2 *Least context* — AI chỉ nhận hostname/kind/role, topology suy ra; không credential, không IP quản trị, không full config.
- P3 *Deny by default* — allowlist lệnh đọc; grant đánh giá theo một phạm vi đầy đủ, không cộng dồn.
- P4 *Every write is reversible and auditable* — backup trước, verify sau, checkpoint, audit đã redact.
- P5 *Project isolation* — thiết bị, chat, change, audit, ngữ cảnh AI không vượt project.

#### B. System Architecture [Bắt buộc] — *vẽ lại Hình 1*
Hình 1 mới gồm 4 lớp:
1. **Presentation**: Chat/Copilot, Canvas sơ đồ, Dashboard, Users (Alpine.js, SSE, VI/EN).
2. **Application (Flask Blueprints)**: Auth & Governance · AI (Intent Router, AI Service, Design Advisor) · Topology (Designer, Config Generator, Discovery, Versions) · Change Control (Preview/Batch, Risk, Approval, Apply, Verify, Checkpoints) · Monitoring · Audit.
3. **Integration**: SSH adapters (Paramiko cho IOS/ASA, Netmiko cho PAN-OS), PNETLab API client (import/writer), Telnet console (bootstrap), LLM providers (Gemini/Claude).
4. **Data**: SQLAlchemy/SQLite (~45 bảng), credential mã hóa Fernet.
Bên phải: PNETLab host với các node Cisco/PA-VM trên Cloud0 (MGMT-NET).

**Table 1 (cập nhật)** — Công nghệ: thêm dòng *Netmiko* (PAN-OS), *requests* (PNETLab API), *SSE* (realtime), *Gunicorn gthread*, *i18n VI/EN*; sửa ô AI tool: "Google Gemini (default) / Anthropic Claude (optional); separate router model".

#### C. AI Pipeline [Bắt buộc] — *mở rộng lớn*
- **C1. Intent routing (mới).** Một lời gọi LLM ngắn, đầu vào: tin nhắn + ≤ N lượt gần nhất + tên/kind thiết bị + cờ "trả lời trước là thiết kế"; đầu ra JSON theo schema một trong `design/read/configure/chat`. Lỗi/không chắc → luật từ khóa dự phòng. Nhấn mạnh: *nhãn chỉ chọn pipeline, không cấp quyền*. Ví dụ hội thoại thật: "xây 1 hệ thống mạng…" → design; "đặt dải ip là 172.20.5.0/24" → vẫn design; "chạy show running config trên sw1" → read.
- **C2. Structured action.** `AIAction` (Pydantic) với `intent ∈ {monitor, configure, troubleshoot, chat}`, operations {hostname, execution_mode, commands, verification}; validate schema + ràng buộc intent; troubleshoot gửi output đã redact cho LLM để giải thích.
- **C3. Safe context.** Topology suy ra từ snapshot (VLAN, subnet, gateway, interface), loại mạng chứa IP quản trị; AI template (do Admin quản lý) được thêm vào prompt nhưng không thay kiểm tra quyền.
- **C4. AI Design Advisor (mới).** Đầu vào mô tả tự nhiên; model trả về *toàn bộ* thiết kế mong muốn; backend áp thử vào CSDL trong transaction → chuẩn hóa (địa chỉ `auto` thành thật) → kiểm tra → **rollback**; sai thì gửi lỗi cho model sinh lại (tối đa 2 lần). Người dùng chấp nhận mới ghi, và chỉ tới thiết bị qua Preview→Approve→Apply.
- **Hình mới (Fig. 2)**: sơ đồ pipeline AI: message → router → {chat | read | configure | design} → validation/policy → kết quả.

#### D. Network Design and Lab Provisioning [Bắt buộc] — *hoàn toàn mới*
- Canvas, loại thiết bị (managed: router, L3 switch, switch, firewall; unmanaged: PC, server, cloud, AP, segment), vòng đời `planned → provisioned`.
- Blueprint + IPAM tự động + lint.
- Config generator: thứ tự VLAN → access port → link interface → SVI → static route → OSPF (IOS); nameif/security-level/route (ASA); kết quả đóng băng thành change batch.
- **PNETLab round-trip**: import lab → (hoặc) export project thành lab mới (node theo template, link p2p, Cloud0) → start node → bootstrap SSH qua telnet console (IOS; PA-VM chạy nền ~10–15 phút boot) → thiết bị sẵn sàng quản lý qua SSH. Lỗi giữa chừng tự xóa lab đã tạo.
- CDP/LLDP discovery so sánh thiết kế với thực tế (khớp / thiếu / không trả lời / chưa vẽ).
- **Hình mới (Fig. 3)**: luồng "Natural language → Design → Canvas → PNETLab lab → Bootstrap → Managed devices".

#### E. Safety-Governed Change Control [Bắt buộc] — *viết lại phần System Operation cũ*
- Vai trò & grant: bảng ngắn ADMIN / TEACHER / STUDENT (+ OPERATOR/VIEWER theo project), action `config.create/approve/deploy/restore`, `device.observe`, `ai.use`, `lab.*`; grant theo (nhóm, thiết bị, môi trường, hạn), ủy quyền `delegable`, thu hồi theo chuỗi.
- **Phân loại rủi ro tự động** (Table mới): Low = hostname, description, VLAN, access port trong lab ảo cô lập; High = routing/OSPF/ACL/IP, AAA/SSH/SNMP, xóa/shutdown/trunk, mọi thiết bị production/core/firewall. Một lệnh High → cả change High. Mức rủi ro **không** lấy từ AI hay người dùng.
- Workflow **Preview → Approve → (Confirm) → Backup → Apply → Verify → Checkpoint** với: approval digest + hết hạn; kiểm tra lại quyền/baseline/lịch/bảo trì ngay trước SSH; device lease; typed confirmation (hostname hoặc `CONFIRM ALL`); trạng thái `pending_approval / approved / running / success / failed / partial_success / cancelled`.
- **Checkpoint Undo/Redo**: diff cấu trúc running-config (theo indentation, bỏ secret/banner/cert) → lệnh khôi phục → preview `source="restore"` → duyệt như thay đổi thường.
- Audit redact, credential Fernet, rate limit, security headers.
- **Vẽ lại Fig. 4** (workflow cấu hình) có nhánh Smart Approval và Checkpoint.

#### F. Educational Features [Nên có] — *mới*
Nhóm lớp, lời mời, bài tập (thiết bị, thời gian, prefix cho phép), bản nháp có phiên bản + diff, nộp bài, chấm điểm 0–100, reservation thiết bị, quota AI/ngày, thông báo. Đây là điểm khác biệt chính so với các hệ LLM-NetCFG/Intent-LLM.

#### G. Real-time and Multi-vendor Support [Nếu còn chỗ]
SSE fingerprint theo project; monitoring song song có giới hạn; adapter PAN-OS (candidate config + commit, `revert config` khi lỗi); parser PAN-OS.

### III. IMPLEMENTATION AND EVALUATION

#### A. Test-bed [Bắt buộc] — *cập nhật Hình & Table 2*
- PNETLab server CSN301, folder `/Courses/CSN301-StudentLab/Group-1`; AI Server (Gunicorn) trên MGMT-NET 172.16.3.0/24; R1, S1, S2 (+ PA-VM nếu demo). Sửa "Based on Table 2".
- Thêm cột "Vendor/Image" và "Managed via" (SSH Paramiko / Netmiko) vào Table 2.

#### B. Functional Scenarios [Bắt buộc] — giữ 2–3 ví dụ cũ, thêm ví dụ mới
| # | Kịch bản | Minh họa điều gì | Ảnh cần chụp |
|---|---|---|---|
| S1 | "show IP R1" (giữ) | Hiểu yêu cầu không đúng cú pháp, parser bảng | có sẵn |
| S2 | "show vlan sw1" (giữ, có thể gộp với S1) | Đọc switch | có sẵn |
| S3 | **"Xây 1 hệ thống mạng gồm 1 router, 1 switch, 6 PC, dải 172.20.5.0/24"** | Router → design, advisor sinh thiết kế, nhập canvas | chat + canvas |
| S4 | **Export sang PNETLab + bootstrap SSH** | Từ sơ đồ ra lab chạy được | trang xác nhận export, bảng kết quả bootstrap, PNETLab UI |
| S5 | Xóa VLAN (giữ, High Risk, typed confirmation) | Change control | có sẵn |
| S6 | **Student tạo cấu hình → Teacher duyệt → Student triển khai**; Student bị từ chối khi không có grant | Governance | 2–3 ảnh |
| S7 | **Undo một thay đổi đã áp dụng** (checkpoint → restore preview → apply) | Khả năng phục hồi | panel checkpoint + preview |

#### C. Quantitative Evaluation [Bắt buộc] — *mới, quan trọng nhất để tăng chất lượng bài*
| Thí nghiệm | Cách đo | Lệnh / nguồn | Trạng thái |
|---|---|---|---|
| E1. Kiểm thử tự động | Số test, thời gian, độ phủ | `pytest -q` → **1.930 passed / 483 s**; thêm `--cov` để báo % coverage | Đã có số |
| E2. Intent router | Accuracy (và confusion matrix 4 lớp) trên tập hội thoại VI/EN; so sánh với luật từ khóa | `scripts/eval_intent_router.py` (mở rộng lên ≥ 50–100 câu) | Cần chạy với API key thật |
| E3. Structured action | Validity, intent accuracy, target accuracy, command match trên 50 case corpus (8 nhóm: switching, static route, ACL/DHCP/OSPF, monitor, troubleshoot, chat, dangerous, ambiguous) | `scripts/evaluate_ai.py --provider gemini` | Chạy fake chỉ cho validity 1.0 và unsafe SSH 0; cần chạy provider thật |
| E4. Safety | Số lệnh nguy hiểm/không cho phép lọt tới SSH; % High Risk bắt typed confirmation; % thử nghiệm Student không grant bị chặn | corpus nhóm `dangerous_unauthorized` + test governance | Có từ test; cần bảng tổng hợp |
| E5. Design advisor | % thiết kế hợp lệ lần 1 / sau 2 lần; số lỗi lint; thời gian | 10–20 mô tả topology khác nhau | Cần chạy |
| E6. Latency | Trung vị/p95 thời gian router, chat, read (SSH), apply+verify | log/trace evaluation | Cần chạy |
| E7. PNETLab | Thời gian export N node, bootstrap IOS/PA-VM, tỷ lệ thành công | lab CSN301 | Cần chạy |
| E8. (Nếu kịp) Khảo sát người dùng | 10–20 sinh viên, Likert 5 mức (dễ dùng, hữu ích, tin cậy) | form | Tùy chọn |

Trình bày bằng **Table 4 (kết quả định lượng)** và nếu có, một biểu đồ cột độ chính xác router theo lớp.

#### D. Discussion [Nên có] — *mới*
- So sánh với related work (bảng nhỏ): Ahmed & Hasnine / Soto (giáo dục, không nối thiết bị) · NetConfEval (đánh giá, không triển khai) · LLM-NetCFG / Intent-LLM (tự động, ít kiểm soát) · **Hệ thống này** (thiết bị thật + PNETLab + human-in-the-loop + phân quyền lớp học + undo).
- Bài học: LLM chỉ dùng cho hiểu ý định và đề xuất; tính an toàn đến từ backend; router LLM xử lý hội thoại tốt hơn luật từ khóa.

### IV. CONCLUSION AND FUTURE WORK [Bắt buộc] — *viết lại*
- Tóm tắt 3 đóng góp và kết quả số.
- **Hạn chế thật**: không tự rollback (chỉ checkpoint qua duyệt); OSPF một area; discovery chỉ IOS; bootstrap ASA làm tay; SQLite một node, job bootstrap trong bộ nhớ (1 worker Gunicorn); lab driver/simulator cô lập chưa có adapter thật; PNETLab chỉ một session/tài khoản; chưa có đánh giá người dùng quy mô lớn (nếu không làm E8).
- **Hướng phát triển**: thêm vendor (Juniper, MikroTik qua Netmiko), multi-area OSPF/BGP, mô phỏng cô lập trước khi apply, chấm bài tự động bằng so sánh trạng thái, PostgreSQL + hàng đợi job, nghiên cứu hiệu quả học tập.

### REFERENCES
Giữ toàn bộ danh sách cũ; **thêm**:
- Netmiko (Byers, K., 2026). *Netmiko documentation*. https://github.com/ktbyers/netmiko
- PNETLab (2026). *PNETLab documentation*. https://pnetlab.com
- Anthropic (2026). *Claude API documentation*. https://docs.anthropic.com
- Palo Alto Networks (2026). *PAN-OS CLI quick start*. https://docs.paloaltonetworks.com
- Cisco CDP/LLDP configuration guide (nếu viết mục discovery).
- 1–2 bài về human-in-the-loop / safety cho LLM agent và intent classification bằng LLM (tìm bài 2024–2026 để củng cố Design Principles).
- Russell & Norvig (2021) đã được trích trong bài nhưng **thiếu trong danh sách tham khảo** — bổ sung.

---

## 3. Danh sách hình & bảng cần chuẩn bị

| Mã | Nội dung | Trạng thái |
|---|---|---|
| Fig. 1 | Kiến trúc 4 lớp (mục II.B) | **Vẽ lại** |
| Fig. 2 | AI pipeline: router + 4 nhánh | **Mới** |
| Fig. 3 | Design → PNETLab → Bootstrap → Managed | **Mới** |
| Fig. 4 | Workflow change control có Smart Approval, Checkpoint | **Vẽ lại** từ Fig. 3 cũ |
| Fig. 5 | Test-bed | Cập nhật |
| Fig. 6–9 | Ảnh chụp kịch bản S1/S3/S4/S5–S7 | Chụp thêm S3, S4, S6, S7 |
| Table 1 | Công nghệ | Sửa |
| Table 2 | IP quản trị | Sửa (thêm cột vendor) |
| Table 3 | Phân loại rủi ro Low/High | **Mới** |
| Table 4 | Kết quả định lượng E1–E7 | **Mới** |
| Table 5 | So sánh với related work | **Mới** (nếu đủ trang) |

## 4. Thứ tự làm việc khuyến nghị

1. Chạy E2, E3, E5 với Gemini thật (cần `AI_API_KEY` trong `backend/.env`) — đây là phần thiếu lớn nhất.
2. Chụp ảnh S3, S4, S6, S7 trên lab CSN301.
3. Vẽ lại Fig. 1, 2, 3, 4.
4. Viết lại Abstract → Introduction (đóng góp) → II → III → IV.
5. Rà các lỗi ở mục 1.3 và bổ sung tham khảo.

Nếu bị giới hạn trang: gộp S1+S2 thành một hình, bỏ II.G và Table 5, rút II.F còn một đoạn.
