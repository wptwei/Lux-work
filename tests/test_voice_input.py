from __future__ import annotations
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import voice_asr
from core import voice_input


class VoiceDraftTests(unittest.TestCase):
    def setUp(self):
        self.guard = Mock()
        self.guard.scan.return_value = ('allow', [], {})

    def test_transcript_is_checked_before_preview(self):
        with patch.object(voice_input, 'speech_to_text', return_value='  今天巡检正常。 '):
            result = voice_input.transcribe_voice('clip.wav', self.guard, 'H区')
        self.assertTrue(result.ok)
        self.assertEqual(result.text, '今天巡检正常。')
        self.guard.scan.assert_called_once_with('今天巡检正常。', source_type='voice_transcript', region='H区')

    def test_block_and_review_do_not_expose_transcript(self):
        for level in ['block','review']:
            with self.subTest(level=level), patch.object(voice_input, 'speech_to_text', return_value='敏感原文'):
                self.guard.scan.return_value=(level, [], {})
                result=voice_input.transcribe_voice('clip.wav', self.guard)
                self.assertFalse(result.ok); self.assertEqual(result.text, '')
                self.assertNotIn('敏感原文', result.message)

    def test_missing_model_returns_actionable_message(self):
        with patch.object(voice_input, 'speech_to_text', side_effect=voice_asr.VoiceInputError('请运行 install_voice.ps1')):
            result=voice_input.transcribe_voice('clip.wav', self.guard)
        self.assertFalse(result.ok); self.assertIn('install_voice.ps1', result.message)

    def test_unexpected_exception_does_not_expose_path(self):
        with patch.object(voice_input, 'speech_to_text', side_effect=RuntimeError('secret/path')):
            result=voice_input.transcribe_voice('clip.wav', self.guard)
        self.assertNotIn('secret', result.message)

    def test_append_preserves_existing_text_and_does_not_send(self):
        result=voice_input.append_voice_text('新增内容', '原有内容', self.guard)
        self.assertTrue(result.ok); self.assertEqual(result.text, '原有内容\n新增内容')

    def test_empty_or_blocked_edit_preserves_draft(self):
        for text,level in [('', 'allow'),('编辑后违规内容', 'block')]:
            self.guard.scan.return_value=(level,[],{})
            result=voice_input.append_voice_text(text, '原有内容', self.guard)
            self.assertFalse(result.ok); self.assertEqual(result.text, '原有内容')

    def test_length_overflow_preserves_draft(self):
        with patch.object(voice_input, 'settings', SimpleNamespace(max_input_chars=5)):
            result=voice_input.append_voice_text('新增', '原有内容', self.guard)
        self.assertFalse(result.ok); self.assertEqual(result.text, '原有内容')


class LocalSpeechTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.path=Path(self.tmp.name)/'test.wav'
        with wave.open(str(self.path),'wb') as f:
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000); f.writeframes(b'\0\0'*16000)
        voice_asr.load_model.cache_clear()

    def tearDown(self):
        voice_asr.load_model.cache_clear(); self.tmp.cleanup()

    def test_missing_file_does_not_load_model(self):
        with patch.object(voice_asr,'load_model') as loader:
            for path in [None, str(self.path)+'missing']:
                with self.assertRaises(voice_asr.VoiceInputError): voice_asr.speech_to_text(path)
            loader.assert_not_called()

    def test_long_recording_rejected_before_model_load(self):
        with patch.object(voice_asr,'_audio_duration',return_value=121), patch.object(voice_asr,'load_model') as loader:
            with self.assertRaisesRegex(voice_asr.VoiceInputError,'120'): voice_asr.speech_to_text(self.path)
            loader.assert_not_called()

    def test_transcribe_consumes_lazy_segments(self):
        model=Mock(); model.transcribe.return_value=(iter([SimpleNamespace(text=' 今天'),SimpleNamespace(text='巡检正常。')]),SimpleNamespace(duration=1))
        with patch.object(voice_asr,'_audio_duration',return_value=1),patch.object(voice_asr,'load_model',return_value=model):
            self.assertEqual(voice_asr.speech_to_text(self.path),'今天巡检正常。')
        self.assertEqual(model.transcribe.call_args.kwargs['language'],'zh')
        self.assertTrue(model.transcribe.call_args.kwargs['vad_filter'])

    def test_empty_transcription_is_not_success(self):
        model=Mock(); model.transcribe.return_value=(iter([]),SimpleNamespace(duration=1))
        with patch.object(voice_asr,'_audio_duration',return_value=1),patch.object(voice_asr,'load_model',return_value=model):
            with self.assertRaisesRegex(voice_asr.VoiceInputError,'有效语音'): voice_asr.speech_to_text(self.path)

    def test_normal_model_load_is_offline(self):
        model_class=Mock()
        with patch.object(voice_asr,'_whisper_class',return_value=model_class): voice_asr.load_model()
        self.assertIs(model_class.call_args.kwargs['local_files_only'],True)

    def test_prepare_is_explicit_download(self):
        model_class=Mock()
        with patch.object(voice_asr,'MODEL_DIR',Path(self.tmp.name)/'models'),patch.object(voice_asr,'_whisper_class',return_value=model_class): voice_asr.prepare_model()
        self.assertIs(model_class.call_args.kwargs['local_files_only'],False)


if __name__=='__main__': unittest.main()
