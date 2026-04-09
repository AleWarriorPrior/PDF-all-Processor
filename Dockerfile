# 固定使用 Debian Bookworm (12) 基础镜像，避免 Trixie 包名变更导致构建失败
FROM python:3.12-slim-bookworm

LABEL maintainer="PDF Processor Team"
LABEL description="智能PDF批量处理工具 - Web版 (Flask + PocketBase)"

# 安装系统依赖（PyMuPDF 底层 MuPDF 引擎需要这些图形库）
RUN apt-get update && apt-get install -y --no-install-recommends \
    # OpenGL / Mesa 库 — PyMuPDF 渲染引擎依赖
    libgl1-mesa-glx libgl1-mesa-dri \
    # GTK/X11 运行时
    libglib2.0-0 libsm6 libxext6 libxrender-dev \
    # 工具：下载解压 PocketBase 二进制文件
    curl unzip \
    && rm -rf /var/lib/apt/lists/*

# 下载 PocketBase
ENV PB_VERSION=0.36.8
RUN set -e; \
    # 检测架构
    _arch_name="$(uname -m)"; \
    case "$_arch_name" in \
        x86_64|amd64) pb_arch="linux_amd64" ;; \
        aarch64|arm64) pb_arch="linux_arm64" ;; \
        *) echo "Unsupported architecture: $_arch_name"; exit 1 ;; \
    esac; \
    _pb_url="https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}/pocketbase_${PB_VERSION}_${pb_arch}.zip"; \
    echo ">>> Downloading PocketBase v${PB_VERSION} (${pb_arch})..."; \
    # 重试机制：GitHub 在国内/企业网络可能不稳定，最多重试 5 次
    for i in 1 2 3 4 5; do \
        echo ">>> Attempt $i of 5..."; \
        if curl -fSL --connect-timeout 30 --max-time 300 "$_pb_url" -o /tmp/pb.zip; then \
            _dl_ok=1; break; \
        else \
            echo ">>> Download failed, retrying in 5s..."; \
            rm -f /tmp/pb.zip; sleep 5; \
        fi; \
    done; \
    if [ "$_dl_ok" != "1" ]; then \
        echo "ERROR: Failed to download PocketBase after 5 attempts"; \
        exit 1; \
    fi; \
    # 验证下载文件不是空文件也不是 HTML 错误页
    _fsize=$(stat -c%s /tmp/pb.zip 2>/dev/null || echo 0); \
    if [ "$_fsize" -lt 10000 ]; then \
        echo "ERROR: Downloaded file too small ($_fsize bytes), likely not a valid ZIP"; \
        exit 1; \
    fi; \
    # 解压到目标目录（使用 -d 而不是 -o 避免参数歧义）
    mkdir -p /usr/local/bin; \
    unzip -o /tmp/pb.zip -d /tmp/pb_extract && \
    mv /tmp/pb_extract/pocketbase /usr/local/bin/pocketbase || \
    cp /tmp/pb_extract/*/pocketbase /usr/local/bin/pocketbase 2>/dev/null || true; \
    chmod +x /usr/local/bin/pocketbase && \
    rm -rf /tmp/pb.zip /tmp/pb_extract && \
    echo ">>> PocketBase installed successfully:" && \
    /usr/local/bin/pocketbase --version

WORKDIR /app

# 先复制依赖文件（利用Docker缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 复制核心处理模块
COPY pdf_processor.py .
COPY pdf_detector.py .
COPY mineru_client.py .

# 复制 Web 应用
COPY web/ ./web/

# 创建必要目录
RUN mkdir -p /app/web/uploads logs /pb_data

# 环境变量
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Shanghai
ENV PB_URL=http://127.0.0.1:8090
ENV FLASK_PORT=5000
ENV FLASK_DEBUG=0

# 启动脚本：同时运行 PocketBase + Flask
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

EXPOSE 5000 8090

ENTRYPOINT ["/docker-entrypoint.sh"]
