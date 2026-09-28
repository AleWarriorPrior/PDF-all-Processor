[English](README.md) | [简体中文](README.zh-CN.md)

# 📄 PDF-all-Processor — Intelligent Batch PDF Text Extraction

## 🎯 Overview

An intelligent batch PDF processing tool (Web + CLI) that:

- **Auto-identifies PDF type**: text-only, scanned, or mixed — each handled by the strategy that fits best
- **Smart extraction routing**: text PDFs are read directly with PyMuPDF; scanned files go through MinerU OCR
- **Web UI**: drag & drop upload → live progress → one-click CSV download
- **Persistent tasks**: backed by PocketBase — task records survive restarts

### Why this tool?

| Scenario | Traditional approach | This tool |
|----------|---------------------|-----------|
| Text-only PDF | OCR everything → **slow, error-prone** | PyMuPDF direct read ✅ **fast & exact** |
| Scanned PDF | Read text layer → **finds nothing** | MinerU OCR ✅ **high-quality recognition** |
| Mixed PDF | One strategy for all → **something always breaks** | Smart routing ✅ **best of both** |

---

## 🏗️ Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Browser (frontend)                       │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │  Upload panel │ Progress board (type stats) │ CSV download │  │
│   └──────────────────────┬──────────────────────────────────┘    │
└─────────────────────────│───────────────────────────────────────┘
                          │ HTTP / SSE
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Flask web service (port 5000)                  │
│   ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐     │
│   │ Upload API   │  │ SSE push     │  │ CSV download/cancel│     │
│   └──────┬───────┘  └──────┬───────┘  └────────▲───────────┘     │
│          └────────┬────────┘                   │                 │
│                   ▼                            │                 │
│          ┌─────────────────────┐               │                 │
│          │ Background workers  │───────────────┘                 │
│          │ detect → extract → CSV                                │
│          └──────────┬──────────┘                                  │
└─────────────────────│─────────────────────────────────────────────┘
                      │ read/write
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│            PocketBase (port 8090) — data & file storage           │
│   ┌─────────────────────┐      ┌──────────────────────────┐      │
│   │ tasks collection    │ 1:N  │ pdf_files collection     │      │
│   │ · status            │──────│· task (relation)         │      │
│   │ · total_files       │      │· pdf_file (file field)   │      │
│   │ · result_csv (file) │      │· filename / content      │      │
│   │ · progress ...      │      │· pdf_type                │      │
│   └─────────────────────┘      └──────────────────────────┘      │
└──────────────────────────────────────────────────────────────────┘
                      │ (scanned/mixed only)
                      ▼
┌──────────────────────────────────────┐
│         MinerU API (external)        │
│   AI OCR / formula / table parsing   │
└──────────────────────────────────────┘
```

### Core modules

| Module | File | Responsibility |
|--------|------|----------------|
| Web entry | `web/app.py` | Flask routes, SSE push, background task orchestration |
| PB client | `web/pb_client.py` | PocketBase auth, CRUD, file upload/download |
| PB init | `web/init_pb.py` | Auto-creates admin account + `tasks`/`pdf_files` collections (honors `PB_URL` env) |
| Flask launcher | `web/run_flask.py` | Container-only Flask bootstrap |
| Frontend | `web/templates/index.html` | Upload UI + live progress + type stats |
| Stylesheet | `web/static/style.css` | Responsive layout, type stat cards |
| Type detection | `pdf_detector.py` | Per-page font presence + text density + significant image coverage (filters logos/watermarks) |
| Text extraction | `pdf_processor.py` | PyMuPDF local extraction + MinerU OCR dispatch |
| OCR client | `mineru_client.py` | MinerU async API wrapper (Precision/Agent auto-select) |

---

## 🚀 Quick start

### Option 1: Docker one-command deploy (recommended)

> The image bundles PocketBase + Flask and initializes everything on first boot.

```bash
# 1. Clone
git clone https://github.com/AleWarriorPrior/PDF-all-Processor.git
cd PDF-all-Processor

# 2. Build image (bundles PocketBase v0.36.x)
docker build -t pdf-all-processor .

# 3. Run
docker run -d \
  --name pdf-extractor \
  -p 5000:5000 \
  -p 8090:8090 \
  -e MINERU_API_TOKEN=your_token_here \
  -v pdf-extractor-data:/pb_data \
  pdf-all-processor

# 4. Open
open http://localhost:5000
```

**Ports:**

| Port | Service | Purpose |
|------|---------|---------|
| **5000** | Flask | Web frontend + API |
| **8090** | PocketBase | Database + file storage + admin UI |

> 💡 PocketBase admin UI: `http://<host>:8090/_/` (default account `admin@admin.com` / `adminadmin123`)

**Platform compatibility:** Ubuntu/Debian, CentOS/RHEL/Fedora, macOS (Docker Desktop/OrbStack), Windows 10/11 (WSL2). The repo enforces LF line endings via `.gitattributes`, so cloned sources build cleanly everywhere.

### Option 2: Local development

#### Step 1 — Start PocketBase (required for web mode)

```bash
# Download PocketBase v0.36.8 for your platform (see README assets), then:
unzip pb.zip && ./pocketbase serve        # listens on http://127.0.0.1:8090
# First run: create the admin account (defaults: admin@admin.com / adminadmin123)
```

#### Step 2 — Initialize collections

```bash
python web/init_pb.py       # idempotent; uses PB_URL env (default 127.0.0.1:8090)
```

This creates the `tasks` and `pdf_files` collections with all fields and sets the file-size limit to 200MB. You can also create them by hand in the PB admin UI (field list below).

<details>
<summary>Collection schemas (manual setup)</summary>

**tasks**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| status | text | ✅ | pending / processing / completed / failed / cancelled |
| total_files | number(int) | ✅ | |
| processed_files | number(int) | ❌ | |
| success_count | number(int) | ❌ | |
| failed_count | number(int) | ❌ | |
| current_filename | text | ❌ | |
| error_message | text | ❌ | |
| result_csv | file | ❌ | generated CSV result |

**pdf_files**

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| task | relation(tasks) | ✅ | cascadeDelete |
| filename | text | ✅ | original filename |
| status | text | ❌ | |
| pdf_type | text | ❌ | text_only / scan_only / mixed |
| content | editor | ❌ | extracted text |
| error_message | text | ❌ | |
| pdf_file | file | ❌ | original PDF |

</details>

#### Step 3 — Configure and run Flask

```bash
# 1. Clone
git clone https://github.com/AleWarriorPrior/PDF-all-Processor.git
cd PDF-all-Processor

# 2. Virtual environment
python -m venv venv
source venv/bin/activate  # Linux/Mac

# 3. Dependencies
pip install -r requirements.txt

# 4. Configuration
cp .env.example .env
# edit .env — set your MinerU token (needed only for scanned files)

# 5. Start
export FLASK_PORT=5002        # optional; defaults to 5000
python web/app.py
```

### Option 3: CLI mode (no web UI)

Bypasses PocketBase and Flask entirely:

```bash
python pdf_processor.py --input ./your_pdf_folder -o ./output/result.csv
```

See the [CLI guide](#-cli-usage-no-web-ui) below.

---

## 📖 Web usage

```
Open page → drop PDFs → "Start" → watch live progress (with type breakdown)
→ done → download CSV
```

| Area | Function |
|------|----------|
| **Upload zone** | Multi-select & drag-drop, up to 200MB per file |
| **Progress panel** | SSE live updates: processed/success/fail counters, current file |
| **📊 Type breakdown** | Three live cards: text-only (green), scanned (red), mixed (orange) |
| **Result area** | Summary + colored type badges + CSV download |
| **Cancel button** | Aborts a running task anytime |

### API quick reference

| Endpoint | Purpose |
|----------|---------|
| `POST /api/tasks` | multipart upload (`files`), creates task, starts processing → `{task_id}` |
| `GET /api/tasks/<id>/status` | Current progress & counters |
| `GET /api/tasks/<id>/events` | SSE stream (`message` / `done` / `cancelled` / `error`) |
| `GET /api/tasks/<id>/download` | Download result CSV |
| `POST /api/tasks/<id>/cancel` | Cancel a running task |
| `GET /health` | `{"status":"ok","pocketbase":"connected"}` |

### CSV output

Web mode columns: `unique_id, source_filename, content, pdf_type, filename` (+ `error` only when failures occurred). CLI mode columns: `unique_id, source_filename, content, pdf_type, error`. `filename` is `source_filename` without the `.pdf` suffix. Files are written `utf-8-sig` so Excel renders Chinese correctly.

```csv
unique_id,source_filename,content,pdf_type,filename
"550e8400-e29b...","contract_2024.pdf","Purchase contract
Party A: ...",text_only,contract_2024
"a1b2c3d4-e5f6...","receipt_scan.pdf","Receipt No.: 12345678
Amount: ...",scan_only,receipt_scan
```

---

## 🔧 Configuration

All settings live in `.env` (see `.env.example`) or process environment:

| Variable | Description | Default |
|----------|-------------|---------|
| **PB_URL** | PocketBase URL | `http://127.0.0.1:8090` |
| **PB_ADMIN_EMAIL** | PB superuser email | `admin@admin.com` |
| **PB_ADMIN_PASSWORD** | PB superuser password | `adminadmin123` |
| `MINERU_API_TOKEN` | MinerU API token | — (free tier works without) |
| `FLASK_PORT` | Flask listen port | `5000` |
| `FLASK_DEBUG` | Flask debug mode | `0` |
| `MAX_CONCURRENT_TASKS` | MinerU OCR concurrency (web default; CLI `--workers` overrides) | `3` |
| `POLL_INTERVAL` | MinerU task-status poll interval (seconds) | `5` |
| `TASK_TIMEOUT` | Per-file OCR timeout (seconds) | `600` |

### Getting a MinerU token

1. Register at [mineru.net](https://mineru.net/)
2. Open the [API management page](https://mineru.net/apiManage)
3. Create and copy your token

> **Note:** the free Agent API works without a token but limits files to ≤10MB and ≤20 pages. Use the Precision API (token required) for larger files.

---

## 🧪 Testing & verification

```bash
# Health check (Flask + PocketBase connectivity)
curl http://localhost:5000/health
# expected: {"status":"ok","pocketbase":"connected"}

# PDF type detection
python pdf_detector.py test_file.pdf

# MinerU API connectivity
python mineru_client.py test_scan.pdf

# Preview type distribution for a folder
python pdf_processor.py --input ./test_pdfs --detect-only
```

End-to-end smoke test against a running web instance:

```bash
# Upload files and create a task
curl -F "files=@a.pdf" -F "files=@b.pdf" http://localhost:5000/api/tasks
# → {"task_id":"..."} — then poll / download:
curl http://localhost:5000/api/tasks/<task_id>/status
curl -o result.csv http://localhost:5000/api/tasks/<task_id>/download
```

---

## 📂 Project structure

```
pdf-all-processor/
├── web/
│   ├── app.py                  # Flask entry (routes / SSE / background tasks)
│   ├── pb_client.py            # PocketBase client wrapper
│   ├── init_pb.py              # PB auto-init (admin + collections, PB_URL-aware)
│   ├── run_flask.py            # Container-only Flask launcher
│   ├── templates/
│   │   └── index.html          # Frontend single page (upload + progress + result)
│   └── static/
│       └── style.css           # Stylesheet (responsive)
├── pdf_processor.py            # Core pipeline (PyMuPDF + MinerU dispatch, CLI entry)
├── pdf_detector.py             # PDF type detection (fonts / density / image coverage)
├── mineru_client.py            # MinerU async OCR API client
├── requirements.txt            # Python dependencies
├── Dockerfile                  # Docker build (bundles PB + Flask)
├── docker-entrypoint.sh        # Container bootstrap (PB init + Flask)
├── .env.example                # Environment variable template
├── CLAUDE.md                   # Codebase guide for Claude Code sessions
├── README.md                   # This file (English)
└── README.zh-CN.md             # 简体中文版
```

---

## 🚀 Production deployment

### Docker Compose (recommended)

```yaml
# docker-compose.yml
services:
  pdf-extractor:
    build: .
    container_name: pdf-extractor
    ports:
      - "5000:5000"    # Flask web
      - "8090:8090"    # PocketBase
    environment:
      - MINERU_API_TOKEN=${MINERU_API_TOKEN}
      - FLASK_PORT=5000
      - PB_ADMIN_EMAIL=admin@admin.com
      - PB_ADMIN_PASSWORD=your_secure_password_here
    volumes:
      - pb_data:/pb_data          # PB data persistence
      - uploads:/app/web/uploads   # upload temp storage
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:5000/health"]
      interval: 30s
      timeout: 10s
      retries: 3

volumes:
  pb_data:
  uploads:
```

```bash
docker compose up -d
```

### Server checklist

- [ ] PocketBase v0.36.x (or let Docker install it)
- [ ] Python 3.9+ (not needed with Docker)
- [ ] Open ports: **5000** (Flask), **8090** (PB)
- [ ] Configure `.env` (especially PB credentials and MinerU token)
- [ ] Reverse proxy (Nginx/Caddy): 80/443 → 5000
- [ ] If PB admin needs external access, forward 8090 (restrict by IP in production)
- [ ] Backup strategy for **pb_data** (SQLite + stored files)
- [ ] Log monitoring and alerting

### Nginx reverse proxy reference

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # Flask web frontend
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # SSE long-connection essentials
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }

    # PocketBase (optional; restrict to trusted IPs)
    location /pb-admin/ {
        proxy_pass http://127.0.0.1:8090/_/;
        allow your_trusted_ip;
        deny all;
    }
}
```

### Performance tips

1. **Keep concurrency moderate**: 2–5 OCR workers avoids MinerU rate limits
2. **Large files first**: process big scanned files early, small ones later
3. **PB file size limit**: PB defaults to 5MB; this project raises it to 200MB (set automatically at init)
4. **Watch memory**: many PDFs in flight consume RAM
5. **Disk space**: PB stores original PDFs and CSVs — periodically clean completed tasks

---

## 🧰 CLI usage (no web UI)

```bash
# Process every PDF in a directory
python pdf_processor.py --input /path/to/pdfs

# Process specific files
python pdf_processor.py --input file1.pdf file2.pdf file3.pdf

# MinerU token (higher quota, larger files)
python pdf_processor.py --input ./pdfs --token YOUR_TOKEN

# Custom output path
python pdf_processor.py --input ./pdfs -o ./results/my_data.csv

# OCR concurrency (default 5)
python pdf_processor.py --input ./pdfs --workers 10

# Type detection only (no extraction)
python pdf_processor.py --input ./pdfs --detect-only

# Verbose logs
python pdf_processor.py --input ./pdfs --verbose
```

---

## ❓ FAQ

**Q: What is PocketBase and why is it needed?**
A: PocketBase is an embedded Go backend with SQLite + file storage + REST API + realtime subscriptions. This project uses it to store task state and progress, keep original PDFs and result CSVs, query task↔file relations (1:N), and revisit historical tasks. Far lighter than a PostgreSQL + MinIO + Redis stack for small/mid deployments.

**Q: Can I skip PocketBase?**
A: Not for web mode — the Flask layer depends on it for persistence and file storage. CLI mode has zero PB dependency: `python pdf_processor.py --input ...`.

**Q: Why not send text PDFs to MinerU too?**
A: Text PDFs already carry an exact, selectable text layer; OCR would only risk introducing errors. PyMuPDF reads it 100% accurately.

**Q: Limits of the free API?**
A: The Agent lightweight API is token-free but caps files at ≤10MB and ≤20 pages. Beyond that, use the Precision API with a token.

**Q: What happens when a file fails?**
A: One failing file never blocks the batch — failures are flagged in the CSV `error` column and counted in task stats.

**Q: PocketBase data lost?**
A: Everything lives in the `pb_data` directory (SQLite + files). Back up that directory on a schedule; mount a named volume with Docker.

---

## 📝 Changelog

### v2.1.0 (2026-09)
- ✅ Bilingual README (English + 简体中文)
- ✅ All documented env vars actually wired: `PB_URL`, `MAX_CONCURRENT_TASKS`, `POLL_INTERVAL`, `TASK_TIMEOUT`
- ✅ CLI `--workers` flag now controls MinerU OCR concurrency
- ✅ Web task lifecycle hardening: valid-JSON `cancelled` SSE events, `cancelled` treated as terminal status, worker-thread state cleanup, race-free CSV download temp files
- ✅ Added `.env.example`, `.gitignore`, `CLAUDE.md`

### v2.0.1 (2026-04)
- ✅ Docker build hardening: pinned Debian Bookworm, PB download retries, pip mirror fallback
- ✅ PocketBase init extracted to `web/init_pb.py` (auto admin + collections, rebuilds incomplete sets, 200MB file limit)
- ✅ CSV output: new `filename` column (source filename without `.pdf`)

### v2.0.0 (2026-04)
- ✅ Brand-new web UI: Flask + SSE live progress
- ✅ PocketBase integration: task persistence, file storage, history
- ✅ Docker one-command deploy (bundles PocketBase + Flask)
- ✅ PDF type breakdown visualization: frontend cards + result badges
- ✅ SSE realtime progress with task cancellation
- ✅ `/health` endpoint; Nginx SSE proxy reference config

### v1.0.0 (2025-01)
- ✅ Initial release (CLI mode)
- ✅ Three-type PDF detection
- ✅ PyMuPDF + MinerU dual engine
- ✅ Async batch processing with stats report

---

## 📄 License

MIT License
