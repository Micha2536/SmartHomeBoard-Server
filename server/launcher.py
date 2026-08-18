import os
import signal
import subprocess
import sys
import time

from .config import SETUP_PORT, load_server_config


def main():
    configuration = load_server_config()
    api_port = configuration["port"]
    if api_port == SETUP_PORT:
        raise SystemExit(f"Der Kommunikationsport darf nicht dem Setup-Port {SETUP_PORT} entsprechen.")

    processes = [
        subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:setup_app", "--host", "0.0.0.0", "--port", str(SETUP_PORT)]),
        subprocess.Popen([sys.executable, "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", str(api_port)]),
    ]

    def stop(_signal=None, _frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        # Docker startet beide Dienste nach einer Portänderung gemeinsam neu.
        os._exit(0)


if __name__ == "__main__":
    main()
