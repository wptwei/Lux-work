"""
红线守卫 + DeepSeek 演示。
跑法：
  1. 先设 key（PowerShell）:  $env:DEEPSEEK_API_KEY="sk-你的key"
  2. python demo.py
没配 key 会自动用假模型演示流程，配了就真调 DeepSeek。
"""
from core.providers import deepseek_llm
from core.redline_guard import RedlineGuard

guard = RedlineGuard()


def get_llm_fn():
    """配了真key就用真模型，否则用假模型演示流程。"""
    import os
    if os.environ.get("DEEPSEEK_API_KEY"):
        print(">>> 使用真模型 DeepSeek\n")
        return deepseek_llm
    print(">>> 未检测到 DEEPSEEK_API_KEY，使用假模型演示流程\n")

    def fake_llm(prompt: str, system: str = "") -> str:
        return f"[假模型处理] 收到：{prompt[:60]}..."

    return fake_llm


llm = get_llm_fn()


def show(title, result):
    print("=" * 60)
    print(title)
    print("-" * 60)
    print("状态:", result["status"])
    if result["status"] == "ok":
        print("处理级别:", result["level"], "| 是否脱敏:", result["masked"])
        print("输出:", result["output"])
        if result["input_hits"]:
            print("命中规则:", [h["category"] for h in result["input_hits"]])
    else:
        print(result["reason"])
        print("命中:", [h["category"] for h in result.get("hits", [])])
    print()


# 场景1：干净内容 → 直接放行走公网
show("场景1 干净内容", guard.call_llm(
    prompt="帮我写一份秋季安保培训通知，语气正式，300字以内。",
    task_desc="写培训通知",
    llm_fn=llm,
))

# 场景2：含工号/邮箱 → 自动脱敏后送公网，回来再还原
show("场景2 含工号邮箱", guard.call_llm(
    prompt="帮我整理一下：员工工号12345678今天提交了巡检记录，联系邮箱是 test@example.com。",
    task_desc="整理巡检记录",
    llm_fn=llm,
))

# 场景3：含身份证 → 直接拦截，不送公网
show("场景3 含身份证号", guard.call_llm(
    prompt="帮我分析身份证号 340200199001011234 的门禁进出记录。",
    task_desc="分析门禁记录",
    llm_fn=llm,
))
