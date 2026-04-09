# 📄 PDF Text Extractor — 智能批量 PDF 文本提取工具

## 🎯 项目简介

一个智能的 PDF 批量处理工具（Web 版），能够：

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
│                    Flask Web 服务 (端口 5000)                     │
│                                                                  │
│   ┌──────────────┐  ┌──────────────┐  ┌────────────────────┐    │
│   │  文件上传 API │  │ SSE 实时推送  │  │  CSV 下载 / 取消    │    │
│   └──────┬───────┘  └──────┬───────┘  └────────▲───────────┘    │
│          │                 │                     │               │
│          └────────┬────────┘                     │               │
│                   ▼                              │               │
│          ┌─────────────────────┐                │               │
│          │  后台处理线程池       │────────────────┘               │
│          │  类型检测 → 提取 → CSV │                               │
│          └──────────┬──────────┘                                │
└─────────────────────│───────────────────────────────────────────┘
                      │ 读写
                      ▼
┌──────────────────────────────────────────────────────────────────┐
│              PocketBase (端口 8090) — 数据 & 文件存储             │
│                                                                  │
│   ┌─────────────────────┐      ┌──────────────────────────┐     │
│   │  tasks 集合         │ 1∶N │  pdf_files 集合           │     │
│   │  · status           │─────│· task (relation)          │     │
│   │  · total_files      │      │· pdf_file (文件字段)       │     │
│   │  · result_csv (文件) │      │· filename / content       │     │
│   │  · progress ...     │      │· pdf_type                 │     │
│   └─────────────────────┘      └──────────────────────────┘     │
└──────────────────────────────────────────────────────────────────┘
                      │ (仅扫描件/混合型)
                      ▼
┌──────────────────────────────────────┐
│        MinerU API (外部服务)          │
│   AI/OCR 公式识别 / 表格识别         │
└──────────────────────────────────────┘
```

### 核心模块

| 模块 | 文件 | 职责 |
|------|------|------|
| Web 入口 | `web/app.py` | Flask 路由、SSE 推送、后台任务编排 |
| PB 客户端 | `web/pb_client.py` | PocketBase 认证、CRUD、文件上传下载 |
| 前端页面 | `web/templates/index.html` | 上传界面 + 实时进度 + 类型分布展示 |
| 样式表 | `web/static/style.css` | 响应式布局、类型统计卡片样式 |
| 类型检测 | `pdf_detector.py` | 分析每页文本/图像占比，判断 PDF 类型 |
| 文本提取 | `pdf_processor.py` | PyMuPDF 本地提取 + MinerU OCR 调度 |
| OCR 客户端 | `mineru_client.py` | MinerU 异步 API 封装 |

---

## 🚀 快速开始

### 方式一：Docker 一键部署（推荐生产环境）

> Docker 镜像内置 PocketBase + Flask，启动即可使用。

```bash
# 1. 克隆项目
git clone https://github.com/AleWarriorPrior/PDF-Text-Extractor.git
cd PDF-Text-Extractor

# 2. 构建镜像（包含 PocketBase v0.36.x）
docker build -t pdf-text-extractor .

# 3. 运行容器
docker run -d \
  --name pdf-extractor \
  -p 5000:5000 \
  -p 8090:8090 \
  -e MINERU_API_TOKEN=your_token_here \
  -v pdf-extractor-data:/pb_data \
  pdf-text-extractor

# 4. 打开浏览器访问
open http://localhost:5000
```

**端口说明：**

| 端口 | 服务 | 用途 |
|------|------|------|
| **5000** | Flask | Web 前端 + API |
| **8090** | PocketBase | 数据库 + 文件存储 + 管理后台 |

> 💡 PocketBase 管理后台：`http://<host>:8090/_/` （默认账号 `admin@admin.com` / `adminadmin123`）

### 平台兼容性

| 宿主机系统 | 是否支持 | 说明 |
|-----------|---------|------|
| **Ubuntu / Debian** | ✅ | 原生支持，`docker build && docker run` 直接运行 |
| **CentOS / RHEL / Fedora** | ✅ | 同上，容器内为 Debian，不受主机发行版影响 |
| **macOS** (Intel / Apple Silicon) | ✅ | 需要 [Docker Desktop](https://www.docker.com/products/docker-desktop/) |
| **Windows 10/11** | ✅ | 需要安装 [Docker Desktop (WSL2)](https://docs.docker.com/desktop/setup/install/windows-install/) |

> **Windows 用户注意：** 项目已配置 `.gitattributes` 强制 LF 换行，`git clone` 后直接构建即可。如果遇到脚本执行错误（`\r: No such file`），请确保使用 Git Bash 或 WSL2 终端执行 docker 命令。

### 方式二：本地开发运行

#### 第一步：启动 PocketBase

PocketBase 是本项目的**必需依赖**——用于存储任务记录、上传的 PDF 文件和生成的 CSV 结果。

```bash
# 下载 PocketBase（根据你的系统选择）

# macOS (Apple Silicon):
curl -fsSL https://github.com/pocketbase/pocketbase/releases/download/v0.36.8/pocketbase_0.36.8_darwin_arm64.zip -o pb.zip

# macOS (Intel):
curl -fsSL https://github.com/pocketbase/pocketbase/releases/download/v0.36.8/pocketbase_0.36.8_darwin_amd64.zip -o pb.zip

# Linux (x86_64):
curl -fsSL https://github.com/pocketbase/pocketbase/releases/download/v0.36.8/pocketbase_0.36.8_linux_amd64.zip -o pb.zip

# 解压并启动
unzip pb.zip && ./pocketbase serve
```

> 启动后 PocketBase 运行在 `http://127.0.0.1:8090`
>
> 首次启动会引导创建管理员账号。建议使用默认：
> - 邮箱：`admin@admin.com`
> - 密码：`adminadmin123`

#### 第二步：初始化数据集合

PocketBase 首次启动后需要创建两个数据集合（`tasks` 和 `pdf_files`）。

**方式 A：通过管理后台手动创建**
1. 打开 `http://127.0.0.1:8090/_/` 登录
2. 左侧菜单 → **Collections** → 创建以下两个集合：

**tasks 集合字段：**

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| status | text | ✅ | 任务状态：pending / processing / completed / failed |
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

> ⚠️ **注意**：创建 `task` relation 字段时，必须指定目标 collectionId 为 `tasks` 集合的实际 ID。

**方式 B：Docker 部署时自动初始化**

Docker entrypoint 脚本 (`docker-entrypoint.sh`) 会在容器首次启动时自动完成：
1. 管理员账号创建（或 upsert）
2. `tasks` 和 `pdf_files` 数据集合的创建与字段校验
3. 不完整旧集合的清理与重建

无需手动操作。

#### 第三步：配置并启动 Flask

```bash
# 1. 克隆项目
git clone https://github.com/AleWarriorPrior/PDF-Text-Extractor.git
cd PDF-Text-Extractor

# 2. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Linux/Mac

# 3. 安装依赖
pip install -r requirements.txt

# 4. 配置环境变量（可选）
cp .env.example .env
# 编辑 .env，填入你的 MinerU Token（扫描件 OCR 需要）

# 5. 启动 Flask
export FLASK_PORT=5002        # 默认 5000，被占用时可改
python web/app.py

# 或使用 nohup 后台运行
# nohup env FLASK_PORT=5002 python web/app.py &
```

启动成功后会看到：

```
============================================================
  📄 PDF Processor Web Service
============================================================
  URL:     http://127.0.0.1:5002
  Debug:   False
  PocketBase: http://127.0.0.1:8090
============================================================
```

浏览器打开对应地址即可使用。

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

### 输出示例（CSV 格式）

```csv
unique_id,source_filename,content,pdf_type
"550e8400-e29b...","合同_2024.pdf","采购合同\n甲方：XX公司\n...",text_only
"a1b2c3d4-e5f6...","发票_扫描件.pdf","发票号码: 12345678\n金额: ¥10,000",scan_only
```

---

## 🔧 配置说明

### 环境变量 (.env)

| 变量名 | 说明 | 默认值 | 是否必填 |
|--------|------|--------|----------|
| **PB_URL** | PocketBase 服务地址 | `http://127.0.0.1:8090` | 推荐 |
| **PB_ADMIN_EMAIL** | PB 管理员邮箱 | `admin@admin.com` | PB 默认 |
| **PB_ADMIN_PASSWORD** | PB 管理员密码 | `adminadmin123` | PB 默认 |
| `MINERU_API_TOKEN` | MinerU API Token | 无 | 可选（免费 API 无需 Token） |
| `FLASK_PORT` | Flask 监听端口 | `5000` | 可选 |
| `FLASK_DEBUG` | Flask 调试模式 | `0` | 可选 |
| `MAX_CONCURRENT_TASKS` | 并发任务数 | `3` | 可选 |
| `POLL_INTERVAL` | API 轮询间隔(秒) | `2` | 可选 |
| `TASK_TIMEOUT` | 单任务超时时间(秒) | `300` | 可选 |

### 获取 MinerU Token

1. 访问 [MinerU 官网](https://mineru.net/)
2. 注册账号并登录
3. 进入 [API 管理页面](https://mineru.net/apiManage)
4. 创建/复制你的 API Token

> **注意**: 免费版 Agent API 无需 Token，但限制文件大小 ≤10MB 且 ≤20 页。
> 如果需要处理更大文件或更高并发，请申请 Token。

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

---

## 📂 项目结构

```
pdf-processor-all/
├── web/
│   ├── app.py                  # Flask Web 入口（路由/SSE/后台任务）
│   ├── pb_client.py            # PocketBase 客户端封装
│   ├── templates/
│   │   └── index.html          # 前端单页（上传+进度+结果）
│   └── static/
│       └── style.css           # 样式表（响应式布局）
├── pdf_processor.py            # 核心处理逻辑（PyMuPDF + MinerU）
├── pdf_detector.py             # PDF 类型智能检测模块
├── mineru_client.py            # MinerU 异步 OCR API 客户端
├── requirements.txt            # Python 依赖清单
├── Dockerfile                  # Docker 构建文件（含 PB + Flask）
├── docker-entrypoint.sh        # 容器启动脚本（初始化 PB + 集合）
├── .env.example                # 环境变量模板
└── README.md                   # 本文件
```

---

## 🚀 部署到生产环境

### Docker Compose 部署（推荐）

```yaml
# docker-compose.yml
version: "3.8"
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

1. **控制并发数**：建议 2-5 个并发，避免触发 MinerU API 限频
2. **大文件优先**：先处理大的扫描件，小文件后处理
3. **PB 文件大小限制**：默认 5MB，本项目已调整为 200MB（通过 PATCH 单独设置 maxSize）
4. **内存监控**：大量 PDF 同时处理会占用较多内存
5. **磁盘空间**：PB 会存储原始 PDF 和 CSV，定期清理已完成的历史任务

---

## CLI 使用指南（无 Web 界面）

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

### Q: 如何集成到 ElasticSearch？
A: CSV 输出后可以用 Logstash、Filebeat 或自定义脚本导入 ES。后续可以考虑增加直连 ES 的功能。

### Q: PocketBase 数据丢了怎么办？
A: 所有数据都在 `pb_data` 目录下的 SQLite 文件中。**定时备份此目录即可**。Docker 部署时建议挂载命名卷（volume）。

---

## 📝 更新日志

### v2.0.0 (2025-04)
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

## 👥 开发团队

由资深开发工程师设计并实现，专注于代码质量和生产可用性。

## 📄 许可证

MIT License
