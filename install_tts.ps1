param(
    [string]$Mirror = "https://hf-mirror.com"
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 下载并校验回答朗读（TTS）所需的本地 Kokoro 中文多语模型（int8，Apache-2.0）。
# 模型来自 huggingface csukuangfj/kokoro-int8-multi-lang-v1_0（sherpa-onnx 官方发布），
# 走 hf-mirror 镜像；huggingface_hub 会对大文件做 sha256 校验，resume 可断点续传。
# 模型约 160MB，含中文数字/日期/电话正规化所需的 .fst 文件。

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到 .venv\Scripts\python.exe，请先在项目根目录创建虚拟环境并安装依赖。"
}

$dest = Join-Path $PSScriptRoot "models\tts\kokoro-int8-multi-lang-v1_0"
$modelFile = Join-Path $dest "model.int8.onnx"

if (Test-Path -LiteralPath $modelFile) {
    Write-Host "TTS model already present: $modelFile" -ForegroundColor Green
} else {
    $script = @"
import os
os.environ.setdefault("HF_ENDPOINT", r"$Mirror")
try:
    from huggingface_hub import snapshot_download
except ImportError:
    raise SystemExit("missing huggingface_hub: run .venv\\Scripts\\python.exe -m pip install huggingface_hub")
repo = "csukuangfj/kokoro-int8-multi-lang-v1_0"
dest = r"$dest"
print("Downloading TTS model", repo, "->", dest)
snapshot_download(repo_id=repo, local_dir=dest, local_dir_use_symlinks=False,
                  ignore_patterns=[".gitattributes"], resume_download=True)
print("TTS model downloaded.")
"@
    & $python -c $script
    if ($LASTEXITCODE -ne 0) { throw "TTS model download failed (exit $LASTEXITCODE)." }
}

if (-not (Test-Path -LiteralPath $modelFile)) {
    throw "TTS model incomplete: missing model.int8.onnx."
}

Write-Host "TTS resources ready. Set LUX_TTS_ENABLED=true in .env and restart LUX." -ForegroundColor Green
