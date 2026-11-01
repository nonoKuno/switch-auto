"""端末の q / Enter と Ctrl+C を受ける。"""
import select
import sys
import termios
import threading
import tty


class Keys:

    def __init__(self):
        self.quit = threading.Event()
        self.enter = threading.Event()
        self._stop = threading.Event()
        self._old = None
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        if not sys.stdin.isatty():
            return
        self._old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1)
        if self._old is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)
            self._old = None

    def _loop(self):
        while not self._stop.is_set():
            if not select.select([sys.stdin], [], [], 0.1)[0]:
                continue
            ch = sys.stdin.read(1)
            if ch in ("q", "\x03"):
                self.quit.set()
            elif ch in ("\n", "\r"):
                self.enter.set()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
