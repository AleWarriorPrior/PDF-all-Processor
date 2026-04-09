"""
MinerU API 集成模块
用于处理扫描件PDF和混合型PDF的OCR识别

API文档参考：https://mineru.net/apiManage/docs
提供两种模式：
1. 精准解析API（需要Token，支持大文件）
2. Agent轻量解析API（免Token，适合小文件）
"""

import os
import time
import aiohttp
import asyncio
from typing import Optional
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from loguru import logger


class MinerUAPIType(Enum):
    """MinerU API类型"""
    PRECISION = "precision"      # 精准解析API（需要Token）
    AGENT = "agent"              # Agent轻量解析API（免Token）


class TaskStatus(Enum):
    """任务状态"""
    PENDING = "pending"           # 等待中
    PROCESSING = "processing"     # 处理中
    DONE = "done"                 # 完成
    FAILED = "failed"             # 失败


@dataclass
class ParseResult:
    """解析结果"""
    success: bool
    content: str                  # 提取出的文本内容（Markdown格式）
    task_id: str                  # 任务ID
    status: TaskStatus            # 最终状态
    error_message: Optional[str]  # 错误信息
    processing_time: float        # 处理耗时（秒）
    
    def __str__(self) -> str:
        if self.success:
            return f"[{self.task_id}] 成功，内容长度: {len(self.content)} 字符"
        else:
            return f"[{self.task_id}] 失败: {self.error_message}"


class MinerUClient:
    """
    MinerU API 客户端
    
    功能：
    1. 自动选择最优API模式（优先使用精准API，Token不可用时降级为Agent API）
    2. 支持文件上传和URL解析两种方式
    3. 异步轮询任务状态直到完成
    4. 自动重试和错误处理
    5. 结果缓存和去重
    
    使用流程：
    1. 初始化客户端（传入Token或留空使用免费API）
    2. 调用 parse_file() 或 parse_url() 提交解析任务
    3. 等待结果返回
    """
    
    # API端点配置
    BASE_URL = "https://mineru.net"
    
    # 精准API端点
    PRECISION_SUBMIT_URL = f"{BASE_URL}/api/v4/extract/task"
    PRECISION_STATUS_URL = f"{BASE_URL}/api/v4/extract/task/{{task_id}}"
    
    # 精准API - 本地文件批量上传
    PRECISION_BATCH_UPLOAD_URL = f"{BASE_URL}/api/v4/file-urls/batch"
    PRECISION_BATCH_RESULT_URL = f"{BASE_URL}/api/v4/extract-results/batch/{{batch_id}}"
    
    # Agent API端点
    AGENT_FILE_UPLOAD_URL = f"{BASE_URL}/api/v1/agent/parse/file"
    AGENT_STATUS_URL = f"{BASE_URL}/api/v1/agent/parse/{{task_id}}"
    
    # 配置参数
    MAX_RETRIES = 3                    # 最大重试次数
    POLL_INTERVAL = 5                  # 轮询间隔（秒）
    TASK_TIMEOUT = 600                 # 单任务超时时间（秒）— 大文件OCR可能需要10分钟
    UPLOAD_CHUNK_SIZE = 8 * 1024 * 1024  # 上传分块大小 (8MB)
    
    def __init__(self, 
                 api_token: Optional[str] = None,
                 prefer_api_type: MinerUAPIType = MinerUAPIType.PRECISION,
                 max_concurrent: int = 3,
                 verbose: bool = False):
        """
        初始化MinerU客户端
        
        Args:
            api_token: MinerU API Token（可选，不提供则使用免费Agent API）
            prefer_api_type: 优选的API类型
            max_concurrent: 最大并发任务数
            verbose: 是否输出详细调试日志
        """
        self.api_token = api_token or os.getenv("MINERU_API_TOKEN")
        self.prefer_api_type = prefer_api_type
        self.max_concurrent = max_concurrent
        self.verbose_debug = verbose
        self._semaphore = asyncio.Semaphore(max_concurrent)
        
        # 确定可用的API模式
        if self.api_token and prefer_api_type == MinerUAPIType.PRECISION:
            self.active_api_type = MinerUAPIType.PRECISION
            logger.info("使用 MinerU 精准解析API")
        else:
            self.active_api_type = MinerUAPIType.AGENT
            logger.info("使用 MinerU Agent 轻量解析API（免Token）")
        
        # HTTP会话（复用连接提高性能）
        self._session: Optional[aiohttp.ClientSession] = None
        
        # 任务缓存（避免重复提交相同文件）
        self._task_cache = {}
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建HTTP会话"""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.TASK_TIMEOUT + 10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        """关闭HTTP会话"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def _get_headers(self) -> dict:
        """获取请求头"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        if self.active_api_type == MinerUAPIType.PRECISION and self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        
        return headers
    
    async def parse_file(self, 
                         file_path: str, 
                         language: str = "ch",
                         enable_ocr: bool = True,
                         enable_formula: bool = True,
                         enable_table: bool = True) -> ParseResult:
        """
        解析PDF文件
        
        Args:
            file_path: PDF文件路径
            language: 语言设置（ch=中英文, en=英文, japan=日文）
            enable_ocr: 是否启用OCR（扫描件必须开启）
            enable_formula: 是否识别公式
            enable_table: 是否识别表格
            
        Returns:
            ParseResult: 解析结果
        """
        start_time = time.time()
        file_path = Path(file_path).resolve()
        
        if not file_path.exists():
            return ParseResult(
                success=False,
                content="",
                task_id="",
                status=TaskStatus.FAILED,
                error_message=f"文件不存在: {file_path}",
                processing_time=0
            )
        
        # 检查文件大小（在任何API模式下都要检查）
        file_size = file_path.stat().st_size
        size_mb = file_size / (1024 * 1024)
        
        # Agent API 硬限制 10MB
        if size_mb > 10:
            logger.warning(f"⚠️ 文件过大 ({size_mb:.1f}MB > 10MB)，Agent API 无法处理")
            if not self.api_token:
                return ParseResult(
                    success=False,
                    content="",
                    task_id="",
                    status=TaskStatus.FAILED,
                    error_message=f"文件过大 ({size_mb:.1f}MB > 10MB)，Agent API 限制。需要提供 MINERU_API_TOKEN 使用精准模式。",
                    processing_time=time.time() - start_time
                )
            else:
                logger.info(f"有 Token 可用，尝试使用精准 API 处理大文件 ({size_mb:.1f}MB)")
        
        async with self._semaphore:
            try:
                # 根据API类型选择不同的提交流程
                if self.active_api_type == MinerUAPIType.PRECISION:
                    result = await self._parse_file_precision(
                        file_path, language, enable_ocr, enable_formula, enable_table
                    )
                else:
                    result = await self._parse_file_agent(
                        file_path, language, enable_ocr, enable_formula, enable_table
                    )
                
                result.processing_time = time.time() - start_time
                
                # 记录详细结果日志
                if result.success:
                    logger.info(f"✅ [{file_path.name}] OCR成功，内容 {len(result.content)} 字符，耗时 {result.processing_time:.1f}s")
                else:
                    logger.error(f"❌ [{file_path.name}] OCR失败: {result.error_message} (耗时 {result.processing_time:.1f}s)")
                
                return result
                
            except Exception as e:
                logger.exception(f"解析文件异常 [{file_path.name}]: {e}")
                return ParseResult(
                    success=False,
                    content="",
                    task_id="",
                    status=TaskStatus.FAILED,
                    error_message=f"{type(e).__name__}: {str(e)}",
                    processing_time=time.time() - start_time
                )
    
    async def _parse_file_agent(self, 
                                 file_path: Path,
                                 language: str,
                                 enable_ocr: bool,
                                 enable_formula: bool,
                                 enable_table: bool) -> ParseResult:
        """使用Agent API解析文件"""
        
        session = await self._get_session()
        
        # Step 1: 获取上传URL
        upload_data = {
            "file_name": file_path.name,
            "language": language,
            "enable_table": enable_table,
            "is_ocr": enable_ocr,
            "enable_formula": enable_formula
        }
        
        async with session.post(
            self.AGENT_FILE_UPLOAD_URL,
            json=upload_data,
            headers=self._get_headers()
        ) as response:
            resp_data = await response.json()
            
            if resp_data.get("code") != 0:
                raise Exception(f"获取上传URL失败: {resp_data.get('msg', '未知错误')}")
            
            task_id = resp_data["data"]["task_id"]
            upload_url = resp_data["data"]["file_url"]
            logger.info(f"获得上传授权 [{file_path.name}] task_id={task_id}")
        
        # Step 2: 上传文件到OSS（预签名URL）
        # OSS预签名URL对请求头非常敏感，必须使用http.client精确控制
        import http.client
        from urllib.parse import urlparse

        parsed = urlparse(upload_url)
        host = parsed.hostname
        port = parsed.port or 443
        path = parsed.path + '?' + parsed.query if parsed.query else parsed.path

        with open(file_path, 'rb') as f:
            file_content = f.read()

        conn = http.client.HTTPSConnection(host, port)
        # 只发送必要的Header，Content-Type必须与预签名一致
        # 从URL中可能包含的参数推断（或保持为空让服务端判断）
        content_length = len(file_content)
        conn.request('PUT', path, body=file_content,
                     headers={
                         'Content-Length': str(content_length),
                     })
        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode('utf-8', errors='replace')
        conn.close()

        if status not in (200, 201, 204):
            raise Exception(f"文件上传失败，状态码: {status}, 响应: {body[:300]}")
        
        # Step 3: 轮询等待结果
        result = await self._poll_task_status(task_id, is_agent=True)
        
        # Step 4: 如果成功，下载结果
        if result.success and result.content:
            pass  # Agent API直接在响应中返回markdown_url
        
        result.task_id = task_id
        return result
    
    async def _parse_file_precision(self,
                                     file_path: Path,
                                     language: str,
                                     enable_ocr: bool,
                                     enable_formula: bool,
                                     enable_table: bool) -> ParseResult:
        """
        使用精准API解析文件（支持大文件 > 10MB，最大200MB）
        
        流程：
        1. POST /api/v4/file-urls/batch → 获取上传URL + batch_id
        2. PUT 上传文件到OSS
        3. GET /api/v4/extract-results/batch/{batch_id} → 等待结果
        4. 下载markdown内容
        """
        
        session = await self._get_session()
        file_size = file_path.stat().st_size
        size_mb = file_size / (1024 * 1024)
        
        # 文件大小限制检查（Precision API: ≤200MB）
        if size_mb > 200:
            return ParseResult(
                success=False, content="", task_id="",
                status=TaskStatus.FAILED,
                error_message=f"文件过大 ({size_mb:.1f}MB > 200MB)，超出精准API上限",
                processing_time=0
            )
        
        logger.info(f"📤 使用精准API批量上传 [{file_path.name}] ({size_mb:.1f}MB)")
        
        # ===== Step 1: 申请上传URL =====
        # data_id 硬限制128字符 — 用纯MD5 hash彻底杜绝长度问题（32字符）
        import hashlib as _hl
        _safe_data_id = _hl.md5(file_path.name.encode('utf-8')).hexdigest()
        
        upload_request = {
            "files": [{
                "name": file_path.name,
                "data_id": _safe_data_id,
            }],
            "model_version": "vlm",  # vlm模型质量最高
            "language": language,
            "is_ocr": enable_ocr,
            "enable_formula": enable_formula,
            "enable_table": enable_table,
        }
        
        try:
            async with session.post(
                self.PRECISION_BATCH_UPLOAD_URL,
                json=upload_request,
                headers=self._get_headers()
            ) as response:
                resp_data = await response.json()
                
                code = resp_data.get("code", -1)
                if code != 0:
                    msg = resp_data.get("msg", "未知API错误")
                    detail = resp_data.get("data", "")
                    raise Exception(f"申请上传URL失败(code={code}): {msg} {detail}".strip())
                
                data = resp_data["data"]
                batch_id = data.get("batch_id", "")
                file_urls = data.get("file_urls", [])
                
                if not batch_id or not file_urls:
                    raise Exception(f"返回数据异常: batch_id={batch_id}, file_urls count={len(file_urls)}")
                
                upload_url = file_urls[0]  # 第一个文件的上传URL
                
                logger.info(f"✅ 批量上传任务已创建 [{batch_id}]")
                
        except aiohttp.ClientResponseError as e:
            raise Exception(f"精准API HTTP错误: status={e.status}, body={await e.text() if e.status else ''}")
        except Exception as e:
            raise Exception(f"申请上传URL异常: {str(e)}")
        
        # ===== Step 2: 上传文件到OSS =====
        import http.client
        from urllib.parse import urlparse

        parsed = urlparse(upload_url)
        host = parsed.hostname
        port = parsed.port or 443
        path = parsed.path + '?' + parsed.query if parsed.query else parsed.path

        logger.info(f"⬆️ 开始上传到 OSS ({size_mb:.1f}MB)...")
        upload_start = time.time()
        
        with open(file_path, 'rb') as f:
            file_content = f.read()

        content_length = len(file_content)
        conn = http.client.HTTPSConnection(host, port)
        conn.request('PUT', path, body=file_content,
                     headers={'Content-Length': str(content_length)})
        resp = conn.getresponse()
        status = resp.status
        body = resp.read().decode('utf-8', errors='replace')
        conn.close()
        
        upload_elapsed = time.time() - upload_start
        
        if status not in (200, 201, 204):
            raise Exception(f"OSS上传失败(HTTP {status}): {body[:500]}")
        
        logger.info(f"✅ 文件上传完成 ({upload_elapsed:.1f}s)")
        
        # ===== Step 3: 轮询等待结果 =====
        result = await self._poll_batch_result(batch_id)
        
        # 如果轮询返回成功，下载内容
        if result.success and result.content:
            return result
        
        result.task_id = batch_id  # 用batch_id作为标识
        return result
    
    async def _poll_task_status(self, 
                                  task_id: str, 
                                  is_agent: bool = False) -> ParseResult:
        """轮询任务状态直到完成"""
        
        session = await self._get_session()
        status_url = self.AGENT_STATUS_URL.format(task_id=task_id) if is_agent else \
                     self.PRECISION_STATUS_URL.format(task_id=task_id)
        
        api_label = "Agent" if is_agent else "Precision"
        start_time = time.time()
        poll_count = 0
        last_raw_response = ""  # 保存最后一次原始响应用于诊断
        
        logger.debug(f"🔍 开始轮询 {api_label} API 任务状态 [{task_id}]")
        
        while time.time() - start_time < self.TASK_TIMEOUT:
            poll_count += 1
            elapsed = time.time() - start_time
            
            try:
                async with session.get(
                    status_url,
                    headers=self._get_headers()
                ) as response:
                    raw_text = await response.text()
                    last_raw_response = raw_text[:1000]  # 保留用于诊断
                    
                    resp_data = None
                    try:
                        resp_data = await response.json()
                    except Exception as json_err:
                        logger.warning(f"[{task_id}] 第{poll_count}次 轮询 响应非JSON: {raw_text[:200]}")
                        await asyncio.sleep(self.POLL_INTERVAL)
                        continue
                    
                    if poll_count % 10 == 0 or self.verbose_debug:
                        logger.info(f"🔄 轮询 [{task_id}] 第{poll_count}次 ({elapsed:.0f}s elapsed)")
                    
                    code = resp_data.get("code", -1)
                    
                    if code != 0:
                        # API错误码处理
                        msg = resp_data.get('msg', '未知错误')
                        
                        known_errors = {
                            -60005: "文件超过大小限制",
                            -60001: "参数错误",
                            -60002: "文件格式不支持",
                            -60003: "OCR服务异常",
                            -60004: "任务不存在",
                        }
                        err_desc = known_errors.get(code, msg)
                        
                        if code in (-60004,):
                            return ParseResult(
                                success=False,
                                content="",
                                task_id=task_id,
                                status=TaskStatus.FAILED,
                                error_message=f"API错误(code={code}): {err_desc}",
                                processing_time=time.time() - start_time
                            )
                        
                        # 其他非致命错误，继续等待
                        if poll_count % 5 == 0:
                            logger.warning(f"⚠️ [{task_id}] API返回code={code}: {err_desc}")
                        await asyncio.sleep(self.POLL_INTERVAL)
                        continue
                    
                    data = resp_data.get("data", {})
                    state = data.get("state", "")
                    
                    if state == "done":
                        # 任务完成
                        markdown_url = data.get("markdown_url") if is_agent else \
                                      data.get("full_zip_url", "")
                        
                        if is_agent and markdown_url:
                            content = await self._download_content(markdown_url)
                            if not content:
                                return ParseResult(
                                    success=False,
                                    content="",
                                    task_id=task_id,
                                    status=TaskStatus.DONE,
                                    error_message="任务已完成但下载内容为空 (markdown_url可能已过期)",
                                    processing_time=time.time() - start_time
                                )
                            return ParseResult(
                                success=True,
                                content=content,
                                task_id=task_id,
                                status=TaskStatus.DONE,
                                error_message=None,
                                processing_time=time.time() - start_time
                            )
                        elif not is_agent:
                            # 精准API返回结果
                            md_url = data.get("markdown_url", "") or data.get("full_zip_url", "")
                            if md_url:
                                content = await self._download_content(md_url)
                                if content:
                                    return ParseResult(
                                        success=True,
                                        content=content,
                                        task_id=task_id,
                                        status=TaskStatus.DONE,
                                        error_message=None,
                                        processing_time=time.time() - start_time)
                            
                            return ParseResult(
                                success=False,
                                content="",
                                task_id=task_id,
                                status=TaskStatus.DONE,
                                error_message=f"精准API任务完成但无可用下载URL。原始数据: {str(data)[:300]}",
                                processing_time=time.time() - start_time
                            )
                        else:
                            return ParseResult(
                                success=False,
                                content="",
                                task_id=task_id,
                                status=TaskStatus.DONE,
                                error_message="未返回结果URL",
                                processing_time=time.time() - start_time
                            )
                    
                    elif state in ("failed", "error"):
                        error_msg = data.get("error_message", "") or data.get("message", "") or "未知处理错误"
                        detail = f", 原始响应: {last_raw_response[:200]}" if not error_msg else ""
                        full_error = f"{error_msg}{detail}"
                        logger.error(f"❌ [{task_id}] 任务失败: {full_error}")
                        return ParseResult(
                            success=False,
                            content="",
                            task_id=task_id,
                            status=TaskStatus.FAILED,
                            error_message=full_error,
                            processing_time=time.time() - start_time
                        )
                    
                    # 其他状态: pending / processing 等，继续等待
                    if poll_count == 1 or poll_count % 10 == 0:
                        logger.info(f"⏳ [{task_id}] 状态: {state} ({elapsed:.0f}s)")
                    
                    await asyncio.sleep(self.POLL_INTERVAL)
                    
            except asyncio.TimeoutError:
                logger.warning(f"⚠️ 轮询超时(第{poll_count}次)，重试... [{task_id}]")
                continue
            except aiohttp.ClientError as e:
                logger.error(f"❌ 网络错误 [{task_id}] 第{poll_count}次: {type(e).__name__}: {e}")
                await asyncio.sleep(min(self.POLL_INTERVAL * 2, 30))  # 指数退避上限30s
                continue
            except Exception as e:
                logger.exception(f"❌ 轮询异常 [{task_id}]: {e}")
                await asyncio.sleep(self.POLL_INTERVAL * 2)
                continue
        
        # 最终超时
        timeout_detail = f"(共轮询{poll_count}次, 最后响应: {last_raw_response[:200]})"
        return ParseResult(
            success=False,
            content="",
            task_id=task_id,
            status=TaskStatus.FAILED,
            error_message=f"处理超时({self.TASK_TIMEOUT}s) {timeout_detail}",
            processing_time=time.time() - start_time
        )
    
    async def _download_content(self, url: str) -> str:
        """下载并提取文本内容"""
        session = await self._get_session()
        
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    content_type = response.headers.get('Content-Type', '')
                    
                    if 'text' in content_type or 'json' in content_type:
                        # 直接是文本内容
                        text = await response.text()
                        return text
                    else:
                        # 可能是二进制文件
                        data = await response.read()
                        try:
                            return data.decode('utf-8')
                        except UnicodeDecodeError:
                            return data.decode('latin-1')
                else:
                    logger.error(f"下载失败，状态码: {response.status}")
                    return ""
                    
        except Exception as e:
            logger.error(f"下载内容失败: {e}")
            return ""
    
    async def _poll_batch_result(self, batch_id: str) -> ParseResult:
        """
        轮询精准API批量任务结果
        
        端点: GET /api/v4/extract-results/batch/{batch_id}
        实际返回字段: extract_result (不是 results!)
        """
        
        session = await self._get_session()
        result_url = self.PRECISION_BATCH_RESULT_URL.format(batch_id=batch_id)
        
        start_time = time.time()
        poll_count = 0
        last_raw_response = ""
        
        logger.info(f"🔍 开始轮询精准API批量任务 [{batch_id}]")
        
        while time.time() - start_time < self.TASK_TIMEOUT:
            poll_count += 1
            elapsed = time.time() - start_time
            
            try:
                async with session.get(
                    result_url,
                    headers=self._get_headers()
                ) as response:
                    raw_text = await response.text()
                    last_raw_response = raw_text[:1500]
                    
                    try:
                        resp_data = await response.json()
                    except Exception:
                        logger.warning(f"[{batch_id}] 第{poll_count}次 响应非JSON: {raw_text[:200]}")
                        await asyncio.sleep(self.POLL_INTERVAL)
                        continue
                    
                    code = resp_data.get("code", -1)
                    
                    if code != 0:
                        msg = resp_data.get("msg", "")
                        if poll_count % 5 == 0:
                            logger.warning(f"[{batch_id}] API返回code={code}: {msg}")
                        await asyncio.sleep(self.POLL_INTERVAL)
                        continue
                    
                    data = resp_data.get("data", {})
                    
                    # ===== 关键修复：实际API返回的字段名是 extract_result =====
                    extract_results = data.get("extract_result", []) or data.get("results", [])
                    
                    if not extract_results:
                        # 可能还在处理中，检查是否有状态字段
                        state = data.get("state", "")
                        if poll_count % 10 == 0 or poll_count == 1:
                            logger.info(f"⏳ [{batch_id}] 状态: {state or 'processing'} ({elapsed:.0f}s)")
                            # 每30次打印一次原始响应结构（调试用）
                            if poll_count % 30 == 0 and self.verbose_debug:
                                keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
                                logger.debug(f"[{batch_id}] data keys: {keys}")
                        await asyncio.sleep(self.POLL_INTERVAL)
                        continue
                    
                    # 取第一个文件的结果
                    file_result = extract_results[0]
                    state = file_result.get("state", "").lower()
                    
                    if state == "done":
                        markdown_url = file_result.get("markdown_url", "")
                        
                        if markdown_url:
                            content = await self._download_content(markdown_url)
                            if not content:
                                return ParseResult(
                                    success=False, content="", task_id=batch_id,
                                    status=TaskStatus.DONE,
                                    error_message="任务完成但markdown内容为空(可能URL已过期)",
                                    processing_time=time.time() - start_time
                                )
                            logger.info(f"✅ 精准API任务完成 [{batch_id}]，内容 {len(content)} 字符，耗时 {elapsed:.0f}s")
                            return ParseResult(
                                success=True,
                                content=content,
                                task_id=batch_id,
                                status=TaskStatus.DONE,
                                error_message=None,
                                processing_time=time.time() - start_time
                            )
                        
                        # 尝试 ZIP 包
                        zip_url = file_result.get("full_zip_url", "") or file_result.get("zip_url", "")
                        if zip_url:
                            logger.info(f"📦 精准API返回ZIP包，尝试解析...")
                            zip_content = await self._download_zip_as_text(zip_url)
                            if zip_content:
                                return ParseResult(
                                    success=True, content=zip_content,
                                    task_id=batch_id, status=TaskStatus.DONE,
                                    error_message=None,
                                    processing_time=time.time() - start_time
                                )
                            return ParseResult(
                                success=False, content="", task_id=batch_id,
                                status=TaskStatus.DONE,
                                error_message="ZIP包下载或解析失败",
                                processing_time=time.time() - start_time
                            )
                        
                        return ParseResult(
                            success=False, content="", task_id=batch_id,
                            status=TaskStatus.DONE,
                            error_message=f"任务完成但无可用的结果URL。可用keys: {list(file_result.keys())}",
                            processing_time=time.time() - start_time
                        )
                    
                    elif state in ("failed", "error"):
                        err_msg = (file_result.get("err_msg", "") 
                                   or file_result.get("error_message", "")
                                   or file_result.get("message", "")
                                   or "未知处理错误")
                        logger.error(f"❌ 精准API任务失败 [{batch_id}]: {err_msg}")
                        return ParseResult(
                            success=False, content="", task_id=batch_id,
                            status=TaskStatus.FAILED,
                            error_message=f"精准API处理失败: {err_msg}",
                            processing_time=time.time() - start_time
                        )
                    
                    # 其他状态: pending/running 等 — 继续等待
                    if poll_count % 15 == 0:
                        logger.info(f"⏳ [{batch_id}] 文件状态: '{state}' ({elapsed:.0f}s elapsed)")
                    
                    await asyncio.sleep(self.POLL_INTERVAL)
            
            except asyncio.TimeoutError:
                logger.warning(f"⏰ 轮询超时 第{poll_count}次 [{batch_id}]")
                continue
            except aiohttp.ClientError as e:
                logger.error(f"🌐 网络错误 [{batch_id}] 第{poll_count}次: {type(e).__name__}: {e}")
                await asyncio.sleep(min(self.POLL_INTERVAL * 2, 30))
                continue
            except Exception as e:
                logger.exception(f"❌ 轮询异常 [{batch_id}]: {e}")
                await asyncio.sleep(self.POLL_INTERVAL * 2)
                continue
        
        timeout_detail = f"(共轮询{poll_count}次，耗时{elapsed:.0f}s，最后响应: {last_raw_response[:300]})"
        return ParseResult(
            success=False, content="", task_id=batch_id,
            status=TaskStatus.FAILED,
            error_message=f"精准API处理超时({self.TASK_TIMEOUT}s) {timeout_detail}",
            processing_time=time.time() - start_time
        )
    
    async def _download_zip_as_text(self, zip_url: str) -> str:
        """从ZIP包URL下载并提取文本内容"""
        import io
        import zipfile as zf
        
        try:
            session = await self._get_session()
            async with session.get(zip_url) as response:
                if response.status != 200:
                    logger.error(f"ZIP下载失败: HTTP {response.status}")
                    return ""
                
                zip_data = await response.read()
                
            buf = io.BytesIO(zip_data)
            with zf.ZipFile(buf, 'r') as z:
                # 优先找 markdown 文件
                md_files = [n for n in z.namelist() if n.endswith('.md') or n.endswith('.markdown')]
                if md_files:
                    text = z.read(md_files[0]).decode('utf-8', errors='replace')
                    logger.info(f"从ZIP提取到: {md_files[0]} ({len(text)}字符)")
                    return text
                
                # 其次找 txt 文件
                txt_files = [n for n in z.namelist() if n.endswith('.txt')]
                if txt_files:
                    return z.read(txt_files[0]).decode('utf-8', errors='replace')
                
                # 最后读第一个非目录文件
                all_files = [n for n in z.namelist() if not n.endswith('/')]
                if all_files:
                    return z.read(all_files[0]).decode('utf-8', errors='replace')
                
                logger.warning("ZIP包为空或无可用文件")
                return ""
                
        except Exception as e:
            logger.error(f"ZIP解析异常: {type(e).__name__}: {e}")
            return ""


# 同步包装器（方便非异步环境调用）
def parse_pdf_sync(file_path: str,
                   api_token: str = None,
                   verbose: bool = False,
                   **kwargs) -> ParseResult:
    """
    同步版本的PDF解析函数
    
    Args:
        file_path: PDF文件路径
        api_token: API Token
        verbose: 是否输出详细日志
        **kwargs: 其他参数传递给parse_file()
        
    Returns:
        ParseResult: 解析结果
    """
    async def _run():
        client = MinerUClient(api_token=api_token, verbose=verbose)
        try:
            result = await client.parse_file(file_path, **kwargs)
            return result
        finally:
            await client.close()
    
    # 运行异步函数
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 如果已经在事件循环中，创建新的线程运行
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _run())
                return future.result()
        else:
            return loop.run_until_complete(_run())
    except RuntimeError:
        # 没有事件循环，创建新的
        return asyncio.run(_run())


if __name__ == "__main__":
    # 测试代码
    import sys
    
    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        print(f"\n测试解析文件: {test_file}\n")
        print("="*60)
        
        result = parse_pdf_sync(test_file, enable_ocr=True)
        
        print(result)
        
        if result.success:
            print("\n--- 提取的内容预览 ---\n")
            print(result.content[:1000] if len(result.content) > 1000 else result.content)
            print("\n..." if len(result.content) > 1000 else "")
