param(
    [string]$Mirror = "https://hf-mirror.com"
)
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# 下载并校验浏览器端语音唤醒（KWS）所需的本地模型与 WASM 运行时。
# 均为公开的 Apache-2.0 资源，不包含任何账号、密钥或用户数据。
# 唤醒词“小安小安”在浏览器本地检测，模型文件由 LUX 自身提供（不走第三方 CDN）。

$modelDir = Join-Path $PSScriptRoot "models\kws"
$frontendDir = Join-Path $PSScriptRoot "frontend\kws\prebuilt"
New-Item -ItemType Directory -Force -Path $modelDir, $frontendDir | Out-Null

function Get-Sha256([string]$Path) {
    $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
    return $hash.Hash.ToLowerInvariant()
}

# ---- KWS WASM 运行时（来自 @sherpaw/kws@0.3.0，Apache-2.0）----
$wasmPath = Join-Path $frontendDir "kws.wasm"
$wasmSha = "b75814d1f900cc99efc7de51ae561ffb2517456307488ec4387689c953b9b983"
if (-not (Test-Path -LiteralPath $wasmPath) -or (Get-Sha256 $wasmPath) -ne $wasmSha) {
    $tmpDir = Join-Path $env:TEMP ("lux-kws-wasm-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $tmpDir | Out-Null
    try {
        $tgz = Join-Path $tmpDir "kws.tgz"
        Write-Host "Downloading KWS WASM runtime (@sherpaw/kws 0.3.0)..."
        Invoke-WebRequest -Uri "https://registry.npmjs.org/@sherpaw/kws/-/kws-0.3.0.tgz" -OutFile $tgz
        tar -xzf $tgz -C $tmpDir "package/dist/prebuilt/kws.wasm"
        Copy-Item -LiteralPath (Join-Path $tmpDir "package\dist\prebuilt\kws.wasm") -Destination $wasmPath -Force
        if ((Get-Sha256 $wasmPath) -ne $wasmSha) { throw "KWS WASM 校验失败，请重试。" }
    } finally {
        Remove-Item -Recurse -Force -LiteralPath $tmpDir -ErrorAction SilentlyContinue
    }
} else {
    Write-Host "KWS WASM runtime already present."
}

# ---- KWS 模型（sherpa-onnx zh-en Zipformer 3M，Apache-2.0）----
$dataPath = Join-Path $modelDir "preload.data"
$metaPath = Join-Path $modelDir "preload.js.metadata"
$dataSha = "4ca3ae0d147df1576fac87a4837f5a8959f4f87600a8306c57cc67eb8b40494d"
$metaSha = "e7e223298765f02cdc868d8d1af52fd2c0da6aeef2a5af4122489fd7a8ed1e7b"
$modelRepo = "$Mirror/moeru-ai/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20/resolve/main"

if (-not (Test-Path -LiteralPath $dataPath) -or (Get-Sha256 $dataPath) -ne $dataSha) {
    Write-Host "Downloading KWS model data (about 13 MB)..."
    Invoke-WebRequest -Uri "$modelRepo/install/bin/wasm/preload.data" -OutFile $dataPath
    if ((Get-Sha256 $dataPath) -ne $dataSha) { throw "KWS 模型数据校验失败，请删除后重试。" }
} else {
    Write-Host "KWS model data already present."
}
if (-not (Test-Path -LiteralPath $metaPath) -or (Get-Sha256 $metaPath) -ne $metaSha) {
    Write-Host "Downloading KWS model metadata..."
    Invoke-WebRequest -Uri "$modelRepo/install/bin/wasm/preload.js.metadata" -OutFile $metaPath
    if ((Get-Sha256 $metaPath) -ne $metaSha) { throw "KWS 模型元数据校验失败，请删除后重试。" }
} else {
    Write-Host "KWS model metadata already present."
}

Write-Host "语音唤醒（KWS）资源已就绪。在 .env 设置 LUX_VOICE_DIALOGUE_ENABLED=true 后重启 LUX 即可。" -ForegroundColor Green
