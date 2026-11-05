"""本地 Kokoro 中文多语 TTS 适配器（sherpa-onnx + int8 模型）。

模型由 install_tts.ps1 下载到 models/tts/kokoro-int8-multi-lang-v1_0：
model.int8.onnx / voices.bin / tokens.txt / lexicon-*.txt / espeak-ng-data /
number-zh.fst / date-zh.fst / phone-zh.fst（中文数字、日期、电话的正规化）。

合成在服务器 CPU 上完成，返回 float32 采样；由 core.tts 写成 WAV 供浏览器播放。
Kokoro 解码是自回归的，多线程加速有限；引擎用一把锁串行化，避免并发调用互相拖慢。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from core.settings import settings

# 中文音色名 -> 说话人 ID（kokoro-multi-lang-v1_0 / int8 v1_0，53+1 个说话人）。
VOICE_SIDS = {
    "zf_xiaobei": 45,
    "zf_xiaoni": 46,
    "zf_xiaoxiao": 47,
    "zf_xiaoyi": 48,
    "zm_yunjian": 49,
    "zm_yunxi": 50,
    "zm_yunxia": 51,
    "zm_yunyang": 52,
}

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent.parent / "models" / "tts" / "kokoro-int8-multi-lang-v1_0"


class TtsUnavailableError(RuntimeError):
    """模型目录缺失或未安装 sherpa-onnx 时抛出，前端据此显示可理解提示。"""


def resolve_sid(voice: str) -> int:
    if voice.isdigit():
        return int(voice)
    return VOICE_SIDS.get(voice, VOICE_SIDS["zf_xiaoxiao"])


class LocalKokoroEngine:
    """惰性加载的单例 Kokoro 引擎，进程内共享一份模型，避免每个请求各加载一次。"""

    def __init__(self) -> None:
        self._tts = None
        self._lock = threading.Lock()
        self._load_error: str | None = None

    @property
    def model_dir(self) -> Path:
        return Path(settings.tts_model_dir).expanduser().resolve() if settings.tts_model_dir else DEFAULT_MODEL_DIR

    def is_ready(self) -> bool:
        return (self.model_dir / "model.int8.onnx").is_file()

    def _ensure_loaded(self):
        if self._tts is not None:
            return
        if self._load_error:
            raise TtsUnavailableError(self._load_error)
        if not self.is_ready():
            raise TtsUnavailableError(
                "TTS 模型未就绪，请先在服务器运行 install_tts.ps1 下载模型。"
            )
        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover
            raise TtsUnavailableError("缺少 sherpa-onnx，请先 pip install sherpa-onnx。") from exc

        m = self.model_dir
        kokoro = sherpa_onnx.OfflineTtsKokoroModelConfig(
            model=str(m / "model.int8.onnx"),
            voices=str(m / "voices.bin"),
            tokens=str(m / "tokens.txt"),
            lexicon=",".join(
                str(m / f)
                for f in ("lexicon-us-en.txt", "lexicon-zh.txt")
            ),
        )
        config = sherpa_onnx.OfflineTtsConfig(
            model=sherpa_onnx.OfflineTtsModelConfig(kokoro=kokoro, num_threads=2),
        )
        # 中文数字/日期/电话正规化：缺了会把“110”“2025年6月18号”里的数字当 OOV 跳过。
        config.rule_fsts = ",".join(
            str(m / f) for f in ("number-zh.fst", "date-zh.fst", "phone-zh.fst")
        )
        if not config.validate():
            raise TtsUnavailableError("Kokoro TTS 配置校验失败，请检查模型文件是否完整。")
        self._tts = sherpa_onnx.OfflineTts(config)

    def sample_rate(self) -> int:
        self._ensure_loaded()
        return int(self._tts.sample_rate)

    def synthesize(self, text: str, sid: int, speed: float, timeout: float) -> tuple:
        """返回 (samples: float32 ndarray, sample_rate: int)。超时/取消用回调实现。"""
        self._ensure_loaded()
        deadline = time.monotonic() + timeout
        cancelled = {"flag": False}

        def _progress(_samples, _progress):
            if cancelled["flag"] or time.monotonic() > deadline:
                return 0  # 返回 0 提前停止生成
            return 1

        with self._lock:
            audio = self._tts.generate(text=text, sid=sid, speed=speed, callback=_progress)
        return audio.samples, int(audio.sample_rate)


engine = LocalKokoroEngine()
