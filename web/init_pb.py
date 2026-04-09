#!/usr/bin/env python3
"""
PocketBase 自动初始化脚本（容器启动时由 docker-entrypoint.sh 调用）
功能：创建管理员账号、检查/创建 tasks & pdf_files 数据集合
"""
import sys
import json
import time
import urllib.request
import urllib.error

PB_URL = "http://127.0.0.1:8090"
DEFAULT_EMAIL = "admin@admin.com"
DEFAULT_PASS = "adminadmin123"


def api_request(path, method="GET", data=None, token=None, silent=False):
    """发送 HTTP 请求到 PocketBase API"""
    url = f"{PB_URL}{path}"
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        if not silent:
            print(f"  ⚠️ API {method} {path} → HTTP {e.code}: {err_body[:200]}")
        return json.loads(err_body) if err_body else {}
    except Exception as e:
        if not silent:
            print(f"  ❌ API request failed: {e}")
        return None


def get_token(email, password):
    """获取管理员认证 token"""
    result = api_request(
        "/api/collections/_superusers/auth-with-password",
        method="POST",
        data={"identity": email, "password": password}
    )
    return result.get("token", "") if result else ""


def check_collection(token, name):
    """
    检查 collection 是否存在且结构完整。
    返回: 'ok' | 'incomplete' | 'missing'
    """
    resp = api_request(f"/api/collections/{name}", token=token, silent=True)
    if not resp or "error" in resp:
        return "missing"
    fields = [f.get("name") for f in resp.get("fields", [])]

    # 检查关键字段是否存在
    if name == "tasks":
        required = {"status", "total_files"}
        return "ok" if required.issubset(set(fields)) else "incomplete"
    elif name == "pdf_files":
        required = {"filename", "task"}
        return "ok" if required.issubset(set(fields)) else "incomplete"
    return "ok"


def create_tasks_collection(token):
    """创建 tasks 数据集合"""
    print("  >>> Creating 'tasks' collection...")
    payload = {
        "name": "tasks",
        "type": "base",
        "fields": [
            {"name": "status", "type": "text", "required": True},
            {"name": "total_files", "type": "number", "required": True, "onlyInt": True, "min": 0},
            {"name": "processed_files", "type": "number", "required": False, "onlyInt": True, "min": 0},
            {"name": "success_count", "type": "number", "required": False, "onlyInt": True, "min": 0},
            {"name": "failed_count", "type": "number", "required": False, "onlyInt": True, "min": 0},
            {"name": "current_filename", "type": "text"},
            {"name": "error_message", "type": "text"},
            {"name": "result_csv", "type": "file"},
        ],
    }
    resp = api_request("/api/collections", method="POST", data=payload, token=token)
    if not resp or "error" in resp:
        print(f"  ❌ Failed to create tasks: {resp}")
        sys.exit(1)

    task_id = resp.get("id") or resp.get("Id", "")
    if not task_id:
        print(f"  ❌ No ID in create response: {resp}")
        sys.exit(1)

    print(f"  ✅ tasks created (ID: {task_id})")
    return task_id


def create_pdf_files_collection(token, tasks_id):
    """创建 pdf_files 数据集合"""
    print("  >>> Creating 'pdf_files' collection...")
    payload = {
        "name": "pdf_files",
        "type": "base",
        "fields": [
            {
                "name": "task",
                "type": "relation",
                "required": True,
                "collectionId": tasks_id,
                "maxSelect": 1,
                "cascadeDelete": True,
            },
            {"name": "filename", "type": "text", "required": True},
            {"name": "status", "type": "text"},
            {"name": "pdf_type", "type": "text"},
            {"name": "content", "type": "editor"},
            {"name": "error_message", "type": "text"},
            {"name": "pdf_file", "type": "file"},
        ],
    }
    resp = api_request("/api/collections", method="POST", data=payload, token=token)
    if not resp or "error" in resp:
        print(f"  ❌ Failed to create pdf_files: {resp}")
        sys.exit(1)

    print("  ✅ pdf_files created")


def delete_collection(token, coll_id):
    """删除数据集合"""
    api_request(f"/api/collections/{coll_id}", method="DELETE", token=token, silent=True)


def main():
    import os
    import subprocess
    email = os.environ.get("PB_ADMIN_EMAIL", DEFAULT_EMAIL)
    password = os.environ.get("PB_ADMIN_PASSWORD", DEFAULT_PASS)

    # 等待 PB 就绪（最多 30 秒）
    print("⏳ 等待 PocketBase 启动...")
    for i in range(30):
        health = api_request("/api/health", silent=True)
        if health:
            print("✅ PocketBase 已就绪 (port 8090)")
            break
        time.sleep(1)
    else:
        print("❌ PocketBase 启动超时")
        sys.exit(1)

    # 先创建/更新管理员账号（通过子进程调用 pocketbase CLI）
    # 首次启动 PB 时还没有任何管理员，必须先 upsert 才能认证
    print("📋 初始化管理员账号...")
    try:
        result = subprocess.run(
            ["pocketbase", "superuser", "upsert", email, password],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 or "already" in result.stderr.lower() or "successfully" in result.stdout.lower():
            print(f"✅ 管理员账号已就绪 ({email})")
        else:
            print(f"⚠️ upsert: {result.stderr.strip() or result.stdout.strip()}")
    except Exception as e:
        print(f"⚠️ upsert 异常（可能已有账号）: {e}")

    # 获取 token
    time.sleep(1)
    token = get_token(email, password)
    if not token:
        print("⚠️ 无法获取 admin token，跳过 Collection 初始化")
        return

    # 检查现有 collections
    tasks_status = check_collection(token, "tasks")

    if tasks_status == "ok":
        pdf_status = check_collection(token, "pdf_files")
        if pdf_status == "ok":
            print("✅ 数据集合已就绪")
            return

    print("📋 创建/重建 tasks & pdf_files 数据集合...")

    # 如果 tasks 不完整，先删掉旧的
    if tasks_status == "incomplete":
        resp = api_request(f"/api/collections/tasks", token=token, silent=True)
        if resp and resp.get("id"):
            delete_collection(token, resp["id"])
            print("  🗑 已清除不完整的 tasks 集合")

    # 创建 tasks
    tasks_id = create_tasks_collection(token)

    # 创建 pdf_files
    create_pdf_files_collection(token, tasks_id)

    print("✅ 数据集合初始化完成")


if __name__ == "__main__":
    import os
    main()
