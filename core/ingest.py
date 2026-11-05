"""文件接入闸门：先看文件名/大小，再解析内容，再做内容红线检查。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from core.file_reader import read_file
from core.ocr import ocr_image
from core.image_classifier import classify_image
from core.redline_guard import RedlineGuard
from core.review_queue import ReviewQueue
from core.settings import settings
from core.providers import ModelProviderError, ollama_vision


@dataclass(frozen=True)
class IngestResult:
    ok: bool
    text: str = ""
    message: str = ""
    level: str = "allow"
    hit_count: int = 0
    review_id: str = ""
    classification: str = ""


def _path(value) -> Path:
    if isinstance(value, (str, Path)):
        return Path(value)
    raw = getattr(value, "name", value)
    return Path(str(raw))


def _precheck(
    value,
    guard: RedlineGuard,
    extensions: set[str],
    region: str | None = None,
    max_mb: int | None = None,
) -> tuple[Path | None, str]:
    path = _path(value)
    if not path.is_file():
        return None, "文件不存在或已失效"
    if path.suffix.lower() not in extensions:
        return None, f"不支持的文件类型：{path.suffix.lower() or '无扩展名'}"
    limit_mb = max_mb if max_mb is not None else settings.max_file_mb
    if path.stat().st_size > limit_mb * 1024 * 1024:
        return None, f"文件超过 {limit_mb}MB 限制"
    level, _, _ = guard.scan(file_path=str(path), source_type="file_metadata", region=region)
    if level == "block":
        return None, "文件名命中红线，未读取文件内容"
    return path, ""


def ingest_document(
    value,
    guard: RedlineGuard,
    region: str | None = None,
    review_queue: ReviewQueue | None = None,
    *,
    extensions: set[str] | None = None,
    max_mb: int | None = None,
) -> IngestResult:
    path, error = _precheck(
        value,
        guard,
        extensions or {".docx", ".xlsx", ".xlsm", ".pptx", ".txt", ".csv", ".md"},
        region,
        max_mb=max_mb,
    )
    if error:
        return IngestResult(False, message=error, level="block")
    try:
        full_text = read_file(str(path))
    except Exception as exc:
        return IngestResult(False, message=f"文件解析失败：{exc}")
    # 先扫完整解析结果，再截取给输入框，防止红线内容藏在长文件尾部。
    level, hits, _ = guard.scan(full_text, file_path=str(path), source_type="document", region=region)
    if level == "block":
        return IngestResult(False, message="文件内容命中红线，未加入输入框", level=level, hit_count=len(hits))
    if level == "review":
        review_id = review_queue.submit(
            path, region=guard.region_policy.get(region)["name"], asset_type="文档",
            risk_level="疑似受控文档", evidence=[h["category"] for h in hits],
        ) if review_queue else ""
        suffix = f"，审核编号 {review_id}" if review_id else ""
        return IngestResult(False, message=f"文件疑似包含受控资产，需人工复核{suffix}，未加入输入框", level=level, hit_count=len(hits), review_id=review_id)
    text = full_text[: settings.max_input_chars]
    return IngestResult(True, text=text, message="文件已在本机解析", level=level, hit_count=len(hits))


def ingest_image(value, guard: RedlineGuard, region: str | None = None, review_queue: ReviewQueue | None = None) -> IngestResult:
    path, error = _precheck(value, guard, {".png", ".jpg", ".jpeg", ".bmp", ".webp"}, region)
    if error:
        return IngestResult(False, message=error, level="block")
    try:
        full_text = ocr_image(str(path))
    except Exception as exc:
        return IngestResult(False, message=f"图片识别失败：{exc}")
    level, hits, _ = guard.scan(full_text, file_path=str(path), source_type="image_ocr", region=region)
    policy = guard.region_policy.get(region)
    image_risk = classify_image(path, full_text, float(policy.get("image_review_threshold", 0.58)))
    if image_risk.is_risky:
        hits.append({"category": image_risk.label, "term": "、".join(image_risk.evidence), "action": "review"})
        if level != "block":
            level = "review"
    if level == "block":
        return IngestResult(False, message="识别文字命中红线，未加入输入框", level=level, hit_count=len(hits))
    if level == "review":
        review_id = review_queue.submit(
            path, region=policy["name"], asset_type="图片", risk_level=image_risk.label if image_risk.is_risky else "OCR/文件名风险",
            confidence=image_risk.score, evidence=image_risk.evidence or tuple(h["category"] for h in hits),
        ) if review_queue else ""
        suffix = f"，审核编号 {review_id}" if review_id else ""
        return IngestResult(False, message=f"图片疑似包含受控资产，需人工复核{suffix}，未加入输入框", level=level, hit_count=len(hits), review_id=review_id, classification=image_risk.label)
    visual_text = ""
    vision_note = ""
    if settings.vision_enabled:
        try:
            visual_text = ollama_vision(str(path))
            vision_level, vision_hits, safe_visual = guard.scan(
                visual_text,
                file_path=str(path),
                source_type="vision_output",
                region=region,
            )
            hits.extend(vision_hits)
            if vision_level == "block":
                return IngestResult(
                    False,
                    message="视觉模型结果命中红线，图片未加入任务",
                    level="block",
                    hit_count=len(hits),
                )
            visual_text = safe_visual
        except ModelProviderError as exc:
            # 视觉模型缺失或暂不可用时继续保留 OCR 能力，不阻塞一线任务。
            vision_note = f"；视觉理解暂不可用（{exc}），已使用 OCR"

    parts = []
    if visual_text.strip():
        parts.append("【本地视觉模型理解】\n" + visual_text.strip())
    if full_text.strip():
        parts.append("【本地 OCR 文字】\n" + full_text.strip())
    if not parts:
        parts.append("【图片附件】未识别到可描述内容")
    text = "\n\n".join(parts)[: settings.max_input_chars]
    message = f"图片已在本机完成安全检查与解析{vision_note}"
    return IngestResult(True, text=text, message=message, level=level, hit_count=len(hits), classification=image_risk.label)


WORD_CONVERT_MAX_MB = 50


def ingest_word_for_conversion(
    value,
    guard: RedlineGuard,
    region: str | None = None,
    review_queue: ReviewQueue | None = None,
) -> IngestResult:
    """Word→PDF 转换前的红线闸门：文件名与正文都检查，未通过不交给 Word 打开。

    旧版 .doc 是二进制格式，本机无法在转换前读取正文，因此不放行；
    请使用者另存为 .docx 后再转换。
    """
    path = _path(value)
    if path.suffix.lower() == ".doc":
        return IngestResult(
            False,
            message="旧版 .doc 无法在转换前做红线检查，请先另存为 .docx 后再转换",
            level="block",
        )
    return ingest_document(
        value, guard, region=region, review_queue=review_queue,
        extensions={".docx"}, max_mb=WORD_CONVERT_MAX_MB,
    )
