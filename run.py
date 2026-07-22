"""Точка входа для локального запуска: поднимает uvicorn и открывает браузер.

Браузер открывается один раз (поток-наблюдатель живёт в родительском процессе,
поэтому перезапуски reload-воркера его не переоткрывают). Для чистого dev-режима
без браузера по-прежнему можно запускать `uvicorn app.main:app --reload`.
"""
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from app.config import settings

# На некоторых консолях Windows stdout не в UTF-8, и print() с кириллицей
# падает с UnicodeEncodeError, убивая процесс до старта uvicorn.
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
        print(f"[run] Сервер не поднялся за {timeout:.0f}с, браузер не открываю.")
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
        print(f"Открываю {url}, как только сервер поднимется")
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_reload,
    )


if __name__ == "__main__":
    main()
