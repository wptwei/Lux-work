"""语音转写与草稿追加；不会触发聊天发送或调用外部语音服务。"""
from __future__ import annotations
from dataclasses import dataclass
from voice_asr import VoiceInputError, speech_to_text
from core.settings import settings


@dataclass(frozen=True)
class VoiceDraft:
    text: str = ""
    message: str = ""
    ok: bool = False
    # 命中 block/review 的转写：与“暂时没有识别到有效语音”区分，前端据此回退语音区间。
    blocked: bool = False


def validate_voice_text(text, guard, region=None) -> VoiceDraft:
    text = str(text or "").strip()
    if not text:
        return VoiceDraft(message="请先录音并识别文字。")
    if len(text) > settings.max_input_chars:
        return VoiceDraft(message="识别文字超过输入长度限制，请分段录制。")
    level, _, _ = guard.scan(text, source_type="voice_transcript", region=region)
    if level in {"block", "review"}:
        return VoiceDraft(message="语音文字命中安全检查，未填入输入框；请改述或按流程人工核查。", blocked=True)
    return VoiceDraft(text=text, message="识别完成，请核对文字后填入输入框。", ok=True)


def transcribe_voice(audio_path, guard, region=None) -> VoiceDraft:
    try:
        return validate_voice_text(speech_to_text(audio_path), guard, region)
    except VoiceInputError as exc:
        return VoiceDraft(message=str(exc))
    except Exception:
        return VoiceDraft(message="语音识别暂不可用，请重试或改用文字输入。")


def append_voice_text(transcript, current_text, guard, region=None) -> VoiceDraft:
    result = validate_voice_text(transcript, guard, region)
    current = str(current_text or "")
    if not result.ok:
        return VoiceDraft(text=current, message=result.message)
    combined = current + ("\n" if current.strip() else "") + result.text
    if len(combined) > settings.max_input_chars:
        return VoiceDraft(text=current, message="追加后超过输入长度限制，原输入已保留，请先精简内容。")
    return VoiceDraft(text=combined, message="已追加到输入框，请检查后点击“发送任务”。", ok=True)
