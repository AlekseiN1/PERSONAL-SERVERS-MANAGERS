"""Single entry point: creates the venv / installs dependencies on first run,
starts uvicorn, and opens the browser.

The only way to launch the app is `python run.py` (or a double-click if .py is
associated with Python). run.bat/run.ps1 are gone: everything they used to do
(venv, pip install) is now done by this file itself, which re-execs under the venv.

For a plain dev mode without the browser, run `uvicorn app.main:app --reload`
from an already-activated venv.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
VENV_PYTHON = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _in_venv() -> bool:
    try:
        return Path(sys.executable).resolve() == VENV_PYTHON.resolve()
    except OSError:
        return False


def _bootstrap_and_reexec() -> None:
    """Create .venv (if missing), install dependencies, re-exec itself under the venv."""
    if not VENV_PYTHON.exists():
        print(f"[run] Creating virtual environment {VENV_DIR} ...")
        subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)], check=True)

    print("[run] Checking dependencies ...")
    subprocess.run(
        [str(VENV_PYTHON), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
        check=True,
    )

    if not (ROOT / ".env").exists():
        print("[run] WARNING: .env not found. Copy .env.example to .env and fill in the tokens.")

    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])


if not _in_venv():
    _bootstrap_and_reexec()

# Imports that need requirements.txt's dependencies — only after the re-exec under the venv.
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402

# On some Windows consoles stdout isn't UTF-8, and print() with non-ASCII characters
# raises UnicodeEncodeError, killing the process before uvicorn even starts.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _wait_and_open_browser(host: str, port: int, url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.2)
    else:
        print(f"[run] Server didn't come up within {timeout:.0f}s, not opening the browser.")
        return
    webbrowser.open(url)


def main() -> None:
    url = f"http://{settings.app_host}:{settings.app_port}"
    if settings.open_browser:
        threading.Thread(
            target=_wait_and_open_browser,
            args=(settings.app_host, settings.app_port, url),
            daemon=True,
        ).start()
        print(f"Opening {url} as soon as the server is up")
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
    )


if __name__ == "__main__":
    main()
