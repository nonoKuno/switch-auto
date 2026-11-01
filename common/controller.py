"""NUXBT の薄い包み。root で動かす。

入力は「今のパケット」を1つ持ち、変えるたびに set_controller_input で送る。
"""
import os
import threading
import time

STICKS = {"L": "L_STICK", "R": "R_STICK"}
PRESS_STICK = {"L3": "L_STICK", "R3": "R_STICK"}
# NUXBT の set_controller_input 経路は + と − のビットが逆（実機で逆に動いた）。ここで入れ替える
SWAP = {"PLUS": "MINUS", "MINUS": "PLUS"}


class Controller:
    def __init__(self, nx=None):
        self._nx = nx
        self._idx = None
        self._lock = threading.Lock()
        self._packet = None
        self._address = None

    def connect(self, reconnect=True, address=None):
        """reconnect=True は保存済みの Switch に再接続する（持ちかたの画面ではない時）。
        False は新規接続（「持ちかた/順番を変える」の画面で初回につなぐ時）。
        address を渡すと、その Switch に PC から接続しに行く（失敗したら待ち受け）。"""
        if self._nx is None:
            import nuxbt
            self._nx = nuxbt.Nuxbt()
        self._address = address
        self._create(reconnect, address)
        self.press("L")

    def _create(self, reconnect=True, address=None):
        import nuxbt
        if address:
            addrs = [address]
        else:
            addrs = self._nx.get_switch_addresses() if reconnect else None
        self._idx = self._nx.create_controller(
            nuxbt.PRO_CONTROLLER, reconnect_address=addrs or None)
        try:
            while self._nx.state[self._idx]["state"] != "connected":
                if self._nx.state[self._idx]["state"] == "crashed":
                    raise OSError(self._nx.state[self._idx]["errors"])
                time.sleep(0.1)
        except KeyboardInterrupt:
            # 中断のあと NUXBT の終了処理が固まるので、子プロセスを止めて即終了する
            self._nx.controllers.terminate()
            os._exit(1)
        self._packet = self._nx.create_input_packet()
        self._send()

    def reconnect(self):
        """1回だけ再接続する。駄目なら例外を投げる。"""
        try:
            self._nx.remove_controller(self._idx)
        except ValueError:
            pass
        self._create(True, self._address)

    def alive(self):
        return self._idx is not None and self._nx.state[self._idx]["state"] == "connected"

    def _send(self):
        self._nx.set_controller_input(self._idx, self._packet)

    def _set(self, b, v):
        if b in PRESS_STICK:
            self._packet[PRESS_STICK[b]]["PRESSED"] = v
        else:
            self._packet[SWAP.get(b, b)] = v

    def hold(self, *buttons):
        with self._lock:
            for b in buttons:
                self._set(b, True)
            self._send()

    def release(self, *buttons):
        with self._lock:
            for b in buttons:
                self._set(b, False)
            self._send()

    def press(self, *buttons, down=0.1):
        self.hold(*buttons)
        time.sleep(down)
        self.release(*buttons)

    def stick(self, side, x, y):
        """side は "L" か "R"、x・y は -100〜100。(0, 0) で戻す。"""
        with self._lock:
            s = self._packet[STICKS[side]]
            s["X_VALUE"] = x
            s["Y_VALUE"] = y
            self._send()

    def release_all(self):
        with self._lock:
            self._packet = self._nx.create_input_packet()
            self._send()

    def close(self):
        if self._idx is None:
            return
        self.release_all()
        self._nx.remove_controller(self._idx)
        self._idx = None
