"""语音对话闭环（里程碑 C）的服务端部分：把浏览器录下的一整轮 PCM 转成文字。

浏览器端（frontend/voice_dialogue.js）负责唤醒→聆听→VAD 判定讲话结束→把最终 PCM
一次性送到这里。这里只做三件事：解码/重采样、本地 ASR 最终转写、以及把转写结果
过一遍语音文字安全检查。是否提交到聊天、是否播放回答，由前端在拿到最终文字后决定，
本模块不直接驱动模型或 TTS，因此完全复用现有对话、检索、权限与输出检查链路。

文字为空、命中红线（block/review）都不会返回可提交的正文，前端据此不得提交。
"""
from __future__ import annotations

import base64
import binascii
import inspect
from math import gcd

import numpy as np
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel

from core.voice_input import validate_voice_text
from voice_asr import MAX_AUDIO_SECONDS, VoiceInputError, transcribe_samples

RATE = 16000
# 单次转写请求的 base64 负载上限（约 12MB base64 ≈ 6MB 二进制），足够覆盖最大轮次。
_MAX_B64_BYTES = 12_000_000


class TranscribeRequest(BaseModel):
    session: str = ""
    pcm: str = ""
    rate: int = 48000
    region: str = ""


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


def _decode_and_resample(pcm: str, rate: int) -> np.ndarray:
    if not isinstance(rate, int) or not 8000 <= rate <= 192000:
        raise VoiceInputError("录音采样率无效，请重新录制。")
    if not pcm:
        raise VoiceInputError("没有收到录音，请重新录制。")
    if len(pcm) > _MAX_B64_BYTES:
        raise VoiceInputError("录音过长，请简短一些再说。")
    try:
        data = base64.b64decode(pcm, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VoiceInputError("录音数据无效，请重新录制。") from exc
    if len(data) % 2:
        raise VoiceInputError("录音数据无效，请重新录制。")
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    if not len(samples):
        raise VoiceInputError("录音为空，请重新录制。")
    if len(samples) > rate * MAX_AUDIO_SECONDS:
        raise VoiceInputError(f"单次录音最多 {MAX_AUDIO_SECONDS} 秒。")
    if rate != RATE:
        try:
            from scipy.signal import resample_poly
        except ImportError as exc:
            raise VoiceInputError("实时语音组件未安装完整，请运行 install_voice.ps1。") from exc
        divisor = gcd(rate, RATE)
        samples = resample_poly(samples, RATE // divisor, rate // divisor).astype(np.float32)
    return samples


def voice_dialogue_routes(users, guard, default_region: str = "") -> list[APIRoute]:
    """返回语音对话的受控路由。转写文字经过与听写相同的安全检查。"""

    async def transcribe(body: TranscribeRequest, request: Request):
        await _require_user(request, users)
        region = (body.region or "").strip() or default_region
        try:
            samples = _decode_and_resample(body.pcm or "", body.rate)
            segments = transcribe_samples(samples, final=True)
            text = "".join(s["text"] for s in segments).strip()
        except VoiceInputError as exc:
            return JSONResponse({
                "ok": False, "text": "", "message": str(exc),
                "blocked": False, "empty": True,
                "duration_ms": 0,
            })
        except Exception:  # noqa: BLE001 - 转写失败统一转可读提示
            return JSONResponse({
                "ok": False, "text": "", "message": "语音识别失败，请重试或改用文字输入。",
                "blocked": False, "empty": False,
                "duration_ms": 0,
            })

        if not text:
            return JSONResponse({
                "ok": False, "text": "", "message": "没有识别到有效语音，请靠近麦克风再说一遍。",
                "blocked": False, "empty": True,
                "duration_ms": int(round(len(samples) / RATE * 1000)),
            })
        draft = validate_voice_text(text, guard, region)
        return JSONResponse({
            "ok": bool(draft.ok),
            "text": draft.text,
            "message": draft.message,
            "blocked": bool(draft.blocked),
            "empty": False,
            "duration_ms": int(round(len(samples) / RATE * 1000)),
        })

    return [
        APIRoute("/lux-voice-dialogue/transcribe", transcribe, methods=["POST"], include_in_schema=False),
    ]
