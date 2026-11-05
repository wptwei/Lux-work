"""回答朗读（TTS）的受控路由：合成、按用户取回音频、预合成唤醒应答。

音频只读“服务器登记过的、通过输出检查的最终答案”，不接受客户端随意传入的文本。
取回音频时按登录身份重新校验归属，audio_id 随机不可枚举，且带 TTL 清理。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from core import tts as tts_module
from core.settings import settings
from core.tts import TtsError


class SynthesizeRequest(BaseModel):
    session: str = ""


async def _authenticate(request: Request) -> str | None:
    app = request.app
    dependency = getattr(app, "auth_dependency", None)
    if dependency:
        username = dependency(request)
        if inspect.isawaitable(username):
            username = await username
    else:
        cookie_id = getattr(app, "cookie_id", "")
        token = request.cookies.get(f"access-token-{cookie_id}") or request.cookies.get(
            f"access-token-unsecure-{cookie_id}"
        )
        username = getattr(app, "tokens", {}).get(token)
    return username or None


async def _require_user(request: Request, users) -> str:
    username = await _authenticate(request)
    if not username:
        raise HTTPException(401, "请先登录 LUX。")
    user = users.get_public(username) if username else None
    if not user or not user.get("enabled"):
        raise HTTPException(401, "请先登录 LUX。")
    return username


def _client_session(request: Request) -> str:
    """浏览器会话标识：与 app.py 读取同一个 cookie，用于按设备隔离答案与音频。"""
    try:
        value = request.cookies.get("lux-voice-sid")
        return str(value)[:64] if value else ""
    except Exception:  # noqa: BLE001
        return ""


def tts_routes(users) -> list[APIRoute]:
    async def status():
        return JSONResponse({
            "enabled": settings.tts_enabled,
            "ready": tts_module.is_available(),
            "voice": settings.tts_voice,
            "speed": settings.tts_speed,
            "max_chars": settings.tts_max_chars,
        })

    async def synthesize(body: SynthesizeRequest, request: Request):
        username = await _require_user(request, users)
        if not settings.tts_enabled:
            raise HTTPException(404, "回答朗读未开启。")
        # 只朗读服务器登记过的、本轮最终通过输出检查的答案，不信任客户端传入文本；
        # 按浏览器会话查找，避免同一账号的另一台设备读到本设备的问题。
        session = _client_session(request) or (body.session or "")
        text = tts_module.last_answer(username, session)
        if not text:
            raise HTTPException(404, "还没有可朗读的回答。")
        try:
            info = tts_module.synthesize_for_user(username, text, session=session)
        except TtsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse({
            "audio_id": info["audio_id"],
            "url": f"/lux-tts/audio/{info['audio_id']}",
            "duration_ms": info["duration_ms"],
            "sample_rate": info["sample_rate"],
        })

    async def wake_reply(request: Request):
        await _require_user(request, users)
        if not settings.tts_enabled:
            raise HTTPException(404, "回答朗读未开启。")
        try:
            tts_module.wake_reply_path()
        except TtsError as exc:
            raise HTTPException(422, str(exc)) from exc
        return JSONResponse({
            "url": "/lux-tts/wake-reply.wav",
            "duration_ms": tts_module.wake_reply_duration_ms(),
        })

    async def wake_reply_audio(request: Request):
        await _require_user(request, users)
        try:
            path = tts_module.wake_reply_path()
        except TtsError as exc:
            raise HTTPException(404, str(exc)) from exc
        return _audio_file(path)

    async def audio(audio_id: str, request: Request):
        username = await _require_user(request, users)
        path = tts_module.registry.resolve(username, audio_id, session=_client_session(request) or None)
        if path is None or not path.is_file():
            raise HTTPException(404, "音频不存在或已过期。")
        return _audio_file(path)

    return [
        APIRoute("/lux-tts/status", status, methods=["GET"], include_in_schema=False),
        APIRoute("/lux-tts/synthesize", synthesize, methods=["POST"], include_in_schema=False),
        APIRoute("/lux-tts/wake-reply", wake_reply, methods=["POST"], include_in_schema=False),
        APIRoute("/lux-tts/wake-reply.wav", wake_reply_audio, methods=["GET"], include_in_schema=False),
        APIRoute("/lux-tts/audio/{audio_id}", audio, methods=["GET"], include_in_schema=False),
    ]


def _audio_file(path: Path) -> FileResponse:
    return FileResponse(
        path,
        media_type="audio/wav",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Accept-Ranges": "bytes",
        },
    )
