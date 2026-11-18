# AI Network Copilot — Hướng dẫn Cài đặt, Triển khai, Vận hành & Xử lý sự cố

> **Đề tài:** AI Network Copilot (NetAI): quản trị mạng Cisco/Palo Alto trong PNETLab bằng AI, qua SSH
> **Môn học:** CSN301 · **Nhóm:** Group 1
> **Hệ điều hành triển khai:** Ubuntu Server 24.04 LTS / Debian 12 (Python ≥ 3.11). Máy dev: Windows / Linux / macOS
> **Mô hình:** Flask backend all-in-one (API + giao diện web + scheduler) chạy trên node "AI Server" trong PNETLab, SQLite, LLM Gemini (mặc định) hoặc Anthropic
> **Cập nhật:** 18/11/2026 · Nhánh mã nguồn: `main`, commit `75f45b3d3f39fcc1680a2dbedc307656e975454c`
> **Mục tiêu:** Từ lúc `git clone` về đến khi hệ thống chạy được trong lab: cài môi trường, cấu hình biến, triển khai, giải thích từng luồng hoạt động, cơ chế rule/policy, phân loại chat, phân quyền user, vận hành hằng ngày và xử lý lỗi.
> **Mức kiểm chứng:** Tài liệu được đối chiếu với mã nguồn tại commit trên. Trong môi trường Linux (Python 3.13), các bước cài đặt, migration, seed, `/api/health`, đăng nhập và toàn bộ test suite (1995 test pass) **đã được chạy thử**. Các bước với thiết bị thật, PNETLab và AI key thật **chưa được kiểm chứng trong lab của nhóm**: cần thay IP, tài khoản, mật khẩu theo lab và kiểm tra kết quả thực tế.

<a id="top"></a>

## Mục lục

1. [Tổng quan và kiến trúc](#architecture)
2. [Yêu cầu hệ thống và chuẩn bị](#prerequisites)
3. [Cài đặt từ mã nguồn (máy dev / máy lab)](#install)
4. [Biến môi trường (`.env`): giải thích đầy đủ](#env)
5. [Triển khai lên AI Server trong PNETLab](#deploy)
6. [Khởi tạo dữ liệu: user, project, thiết bị, SSH](#bootstrap)
7. [Phân quyền người dùng](#rbac)
8. [Các luồng hoạt động của hệ thống](#flows)
9. [Cơ chế rule, policy và kiểm soát an toàn](#rules)
10. [Hướng dẫn sử dụng giao diện](#ui)
11. [Tập lệnh quản trị quan trọng (Cheat Sheet)](#commands)
12. [Sao lưu, khôi phục và nâng cấp](#backup)
13. [Khắc phục sự cố thường gặp](#troubleshooting)
14. [Kiểm thử](#testing)
15. [Hardening và vận hành an toàn](#security)
16. [Checklist nghiệm thu](#checklist)
17. [Tài liệu tham khảo](#references)

---

<a id="architecture"></a>
## 1. Tổng quan và kiến trúc

**AI Network Copilot** là một backend Flask giúp quản trị mạng Cisco (IOS, ASA) và Palo Alto (PAN-OS) trong PNETLab hoặc trên thiết bị thật, qua SSH. Người dùng chat bằng tiếng Việt hoặc tiếng Anh: hỏi trạng thái thiết bị, nhờ phân tích lỗi, thiết kế sơ đồ mạng hoặc yêu cầu cấu hình.

Nguyên tắc cốt lõi: **AI chỉ đề xuất, không bao giờ tự thực thi.** Mọi thay đổi cấu hình qua change workflow đều đi theo quy trình bắt buộc:

```text
Preview  →  Approve  →  Apply (backup trước)  →  Verify (kiểm tra sau)
```

**Ngoại lệ:** *Bootstrap SSH qua console* (và bước khởi động node khi "Đẩy sang PNETLab") ghi cấu hình trực tiếp lên thiết bị **không qua change workflow**, vì lúc đó thiết bị chưa SSH được. Luồng này có preview riêng, chỉ owner/ADMIN được chạy và có ghi audit (xem [6.5](#bootstrap)).

### 1.1. Sơ đồ kiến trúc

```text
   Trình duyệt (HTML + Alpine.js)                     LLM bên ngoài
   /, /projects, /dashboard, /users                Gemini (mặc định) /
              │  REST + JWT, SSE                     Anthropic (tùy chọn)
              ▼                                            ▲ HTTPS 443
 ┌───────────────────────── AI Server (Flask, :5000) ───────┼──────────┐
 │  app.py: request-id, security headers, error contract, redaction    │
 │  ┌────────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌───────────┐   │
 │  │ auth   │ │ projects │ │ ai / chat │ │ changes  │ │ topology  │   │
 │  │ JWT    │ │ scope    │ │ intent    │ │ preview/ │ │ canvas,   │   │
 │  │ roles  │ │ sharing  │ │ router    │ │ approve/ │ │ design,   │   │
 │  └────────┘ └──────────┘ └───────────┘ │ apply    │ │ PNETLab   │   │
 │  ┌────────────┐ ┌──────────┐ ┌───────┐ └──────────┘ └───────────┘   │
 │  │ governance │ │ commands │ │ audit │  ┌────────────┐ ┌─────────┐  │
 │  │ risk,      │ │ policy   │ │ redact│  │ monitoring │ │ backups │  │
 │  │ risk, lease│ │ allowlist│ └───────┘  │ APScheduler│ │checkpts │  │
 │  └────────────┘ └──────────┘            └────────────┘ └─────────┘  │
 │  ssh/ (Paramiko, Netmiko PAN-OS): kết nối SSH; integrations/: console telnet│
 │                  SQLite (SQLAlchemy + Alembic), credentials mã hóa Fernet │
 └──────────────────────────────┬──────────────────────────────────────┘
                                │ SSH TCP 22 (mạng quản trị, vd 172.16.3.0/24)
                                │ PNETLab API HTTP(S) + console telnet (import/bootstrap)
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
      R1 (IOS)            SW1 / SW2 (IOSv-L2)    ASA / PA-VM ...
```

| Thành phần | Vai trò |
|---|---|
| **Giao diện web** | Trang Chat/Copilot (`/`), Projects + canvas sơ đồ mạng (`/projects`), Dashboard (`/dashboard`), Quản lý user (`/users`). HTML + CSS + Alpine.js (vendored), không cần build frontend. |
| **Flask API** | Toàn bộ REST API `/api/...`, xác thực JWT, giới hạn tần suất, ghi audit. |
| **AI layer** (`ai/`) | Gọi LLM với ngữ cảnh inventory/topology đã lọc mật khẩu, IP quản trị và running-config đầy đủ (output chẩn đoán gửi cho AI chỉ được che bí mật, xem [8.3](#flow-chat)); ép output theo JSON schema, validate bằng Pydantic. |
| **Policy engine** (`commands/policy.py`, `governance/`) | Allowlist lệnh đọc, phát hiện lệnh nguy hiểm, phân loại rủi ro, kiểm tra quyền. |
| **Change workflow** (`changes/`) | Preview, duyệt, backup, áp dụng, xác minh, đề xuất rollback, batch nhiều thiết bị. |
| **SSH layer** (`ssh/`) | Paramiko (IOS/ASA) và Netmiko (`paloalto_panos`) cho mọi lệnh SSH. Ngoài ra `integrations/console.py` mở telnet console (bootstrap) và `integrations/pnetlab.py` gọi HTTP tới PNETLab. |
| **Monitoring** | APScheduler poll trạng thái định kỳ (tắt mặc định). |
| **Database** | SQLite qua SQLAlchemy 2, migration Alembic (`backend/migrations`). |

### 1.2. Công nghệ

| Lớp | Công nghệ |
|---|---|
| Ngôn ngữ | Python **3.11+** (3.10 lỗi lúc import vì dùng cú pháp `X \| None` khi chạy) |
| Web | Flask 3, Flask-JWT-Extended, Flask-Limiter, Flask-Migrate |
| ORM / DB | SQLAlchemy 2, SQLite (mặc định) |
| Validate | Pydantic 2 |
| SSH | Paramiko `>=3.4,<5` (giữ thuật toán SHA-1 KEX cho image IOSv cũ), Netmiko 4 |
| Lập lịch | APScheduler 3 |
| Mã hóa | `cryptography` (Fernet) |
| AI | `google-genai` (Gemini), tùy chọn `anthropic` |
| Test | pytest, pytest-cov (SSH và AI đều được giả lập) |

### 1.3. Cấu trúc thư mục

```text
project-csn301-group1/
├── backend/
│   ├── pyproject.toml            # dependency + cấu hình pytest
│   ├── .env.example              # mẫu biến môi trường
│   ├── wsgi.py                   # entrypoint: load .env rồi create_app()
│   ├── migrations/               # Alembic (flask db upgrade)
│   ├── scripts/                  # seed, quản lý user, smoke test, setup server, chẩn đoán
│   ├── tests/                    # ~2000 test (unit, API, e2e)
│   ├── evaluation/               # bộ prompt đánh giá AI
│   └── src/network_copilot/
│       ├── app.py, config.py, extensions.py, errors.py
│       ├── auth/ projects/ devices/ credentials/ ssh/ commands/ parsers/
│       ├── monitoring/ dashboard/ changes/ backups/ checkpoints/ audit/
│       ├── ai/ chat/ topology/ integrations/ realtime/
│       ├── governance/ groups/ history_extension/ evaluation/
│       ├── templates/            # các trang HTML
│       └── static/               # js, css, img, vendor/alpine.min.js
└── docs/                         # tài liệu thiết kế, database, governance
```

**Lưu ý:** Đây là kiến trúc một máy, phù hợp lab và triển khai nhỏ. Background job (bootstrap console, export PNETLab) và luồng SSE nằm trong bộ nhớ tiến trình, nên production chạy **1 worker Gunicorn với nhiều thread**.

[↑ Về mục lục](#top)

---

<a id="prerequisites"></a>
## 2. Yêu cầu hệ thống và chuẩn bị

### 2.1. Tài nguyên khuyến nghị

| Thành phần | AI Server (lab) | Máy dev |
|---|---|---|
| OS | Ubuntu 24.04 (Python 3.12) hoặc Debian 12 (Python 3.11) | Windows 10/11, Linux, macOS |
| Python | ≥ 3.11 | ≥ 3.11 |
| CPU / RAM | 2 vCPU / 2–4 GiB là đủ (LLM chạy ở cloud) | tùy |
| Disk | 5–10 GiB (DB SQLite, backup cấu hình, snapshot) | tùy |
| Network | IP tĩnh trong mạng quản trị (vd `172.16.3.28/24`), đi Internet HTTPS để gọi Gemini | Internet để `pip install` |
| Tài khoản AI | API key Gemini (<https://aistudio.google.com/apikey>) hoặc Anthropic | như bên |

> Ubuntu **22.04** có sẵn Python 3.10, **không chạy được**. Dùng 24.04, hoặc cài thêm `python3.11 python3.11-venv`.

### 2.2. Kiểm tra hệ thống

```bash
python3 --version          # phải >= 3.11
lsb_release -a
ip -br a
ip route
df -h
git --version
```

### 2.3. Cài tiện ích (Ubuntu/Debian)

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip curl sqlite3
```

### 2.4. Các cổng / kết nối quan trọng

| Cổng | Chiều | Chức năng | Phạm vi cho phép |
|---|---|---|---|
| 5000/TCP | vào AI Server | Web UI + REST API (Flask/Gunicorn) | Mạng quản trị / VPN lab |
| 22/TCP (hoặc `ssh_port` của thiết bị) | AI Server → thiết bị | SSH quản trị Cisco/PAN-OS | Mạng quản trị (`MANAGEMENT_NETWORK`) |
| 80/443/TCP | AI Server → PNETLab | API PNETLab (nhập/xuất lab) | Chỉ địa chỉ **private**, không theo redirect |
| Cổng console telnet của node | AI Server → PNETLab host | Bootstrap SSH qua console | Do PNETLab cấp cho từng node |
| 443/TCP | AI Server → Internet | Gọi Gemini / Anthropic API | Outbound |

**Không mở cổng 5000 ra Internet.** Ứng dụng chạy HTTP thuần; nếu cần truy cập từ xa, đặt sau reverse proxy HTTPS hoặc VPN.

[↑ Về mục lục](#top)

---

<a id="install"></a>
## 3. Cài đặt từ mã nguồn (máy dev / máy lab)

### Bước 1: Clone mã nguồn

```bash
git clone https://github.com/TuanTran-05/project-csn301-group1.git
cd project-csn301-group1
git checkout 75f45b3d3f39fcc1680a2dbedc307656e975454c   # đúng commit mà tài liệu này mô tả
cd backend
```

Bỏ bước `git checkout` nếu muốn chạy bản mới nhất của `main`; khi đó cần đối chiếu lại tài liệu với code.

### Bước 2: Tạo môi trường ảo và cài dependency

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e ".[dev]"
```

Muốn dùng Claude thay Gemini thì cài thêm extra `anthropic`:

```bash
pip install -e ".[dev,anthropic]"
```

### Bước 3: Tạo file `.env`

```bash
cp .env.example .env
```

Sinh các khóa bí mật và dán vào `.env`:

```bash
# CREDENTIAL_ENCRYPTION_KEY: khóa Fernet mã hóa mật khẩu SSH của thiết bị
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# SECRET_KEY và JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(48))"
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Tối thiểu cần điền: `SECRET_KEY`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, `AI_API_KEY`. Mọi biến được giải thích ở [mục 4](#env).

> **Cảnh báo:** **Mất `CREDENTIAL_ENCRYPTION_KEY` thì toàn bộ mật khẩu SSH đã lưu không giải mã được nữa.** Sao lưu khóa này ở nơi an toàn, không commit `.env` lên Git (`.gitignore` đã chặn sẵn).

### Bước 4: Tạo database (migration)

```bash
flask --app wsgi db upgrade
```

Lệnh này tạo `backend/network_copilot.db` (đường dẫn sqlite tương đối luôn được neo vào thư mục `backend/`, xem `config.py::_database_uri`).

### Bước 5: Tạo tài khoản ADMIN và lab mẫu

Cách 1: seed lab PNETLab mẫu (project `PNETLab` với `R1`, `SW1`, `SW2`):

```bash
export SEED_ADMIN_PASSWORD='<mật-khẩu-bạn-tự-chọn>'      # Windows: $env:SEED_ADMIN_PASSWORD='...'
export LAB_SSH_USERNAME='g1'                             # tùy chọn: login SSH chung cho thiết bị
export LAB_SSH_PASSWORD='<mật-khẩu-ssh-thiết-bị>'
python scripts/seed_lab.py
```

Cách 2: chỉ tạo ADMIN, không seed thiết bị:

```bash
python scripts/manage_users.py ensure-admin admin        # hỏi mật khẩu, không hiển thị
```

> `ensure-admin` sẽ **nâng quyền và kích hoạt lại** một tài khoản nếu username đó đã tồn tại (giữ nguyên mật khẩu cũ). Hãy chọn đúng username.

Hệ thống cho phép **tối đa một ADMIN** (unique index `uq_single_admin`), nên `ensure-admin` không nâng được user thứ hai khi đã có ADMIN. Repository không chứa mật khẩu mặc định nào.

### Bước 6: Chạy server

```bash
flask --app wsgi run --host 0.0.0.0 --port 5000
```

Mở trình duyệt: `http://127.0.0.1:5000/` và đăng nhập bằng tài khoản vừa tạo.

### Bước 7: Kiểm tra nhanh

```bash
curl -s http://127.0.0.1:5000/api/health
# {"status":"ok"}

curl -s -X POST http://127.0.0.1:5000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<mật-khẩu>"}'
# {"access_token":"eyJ...", ...}
```

> Các bước trên đã được chạy thử với Python 3.13 khi biên soạn: migration, seed, `/api/health` và đăng nhập đều thành công. `/api/health` chỉ chứng minh ứng dụng đang trả lời, chưa chứng minh AI hay SSH hoạt động.

[↑ Về mục lục](#top)

---

<a id="env"></a>
## 4. Biến môi trường (`.env`): giải thích đầy đủ

`wsgi.py` và các script gọi `load_dotenv()`, nên biến được đọc từ `backend/.env` hoặc từ môi trường hệ thống. Định nghĩa nằm trong `src/network_copilot/config.py`.

### 4.1. Bảo mật và database

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `SECRET_KEY` | `dev-secret-change-me` | Khóa bí mật Flask. **Bắt buộc đổi.** |
| `JWT_SECRET_KEY` | = `SECRET_KEY` | Khóa ký JWT. **Bắt buộc đổi.** |
| `JWT_ACCESS_TOKEN_MINUTES` | `60` | Thời hạn access token (phút). |
| `CREDENTIAL_ENCRYPTION_KEY` | *(trống)* | Khóa Fernet mã hóa credential SSH. **Bắt buộc**, chỉ đọc từ môi trường. |
| `DATABASE_URL` | `sqlite:///network_copilot.db` | URI database; đường dẫn tương đối được neo vào `backend/`. |
| `SQLITE_FOREIGN_KEYS` | `true` | Bật `PRAGMA foreign_keys=ON` để ràng buộc khóa ngoại có hiệu lực. |

### 4.2. Mạng lab, SSH và PNETLab

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `MANAGEMENT_NETWORK` | `172.16.3.0/24` | Dải quản trị mặc định (project có thể khai báo dải riêng, private, rộng tối đa /16). |
| `SSH_CONNECT_TIMEOUT` | `10` | Timeout kết nối SSH (giây). |
| `SSH_COMMAND_TIMEOUT` | `30` | Timeout mỗi lệnh SSH (giây). |
| `LAB_SSH_USERNAME` / `LAB_SSH_PASSWORD` | `g1` / `group1` (trong code) | Login SSH dùng cho `seed_lab.py`, `smoke_test_lab.py` và cho các node mà "Đẩy sang PNETLab" khởi tạo. |
| `PNETLAB_EXPORT_FOLDER` | `/Courses/CSN301-StudentLab/Group-1` | Thư mục trên PNETLab mà tài khoản export có quyền ghi. |
| `PNETLAB_BOOT_TIMEOUT` | `900` | Thời gian chờ node boot khi export và khởi động (giây). |
| `PNETLAB_BOOT_RETRY_SECONDS` | `30` | Chu kỳ thử lại console khi node đang boot. |
| `PNETLAB_EXPORT_PING_CHECK` | `true` | Ping IP quản trị trước khi cấp, để tránh trùng IP trên LAN lab. Ping không trả lời **không bảo đảm** IP còn trống (thiết bị có thể chặn ICMP). |
| `VERIFICATION_RETRIES` / `VERIFICATION_RETRY_SECONDS` | `2` / `5` | Số lần và khoảng cách đọc lại khi verify sau apply thất bại (link/trunk cần thời gian lên). |

> **Hai giá trị không chỉnh được qua `.env`:** `BOOTSTRAP_PANOS_BOOT_SECONDS` (900s, chờ PA-VM boot) và `PANOS_COMMIT_TIMEOUT` (600s, timeout `commit` PAN-OS) là **Flask config key** được đọc bằng `current_app.config.get(..., mặc định)`, nhưng `Config` không nạp chúng từ biến môi trường. Thêm vào `.env` sẽ **không có tác dụng**; muốn đổi phải sửa `config.py` (thêm `os.environ.get`) hoặc truyền class cấu hình riêng vào `create_app()`.

> **Lưu ý về `.env.example`:** `LAB_SSH_USERNAME` và `LAB_SSH_PASSWORD` xuất hiện **hai lần** (một lần có giá trị `g1`/`group1`, một lần để trống ở cuối file). Với python-dotenv, dòng sau ghi đè dòng trước, nên giá trị cuối cùng là **rỗng**. Hãy giữ một dòng duy nhất cho mỗi biến.

### 4.3. AI

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AI_PROVIDER` | `gemini` | `gemini` hoặc `anthropic`. |
| `AI_MODEL` | `gemini-2.5-flash` | Model chính. Gợi ý: `gemini-2.5-flash-lite` (rẻ nhất), `gemini-2.5-flash`, `gemini-2.5-pro`. Với Anthropic: vd `claude-sonnet-5`. |
| `AI_API_KEY` | *(trống)* | API key. Thiếu key thì mọi API khác vẫn chạy. Trong `/api/ai/chat`, các nhánh cần gọi AI provider trả `503 ai_not_configured`; các nhánh xử lý trực tiếp không cần model (vd `show run R1`, onboarding tạo project) vẫn có thể hoạt động. Gọi AI có thể phát sinh chi phí theo model; kiểm tra quota của tài khoản. |
| `AI_INTENT_ROUTER` | `true` | Bật phân loại bằng model khi luồng chat đi tới nhánh `_wants_design()` và đủ điều kiện; các tin nhắn khác xử lý theo nhánh tương ứng (xem [8.3](#flow-chat)). `false`: nhánh đó chỉ dùng rule từ khóa. |
| `AI_ROUTER_MODEL` | *(trống, dùng `AI_MODEL`)* | Model cho router; nên chọn model nhanh, rẻ (vd `gemini-2.5-flash-lite`). |

### 4.4. Monitoring, realtime, giới hạn

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `MONITORING_ENABLED` | `false` | Bật scheduler poll thiết bị định kỳ. |
| `MONITORING_INTERVAL_SECONDS` | `60` | Chu kỳ poll. |
| `MONITORING_MAX_WORKERS` | `4` | Số thiết bị poll song song. |
| `CONFIG_CHECKPOINTS_ENABLED` | `true` | Ghi bước undo/redo cho mỗi thay đổi đã apply. |
| `REALTIME_ENABLED` | `true` | Server-Sent Events cho trang chat. `false` thì UI tự chuyển sang polling. |
| `REALTIME_POLL_SECONDS` / `REALTIME_MAX_SECONDS` / `REALTIME_MAX_STREAMS` | `2` / `55` / `20` | Chu kỳ kiểm tra, thời gian giữ một stream, số stream tối đa mỗi tiến trình. |
| `MAX_PROJECTS_PER_USER` / `MAX_DEVICES_PER_PROJECT` / `MAX_LINKS_PER_PROJECT` | `20` / `300` / `1000` | Hạn mức (ADMIN miễn hạn mức project; `0` = tắt). |
| `RATELIMIT_ENABLED` / `RATELIMIT_STORAGE_URI` | `true` / `memory://` | Giới hạn tần suất request. `memory://` đếm riêng trong từng tiến trình, không dùng chung giữa nhiều worker. |

### 4.5. Biến chỉ dùng cho script

| Biến | Dùng bởi | Ý nghĩa |
|---|---|---|
| `SEED_ADMIN_USERNAME` | `seed_lab.py` | Tên ADMIN (mặc định `admin`). |
| `SEED_ADMIN_PASSWORD` | `seed_lab.py` | **Bắt buộc** khi seed lần đầu. |
| `SEED_PROJECT_NAME` | `seed_lab.py`, config | Tên project lab mặc định (mặc định `PNETLab`). |
| `NEW_USER_PASSWORD` | `manage_users.py` | Mật khẩu không cần nhập tương tác. |

### 4.6. Ví dụ `.env` cho AI Server

```dotenv
SECRET_KEY=<token_urlsafe_48>
JWT_SECRET_KEY=<token_urlsafe_48>
JWT_ACCESS_TOKEN_MINUTES=60
DATABASE_URL=sqlite:///network_copilot.db
CREDENTIAL_ENCRYPTION_KEY=<fernet_key>

MANAGEMENT_NETWORK=172.16.3.0/24
SSH_CONNECT_TIMEOUT=10
SSH_COMMAND_TIMEOUT=30
LAB_SSH_USERNAME=g1
LAB_SSH_PASSWORD=<mật-khẩu-ssh-lab>
PNETLAB_EXPORT_FOLDER=/Courses/CSN301-StudentLab/Group-1

MONITORING_ENABLED=true
MONITORING_INTERVAL_SECONDS=60

AI_PROVIDER=gemini
AI_MODEL=gemini-2.5-flash
AI_INTENT_ROUTER=true
AI_ROUTER_MODEL=gemini-2.5-flash-lite
AI_API_KEY=<gemini_api_key>

SEED_ADMIN_PASSWORD=<mật-khẩu-admin>
```

[↑ Về mục lục](#top)

---

<a id="deploy"></a>
## 5. Triển khai lên AI Server trong PNETLab

AI Server là một node Linux nằm trong PNETLab, có card mạng nối vào mạng quản trị của các thiết bị.

### Bước 1: Cấu hình mạng cho node

| Interface | Mạng | Địa chỉ | Gateway |
|---|---|---|---|
| `enp6s18` (kiểm tra bằng `ip link`) | management | `172.16.3.28/24` | `172.16.3.1` |

`/etc/netplan/01-lab.yaml`:

```yaml
network:
  version: 2
  ethernets:
    enp6s18:
      dhcp4: false
      addresses: [172.16.3.28/24]
      routes:
        - to: default
          via: 172.16.3.1
```

```bash
sudo netplan apply
ping -c 2 172.16.3.1
```

### Bước 2: Đưa mã nguồn lên node

Cách A, `git clone` trực tiếp (node có Internet):

```bash
git clone https://github.com/TuanTran-05/project-csn301-group1.git ~/network-copilot
git -C ~/network-copilot checkout 75f45b3d3f39fcc1680a2dbedc307656e975454c
```

Cách B, đóng gói từ máy dev (chỉ chứa file đã commit; `.venv`, `.env` và DB bị bỏ lại):

```bash
git rev-parse HEAD          # xác nhận đúng commit cần triển khai trước khi đóng gói
git archive --format=tar.gz -o network-copilot.tar.gz HEAD
scp network-copilot.tar.gz user@172.16.3.28:~/
```

```bash
mkdir -p ~/network-copilot && tar -xzf ~/network-copilot.tar.gz -C ~/network-copilot
```

### Bước 3: Chạy script setup tự động

```bash
cd ~/network-copilot/backend
chmod +x scripts/setup_ai_server.sh
./scripts/setup_ai_server.sh
```

Script `setup_ai_server.sh` chạy lại nhiều lần vẫn an toàn và **không in ra bí mật nào**. Nó thực hiện:

1. Tìm Python ≥ 3.11 (`python3.13` → `python3.11` → `python3`), dừng lại nếu không có.
2. Tạo `.venv`, nâng cấp pip, `pip install -e ".[dev]"`.
3. Nếu chưa có `.env`: copy từ `.env.example`, **tự sinh** `SECRET_KEY`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, rồi `chmod 600 .env`.
4. `flask db upgrade`.
5. Chạy toàn bộ test suite; dừng nếu có test fail.
6. Liệt kê các biến còn thiếu: `AI_API_KEY`, `LAB_SSH_USERNAME`, `LAB_SSH_PASSWORD`, `SEED_ADMIN_PASSWORD`.

Điền các biến còn thiếu (`nano .env`), sau đó:

```bash
./.venv/bin/python scripts/seed_lab.py          # ADMIN + project PNETLab + credential mã hóa
./.venv/bin/python scripts/smoke_test_lab.py    # SSH thật + "show clock" trên R1, SW1, SW2
```

### Bước 4: Chạy production bằng Gunicorn + systemd

Gunicorn không nằm trong dependency, cần cài thêm:

```bash
./.venv/bin/pip install gunicorn
```

Ví dụ unit `/etc/systemd/system/network-copilot.service` (repo không có sẵn file unit, đây là mẫu tham khảo):

```ini
[Unit]
Description=AI Network Copilot (Flask/Gunicorn)
After=network-online.target
Wants=network-online.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/network-copilot/backend
EnvironmentFile=/home/ubuntu/network-copilot/backend/.env
# 1 worker + nhiều thread: job nền và SSE sống trong bộ nhớ tiến trình
ExecStart=/home/ubuntu/network-copilot/backend/.venv/bin/gunicorn \
  --worker-class gthread --workers 1 --threads 32 \
  --timeout 120 --bind 172.16.3.28:5000 wsgi:app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now network-copilot
sudo systemctl status network-copilot --no-pager
sudo journalctl -u network-copilot -f
```

**Vì sao phải dùng `gthread`, 1 worker:**

- Mỗi tab chat mở một stream SSE giữ một thread tối đa `REALTIME_MAX_SECONDS` (55s). **Số thread phải lớn hơn hẳn `REALTIME_MAX_STREAMS`** (mặc định 20), nếu không các stream có thể chiếm hết thread và request thường phải chờ. Ví dụ trên dùng 32 thread cho 20 stream; hoặc giảm `REALTIME_MAX_STREAMS` (vd 8 với 16 thread). Với worker đồng bộ thông thường, đặt `REALTIME_ENABLED=false` (UI tự chuyển sang polling).
- Job bootstrap console và export PNETLab lưu trong bộ nhớ của tiến trình. Nhiều worker thì request poll job có thể rơi vào worker khác và trả 404.
- Bật `MONITORING_ENABLED=true` với nhiều worker sẽ khiến **mỗi worker chạy một scheduler riêng**, poll trùng lặp.

### Bước 5: Kiểm tra sau triển khai

```bash
curl -s http://172.16.3.28:5000/api/health
sudo ss -lntp | grep ':5000'
```

[↑ Về mục lục](#top)

---

<a id="bootstrap"></a>
## 6. Khởi tạo dữ liệu: user, project, thiết bị, SSH

### 6.1. Tạo tài khoản

- **ADMIN đầu tiên:** `python scripts/manage_users.py ensure-admin <username>` hoặc `seed_lab.py`.
- **Các tài khoản khác:** ADMIN vào `http://<server>:5000/users` → *Tạo tài khoản* (username, mật khẩu tối thiểu 10 ký tự, role, họ tên, email), hoặc gọi `POST /api/users`.
- Tài khoản **chỉ bị vô hiệu hóa, không bị xóa**, để audit vẫn giữ được tác giả.

### 6.2. Tạo project (mỗi mạng/lab là một project)

Vào `/projects` → *Tạo project*: tên, **management network** (dải IPv4 private, rộng tối đa /16, ví dụ `172.16.3.0/24`), môi trường `pnetlab` / `physical` / `mixed`.

- Hostname và IP quản trị **duy nhất trong phạm vi một project**, nên hai project khác nhau đều có thể có `R1`.
- Gọi API với header `X-Project-Id: <id>` (hoặc `?project_id=`). Nếu user chỉ có một project thì hệ thống tự chọn; có nhiều project mà không gửi header thì nhận `400 project_required`.

### 6.3. Thêm thiết bị (5 cách)

| Cách | Mô tả |
|---|---|
| Form / API | Tab *Thiết bị* hoặc `POST /api/devices` (hostname, management_ip, device_type `cisco_ios`/`cisco_asa`/`paloalto_panos`, role, ssh_port, login SSH). |
| Kéo thả trên canvas | Tab *Sơ đồ mạng*: kéo Router/Switch/Firewall vào. Tên (`R1`, `SW2`…) và IP trống được cấp tự động. |
| CSV | `POST /api/projects/<pid>/devices/import-csv` (cột bắt buộc `hostname`, `management_ip`). Chỉ cần một dòng lỗi là không nhập dòng nào. |
| Nhập từ PNETLab | *Thiết bị → Nhập từ PNETLab*: đọc node, network và dây nối của lab (chỉ đọc, không sửa lab). |
| Blueprint | *Mẫu*: star, branch, campus, dmz, ring, hub_spoke. Có thể tạo ở trạng thái `planned`, chưa có IP. |

**Vòng đời thiết bị:** `planned` (chỉ vẽ trên sơ đồ, không SSH) → **Triển khai** (`POST /api/devices/<id>/provision`, cấp IP và login) → `provisioned` (được poll, chat và cấu hình). Thiết bị loại PC/server/cloud/AP/segment là **unmanaged**: chỉ vẽ, backend không bao giờ SSH tới.

### 6.4. Cấu hình credential SSH

| Cách | Lệnh / thao tác |
|---|---|
| Login mặc định của project | *Thiết bị → SSH mặc định của project*. Thiết bị mới không có login sẽ nhận login này. `PUT /api/projects/<pid>/ssh-default`. |
| Riêng một thiết bị (CLI) | `python scripts/set_device_credential.py <hostname> <username> [project]` (hỏi mật khẩu, không echo). |
| Riêng một thiết bị (API) | `PUT /api/devices/<id>/credentials` với `username`, `password`, `enable_secret` (tùy chọn). |
| Toàn lab khi seed | `LAB_SSH_USERNAME` / `LAB_SSH_PASSWORD` + `seed_lab.py`. |

Mật khẩu được mã hóa Fernet trước khi lưu và **không bao giờ được trả về qua API**.

### 6.5. Bootstrap SSH qua console (cho node PNETLab mới khởi động)

Node mới trong PNETLab có cấu hình trống nên chưa SSH được. Thao tác *Khởi tạo SSH qua console*:

1. **Preview** (`POST .../topology/bootstrap/pnetlab/preview`): hiển thị chính xác các lệnh sẽ gõ (bí mật đã che), chưa mở console.
2. **Run** (`POST .../topology/bootstrap/pnetlab`, chỉ owner/ADMIN): mở telnet console của node trên PNETLab host, đặt hostname, IP quản trị (trên cổng nối **Management(Cloud0)**; switch L2 dùng `Vlan1`), gateway, user, RSA key, `ip ssh version 2`, `transport input ssh`, rồi `write memory` và kiểm tra SSH.
3. Trả về `202` kèm job; UI poll `GET .../topology/bootstrap/jobs/<id>`.

PA-VM (Palo Alto) được hỗ trợ dưới dạng job nền dài: đăng nhập `admin` với mật khẩu xuất xưởng, đổi mật khẩu bắt buộc, `set deviceconfig system ...`, `commit`.

### 6.6. Kiểm tra kết nối

```bash
# Từ UI: Thiết bị → Test kết nối, hoặc:
curl -s -X POST http://<server>:5000/api/devices/<id>/test-connection \
  -H "Authorization: Bearer $TOKEN" -H "X-Project-Id: <pid>"
```

[↑ Về mục lục](#top)

---

<a id="rbac"></a>
## 7. Phân quyền người dùng

Project dùng **3 vai trò**: `ADMIN`, `OPERATOR`, `VIEWER`. Quyền thực tế là **giao** của hai lớp:

```text
Quyền thực tế = Vai trò tài khoản (global role: ADMIN / OPERATOR / VIEWER)
              ∩ Mức truy cập trên project (owner / editor / viewer)
```

### 7.1. Lớp 1: Vai trò tài khoản (`users.role`)

| Role | Ý nghĩa | Ghi chú |
|---|---|---|
| `ADMIN` | Quản trị toàn hệ thống | **Tối đa 1** tài khoản (unique index `uq_single_admin`; DB mới có thể chưa có ADMIN nào). Thấy mọi project, quản lý user, policy. (`POST /api/admin/transfer` chỉ chuyển quyền cho tài khoản TEACHER nên không dùng trong project này.) |
| `OPERATOR` | Kỹ sư vận hành | Tạo project; cấu hình thiết bị trên project mình **sở hữu** hoặc được chia sẻ quyền **editor**. |
| `VIEWER` | Người xem | Luôn bị hạ xuống `viewer` trên mọi project; chỉ xem, chat tư vấn, chạy lệnh đọc. |

Tài khoản được tạo tại trang `/users` với một trong ba role trên.

> **Phạm vi:** Mã nguồn còn chứa module governance (role `TEACHER`/`STUDENT`, group, grant, bài tập, lịch đặt thiết bị) từ một hướng mở rộng trước đây. **Project của nhóm không sử dụng module này**: không tạo tài khoản `TEACHER`/`STUDENT` và không dùng các API `/api/groups`, `/api/permissions`, `/api/reservations`. Với `ADMIN` và `OPERATOR` có quyền owner/editor, backend bỏ qua các kiểm tra grant/group.

**MFA:** Database có các cột MFA, nhưng tại commit này MFA **không được thực thi**. Login trả `mfa_setup_required: false`, token của ADMIN chưa bật MFA được cấp kèm cờ `mfa_pending` (`auth/routes.py`), và hàm kiểm tra token chung cho mọi endpoint (`app.py::token_revoked`) chấp nhận token có cờ này. Đã thử thực tế với `/api/auth/me`. Không nên tuyên bố hệ thống có MFA.

**Cơ chế token:** JWT chứa `token_version`. Đổi mật khẩu, đổi role hoặc vô hiệu hóa tài khoản sẽ tăng `token_version`, nên **mọi token cũ bị thu hồi ngay**. Role dùng để phân quyền luôn được **đọc từ database**, không lấy từ token, nên hạ quyền có hiệu lực tức thì. Không thể hạ quyền hoặc vô hiệu hóa ADMIN cuối cùng, và ADMIN không tự hạ quyền chính mình được.

### 7.2. Lớp 2: Mức truy cập project (`projects/service.py::access_level`)

```text
ADMIN                         → owner trên mọi project
Project lab mặc định (PNETLab)→ viewer với mọi tài khoản không phải ADMIN
Chủ project                   → owner
Thành viên được chia sẻ       → viewer | editor (theo project_members.access)
Tài khoản VIEWER              → luôn bị hạ xuống viewer
Không thuộc các trường hợp    → không có quyền (API trả 404, không trả 403, để không lộ project tồn tại)
```

### 7.3. Ma trận quyền

| Hành động | ADMIN | OPERATOR (owner/editor) | OPERATOR (viewer) | VIEWER |
|---|:-:|:-:|:-:|:-:|
| Xem project, topology, dashboard | ✓ | ✓ | ✓ | ✓ |
| Tạo project qua trang Projects / `POST /api/projects` | ✓ | ✓ | ✓ | ✗ |
| Tạo project bằng câu "tạo project mới" trong chat (onboarding) | ✗ (nhánh chỉ dành cho OPERATOR) | ✓ | ✗ (bị 403 vì đang ở project chỉ có quyền viewer, vd lab mặc định) | ✗ |
| Chia sẻ project / cài đặt project | ✓ | chỉ **owner** | ✗ | ✗ |
| Sửa thiết bị, sơ đồ, design L2/L3 | ✓ | ✓ | ✗ | ✗ |
| Chat tư vấn AI, lệnh đọc (`show`, `ping`, `traceroute`) | ✓ | ✓ | ✓ | ✓ |
| `show run` gõ trực tiếp (không qua AI, dòng mật khẩu bị che) | ✓ | ✓ | ✓ | ✓ |
| Chat AI tạo cấu hình, Preview → Approve → Apply | ✓ | ✓ (được tự duyệt) | ✗ | ✗ |
| AI Management (áp gợi ý chat lên PNETLab) | ✓ | khi project bật AI Management | ✗ | ✗ |
| Bootstrap console, xuất PNETLab kèm khởi động node | ✓ | chỉ **owner** | ✗ | ✗ |
| Xem nguyên văn lệnh trong change/batch (`may_read_commands`) | ✓ | ✓ | ✓ (mọi OPERATOR; quyền truy cập bản ghi vẫn kiểm tra riêng) | bị che, trừ khi vẫn là owner của project theo `owner_id` (vd OPERATOR bị hạ xuống VIEWER mà vẫn giữ ownership); bí mật trong lệnh vẫn bị redact |
| Audit log (`/api/audit-logs`) | ✓ (`?scope=all` xem mọi project) | ✗ | ✗ | ✗ |
| Quản lý user, policy, draft/pending history | ✓ | ✗ | ✗ | ✗ |

### 7.4. Phiên chat và quyền riêng tư

- Trong một project thường, **mọi thành viên dùng chung các phiên chat** (đọc và tiếp tục được phiên của người khác).
- **Riêng project lab mặc định**: phiên chat là riêng của từng người. ADMIN được đọc để kiểm tra nhưng không ghi vào phiên của người khác.

[↑ Về mục lục](#top)

---

<a id="flows"></a>
## 8. Các luồng hoạt động của hệ thống

### 8.1. Vòng đời một request API

```text
Request ──► before_request: gán X-Request-ID; có header Authorization thì verify JWT
        ──► JWT blocklist: user tồn tại? is_active? token_version khớp?
        ──► Route (Blueprint) ──► current_project(): đọc X-Project-Id, kiểm tra access_level
        ──► Service: nghiệp vụ (policy, governance, SSH, DB)
        ──► after_request: non-ADMIN → redact output (che dòng password/secret/key, private key)
                           thêm header nosniff, X-Frame-Options DENY, no-store, X-Request-ID
        ──► Lỗi: JSON {error, message, details, request_id}; traceback chỉ ghi vào log server
```

### 8.2. Luồng khởi động ứng dụng (`create_app`)

1. Nạp `Config` từ biến môi trường.
2. Khởi tạo SQLAlchemy (bật foreign key cho SQLite), Migrate, JWT, Limiter.
3. Import toàn bộ model, đăng ký hook request, gắn hash nội dung vào URL static (`?v=<sha256>`) để trình duyệt không dùng bản JS/CSS cũ sau mỗi lần deploy.
4. Đăng ký blueprint: `auth`, `users`, `projects`, `realtime`, `topology`, `devices`, `commands`, `monitoring`, `changes`, `change_batches`, `checkpoints`, `audit`, `ai`, `chat`, `dashboard`, `groups`, `governance`, `learning`, `history_extension`.
5. Đăng ký error handler, rồi khởi động scheduler nếu `MONITORING_ENABLED=true`.

<a id="flow-chat"></a>
### 8.3. Luồng chat và **cơ chế phân loại chat**

Endpoint: `POST /api/ai/chat` (giới hạn 20 request/phút/user). Body: `message`, `session_id`, `mode` (tùy chọn `design`), `language` (`vi`/`en`).

Code trong `ai/routes.py::chat` xét tin nhắn **theo đúng thứ tự dưới đây** và dừng ở nhánh đầu tiên khớp:

```text
                         Tin nhắn người dùng
                                 │
 0. Validate (Pydantic), resolve phiên chat của project, kiểm tra quyền viewer
                                 │
 1. Viewer + (mode=design hoặc "tạo project")? ─────────► 403 (chỉ được đọc)
                                 │
 2. OPERATOR + "tạo project/topology" (không phải yêu cầu vẽ)?
        └► Tạo project mới, hỏi "bao nhiêu thiết bị (2–25)?"   flow=topology_setup_count
                                 │
 3. OPERATOR + câu trả lời trước của trợ lý đang hỏi số thiết bị?
        └► Đưa ra các phương án star / ring / campus / hub-spoke  flow=topology_options
                                 │
 4. mode = "design"? ────────────► design_assistant.propose(): đề xuất thiết kế L2/L3
                                    (VLAN, SVI, route, OSPF) trên thiết bị và dây nối ĐÃ CÓ
                                 │
 5. "show run [R1]" gõ trực tiếp? ► chạy show running-config KHÔNG qua AI,
                                    che dòng mật khẩu, không đưa vào ngữ cảnh model
                                 │
 6. [không phải viewer] Câu đặt IP có địa chỉ IP, nêu tên thiết bị managed của project,
    chưa nói cổng, thiết bị có dây nối phù hợp? ─► port_choice: hỏi chọn cổng
                                 │
 7. [không phải viewer] _wants_design(): Intent Router AI được gọi TẠI ĐÂY
    (bỏ qua router nếu câu bắt đầu bằng show/ping/traceroute; router lỗi → rule từ khóa)
        └► nhãn design → AIService.advise(): tư vấn + sơ đồ đề xuất mới + snippet cấu hình
                                 │
 8. Còn lại ─────────────────────► AIService.handle(): LLM trả về AIAction với intent
                                     ├─ chat         → trả lời, không chạm thiết bị
                                     ├─ monitor      → chạy lệnh đọc
                                     ├─ troubleshoot → chạy lệnh đọc + AI phân tích
                                     └─ configure    → tạo batch preview (chờ duyệt)
        Nếu handle() lỗi SCHEMA_MISMATCH ngay sau một đề xuất thiết kế
        (và user không phải viewer) → chuyển sang AIService.advise()
```

> Intent Router **không** chạy trước mọi tin nhắn: các nhánh 1–6 được xử lý trước, và viewer không bao giờ đi qua nhánh 7.

**Hai tầng phân loại:**

| Tầng | Nơi | Nhãn | Cách làm |
|---|---|---|---|
| **Intent Router** (chọn pipeline) | `ai/intent.py` | `design` · `read` · `configure` · `chat` | Một lệnh model ngắn, gửi kèm 6 lượt hội thoại gần nhất, danh sách hostname/kind của project (**không gửi IP**) và cờ "câu trả lời trước có phải thiết kế không". Bật/tắt bằng `AI_INTENT_ROUTER`. |
| **Fallback rule từ khóa** | `chat/onboarding.py`, `ai/routes.py` | tương tự | Bỏ dấu tiếng Việt (`_fold`), regex: "tạo project", "sơ đồ", "topology", "thiết kế mạng", "xây … 1 router, 1 switch và 6 pc"…; lệnh `show/ping/traceroute/kiểm tra` hoặc tên thiết bị (vd `sw1` khớp `SWITCH-SW1`) nghĩa là thao tác trên **mạng thật**. |
| **AIAction intent** (hành động) | `ai/service.py` `SYSTEM_PROMPT` | `monitor` · `configure` · `troubleshoot` · `chat` | LLM trả JSON theo `response_schema` (Gemini) với danh sách hostname hợp lệ dạng enum; tắt "thinking"; thử lại 1 lần nếu JSON lỗi; một lời từ chối hợp lệ thì không thử lại. |

> Nhãn của model **chỉ dùng để chọn pipeline**. Quyền không do model quyết định: mọi pipeline đều tự kiểm tra role, quyền project, policy và workflow duyệt. Nếu router lỗi (hết quota, mạng, JSON hỏng), hệ thống tự quay về rule từ khóa.

**Ngữ cảnh gửi cho AI** (`AIService.build_context`): hostname, role, device_type, status của các thiết bị **managed + provisioned trong đúng project**; danh sách lệnh đọc được phép (đã loại `show running-config`); template cấu hình đang bật; bảng tương đương lệnh ASA; topology và addressing đã suy ra từ snapshot (đã loại mạng quản trị). Hệ thống **chủ động không đưa** mật khẩu, IP quản trị và running-config đầy đủ vào phần ngữ cảnh tự động này.

> **Giới hạn:** (1) Ở luồng **troubleshoot**, lệnh và output đọc từ thiết bị được gửi cho `provider.explain()` sau khi qua `redact_sensitive()`. Hàm này che mật khẩu/secret/key nhưng **không che địa chỉ IP**, nên output (vd `show ip interface brief`) **có thể chứa IP quản trị** ngay cả khi người dùng không gõ IP nào. (2) Tin nhắn người dùng và các lượt hội thoại gần đây (`_recent_history`) được gửi nguyên văn cho nhà cung cấp AI, kể cả cho Intent Router. Nếu người dùng tự gõ mật khẩu, IP quản trị hay dán cấu hình vào chat, nội dung đó **có thể được gửi đi**. Quy định cho người dùng: **không nhập thông tin nhạy cảm vào chat.**

### 8.4. Luồng Monitor / Troubleshoot (chỉ đọc)

```text
AIAction(monitor|troubleshoot)
  → consume_ai(): kiểm tra quyền ai.use + quota AI/ngày
  → bắt buộc đúng 1 operation, 1 hostname cụ thể (không "*"), mode exec
  → resolve thiết bị theo hostname TRONG project hiện tại
  → ai_policy.evaluate() cho từng lệnh: allowlist, chặn lệnh nguy hiểm, chặn nối lệnh
      └ bị chặn → audit "ai.command_blocked" + 403 policy_violation (chưa mở SSH)
  → command_service.execute_readonly(): SSH → chạy lệnh → parser → lưu command_executions
  → troubleshoot: output được che bí mật (KHÔNG che IP) rồi gửi lại LLM để giải thích nguyên nhân
    và bước kiểm tra tiếp
  → lưu transcript + audit "ai.action"
```

Parser hỗ trợ: interface, route (IOS/ASA/PAN-OS), OSPF neighbor, VLAN, switchport/trunk, DHCP pool, ACL, CDP/LLDP neighbor.

### 8.5. Luồng Cấu hình: **Preview → Approve → Apply → Verify**

```text
① PREVIEW
   - Kiểm tra quyền vào (cả đường AI configure qua /api/ai/chat và POST /api/changes/preview):
     chỉ ADMIN, hoặc OPERATOR có quyền owner/editor trên project. VIEWER bị từ chối.
   - validate_commands: chặn ký tự nối lệnh ; | & ` $( > < và xuống dòng
                         lệnh nguy hiểm / VLAN hệ thống / ip access-group → requires_confirmation
   - management_guard (chỉ config mode, không áp dụng PAN-OS): từ chối thay đổi trên interface
     quản trị NẾU xác định được interface đó (xem 9.4)
   - workflow.bind: kiểm tra quota thay đổi đang chờ (pending_change_limit),
                    tính risk (low/high), lưu baseline_hash (hash running-config gần nhất)
   - đóng băng danh tính đích: target_hostname / management_ip / ssh_port / device_type
   - sinh verification_commands + rollback_commands (CHỈ gợi ý, không tự chạy)
   - trạng thái: pending_approval

② APPROVE (POST /api/changes/<id>/approve hoặc /api/change-batches/<id>/approve)
   - approve_allowed: ADMIN, hoặc OPERATOR có quyền owner/editor trên project (kể cả thiết bị
     production; OPERATOR được tự duyệt change do chính mình tạo). VIEWER không duyệt được
   - baseline_revision phải không đổi
   - approval_digest = SHA-256(commands, target, baseline, risk, environment, policy...)
   - trạng thái: approved (hết hạn sau approval_minutes, mặc định 60 phút)

③ APPLY (POST /api/changes/<id>/apply, 10 req/phút)
   - requires_confirmation = true: change đơn gửi confirm_hostname = đúng hostname;
     batch 1 thiết bị gửi hostname, batch ≥ 2 thiết bị gửi "CONFIRM ALL"
   - deployment_allowed: kiểm tra lại quyền, digest, hạn duyệt, maintenance window (nếu có),
     thiết bị không offline
   - claim: tạo DeviceLease (khóa duy nhất mỗi thiết bị) + đổi trạng thái có điều kiện → running
   - a. BACKUP "show running-config" (PAN-OS: "show config running") → config_backups
        (backup lỗi thì không cấu hình gì)
   - b. so baseline_hash: running-config đổi kể từ lúc preview → fail
   - c. kiểm tra lại quyền SAU khi backup (thu hồi quyền luôn thắng)
   - d. SSH: run_config (config mode) hoặc run_exec; PAN-OS: configure → set... → commit
         (lệnh bị từ chối/commit lỗi → THỬ `revert config` candidate chưa commit; revert lỗi
          chỉ ghi warning. Đây không phải rollback sau khi đã commit)
   - e. VERIFY: chạy lệnh kiểm tra chỉ đọc, thử lại VERIFICATION_RETRIES lần
   - f. verify fail → failed (kèm rollback_commands để làm tay); pass → success
   - g. thử backup sau thay đổi (lỗi chỉ ghi warning, change vẫn success), thử tạo checkpoint
        undo/redo (có thể bị bỏ qua, xem 8.8), thông báo, audit "change.apply"
   - release lease (kể cả khi lỗi)

Trạng thái:  pending_approval → approved → running → success | failed
             cancelled (từ 2 trạng thái đầu) · batch thêm partial_success
```

### 8.6. Luồng Batch nhiều thiết bị

Ví dụ: *"thực hiện lệnh write trên toàn bộ thiết bị"*.

1. AI trả `device_hostnames: ["*"]` (`*` = mọi thiết bị **của project này**). Backend tạo `change_batches` và một `change_requests` con cho mỗi thiết bị, tất cả ở trạng thái đóng băng.
2. Người vận hành xem từng thiết bị, mode, lệnh và mức rủi ro rồi bấm **Approve batch**.
3. Nếu batch có `requires_confirmation = true` (chứa lệnh thuộc [9.3](#rules)): batch 1 thiết bị phải gõ đúng **hostname**, batch từ 2 thiết bị trở lên phải gõ chính xác `CONFIRM ALL`. Risk `high` một mình **không** kích hoạt bước này.
4. Apply chạy **tuần tự theo hostname**. Mỗi thiết bị con lại được kiểm tra quyền khi đến lượt.
5. Kết quả `success` / `failed` / `partial_success`. Với `partial_success`, người vận hành phải xem từng kết quả con và xử lý tay trước khi chạy lại.

### 8.7. Luồng AI Management (áp gợi ý chat lên PNETLab)

- Chủ project bật *AI Management* trong cài đặt project.
- Backend kiểm tra mỗi thiết bị đích (`chat/routes.py`): hostname tồn tại và không trùng lặp trong project; `device_type` khớp với gợi ý (`cisco_ios`, `cisco_asa` hoặc `paloalto_panos`); `environment = pnetlab`; `deployable` (managed, provisioned, có IP quản trị); `isolated`; không `offline`; có credential SSH. Thiết bị **không bắt buộc** phải được tạo bằng import.
- Ở bước duyệt/triển khai (`governance/workflow.py::_ai_management_consent`), đường đồng ý riêng của AI Management chỉ được chấp nhận khi project bật AI Management, project có `environment = pnetlab`, và người thao tác chính là OPERATOR đã tạo yêu cầu, có quyền edit project. Không thỏa các điều kiện này thì change đi theo các kiểm tra duyệt/triển khai thông thường (vd ADMIN).
- Mức hỗ trợ thực tế vẫn khác nhau theo loại thiết bị: undo/redo chỉ có cho IOS, verification ngữ nghĩa chủ yếu cho IOS (xem 9.2b).
- Nút *Duyệt và áp dụng trên PNETLab* trên từng gợi ý (`POST /api/chat/messages/<id>/apply-configurations`) là **một lần duyệt riêng cho mỗi gợi ý**. Sau đó lệnh vẫn đi qua backup → policy → verify → audit. Không áp dụng được cho thiết bị physical/production.
- Hoặc tải ZIP gồm các file `.cfg` cho từng thiết bị (`POST /api/chat/messages/<id>/config-files`) rồi nhập tay vào PNETLab.

### 8.8. Luồng Undo/Redo cấu hình (Checkpoints)

- Thay đổi apply và verify thành công **có thể** trở thành một **bước** trong *Lịch sử cấu hình thiết bị*, lưu backup `pre_change` và `post_change`. Không tạo bước khi: `CONFIG_CHECKPOINTS_ENABLED=false`, change ở EXEC mode (vd `write memory`), loại thiết bị không được hỗ trợ (chỉ Cisco IOS), hoặc việc ghi checkpoint gặp lỗi (chỉ ghi log, apply vẫn thành công).
- **Undo:** đọc running-config hiện tại, diff với `pre_change`, sinh lệnh đưa cấu hình về trạng thái cũ, rồi tạo một change `source: restore` (High Risk). Change này vẫn phải Approve → gõ hostname → Apply → Verify.
- Các bước của mỗi thiết bị là một stack: chỉ undo được bước mới nhất. Không sinh lệnh cho secret, banner, certificate. Chưa hỗ trợ PAN-OS/ASA.

### 8.9. Luồng Monitoring nền

```text
APScheduler (mỗi MONITORING_INTERVAL_SECONDS, max_instances=1, coalesce)
  → poll_all_enabled_devices(): ThreadPool MONITORING_MAX_WORKERS
      → mỗi thiết bị: SSH chạy lệnh theo loại
           IOS:    show ip interface brief, show ip route (+ lệnh theo role: VLAN, OSPF...)
           ASA:    show interface ip brief, show route
           PAN-OS: show system info, show interface all, show routing route
      → parse → lưu device_snapshots, cập nhật status/last_seen
  → ghi thời lượng vòng; GET /api/dashboard/monitoring (ADMIN) cảnh báo khi vòng poll quá chu kỳ
```

Poll tay: `POST /api/devices/<id>/refresh`.

### 8.10. Luồng realtime (SSE)

Trang chat mở `GET /api/projects/<id>/events`. Server so "fingerprint" của các phần dữ liệu (chat, changes, topology…) mỗi `REALTIME_POLL_SECONDS` giây và gửi event khi có thay đổi; UI chỉ tải lại phần đã đổi. Nếu stream lỗi, UI tự quay về polling `GET .../state` (có ETag, trả 304 khi không đổi).

### 8.11. Luồng PNETLab: nhập, khám phá, xuất

| Luồng | Mô tả |
|---|---|
| **Import** | Đăng nhập `/store/public/auth/login/login` (PNETLab 4/5), mở lab bằng `/api/labs/session/factory/create`, đọc `/api/labs/session/topology`. Fallback sang API EVE-NG. Chỉ chấp nhận URL private, không theo redirect. vIOS/IOL/CSR được nhận là router, `*l2*` là switch, ASAv là firewall, `paloalto`/`pa-vm` là PAN-OS. |
| **Ghi nhớ đăng nhập** | Lưu mật khẩu PNETLab (đã mã hóa) sau một lần preview thành công. Chỉ dùng lại cho đúng địa chỉ và tài khoản đó. |
| **Discover CDP/LLDP** | `show cdp neighbors detail` (hoặc LLDP) trên 4 thiết bị cùng lúc, so với sơ đồ: xanh = khớp, đỏ đứt nét = thiết kế có nhưng thiết bị không báo, xám = thiết bị không trả lời. |
| **Export ("Đẩy sang PNETLab")** | Tạo lab mới từ sơ đồ trong `PNETLAB_EXPORT_FOLDER` (job nền). Tùy chọn khởi động node và bootstrap SSH (chỉ owner). |

[↑ Về mục lục](#top)

---

<a id="rules"></a>
## 9. Cơ chế rule, policy và kiểm soát an toàn

### 9.1. Read-only command policy (allowlist), `commands/policy.py`

Thứ tự đánh giá mỗi lệnh đọc:

1. Chuỗi rỗng → từ chối.
2. Có ký tự nối lệnh `;` `|` `&` `\n` `\r` `$(` `` ` `` `>` `<` → từ chối (kiểm tra trên chuỗi **gốc**, trước khi chuẩn hóa).
3. Chuẩn hóa: lowercase, gộp khoảng trắng, `show run` thành `show running-config`, `tracert` thành `traceroute`.
4. Khớp **DANGEROUS_PATTERNS** → từ chối.
5. Khớp một rule allowlist, nhưng role thiết bị không nằm trong `allowed_roles` của rule → từ chối.
6. Không khớp rule nào → **từ chối mặc định** ("Unknown commands are denied by default").

**Lệnh bị cấm tuyệt đối** (DANGEROUS_PATTERNS): `write`, `erase`, `reload`, `delete`, `format`, `debug`, `undebug`, `copy`, `configure`/`conf t`, `no …`, `shutdown`, `clear`, `reset`, `boot`, `archive`, `setup`, `tclsh`, `squeeze`.

**Allowlist chính** (lấy theo role thiết bị: `core`, `distribution`, `access`, `firewall`, `isp`, `dmz`, `management`):

| Nhóm | Lệnh |
|---|---|
| IOS interface | `show ip interface brief`, `show interfaces status`, `show interfaces <if> switchport`, `show interfaces trunk` |
| L2 / L3 | `show vlan brief`, `show ip route`, `show ip route vrf <vrf>`, `show ip ospf neighbor`, `show ip dhcp pool`, `show access-lists`, `show vrf`, `show ip vrf` |
| Hệ thống | `show version`, `show clock`, `show logging`, `show ip ssh`, `show running-config` |
| ASA | `show interface ip brief`, `show route`, `show access-list` |
| PAN-OS | `show system info`, `show interface all`, `show interface <name>`, `show routing route`, `show config running` |
| Kết nối | `ping <ipv4>`, `ping <ipv4> source <ip\|if>`, `ping vrf <vrf> <ipv4>`, `traceroute <ipv4>`, `traceroute vrf <vrf> <ipv4>` |

**AI-safe policy** (`ai_policy`): giống allowlist trên nhưng **loại** `show running-config`, `show startup-config`, `show config running`, để cấu hình đầy đủ không bao giờ bị gửi cho model. Người dùng vẫn xem được `show run` khi gõ trực tiếp (nhánh 5 ở [8.3](#flow-chat)).

### 9.2. Phân loại rủi ro thay đổi, `governance/workflow.py::risk`

Rủi ro do **backend tự tính**, không lấy từ người dùng hay AI. **Chỉ cần một lệnh High thì cả change là High.**

| Điều kiện (xét theo thứ tự) | Kết quả |
|---|---|
| Lệnh nằm trong `policy.forbidden_commands` (ADMIN cấu hình) | **Từ chối** (`policy_violation`) |
| Lệnh nằm trong `policy.high_risk_commands` | High |
| Thiết bị `environment = production` | High |
| `physical_lab` và lệnh không thuộc `physical_lab_low_risk_commands` | High |
| `virtual` nhưng không `isolated` | High |
| Role `core`/`distribution`/`isp`/`firewall` | High |
| Routing, `ip route`, `ip address`, ACL, `username`, `enable`, `line`, `snmp-server`, `aaa`, `crypto`, `interface vlan/loopback` | High |
| Chỉ gồm lệnh an toàn: `hostname`, `vlan N`, `name`, `interface Gi/Fa/Eth…`, `description`, `switchport access vlan N`, `switchport mode access` | **Low** |
| Mọi trường hợp còn lại | High |

Sau khi `risk()` trả kết quả, `workflow.bind()` còn **ép thêm**: change có `requires_confirmation` → high; change khôi phục (`source: restore`, do Undo/Redo tạo) → high + bắt buộc xác nhận.

> **Lưu ý nhãn môi trường:** Thiết bị có 5 giá trị `environment`: `pnetlab`, `physical`, `virtual`, `physical_lab`, `production`. Bộ phân loại rủi ro chỉ có quy tắc riêng cho `production`, `physical_lab` và `virtual`. Với thiết bị `pnetlab`/`physical`, rủi ro chỉ còn phụ thuộc role và nội dung lệnh. Project dùng một bộ nhãn khác (`pnetlab`/`physical`/`mixed`), không đồng nghĩa với nhãn của thiết bị.

### 9.2b. Mức xác minh sau thay đổi (`changes/capabilities.py`)

| Tier | Nhóm cấu hình IOS | Ý nghĩa |
|---|---|---|
| `level_a_core` | VLAN, access/trunk, mô tả và trạng thái cổng, IPv4 cổng, static route, lưu cấu hình | Xác minh **ngữ nghĩa** (đọc lại thiết bị và so với ý định) |
| `level_a_extended` | ACL IPv4 chuẩn, DHCP pool, OSPF một area | Có recognizer giới hạn; cần bằng chứng lab trước khi coi là hoàn chỉnh |
| `best_effort` | Mọi lệnh khác | Chỉ kiểm tra chung (output không có lỗi); **không bảo đảm** thay đổi đúng ý định |

### 9.3. Lệnh cần xác nhận gõ tay (typed confirmation)

`changes/service.py::validate_commands` đặt `requires_confirmation = true` khi có:

- lệnh khớp DANGEROUS_PATTERNS (vd `no shutdown`, `write memory`, `copy …`);
- lệnh khớp đúng `vlan <id>`, `no vlan <id>` hoặc `switchport access vlan <id>` với id thuộc **VLAN hệ thống** (1, 1002–1005). Regex chỉ nhận đúng các dạng này, không nhận diện mọi cách sửa VLAN hệ thống (vd danh sách VLAN trên trunk);
- `ip access-group <acl> in|out` (dễ tự khóa đường quản trị).

Change đơn: gửi `confirm_hostname` = **đúng hostname**. Batch (`ChangeBatch.confirmation_text`): 1 thiết bị → hostname, ≥ 2 thiết bị → `CONFIRM ALL`. Lưu ý `requires_confirmation` và `risk_level = high` là **hai cờ khác nhau**: mọi change cần xác nhận đều là high, nhưng change high (vd static route) không nhất thiết cần xác nhận.

### 9.4. Management Interface Guard, `changes/management_guard.py`

Đây là lớp bảo vệ **có điều kiện**, giúp tránh tự cắt đường SSH của NetAI. Khi chặn được, thay đổi bị từ chối ngay ở bước preview kèm thông báo nêu cổng nên dùng thay thế.

- **Chỉ chạy** với change ở config mode trên thiết bị không phải PAN-OS.
- **Xác định interface quản trị** từ backup running-config mới nhất (interface chứa IP quản trị). Nếu chưa có backup thì fallback `Vlan1`, nhưng chỉ cho switch có environment `pnetlab`.
- **Không xác định được interface** (chưa có backup, không phải switch PNETLab) → **không chặn gì**.
- Khi xác định được: chặn `no interface …`/`default interface …` của cổng đó, và mọi lệnh bên trong khối `interface <cổng đó>` trừ `description` / `no description`.

Guard này **không bảo đảm** phát hiện mọi thao tác làm mất kết nối quản trị: ví dụ đổi route/gateway, ACL trên cổng khác, hoặc cổng không xác định được.

### 9.5. Policy hệ thống (ADMIN chỉnh qua `PATCH /api/policy`)

| Trường | Mặc định | Ý nghĩa |
|---|---|---|
| `independent_approval`, `production_admin_only` | `true` | Thuộc module governance; **không ảnh hưởng** ADMIN và OPERATOR có quyền owner/editor |
| `approval_minutes` | `60` | Hạn hiệu lực của một lần duyệt |
| `ai_daily_limit` | `100` | Số tin nhắn đi qua `AIService.handle()` mỗi user mỗi ngày. **Không** đếm lời gọi Intent Router, tư vấn thiết kế (`advise`, `mode=design`) hay phân tích troubleshoot, nên không phải giới hạn tổng số request hay chi phí gửi tới provider |
| `pending_change_limit` | `20` | Số change đang chờ/chạy tối đa mỗi user |
| `forbidden_commands`, `high_risk_commands`, `physical_lab_low_risk_commands` | `[]` | Danh sách tiền tố lệnh do ADMIN bổ sung |

### 9.6. Rate limit (Flask-Limiter, theo user, chưa đăng nhập thì theo IP)

| Endpoint | Giới hạn |
|---|---|
| `POST /api/auth/login` | 5/phút/IP |
| `POST /api/ai/chat` | 20/phút/user |
| `POST /api/changes/<id>/apply`, undo/redo checkpoint, import/bootstrap/export PNETLab, khôi phục version | 5–10/phút |
| Blueprint topology | 20/phút |

### 9.7. Bảo vệ dữ liệu

| Cơ chế | Vị trí |
|---|---|
| Mật khẩu user: hash (werkzeug) | `auth/model.py` |
| Credential thiết bị, login PNETLab, SSH mặc định: mã hóa Fernet, không bao giờ serialize | `credentials/service.py` |
| Audit log được redact trước khi lưu | `audit/service.py::redact_sensitive` |
| Response cho non-ADMIN: che dòng `password/secret/key/community…`, private key; che danh sách lệnh nếu không có quyền xem | `governance/redaction.py` |
| Cô lập project: mọi truy vấn lọc theo `project_id`; `*` = thiết bị của project này | `projects/scope.py` |
| Đóng băng đích + digest: đổi thiết bị/lệnh/baseline/policy sau khi duyệt thì phải duyệt lại | `governance/workflow.py::digest` |
| Lease thiết bị: không cho hai worker cùng ghi lên một thiết bị | `DeviceLease` |
| Header bảo mật: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `no-store` | `app.py` |
| Ngữ cảnh inventory/topology gửi AI (`build_context`) không chứa credential/IP quản trị/running-config. **Nhưng** output chẩn đoán troubleshoot chỉ được che bí mật (vẫn có thể chứa IP quản trị), và tin nhắn/lịch sử chat do người dùng nhập được gửi nguyên văn cho provider | `ai/service.py`, `ai/intent.py`, `history_extension/protection.py` |

[↑ Về mục lục](#top)

---

<a id="ui"></a>
## 10. Hướng dẫn sử dụng giao diện

### 10.1. Quy trình vận hành cơ bản

1. Đăng nhập tại `http://<server>:5000/`.
2. Chọn project ở thanh trên cùng (lựa chọn dùng chung cho các trang chat, dashboard, projects).
3. **Dashboard** (`/dashboard`): thiết bị theo role, trạng thái online/offline, OSPF, hoạt động gần đây.
4. **Projects** (`/projects`): tab *Thiết bị*, *Sơ đồ mạng* (canvas kéo thả, nối dây, Ctrl+Z/Y), *Thiết kế L2/L3*, *Phiên bản*, *Nhóm / chia sẻ*.
5. **Chat** (`/`): hỏi đáp, ra lệnh; cột *Pending* hiển thị change chờ duyệt và lịch sử undo/redo.

### 10.2. Ví dụ câu lệnh chat

| Mục đích | Câu ví dụ | Pipeline |
|---|---|---|
| Hỏi lý thuyết | "OSPF khác RIP thế nào?" | chat |
| Xem trạng thái | "show ip interface brief trên R1" · "kiểm tra vlan SW1" | monitor |
| Xem cấu hình | "show run SW1" | đọc trực tiếp (không qua AI) |
| Chẩn đoán | "PC VLAN 10 không ping được gateway, kiểm tra SW1" | troubleshoot |
| Cấu hình | "tạo vlan 10 tên SALES trên SW1, gán Gi0/2 vào vlan 10" | configure → preview |
| Batch | "thực hiện lệnh write trên toàn bộ thiết bị" | configure → batch + `CONFIRM ALL` |
| Thiết kế | "thiết kế mạng tiệm net 1 router, 2 switch, 30 PC" | design advisor |
| Tạo project | "tạo project mới" | onboarding (OPERATOR) |

### 10.3. Quy trình duyệt một thay đổi

- Đọc kỹ **thiết bị đích**, **lệnh**, **risk**, **warnings**, **verification**, **rollback gợi ý**.
- Approve, sau đó Apply. Nếu được yêu cầu, gõ hostname hoặc `CONFIRM ALL`.
- Xem kết quả: output apply, kết quả verify, backup ID. Nếu `failed`/`partial_success`, đọc lỗi và dùng rollback gợi ý **làm tay** hoặc Undo checkpoint.

[↑ Về mục lục](#top)

---

<a id="commands"></a>
## 11. Tập lệnh quản trị quan trọng (Cheat Sheet)

### 11.1. Ứng dụng

| Mục đích | Lệnh |
|---|---|
| Kích hoạt venv | `source backend/.venv/bin/activate` |
| Migration DB | `flask --app wsgi db upgrade` |
| Kiểm tra lệch model và migration | `flask --app wsgi db check` |
| Chạy dev | `flask --app wsgi run --host 0.0.0.0 --port 5000` |
| Trạng thái service | `sudo systemctl status network-copilot --no-pager` |
| Restart service | `sudo systemctl restart network-copilot` |
| Log service | `sudo journalctl -u network-copilot -f` |
| Health | `curl -s http://<server>:5000/api/health` |

### 11.2. Script trong `backend/scripts/`

| Script | Công dụng |
|---|---|
| `setup_ai_server.sh` | Cài đặt trọn gói trên node AI Server |
| `seed_lab.py` | Tạo ADMIN + project `PNETLab` + R1/SW1/SW2 + credential (idempotent) |
| `manage_users.py ensure-admin <u>` | Tạo hoặc nâng quyền ADMIN (giữ nguyên mật khẩu nếu tài khoản đã có) |
| `manage_users.py list` | Liệt kê tài khoản |
| `manage_users.py set-password <u>` | Đặt lại mật khẩu (hỏi tương tác, hoặc dùng `NEW_USER_PASSWORD`) |
| `set_device_credential.py <host> <user> [project]` | Đặt SSH login cho một thiết bị |
| `smoke_test_lab.py` | SSH thật + `show clock` lên R1/SW1/SW2; exit 1 nếu có thiết bị lỗi |
| `demo_check.py --username admin --password '…'` | Demo đầy đủ luồng batch (yêu cầu gõ `CONFIRM ALL` tương tác) |
| `diagnose_login.py <user>` | Giải thích vì sao đăng nhập báo "Invalid credentials" |
| `diagnose_gemini.py` | Tìm option nào trong request Gemini bị API từ chối |
| `eval_intent_router.py` | Đánh giá độ chính xác của Intent Router |
| `evaluate_ai.py` | Chạy bộ prompt đánh giá AI |
| `probe_pnetlab_write.py` | Kiểm tra quyền ghi lab trên PNETLab |
| `course_evidence.py` | Thu thập bằng chứng cho báo cáo môn học |

### 11.3. Gọi API bằng curl

```bash
BASE=http://127.0.0.1:5000
TOKEN=$(curl -s -X POST $BASE/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"<pw>"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
H=(-H "Authorization: Bearer $TOKEN" -H "X-Project-Id: 1" -H 'Content-Type: application/json')

curl -s "${H[@]}" $BASE/api/devices
curl -s "${H[@]}" -X POST $BASE/api/commands/execute-readonly -d '{"device_id":1,"command":"show clock"}'
curl -s "${H[@]}" -X POST $BASE/api/ai/chat -d '{"message":"show ip interface brief trên R1"}'
curl -s "${H[@]}" $BASE/api/changes
curl -s "${H[@]}" -X POST $BASE/api/changes/5/approve
curl -s "${H[@]}" -X POST $BASE/api/changes/5/apply -d '{"confirm_hostname":"SW1"}'
curl -s "${H[@]}" "$BASE/api/audit-logs"
```

### 11.4. Đường dẫn quan trọng

```text
backend/.env                                Biến môi trường (chmod 600)
backend/network_copilot.db                  Database SQLite
backend/migrations/versions/                Migration Alembic
backend/src/network_copilot/config.py       Định nghĩa toàn bộ cấu hình
backend/src/network_copilot/commands/policy.py      Allowlist + lệnh nguy hiểm
backend/src/network_copilot/governance/workflow.py  Phân loại rủi ro, duyệt, triển khai
backend/src/network_copilot/ai/service.py           Prompt AI, xử lý intent
backend/src/network_copilot/ai/intent.py            Intent router
backend/src/network_copilot/ai/routes.py            Điều phối chat
```

### 11.5. Truy vấn nhanh database

```bash
sqlite3 backend/network_copilot.db "SELECT id, username, role, is_active FROM users;"
sqlite3 backend/network_copilot.db "SELECT id, project_id, hostname, management_ip, status FROM devices;"
sqlite3 backend/network_copilot.db "SELECT id, status, risk_level, error_message FROM change_requests ORDER BY id DESC LIMIT 10;"
sqlite3 backend/network_copilot.db "SELECT created_at, action, result, username FROM audit_logs ORDER BY id DESC LIMIT 20;"
sqlite3 backend/network_copilot.db "SELECT * FROM device_leases;"
```

[↑ Về mục lục](#top)

---

<a id="backup"></a>
## 12. Sao lưu, khôi phục và nâng cấp

### 12.1. Sao lưu

```bash
sudo systemctl stop network-copilot          # hoặc dùng .backup để chụp nhất quán khi đang chạy
install -d -m 700 ~/netai-backup
sqlite3 ~/network-copilot/backend/network_copilot.db ".backup '$HOME/netai-backup/netai-$(date +%F).db'"
cp ~/network-copilot/backend/.env ~/netai-backup/env-$(date +%F)
chmod 600 ~/netai-backup/*
sudo systemctl start network-copilot
```

**Database và `CREDENTIAL_ENCRYPTION_KEY` phải luôn đi cùng nhau.** Có DB mà không có khóa thì mọi credential SSH là vô dụng. Bản backup chứa dữ liệu nhạy cảm (credential đã mã hóa, cấu hình thiết bị, audit), nên cần mã hóa và lưu ngoài máy.

Ngoài ra, hệ thống lưu **backup running-config** của thiết bị trước mỗi lần apply (bắt buộc: backup lỗi thì không ghi) và thử lưu sau khi apply (không bắt buộc), trong bảng `config_backups`, xem tại `GET /api/devices/<id>/backups`. Bản backup này không thay thế việc sao lưu database.

### 12.2. Khôi phục

1. Cài lại mã nguồn đúng phiên bản (cùng commit hoặc mới hơn).
2. Chép file DB vào `backend/network_copilot.db` và `.env` cũ (giữ nguyên `CREDENTIAL_ENCRYPTION_KEY`).
3. `flask --app wsgi db upgrade` (nâng schema nếu mã mới hơn).
4. Khởi động service, đăng nhập, `smoke_test_lab.py` để kiểm tra SSH.

### 12.3. Nâng cấp mã nguồn

```bash
cd ~/network-copilot
sqlite3 backend/network_copilot.db ".backup 'backend/pre-upgrade.db'"
git fetch origin
git log --oneline origin/main -5           # chọn commit đích đã kiểm tra
git checkout <commit-đích>                 # repo đang ở detached HEAD do bước cài đã checkout hash
# Hoặc, nếu chủ động chạy bản mới nhất: git checkout main && git pull --ff-only
# Cài bằng archive: giải nén archive mới thay cho các lệnh git ở trên
cd backend && ./scripts/setup_ai_server.sh # cài dependency mới, migration, chạy test
sudo systemctl restart network-copilot
```

- Đọc migration mới trong `migrations/versions/` trước khi nâng cấp. Ví dụ migration `9e54c2bd3d7e` hủy các preview/approval cũ chưa chạy và vô hiệu hóa token cũ.
- **Cảnh báo về role khi nâng cấp database cũ:** migration `9e54c2bd3d7e` chạy `UPDATE users SET role = 'STUDENT' WHERE role IN ('VIEWER', 'OPERATOR')`. Database tạo mới bằng `flask db upgrade` **không bị ảnh hưởng**, vì lúc đó chưa có user nào. Nhưng nếu nâng cấp một database **cũ hơn** migration này, mọi tài khoản OPERATOR/VIEWER sẽ thành STUDENT. Sau khi nâng cấp, ADMIN phải vào `/users` đặt lại role cho từng tài khoản. Kiểm tra bằng: `sqlite3 backend/network_copilot.db "SELECT username, role FROM users;"`
- Không giữ `paramiko ≥ 5`: bản này đã bỏ KEX SHA-1 mà image IOSv cần dùng.

[↑ Về mục lục](#top)

---

<a id="troubleshooting"></a>
## 13. Khắc phục sự cố thường gặp

### 13.1. Bảng mã lỗi API

Hầu hết lỗi trả JSON `{error, message, details, request_id}`; mọi response đều có header `X-Request-ID`. Tìm giá trị này trong log server để thấy traceback. Ngoại lệ: đăng nhập sai trả trực tiếp `{"error": "unauthorized", "message": "Invalid credentials."}` (401), không có `request_id` trong body.

| HTTP | `error` | Ý nghĩa và cách xử lý |
|---|---|---|
| 400 | `project_required` | User có nhiều project: gửi header `X-Project-Id`. |
| 422 | `validation_error` | Dữ liệu sai; xem `details`. (Một số payload sai khác, vd body login không hợp lệ, trả 400.) |
| 401 | — | Token thiếu, hết hạn hoặc bị thu hồi (đổi mật khẩu/role): đăng nhập lại. |
| 403 | `forbidden` | Role hoặc quyền project không đủ. |
| 403 | `policy_violation` | Policy chặn lệnh (không có trong allowlist, lệnh nguy hiểm, ký tự nối lệnh). |
| 403 | `management_interface` | Thay đổi chạm vào cổng quản trị: chỉ định cổng khác. |
| 404 | `not_found` | Không tồn tại **hoặc không có quyền** truy cập project. |
| 409 | `invalid_state` / `conflict` | Sai trạng thái change; trùng hostname/IP; interface đã nối dây; lease bận. |
| 429 | `rate_limit_exceeded` | Vượt rate limit hoặc quota AI/ngày. |
| 502 | `ssh_timeout`, `ssh_connection_error`, `ssh_authentication_error`, `device_unreachable` | Lỗi SSH tới thiết bị. |
| 502 | `ai_provider_error` | Gemini/Anthropic lỗi hoặc từ chối. |
| 503 | `ai_not_configured` | Thiếu `AI_API_KEY` hoặc chưa cài SDK provider. |

### 13.2. Không cài được / không khởi động được

| Triệu chứng | Nguyên nhân | Xử lý |
|---|---|---|
| Lỗi ngay khi import (thường là `TypeError` liên quan cú pháp `X \| None`) | Python 3.10 | Dùng Python ≥ 3.11 (Ubuntu 24.04 hoặc `python3.11`). |
| `setup_ai_server.sh` dừng ở bước venv | Thiếu gói venv | `sudo apt install python3.12-venv` (đúng phiên bản). |
| Lỗi Fernet / `CREDENTIAL_ENCRYPTION_KEY` khi lưu credential | Khóa trống hoặc sai định dạng | Sinh lại bằng lệnh ở Bước 3 (chỉ khi chưa có credential nào được lưu). |
| `no such table` | Chưa migration hoặc đang mở DB khác | `flask --app wsgi db upgrade`; kiểm tra `DATABASE_URL`. |
| Cổng 5000 bận | Tiến trình khác đang chạy | `sudo ss -lntp \| grep :5000`, dừng tiến trình cũ. |

### 13.3. Không đăng nhập được

```bash
cd backend && python scripts/diagnose_login.py admin
```

Script cho biết: đang mở file DB nào, tài khoản có tồn tại không, có active không, role gì, mật khẩu có khớp không, và các lần đăng nhập gần nhất trong audit. Quên mật khẩu thì chạy `python scripts/manage_users.py set-password admin`. Đăng nhập sai quá 5 lần/phút sẽ nhận 429: chờ 1 phút.

### 13.4. Chat AI lỗi

| Triệu chứng | Kiểm tra |
|---|---|
| `503 ai_not_configured` | `grep AI_API_KEY backend/.env`; với Anthropic cần `pip install -e ".[anthropic]"`. |
| `502 ai_provider_error` | Quota/key Gemini; chạy `python scripts/diagnose_gemini.py`; thử đổi `AI_MODEL`. |
| AI hiểu sai ý (vd coi câu đặt IP là thiết kế) | Kiểm tra `AI_INTENT_ROUTER`/`AI_ROUTER_MODEL`; chạy `scripts/eval_intent_router.py`; ghi rõ tên thiết bị và cổng trong câu. |
| "Daily AI quota reached." | Tăng `ai_daily_limit` qua `PATCH /api/policy` (ADMIN) hoặc chờ sang ngày mới. |
| AI không biết thiết bị | Thiết bị phải là `managed` + `provisioned` trong **đúng project**; hostname khớp đúng tên trên PNETLab. |

### 13.5. SSH tới thiết bị thất bại

Trên AI Server:

```bash
ping -c 3 172.16.3.111
nc -zv 172.16.3.111 22
ssh -o KexAlgorithms=+diffie-hellman-group14-sha1 -o HostKeyAlgorithms=+ssh-rsa g1@172.16.3.111
./.venv/bin/python scripts/smoke_test_lab.py
```

| Lỗi | Nguyên nhân thường gặp |
|---|---|
| `device_unreachable` / `ssh_timeout` | Sai IP, thiếu route/gateway, node PNETLab chưa chạy, chưa bootstrap SSH. |
| `ssh_authentication_error` | Sai credential: đặt lại bằng `set_device_credential.py`. |
| Lỗi đàm phán KEX | Đang dùng `paramiko>=5`: cài lại `pip install "paramiko>=3.4,<5"`. |
| Thiết bị báo "Invalid input" khi apply | Sai cú pháp với loại thiết bị (IOS vs ASA vs PAN-OS); xem `apply_output`. |

Trên thiết bị Cisco, kiểm tra cấu hình SSH:

```text
show ip ssh
show running-config | section line vty
show ip interface brief
```

### 13.6. Thay đổi cấu hình bị kẹt hoặc báo lỗi

| Thông báo | Ý nghĩa và cách xử lý |
|---|---|
| "Approval expired; preview and approve again." | Quá `approval_minutes` (60 phút): tạo preview mới. |
| "Approval no longer matches this configuration…" | Lệnh, đích, baseline hoặc policy đã đổi sau khi duyệt: preview lại. |
| "Running configuration changed since preview" | Có người sửa thiết bị ngoài hệ thống: preview lại. |
| "Chưa có cấu hình nền của …" | Lúc tạo change không SSH được để chụp baseline: kiểm tra thiết bị, hủy change và gửi lại. |
| "Another deployment owns this device." | Lease còn tồn tại (worker cũ bị ngắt). Xác nhận không còn tiến trình nào ghi, rồi ADMIN gọi `POST /api/operations/leases/<device_id>/release` với `{"confirm_worker_stopped": true, "reason": "..."}`. |
| "Verification failed…" | Thiết bị không phản ánh thay đổi: xem `verification_output`, áp rollback gợi ý bằng tay hoặc Undo. |
| "Device is offline." | Snapshot gần nhất báo offline: `POST /api/devices/<id>/refresh`. |
| `partial_success` | Một số thiết bị trong batch lỗi: xử lý từng thiết bị con. |

### 13.7. PNETLab

| Triệu chứng | Xử lý |
|---|---|
| "Resource not found (60038)" | PNETLab 4/5 chỉ có API theo session; để *Kiểu API* = PNETLab, dùng đúng tài khoản đang mở lab. |
| Import không thấy lab | PNETLab mỗi tài khoản chỉ có 1 session; lab đang chạy thuộc tài khoản đã mở nó. Để trống lab path để dùng lab đang mở. |
| "Console đang là VNC, đổi sang TELNET trong PNETLab" | Đổi kiểu console của node sang telnet. |
| Bootstrap không đặt được IP | Node chưa chạy, hoặc thiếu dây nối tới **Management(Cloud0)**. |
| PA-VM bootstrap lâu, timeout | Bình thường là nhiều phút; chạy Gunicorn 1 worker. Muốn tăng thời gian chờ phải sửa `config.py` (xem ghi chú ở [mục 4.2](#env)). |
| Job bootstrap/export trả 404 "This bootstrap job does not exist (or the service restarted)" | Job chỉ nằm trong bộ nhớ tiến trình: Gunicorn chạy nhiều worker, hoặc service vừa restart. Chuyển về `--workers 1 --threads N`; kiểm tra trạng thái node thực tế trước khi chạy lại. |

### 13.8. Giao diện

| Triệu chứng | Xử lý |
|---|---|
| Chat không tự cập nhật | Gunicorn đang dùng worker sync: chuyển sang `gthread` hoặc đặt `REALTIME_ENABLED=false`. |
| Giao diện cũ sau khi deploy | URL static có hash nên thường tự làm mới; thử Ctrl+F5. |
| Monitoring không chạy | `MONITORING_ENABLED=true` rồi restart; xem `GET /api/dashboard/monitoring`. |

### 13.9. Phân biệt ba nguyên nhân "không có kết quả"

1. **Không tới được thiết bị** (SSH): xem `command_executions.status` và lỗi 502.
2. **Bị policy chặn** (403 `policy_violation`): xem `audit_logs` với action `ai.command_blocked`.
3. **AI hiểu sai ý định**: xem payload `intent` trong transcript; thử viết rõ hơn, dùng `mode=design`, hoặc gõ thẳng lệnh `show ...`.

[↑ Về mục lục](#top)

---

<a id="testing"></a>
## 14. Kiểm thử

```bash
cd backend
pytest -q
pytest -v --cov=network_copilot --cov-report=term-missing
pytest tests/changes -q            # chỉ chạy một nhóm
```

- Bộ test **không bao giờ mở socket hay gọi model thật**: SSH và AI được giả lập qua `SSH_CLIENT_FACTORY` và `AI_PROVIDER_INSTANCE` (`tests/fakes/`).
- Kết quả chạy ngày 18/11/2026 trên Python 3.13: **1995 passed** (khoảng 8,5 phút).
- Kiểm thử tích hợp với lab thật: `smoke_test_lab.py`, rồi `demo_check.py`.
- Đánh giá AI: `python scripts/evaluate_ai.py` (corpus `evaluation/prompt_corpus.json`), `python scripts/eval_intent_router.py`.

[↑ Về mục lục](#top)

---

<a id="security"></a>
## 15. Hardening và vận hành an toàn

1. **Bí mật:** đổi `SECRET_KEY`/`JWT_SECRET_KEY`, `chmod 600 .env`, không commit `.env`, sao lưu `CREDENTIAL_ENCRYPTION_KEY` riêng.
2. **Mạng:** chỉ mở cổng 5000 cho mạng quản trị/VPN; đặt reverse proxy HTTPS nếu truy cập ngoài lab.
3. **Tài khoản:** một ADMIN duy nhất; cấp `VIEWER` cho người chỉ cần xem; chia sẻ project với quyền tối thiểu (viewer trước, editor khi thật cần).
4. **Phân loại thiết bị đúng:** đặt `environment` (`virtual`/`physical_lab`/`production`), `isolated`, `role` chính xác, vì phân loại rủi ro dựa vào các trường này.
5. **Credential thiết bị:** dùng tài khoản SSH riêng cho NetAI (vd `ai-automation`) với quyền vừa đủ.
6. **Duyệt cẩn thận:** AI có thể đề xuất sai. Luôn đọc lệnh trước khi Approve; rollback **không** tự động chạy.
7. **Policy:** bổ sung `forbidden_commands`/`high_risk_commands` phù hợp tổ chức.
8. **Giám sát:** theo dõi `audit_logs`, `/api/dashboard/monitoring`, dung lượng `device_snapshots`/`command_executions` (hai bảng này tăng liên tục, chưa có job dọn tự động).
9. **Sao lưu định kỳ** DB + khóa; thử khôi phục.
10. **Quyền riêng tư:** transcript chat và audit có thể chứa tên user, hostname, output thiết bị; giới hạn người đọc.

[↑ Về mục lục](#top)

---

### 15.1. Những điều không được tuyên bố khi trình bày

- `setup_ai_server.sh` **không** tạo systemd service, không cài Gunicorn và không cấu hình IP cho node (câu "installed systemd service" trong README đã cũ).
- Không có rollback tự động; rollback chỉ là gợi ý.
- Không có MFA đang hoạt động (xem [7.1](#rbac)).
- Không hỗ trợ đầy đủ đa vendor; ASA/PAN-OS chỉ hỗ trợ một phần.
- Kết quả AI và verification `best_effort` không phải chứng nhận mạng đã an toàn.
- Không nói "AI không bao giờ thấy dữ liệu nhạy cảm" hay "AI không thấy IP quản trị": chỉ ngữ cảnh inventory/topology được lọc; output troubleshoot (có thể chứa IP) và nội dung người dùng gõ vẫn được gửi đi.
- Không nói management guard chặn mọi thao tác làm mất kết nối quản trị.
- Không nói bootstrap console đi qua Preview → Approve → Apply.
- Không trình bày role TEACHER/STUDENT, group hay Smart Approval: project không dùng module governance.

[↑ Về mục lục](#top)

---

<a id="checklist"></a>
## 16. Checklist nghiệm thu

- [ ] Python ≥ 3.11, `pip install -e ".[dev]"` thành công.
- [ ] `.env` có `SECRET_KEY`, `JWT_SECRET_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, `AI_API_KEY`; không có biến trùng lặp; quyền 600.
- [ ] `flask db upgrade` chạy không lỗi; `flask db check` không báo lệch.
- [ ] `pytest -q` pass toàn bộ.
- [ ] Có đúng 1 tài khoản ADMIN; đăng nhập được ở `/`.
- [ ] Đã phổ biến cho người dùng: không nhập mật khẩu/cấu hình nhạy cảm vào chat AI.
- [ ] `GET /api/health` trả `{"status":"ok"}`.
- [ ] AI Server có IP tĩnh trong mạng quản trị, ping được gateway và các thiết bị.
- [ ] Project lab có thiết bị với hostname khớp PNETLab, credential đã lưu.
- [ ] `smoke_test_lab.py` SSH thành công tới mọi thiết bị.
- [ ] Chat "show ip interface brief trên R1" trả về output đã parse.
- [ ] Gửi `reload` / `write erase` vào API read-only (`POST /api/commands/execute-readonly`) bị từ chối với `policy_violation`. (Trong change workflow, các lệnh này **không** tự bị cấm mà chỉ bật `requires_confirmation`, trừ khi ADMIN thêm vào `forbidden_commands`.)
- [ ] Một thay đổi VLAN đi đủ Preview → Approve → Apply → Verify, có backup và audit.
- [ ] Batch ≥ 2 thiết bị có lệnh cần xác nhận yêu cầu `CONFIRM ALL`; batch 1 thiết bị yêu cầu hostname.
- [ ] Tài khoản VIEWER không tạo được cấu hình/project; OPERATOR chỉ thấy project mình sở hữu, project được chia sẻ và lab mặc định (quyền viewer).
- [ ] Monitoring bật và dashboard hiển thị trạng thái mới.
- [ ] Gunicorn chạy `gthread`, 1 worker, số thread lớn hơn `REALTIME_MAX_STREAMS`; service tự khởi động lại.
- [ ] Có backup DB + khóa mã hóa và đã thử khôi phục.

### 16.1. Kịch bản trình bày gợi ý

1. Giới thiệu mục tiêu, kiến trúc (mục 1) và môi trường lab thực tế của nhóm (IP, image, phiên bản).
2. Minh họa clone, venv, `.env` (đã che khóa), `db upgrade`, khởi chạy, `/api/health`.
3. Chọn project, nhập thiết bị/topology từ PNETLab, Test kết nối SSH.
4. Chat lần lượt: câu lý thuyết, yêu cầu thiết kế, lệnh kiểm tra thiết bị; giải thích 4 nhãn router và cây quyết định ở [8.3](#flow-chat).
5. Gọi `POST /api/commands/execute-readonly` với `"command": "reload"` để minh họa policy đọc trả `403 policy_violation`. Không minh họa bằng chat, vì AI có thể hiểu đó là yêu cầu cấu hình và tạo preview.
6. Tạo thay đổi VLAN nhỏ: giải thích risk, phân quyền, duyệt, backup, apply, verification; mở audit log.
7. Đăng nhập bằng tài khoản VIEWER để chứng minh không vượt quyền; nêu các giới hạn ở [15.1](#security).

[↑ Về mục lục](#top)

---

<a id="references"></a>
## 17. Tài liệu tham khảo

**Tài liệu nội bộ (trong repository):**

- [`backend/README.md`](../backend/README.md): mô hình an toàn, toàn bộ API, cấu hình AI provider
- [`docs/TONG_QUAN_PROJECT.md`](TONG_QUAN_PROJECT.md): tổng quan kiến trúc và module
- [`docs/database.md`](database.md): thiết kế database, ràng buộc, ER diagram
- [`docs/pending-history-integration.md`](pending-history-integration.md), [`docs/history-extension-integration.md`](history-extension-integration.md): lịch sử nháp/pending
- [`docs/roadmap.md`](roadmap.md): tính năng đã làm và giới hạn đã biết
- [`docs/evidence/2026-08-03-ai-network-copilot-evaluation.md`](evidence/2026-08-03-ai-network-copilot-evaluation.md): bằng chứng đánh giá AI

**Tài liệu chính thức bên ngoài:**

- [Flask](https://flask.palletsprojects.com/) · [Flask-Migrate](https://flask-migrate.readthedocs.io/) · [Flask-JWT-Extended](https://flask-jwt-extended.readthedocs.io/)
- [Gunicorn: worker types](https://docs.gunicorn.org/en/stable/design.html)
- [Paramiko](https://www.paramiko.org/) · [Netmiko](https://github.com/ktbyers/netmiko)
- [Google Gemini API](https://ai.google.dev/gemini-api/docs) · [Anthropic API](https://docs.anthropic.com/)
- [PNETLab](https://pnetlab.com/)
- [Cisco IOS: Configuring SSH](https://www.cisco.com/c/en/us/support/docs/security-vpn/secure-shell-ssh/4145-ssh.html)

> **Ghi chú phạm vi:** Chưa có trong phiên bản này: zero-touch provisioning, rollback tự động (rollback chỉ được gợi ý), hỗ trợ đa vendor đầy đủ, HA/cluster. ACL, DHCP pool và OSPF single-area là capability mở rộng, cần thêm bằng chứng lab trước khi coi là sẵn sàng production.

[↑ Về mục lục](#top)
