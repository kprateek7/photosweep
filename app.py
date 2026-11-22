"""PhotoSweep launcher: starts the local FastAPI UI and opens your browser."""

from __future__ import annotations

import argparse
import multiprocessing
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from photosweep.server import create_app


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _is_photosweep_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=1.0) as resp:
            body = resp.read(2048).decode("utf-8", errors="ignore")
            return "PhotoSweep" in body
    except Exception:
        return False


def _find_available_port(host: str, preferred: int, span: int = 100) -> int:
    for port in range(preferred, preferred + span):
        if _is_port_free(host, port):
            return port
    # Fall back to OS-assigned ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def main() -> None:
    parser = argparse.ArgumentParser(description="PhotoSweep - Local Photo & Video Cleaner")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Preferred port (default: 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open browser")
    args = parser.parse_args()

    port = args.port
    if not _is_port_free(args.host, port):
        if _is_photosweep_running(args.host, port):
            url = f"http://{args.host}:{port}"
            print(f"PhotoSweep is already running at {url} — opening browser...")
            if not args.no_browser:
                webbrowser.open(url)
            return
        new_port = _find_available_port(args.host, port + 1)
        print(f"Port {port} is in use by another app; switching to port {new_port}.")
        port = new_port

    url = f"http://{args.host}:{port}"
    print(f"PhotoSweep starting at {url}")
    print("Close this window (or press Ctrl+C) to stop the server.")

    if not args.no_browser:
        def _open() -> None:
            time.sleep(1.0)
            webbrowser.open(url)

        threading.Thread(target=_open, daemon=True).start()

    app = create_app()
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
