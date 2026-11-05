"""完全本地的图片风险初筛。

这是可解释的启发式分类器，不把“疑似”表述成确定结论。它综合 OCR 标记、
暗色画面占比和线稿边缘密度，筛出可能的监控截图或布局图交给人工复核。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat


@dataclass(frozen=True)
class ImageRisk:
    label: str
    score: float
    evidence: tuple[str, ...]

    @property
    def is_risky(self) -> bool:
        return self.label != "普通图片"


def classify_image(path: str | Path, ocr_text: str = "", threshold: float = 0.58) -> ImageRisk:
    with Image.open(path) as source:
        image = source.convert("RGB")
        image.thumbnail((640, 640))
        gray = image.convert("L")
        histogram = gray.histogram()
        pixels = max(1, image.width * image.height)
        dark_ratio = sum(histogram[:70]) / pixels
        edge_mean = ImageStat.Stat(gray.filter(ImageFilter.FIND_EDGES)).mean[0] / 255
        saturation = ImageStat.Stat(image.convert("HSV").getchannel("S")).mean[0] / 255

    lowered = (ocr_text or "").lower()
    monitor_terms = [t for t in ("cctv", "cam", "通道", "摄像头", "录像", "时间戳", "实时预览") if t in lowered]
    layout_terms = [t for t in ("平面图", "楼层图", "比例尺", "房间编号", "疏散路线", "出入口") if t in lowered]

    monitor_score = min(0.98, len(monitor_terms) * 0.24 + dark_ratio * 0.50 + max(0, 0.25 - saturation) * 0.30)
    layout_score = min(0.98, len(layout_terms) * 0.26 + edge_mean * 1.35 + max(0, 0.22 - saturation) * 0.28)

    if monitor_score >= threshold and monitor_score >= layout_score:
        evidence = tuple(monitor_terms[:3]) + (f"暗色画面占比{dark_ratio:.0%}",)
        return ImageRisk("疑似监控画面", round(monitor_score, 3), evidence)
    if layout_score >= threshold:
        evidence = tuple(layout_terms[:3]) + (f"线稿边缘密度{edge_mean:.0%}",)
        return ImageRisk("疑似厂区布局图", round(layout_score, 3), evidence)
    return ImageRisk("普通图片", round(max(monitor_score, layout_score), 3), ())

