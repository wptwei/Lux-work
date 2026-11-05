
import gradio as gr
from pathlib import Path
import traceback

from voice_asr import speech_to_text


def recognize_audio(audio_path):
    print("[1] 语音识别按钮已触发", flush=True)
    print(f"[2] 收到音频路径: {audio_path}", flush=True)

    if not audio_path:
        raise gr.Error("没有收到音频，请先完成录音。")

    path = Path(audio_path)

    if not path.is_file():
        raise gr.Error("音频文件不存在。")

    size = path.stat().st_size
    print(f"[3] 音频文件大小: {size} 字节", flush=True)

    if size < 1024:
        raise gr.Error("音频文件过小，请重新录音。")

    try:
        print("[4] 开始加载模型并识别...", flush=True)

        text = speech_to_text(str(path))

        print(f"[5] 识别结果: {text!r}", flush=True)

        if not text:
            return "没有识别到有效语音，请检查麦克风或重新录制。"

        return text

    except Exception as e:
        traceback.print_exc()
        raise gr.Error(
            f"识别失败：{type(e).__name__}: {e}"
        )


with gr.Blocks(title="LUX 本地语音识别测试") as demo:

    gr.Markdown("## LUX 本地语音识别测试")

    audio = gr.Audio(
        sources=["microphone", "upload"],
        type="filepath",
        label="录音或上传音频"
    )

    btn = gr.Button(
        "开始语音识别",
        variant="primary"
    )

    result = gr.Textbox(
        label="语音识别结果",
        lines=5
    )

    btn.click(
        fn=recognize_audio,
        inputs=audio,
        outputs=result
    )


demo.launch(
    server_name="127.0.0.1",
    server_port=7865,
    share=False,
    show_error=True
)
