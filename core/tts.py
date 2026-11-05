"""回答朗读（本地 TTS）的统一接口：文本清理、长度限制、合成、音频隔离与清理。

文本来源由 app.py 的 extract_last_spoken 从最终通过输出检查的助手回答中提取并清理，
前端只把这段“最终答案”送过来合成；路由端不信任客户端随意注入的未检查内容，但仍需
校验长度、清理排版符号，并只在启用 TTS 时工作。

音频按用户隔离：每个用户只能取回自己名下、且未过期的音频；audio_id 用随机 UUID，
不可枚举。临时文件带 TTL，由后台线程定期清理。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf

from core.paths import data_path
from core.settings import settings
from core.tts_providers.local_kokoro import (
    TtsUnavailableError,
    engine,
    resolve_sid,
)

TTS_AUDIO_DIR = data_path("tts_audio")
_SWEEP_INTERVAL = 300  # 清理线程扫描间隔（秒）

# 模型回答里用于排版、非朗读内容的部分：Markdown、代码、链接、UI 提示前缀等。
_CODE_FENCE = re.compile(r"```[^`]*```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"!\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_MARKDOWN = re.compile(r"([*_~#>|]|\s-{2,}\s|\s-{3,}\s)")
_HTML_TAG = re.compile(r"<[^>]+>")
_NOTICE_LINE = re.compile(r"^[>🔒📌⚠️✅🚫⏳]\s*.*$", re.MULTILINE)
_MULTI_SPACE = re.compile(r"\s+")


class TtsError(RuntimeError):
    """给调用方的业务错误：文本为空、超长、合成失败等。"""


class AudioRegistry:
    """audio_id -> 归属元数据，负责写入、按用户取回与 TTL 清理。"""

    def __init__(self, root: Path, ttl: int) -> None:
        self.root = root
        self.ttl = ttl
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._sweep_thread: threading.Thread | None = None

    def start(self) -> None:
        if self._sweep_thread and self._sweep_thread.is_alive():
            return
        self._sweep_thread = threading.Thread(target=self._sweep_loop, name="tts-audio-sweep", daemon=True)
        self._sweep_thread.start()

    def register(self, username: str, session: str, path: Path, duration_ms: int, audio_id: str | None = None) -> str:
        audio_id = audio_id or uuid.uuid4().hex
        with self._lock:
            self._items[audio_id] = {
                "username": username,
                "session": session,
                "path": str(path),
                "duration_ms": duration_ms,
                "created": time.time(),
            }
        self._sweep_once()
        return audio_id

    def resolve(self, username: str, audio_id: str, session: str | None = None) -> Path | None:
        expired_path: Path | None = None
        with self._lock:
            item = self._items.get(audio_id)
            if not item:
                return None
            if item["username"] != username:
                return None
            # 同一账号在手机/电脑同时登录时，音频只在发起合成的浏览器可取回。
            if session is not None and item.get("session") and item["session"] != session:
                return None
            if time.time() - item["created"] > self.ttl:
                self._items.pop(audio_id, None)
                expired_path = Path(item["path"])
            else:
                return Path(item["path"])
        # 过期即回收文件，避免仅靠后台线程清理时留下孤儿文件。
        if expired_path is not None:
            try:
                expired_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    def _sweep_once(self) -> None:
        now = time.time()
        expired: list[tuple[str, Path]] = []
        with self._lock:
            for audio_id, item in list(self._items.items()):
                if now - item["created"] > self.ttl:
                    expired.append((audio_id, Path(item["path"])))
                    self._items.pop(audio_id, None)
        for _, path in expired:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _sweep_loop(self) -> None:
        while True:
            time.sleep(_SWEEP_INTERVAL)
            self._sweep_once()


registry = AudioRegistry(TTS_AUDIO_DIR, settings.tts_audio_ttl)


def start_cleanup() -> None:
    """应用启动后调用一次，开启后台 TTL 清理线程。"""
    registry.start()


def prewarm_wake_reply() -> None:
    """后台预热：加载模型并预合成“我在，请说”，让服务启动后的首次唤醒也能快速播放。

    放在独立守护线程里，不阻塞 launch；预热失败不致命，真正唤醒时会重试并给可读提示。
    """

    def _run() -> None:
        if not settings.tts_enabled:
            return
        try:
            wake_reply_path()
        except Exception:  # noqa: BLE001 - 预热失败不影响主流程
            pass

    threading.Thread(target=_run, name="tts-prewarm", daemon=True).start()


# 每个浏览器会话最近一次“通过输出检查”的干净模型输出（result["output"]，不含来源/提示前缀）。
# 以 (用户名, 浏览器会话) 为键：同一账号在手机和电脑同时使用时，各自只朗读自己的回答，
# 不会把另一台设备的答案播出来。app.py 在每次 respond 开始时清空、仅在 status == ok
# 时写入，确保朗读的一定是本轮最终通过检查的答案。
_last_answers: dict[tuple[str, str], str] = {}
_last_lock = threading.Lock()


def record_last_answer(username: str, text: str, session: str = "") -> None:
    """写入或清空（text 为空时清空）该用户、该浏览器会话最近一次可通过检查的答案。"""
    text = (text or "").strip()
    key = (username, session or "")
    with _last_lock:
        if text:
            _last_answers[key] = text
        else:
            _last_answers.pop(key, None)


def last_answer(username: str, session: str = "") -> str:
    with _last_lock:
        return _last_answers.get((username, session or ""), "")


def clean_spoken_text(text: str) -> str:
    """把模型回答里的排版/代码/链接/UI 提示去掉，只留适合朗读的自然语言。

    数字、日期、否定和业务含义交给模型本身与中文 FST 正规化，不做会改变语义的改写。
    """
    text = text or ""
    text = _CODE_FENCE.sub(" ", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINK.sub(lambda m: (m.group(1) or m.group(2) or ""), text)
    text = _URL.sub(" ", text)
    text = _HTML_TAG.sub(" ", text)
    # 去掉 LUX 自己的提示前缀（🔒 已脱敏 / 📌 专项技能 / > ⚠️ 检索提示）。
    text = _NOTICE_LINE.sub(" ", text)
    text = _MARKDOWN.sub(" ", text)
    text = _MULTI_SPACE.sub(" ", text).strip()
    return text


def _truncate_sentences(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = text[:limit]
    # 在限制内找最后一个句末标点，避免从半句截断。
    for marker in ("。", "！", "？", "；", "!", "?", ";"):
        idx = head.rfind(marker)
        if idx > limit // 2:
            return head[: idx + 1]
    return head


def synthesize_for_user(username: str, text: str, session: str = "") -> dict:
    """合成一段通过检查的文本，写 WAV 并登记，返回 audio_id / 时长 / 采样率。"""
    if not settings.tts_enabled:
        raise TtsError("回答朗读未开启。")
    spoken = clean_spoken_text(text)
    if not spoken:
        raise TtsError("没有可朗读的内容。")
    if len(spoken) > settings.tts_max_chars:
        spoken = _truncate_sentences(spoken, settings.tts_max_chars)
    if not spoken:
        raise TtsError("内容过长，无法朗读。")

    sid = resolve_sid(settings.tts_voice)
    speed = settings.tts_speed
    # 自回归解码，超时随长度放宽；回调提前停止作为兜底。
    timeout = max(30.0, min(300.0, len(spoken) * 0.4))
    try:
        samples, sample_rate = engine.synthesize(spoken, sid=sid, speed=speed, timeout=timeout)
    except TtsUnavailableError as exc:
        raise TtsError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 合成失败统一转成业务错误，不泄露内部细节
        raise TtsError("语音合成失败，请稍后重试。") from exc

    if samples is None or len(samples) == 0:
        raise TtsError("语音合成失败，请稍后重试。")

    user_dir = TTS_AUDIO_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    audio_id = uuid.uuid4().hex
    path = user_dir / f"{audio_id}.wav"
    sf.write(str(path), np.asarray(samples, dtype=np.float32), int(sample_rate), subtype="PCM_16")
    duration_ms = int(round(len(samples) / int(sample_rate) * 1000))
    registry.register(username, session, path, duration_ms, audio_id=audio_id)
    return {
        "audio_id": audio_id,
        "duration_ms": duration_ms,
        "sample_rate": int(sample_rate),
    }


def wake_reply_for_user(username: str, session: str = "") -> dict:
    """预合成“我在，请说”，供唤醒后快速播放（复用最后一次的合成缓存由前端决定）。"""
    return synthesize_for_user(username, "我在，请说。", session=session)


# “我在，请说”是固定的系统提示语，不含用户数据，进程内合成一次并全局复用，
# 供唤醒后最快拿到音频，避免每次唤醒都重新跑一遍合成。
_wake_reply: dict = {"path": None, "lock": threading.Lock()}


def wake_reply_path() -> Path:
    with _wake_reply["lock"]:
        path = _wake_reply.get("path")
        if path and Path(path).is_file():
            return Path(path)
        spoken = "我在，请说。"
        try:
            samples, sample_rate = engine.synthesize(
                spoken, sid=resolve_sid(settings.tts_voice), speed=settings.tts_speed, timeout=30.0
            )
        except Exception as exc:  # noqa: BLE001
            raise TtsError("唤醒应答合成失败。") from exc
        TTS_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        path = TTS_AUDIO_DIR / "_wake_reply.wav"
        sf.write(str(path), np.asarray(samples, dtype=np.float32), int(sample_rate), subtype="PCM_16")
        _wake_reply["path"] = str(path)
        return Path(path)


def wake_reply_duration_ms() -> int:
    try:
        import soundfile as sf
        info = sf.info(str(wake_reply_path()))
        return int(round(info.duration * 1000))
    except Exception:  # noqa: BLE001
        return 0


def is_available() -> bool:
    return settings.tts_enabled and engine.is_ready()
