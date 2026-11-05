# 一键运行演示（右键 -> 使用 PowerShell 运行）
# 前提：已经设过 DEEPSEEK_API_KEY 环境变量
Set-Location $PSScriptRoot
& ".\.venv\Scripts\python.exe" demo.py
Read-Host "`n按回车关闭"
