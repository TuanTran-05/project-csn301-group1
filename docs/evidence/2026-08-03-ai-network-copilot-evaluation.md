# AI Network Copilot — trạng thái triển khai

## Phạm vi

Đồ án tập trung vào hỗ trợ cấu hình và giám sát Cisco IOS qua AI, với Preview → Approve → Apply → Verify. AI không được tự mở SSH trong luồng chat; mọi thay đổi phải đi qua policy, snapshot capability, backup và xác minh backend.

## Kiến trúc đã triển khai

- Policy AI-safe tách khỏi policy vận hành; các lệnh đọc full configuration bị chặn trước SSH.
- Snapshot capability đóng băng tại Preview, gồm tier, family, expectation, verification plan và rollback guidance.
- Verification engine dùng kế hoạch backend đóng băng; output running/startup-config được đánh dấu nhạy cảm và serialize rỗng.
- Parser IOS hỗ trợ alias interface, VLAN, switchport/trunk, interface stanza, ACL và DHCP pool.
- Evaluation corpus gồm 50 case theo phân bố thiết kế; runner fake provider không mở SSH.

## Ma trận năng lực

| Nhóm | Trạng thái |
|---|---|
| VLAN, access/trunk port | Automated-test verified |
| Interface description/admin/IPv4 | Automated-test verified |
| Static/default route | Automated-test verified |
| Save configuration | Automated-test verified, evidence nhạy cảm được che |
| Standard IPv4 ACL | Automated-test verified; cần live evidence |
| IOS DHCP pool | Automated-test verified; cần live evidence |
| Single-area OSPF | Automated-test verified; cần live evidence |
| Full NAT, advanced routing, ASA config, multi-vendor | Preview-only / ngoài phạm vi |

## Kiểm thử và tái lập

- Automated gate: **681 passed** in 104.32 seconds (`pytest -q`).
- Migration round-trip: upgrade → downgrade `1d6734caee3b` → upgrade; final head `6f2a1c8d90be`.
- Fake corpus run: 50 cases, structured validity 1.0, unsafe SSH 0.

## An toàn và giới hạn

Không tự động rollback; rollback chỉ là hướng dẫn dựa trên backup. Chưa có bằng chứng PNETLab/live-provider trong môi trường hiện tại; các extension vì vậy chưa được tuyên bố live-verified.

## Tái lập

```powershell
..\.venv\Scripts\python.exe -m pytest -q
..\.venv\Scripts\python.exe scripts\evaluate_ai.py --provider fake --corpus evaluation\prompt_corpus.json --output-dir artifacts\evaluation
..\.venv\Scripts\python.exe scripts\course_evidence.py evaluation\pnetlab_scenario.example.json
```

Báo cáo này không chứa credential, management IP hoặc raw full configuration.
