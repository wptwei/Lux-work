"""界面上“数据去向”类文案的唯一来源。

页面文案必须与真实配置一致：旧版页头写死“100% AIR-GAPPED”“业务数据不出域”，
即使 LUX_MODEL_MODE=external 或开启局域网共享也照样显示。
"""
from __future__ import annotations


def boundary_labels(is_external: bool) -> dict[str, str]:
    """根据模型模式返回页面用到的数据边界文案。"""
    if is_external:
        return {
            "mode": "公网模型",
            "header_meta": "V0.6 · EXTERNAL LINK · 脱敏后外发",
            "quick_title": "公网推理",
            "quick_copy": "脱敏后外发 · 需部门批准",
            "boundary": "脱敏后发往公网模型",
            "badge": "脱敏外发",
        }
    return {
        "mode": "本地模型",
        "header_meta": "V0.6 · LOCAL CORE · 数据仅在本机处理",
        "quick_title": "本地推理",
        "quick_copy": "文本不离开本机",
        "boundary": "仅在本机处理",
        "badge": "本地运行",
    }


def weekly_copy(is_external: bool) -> dict[str, str]:
    """周报页面里“页数由谁决定”的文案。

    公网模型模式：AI 先策划页数；本机模式：页面骨架由事实类别规则生成（页数随素材变化），
    只有页面正文由 AI 创作。旧文案在两种模式下都写“AI 自主决定页数”，与代码不符。
    """
    if is_external:
        return {
            "mode_label": "智能创作（AI 自适应页数）",
            "intro": "AI 先提取事实与来源，再按本期素材自主策划页数与内容；保留 H 区历史周报的视觉风格，核对后导出可编辑 Word / PPT。",
            "panel": "模板控制视觉；AI 先提取事实与数据，再规划每页重点。",
            "empty": "在左侧导入或填写本期数据，然后点击“分析素材并生成周报”。AI 会按素材决定页面数量。",
        }
    return {
        "mode_label": "智能创作（按素材类别生成页面）",
        "intro": "AI 先提取事实与来源；本机模式下页面骨架按素材覆盖的事实类别稳定生成（页数随素材变化），页面正文由 AI 创作。保留 H 区历史周报的视觉风格，核对后导出可编辑 Word / PPT。",
        "panel": "模板控制视觉；AI 先提取事实与数据，页面骨架按事实类别生成。",
        "empty": "在左侧导入或填写本期数据，然后点击“分析素材并生成周报”。页面数量由素材覆盖的事实类别决定，正文由 AI 创作。",
    }
