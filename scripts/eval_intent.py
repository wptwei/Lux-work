"""意图路由对比评测：固定测试集下比较旧门控、规则路由、规则+模型路由。

用法（只读，不下载模型、不切换云端、不修改配置）：

    python scripts/eval_intent.py                     # 只跑规则与旧门控，无需模型
    python scripts/eval_intent.py --model qwen2.5:3b  # 额外跑一次真实模型路由
    python scripts/eval_intent.py --check-models      # 只列出已安装模型与资源信息

指标口径：
- 准确率：action 命中即为对；case 指定 expected_scope 时 scope 也必须一致。
- 误触发检索率：应做别的事却走了文件检索——这是最需要压低的错误。
- 指代成功率：带 expected_target 的用例里 target 是否对应真实候选 ID。
- 约束保留率：给定历史约束时，新轮次状态里约束是否仍在。
- 延迟：单次路由耗时（模型路由才计入网络+推理时间）。

不记录用户原文到台账；输出只进终端。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CASES = ROOT / "tests" / "fixtures" / "intent_cases.json"


def load_cases() -> list[dict]:
    data = json.loads(CASES.read_text(encoding="utf-8"))
    return data.get("cases", [])


def _history(case: dict) -> list[dict]:
    files = case.get("files") or []
    if not files:
        return []
    cards = "".join(f'<article data-version-id="{vid}">文件</article>' for vid in files)
    return [{"role": "user", "content": "帮我找文件"},
            {"role": "assistant", "content": cards}]


def _state(case: dict):
    spec = case.get("state")
    if not spec:
        return None
    from core.conversation_state import TaskState
    return TaskState(task_id="eval", action=spec.get("action", ""),
                     object_type=spec.get("object_type", ""))


def baseline_keyword_gate(message: str, case: dict) -> str:
    """旧行为：关键词门控命中就进文件检索，否则普通对话。"""
    from core.library_retrieval import should_search_library
    return "retrieve" if should_search_library(message) else "chat"


def rules_route(message: str, case: dict):
    from core.intent_router import route_intent
    return route_intent(message, history=_history(case), state=_state(case))


def model_route(message: str, case: dict):
    from core.intent_router import route_intent
    from core.providers import configured_llm

    def model(prompt: str, system: str = "") -> str:
        return configured_llm(prompt, system=system, num_predict=256,
                              json_mode=True, temperature=0.0)

    return route_intent(message, history=_history(case), state=_state(case), model=model)


class _model_override:
    """仅在本进程内把模型名指向已安装的本地模型；不写配置、不下载、不切云端。"""

    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        import dataclasses
        import core.providers as providers
        self._module = providers
        self._original = providers.settings
        providers.settings = dataclasses.replace(self._original, ollama_model=self.name)
        return self

    def __exit__(self, *exc):
        self._module.settings = self._original
        return False


def score(cases, predict, with_route_object: bool):
    """返回 (指标, 明细)。prediction 可以是字符串或 IntentRoute。"""
    rows = []
    correct = 0
    false_retrieve = 0
    referent_total = referent_ok = 0
    latency_total = 0.0
    for case in cases:
        started = time.perf_counter()
        prediction = predict(case["message"], case)
        latency_total += time.perf_counter() - started
        action = prediction if isinstance(prediction, str) else prediction.action
        scope = "" if isinstance(prediction, str) else prediction.source_scope
        target = None if isinstance(prediction, str) else prediction.target_id

        ok = action == case["expected_action"]
        if ok and case.get("expected_scope") and scope != case["expected_scope"]:
            ok = False
        if ok and case.get("expected_relation") and isinstance(prediction, str) is False:
            if prediction.relation != case["expected_relation"]:
                ok = False
        correct += int(ok)

        if action == "retrieve" and case["expected_action"] != "retrieve":
            false_retrieve += 1

        if case.get("expected_target"):
            referent_total += 1
            referent_ok += int(target == case["expected_target"])

        rows.append({
            "id": case["id"], "category": case["category"],
            "expected": case["expected_action"], "predicted": action,
            "scope": scope, "ok": ok,
        })

    total = len(cases) or 1
    metrics = {
        "cases": len(cases),
        "accuracy": round(correct / total, 4),
        "false_retrieve_rate": round(false_retrieve / total, 4),
        "referent_success": round(referent_ok / referent_total, 4) if referent_total else None,
        "mean_latency_ms": round(latency_total / total * 1000, 2),
    }
    return metrics, rows


def constraint_retention(cases) -> float:
    """给定状态与带约束的消息时，新状态里约束是否仍然存在。"""
    from core.conversation_state import ConversationStore, extract_constraints
    store = ConversationStore()
    checked = kept = 0
    for case in cases:
        expected = extract_constraints(case["message"])
        if not expected:
            continue
        checked += 1
        case_state = _state(case)
        route = rules_route(case["message"], case)
        if case_state is not None:
            # 把给定状态灌进 store
            state = store.get("eval")
            state.action = case_state.action
            state.object_type = case_state.object_type
        state = store.apply_route("eval", route, case["message"])
        if all(state.constraints.get(k) == v for k, v in expected.items()):
            kept += 1
    return round(kept / checked, 4) if checked else 1.0


def confusion(rows) -> dict:
    table: dict[str, dict[str, int]] = {}
    for row in rows:
        table.setdefault(row["expected"], {}).setdefault(row["predicted"], 0)
        table[row["expected"]][row["predicted"]] += 1
    return table


def print_report(name: str, metrics: dict, rows) -> None:
    print(f"\n=== {name} ===")
    print(f"用例 {metrics['cases']} 条 | 准确率 {metrics['accuracy']:.1%} | "
          f"误触发检索 {metrics['false_retrieve_rate']:.1%} | "
          f"指代成功率 {metrics['referent_success']} | "
          f"平均延迟 {metrics['mean_latency_ms']} ms")
    print("混淆矩阵（行=期望，列=实际）：")
    table = confusion(rows)
    columns = sorted({p for row in table.values() for p in row})
    print("  " + "期望\\实际".ljust(12) + "".join(c.ljust(11) for c in columns))
    for expected in sorted(table):
        cells = "".join(str(table[expected].get(c, 0)).ljust(11) for c in columns)
        print("  " + expected.ljust(12) + cells)
    errors = [r for r in rows if not r["ok"]]
    if errors:
        print("错误样本：")
        for row in errors:
            print(f"  - {row['id']} [{row['category']}] 期望 {row['expected']} 实际 {row['predicted']}")


def list_models() -> None:
    """只读列出已安装模型，不下载、不切换。"""
    import urllib.request
    from urllib.parse import urlsplit, urlunsplit
    from core.settings import settings
    url = urlsplit(settings.ollama_url)
    tags = urlunsplit((url.scheme, url.netloc, "/api/tags", "", ""))
    try:
        with urllib.request.urlopen(tags, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # 只读探测，失败不影响其它评测
        print(f"[FAIL] 无法读取模型列表：{exc}")
        return
    print(f"当前配置模型：{settings.ollama_model} | num_ctx={settings.ollama_num_ctx} "
          f"| num_predict={settings.ollama_num_predict}")
    for item in data.get("models", []):
        details = item.get("details", {})
        size = item.get("size", 0) / (1024 ** 3)
        print(f"  - {item.get('name')}  {details.get('parameter_size','?')} "
              f"{details.get('quantization_level','?')}  {size:.1f} GB")


def main() -> int:
    parser = argparse.ArgumentParser(description="意图路由对比评测（只读）")
    parser.add_argument("--model", "--models", dest="models", action="append",
                        metavar="NAME",
                        help="用已安装的本地模型跑路由评测，可重复指定多个做对比；不会自动拉取模型")
    parser.add_argument("--check-models", action="store_true", help="只列出已安装模型")
    args = parser.parse_args()

    if args.check_models:
        list_models()
        return 0

    cases = load_cases()
    print(f"固定测试集：{CASES.relative_to(ROOT)}（{len(cases)} 条，版本 1）")

    reference = rules_route("你好", {"message": "你好"})
    print(f"路由版本：{reference.reason} 基线规则集")

    metrics, rows = score(cases, baseline_keyword_gate, with_route_object=False)
    print_report("基线：旧关键词门控", metrics, rows)

    metrics, rows = score(cases, rules_route, with_route_object=True)
    print_report("新方案：规则路由", metrics, rows)
    print(f"约束保留率：{constraint_retention(cases):.1%}")

    if args.models:
        for name in args.models:
            try:
                with _model_override(name):
                    metrics, rows = score(cases, model_route, with_route_object=True)
            except Exception as exc:  # 单个模型失败不打断其它模型
                print(f"\n[FAIL] 模型 {name} 路由评测失败：{exc}")
                continue
            print_report(f"新方案：规则 + 模型路由（{name}）", metrics, rows)
    else:
        print("\n未指定 --model，跳过模型路由评测（可用 --check-models 查看已安装模型）。")

    print("\n说明：以上为路由层评测，不含最终回答质量；模型对比需在同一测试集、"
          "同一上下文配置下重复运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
