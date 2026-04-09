#!/usr/bin/env python3
"""
PDF批量处理主程序

功能：
1. 批量导入PDF文件
2. 自动识别PDF类型（纯文本/扫描件/混合）
3. 根据类型选择最优提取策略
4. 输出CSV格式结果（Unique ID, 源文件名, 解析内容）

使用方式：
    # 处理单个目录中的所有PDF
    python pdf_processor.py --input /path/to/pdfs
    
    # 处理指定文件列表
    python pdf_processor.py --input file1.pdf file2.pdf file3.pdf
    
    # 使用MinerU Token（提高额度和支持大文件）
    python pdf_processor.py --input /path/to/pdfs --token YOUR_TOKEN
    
    # 指定输出文件名
    python pdf_processor.py --input /path/to/pdfs --output result.csv
    
    # 仅检测类型不提取内容（快速预览）
    python pdf_processor.py --input /path/to/pdfs --detect-only

作者：资深开发工程师
版本：1.0.0
"""

import os
import sys
import uuid
import time
import asyncio
import argparse
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

# 数据处理
import pandas as pd
from tqdm import tqdm

# 日志
from loguru import logger

# 本地模块
from pdf_detector import PDFTypeDetector, PDFAnalysisResult, PDFType, batch_detect_pdfs
from mineru_client import MinerUClient, ParseResult


def _load_env_file(env_path: str = ".env"):
    """手动加载 .env 文件（不依赖 python-dotenv）"""
    p = Path(env_path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            # 只设置尚未存在的环境变量（不覆盖系统已有值）
            if key not in os.environ:
                os.environ[key] = value


class PDFProcessor:
    """
    PDF批量处理器 - 核心类

    设计原则：
    1. 智能路由：根据PDF类型自动选择最佳提取方式
       - 纯文本PDF → PyMuPDF直接提取（快速、准确、免费）
       - 扫描件/混合PDF → MinerU API OCR识别（高质量、付费/有限免）

    2. 容错性：单文件失败不影响整体批处理

    3. 可扩展性：易于添加新的处理策略
    """

    def __init__(self,
                 api_token: Optional[str] = None,
                 max_workers: int = 5,
                 output_path: str = "output.csv",
                 verbose: bool = False):
        self.api_token = api_token or os.getenv("MINERU_API_TOKEN")
        self.max_workers = max_workers
        self.output_path = Path(output_path)
        self.verbose = verbose

        # 初始化组件
        self.detector = PDFTypeDetector(verbose=verbose)
        self.mineru_client: Optional[MinerUClient] = None

        # 结果存储
        self.results: List[Dict[str, Any]] = []

        # 统计信息
        self.stats = {
            'total': 0,
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'text_only': 0,
            'scan_or_mixed': 0,
            'start_time': None,
            'end_time': None
        }

        # 配置日志（含文件输出）
        self._setup_logging()

    def _setup_logging(self):
        """配置日志系统（控制台 + 文件双输出）"""
        log_level = "DEBUG" if self.verbose else "INFO"
        logger.remove()
        
        # 控制台输出
        logger.add(
            sys.stderr,
            level=log_level,
            format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                   "<level>{level: <8}</level> | "
                   "<cyan>{message}</cyan>"
        )
        
        # 文件输出（持久化error log，保留最近7天）
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        logger.add(
            log_dir / "pdf_processor_{time:YYYY-MM-DD}.log",
            rotation="00:00",
            retention="7 days",
            level="DEBUG",  # 文件中记录所有级别
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
            encoding="utf-8"
        )
        
        # 独立的错误日志文件
        logger.add(
            log_dir / "errors_{time:YYYY-MM-DD}.log",
            rotation="00:00",
            retention="30 days",
            level="ERROR",
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {message}",
            encoding="utf-8"
        )

    def process_batch(self, input_paths: List[str], detect_only: bool = False) -> pd.DataFrame:
        """批量处理PDF文件"""
        self.stats['start_time'] = datetime.now()

        # Step 1: 收集所有PDF文件
        logger.info("📂 正在收集PDF文件...")
        pdf_files = self._collect_pdf_files(input_paths)

        if not pdf_files:
            logger.error("❌ 未找到任何PDF文件")
            return pd.DataFrame(columns=['unique_id', 'source_filename', 'content', 'pdf_type'])

        self.stats['total'] = len(pdf_files)
        logger.info(f"✅ 找到 {len(pdf_files)} 个PDF文件")

        # Step 2: 批量检测PDF类型
        logger.info("\n🔍 正在分析PDF类型...")
        detection_results = batch_detect_pdfs(pdf_files, verbose=self.verbose)

        # 显示检测结果摘要
        type_counts = {}
        for result in detection_results:
            type_name = result.pdf_type.value
            type_counts[type_name] = type_counts.get(type_name, 0) + 1

        logger.info("📊 类型分布:")
        for type_name, count in sorted(type_counts.items()):
            labels = {'text_only': '纯文本', 'scan_only': '纯扫描件', 'mixed': '混合型'}
            logger.info(f"   • {labels.get(type_name, type_name)}: {count} 个文件")

        if detect_only:
            df = pd.DataFrame([{
                'unique_id': str(uuid.uuid4()),
                'source_filename': Path(r.file_path).name,
                'content': '',
                'pdf_type': r.pdf_type.value,
                'total_pages': r.total_pages,
                'text_pages': r.text_pages,
                'scan_pages': r.scan_pages,
            } for r in detection_results])
            self._save_results(df, detect_only=True)
            return df

        # Step 3: 根据类型分别处理
        logger.info("\n⚙️ 开始提取文本内容...")

        text_only_files = [r for r in detection_results if r.pdf_type == PDFType.TEXT_ONLY]
        scan_mixed_files = [r for r in detection_results if r.pdf_type in [PDFType.SCAN_ONLY, PDFType.MIXED]]

        self.stats['text_only'] = len(text_only_files)
        self.stats['scan_or_mixed'] = len(scan_mixed_files)

        with tqdm(total=len(detection_results), desc="处理进度", unit="file") as pbar:

            # 3a: 处理纯文本PDF（使用PyMuPDF）
            if text_only_files:
                logger.info(f"\n📝 处理 {len(text_only_files)} 个纯文本PDF (使用PyMuPDF)...")

                for result in text_only_files:
                    try:
                        content = self._extract_text_with_pymupdf(result.file_path)
                        record = {
                            'unique_id': str(uuid.uuid4()),
                            'source_filename': Path(result.file_path).name,
                            'content': content,
                            'pdf_type': result.pdf_type.value
                        }
                        self.results.append(record)
                        self.stats['success'] += 1
                    except Exception as e:
                        logger.error(f"❌ 提取文本失败 [{result.file_path}]: {e}")
                        self.results.append({
                            'unique_id': str(uuid.uuid4()),
                            'source_filename': Path(result.file_path).name,
                            'content': '',
                            'pdf_type': result.pdf_type.value,
                            'error': str(e)
                        })
                        self.stats['failed'] += 1
                    pbar.update(1)

            # 3b: 处理扫描件和混合型PDF（使用MinerU OCR）
            if scan_mixed_files:
                logger.info(f"\n🖼️ 处理 {len(scan_mixed_files)} 个扫描件/混合PDF (使用MinerU OCR)...")

                if not self.mineru_client:
                    self.mineru_client = MinerUClient(api_token=self.api_token, verbose=self.verbose)

                # 包装一个完整的async流程（含cleanup）
                async def _run_ocr_with_cleanup():
                    try:
                        return await self._process_ocr_batch(scan_mixed_files)
                    finally:
                        if self.mineru_client:
                            await self.mineru_client.close()

                ocr_results = asyncio.run(_run_ocr_with_cleanup())

                for result, parse_result in zip(scan_mixed_files, ocr_results):
                    if parse_result.success:
                        record = {
                            'unique_id': str(uuid.uuid4()),
                            'source_filename': Path(result.file_path).name,
                            'content': parse_result.content,
                            'pdf_type': result.pdf_type.value
                        }
                        self.results.append(record)
                        self.stats['success'] += 1
                    else:
                        logger.error(f"❌ OCR识别失败 [{result.file_path}]: {parse_result.error_message}")
                        self.results.append({
                            'unique_id': str(uuid.uuid4()),
                            'source_filename': Path(result.file_path).name,
                            'content': '',
                            'pdf_type': result.pdf_type.value,
                            'error': parse_result.error_message
                        })
                        self.stats['failed'] += 1
                    pbar.update(1)

        self.stats['end_time'] = datetime.now()
        df = pd.DataFrame(self.results)
        self._save_results(df)
        self._print_stats_report()
        return df

    async def _process_ocr_batch(self, files_info: List[PDFAnalysisResult]) -> List[ParseResult]:
        """异步批量处理OCR任务"""

        async def process_single(file_info: PDFAnalysisResult) -> ParseResult:
            try:
                result = await self.mineru_client.parse_file(
                    file_info.file_path,
                    language="ch",
                    enable_ocr=True,
                    enable_formula=True,
                    enable_table=True
                )
                return result
            except Exception as e:
                return ParseResult(
                    success=False, content='', task_id='',
                    status=None, error_message=str(e), processing_time=0
                )

        tasks = [process_single(fi) for fi in files_info]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        final_results = []
        for r in results:
            if isinstance(r, Exception):
                final_results.append(ParseResult(
                    success=False, content='', task_id='',
                    status=None, error_message=str(r), processing_time=0
                ))
            else:
                final_results.append(r)

        return final_results

    def _extract_text_with_pymupdf(self, file_path: str) -> str:
        """使用PyMuPDF提取纯文本PDF的内容（速度快、准确率高、免费）"""
        import fitz
        doc = fitz.open(file_path)
        all_text = []

        for page_num, page in enumerate(doc):
            text = page.get_text("text")
            if text.strip():
                all_text.append(f"[第{page_num + 1}页]\n{text}")

        doc.close()
        full_text = "\n\n---\n\n".join(all_text)
        cleaned_text = self._clean_extracted_text(full_text)
        return cleaned_text

    def _clean_extracted_text(self, text: str) -> str:
        """清理提取的文本"""
        if not text:
            return ""
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped:
                cleaned_lines.append(stripped)
            elif cleaned_lines and cleaned_lines[-1] != '':
                cleaned_lines.append('')
        while cleaned_lines and cleaned_lines[0] == '':
            cleaned_lines.pop(0)
        while cleaned_lines and cleaned_lines[-1] == '':
            cleaned_lines.pop()
        return '\n'.join(cleaned_lines)

    def _collect_pdf_files(self, input_paths: List[str]) -> List[str]:
        """从输入路径中收集所有PDF文件"""
        pdf_files = []
        seen = set()
        for path_str in input_paths:
            path = Path(path_str)
            if path.is_file():
                if path.suffix.lower() in ['.pdf']:
                    abs_path = str(path.resolve())
                    if abs_path not in seen:
                        pdf_files.append(abs_path)
                        seen.add(abs_path)
            elif path.is_dir():
                for pdf_path in path.rglob('*.pdf'):
                    abs_path = str(pdf_path.resolve())
                    if abs_path not in seen:
                        pdf_files.append(abs_path)
                        seen.add(abs_path)
            else:
                logger.warning(f"⚠️ 路径不存在或无法访问: {path_str}")
        return sorted(pdf_files)

    def _save_results(self, df: pd.DataFrame, detect_only: bool = False):
        """保存结果到CSV"""
        if df.empty:
            logger.warning("⚠️ 没有结果可保存")
            return
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        if detect_only:
            columns_to_save = ['unique_id', 'source_filename', 'pdf_type',
                              'total_pages', 'text_pages', 'scan_pages']
            export_df = df[columns_to_save]
        else:
            # 输出CSV：包含error列（失败文件保留错误信息）
            export_df = df[['unique_id', 'source_filename', 'content', 'pdf_type']].copy()
            if 'error' in df.columns:
                export_df['error'] = df['error']
            else:
                export_df['error'] = ''
        export_df.to_csv(self.output_path, index=False, encoding='utf-8-sig')
        logger.info(f"\n💾 结果已保存到: {self.output_path.absolute()}")

    def _print_stats_report(self):
        """打印统计报告"""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        print("\n" + "=" * 70)
        print("📊 处理统计报告".center(60))
        print("=" * 70)
        print(f"  总文件数:     {self.stats['total']}")
        print(f"  ✅ 成功处理:  {self.stats['success']}")
        print(f"  ❌ 失败数量:  {self.stats['failed']}")
        print(f"  📝 纯文本PDF:  {self.stats['text_only']} (使用PyMuPDF)")
        print(f"  🖼️ 扫描/混合:  {self.stats['scan_or_mixed']} (使用MinerU OCR)")
        print(f"  ⏱️ 总耗时:     {duration:.2f} 秒")
        if self.stats['success'] > 0:
            print(f"  📈 平均速度:   {duration / self.stats['success']:.2f} 秒/文件")
        print(f"\n  💾 输出文件:   {self.output_path.absolute()}")
        print("=" * 70)


def main():
    """命令行入口"""
    parser = argparse.ArgumentParser(
        description='📄 PDF批量处理工具 - 智能识别类型并提取文本',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例用法:
  %(prog)s --input ./my_pdfs                  # 处理目录下所有PDF
  %(prog)s --input file1.pdf file2.pdf        # 处理指定文件
  %(prog)s --input ./pdfs --token MY_TOKEN    # 使用API Token
  %(prog)s --input ./pdfs --detect-only       # 仅检测类型
  %(prog)s --input ./pdfs -o custom.csv       # 指定输出文件名
        """
    )
    parser.add_argument('--input', '-i', nargs='+', required=True,
                        help='输入的PDF文件或包含PDF的目录（支持多个）')
    parser.add_argument('--output', '-o', default='output.csv',
                        help='输出CSV文件路径（默认: output.csv）')
    parser.add_argument('--token', '-t', default=None,
                        help='MinerU API Token（可选，提供后可提高限额）')
    parser.add_argument('--workers', '-w', type=int, default=5,
                        help='最大并发数（默认: 5）')
    parser.add_argument('--detect-only', action='store_true',
                        help='仅检测PDF类型，不提取内容')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='显示详细日志')
    args = parser.parse_args()

    print("\n" + "🔤 PDF批量处理工具 v1.0".center(70))
    print("智能识别 · 高效提取 · 批量处理".center(70))
    print("-" * 70 + "\n")

    # 加载 .env 文件中的配置
    _load_env_file()

    processor = PDFProcessor(
        api_token=args.token,
        max_workers=args.workers,
        output_path=args.output,
        verbose=args.verbose
    )

    try:
        result_df = processor.process_batch(input_paths=args.input, detect_only=args.detect_only)
        if not result_df.empty:
            print(f"\n✅ 处理完成！共处理 {len(result_df)} 个文件")
        else:
            print("\n⚠️ 没有文件被成功处理")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n⛔ 用户中断操作")
        sys.exit(130)
    except Exception as e:
        logger.exception(f"处理过程中发生错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
