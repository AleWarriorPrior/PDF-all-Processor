#!/bin/bash
set -e

# ─── 启动 PocketBase ───
echo "🚀 Starting PocketBase..."
pocketbase serve --http 0.0.0.0:8090 &
PB_PID=$!

# 等待 PocketBase 就绪
echo "⏳ 等待 PocketBase 启动..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:8090/api/health >/dev/null 2>&1; then
        echo "✅ PocketBase 已就绪 (port 8090)"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ PocketBase 启动超时"
        kill $PB_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done

# ─── 初始化管理员账号 & 数据集合 ───
echo ""
echo "━━━ 初始化 PocketBase ━━━"

pocketbase upsert "${PB_ADMIN_EMAIL:-admin@admin.com}" "${PB_ADMIN_PASSWORD:-adminadmin123}" 2>/dev/null \
  && echo "✅ 管理员账号已就绪" || echo "⚠️ 管理员账号可能需要手动设置"

# 获取 token 并检查/创建 Collections
sleep 1
INIT_RESULT=$(curl -s "http://127.0.0.1:8090/api/collections/_superusers/auth-with-password" \
  -H "Content-Type: application/json" \
  -d "{\"identity\":\"${PB_ADMIN_EMAIL:-admin@admin.com}\",\"password\":\"${PB_ADMIN_PASSWORD:-adminadmin123}\"}" 2>/dev/null)

TOKEN=$(echo "$INIT_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))" 2>/dev/null)

if [ -n "$TOKEN" ]; then
    TASKS_CHECK=$(curl -s "http://127.0.0.1:8090/api/collections/tasks" \
      -H "Authorization: Bearer $TOKEN" 2>/dev/null | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    fields = d.get('fields', [])
    names = [f.get('name') for f in fields]
    print('ok' if 'status' in names and 'total_files' in names else 'incomplete')
except: print('no')" 2>/dev/null)

    if [ "$TASKS_CHECK" != "ok" ]; then
        echo "📋 创建/重建 tasks & pdf_files 数据集合..."

        # 清除不完整的旧集合
        if [ "$TASKS_CHECK" = "incomplete" ]; then
            OLD_ID=$(curl -s "http://127.0.0.1:8090/api/collections/tasks" \
              -H "Authorization: Bearer $TOKEN" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")
            curl -s -X DELETE "http://127.0.0.1:8090/api/collections/$OLD_ID" \
              -H "Authorization: Bearer $TOKEN" > /dev/null 2>&1
            curl -s -X DELETE "http://127.0.0.1:8090/api/collections/pbc_4085902107" \
              -H "Authorization: Bearer $TOKEN" > /dev/null 2>&1
            echo "  🗑 已清除不完整的旧集合"
        fi

        # 创建 tasks（PB v0.36+ 使用 fields 参数）
        curl -s "http://127.0.0.1:8090/api/collections" \
          -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
          -d '{"name":"tasks","type":"base","fields":[
            {"name":"status","type":"text","required":true},
            {"name":"total_files","type":"number","required":true,"onlyInt":true,"min":0},
            {"name":"processed_files","type":"number","required":false,"onlyInt":true,"min":0},
            {"name":"success_count","type":"number","required":false,"onlyInt":true,"min":0},
            {"name":"failed_count","type":"number","required":false,"onlyInt":true,"min":0},
            {"name":"current_filename","type":"text"},
            {"name":"error_message","type":"text"},
            {"name":"result_csv","type":"file"}
          ]}' > /dev/null 2>&1

        TASKS_ID=$(curl -s "http://127.0.0.1:8090/api/collections/tasks" \
          -H "Authorization: Bearer $TOKEN" 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")

        # 创建 pdf_files
        curl -s "http://127.0.0.1:8090/api/collections" \
          -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
          -d "{\"name\":\"pdf_files\",\"type\":\"base\",\"fields\":[
            {\"name\":\"task\",\"type\":\"relation\",\"required\":true,\"collectionId\":\"$TASKS_ID\",\"maxSelect\":1,\"cascadeDelete\":true},
            {\"name\":\"filename\",\"type\":\"text\",\"required\":true},
            {\"name\":\"status\",\"type\":\"text\"},
            {\"name\":\"pdf_type\",\"type\":\"text\"},
            {\"name\":\"content\",\"type\":\"editor\"},
            {\"name\":\"error_message\",\"type\":\"text\"},
            {\"name\":\"pdf_file\",\"type\":\"file\"}
          ]}' > /dev/null 2>&1

        echo "✅ 数据集合创建完成"
    else
        echo "✅ 数据集合已就绪"
    fi
else
    echo "⚠️ 无法自动初始化 Collection"
fi

# ─── 启动 Flask Web 服务 ───
echo ""
echo "🚀 Starting Flask on port ${FLASK_PORT:-5000}..."
cd /app
FLASK_PORT="${FLASK_PORT:-5000}" python -c "
import sys, os
sys.path.insert(0, '.')
os.chdir('.')
from web.app import app
app.run(host='0.0.0.0', port=int(os.getenv('FLASK_PORT', '5000')), threaded=True)
:" &

# 等待 Flask（前台阻塞）
FLASK_PID=$!
wait $FLASK_PID

# 清理：Flask 退出后关闭 PocketBase
echo "🛑 Shutting down..."
kill $PB_PID 2>/dev/null || true
wait $PB_PID 2>/dev/null || true
