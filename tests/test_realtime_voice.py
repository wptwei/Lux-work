from __future__ import annotations
import base64
import copy
import json
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch
import numpy as np
from core.realtime_voice import RealtimeVoiceSession, RATE
from voice_asr import VoiceInputError


def start(session, token='one', order=1):
    return session.receive(json.dumps({'id':token,'kind':'start','seq':0,'order':order}))


def chunk(session, seq, *, token='one', final=False, seconds=1, rate=16000):
    audio=np.full(int(rate*seconds),1000,dtype='<i2')
    return session.receive(json.dumps({'id':token,'seq':seq,'rate':rate,'pcm':base64.b64encode(audio.tobytes()).decode(),'final':final}))


class RealtimeVoiceTests(unittest.TestCase):
    def setUp(self):
        self.session=RealtimeVoiceSession(); start(self.session)
        self.guard=Mock();self.guard.scan.return_value=('allow',[],{})

    def test_stream_returns_text_before_stop(self):
        chunk(self.session,1,seconds=2)
        with patch('core.realtime_voice.transcribe_samples',return_value=[{'text':'今天巡检','start':0,'end':1.8}]) as model:
            draft,final=self.session.recognize(self.guard)
        self.assertTrue(draft.ok);self.assertFalse(final);self.assertEqual(draft.text,'今天巡检')
        self.assertFalse(model.call_args.kwargs['final'])

    def test_stop_keeps_short_final_chunk_and_releases_audio(self):
        chunk(self.session,1,seconds=2);chunk(self.session,2,seconds=.125,final=True)
        with patch('core.realtime_voice.transcribe_samples',return_value=[{'text':'今天巡检正常。','start':0,'end':2.1}]) as model:
            draft,final=self.session.recognize(self.guard)
        self.assertTrue(final);self.assertTrue(draft.ok)
        self.assertEqual(len(model.call_args.args[0]),34000)
        self.assertTrue(model.call_args.kwargs['final'])
        self.assertEqual(self.session.parts,[])

    def test_out_of_order_waits_for_missing_packet(self):
        chunk(self.session,2,final=True)
        self.assertEqual(self.session.phase,'finishing')
        self.assertIsNone(self.session.recognize(self.guard))
        chunk(self.session,1)
        self.assertEqual(self.session.phase,'final');self.assertEqual(self.session.count,2*RATE)

    def test_duplicate_packet_does_not_duplicate_audio(self):
        chunk(self.session,1);chunk(self.session,1)
        self.assertEqual(self.session.count,RATE)

    def test_next_recording_ignores_previous_tail(self):
        chunk(self.session,1);start(self.session,'two',2)
        self.assertFalse(chunk(self.session,2,token='one',final=True))
        self.assertEqual(self.session.count,0)
        self.assertFalse(start(self.session,'one',1))

    def test_copied_gradio_state_is_empty_and_independent(self):
        chunk(self.session,1)
        other=copy.deepcopy(self.session);start(other,'other',2);chunk(other,1,token='other',seconds=2)
        self.assertEqual(self.session.count,RATE);self.assertEqual(other.count,2*RATE)

    def test_stale_model_result_does_not_overwrite_new_recording(self):
        chunk(self.session,1)
        entered,release=Event(),Event();result=[]
        def slow(*args,**kwargs):
            entered.set();release.wait(3)
            return [{'text':'旧录音','start':0,'end':1}]
        with patch('core.realtime_voice.transcribe_samples',side_effect=slow):
            worker=Thread(target=lambda:result.append(self.session.recognize(self.guard)));worker.start()
            self.assertTrue(entered.wait(3));start(self.session,'two',2);release.set();worker.join(3)
        self.assertEqual(result,[None])

    def test_final_packet_suppresses_inflight_partial(self):
        chunk(self.session,1)
        def finish(*args,**kwargs):
            chunk(self.session,2,final=True,seconds=.1)
            return [{'text':'未校正','start':0,'end':1}]
        with patch('core.realtime_voice.transcribe_samples',side_effect=finish):
            self.assertIsNone(self.session.recognize(self.guard))

    def test_redline_applies_to_partial_and_final(self):
        chunk(self.session,1)
        self.guard.scan.return_value=('block',[],{})
        with patch('core.realtime_voice.transcribe_samples',return_value=[{'text':'禁止显示','start':0,'end':1}]):
            draft,final=self.session.recognize(self.guard)
        self.assertFalse(draft.ok);self.assertEqual(draft.text,'')

    def test_complete_segments_reduce_repeated_live_decoding(self):
        for i in range(1,4):chunk(self.session,i,seconds=6)
        with patch('core.realtime_voice.transcribe_samples',return_value=[{'text':'前句。','start':0,'end':10},{'text':'后句','start':10,'end':18}]):
            self.session.recognize(self.guard)
        self.assertEqual(self.session.anchor,10*RATE);self.assertEqual(self.session.prefix,'前句。')
        chunk(self.session,4,final=True,seconds=1)
        with patch('core.realtime_voice.transcribe_samples',return_value=[]) as model:
            self.session.recognize(self.guard)
        self.assertEqual(len(model.call_args.args[0]),19*RATE)

    def test_resample_48khz(self):
        chunk(self.session,1,rate=48000)
        self.assertEqual(self.session.count,RATE)
        self.assertAlmostEqual(float(self.session.parts[0][100]),1000/32768,places=3)

    def test_time_limit_and_corrupt_audio(self):
        for i in range(1,13):chunk(self.session,i,seconds=10)
        with self.assertRaises(VoiceInputError):chunk(self.session,13,seconds=.1)
        self.assertEqual(self.session.parts,[])
        start(self.session,'new',2)
        with self.assertRaises(VoiceInputError):
            self.session.receive(json.dumps({'id':'new','seq':1,'rate':16000,'pcm':'not base64!'}))

    def test_clear_prevents_pending_updates(self):
        chunk(self.session,1);self.session.clear()
        self.assertFalse(chunk(self.session,2,final=True))
        self.assertIsNone(self.session.recognize(self.guard))


if __name__=='__main__':unittest.main()
