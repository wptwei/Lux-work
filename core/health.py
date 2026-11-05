"""v0.6 运行健康检查：模型、磁盘与本地数据状态。"""
from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from core.paths import DATA_DIR, PROJECT_ROOT, describe
from core.settings import settings




def _ollama_tags_url() -> str:
    parts = urlsplit(settings.ollama_url)
    path = parts.path
    if path.endswith("/api/chat"):
        path = path[: -len("/api/chat")] + "/api/tags"
    else:
        path = "/api/tags"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def model_health(timeout: float = 2.0) -> dict:
    started = time.perf_counter()
    if settings.is_external_model:
        configured = bool(os.getenv("DEEPSEEK_API_KEY", "").strip())
        return {
            "ok": configured,
            "label": "DeepSeek 公网模型",
            "detail": "密钥已配置" if configured else "缺少 API 密钥",
            "latency_ms": 0,
        }

    try:
        with urllib.request.urlopen(_ollama_tags_url(), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        names = {str(item.get("name", "")) for item in data.get("models", [])}
        selected = settings.ollama_model
        installed = selected in names or any(name.split(":")[0] == selected.split(":")[0] for name in names)
        latency = int((time.perf_counter() - started) * 1000)
        return {
            "ok": installed,
            "label": f"Ollama / {selected}",
            "detail": f"服务在线 · {latency} ms" if installed else "服务在线，但未发现所选模型",
            "latency_ms": latency,
        }
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "label": f"Ollama / {settings.ollama_model}",
            "detail": f"连接失败：{str(exc)[:80]}",
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }


def system_health() -> dict:
    disk = shutil.disk_usage(PROJECT_ROOT)
    data_dir = DATA_DIR
    database_bytes = sum(p.stat().st_size for p in data_dir.glob("*.db*") if p.is_file())
    return {
        "model": model_health(),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_used_percent": round((disk.used / disk.total) * 100, 1),
        "database_mb": round(database_bytes / (1024**2), 2),
        "host": settings.host,
        "port": settings.port,
        "mode": "公网模型" if settings.is_external_model else "本地模型",
        "paths": describe(),
    }
