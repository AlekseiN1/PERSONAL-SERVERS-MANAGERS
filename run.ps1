# Запуск приложения (PowerShell). Создаёт venv при первом запуске, ставит зависимости,
# поднимает uvicorn. Использование:  .\run.ps1   (Ctrl+C для остановки)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"

# 1. venv
if (-not (Test-Path $py)) {
    Write-Host "[run] Создаю виртуальное окружение .venv ..." -ForegroundColor Cyan
    python -m venv (Join-Path $root ".venv")
}

# 2. зависимости (тихо; ставятся быстро если уже есть)
Write-Host "[run] Проверяю зависимости ..." -ForegroundColor Cyan
& $py -m pip install -q -r (Join-Path $root "requirements.txt")

# 3. .env
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "[run] ВНИМАНИЕ: .env не найден. Скопируй .env.example в .env и заполни PORTAINER_USER/PORTAINER_PASSWORD/MONGO_URI." -ForegroundColor Yellow
}

# 4. запуск (run.py поднимает uvicorn и открывает браузер)
Write-Host "[run] Старт: http://127.0.0.1:8000  (Ctrl+C для остановки)" -ForegroundColor Green
& $py (Join-Path $root "run.py")
