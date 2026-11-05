"""
本地 OCR —— 用 Windows 自带离线识别（中文），图片不出本机、不联网。
用途：看板/表格/文档截图 → 本地抠出文字 → 文字过红线后才允许发模型。
注意：监控画面、厂区布局、花名册等图片本身仍属红线，不应上传识别。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

def _clean_cn_spaces(text: str) -> str:
    """去掉 Windows OCR 在中文字符间误加的空格，保留数字/英文间的空格。"""
    cn = r"\u4e00-\u9fff，。、；：？！“”‘’（）《》【】·×"
    # 中文/中文标点 与 中文 之间的空格，反复收敛
    prev = None
    while prev != text:
        prev = text
        text = re.sub(rf"([{cn}])[ \t]+(?=[{cn}0-9])", r"\1", text)
        text = re.sub(rf"([0-9{cn}])[ \t]+(?=[{cn}])", r"\1", text)
    return text


def ocr_image(image_path: str | Path, lang: str = "zh-Hans-CN") -> str:
    """识别单张图片，返回纯文字。全程本地，不联网。"""
    try:
        from PIL import Image
        from winocr import recognize_pil
    except ImportError as exc:
        raise RuntimeError("OCR 组件未安装；请在 Windows 环境执行 pip install -r requirements.txt") from exc
    img = Image.open(str(image_path))
    if img.mode not in ("RGB", "RGBA", "L"):
        img = img.convert("RGB")

    async def _run():
        return await recognize_pil(img, lang)

    result = asyncio.run(_run())
    return _clean_cn_spaces(result.text or "").strip()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("用法: python core/ocr.py <图片路径>")
    else:
        print(ocr_image(sys.argv[1]))
