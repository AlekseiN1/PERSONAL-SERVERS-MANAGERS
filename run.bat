@echo off
REM Запуск приложения (двойной клик или из cmd). Создаёт venv при первом запуске.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [run] Создаю виртуальное окружение .venv ...
    python -m venv .venv
)

echo [run] Проверяю зависимости ...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt

if not exist ".env" (
    echo [run] ВНИМАНИЕ: .env не найден. Скопируй .env.example в .env и заполни токены.
)

echo [run] Старт: http://127.0.0.1:8000  (Ctrl+C для остановки)
".venv\Scripts\python.exe" run.py

endlocal
