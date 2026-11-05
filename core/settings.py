"""运行配置：全部从环境变量读取，避免把密钥写进项目文件。"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # local（推荐）= Ollama；external = DeepSeek 公网接口
    model_mode: str = os.getenv("LUX_MODEL_MODE", "local").strip().lower()
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    vision_enabled: bool = _bool("LUX_VISION_ENABLED", False)
    vision_model: str = os.getenv("OLLAMA_VISION_MODEL", "qwen2.5vl:3b")
    # 低内存机器上视觉模型完成后立即卸载，给文字模型腾出内存。
    vision_keep_alive: str = os.getenv("OLLAMA_VISION_KEEP_ALIVE", "0")
    ollama_keep_alive: str = os.getenv("OLLAMA_KEEP_ALIVE", "30m")
    ollama_num_ctx: int = _int("OLLAMA_NUM_CTX", 4096)
    ollama_num_predict: int = _int("OLLAMA_NUM_PREDICT", 1024)
    deepseek_url: str = os.getenv(
        "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"
    )
    deepseek_model: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    request_timeout: int = _int("LUX_REQUEST_TIMEOUT", 120)
    max_input_chars: int = _int("LUX_MAX_INPUT_CHARS", 20000)
    max_file_mb: int = _int("LUX_MAX_FILE_MB", 10)
    host: str = os.getenv("LUX_HOST", "127.0.0.1")
    port: int = _int("LUX_PORT", 7860)
    share: bool = _bool("LUX_SHARE", False)
    # 事件中心是否显示“事件处置”（风险/紧急程度/制度研判/人工决定）。默认显示；
    # 设为 false 则只保留奖惩制度参考。
    show_incident_desk: bool = _bool("LUX_SHOW_INCIDENT_DESK", True)
    # 聊天带入的历史：最近几轮、最多多少字（0 轮 = 关闭，回到每轮独立提问）。
    history_turns: int = _int("LUX_HISTORY_TURNS", 3)
    history_chars: int = _int("LUX_HISTORY_CHARS", 1800)
    # 单条历史消息保留多少字：超长时同时保留开头与结尾，结尾常是约束语句。
    history_message_chars: int = _int("LUX_HISTORY_MSG_CHARS", 600)
    # 上下文感知意图路由：关闭则只按规则判定，不额外调用模型。
    intent_router_enabled: bool = _bool("LUX_INTENT_ROUTER", True)
    # 路由模型温度：低温度提高重复性，仍需评测验证，不代表更准。
    intent_router_temperature: float = _float("LUX_INTENT_ROUTER_TEMPERATURE", 0.0)
    # 会话任务状态保留时长（秒），过期后按新任务处理。
    conversation_ttl: int = _int("LUX_CONVERSATION_TTL", 3600)
    # 是否把程序错误详情显示给使用者（默认关闭，避免泄露路径和内部信息）。
    show_errors: bool = _bool("LUX_SHOW_ERRORS", False)
    app_username: str = os.getenv("LUX_APP_USERNAME", "")
    app_password: str = os.getenv("LUX_APP_PASSWORD", "")
    # 联网搜索：服务器总开关与适配器。密钥从环境变量 TAVILY_API_KEY 读取，不进这里。
    web_search_enabled: bool = _bool("LUX_WEB_SEARCH_ENABLED", False)
    web_search_provider: str = os.getenv("LUX_WEB_SEARCH_PROVIDER", "tavily").strip().lower()
    web_search_timeout: int = _int("LUX_WEB_SEARCH_TIMEOUT", 10)
    web_search_max_results: int = _int("LUX_WEB_SEARCH_MAX_RESULTS", 4)
    # 网页前台语音唤醒助手：登录并主动开启后，喊“小安小安”进入语音对话。
    # 默认关闭；唤醒检测全程在浏览器本地运行，未唤醒时不上传环境音频。
    voice_dialogue_enabled: bool = _bool("LUX_VOICE_DIALOGUE_ENABLED", False)
    # 回答朗读（本地 TTS，sherpa-onnx + Kokoro 中文模型）。默认关闭；文字聊天不受影响。
    # 模型文件由 install_tts.ps1 下载到 models/tts/，不随 Git 分发。
    tts_enabled: bool = _bool("LUX_TTS_ENABLED", False)
    tts_provider: str = os.getenv("LUX_TTS_PROVIDER", "local").strip().lower()
    # 模型目录；留空则用仓库内默认 models/tts/kokoro-int8-multi-lang-v1_0。
    tts_model_dir: str = os.getenv("LUX_TTS_MODEL_DIR", "").strip()
    # 音色：zf_xiaobei/zf_xiaoni/zf_xiaoxiao/zf_xiaoyi（女），zm_yunjian/zm_yunxi/zm_yunxia/zm_yunyang（男）。
    tts_voice: str = os.getenv("LUX_TTS_VOICE", "zf_xiaoxiao").strip()
    tts_speed: float = _float("LUX_TTS_SPEED", 1.0)
    # 单次合成最大字符数，超出拒绝，避免把长报告整篇送入模型阻塞队列。
    tts_max_chars: int = _int("LUX_TTS_MAX_CHARS", 2000)
    # 音频临时文件保留时长（秒），过期清理。
    tts_audio_ttl: int = _int("LUX_TTS_AUDIO_TTL", 1800)
    # 语音对话闭环（里程碑 C）：VAD 停顿/超时与追问窗口，均可按真实讲话测量后调整。
    # 停顿阈值：讲话中能量低于阈值持续这么长时间，判定本轮讲完。
    vad_pause_ms: int = _int("LUX_VAD_PAUSE_MS", 800)
    # 等待开口超时：进入聆听后这么久还没检测到语音，返回等待唤醒。
    vad_silence_timeout_ms: int = _int("LUX_VAD_SILENCE_TIMEOUT_MS", 8000)
    # 单轮最大录音时长，防止误判停顿导致无限录音。
    vad_max_turn_ms: int = _int("LUX_VAD_MAX_TURN_MS", 20000)
    # 最短有效语音时长：短于此按噪声忽略，避免咳嗽/点击被当成问题提交。
    vad_min_speech_ms: int = _int("LUX_VAD_MIN_SPEECH_MS", 400)
    # VAD 能量阈值（RMS，归一化 16 位音频）。越低越灵敏，越高越抗噪。
    vad_energy_threshold: float = _float("LUX_VAD_ENERGY_THRESHOLD", 0.02)
    # 回答播完后的追问窗口时长（毫秒），超时返回等待唤醒。
    voice_followup_ms: int = _int("LUX_VOICE_FOLLOWUP_MS", 10000)

    @property
    def is_external_model(self) -> bool:
        return self.model_mode == "external"

    @property
    def auth(self) -> tuple[str, str] | None:
        if self.app_username and self.app_password:
            return self.app_username, self.app_password
        return None

    def validate(self, *, auth_ready: bool = False) -> None:
        if self.model_mode not in {"local", "external"}:
            raise ValueError("LUX_MODEL_MODE 只能是 local 或 external")
        if self.vision_enabled and not self.vision_model:
            raise ValueError("启用视觉模型时必须设置 OLLAMA_VISION_MODEL")
        if self.host not in {"127.0.0.1", "localhost"} and not (auth_ready or self.auth):
            raise ValueError(
                "监听局域网地址时必须设置 LUX_APP_USERNAME 和 LUX_APP_PASSWORD"
            )
        if self.share and not (auth_ready or self.auth):
            raise ValueError("启用公网分享链接时必须设置登录账号和密码")
        if self.web_search_enabled and self.web_search_provider not in {"tavily"}:
            raise ValueError("LUX_WEB_SEARCH_PROVIDER 目前只支持 tavily")
        if self.tts_enabled and self.tts_provider not in {"local"}:
            raise ValueError("LUX_TTS_PROVIDER 目前只支持 local")
        if not 100 <= self.vad_pause_ms <= 5000:
            raise ValueError("LUX_VAD_PAUSE_MS 应在 100–5000 毫秒之间")
        if not 1000 <= self.vad_silence_timeout_ms <= 60000:
            raise ValueError("LUX_VAD_SILENCE_TIMEOUT_MS 应在 1000–60000 毫秒之间")
        if not 1000 <= self.vad_max_turn_ms <= 120000:
            raise ValueError("LUX_VAD_MAX_TURN_MS 应在 1000–120000 毫秒之间")
        if not 1000 <= self.voice_followup_ms <= 60000:
            raise ValueError("LUX_VOICE_FOLLOWUP_MS 应在 1000–60000 毫秒之间")


settings = Settings()
