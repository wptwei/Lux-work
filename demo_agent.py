"""命令行演示：多 Agent 事件处置闭环（需本机 Ollama）。"""
from __future__ import annotations

import sys

from core.orchestrator import SecurityOrchestrator
from core.providers import configured_llm
from core.redline_guard import RedlineGuard


def main() -> None:
    event = sys.argv[1] if len(sys.argv) > 1 else "有人闯入H区东门"
    orchestrator = SecurityOrchestrator(
        guard=RedlineGuard(),
        llm_fn=configured_llm,
    )
    result = orchestrator.run(event)
    print(result["report"])
    print("\n--- 阶段轨迹 ---")
    for step in result["trace"]:
        print(step)


if __name__ == "__main__":
    main()
