"""LUX 本地中文语音输入。正常识别只读本地模型；--prepare 显式下载模型。"""
from __future__ import annotations

import argparse
from functools import lru_cache
import math
import os
from pathlib import Path
import threading

MODEL_DIR = Path(__file__).resolve().parent / "models" / "asr"
MAX_AUDIO_MB = 25
MAX_AUDIO_SECONDS = 120
_MODEL_LOCK = threading.RLock()


class VoiceInputError(RuntimeError):
    """可以展示给使用者的语音错误，不包含录音内容或内部路径。"""


def _whisper_class():
    try:
        from faster_whisper import WhisperModel
    except (ImportError, OSError) as exc:
        raise VoiceInputError("语音组件尚未安装或无法加载，请在 LUX 电脑上运行 install_voice.ps1。") from exc
    return WhisperModel


def _model_name() -> str:
    return os.getenv("LUX_ASR_MODEL", "small").strip() or "small"


@lru_cache(maxsize=1)
def load_model():
    """兼容已有语音实验入口；首次安装应显式执行 --prepare。"""
    whisper = _whisper_class()
    with _MODEL_LOCK:
        try:
            return whisper(_model_name(), device="cpu", compute_type="int8",
                           download_root=str(MODEL_DIR), local_files_only=True)
        except Exception as exc:
            raise VoiceInputError("本地语音模型尚未准备好，请先运行 install_voice.ps1；安装完成后重启 LUX。") from exc


def prepare_model() -> None:
    """只供管理员显式执行：下载公开模型文件，不接收任何录音。"""
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    _whisper_class()(_model_name(), device="cpu", compute_type="int8",
                     download_root=str(MODEL_DIR), local_files_only=False)
    load_model.cache_clear()


def _audio_duration(path: Path) -> float:
    try:
        import av
    except (ImportError, OSError) as exc:
        raise VoiceInputError("录音解码组件不可用，请重新运行 install_voice.ps1。") from exc
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise VoiceInputError("文件中没有音轨，请重新录音。")
            stream = container.streams.audio[0]
            if stream.duration is not None and stream.time_base is not None:
                duration = float(stream.duration * stream.time_base)
            elif container.duration is not None:
                duration = float(container.duration / av.time_base)
            else:
                raise VoiceInputError("无法确定录音时长，请重新录音或上传 WAV 文件。")
    except VoiceInputError:
        raise
    except Exception as exc:
        raise VoiceInputError("录音文件无法读取，请重新录音。") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise VoiceInputError("录音为空，请重新录音。")
    return duration


def speech_to_text(audio_path) -> str:
    if not audio_path:
        raise VoiceInputError("请先录音，或上传一段音频。")
    path = Path(audio_path)
    if not path.is_file():
        raise VoiceInputError("录音已失效，请重新录音。")
    if path.stat().st_size > MAX_AUDIO_MB * 1024 * 1024:
        raise VoiceInputError(f"录音不能超过 {MAX_AUDIO_MB}MB，请分段录制。")
    duration = _audio_duration(path)
    if duration > MAX_AUDIO_SECONDS:
        raise VoiceInputError(f"单次录音最多 {MAX_AUDIO_SECONDS} 秒，请分段录制。")
    with _MODEL_LOCK:
        model = load_model()
        try:
            segments, info = model.transcribe(
                str(path), language="zh", beam_size=3,
                vad_filter=True, condition_on_previous_text=False,
            )
            if info.duration > MAX_AUDIO_SECONDS:
                raise VoiceInputError(f"单次录音最多 {MAX_AUDIO_SECONDS} 秒，请分段录制。")
            text = "".join(segment.text for segment in segments).strip()
        except VoiceInputError:
            raise
        except Exception as exc:
            raise VoiceInputError("语音识别失败，请重试或改用文字输入。") from exc
    if not text:
        raise VoiceInputError("没有识别到有效语音，请靠近麦克风重新录制。")
    return text


def transcribe_samples(samples, *, final: bool = False) -> list[dict]:
    """16kHz mono PCM: fast partial decoding, full-context correction on stop."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1 or not np.isfinite(samples).all():
        raise VoiceInputError("录音数据无效，请重新录制。")
    if len(samples) > 16000 * MAX_AUDIO_SECONDS:
        raise VoiceInputError(f"单次录音最多 {MAX_AUDIO_SECONDS} 秒。")
    if not len(samples) or np.max(np.abs(samples)) < 0.0001:
        return []
    with _MODEL_LOCK:
        model = load_model()
        try:
            segments, _ = model.transcribe(
                samples, language="zh", beam_size=3 if final else 1,
                vad_filter=True, condition_on_previous_text=False,
            )
            return [{"text": s.text, "start": float(s.start), "end": float(s.end)} for s in segments]
        except Exception as exc:
            raise VoiceInputError("语音识别失败，请重试或改用文字输入。") from exc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true", help="下载并验证语音模型，需要联网")
    args = parser.parse_args()
    try:
        if args.prepare:
            prepare_model()
        else:
            load_model()
        print("本地语音模型已就绪。")
    except Exception as exc:
        print(f"语音模型准备失败：{exc}")
        raise SystemExit(1)
