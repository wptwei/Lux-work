"""模型提供方统一入口。

默认连接本机 Ollama；只有明确设置 LUX_MODEL_MODE=external 才会调用 DeepSeek。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import threading
from urllib.parse import urlsplit, urlunsplit
import urllib.error
import urllib.request
from pathlib import Path

from core.settings import settings


class ModelProviderError(RuntimeError):
    """可安全展示给界面的模型调用错误。"""


_ALLOWED_ROLES = {"system", "user", "assistant"}
_usage = threading.local()


def get_last_usage() -> dict:
    """最近一次模型调用的计数元数据（不含任何业务原文），用于诊断与评测。"""
    return dict(getattr(_usage, "value", {}) or {})


def _record_usage(data: dict) -> None:
    keys = ("prompt_eval_count", "eval_count", "total_duration", "eval_duration")
    _usage.value = {key: data[key] for key in keys if isinstance(data.get(key), int)}


def _normalize_messages(messages) -> list[dict[str, str]]:
    """校验并复制传入的 messages：角色必须合法，内容必须是非空文本。"""
    result: list[dict[str, str]] = []
    for item in messages or []:
        if not isinstance(item, dict):
            raise ModelProviderError("[schema] messages 每一项都必须是对象")
        role = item.get("role")
        content = item.get("content")
        if role not in _ALLOWED_ROLES:
            raise ModelProviderError("[schema] messages 角色只能是 system / user / assistant")
        if not isinstance(content, str):
            raise ModelProviderError("[schema] messages 内容必须是文本")
        if content.strip():
            result.append({"role": role, "content": content})
    if not result:
        raise ModelProviderError("[schema] messages 不能为空")
    return result


def _chat_messages(prompt: str, system: str = "", messages=None) -> list[dict[str, str]]:
    """提供方共用消息顺序；传 messages 时按角色原样发送，否则退化为 system + user。"""
    if messages is not None:
        return _normalize_messages(messages)
    built = []
    if system:
        built.append({"role": "system", "content": system})
    built.append({"role": "user", "content": prompt})
    return built


def _post_json(url: str, body: dict, headers: dict[str, str], timeout: int) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # 不向终端/UI 回显服务端原始响应，避免响应中夹带密钥或提交内容。
        raise ModelProviderError(f"[http] 模型服务返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ModelProviderError(("[timeout] 模型服务请求超时" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else f"[connection] 无法连接模型服务：{exc.reason}")) from exc
    except TimeoutError as exc:
        raise ModelProviderError("[timeout] 模型服务请求超时") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ModelProviderError("[non_json] 模型服务返回非 JSON 响应") from exc


def ollama_llm(
    prompt: str,
    system: str = "",
    num_predict: int | None = None,
    json_mode: bool = False,
    messages=None,
    temperature: float | None = None,
) -> str:
    payload = _chat_messages(prompt, system, messages)
    body = {
        "model": settings.ollama_model,
        "messages": payload,
        "stream": False,
        "keep_alive": settings.ollama_keep_alive,
        "options": _ollama_options(num_predict=num_predict, temperature=temperature),
    }
    if json_mode:
        # Ollama 使用语法约束保证输出为完整 JSON，避免小模型漏逗号或混入说明文字。
        body["format"] = "json"
    data = _post_json(
        settings.ollama_url,
        body,
        {"Content-Type": "application/json"},
        settings.request_timeout,
    )
    _record_usage(data)
    try:
        content = data["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ModelProviderError("[schema] Ollama message.content 必须为非空文本")
        return content
    except (KeyError, TypeError) as exc:
        raise ModelProviderError("[schema] Ollama 返回内容缺少有效 message.content") from exc


def deepseek_llm(
    prompt: str,
    system: str = "",
    num_predict: int | None = None,
    json_mode: bool = False,
    messages=None,
    temperature: float | None = None,
) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise ModelProviderError("外网模型未配置：缺少 DEEPSEEK_API_KEY")
    payload = _chat_messages(prompt, system, messages)
    body = {
        "model": settings.deepseek_model,
        "messages": payload,
        "stream": False,
        **({"max_tokens": int(num_predict)} if num_predict else {}),
        **({"temperature": float(temperature)} if temperature is not None else {}),
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = _post_json(
        settings.deepseek_url,
        body,
        {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        settings.request_timeout,
    )
    usage = data.get("usage") if isinstance(data, dict) else None
    if isinstance(usage, dict):
        _usage.value = {"prompt_eval_count": usage.get("prompt_tokens"),
                        "eval_count": usage.get("completion_tokens")}
    try:
        return str(data["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelProviderError("[schema] DeepSeek 返回内容缺少 choices[0].message.content") from exc


def configured_llm(
    prompt: str,
    system: str = "",
    num_predict: int | None = None,
    json_mode: bool = False,
    messages=None,
    temperature: float | None = None,
) -> str:
    """统一模型入口；周报逐页生成可单独限制输出 token，避免整份长稿被截断。"""
    if settings.is_external_model:
        return deepseek_llm(
            prompt, system=system, num_predict=num_predict, json_mode=json_mode,
            messages=messages, temperature=temperature,
        )
    return ollama_llm(
        prompt, system=system, num_predict=num_predict, json_mode=json_mode,
        messages=messages, temperature=temperature,
    )


def ollama_vision(image_path: str, prompt: str = "") -> str:
    """把单张图片交给本机视觉模型；图片以 base64 发往本机 Ollama。"""
    if not settings.vision_enabled:
        raise ModelProviderError("本地视觉模型未启用")
    try:
        image_data = base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
    except OSError as exc:
        raise ModelProviderError("图片文件无法读取") from exc
    instruction = prompt or (
        "你是安保办公场景的本地图片理解助手。请客观描述图片中的场景、物体、"
        "文字、表格或明显安全风险，并用简洁中文分点输出。不得猜测人员真实身份，"
        "不得做人脸识别，不得编造图片中看不到的信息。"
    )
    data = _post_json(
        settings.ollama_url,
        {
            "model": settings.vision_model,
            "messages": [{"role": "user", "content": instruction, "images": [image_data]}],
            "stream": False,
            "keep_alive": settings.vision_keep_alive,
            "options": {"num_ctx": min(settings.ollama_num_ctx, 4096), "num_predict": 512},
        },
        {"Content-Type": "application/json"},
        settings.request_timeout,
    )
    try:
        return str(data["message"]["content"]).strip()
    except (KeyError, TypeError) as exc:
        raise ModelProviderError("视觉模型返回内容缺少 message.content") from exc


def _ollama_options(num_predict: int | None = None, temperature: float | None = None) -> dict:
    """限制上下文与生成长度；长文档任务可覆盖输出 token 上限。"""
    options = {
        "num_ctx": settings.ollama_num_ctx,
        "num_predict": int(num_predict or settings.ollama_num_predict),
    }
    if temperature is not None:
        options["temperature"] = float(temperature)
    return options


def ollama_llm_stream(prompt: str, system: str = "", messages=None):
    """流式调用本机 Ollama，逐段产出文本。"""
    payload = _chat_messages(prompt, system, messages)

    req = urllib.request.Request(
        settings.ollama_url,
        data=json.dumps(
            {
                "model": settings.ollama_model,
                "messages": payload,
                "stream": True,
                "keep_alive": settings.ollama_keep_alive,
                "options": _ollama_options(),
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.request_timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (data.get("message") or {}).get("content") or ""
                if content:
                    yield content
                if data.get("done"):
                    _record_usage(data)
                    break
    except urllib.error.HTTPError as exc:
        raise ModelProviderError(f"[http] 模型服务返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ModelProviderError(("[timeout] 模型服务请求超时" if isinstance(exc.reason, (TimeoutError, socket.timeout)) else f"[connection] 无法连接模型服务：{exc.reason}")) from exc
    except TimeoutError as exc:
        raise ModelProviderError("[timeout] 模型服务请求超时") from exc


def configured_llm_stream(prompt: str = "", system: str = "", messages=None):
    """流式入口：本地模型逐段产出；公网模型退化为一次性产出。"""
    if settings.is_external_model:
        yield deepseek_llm(prompt, system=system, messages=messages)
        return
    yield from ollama_llm_stream(prompt, system=system, messages=messages)


def provider_status() -> str:
    if settings.is_external_model:
        return "公网模型 · DeepSeek（内容会先经过红线检查与脱敏）"
    vision = f" + {settings.vision_model} 视觉" if settings.vision_enabled else ""
    return f"本地模型 · Ollama / {settings.ollama_model}{vision}（数据不离开本机）"


def check_weekly_model() -> dict:
    """只读检查配置服务和模型名；不下载模型，不变更配置。"""
    if settings.is_external_model:
        if not os.getenv("DEEPSEEK_API_KEY", "").strip():
            raise ModelProviderError("[configuration] 缺少 DEEPSEEK_API_KEY")
        return {"provider": "external", "model": settings.deepseek_model}
    url = urlsplit(settings.ollama_url.strip())
    if url.scheme not in {"http", "https"} or not url.netloc or url.path.rstrip("/") != "/api/chat":
        raise ModelProviderError("[configuration] OLLAMA_URL 必须为完整 /api/chat 地址")
    tags_url = urlunsplit((url.scheme, url.netloc, "/api/tags", "", ""))
    try:
        with urllib.request.urlopen(tags_url, timeout=min(settings.request_timeout, 10)) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ModelProviderError(f"[http] 模型列表返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        kind = "timeout" if isinstance(exc.reason, TimeoutError) else "connection"
        raise ModelProviderError(f"[{kind}] 无法读取 Ollama 模型列表：{exc.reason}") from exc
    except TimeoutError as exc:
        raise ModelProviderError("[timeout] Ollama 模型列表请求超时") from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModelProviderError("[non_json] Ollama 模型列表不是 JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ModelProviderError("[schema] Ollama 模型列表缺少 models 数组")
    names = {
        value
        for item in data["models"] if isinstance(item, dict)
        for value in (item.get("name"), item.get("model"))
        if value
    }
    model = settings.ollama_model.strip()
    if not model or (model not in names and (model + ":latest") not in names):
        raise ModelProviderError(f"[model_missing] Ollama 未安装配置模型 {model!r}；请用 ollama list 核对完整名称")
    return {"provider": "ollama", "model": model}
