# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

智能批量 PDF 文本提取工具 (intelligent batch PDF text extractor) with two modes:

- **Web mode**: Flask + PocketBase — browser upload → background processing → SSE progress → CSV download
- **CLI mode**: `pdf_processor.py` standalone — no PocketBase/Flask dependency at all

Both share the same core pipeline: **detect PDF type → route to the right extractor → CSV output**. Comments and logs are in Chinese — match that style. READMEs are bilingual and must be updated together: `README.md` (English) and `README.zh-CN.md` (简体中文).

## Commands

### Local development (web mode)

PocketBase is a hard dependency for web mode — start it first:

```bash
# 1. Install Python deps
pip install -r requirements.txt

# 2. Download PocketBase v0.36.8 (see README for per-OS URLs), then:
unzip pb.zip && ./pocketbase serve        # listens on 127.0.0.1:8090
# First run: create admin (defaults: admin@admin.com / adminadmin123)

# 3. Create tasks/pdf_files collections (idempotent):
python web/init_pb.py                     # uses PB_URL env (default 127.0.0.1:8090)

# 4. Start Flask (run from repo root):
FLASK_PORT=5000 python web/app.py         # FLASK_DEBUG=1 enables debug
```

Health check (verifies Flask + PB connectivity): `curl http://localhost:5000/health`

### CLI mode (no PocketBase needed)

```bash
python pdf_processor.py --input ./pdfs -o out.csv       # full batch extraction
python pdf_processor.py --input ./pdfs --detect-only    # type detection only
python pdf_detector.py test_file.pdf                    # single-file detection diagnostics
python mineru_client.py test_scan.pdf                   # test MinerU OCR connectivity
```

### Docker

```bash
docker build -t pdf-text-extractor .
docker run -d -p 5000:5000 -p 8090:8090 \
  -e MINERU_API_TOKEN=your_token \
  -v pdf-extractor-data:/pb_data \
  pdf-text-extractor
```

Container entrypoint runs: PocketBase serve → `web/init_pb.py` (auto-creates admin + collections, rebuilds incomplete ones) → `web/run_flask.py`.

**No automated test suite and no linter config exist** — the README's "测试与验证" commands above are the manual verification path.

## Architecture

### Core modules (repo root)

- `pdf_detector.py` — `PDFTypeDetector`: classifies each PDF as `text_only` / `scan_only` / `mixed`. Signals in priority order: font presence (strongest — native text layer), text density (>200 chars/page → text, <20 → scan), significant image coverage. Decorative images (logos/watermarks/headers, <5% page area or edge regions) are filtered out via `get_image_rects()` so they don't skew detection. Returns per-page `PageDiagnosis` for debugging.
- `pdf_processor.py` — `PDFProcessor`: batches detection, then routes. `text_only` → PyMuPDF (`fitz`) text-layer extraction (fast, free, exact). `scan_only`/`mixed` → MinerU OCR via async `asyncio.gather`. Also owns `.env` loading (`_load_env_file` — manual parser, no python-dotenv, never overrides already-set env vars) and CSV export.
- `mineru_client.py` — `MinerUClient` (aiohttp, async): auto-selects Precision API (needs `MINERU_API_TOKEN`) or falls back to the free Agent API (limits: ≤10MB, ≤20 pages). Submits file → polls task status until done/timeout.

### Web layer (`web/`)

- `web/app.py` — Flask app. `POST /api/tasks` accepts multipart upload, saves files locally under `web/uploads/<task_id>/`, creates PB records, and spawns a **daemon background thread** (`_process_task_background`) that does per-file detect→extract→progress-update→CSV→attach-to-PB. SSE endpoint (`/api/tasks/<id>/events`) polls the PB task record every 2s and pushes only on change. CSV download pulls from PB with a local-file fallback. Cancellation via `threading.Event` checked between files.
- `web/pb_client.py` — `PocketBaseClient`: thin `requests` wrapper (no SDK). Auth endpoint is PB 0.36+ style: `/api/collections/_superusers/auth-with-password`. Config from `PB_URL`, `PB_ADMIN_EMAIL`, `PB_ADMIN_PASSWORD`.
- **PocketBase is the source of truth for web mode**: `tasks` collection (status/counts/`result_csv` file field) 1:N `pdf_files` (relation + original PDF file field). Both PB file fields have `maxSize` 200MB (matches Flask `MAX_CONTENT_LENGTH`). Progress lives in PB, so SSE survives page refreshes.

### Import patterns (don't break these)

`web/` is **not a package**: `web/app.py` loads `pb_client.py` via `importlib.util.spec_from_file_location` by file path, and imports core modules (`pdf_processor`, `pdf_detector`, `mineru_client`) after inserting the project root into `sys.path`. `web/run_flask.py` is container-only (hardcodes `/app` chdir) — run locally with `python web/app.py` from the repo root.

### CSV output schema

Columns: `unique_id, source_filename, content, pdf_type, error` (empty on success), plus `filename` (source_filename minus `.pdf`) in web mode. Written as `utf-8-sig` so Excel opens Chinese text correctly.

## Configuration

Env vars (loaded from `.env` at repo root + process env — see `.env.example`): `PB_URL` (default `http://127.0.0.1:8090`), `PB_ADMIN_EMAIL`, `PB_ADMIN_PASSWORD`, `MINERU_API_TOKEN`, `FLASK_PORT` (5000), `FLASK_DEBUG` (0), `MAX_CONCURRENT_TASKS` (3 — MinerU OCR concurrency; CLI `--workers` overrides it), `POLL_INTERVAL` (5s), `TASK_TIMEOUT` (600s).

## Platform notes

- `.gitattributes` forces **LF everywhere** — Windows CRLF breaks the Docker entrypoint shell script; never commit CRLF to `.sh`/`Dockerfile`.
- Dockerfile pins `python:3.12-slim-bookworm` deliberately (Debian Trixie renamed apt packages and broke the build) and installs pip deps via CN mirrors (Tsinghua → Aliyun fallback) — keep both when touching the Dockerfile.
