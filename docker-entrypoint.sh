#!/bin/bash
set -e

# ─── 启动 PocketBase ───
echo "🚀 Starting PocketBase..."
pocketbase serve --http 0.0.0.0:8090 &
PB_PID=$!

# ─── 初始化 PB（管理员 + Collections） ───
# 全部逻辑在 Python 脚本里，避免 shell 内联 Python 的引号/换行解析问题
echo "━━━ 初始化 PocketBase ━━━"
python3 /app/web/init_pb.py

# ─── 启动 Flask Web 服务 ───
echo ""
echo "🚀 Starting Flask on port ${FLASK_PORT:-5000}..."
cd /app
python /app/web/run_flask.py &

# 等待 Flask 退出（前台阻塞）
FLASK_PID=$!
wait $FLASK_PID

# 清理：Flask 退出后关闭 PocketBase
echo "🛑 Shutting down..."
kill $PB_PID 2>/dev/null || true
wait $PB_PID 2>/dev/null || true
