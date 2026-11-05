from __future__ import annotations

import argparse
import importlib
import json
import os
import urllib.error
import urllib.request


def _load_dotenv() -> None:
    """加载项目根目录 .env（与 start_web.ps1 一致），已存在的环境变量优先。

    healthcheck 是独立 CLI，不经过 start_web.ps1，这里补一次加载，否则
    ``--web-search`` 探测读不到 LUX_WEB_SEARCH_ENABLED / TAVILY_API_KEY。
    密钥文件位置由 LUX_ENV_FILE 决定（默认仍是项目根目录 .env）。
    """
    from core.paths import ENV_FILE
    env_file = ENV_FILE
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip())


_load_dotenv()

from core.settings import settings


def _check_web_search(probe: bool) -> list[str]:
    """联网搜索专项：默认只报配置状态，不产生付费请求；--web-search 才做真实探测。"""
    from core.web_search import web_search_configured, web_search_status, search_web, WebSearchError

    print(f"[INFO] {web_search_status()}")
    if not settings.web_search_enabled:
        return []
    failures: list[str] = []
    configured, reason = web_search_configured()
    if not configured:
        failures.append(reason)
        return failures
    if not probe:
        print("[OK] 联网搜索已配置（未发起测试请求；加 --web-search 可做真实探测）")
        return failures
    try:
        results = search_web("联网搜索连通性检查", max_results=1, timeout=min(settings.web_search_timeout, 15))
    except WebSearchError as exc:
        failures.append(str(exc))
        return failures
    print(f"[OK] 联网搜索探测成功，返回 {len(results)} 条结果")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="LUX 依赖与配置健康检查")
    parser.add_argument("--web-search", action="store_true",
                        help="真实发起一次联网搜索（会产生付费请求，仅验收时使用）")
    args = parser.parse_args()

    failures = []
    for module in ("gradio", "openpyxl", "docx", "pptx", "PIL"):
        try:
            importlib.import_module(module)
            print(f"[OK] Python 依赖：{module}")
        except ImportError:
            failures.append(f"缺少 Python 依赖：{module}")

    try:
        # 与 app.py 启动时一致：账号库里有启用账号，就不再要求 .env 里的旧共享账号。
        auth_ready = False
        try:
            from core.auth import UserStore
            auth_ready = UserStore().has_enabled_users()
        except Exception as exc:  # 账号库损坏时报出来，而不是让健康检查误判为“未配置账号”
            failures.append(f"账号库不可读取：{exc}")
        settings.validate(auth_ready=auth_ready)
        print(f"[OK] 访问配置：{settings.host}:{settings.port}")
    except ValueError as exc:
        failures.append(str(exc))

    # 运行目录：确认代码、数据、配置、日志、密钥各在其位（不输出任何密钥值）。
    from core.paths import describe
    for name, value in describe().items():
        print(f"[INFO] {name}：{value}")

    if settings.is_external_model:
        import os
        if os.getenv("DEEPSEEK_API_KEY"):
            print("[OK] 外网模型密钥已配置（未发起测试请求）")
        else:
            failures.append("LUX_MODEL_MODE=external，但缺少 DEEPSEEK_API_KEY")
    else:
        tags_url = settings.ollama_url.rsplit("/api/", 1)[0] + "/api/tags"
        try:
            with urllib.request.urlopen(tags_url, timeout=5) as response:
                data = json.loads(response.read().decode("utf-8"))
            names = {item.get("name", "") for item in data.get("models", [])}
            if settings.ollama_model in names:
                print(f"[OK] Ollama 模型：{settings.ollama_model}")
            else:
                failures.append(f"Ollama 已运行，但未找到模型 {settings.ollama_model}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            failures.append(f"无法连接 Ollama：{exc}")

    failures.extend(_check_web_search(args.web_search))

    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1
    print("[OK] 健康检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
