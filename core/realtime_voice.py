"""逐段接收浏览器 PCM；会话隔离、乱序恢复及过期结果抑制。"""
from __future__ import annotations
import base64
import binascii
import json
import threading
from math import gcd
import numpy as np
from core.voice_input import VoiceDraft, validate_voice_text
from voice_asr import MAX_AUDIO_SECONDS, VoiceInputError, transcribe_samples

RATE = 16000


class RealtimeVoiceSession:
    def __init__(self):
        self.lock = threading.RLock()
        self.token = ''
        self.order = 0
        self.phase = 'idle'
        self.parts = []
        self.pending = {}
        self.next_seq = 1
        self.revision = 0
        self.published = -1
        self.count = 0
        self.anchor = 0
        self.prefix = ''
        self.final_seq = None
        self.error = ''

    def __deepcopy__(self, memo):
        # Gradio deepcopies the initial State for each browser session.
        return type(self)()

    def clear(self):
        with self.lock:
            self.phase = 'idle'
            self.parts = []
            self.pending = {}
            self.revision += 1
            self.count = 0
            self.prefix = ''
            self.anchor = 0

    def receive(self, raw: str) -> bool:
        """Only buffer audio here: this callback never waits for model inference."""
        if len(raw or '') > 1_500_000:
            raise VoiceInputError('录音片段过大，请重新录音。')
        try:
            msg = json.loads(raw)
            token = msg['id']
            if not isinstance(token, str) or not 1 <= len(token) <= 64:
                raise ValueError()
            seq = msg['seq']
            if type(seq) is not int or seq < 0:
                raise ValueError()
            with self.lock:
                if msg.get('kind') == 'start':
                    order = msg['order']
                    if type(order) is not int or order <= self.order:
                        return False
                    self.__reset(token, order)
                    return True
                if token != self.token or self.phase not in {'recording', 'finishing'}:
                    return False
                if seq < self.next_seq or seq in self.pending:
                    return False
                if seq > self.next_seq + 12:
                    raise VoiceInputError('录音片段传输中断，请停止后重新录制。')
                if self.final_seq is not None and seq > self.final_seq:
                    return False
                rate = msg['rate']
                if type(rate) is not int or not 8000 <= rate <= 192000:
                    raise ValueError()
                data = base64.b64decode(msg.get('pcm', ''), validate=True)
                if len(data) % 2 or len(data) > rate * 2 * 10:
                    raise ValueError()
                samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
                if rate != RATE and samples.size:
                    try:
                        from scipy.signal import resample_poly
                    except ImportError as exc:
                        raise VoiceInputError('实时语音组件未安装完整，请运行 install_voice.ps1。') from exc
                    divisor = gcd(rate, RATE)
                    samples = resample_poly(samples, RATE // divisor, rate // divisor).astype(np.float32)
                if msg.get('final') is True:
                    self.final_seq = seq
                    self.phase = 'finishing'
                self.pending[seq] = samples
                while self.next_seq in self.pending:
                    block = self.pending.pop(self.next_seq)
                    if self.count + len(block) > RATE * MAX_AUDIO_SECONDS:
                        self.phase = 'error'
                        self.error = f'录音超过 {MAX_AUDIO_SECONDS} 秒，请重新分段录制。'
                        self.parts = []
                        self.pending = {}
                        raise VoiceInputError(self.error)
                    self.parts.append(block)
                    self.count += len(block)
                    self.next_seq += 1
                    self.revision += 1
                if self.final_seq is not None and self.next_seq > self.final_seq:
                    self.phase = 'final'
                return True
        except VoiceInputError:
            raise
        except (KeyError, ValueError, TypeError, binascii.Error) as exc:
            raise VoiceInputError('录音片段无效，请停止后重新录音。') from exc

    def __reset(self, token, order):
        self.token, self.order = token, order
        self.phase = 'recording'
        self.parts, self.pending = [], {}
        self.next_seq, self.revision, self.published = 1, 0, -1
        self.count, self.anchor, self.prefix = 0, 0, ''
        self.final_seq, self.error = None, ''

    def recognize(self, guard, region=None):
        """Return (draft, final), or None when no update/stale; never publish old audio."""
        with self.lock:
            if self.phase not in {'recording', 'final'} or self.published == self.revision:
                return None
            token, revision = self.token, self.revision
            final = self.phase == 'final'
            if not final and self.count < RATE:
                return None
            all_audio = np.concatenate(self.parts) if self.parts else np.empty(0, dtype=np.float32)
            anchor = 0 if final else self.anchor
            prefix = '' if final else self.prefix
            audio = all_audio[anchor:]
        try:
            segments = transcribe_samples(audio, final=final)
            text = prefix + ''.join(s['text'] for s in segments).strip()
            if not text:
                draft = VoiceDraft(message='没有识别到有效语音，请重新录制。' if final else '正在聆听…')
            else:
                draft = validate_voice_text(text, guard, region)
                if draft.ok:
                    draft = VoiceDraft(text=draft.text, ok=True, message=(
                        '转写完成，可修改后填入输入框。' if final else '正在实时转写，停止后会校正全文…'))
        except VoiceInputError as exc:
            draft = VoiceDraft(message=str(exc))
        except Exception:
            draft = VoiceDraft(message='实时识别暂不可用，请重试或改用文字输入。')
        with self.lock:
            if token != self.token or self.phase not in {'recording', 'final'}:
                return None
            if final != (self.phase == 'final') or revision <= self.published:
                return None
            self.published = revision
            if final:
                self.phase = 'done'
                self.parts = []
                self.pending = {}
                self.count = 0
                self.prefix = ''
                self.anchor = 0
            elif draft.ok and len(audio) > RATE * 16:
                # Commit complete, older segments; keep 4 seconds of context for corrections.
                eligible = [s for s in segments if 0 < s['end'] <= len(audio) / RATE - 4]
                if eligible:
                    cut = eligible[-1]['end']
                    self.prefix = prefix + ''.join(s['text'] for s in eligible).strip()
                    self.anchor = anchor + int(cut * RATE)
            return draft, final
