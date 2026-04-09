"""
PDF Processor Web 应用

基于 Flask + PocketBase 的 Web 端 PDF 批量处理服务。

功能：
1. 浏览器端批量上传 PDF 文件
2. 后台异步处理（类型检测 → 文本提取/OCR）
3. 实时进度展示（SSE 推送）
4. 处理完成后下载 CSV 结果

架构：
  浏览器 ←→ Flask (Web层) ←→ PocketBase (数据+文件存储)
                    ↓
              pdf_processor.py (核心逻辑)

运行方式：
    python web/app.py
    或使用 docker-compose up
"""

import os
import sys
import json
import uuid
import shutil
import threading
import tempfile
from pathlib import Path
from datetime import datetime

from flask import (
    Flask, render_template, request, jsonify,
    send_file, Response
)
from werkzeug.utils import secure_filename
import pandas as pd
from loguru import logger

# ─── 将项目根目录加入 sys.path 以便导入核心模块 ───
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 核心处理模块（CLI 版本的复用）
from pdf_detector import batch_detect_pdfs, PDFType
from pdf_processor import PDFProcessor, _load_env_file
from mineru_client import MinerUClient

# PocketBase 客户端
import importlib.util
_pb_path = Path(__file__).parent / "pb_client.py"
_pb_spec = importlib.util.spec_from_file_location("pb_client", _pb_path)
_pb_mod = importlib.util.module_from_spec(_pb_spec)
_pb_spec.loader.exec_module(_pb_mod)
PocketBaseClient = _pb_mod.PocketBaseClient

# ─── Flask 应用配置 ───
app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent / "templates"),
    static_folder=str(Path(__file__).parent / "static"),
    static_url_path="/static",
)
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024   # 单文件最大 200MB
app.config["UPLOAD_FOLDER"] = PROJECT_ROOT / "web" / "uploads"

# 确保 uploads 目录存在
app.config["UPLOAD_FOLDER"].mkdir(parents=True, exist_ok=True)

# 允许的文件扩展名
ALLOWED_EXTENSIONS = {".pdf"}

# ─── 全局状态：运行中的任务线程 ───
_active_tasks: Dict[str, threading.Thread] = {}
_task_cancel_flags: Dict[str, threading.Event] = {}
_task_type_stats: Dict[str, Dict[str, int]] = {}  # {task_id: {"text_only": N, "scan_only": N, "mixed": N}}


# ══════════════════════════════════
# 全局错误处理 —— 确保所有错误返回 JSON
# ══════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "接口不存在"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error(f"服务器 500 错误: {e}")
    return jsonify({"error": "内部服务器错误", "detail": str(e)}), 500

@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "文件过大，单文件最大支持 200MB"}), 413


def _load_env():
    """加载 .env 文件"""
    _load_env_file(str(PROJECT_ROOT / ".env"))


def allowed_file(filename: str) -> bool:
    """检查文件扩展名是否合法"""
    return Path(filename).suffix.lower() in ALLOWED_EXTENSIONS


# ════════════════════════════════════════════
# 页面路由
#════════════════════════════════════════════

@app.route("/")
def index():
    """主页——上传界面"""
    return render_template("index.html")


@app.route("/health")
def health():
    """健康检查端点（用于 Docker / 负载均衡）"""
    try:
        # 快速检查 PB 连接性
        pb = PocketBaseClient()
        if pb.authenticate():
            return jsonify({"status": "ok", "pocketbase": "connected"}), 200
        else:
            return jsonify({"status": "degraded", "pocketbase": "auth_failed"}), 503
    except Exception as e:
        return jsonify({"status": "degraded", "error": str(e)}), 503


# ════════════════════════════════════════════
# API 路由
#════════════════════════════════════════════

@app.route("/api/tasks", methods=["POST"])
def create_task():
    """
    创建新的处理任务

    前端应先调用此接口获取 task_id，
    再逐个或批量上传 PDF 文件到 /api/tasks/<id>/upload。
    也可直接通过 multipart 上传文件并一步创建任务。
    """
    try:
        _load_env()
        pb = PocketBaseClient()

        # 检查是否有上传的文件
        if "files" not in request.files:
            return jsonify({"error": "没有上传文件，请选择 PDF 文件"}), 400

        files = request.files.getlist("files")
        if not files or all(f.filename == "" for f in files):
            return jsonify({"error": "没有上传文件，请选择 PDF 文件"}), 400

        # 过滤有效文件
        valid_files = [f for f in files if f.filename and allowed_file(f.filename)]
        if not valid_files:
            return jsonify({"error": "只支持 .pdf 格式文件"}), 400

            # 在 PocketBase 中创建任务记录
        task_record = pb.create_task(total_files=len(valid_files))
        task_id = task_record["id"]
        logger.info(f"📋 任务记录创建成功: task_id={task_id}")

        # 保存上传的文件到本地临时目录
        upload_dir = app.config["UPLOAD_FOLDER"] / task_id
        upload_dir.mkdir(parents=True, exist_ok=True)

        saved_paths = []
        for idx, f in enumerate(valid_files, 1):
            # 使用原始文件名（保留中文），仅做安全清理
            original_name = Path(f.filename).name
            # 安全处理：去掉路径穿越字符、保留中文和常见合法字符
            safe_name = original_name.replace("/", "_").replace("\\", "_").replace("..", "")
            safe_name = safe_name.lstrip(".")
            if not safe_name:
                safe_name = f"unnamed_{uuid.uuid4().hex[:8]}.pdf"

            filename = safe_name
            filepath = upload_dir / filename
            # 处理同名文件
            counter = 1
            while filepath.exists():
                stem = Path(filename).stem
                suffix = Path(filename).suffix
                filepath = upload_dir / f"{stem}_{counter}{suffix}"
                counter += 1

            f.save(str(filepath))
            saved_paths.append(str(filepath))

            # 在 PB 中创建 pdf_file 记录并上传文件
            try:
                logger.info(f"📎 [{idx}/{len(valid_files)}] 上传到 PB: {filename}")
                pb.attach_pdf_file(task_id, str(filepath))
            except Exception as upload_err:
                logger.error(f"❌ 文件 [{filename}] 上传到 PB 失败: {upload_err}", exc_info=True)
                # 单个文件失败不阻断整个任务（文件已在本地，后续处理仍可进行）
                # 如果是 task 不存在这种致命错误，则直接中断
                err_msg = str(upload_err)
                if "404" in err_msg or "not found" in err_msg.lower() or "validation_missing" in err_msg:
                    raise RuntimeError(
                        f"关键错误 — 任务记录可能已丢失 (task_id={task_id}): {err_msg}"
                    ) from upload_err

        logger.info(f"📋 任务创建成功 [{task_id}]，{len(saved_paths)} 个文件")

        # 启动后台处理线程
        cancel_event = threading.Event()
        _task_cancel_flags[task_id] = cancel_event

        worker = threading.Thread(
            target=_process_task_background,
            args=(task_id, saved_paths),
            daemon=True,
        )
        worker.start()
        _active_tasks[task_id] = worker

        return jsonify({
            "task_id": task_id,
            "file_count": len(saved_paths),
            "status": "pending",
            "message": "任务已创建，正在后台处理",
        }), 202

    except Exception as e:
        logger.error(f"❌ 创建任务失败: {e}", exc_info=True)
        return jsonify({"error": f"创建任务失败: {str(e)}"}), 500


@app.route("/api/tasks/<task_id>/status")
def task_status(task_id: str):
    """查询任务当前状态和进度"""
    pb = PocketBaseClient()
    try:
        task = pb.get_record("tasks", task_id)
        type_stats = _task_type_stats.get(task_id, {})
        return jsonify({
            "task_id": task_id,
            "status": task.get("status", "unknown"),
            "total_files": task.get("total_files", 0),
            "processed_files": task.get("processed_files", 0),
            "success_count": task.get("success_count", 0),
            "failed_count": task.get("failed_count", 0),
            "current_filename": task.get("current_filename", ""),
            "error_message": task.get("error_message", ""),
            "created": task.get("created", ""),
            "updated": task.get("updated", ""),
            **type_stats,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<task_id>/events")
def task_events(task_id: str):
    """
    Server-Sent Events (SSE) 流 —— 实时推送任务进度

    前端使用 EventSource 监听此端点，实现无刷新进度更新。
    """
    def generate():
        pb = PocketBaseClient()
        last_status = None
        last_processed = -1

        while True:
            try:
                task = pb.get_record("tasks", task_id)
                current_status = task.get("status", "")
                processed = task.get("processed_files", 0)

                # 只在数据变化时推送
                type_stats = _task_type_stats.get(task_id, {})
                data = {
                    "task_id": task_id,
                    "status": current_status,
                    "total_files": task.get("total_files", 0),
                    "processed_files": processed,
                    "success_count": task.get("success_count", 0),
                    "failed_count": task.get("failed_count", 0),
                    "current_filename": task.get("current_filename", ""),
                    "error_message": task.get("error_message", ""),
                    **type_stats,  # text_only / scan_only / mixed
                }

                # 状态变化或进度变化时都推送
                if (current_status != last_status or processed != last_processed
                        or current_status == "processing"):
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                    last_status = current_status
                    last_processed = processed

                # 任务结束（完成或失败）则关闭流
                if current_status in ("completed", "failed"):
                    yield f"event: done\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
                    break

                # 检查是否被取消
                if task_id in _task_cancel_flags and _task_cancel_flags[task_id].is_set():
                    yield f"event: cancelled\ndata: {{'task_id': '{task_id}', 'status': 'cancelled'}}\n\n"
                    break

            except Exception as e:
                yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                break

            # SSE 轮询间隔：处理中每 2 秒刷新一次
            import time
            time.sleep(2)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 禁用 Nginx 缓冲
            "Connection": "keep-alive",
        },
    )


@app.route("/api/tasks/<task_id>/download")
def download_result(task_id: str):
    """
    下载任务的 CSV 处理结果

    从 PocketBase 获取 result_csv 字段并返回给浏览器下载。
    """
    pb = PocketBaseClient()
    try:
        task = pb.get_record("tasks", task_id)
        status = task.get("status", "")

        if status != "completed":
            return jsonify({"error": f"任务尚未完成 (当前状态: {status})"}), 400

        # 检查是否有结果文件
        result_csv_field = task.get("result_csv", "")
        if not result_csv_field:
            # 尝试从本地读取（兜底）
            local_csv = app.config["UPLOAD_FOLDER"] / task_id / "result.csv"
            if local_csv.exists():
                return send_file(
                    str(local_csv),
                    as_attachment=True,
                    download_name=f"pdf_extract_{task_id}.csv",
                    mimetype="text/csv",
                )
            return jsonify({"error": "暂无可下载的结果文件"}), 404

        # PB 的 file 字段可能返回字符串（文件名）或列表
        # 先尝试直接用字符串作为 filename 构建下载 URL
        if isinstance(result_csv_field, str):
            csv_filename = result_csv_field.strip()
        elif isinstance(result_csv_field, list) and len(result_csv_field) > 0:
            item = result_csv_field[0]
            csv_filename = item.get("filename", "") if isinstance(item, dict) else str(item)
        elif isinstance(result_csv_field, dict):
            csv_filename = result_csv_field.get("filename", "")
        else:
            csv_filename = ""

        if not csv_filename:
            # 兜底：从本地读取
            local_csv = app.config["UPLOAD_FOLDER"] / task_id / "result.csv"
            if local_csv.exists():
                return send_file(
                    str(local_csv),
                    as_attachment=True,
                    download_name=f"pdf_extract_{task_id}.csv",
                    mimetype="text/csv",
                )
            return jsonify({"error": "结果文件名解析失败"}), 500

        # 从 PocketBase 下载文件
        try:
            file_url = pb.get_file_url("tasks", task_id, "result_csv", csv_filename)
            csv_content = pb.download_file(file_url)

            # 写入临时文件以便 send_file 使用
            tmp_path = Path(tempfile.gettempdir()) / f"pb_download_{task_id}.csv"
            tmp_path.write_bytes(csv_content)

            return send_file(
                str(tmp_path),
                as_attachment=True,
                download_name=f"pdf_extract_{task_id[:8]}.csv",
                mimetype="text/csv",
            )
        except Exception as e:
            logger.error(f"从 PB 下载 CSV 失败 [{task_id}]: {e}")
            # 最终兜底：读本地
            local_csv = app.config["UPLOAD_FOLDER"] / task_id / "result.csv"
            if local_csv.exists():
                return send_file(
                    str(local_csv),
                    as_attachment=True,
                    download_name=f"pdf_extract_{task_id}.csv",
                    mimetype="text/csv",
                )
            return jsonify({"error": f"下载失败: {str(e)}"}), 500

    except Exception as e:
        logger.error(f"下载结果失败 [{task_id}]: {e}")
        return jsonify({"error": f"下载失败: {str(e)}"}), 500


@app.route("/api/tasks/<task_id>/cancel", methods=["POST"])
def cancel_task(task_id: str):
    """取消一个正在运行的任务"""
    if task_id in _task_cancel_flags:
        _task_cancel_flags[task_id].set()

        # 更新 PB 状态
        pb = PocketBaseClient()
        pb.update_task_progress(task_id, status="cancelled")

        return jsonify({"message": "任务已请求取消", "task_id": task_id}), 200
    else:
        return jsonify({"error": "任务不存在或已完成"}), 404


# ════════════════════════════════════════════
# 后台处理线程
#════════════════════════════════════════════

def _process_task_background(task_id: str, file_paths: list):
    """
    后台工作线程：执行实际的 PDF 批量处理

    流程：
    1. 更新任务状态为 processing
    2. 对每个 PDF 文件：
       a. 检测类型
       b. 根据 类型 选择提取方式
       c. 记录结果到 PocketBase
    3. 汇总生成 CSV
    4. 上传 CSV 到 PocketBase
    5. 更新任务状态为 completed / failed
    """
    _load_env()
    pb = PocketBaseClient()

    try:
        # ── Step 1: 开始处理 ──
        pb.update_task_progress(task_id, status="processing")

        results = []
        success_count = 0
        failed_count = 0
        total = len(file_paths)

        # 初始化处理器
        processor = PDFProcessor(verbose=False)

        # ── Step 2: 批量检测类型 ──
        detection_results = batch_detect_pdfs(file_paths, verbose=False)

        # ── Step 2.5: 计算并存储类型分布统计 ──
        type_counts = {"text_only": 0, "scan_only": 0, "mixed": 0}
        for r in detection_results:
            t = r.pdf_type.value  # "text_only" | "scan_only" | "mixed"
            type_counts[t] = type_counts.get(t, 0) + 1
        _task_type_stats[task_id] = type_counts

        # 分组
        text_only_files = [r for r in detection_results if r.pdf_type == PDFType.TEXT_ONLY]
        scan_mixed_files = [r for r in detection_results
                            if r.pdf_type in (PDFType.SCAN_ONLY, PDFType.MIXED)]

        # ── Step 3: 逐文件处理 ──
        for idx, result in enumerate(detection_results):
            # 检查是否被取消
            if task_id in _task_cancel_flags and _task_cancel_flags[task_id].is_set():
                logger.info(f"⛔ 任务 [{task_id}] 被用户取消")
                return

            filename = Path(result.file_path).name
            pb.update_task_progress(
                task_id,
                processed_files=idx + 1,
                current_filename=filename,
            )

            try:
                if result.pdf_type == PDFType.TEXT_ONLY:
                    # PyMuPDF 直接提取
                    content = processor._extract_text_with_pymupdf(result.file_path)
                    record = {
                        "unique_id": str(uuid.uuid4()),
                        "source_filename": filename,
                        "content": content,
                        "pdf_type": result.pdf_type.value,
                    }
                    results.append(record)
                    success_count += 1

                else:
                    # MinerU OCR（需要 async）
                    import asyncio

                    async def _run_ocr():
                        client = MinerUClient(api_token=processor.api_token, verbose=False)
                        try:
                            parse_result = await client.parse_file(
                                result.file_path,
                                language="ch",
                                enable_ocr=True,
                                enable_formula=True,
                                enable_table=True,
                            )
                            return parse_result
                        finally:
                            await client.close()

                    ocr_result = asyncio.run(_run_ocr())

                    if ocr_result.success:
                        record = {
                            "unique_id": str(uuid.uuid4()),
                            "source_filename": filename,
                            "content": ocr_result.content,
                            "pdf_type": result.pdf_type.value,
                        }
                        results.append(record)
                        success_count += 1
                    else:
                        record = {
                            "unique_id": str(uuid.uuid4()),
                            "source_filename": filename,
                            "content": "",
                            "pdf_type": result.pdf_type.value,
                            "error": ocr_result.error_message,
                        }
                        results.append(record)
                        failed_count += 1

            except Exception as e:
                logger.error(f"❌ 处理文件失败 [{filename}]: {e}")
                record = {
                    "unique_id": str(uuid.uuid4()),
                    "source_filename": filename,
                    "content": "",
                    "pdf_type": result.pdf_type.value,
                    "error": str(e),
                }
                results.append(record)
                failed_count += 1

            # 更新计数
            pb.update_task_progress(
                task_id,
                success_count=success_count,
                failed_count=failed_count,
            )

        # ── Step 4: 生成 CSV 并上传 ──
        if results:
            df = pd.DataFrame(results)
            csv_path = str(app.config["UPLOAD_FOLDER"] / task_id / "result.csv")
            export_df = df[["unique_id", "source_filename", "content", "pdf_type"]].copy()
            if "error" in df.columns:
                export_df["error"] = df["error"]
            export_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

            # 上传 CSV 到 PocketBase
            try:
                pb.attach_csv_result(task_id, csv_path)
            except Exception as e:
                logger.warning(f"⚠️ CSV 上传到 PocketBase 失败（仍可本地下载）: {e}")

        # ── Step 5: 完成 ──
        pb.update_task_progress(task_id, status="completed")

        logger.info(
            f"✅ 任务 [{task_id}] 完成 — "
            f"总计:{total}, 成功:{success_count}, 失败:{failed_count}"
        )

    except Exception as e:
        logger.exception(f"💥 任务 [{task_id}] 异常终止: {e}")
        try:
            pb.update_task_progress(task_id, status="failed", error_message=str(e)[:500])
        except Exception:
            pass


# ════════════════════════════════════════════
# 应用入口
#════════════════════════════════════════════

if __name__ == "__main__":
    _load_env()
    port = int(os.getenv("FLASK_PORT", "5000"))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"

    print("\n" + "=" * 60)
    print("  📄 PDF Processor Web Service")
    print("=" * 60)
    print(f"  URL:     http://127.0.0.1:{port}")
    print(f"  Debug:   {debug}")
    print(f"  PocketBase: {os.getenv('PB_URL', 'http://127.0.0.1:8090')}")
    print("=" * 60 + "\n")

    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
