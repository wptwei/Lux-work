"""使用本机 Microsoft Word 将 Word 文档高保真转换为 PDF。"""
from __future__ import annotations

import re
import sys
import uuid
from pathlib import Path

WORD_EXTENSIONS = {".doc", ".docx"}
MAX_WORD_BYTES = 50 * 1024 * 1024


class WordPdfError(RuntimeError):
    """面向界面展示的 Word 转 PDF 错误。"""


def _safe_stem(name: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" ._")
    return stem[:80] or "转换文档"


def validate_word_file(source: str | Path) -> Path:
    path = Path(source).resolve()
    if not path.is_file():
        raise WordPdfError("找不到上传的 Word 文件，请重新上传。")
    if path.suffix.lower() not in WORD_EXTENSIONS:
        raise WordPdfError("仅支持 .docx 或 .doc 格式。")
    if path.stat().st_size > MAX_WORD_BYTES:
        raise WordPdfError("文件超过 50 MB，请压缩后重试。")
    return path


def convert_word_to_pdf(source: str | Path, output_dir: str | Path = "downloads") -> Path:
    """通过独立 Word 进程导出 PDF，禁用宏并保证进程被正确释放。"""
    if sys.platform != "win32":
        raise WordPdfError("当前环境不支持 Microsoft Word 转换。")

    source_path = validate_word_file(source)
    target_dir = Path(output_dir).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / f"{_safe_stem(source_path.stem)}_{uuid.uuid4().hex[:8]}.pdf"

    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise WordPdfError("缺少本地 Word 转换组件，请联系管理员安装 pywin32。") from exc

    pythoncom.CoInitialize()
    word = None
    document = None
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        # msoAutomationSecurityForceDisable：打开上传文档时禁止宏运行。
        word.AutomationSecurity = 3
        document = word.Documents.Open(
            str(source_path),
            ConfirmConversions=False,
            ReadOnly=True,
            AddToRecentFiles=False,
        )
        document.ExportAsFixedFormat(
            OutputFileName=str(target_path),
            ExportFormat=17,  # wdExportFormatPDF
            OpenAfterExport=False,
            OptimizeFor=0,  # wdExportOptimizeForPrint
            CreateBookmarks=1,  # wdExportCreateHeadingBookmarks
        )
    except Exception as exc:
        target_path.unlink(missing_ok=True)
        raise WordPdfError(f"Word 转换失败：{exc}") from exc
    finally:
        if document is not None:
            try:
                document.Close(False)
            except Exception:
                pass
        if word is not None:
            try:
                word.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()

    if not target_path.is_file() or target_path.stat().st_size == 0:
        raise WordPdfError("Word 未生成有效 PDF，请检查文档是否损坏或被加密。")
    return target_path
