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
    # 检测平台和架构（Docker 容器内 uname 行为可能因 shell 而异）
    _os_name="$(uname -s)"; \
    _arch_name="$(uname -m)"; \
    case "$_os_name" in \
        Linux)   pb_os="linux" ;; \
        Darwin)  pb_os="darwin" ;; \
        *)       echo "Unsupported OS: $_os_name"; exit 1 ;; \
    esac; \
    case "$_arch_name" in \
        x86_64|amd64) pb_arch="${pb_os}_amd64" ;; \
        aarch64|arm64) pb_arch="${pb_os}_arm64" ;; \
        *) echo "Unsupported architecture: $_arch_name"; exit 1 ;; \
    esac; \
    echo "Downloading PocketBase v${PB_VERSION} for ${pb_arch}..."; \
    curl -fsSL "https://github.com/pocketbase/pocketbase/releases/download/v${PB_VERSION}/pocketbase_${PB_VERSION}_${pb_arch}.zip" \
         -o /tmp/pb.zip && \
    unzip /tmp/pb.zip -o /usr/local/bin/ && \
    chmod +x /usr/local/bin/pocketbase && \
    rm /tmp/pb.zip && \
    echo "PocketBase installed: $(/usr/local/bin/pocketbase --version)"

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
