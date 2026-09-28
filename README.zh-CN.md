[English](README.md) | [简体中文](README.zh-CN.md)

# 📄 PDF-all-Processor — 智能批量 PDF 文本提取工具

## 🎯 项目简介

一个智能的 PDF 批量处理工具（Web + CLI 双模式），能够：

- **自动识别 PDF 类型**：纯文本、扫描件、混合型 —— 三种策略各取所长
- **智能选择提取方案**：纯文本用 PyMuPDF 直接读，扫描件走 MinerU OCR
- **Web 端操作**：浏览器拖拽上传 → 实时进度查看 → 一键下载 CSV
- **任务持久化**：基于 PocketBase，断电/重启不丢任务记录

### 为什么需要这个工具？

| 场景 | 传统方案 | 本工具 |
|------|---------|--------|
| 纯文本 PDF | 用 OCR → **错误率高、速度慢** | PyMuPDF 直接提取 ✅ **快速准确** |
| 扫描件 PDF | 直接读文字 → **读不到内容** | MinerU OCR ✅ **高质量识别** |
| 混合型 PDF | 统一方案 → **总有一方出问题** | 智能分流处理 ✅ **各取所长** |

---

## 🏗️ 技术架构

```
┌──────────────────────────────────────────────────────────────────┐
│                         浏览器 (前端)                              │
│   ┌─────────────────────────────────────────────────────────┐    │
│   │  上传面板 │ 进度看板(含类型分布) │ 结果展示 & CSV 下载     │    │
│   └──────────────────────┬──────────────────────────────────┘    │
└─────────────────────────│───────────────────────────────────────┘
                          │ HTTP / SSE
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│                    Flask Web 服务 (端口 5000)                      │
│                                                                  │
│   ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐    │
│   │  文件上传 API │  │ SSE 实时推送  │  │  CSV 下载 / 取消    │    │
│   └──────┬───────┘  └──────┬───────┘  └────────▲───────────┘    │
│          │                 │                     │               │
│          └────────┬────────┘                     │               │
│                   ▼                              │               │
│          ┌─────────────────────┐                │               │
│          │  后台处理线程池       │────────────────┘               │
│          │  类型检测 → 提取 → CSV │                                │
│          └──────────┬──────────┘                                 │
└─────────────────────│────────────────────────────────────────────┘
                      │ 读写
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│              PocketBase (端口 8090) — 数据 & 文件存储               │
│                                                                  │
│   ┌─────────────────────┐      ┌──────────────────────────┐     │
│   │  tasks 集合          │ 1∶N  │  pdf_files 集合           │     │
│   │  · status           │──────│· task (relation)          │     │
│   │  · total_files      │      │· pdf_file (文件字段)       │     │
│   │  · result_csv (文件) │      │· filename / content       │     │
│   │  · progress ...     │      │· pdf_type                 │     │
│   └─────────────────────┘      └──────────────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
                      │ (仅扫描件/混合型)
                      ▼
┌──────────────────────────────────────┐
│        MinerU API (外部服务)          │
│   AI/OCR 公式识别 / 表格识别          │
└──────────────────────────────────────┘
```

### 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Web 入口 | `web/app.py` | Flask 路由、SSE 推送、后台任务编排 |
| PB 客户端 | `web/pb_client.py` | PocketBase 认证、CRUD、文件上传下载 |
| PB 初始化 | `web/init_pb.py` | 自动创建管理员与 `tasks`/`pdf_files` 集合（支持 `PB_URL` 环境变量） |
| Flask 启动器 | `web/run_flask.py` | 容器内 Flask 启动脚本 |
| 前端页面 | `web/templates/index.html` | 上传界面 + 实时进度 + 类型分布展示 |
| 样式表 | `web/static/style.css` | 响应式布局、类型统计卡片样式 |
| 类型检测 | `pdf_detector.py` | 逐页字体存在性 + 文本密度 + 有效图像覆盖率（过滤 logo/水印等装饰图） |
| 文本提取 | `pdf_processor.py` | PyMuPDF 本地提取 + MinerU OCR 调度 |
| OCR 客户端 | `mineru_client.py` | MinerU 异步 API 封装（精准/Agent 自动选择） |

---

## 🚀 快速开始

### 方式一：Docker 一键部署（推荐生产环境）

> Docker 镜像内置 PocketBase + Flask，首次启动自动完成全部初始化。

```bash
# 1. 克隆项目
git clone https://github.com/AleWarriorPrior/PDF-all-Processor.git
cd PDF-all-Processor

# 2. 构建镜像（包含 PocketBase v0.36.x）
docker build -t pdf-all-processor .

# 3. 运行容器
docker run -d \
  --name pdf-extractor \
  -p 5000:5000 \
  -p 8090:8090 \
  -e MINERU_API_TOKEN=your_token_here \
  -v pdf-extractor-data:/pb_data \
  pdf-all-processor

# 4. 打开浏览器访问
open http://localhost:5000
```

**端口说明：**

| 端口 | 服务 | 用途 |
|------|------|------|
| **5000** | Flask | Web 前端 + API |
| **8090** | PocketBase | 数据库 + 文件存储 + 管理后台 |

> 💡 PocketBase 管理后台：`http://<host>:8090/_/` （默认账号 `admin@admin.com` / `adminadmin123`）

**平台兼容性：** Ubuntu/Debian、CentOS/RHEL/Fedora、macOS（Docker Desktop/OrbStack）、Windows 10/11（WSL2）均可用。仓库已通过 `.gitattributes` 强制 LF 换行，clone 后可直接构建。

### 方式二：本地开发运行

#### 第一步：启动 PocketBase（Web 模式必需）

```bash
# 下载 PocketBase v0.36.8（按系统选择对应资产），然后：
unzip pb.zip && ./pocketbase serve        # 监听 http://127.0.0.1:8090
# 首次启动创建管理员账号（默认：admin@admin.com / adminadmin123）
```

#### 第二步：初始化数据集合

```bash
python web/init_pb.py       # 幂等脚本；读取 PB_URL 环境变量（默认 127.0.0.1:8090）
```

脚本会自动创建 `tasks` 和 `pdf_files` 两个集合并设置 200MB 文件上限。也可以在 PB 管理后台手动创建（字段见下方）。

<details>
<summary>集合字段说明（手动创建时参考）</summary>

**tasks 集合字段：**

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| status | text | ✅ | 任务状态：pending / processing / completed / failed / cancelled |
| total_files | number(int) | ✅ | 总文件数 |
| processed_files | number(int) | ❌ | 已处理数 |
| success_count | number(int) | ❌ | 成功数 |
| failed_count | number(int) | ❌ | 失败数 |
| current_filename | text | ❌ | 当前正在处理的文件名 |
| error_message | text | ❌ | 错误信息 |
| result_csv | file | ❌ | 生成的 CSV 结果文件 |

**pdf_files 集合字段：**

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| task | relation(tasks) | ✅ | 关联的任务（cascadeDelete） |
| filename | text | ✅ | 原始文件名 |
| status | text | ❌ | 处理状态 |
| pdf_type | text | ❌ | 检测结果：text_only / scan_only / mixed |
| content | editor | ❌ | 提取出的文本内容 |
| error_message | text | ❌ | 错误信息 |
| pdf_file | file | ❌ | 上传的 PDF 原始文件 |

> ⚠️ 创建 `task` relation 字段时，需指定目标为 `tasks` 集合。

</details>

#### 第三步：配置并启动 Flask

```bash
# 1. 克隆项目
git clone https://github.com/AleWarriorPrior/PDF-all-Processor.git
cd PDF-all-Processor

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量
cp .env.example .env
# 编辑 .env，填入你的 MinerU Token（仅扫描件 OCR 需要）

# 5. 启动 Flask
export FLASK_PORT=5002        # 默认 5000，被占用时可改
python web/app.py
```

### 方式三：CLI 命令行模式（无 Web 界面）

如果不需要 Web UI，也可以直接用命令行处理本地 PDF 文件：

```bash
python pdf_processor.py --input ./your_pdf_folder -o ./output/result.csv
```

详见下方「CLI 使用指南」。

---

## 📖 Web 使用指南

### 基本流程

```
打开页面 → 拖拽/选择 PDF 文件 → 点击「开始处理」
→ 实时查看进度（含文档类型分布统计） → 处理完成 → 下载 CSV
```

### 页面功能说明

| 区域 | 功能 |
|------|------|
| **上传区域** | 支持多选、拖拽上传，单文件最大 200MB |
| **进度面板** | SSE 实时推送：已处理/成功/失败计数、当前文件名 |
| **📊 类型分布** | 三栏卡片实时显示纯文本(绿)、扫描件(红)、混合型(橙)数量 |
| **结果区** | 处理完成后显示汇总 + 彩色类型徽章 + CSV 下载按钮 |
| **取消按钮** | 可随时中止正在进行的任务 |

### API 速查

| 接口 | 用途 |
|------|------|
| `POST /api/tasks` | multipart 上传（字段名 `files`），创建任务并开始处理 → 返回 `{task_id}` |
| `GET /api/tasks/<id>/status` | 查询进度与计数 |
| `GET /api/tasks/<id>/events` | SSE 事件流（`message` / `done` / `cancelled` / `error`） |
| `GET /api/tasks/<id>/download` | 下载结果 CSV |
| `POST /api/tasks/<id>/cancel` | 取消正在运行的任务 |
| `GET /health` | 返回 `{"status":"ok","pocketbase":"connected"}` |

### CSV 输出格式

Web 模式列：`unique_id, source_filename, content, pdf_type, filename`（仅在存在失败文件时追加 `error` 列）；CLI 模式列：`unique_id, source_filename, content, pdf_type, error`。`filename` 为去掉 `.pdf` 后缀的源文件名。CSV 以 `utf-8-sig` 编码写入，Excel 打开中文不乱码。

```csv
unique_id,source_filename,content,pdf_type,filename
"550e8400-e29b...","合同_2024.pdf","采购合同\n甲方：XX公司\n...",text_only,合同_2024
"a1b2c3d4-e5f6...","发票_扫描件.pdf","发票号码: 12345678\n金额: ¥10,000",scan_only,发票_扫描件
```

---

## 🔧 配置说明

所有配置来自 `.env` 文件（模板见 `.env.example`）或进程环境变量：

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| **PB_URL** | PocketBase 服务地址 | `http://127.0.0.1:8090` |
| **PB_ADMIN_EMAIL** | PB 管理员邮箱 | `admin@admin.com` |
| **PB_ADMIN_PASSWORD** | PB 管理员密码 | `adminadmin123` |
| `MINERU_API_TOKEN` | MinerU API Token | 无（免费 API 无需 Token） |
| `FLASK_PORT` | Flask 监听端口 | `5000` |
| `FLASK_DEBUG` | Flask 调试模式 | `0` |
| `MAX_CONCURRENT_TASKS` | MinerU OCR 并发数（Web 默认；CLI 可用 `--workers` 覆盖） | `3` |
| `POLL_INTERVAL` | MinerU 任务状态轮询间隔(秒) | `5` |
| `TASK_TIMEOUT` | 单文件 OCR 超时(秒) | `600` |

### 获取 MinerU Token

1. 访问 [MinerU 官网](https://mineru.net/)
2. 注册账号并登录
3. 进入 [API 管理页面](https://mineru.net/apiManage)
4. 创建/复制你的 API Token

> **注意**: 免费版 Agent API 无需 Token，但限制文件大小 ≤10MB 且 ≤20 页。
> 如果需要处理更大文件或更高并发，请申请 Token 使用精准 API。

---

## 🧪 测试与验证

```bash
# 健康检查（验证 Flask + PocketBase 连通性）
curl http://localhost:5000/health
# 预期返回: {"status":"ok","pocketbase":"connected"}

# 测试 PDF 类型检测
python pdf_detector.py test_file.pdf

# 测试 MinerU API 连接
python mineru_client.py test_scan.pdf

# CLI 快速预览一批文件的类型分布
python pdf_processor.py --input ./test_pdfs --detect-only
```

针对运行中的 Web 服务做端到端冒烟测试：

```bash
# 上传文件并创建任务
curl -F "files=@a.pdf" -F "files=@b.pdf" http://localhost:5000/api/tasks
# → {"task_id":"..."}，随后轮询状态 / 下载结果：
curl http://localhost:5000/api/tasks/<task_id>/status
curl -o result.csv http://localhost:5000/api/tasks/<task_id>/download
```

---

## 📂 项目结构

```
pdf-all-processor/
├── web/
│   ├── app.py                  # Flask Web 入口（路由/SSE/后台任务）
│   ├── pb_client.py            # PocketBase 客户端封装
│   ├── init_pb.py              # PB 自动初始化（管理员+集合，支持 PB_URL）
│   ├── run_flask.py            # 容器内 Flask 启动脚本
│   ├── templates/
│   │   └── index.html          # 前端单页（上传+进度+结果）
│   └── static/
│       └── style.css           # 样式表（响应式布局）
├── pdf_processor.py            # 核心处理逻辑（PyMuPDF + MinerU，CLI 入口）
├── pdf_detector.py             # PDF 类型智能检测（字体/密度/图像覆盖率）
├── mineru_client.py            # MinerU 异步 OCR API 客户端
├── requirements.txt            # Python 依赖清单
├── Dockerfile                  # Docker 构建文件（含 PB + Flask）
├── docker-entrypoint.sh        # 容器启动脚本（初始化 PB + 集合）
├── .env.example                # 环境变量模板
├── CLAUDE.md                   # Claude Code 代码库指南
├── README.md                   # 英文版文档
└── README.zh-CN.md             # 本文件（简体中文）
```

---

## 🚀 部署到生产环境

### Docker Compose 部署（推荐）

```yaml
# docker-compose.yml
services:
  pdf-extractor:
    build: .
    container_name: pdf-extractor
    ports:
      - "5000:5000"    # Flask Web
      - "8090:8090"    # PocketBase
    environment:
      - MINERU_API_TOKEN=${MINERU_API_TOKEN}
      - FLASK_PORT=5000
      - PB_ADMIN_EMAIL=admin@admin.com
      - PB_ADMIN_PASSWORD=your_secure_password_here
    volumes:
      - pb_data:/pb_data          # PB 数据持久化
      - uploads:/app/web/uploads   # 上传文件临时存储
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
# 一键启动
docker compose up -d
```

### 服务器部署清单

- [ ] **PocketBase** v0.36.x（或让 Docker 自动安装）
- [ ] Python 3.9+ 环境（Docker 部署则无需单独安装）
- [ ] 开放端口：**5000**（Flask）、**8090**（PB）
- [ ] 配置 `.env` 文件（尤其是 PB 连接信息和 MinerU Token）
- [ ] 反向代理（Nginx/Caddy）：将 80/443 转发到 5000
- [ ] 如果需要从外网访问 PB 管理后台，也转发 8090（生产环境建议限制 IP）
- [ ] 配置备份策略：**pb_data 目录**（SQLite 数据库 + 存储的文件）
- [ ] 监控日志和错误告警

### Nginx 反向代理参考

```nginx
server {
    listen 80;
    server_name your-domain.com;

    # Flask Web 前端
    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;

        # SSE 长连接关键配置
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        chunked_transfer_encoding on;
    }

    # PocketBase（可选，限制内网/IP白名单访问）
    location /pb-admin/ {
        proxy_pass http://127.0.0.1:8090/_/;
        allow your_trusted_ip;
        deny all;
    }
}
```

### 性能优化建议

1. **控制并发数**：建议 2-5 个 OCR 并发，避免触发 MinerU API 限频
2. **大文件优先**：先处理大的扫描件，小文件后处理
3. **PB 文件大小限制**：默认 5MB，本项目初始化时自动调整为 200MB
4. **内存监控**：大量 PDF 同时处理会占用较多内存
5. **磁盘空间**：PB 会存储原始 PDF 和 CSV，定期清理已完成的历史任务

---

## 🧰 CLI 使用指南（无 Web 界面）

> 此模式绕过 PocketBase 和 Flask，直接在本地处理。

```bash
# 处理单个目录中的所有 PDF
python pdf_processor.py --input /path/to/pdfs

# 处理指定的多个文件
python pdf_processor.py --input file1.pdf file2.pdf file3.pdf

# 使用 MinerU API Token
python pdf_processor.py --input ./pdfs --token YOUR_TOKEN

# 自定义输出路径
python pdf_processor.py --input ./pdfs -o ./results/my_data.csv

# OCR 并发数（默认 5）
python pdf_processor.py --input ./pdfs --workers 10

# 仅检测 PDF 类型（不实际提取）
python pdf_processor.py --input ./pdfs --detect-only

# 显示详细日志
python pdf_processor.py --input ./pdfs --verbose
```

---

## ❓ 常见问题

### Q: PocketBase 是什么？为什么需要它？
A: **PocketBase** 是一个嵌入式 Go 后端，内建 SQLite 数据库 + 文件存储 + RESTful API + 实时订阅。本项目用它来：
- 存储**任务状态**和**处理进度**
- 存储**上传的 PDF 原始文件**和**生成的 CSV 结果**
- 提供**关系型查询**（任务 ↔ 文件的 1:N 关系）
- 支持**断点续查**历史任务记录

它比 PostgreSQL + MinIO + Redis 的组合轻量得多，适合中小规模部署。

### Q: 可以不用 PocketBase 吗？
A: **Web 模式不行**——Flask 层强依赖 PB 做数据持久化和文件存储。但 **CLI 模式**完全不依赖 PB，可以直接 `python pdf_processor.py --input ...` 运行。

### Q: 为什么纯文本 PDF 不要用 MinerU？
A: 纯文本 PDF 本身包含可提取的文本层，用 OCR 反而可能引入错误识别。PyMuPDF 直接读取文本层是 100% 准确的。

### Q: 免费 API 有限制吗？
A: Agent 轻量 API 免 Token 但限制：文件 ≤10MB，页数 ≤20 页。超过此限制需使用精准 API（需要 Token）。

### Q: 处理失败怎么办？
A: 单个文件失败不影响其他文件。失败的文件在 CSV 中会有 error 列标注原因。可以单独重试。

### Q: PocketBase 数据丢了怎么办？
A: 所有数据都在 `pb_data` 目录下的 SQLite 文件中。**定时备份此目录即可**。Docker 部署时建议挂载命名卷（volume）。

---

## 📝 更新日志

### v2.1.0 (2026-09)
- ✅ 中英双语 README（English + 简体中文）
- ✅ 文档中的环境变量全部实际生效：`PB_URL`、`MAX_CONCURRENT_TASKS`、`POLL_INTERVAL`、`TASK_TIMEOUT`
- ✅ CLI `--workers` 参数生效（控制 MinerU OCR 并发）
- ✅ Web 任务生命周期加固：`cancelled` SSE 事件改为合法 JSON、取消状态视为终态、后台线程状态清理、CSV 下载临时文件唯一化
- ✅ 新增 `.env.example`、`.gitignore`、`CLAUDE.md`

### v2.0.1 (2026-04)
- ✅ Docker 构建加固：固定 Debian Bookworm 基础镜像、PocketBase 下载重试、pip 镜像源回退
- ✅ PocketBase 初始化逻辑抽取为 `web/init_pb.py`（自动创建管理员与集合、重建不完整集合、文件上限 200MB）
- ✅ CSV 输出新增 `filename` 列（源文件名去掉 `.pdf` 后缀）

### v2.0.0 (2026-04)
- ✅ **全新 Web 界面**：Flask + SSE 实时进度推送
- ✅ **引入 PocketBase**：任务持久化、文件存储、历史查询
- ✅ **Docker 一键部署**：镜像内置 PocketBase + Flask
- ✅ **PDF 类型分布可视化**：前端三栏卡片 + 结果徽章
- ✅ **SSE 实时进度**：无刷新更新，支持取消任务
- ✅ **健康检查端点**：`/health` 验证 PB 连通性
- ✅ **Nginx SSE 适配**：提供反向代理参考配置

### v1.0.0 (2025-01)
- ✅ 初始版本发布（CLI 模式）
- ✅ 支持三种 PDF 类型智能检测
- ✅ PyMuPDF + MinerU 双引擎
- ✅ 异步批量处理
- ✅ 详细统计报告

---

## 📄 许可证

MIT License
