# 一条命令跑起来：建环境 -> 装依赖 -> 重建 -> 起服务。
#   powershell -File start.ps1
#   $env:PORT = 9000; powershell -File start.ps1
# 配 Key 再加三个环境变量（见 README.md 第一节、LLM_SETUP.md）。
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
$Port = if ($env:PORT) { $env:PORT } else { "8000" }
$Py = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $Py)) {
  Write-Host "[1/3] 建虚拟环境"
  python -m venv (Join-Path $Root ".venv")
} else {
  Write-Host "[1/3] 虚拟环境已存在，跳过"
}

Write-Host "[2/3] 安装依赖"
& $Py -m pip install -q --disable-pip-version-check -r (Join-Path $Root "starter\requirements.txt")

Write-Host "[3/3] 重建清洗表与索引"
Push-Location (Join-Path $Root "starter")
& $Py -m kbqa.rebuild

Write-Host ""
Write-Host "服务地址：http://127.0.0.1:$Port"
if ($env:LLM_API_KEY) {
  $Model = if ($env:LLM_MODEL) { $env:LLM_MODEL } else { "未设置" }
  Write-Host "模型：$Model（live 模式）"
} else {
  Write-Host "模型：未配置 LLM_API_KEY，问答走降级模式（见 README.md 第三节）"
}
Write-Host "按 Ctrl+C 停止。"
Write-Host ""
& $Py -m uvicorn kbqa.server:app --host 127.0.0.1 --port $Port
Pop-Location
