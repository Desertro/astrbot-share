$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'Install uv first: https://docs.astral.sh/uv/getting-started/installation/' }
uv sync --frozen --inexact
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
if (-not (Test-Path -LiteralPath 'data/cmd_config.json')) {
    uv run --no-sync python tools/setup.py
    if ($LASTEXITCODE -ne 0) { throw 'Configuration failed' }
}
uv run --no-sync python tools/setup.py --check
if ($LASTEXITCODE -ne 0) { throw 'Configuration check failed' }
if (-not (Test-Path -LiteralPath '.venv/.share-plugins-installed')) {
    uv pip install --python .venv/Scripts/python.exe -r requirements-plugins.txt --override local-dependency-overrides.txt
    if ($LASTEXITCODE -ne 0) { throw 'Plugin dependency installation failed' }
    uv run --no-sync python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { throw 'Browser installation failed' }
    New-Item -ItemType File -Path '.venv/.share-plugins-installed' -Force | Out-Null
}
uv run --no-sync python main.py
if ($LASTEXITCODE -ne 0) { throw 'AstrBot exited with an error' }
