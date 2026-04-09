"""
PDF类型检测模块 v2.0
用于智能判断PDF文档的类型：纯文本、扫描件、混合型

v2.0 改进：
- 使用 PyMuPDF 的 get_image_rects() 获取图像精确位置和尺寸
- 自动过滤小logo、水印、页眉页脚等装饰性图片（<5%页面面积 或 位于边缘区域）
- 基于多维度信号综合判断：文本密度 + 字体存在性 + 有效图像覆盖率
- 新增逐页诊断输出，便于调试和调优
"""

import fitz  # PyMuPDF
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Dict, Any, Optional
from loguru import logger


class PDFType(Enum):
    """PDF文档类型枚举"""
    TEXT_ONLY = "text_only"           # 纯文本PDF → PyMuPDF直接提取
    SCAN_ONLY = "scan_only"           # 纯扫描件PDF → MinerU OCR
    MIXED = "mixed"                   # 混合型（部分页面为扫描）→ MinerU OCR


@dataclass
class PageDiagnosis:
    """单页诊断信息"""
    page_num: int
    text_length: int                  # 清理后的文本字符数
    raw_text_length: int              # 原始文本字符数
    has_fonts: bool                   # 是否包含字体信息（原生文本的标志）
    total_images: int                 # 图像总数
    significant_images: int           # 有效(非装饰)图像数
    significant_image_ratio: float    # 有效图像占页面面积比 (0-1)
    is_header_footer_only: bool       # 文本是否仅在页眉页脚区域
    judgment: str                     # 本页初步判断: text / scan / ambiguous


@dataclass
class PDFAnalysisResult:
    """PDF分析结果"""
    file_path: str
    pdf_type: PDFType
    text_pages: int                   # 明确为文本的页数
    scan_pages: int                   # 明确为扫描的页数
    ambiguous_pages: int              # 无法确定的页数
    total_pages: int                  # 总页数
    text_confidence: float            # 整体文本可信度 (0-1)
    significant_image_coverage: float # 有效图像总覆盖率 (0-1，已排除装饰图)
    pages_detail: List[PageDiagnosis] = field(default_factory=list)  # 每页详情
    
    def __str__(self) -> str:
        type_names = {
            PDFType.TEXT_ONLY: "纯文本",
            PDFType.SCAN_ONLY: "纯扫描件", 
            PDFType.MIXED: "混合型"
        }
        return (f"[{self.file_path}] 类型: {type_names[self.pdf_type]}, "
                f"总页数: {self.total_pages}, "
                f"文本页: {self.text_pages}, 扫描页: {self.scan_pages}, "
                f"不确定: {self.ambiguous_pages}")


class PDFTypeDetector:
    """
    PDF类型检测器 v2.0

    核心设计思想：
    ────────────────────────────────────────────────
    判断一个PDF是否为"纯文本"，关键不在于"有没有图"，
    而在于"文字内容是否以可选择/可搜索的原生形式存在"。

    大多数正式文档（合同、报告、发票）都会包含：
      • 公司 logo（页眉）
      • 页码/签名（页脚）
      • 电子签章
      • 小型图标

    这些都不应该影响类型判断。

    判断信号（按优先级排列）：
    ────────────────────────────────────────────────
    1. 【强信号】字体存在性
       - PyMuPDF的 get_text("dict") 返回每个文字span的字体信息
       - 有字体 = 原生文本层（即使同时有图片也是text_dominant）
       - 无字体 = 图片/扫描（除非是特殊生成方式）

    2. 【核心信号】有效文本密度
       - 每页提取到的有意义文本量
       - >200字/页 → 基本确定是文本页
       - <20字/页 → 基本确定是扫描页

    3. 【辅助信号】有效图像覆盖率
       - 排除小logo/水印后的大图面积占比
       - >50% 且文本少 → 扫描页
       - <10% → 忽略不计
    """

    # ═════════════════════════════════════════════
    # 阈值配置（可根据实际数据调优）
    # ═════════════════════════════════════════════

    # 文本密度阈值（每页清理后文本字符数）
    TEXT_DENSE_MIN = 200               # 高于此值 → 明确是文本页
    TEXT_SPARSE_MAX = 20               # 低于此值 → 明确无文本（扫描页）
    
    # 装饰性图片过滤
    DECORATIVE_IMAGE_RATIO = 0.05      # 占页面面积 < 5% 视为装饰图
    HEADER_FOOTER_HEIGHT_RATIO = 0.12  # 页面顶部/底部 12% 区域视为页眉页脚区
    MARGIN_WIDTH_RATIO = 0.08          # 左右边距 8% 区域视为边缘

    # 全局类型判断阈值
    TEXT_DOMINANT_RATIO = 0.7          # ≥70%的页是文本页 → 整体归为纯文本
    SCAN_DOMINANT_RATIO = 0.7          # ≥70%的页是扫描页 → 整体归为扫描件
    SIGNIFICANT_IMAGE_COVERAGE_LOW = 0.10  # 总有效图像覆盖率 < 10% 可忽略

    def __init__(self,
                 verbose: bool = False):
        self.verbose = verbose

    def detect(self, pdf_path: str) -> PDFAnalysisResult:
        """检测PDF文档类型"""
        try:
            doc = fitz.open(pdf_path)
            total_pages = len(doc)

            if total_pages == 0:
                return self._empty_result(pdf_path)

            pages_detail: List[PageDiagnosis] = []
            text_page_count = 0
            scan_page_count = 0
            ambiguous_count = 0
            total_sig_img_area = 0.0
            total_page_area = 0.0

            for page_num in range(total_pages):
                page = doc[page_num]
                diagnosis = self._diagnose_page(page, page_num)
                pages_detail.append(diagnosis)

                # 累计统计
                page_rect = page.rect
                total_page_area += page_rect.width * page_rect.height
                total_sig_img_area += diagnosis.significant_image_ratio * page_rect.width * page_rect.height

                if diagnosis.judgment == "text":
                    text_page_count += 1
                elif diagnosis.judgment == "scan":
                    scan_page_count += 1
                else:
                    ambiguous_count += 1

                if self.verbose:
                    logger.debug(
                        f"  P{page_num+1}: text={diagnosis.text_length}chars, "
                        f"fonts={diagnosis.has_fonts}, "
                        f"images={diagnosis.total_images}→{diagnosis.significant_images}(sig), "
                        f"sig_img_ratio={diagnosis.significant_image_ratio:.1%}, "
                        f"→ {diagnosis.judgment}"
                    )

            doc.close()

            # 计算全局指标
            sig_image_coverage = total_sig_img_area / total_page_area if total_page_area > 0 else 0.0
            checked = total_pages

            # 计算文本置信度
            avg_text = sum(d.text_length for d in pages_detail) / max(checked, 1)
            text_confidence = min(1.0, avg_text / 500)

            # 综合判断PDF类型
            pdf_type = self._determine_type_v2(
                text_pages=text_page_count,
                scan_pages=scan_page_count,
                ambiguous=ambiguous_count,
                total=checked,
                sig_image_coverage=sig_image_coverage,
                text_confidence=text_confidence,
                pages_detail=pages_detail,
            )

            result = PDFAnalysisResult(
                file_path=pdf_path,
                pdf_type=pdf_type,
                text_pages=text_page_count,
                scan_pages=scan_page_count,
                ambiguous_pages=ambiguous_count,
                total_pages=total_pages,
                text_confidence=text_confidence,
                significant_image_coverage=sig_image_coverage,
                pages_detail=pages_detail,
            )

            if self.verbose:
                logger.info(f"检测结果: {result}")

            return result

        except Exception as e:
            logger.error(f"检测PDF类型失败 [{pdf_path}]: {e}")
            raise

    def _empty_result(self, pdf_path: str) -> PDFAnalysisResult:
        return PDFAnalysisResult(
            file_path=pdf_path,
            pdf_type=PDFType.TEXT_ONLY,
            text_pages=0, scan_pages=0, ambiguous_pages=0,
            total_pages=0, text_confidence=0.0,
            significant_image_coverage=0.0,
        )

    # ═════════════════════════════════════════════
    # 单页诊断 —— 核心方法
    # ═════════════════════════════════════════════

    def _diagnose_page(self, page, page_num: int) -> PageDiagnosis:
        """对单页进行全面诊断，返回多维度的分析结果"""

        page_rect = page.rect
        pw, ph = page_rect.width, page_rect.height

        # ── 1. 提取文本并分析 ──
        raw_text = page.get_text("text")
        clean_text = self._clean_text(raw_text)

        # 提取带详细信息的文本字典（包含字体信息）
        text_dict = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        has_fonts = self._check_has_fonts(text_dict)

        # ── 2. 检测图像并过滤装饰性图片 ──
        all_images = self._get_image_rects(page)
        significant_images = []
        for img_info in all_images:
            if self._is_significant_image(img_info, pw, ph):
                significant_images.append(img_info)

        # 计算有效图像面积占比
        sig_img_area = sum(rect.width * rect.height for rect in significant_images)
        sig_img_ratio = sig_img_area / (pw * ph) if (pw * ph) > 0 else 0.0

        # ── 3. 检测文本是否只在页眉页脚区域 ──
        is_header_footer_only = self._is_text_in_header_footer_only(text_dict, pw, ph)

        # ── 4. 综合判断本页类型 ──
        judgment = self._judge_single_page(
            text_len=len(clean_text),
            has_fonts=has_fonts,
            sig_img_ratio=sig_img_ratio,
            is_header_footer_only=is_header_footer_only,
            total_images=len(all_images),
            sig_images=len(significant_images),
        )

        return PageDiagnosis(
            page_num=page_num,
            text_length=len(clean_text),
            raw_text_length=len(raw_text.strip()),
            has_fonts=has_fonts,
            total_images=len(all_images),
            significant_images=len(significant_images),
            significant_image_ratio=sig_img_ratio,
            is_header_footer_only=is_header_footer_only,
            judgment=judgment,
        )

    def _get_image_rects(self, page):
        """
        获取页面中所有图像的位置和尺寸矩形
        
        使用 get_image_info() 获取每个图像的详细信息（包括bbox），
        这是 PyMuPDF 1.27+ 推荐的方式，返回精确的渲染位置。
        """
        try:
            infos = page.get_image_info()
            rects = []
            for info in infos:
                bbox = info.get("bbox")
                if bbox and len(bbox) == 4:
                    rects.append(fitz.Rect(bbox[0], bbox[1], bbox[2], bbox[3]))
            return rects
        except Exception as e:
            if self.verbose:
                logger.debug(f"get_image_info() 失败: {e}，降级为空列表")
            return []

    def _is_significant_image(self, rect, page_w: float, page_h: float) -> bool:
        """
        判断一个图像是否为"有效的"（非装饰性的）

        过滤规则：
        1. 面积太小 (< 5% 页面) → 装饰图，忽略
        2. 完全位于页眉区域（顶部 12%）→ 可能是header logo，忽略
        3. 完全位于页脚区域（底部 12%）→ 可能是footer icon，忽略
        4. 其他情况 → 有效图像
        """
        area = rect.width * rect.height
        page_area = page_w * page_h
        area_ratio = area / page_area if page_area > 0 else 0

        # 规则1：面积太小 → 装饰性
        if area_ratio < self.DECORATIVE_IMAGE_RATIO:
            return False

        # 规则2&3：位于页眉或页脚区域
        header_limit = page_h * self.HEADER_FOOTER_HEIGHT_RATIO
        footer_start = page_h * (1 - self.HEADER_FOOTER_HEIGHT_RATIO)

        # 完全在页眉区
        if rect.y1 <= header_limit:
            return False

        # 完全在页脚区
        if rect.y0 >= footer_start:
            return False

        # 通过所有过滤 → 有效图像
        return True

    def _check_has_fonts(self, text_dict: Dict[str, Any]) -> bool:
        """
        检查页面文本是否有字体信息
        
        这是区分原生文本PDF vs 扫描件的最强信号：
        - 原生文本PDF：每个文字span都有font属性（如"SimSun", "Arial"等）
        - 扫描件PDF：没有文本，或者文本是通过OCR后嵌入的（可能也有font）
        
        但对于我们的场景：
        - 有大量文本+有字体 → 100%是原生文本页
        - 无文本/极少文本 → 不管有没有font都是扫描/空页
        """
        blocks = text_dict.get("blocks", [])
        for block in blocks:
            if block.get("type") != 0:  # 0=文本块
                continue
            lines = block.get("lines", [])
            for line in lines:
                spans = line.get("spans", [])
                for span in spans:
                    font = span.get("font", "")
                    text = span.get("text", "").strip()
                    # 有字体名且文本不为空 → 原生文本存在
                    if font and text and len(text) > 2:
                        return True
        return False

    def _is_text_in_header_footer_only(self, text_dict: Dict[str, Any],
                                        page_w: float, page_h: float) -> bool:
        """检测文本是否仅存在于页眉页脚区域"""
        blocks = text_dict.get("blocks", [])
        header_limit = page_h * self.HEADER_FOOTER_HEIGHT_RATIO
        footer_start = page_h * (1 - self.HEADER_FOOTER_HEIGHT_RATIO)

        body_text_found = False
        total_chars = 0

        for block in blocks:
            if block.get("type") != 0:
                continue
            # 获取块的边界框
            bbox = block.get("bbox", [0, 0, 0, 0])
            _, y0, _, y1 = bbox[:4]

            chars = self._count_block_text(block)
            total_chars += chars

            # 文本块的主体位于页面中间区域（非页眉页脚）
            block_mid_y = (y0 + y1) / 2
            if header_limit < block_mid_y < footer_start and chars > 5:
                body_text_found = True
                break

        # 只有少量字符且都在页眉页脚 → 视为页眉页脚only
        return (not body_text_found) and (0 < total_chars < 100)

    @staticmethod
    def _count_block_text(block: Dict) -> int:
        """计算一个文本块中的字符总数"""
        count = 0
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                count += len(span.get("text", ""))
        return count

    # ═════════════════════════════════════════════
    # 单页判断逻辑
    # ═════════════════════════════════════════════

    def _judge_single_page(self, text_len: int, has_fonts: bool,
                           sig_img_ratio: float, is_header_footer_only: bool,
                           total_images: int, sig_images: int) -> str:
        """
        对单页做出三选一判断：text / scan / ambiguous

        决策树：
        ┌────────────────────────────────────────┐
        │  文本量 >= TEXT_DENSE_MIN (200字)?      │──Yes──→ ✅ text
        └────────────────┬───────────────────────┘
                         │ No
        ┌────────────────▼───────────────────────┐
        │  文本量 <= TEXT_SPARSE_MAX (20字)?     │──Yes──→ ⚠️ check more
        └────────────────┬───────────────────────┘
                         │ No
        ┌────────────────▼───────────────────────┐
        │  20 < 文本 < 200                       │
        │  ├─ has_fonts? Yes → text             │
        │  ├─ has_fonts? No + header_only? → scan│
        │  └─ otherwise → ambiguous             │
        └────────────────────────────────────────┘
        """

        # 强文本信号
        if text_len >= self.TEXT_DENSE_MIN:
            return "text"

        # 弱/无文本信号
        if text_len <= self.TEXT_SPARSE_MAX:
            # 几乎没有文本 → 很可能是扫描页
            # 但要排除完全空白的页面
            if sig_img_ratio > 0.1 or total_images > 0:
                return "scan"
            elif text_len <= 5:
                # 完全空白或接近空白
                return "scan"
            else:
                # 极少文本，可能是页码之类的
                if is_header_footer_only:
                    return "scan"
                return "ambiguous"

        # 中间地带：20~200 字符
        if has_fonts:
            # 有字体信息说明是原生文本，只是这页内容少
            return "text"
        elif is_header_footer_only:
            # 只有页眉页脚有点文字 → 这页主体是扫描/图片
            return "scan"
        elif sig_img_ratio > 0.3:
            # 有较多有效图像 → scan
            return "scan"
        else:
            return "ambiguous"

    # ═════════════════════════════════════════════
    # 全局类型判断
    # ═════════════════════════════════════════════

    def _determine_type_v2(self, text_pages: int, scan_pages: int,
                            ambiguous: int, total: int,
                            sig_image_coverage: float,
                            text_confidence: float,
                            pages_detail: List[PageDiagnosis]) -> PDFType:
        """
        基于逐页结果综合判断整个PDF的类型

        核心原则：
        - 宁可把 mixed 判成 text_only（用OCR多跑一遍），也不要把 text_only 判成 scan（丢失准确文本）
        - 因为 PyMuPDF 对纯文本100%正确，而OCR总有出错风险
        """
        if total == 0:
            return PDFType.TEXT_ONLY

        text_ratio = text_pages / total
        scan_ratio = scan_pages / total

        # ── 情况1：绝大多数页都是文本页 → 纯文本 ──
        if text_ratio >= self.TEXT_DOMINANT_RATIO:
            # 即使有少量扫描页，整体仍优先走PyMuPDF
            # （那几页扫描的内容会丢失，但保证大部分准确）
            # 如果需要更精细处理可以归为MIXED
            if scan_pages > 0 and scan_pages >= 2:
                # 有超过2页的扫描内容 → 归为混合型确保完整
                return PDFType.MIXED
            return PDFType.TEXT_ONLY

        # ── 情况2：绝大多数页都是扫描页 → 纯扫描件 ──
        if scan_ratio >= self.SCAN_DOMINANT_RATIO:
            return PDFType.SCAN_ONLY

        # ── 情况3：有效图像覆盖率很低，主要是文本 → 纯文本 ──
        if (sig_image_coverage < self.SIGNIFICANT_IMAGE_COVERAGE_LOW
                and text_ratio > 0.4
                and text_confidence > 0.15):
            return PDFType.TEXT_ONLY

        # ── 情况4：文本和扫描都不少 → 混合型 ──
        return PDFType.MIXED

    # ═════════════════════════════════════════════
    # 工具方法
    # ═════════════════════════════════════════════

    def _clean_text(self, text: str) -> str:
        """清理提取的文本，去除空白和乱码"""
        if not text:
            return ""

        lines = [line.strip() for line in text.split('\n') if line.strip()]
        cleaned = ' '.join(lines)

        # 过滤乱码行——保留包含中文或英文的行
        valid_lines = []
        for segment in cleaned.split(' '):
            if re.search(r'[\u4e00-\u9fff\u0041-\u007a]', segment):
                valid_lines.append(segment)

        return ' '.join(valid_lines)


# ══════════════════════════════════════════════════
# 批量处理入口
# ══════════════════════════════════════════════════

def batch_detect_pdfs(pdf_paths: List[str],
                      verbose: bool = False) -> List[PDFAnalysisResult]:
    """批量检测多个PDF文件"""
    detector = PDFTypeDetector(verbose=verbose)
    results = []

    for pdf_path in pdf_paths:
        try:
            result = detector.detect(pdf_path)
            results.append(result)
        except Exception as e:
            logger.error(f"处理文件失败 [{pdf_path}]: {e}")

    return results


# 测试代码
if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        test_file = sys.argv[1]
        detector = PDFTypeDetector(verbose=True)
        result = detector.detect(test_file)

        print(f"\n{'='*70}")
        print(f"文件: {result.file_path}")
        print(f"{'='*70}")
        print(f"类型: {result.pdf_type.value}")
        print(f"总页数: {result.total_pages}")
        print(f"  文本页: {result.text_pages}")
        print(f"  扫描页: {result.scan_pages}")
        print(f"  不确定: {result.ambiguous_pages}")
        print(f"文本置信度: {result.text_confidence:.2f}")
        print(f"有效图像覆盖率: {result.significant_image_coverage:.2f}")

        print(f"\n--- 逐页详情 ---")
        for p in result.pages_detail:
            print(f"  P{p.page_num+1:3d}: {p.text_length:5d}chars | "
                  f"fonts={'Y' if p.has_fonts else 'N'} | "
                  f"imgs={p.total_images}→{p.significant_images}sig | "
                  f"sig_ratio={p.significant_image_ratio:.1%} | "
                  f"→ {p.judgment}")

        print(f"\n建议处理方式:")
        if result.pdf_type == PDFType.TEXT_ONLY:
            print("  → 使用 PyMuPDF 直接提取文本（快速、准确）")
        elif result.pdf_type == PDFType.SCAN_ONLY:
            print("  → 使用 MinerU API 进行 OCR 识别")
        elif result.pdf_type == PDFType.MIXED:
            print("  → 使用 MinerU API 处理（确保扫描部分被识别）")
