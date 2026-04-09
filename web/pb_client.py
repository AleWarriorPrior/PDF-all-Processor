"""
PocketBase HTTP API 客户端封装

提供对 PocketBase REST API 的简洁 Python 封装，用于：
- 任务记录 CRUD
- PDF 文件上传与管理
- 处理结果存储与查询

PocketBase API 文档: https://pocketbase.io/docs/go-api/
REST API 用法参考: https://pocketbase.io/docs/api-overview/
"""

import os
import json
import requests
from typing import Optional, Dict, Any, List, IO
from pathlib import Path
from loguru import logger


class PocketBaseClient:
    """
    PocketBase REST API 客户端

    设计原则：
    1. 轻量 —— 使用 requests 库，不引入重量级 SDK
    2. 容错 —— 自动重试 + 详细错误日志
    3. 类型安全 —— 返回 Dict，调用方自行解析
    """

    # 默认配置（可通过环境变量覆盖）
    DEFAULT_URL = "http://127.0.0.1:8090"
    DEFAULT_ADMIN_EMAIL = "admin@admin.com"
    DEFAULT_ADMIN_PASSWORD = "adminadmin123"

    def __init__(self,
                 base_url: Optional[str] = None,
                 email: Optional[str] = None,
                 password: Optional[str] = None):
        self.base_url = (base_url or os.getenv("PB_URL", self.DEFAULT_URL)).rstrip("/")
        self.email = email or os.getenv("PB_ADMIN_EMAIL", self.DEFAULT_ADMIN_EMAIL)
        self.password = password or os.getenv("PB_ADMIN_PASSWORD", self.DEFAULT_ADMIN_PASSWORD)
        self._token: Optional[str] = None
        self._session = requests.Session()

        # 设置请求超时（连接5秒，读取30秒——OCR轮询可能较慢）
        self._timeout = (5, 30)

        logger.info(f"📦 PocketBase 客户端初始化 → {self.base_url}")

    # ─────────────────────────────────────────────
    # 认证管理
    # ─────────────────────────────────────────────

    def authenticate(self) -> bool:
        """使用超级用户账号登录获取 Token（PB 0.36+ 使用新端点）"""
        try:
            # PB 0.36.x 新端点：/api/collections/_superusers/auth-with-password
            resp = self._session.post(
                f"{self.base_url}/api/collections/_superusers/auth-with-password",
                json={
                    "identity": self.email,
                    "password": self.password,
                },
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["token"]
            self._session.headers.update({"Authorization": f"Bearer {self._token}"})
            logger.info("✅ PocketBase 认证成功")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ PocketBase 认证失败: {e}")
            return False

    def _ensure_auth(self):
        """确保已认证，未认证则自动登录"""
        if not self._token:
            if not self.authenticate():
                raise ConnectionError("无法连接到 PocketBase 或认证失败")

    # ─────────────────────────────────────────────
    # 通用 CRUD 方法
    # ─────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """
        发送请求的统一入口

        Args:
            method: HTTP 方法 (GET / POST / PATCH / DELETE)
            path: API 路径（不含 base_url 和 /api 前缀）
            **kwargs: 传递给 requests 的参数

        Returns:
            API 响应的 JSON 字典
        """
        self._ensure_auth()
        url = f"{self.base_url}/api/{path.lstrip('/')}"

        # 合并默认 timeout
        kwargs.setdefault("timeout", self._timeout)

        try:
            resp = self._session.request(method, url, **kwargs)
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = e.response.text[:2000]
            except Exception:
                pass
            logger.error(
                f"PB API 错误 [{method} {path}]: HTTP {e.response.status_code}\n"
                f"  Response: {body}"
            )
            # 将完整响应信息附加到异常中，方便上层调用者获取详情
            if hasattr(e, 'args'):
                e.args = (f"{str(e.args[0]) if e.args else ''} | Body: {body}",)
            raise
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"无法连接到 PocketBase ({self.base_url})。请确认服务已启动。")

    def get_record(self, collection: str, record_id: str) -> Dict[str, Any]:
        """获取单条记录"""
        return self._request("GET", f"/collections/{collection}/records/{record_id}")

    def list_records(self, collection: str,
                     filter_str: Optional[str] = None,
                     sort: str = "-created",
                     page: int = 1,
                     per_page: int = 50) -> Dict[str, Any]:
        """
        查询记录列表

        Args:
            collection: 集合名称（表名）
            filter_str: PocketBase 过滤语法，如 'status="processing"'
            sort: 排序字段
            page: 页码
            per_page: 每页条数
        """
        params = {"sort": sort, "page": page, "perPage": per_page}
        if filter_str:
            params["filter"] = filter_str
        return self._request("GET", f"/collections/{collection}/records", params=params)

    def create_record(self, collection: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """创建记录"""
        return self._request("POST", f"/collections/{collection}/records", json=data)

    def update_record(self, collection: str,
                      record_id: str,
                      data: Dict[str, Any]) -> Dict[str, Any]:
        """更新记录"""
        return self._request("PATCH", f"/collections/{collection}/records/{record_id}", json=data)

    def delete_record(self, collection: str, record_id: str) -> None:
        """删除记录"""
        self._request("DELETE", f"/collections/{collection}/records/{record_id}")

    # ─────────────────────────────────────────────
    # 文件操作
    # ─────────────────────────────────────────────

    def upload_file(self,
                    collection: str,
                    record_id: str,
                    field_name: str,
                    file_path: str) -> Dict[str, Any]:
        """
        上传文件到指定记录的字段

        Args:
            collection: 集合名称
            record_id: 记录 ID
            field_name: 文件字段名（如 'pdf_file'、'result_csv'）
            file_path: 本地文件路径

        Returns:
            更新后的记录数据
        """
        self._ensure_auth()
        url = f"{self.base_url}/api/collections/{collection}/records/{record_id}"

        with open(file_path, "rb") as f:
            # PocketBase 文件上传使用 multipart/form-data + PATCH
            files = {field_name: (Path(file_path).name, f, "application/octet-stream")}
            resp = self._session.patch(url, files=files, timeout=self._timeout)
            resp.raise_for_status()
            return resp.json()

    def upload_file_obj(self,
                        collection: str,
                        record_id: str,
                        field_name: str,
                        file_obj: IO[bytes],
                        filename: str,
                        content_type: str = "application/octet-stream") -> Dict[str, Any]:
        """
        从文件对象上传文件到 PocketBase

        适用场景：从内存中的 BytesIO / tempfile 上传
        """
        self._ensure_auth()
        url = f"{self.base_url}/api/collections/{collection}/records/{record_id}"

        files = {field_name: (filename, file_obj, content_type)}
        resp = self._session.patch(url, files=files, timeout=self._timeout)
        resp.raise_for_status()
        return resp.json()

    def get_file_url(self,
                     collection: str,
                     record_id: str,
                     field_name: str,
                     filename: str) -> str:
        """
        构建文件的下载 URL

        Returns:
           完整的文件下载链接
        """
        return f"{self.base_url}/api/files/{collection}/{record_id}/{filename}"

    def download_file(self, url: str) -> bytes:
        """下载文件内容（返回 bytes）"""
        self._ensure_auth()
        resp = self._session.get(url, timeout=self._timeout)
        resp.raise_for_status()
        return resp.content

    # ─────────────────────────────────────────────
    # 业务便捷方法：任务管理
    # ─────────────────────────────────────────────

    def create_task(self, total_files: int = 0) -> Dict[str, Any]:
        """
        创建一个新的处理任务

        Returns:
            新建的任务记录
        """
        return self.create_record("tasks", {
            "status": "pending",
            "total_files": total_files,
            "processed_files": 0,
            "success_count": 0,
            "failed_count": 0,
            "current_filename": "",
            "error_message": "",
        })

    def update_task_progress(self,
                             task_id: str,
                             *,
                             status: Optional[str] = None,
                             processed_files: Optional[int] = None,
                             current_filename: Optional[str] = None,
                             success_count: Optional[int] = None,
                             failed_count: Optional[int] = None,
                             error_message: Optional[str] = None) -> Dict[str, Any]:
        """
        原子式更新任务进度（只传需要更新的字段）

        这是后台处理线程最常调用的方法。
        """
        data = {}
        if status is not None:
            data["status"] = status
        if processed_files is not None:
            data["processed_files"] = processed_files
        if current_filename is not None:
            data["current_filename"] = current_filename
        if success_count is not None:
            data["success_count"] = success_count
        if failed_count is not None:
            data["failed_count"] = failed_count
        if error_message is not None:
            data["error_message"] = error_message

        if not data:
            return self.get_record("tasks", task_id)

        return self.update_record("tasks", task_id, data)

    def attach_csv_result(self, task_id: str, csv_path: str) -> Dict[str, Any]:
        """将生成的 CSV 结果文件附加到任务记录上"""
        return self.upload_file("tasks", task_id, "result_csv", csv_path)

    def attach_pdf_file(self, task_id: str, file_path: str) -> Dict[str, Any]:
        """
        上传一个 PDF 文件并关联到任务

        分两步执行：
        1. 在 pdf_files 集合中创建记录（含 relation 到 tasks）
        2. 将文件上传到该记录的 pdf_file 字段
        """
        filename = Path(file_path).name
        logger.info(f"📎 attach_pdf_file: task_id={task_id}, file={filename}")

        # Step 1: 创建 pdf_files 记录
        try:
            record = self.create_record("pdf_files", {
                "task": task_id,
                "filename": filename,
                "status": "pending",
                "pdf_type": "",
                "content": "",
                "error_message": "",
            })
            record_id = record.get("id", "")
            logger.info(f"  ✅ pdf_files 记录创建成功: id={record_id}")
        except requests.exceptions.HTTPError as e:
            status = getattr(e.response, 'status_code', '?') if hasattr(e, 'response') else '?'
            body = ""
            if hasattr(e, 'response') and e.response:
                try:
                    body = e.response.text[:1000]
                except Exception:
                    pass
            # 尝试解析PB的具体错误信息
            detail = ""
            if body:
                import json
                try:
                    jd = json.loads(body)
                    data = jd.get('data', {})
                    msg = jd.get('message', '')
                    detail = f"PB详情: {msg}"
                    if isinstance(data, dict):
                        for k, v in data.items():
                            detail += f" | {k}: {v}"
                except Exception:
                    detail = f"Body: {body}"

            logger.error(
                f"❌ attach_pdf_file: 创建 pdf_files 记录失败!\n"
                f"   task_id={task_id}, filename={filename}\n"
                f"   HTTP {status} — {detail}"
            )
            raise RuntimeError(f"创建PDF文件记录失败 (HTTP {status}): {detail}") from e

        except Exception as e:
            logger.error(
                f"❌ attach_pdf_file: 创建 pdf_files 记录时异常!\n"
                f"   task_id={task_id}, filename={filename}\n"
                f"   Error: {type(e).__name__}: {e}"
            )
            raise

        # Step 2: 上传文件到该记录
        try:
            result = self.upload_file("pdf_files", record_id, "pdf_file", file_path)
            uploaded_name = result.get("pdf_file", "")
            logger.info(f"  ✅ 文件上传成功: pdf_file={uploaded_name}")
            return result
        except Exception as e:
            logger.error(
                f"❌ attach_pdf_file: 文件上传失败!\n"
                f"   pdf_files_id={record_id}, file={file_path}\n"
                f"   Error: {type(e).__name__}: {e}"
            )
            raise

    def get_task_with_files(self, task_id: str) -> Dict[str, Any]:
        """获取任务详情（含关联的 PDF 文件列表）—— 使用 expand 参数"""
        return self._request(
            "GET",
            f"/collections/tasks/records/{task_id}",
            params={"expand": "pdf_files(task)"}
        )

    # ─────────────────────────────────────────────
    # 集合管理（开发用：确保集合结构存在）
    # ─────────────────────────────────────────────

    def ensure_collections_exist(self) -> bool:
        """
        检查并确认所需的 Collection 是否已存在且字段完整。
        
        PB v0.36+ 使用 fields 参数（非 schema），如果字段缺失会导致数据写入静默失败。

        所需的 Collection:
        1. tasks   — 处理任务（需含 status/total_files 等业务字段）
        2. pdf_files — 上传的PDF文件

        Returns:
            True 如果所有集合都存在且字段完整
        """
        required = ["tasks", "pdf_files"]
        try:
            # 先确保已认证
            if not self._token:
                self.authenticate()
                
            resp = self._session.get(
                f"{self.base_url}/api/collections",
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=(5, 10),
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                existing_names = [c["name"] for c in items]
                missing = [c for c in required if c not in existing_names]
                if missing:
                    logger.error(f"❌ 缺少 Collection: {missing}，请重启服务以自动创建")
                    return False

                # 检查每个 collection 的业务字段是否完整
                for col_info in items:
                    name = col_info.get("name", "")
                    if name not in required:
                        continue
                    field_names = [f.get("name") for f in col_info.get("fields", [])]
                    
                    if name == "tasks":
                        needed = {"status", "total_files", "processed_files"}
                        if not needed.issubset(field_names):
                            logger.error(f"❌ tasks Collection 字段不完整，缺少: {needed - set(field_names)}。请删除旧集合并重启服务重建")
                            return False
                    
                    if name == "pdf_files":
                        needed = {"task", "filename"}
                        if not needed.issubset(field_names):
                            logger.error(f"❌ pdf_files Collection 字段不完整，缺少: {needed - set(field_names)}。请删除旧集合并重启服务重建")
                            return False

                logger.info(f"✅ 所有 Collection 已就绪且字段完整: {required}")
                return True
            else:
                logger.warning(f"⚠️ 无法检查 Collections: HTTP {resp.status_code}")
                return False
        except Exception as e:
            logger.warning(f"⚠️ 检查 Collection 时出错: {e}")
            return True  # 假设存在，后续请求会报错再处理


if __name__ == "__main__":
    # 快速测试连接
    import sys
    pb = PocketBaseClient()
    if pb.authenticate():
        print("✅ 连接成功!")
        print(f"  URL: {pb.base_url}")
        # 列出现有 collections
        try:
            cols = pb._session.get(f"{pb.base_url}/api/collections",
                                   headers={"Authorization": f"Bearer {pb._token}"})
            for c in cols.json().get("items", []):
                print(f"  Collection: {c['name']}")
        except Exception as e:
            print(f"  列出 Collections 失败: {e}")
    else:
        print("❌ 连接失败")
        sys.exit(1)
