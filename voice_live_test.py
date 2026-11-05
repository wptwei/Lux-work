
import gradio as gr
import numpy as np
from scipy.signal import resample_poly
from math import gcd
import traceback

from voice_asr import load_model

TARGET_SR = 16000


def process_audio(chunk, history):
    """接收录音片段，持续更新识别文本。"""

    if chunk is None:
        return history, ""

    try:
        sr, audio = chunk

        if audio is None or len(audio) == 0:
            return history, ""

        # 转为单声道
        if audio.ndim > 1:
            audio = audio.mean(axis=1)

        # 转为 float32 音频
        if np.issubdtype(audio.dtype, np.integer):
            scale = max(
                abs(np.iinfo(audio.dtype).min),
                np.iinfo(audio.dtype).max
            )
            audio = audio.astype(np.float32) / scale
        else:
            audio = audio.astype(np.float32)

        # 重采样为 16kHz
        if sr != TARGET_SR:
            d = gcd(sr, TARGET_SR)
            audio = resample_poly(
                audio,
                TARGET_SR // d,
                sr // d
            ).astype(np.float32)

        # 保存这次录音的历史音频
        if history is None:
            history = audio
        else:
            history = np.concatenate([history, audio])

        duration = len(history) / TARGET_SR
        print(f"已接收音频：{duration:.1f} 秒", flush=True)

        if duration < 1:
            return history, "正在聆听..."

        # 调用现有 Whisper 模型
        model = load_model()

        segments, info = model.transcribe(
            history,
            language="zh",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False
        )

        text = "".join(
            seg.text for seg in segments
        ).strip()

        print("实时识别：", text, flush=True)

        return history, text

    except Exception as e:
        traceback.print_exc()
        raise gr.Error(
            f"实时识别失败：{type(e).__name__}: {e}"
        )


with gr.Blocks(title="LUX 实时语音识别") as demo:

    gr.Markdown("# LUX 实时语音识别")
    gr.Markdown(
        "开始录音后，系统会持续识别并更新文字。"
        "建议先测试 10～15 秒的中文语音。"
    )

    history = gr.State(value=None)

    mic = gr.Audio(
        sources=["microphone"],
        type="numpy",
        streaming=True,
        label="实时语音输入"
    )

    result = gr.Textbox(
        label="实时转写结果",
        lines=6,
        interactive=True
    )

    # 开始新录音时清除历史
    mic.start_recording(
        fn=lambda: (None, ""),
        inputs=[],
        outputs=[history, result],
        queue=False
    )

    # 每隔 2 秒发送一个音频片段
    mic.stream(
        fn=process_audio,
        inputs=[mic, history],
        outputs=[history, result],
        stream_every=2.0,
        time_limit=120,
        concurrency_limit=1
    )


demo.queue().launch(
    server_name="127.0.0.1",
    server_port=7866,
    share=False,
    show_error=True
)
