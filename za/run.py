"""flow.yaml を実機で実行する本番のループ。

使い方:
  .venv/bin/python za/run.py --switch AA:BB:CC:DD:EE:FF [--from language] [--to face] [--lap 1] [--port 8000] [--dev [--video]]
起動後は人が周の準備をして、ダッシュボードの「▶ 開始」（端末から起動したときは Enter でも可）で始まる。q か Ctrl+C で全部離して終了。
"""
import argparse
import json
import re
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from common.controller import Controller  # noqa: E402
from common.dashboard import Live, make_live_handler  # noqa: E402
from common.keys import Keys  # noqa: E402
from common.notify import notify  # noqa: E402
from za import flowplay as fp  # noqa: E402
from za.screens import SIGNALS, Screens  # noqa: E402

TICK = 0.1
LATENCY = 0.0  # 映像の遅れ（未測定）。上限の時間に足す
NAMES = {"UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT", "RIGHT": "DPAD_RIGHT"}
SEQ = {"L": "DPAD_LEFT", "R": "DPAD_RIGHT", "U": "DPAD_UP", "D": "DPAD_DOWN", "A": "A"}
DIR_XY = {"up": (0, 100), "down": (0, -100), "left": (-100, 0), "right": (100, 0)}
PRESS_DOWN = 0.1
MENU_OPEN_IGNORE = 0.3  # ボタンを押してからこの秒数以内の menu_open は、画面遷移中の誤検出として無視する
MASH_GAP = 0.4  # 押している0.1秒と合わせて0.5秒ごと
STEP_GAP = 0.2
RANGE_GAP = 0.4
IDLE_MAX = 0.5  # replay で、スティック→スティックの間と範囲の頭・終わりの何も押していない間はこの秒数まで詰める
REPLAY_DELAY = 1.0  # replay は wait lit のあと、さらにこの秒数待ってから流す
LIT_MIN = 1.0  # 暗転でない状態がこの秒数続いたら「明けた」（wait lit・lit_ のコマ）
VIDEO_FPS = 30
VIDEO_RANGES = {"player_1": ("player_1", "player_1"), "x_x": ("talk_30", "talk_31")}  # --video で、録画の名前: (始めの区間, 終わりの区間)。この間にいる間を録画する
STICK_DIRS = {"UP": (0, 100), "DOWN": (0, -100), "LEFT": (-100, 0), "RIGHT": (100, 0)}


def btn(name):
    return NAMES.get(name, name)


class Recorder:
    """live.frame を VIDEO_FPS で mp4 に書く。start のたびに別ファイル、stop で閉じる。"""

    def __init__(self, live):
        self.live, self._th, self._stop = live, None, threading.Event()

    def start(self, path):
        self.stop()
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, args=(str(path), self._stop), daemon=True)
        self._th.start()

    def stop(self):
        if self._th:
            self._stop.set()
            self._th.join(timeout=3)
            self._th = None

    def _loop(self, path, stop):
        w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), VIDEO_FPS, (1280, 720))
        try:
            while not stop.is_set():
                t = time.monotonic()
                frame = self.live.frame
                if frame is not None:
                    w.write(frame)
                stop.wait(max(0.0, 1 / VIDEO_FPS - (time.monotonic() - t)))
        finally:
            w.release()


class InputLog:
    """Executor が使う pad の包み。押す・離す・倒すたびに inputs.jsonl へ {t, section, op, args} を書く。"""

    def __init__(self, pad, run, section):
        self._pad, self._run, self._section = pad, run, section

    def _w(self, op, *args):
        self._run.log_input(self._section, op, list(args))

    def press(self, *buttons, down=0.1):
        self._w("press", *buttons)
        self._pad.press(*buttons, down=down)

    def hold(self, *buttons):
        self._w("hold", *buttons)
        self._run.pc_held |= set(buttons)
        self._pad.hold(*buttons)

    def release(self, *buttons):
        self._w("release", *buttons)
        self._run.pc_held -= set(buttons)
        self._pad.release(*buttons)

    def stick(self, side, x, y):
        self._w("stick", side, x, y)
        h = self._run.pc_held - {f"{side}S_{d}" for d in ("UP", "DOWN", "LEFT", "RIGHT")}
        self._run.pc_held = h | ({f"{side}S_UP"} if y > 0 else {f"{side}S_DOWN"} if y < 0 else set()) \
            | ({f"{side}S_RIGHT"} if x > 0 else {f"{side}S_LEFT"} if x < 0 else set())
        self._pad.stick(side, x, y)

    def release_all(self):
        self._w("release_all")
        self._run.pc_held = set()
        self._pad.release_all()


class Executor(threading.Thread):
    """区間の steps を上から順に行う。stop が立ったら止まる。最後まで行ったら done が立つ。"""

    def __init__(self, run, sec):
        super().__init__(daemon=True)
        self.run_, self.sec = run, sec
        self.pad = InputLog(run.pad, run, sec["id"])
        self.stop = threading.Event()
        self.done = threading.Event()
        self.replayed = threading.Event()
        self.half = threading.Event()  # replay の再生の時間の半分が過ぎた（blackout_end を数え始める）
        self.error = None

    def run(self):
        try:
            if self.sec["kind"] == "replay":
                self._replay()
                self.done.set()
                return
            while True:
                for step in self.sec["steps"]:
                    if self.stop.is_set():
                        return
                    self._do(step.split())
                if self.sec["kind"] != "battle":
                    break  # battle は終わりの合図で止められるまで繰り返す
            self.done.set()
        except Exception as e:  # noqa: BLE001 コントローラの失敗は main が拾って人に渡す
            self.error = e

    def _sleep(self, sec):
        self.stop.wait(sec)

    def _replay(self):
        """wait lit のあと、記録の trim の範囲の入力を記録どおりの間隔で押す。
        trim は [始め, 終わり] か、その list（範囲をつなげる。間は RANGE_GAP 秒空け、範囲の終わりで全部離す）。"""
        for step in self.sec["pre"]:
            if self.stop.is_set():
                return
            self._do(step.split())
        self._do(["wait", "lit"])
        self._sleep(REPLAY_DELAY)
        events = json.loads((ROOT / "za" / "routes" / self.sec["route"]).read_text(encoding="utf-8"))["events"]
        trim = self.sec["trim"]
        ranges = trim if isinstance(trim[0], list) else [trim]
        timeline, offset = [], 0.0  # (開始からの秒, 押す, 離す)
        for a, b in ranges:
            held = set()
            for ev in events:
                if ev["t"] < a:
                    held = (held | set(ev["down"])) - set(ev["up"])
            if held:
                timeline.append((offset, held, set()))
            prev_t, saved = a, 0.0
            prev_stick, first = True, True  # 直前がスティックだけの入力か（ボタンの前後は詰めない）、最初の入力か
            for ev in events:
                if a <= ev["t"] <= b:
                    stick_only = all(x[1:3] == "S_" for x in ev["down"] + ev["up"])
                    gap = ev["t"] - prev_t
                    if not held and gap > IDLE_MAX and (first or (prev_stick and stick_only)):
                        saved += gap - IDLE_MAX
                    timeline.append((offset + ev["t"] - a - saved, set(ev["down"]), set(ev["up"])))
                    held = (held | set(ev["down"])) - set(ev["up"])
                    prev_t, prev_stick, first = ev["t"], stick_only, False
            if not held and b - prev_t > IDLE_MAX:
                saved += b - prev_t - IDLE_MAX
            offset += b - a - saved
            timeline.append((offset, set(), held))
            offset += RANGE_GAP
        total = timeline[-1][0]
        if total <= 0:
            self.half.set()
        else:
            threading.Thread(target=lambda: self.stop.wait(total / 2) or self.half.set(), daemon=True).start()
        start = time.monotonic()
        for t, down, up in timeline:
            self.stop.wait(max(0.0, t - (time.monotonic() - start)))
            if self.stop.is_set():
                return
            self._apply(down, up)
        self.pad.release_all()
        self.run_.pressing = set()
        self.replayed.set()
        # 終わりの合図が出るまで待つ（main が止める）
        while not self.stop.is_set():
            self._sleep(TICK)

    def _press(self, *names):
        self.run_.pressing = set(names)
        self.run_.last_press = self.run_.now()
        self.pad.press(*names, down=PRESS_DOWN)
        self.run_.pressing = set()
        if self.sec.get("snap"):
            self.run_.snap_due.append((self.run_.now() + 0.5, f"step_{self.sec['id']}_{'+'.join(names)}"))

    def _apply(self, down, up):
        """記録の down・up（LS_UP など）を押す・離す。スティックは押している向きの組み合わせで倒す。"""
        run, pad = self.run_, self.pad
        cur = run.pressing
        cur = (cur | down) - up
        run.pressing = cur
        for side in "LR":
            x = sum(STICK_DIRS[d][0] for d in STICK_DIRS if f"{side}S_{d}" in cur)
            y = sum(STICK_DIRS[d][1] for d in STICK_DIRS if f"{side}S_{d}" in cur)
            pad.stick(side, max(-100, min(100, x)), max(-100, min(100, y)))
        pad.hold(*[btn(b) for b in down if b[1:3] != "S_"])
        pad.release(*[btn(b) for b in up if b[1:3] != "S_"])

    def _do(self, w):
        pad = self.pad
        op = w[0]
        if op == "press":
            self._press(*[btn(b) for b in w[1].split("+")])
            self._sleep(STEP_GAP)
        elif op == "hold" and len(w) == 3:
            b = btn(w[1])
            pad.hold(b)
            self._sleep(float(w[2][:-1]))
            pad.release(b)
        elif op == "hold":
            pad.hold(btn(w[1]))
        elif op == "release":
            pad.release(btn(w[1]))
        elif op == "wait":
            if re.fullmatch(r"\d+(\.\d+)?s", w[1]):
                self._sleep(float(w[1][:-1]))
            elif w[1] == "lit":
                while not self.stop.is_set() and self.run_.lit_for() < LIT_MIN:
                    self._sleep(TICK)
            else:
                while not self.stop.is_set() and not self.run_.sig.get(w[1]):
                    self._sleep(TICK)
        elif op == "seq":
            for ch in w[1]:
                if self.stop.is_set():
                    return
                self._press(SEQ[ch])
                self._sleep(STEP_GAP)
        elif op == "tap":
            pad.stick(w[2], *DIR_XY[w[3]])
            self._sleep(0.2)
            pad.stick(w[2], 0, 0)
            self._sleep(STEP_GAP)
        elif op == "press_until":
            while not self.stop.is_set() and not self.run_.sig.get(w[2]):
                self._press(btn(w[1]))
                self._sleep(1.0)
        elif op == "press_while":
            while not self.stop.is_set() and self.run_.sig.get(w[2]):
                self._press(btn(w[1]))
                self._sleep(1.0)
        elif op == "stick":
            pad.stick(w[1], *DIR_XY[w[2]])
        elif op == "mash":
            b = btn(w[1])
            while not self.stop.is_set():
                press = b
                for when, other in self.sec["excepts"]:
                    if self.run_.sig.get(when):
                        press = btn(other)
                        break
                self._press(press)
                self._sleep(MASH_GAP)
        else:
            raise ValueError(f"steps が分からない: {' '.join(w)}")


class Run:
    def __init__(self, pad, live, args):
        self.pad, self.live, self.args = pad, live, args
        raw = fp.load_flow()
        self.limits = [s["limit"] for s in raw]
        self.secs = [{**s, "limit": None} for s in raw]  # 上限はこの Run が見る（過ぎたら人に渡す）
        self.ids = [s["id"] for s in raw]
        self.i_from = self.ids.index(args.from_) if args.from_ else 0
        self.i_to = self.ids.index(args.to) if args.to else len(raw) - 1
        self.screens = Screens()
        self.t0 = time.monotonic()
        self.player = fp.FlowPlayer(self.secs, 0.0, defer_end=True)
        self.player.gate = self.replay_gate
        self.player.end_gate = self.end_gate
        self.sig = {n: False for n in SIGNALS}
        self.mode, self.reason = "hold", "準備"  # auto | hold
        self.idle = True
        self.lap, self.touched = args.lap, False
        self.exec = None
        self.selected = None
        self.reqs = []
        self.lock = threading.Lock()
        self.logdir = ROOT / "logs" / datetime.now().strftime("%Y%m%d-%H%M%S")
        self.dev = args.dev
        self.logdir.mkdir(parents=True, exist_ok=True)
        if self.dev:
            (self.logdir / "frames").mkdir(exist_ok=True)
        self.n_log = 0
        self.reconnected = False
        self.last_press = -1e9
        self.pressing = set()
        self.pc_held = set()
        self.lit_since = None
        self.snap_due = []      # snap: true の区間で、押したあとに残すコマ (時刻, 名前)
        self.snap_next = 0.0
        self.lit_pending = None
        self.lit_start = 0.0
        self.mid_buf = []
        self.mid_next = 0.0
        self.mid_visits = {}
        self.rec = Recorder(live)
        self.rec_name = None

    def replay_gate(self, i):
        """replay の区間は、範囲を押し終わるまで終わりの合図を数えない（途中の会話で早く終わらないため）。"""
        return self.secs[i]["kind"] != "replay" or (self.exec is not None and self.exec.replayed.is_set())

    def end_gate(self, i):
        """replay の区間は、再生の時間の半分が過ぎるまで blackout_end を数えない（前の区間から続く暗転の明けで飛ばないため）。"""
        return self.secs[i]["kind"] != "replay" or (self.exec is not None and self.exec.half.is_set())

    def request(self, kind, arg=None):
        with self.lock:
            self.reqs.append((kind, arg))

    def state(self):
        p = self.player
        rows = []
        for i, s in enumerate(self.secs):
            r = p.records[i]
            status = "done" if r["end"] is not None else ("current" if p.cur == i else "pending")
            rows.append({"id": s["id"], "kind": s["kind"], "status": status, "reason": r["reason"]})
        label = "PC が操作中" if self.mode == "auto" else f"人に渡している（{self.reason}）"
        return {"mode": self.mode, "label": label, "lap": self.lap, "touched": self.touched,
                "signals": self.sig, "selected": self.selected, "sections": rows,
                "pressing": sorted(self.pressing | self.pc_held) if self.mode == "auto" else [],
                "ops": self._ops()}

    def _ops(self):
        p = self.player
        if p.cur is None:
            return None
        s = self.secs[p.cur]
        return {"id": s["id"], "kind": s["kind"], "until": s["until"], "limit": self.limits[p.cur],
                "elapsed": round(self.now() - p.records[p.cur]["start"], 1)}

    def lit_for(self):
        """暗転でない状態が続いている秒数。暗転中は 0。"""
        return 0.0 if self.lit_since is None else self.now() - self.lit_since

    def now(self):
        return time.monotonic() - self.t0

    def log(self, event, **kw):
        row = {"time": datetime.now().isoformat(timespec="seconds"), "t": round(self.now(), 1),
               "event": event, **kw}
        with open(self.logdir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{row['t']:7.1f}] {event} {kw}", flush=True)

    def snap(self, tag):
        frame = self.live.frame
        if frame is None or not self.dev:
            return None
        self.n_log += 1
        name = f"frames/{self.n_log:04d}_{tag}.jpg"
        cv2.imwrite(str(self.logdir / name), frame)
        return name

    def log_input(self, section, op, args):
        if not self.dev:
            return
        row = {"t": round(self.now(), 3), "section": section, "op": op, "args": args}
        with open(self.logdir / "inputs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def flush_mid(self, i):
        """区間 i にいる間に溜めた真ん中の1枚を frames/mid_<flow上の番号3桁>_<区間名>[_<回数>].jpg に書いて捨てる。"""
        buf, self.mid_buf = self.mid_buf, []
        if i is None or not buf:
            return
        n = self.mid_visits.get(i, 1)
        suffix = f"_{n}" if n > 1 else ""
        (self.logdir / "frames" / f"mid_{i:03d}_{self.ids[i]}{suffix}.jpg").write_bytes(buf[len(buf) // 2])

    def collect_mid(self, t):
        if self.dev and self.player.cur is not None and t >= self.mid_next and self.live.jpg:
            self.mid_buf.append(self.live.jpg)
            self.mid_next = t + 2.0

    def record(self, name):
        """--video の時、logs/<日時>/video_<name>_<周>[_<回数>].mp4 に録画を始める（前の録画は閉じる）。"""
        self.rec.stop()
        self.rec_name = None
        if not (self.dev and self.args.video):
            return
        self.rec_name = name
        path = self.logdir / f"video_{name}_{self.lap}.mp4"
        n = 1
        while path.exists():
            n += 1
            path = self.logdir / f"video_{name}_{self.lap}_{n}.mp4"
        self.rec.start(path)
        self.log("video_start", name=name, file=path.name)

    def stop_record(self):
        self.rec.stop()
        self.rec_name = None

    def video_name(self, i):
        for name, (a, b) in VIDEO_RANGES.items():
            if self.ids.index(a) <= i <= self.ids.index(b):
                return name
        return None

    def save_route(self):
        rec = self.live.stop_recording()
        if not self.dev:
            return
        if self.reason == "完了":  # 完了のあとに人が押したもの
            if rec:
                (self.logdir / f"after_end_{self.lap}.json").write_text(
                    json.dumps({"section": "after_end", "lap": self.lap, "events": rec}, ensure_ascii=False),
                    encoding="utf-8")
            return
        i = self.player.cur
        if not rec or i is None:
            return
        d = ROOT / "za" / "routes" / self.ids[i]
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{self.lap}.json"
        path.write_text(json.dumps({"section": self.ids[i], "lap": self.lap, "events": rec},
                                   ensure_ascii=False), encoding="utf-8")

    def stop_exec(self):
        if self.exec:
            self.exec.stop.set()
            self.exec.join(timeout=2)
            self.exec = None
        self.pad.release_all()
        self.pc_held = set()

    def enter_section(self):
        i = self.player.cur
        self.pc_held = set()
        name = self.video_name(i)
        if name != self.rec_name:  # 同じ録画の中の区間移動・選び直しでは止めない
            self.stop_record()
            if name:
                self.record(name)
        self.mid_visits[i] = self.mid_visits.get(i, 0) + 1
        self.mid_buf, self.mid_next = [], self.now()
        self.log("enter", section=self.ids[i], frame=self.snap(f"in_{self.ids[i]}"))
        self.lit_pending, self.lit_start = self.ids[i], self.now()
        if self.secs[i]["kind"] == "handoff":
            self.hold("handoff")
            return
        self.mode, self.reason = "auto", ""
        self.live.set_enabled(False)
        self.exec = Executor(self, self.secs[i])
        self.exec.start()

    def hold(self, reason):
        """全部離して人に渡す。キーボードの操作を送り、入力を記録する。"""
        self.stop_exec()
        self.mode, self.reason = "hold", reason
        i = self.player.cur
        sid = self.ids[i] if i is not None else "-"
        if reason not in ("handoff", "準備", "完了"):
            self.touched = True
        self.log("hold", section=sid, reason=reason, frame=self.snap(f"hold_{sid}"))
        self.live.set_enabled(True)
        if reason != "準備":
            self.live.start_recording()
        notify("ZA 自動化", f"{sid}: {reason}")

    def finish(self, why):
        self.flush_mid(self.player.cur)
        self.stop_exec()
        self.log("lap_end", lap=self.lap, touched=self.touched, why=why)
        self.hold("完了")

    def after_advance(self, prev):
        p = self.player
        if p.cur != prev or p.done:
            self.flush_mid(prev)
            self.pad.release_all()
            if self.exec:
                self.stop_exec()
            if p.done or p.cur > self.i_to:
                self.finish("終わり")
            else:
                self.enter_section()

    def back(self):
        p, t = self.player, self.now()
        if self.mode != "hold":
            return
        self.save_route()
        if self.reason == "完了":
            self.stop_record()
            self.lap, self.touched = self.lap + 1, False
        if self.idle:
            self.mid_buf = []
        else:
            self.flush_mid(p.cur)
        self.live.set_enabled(False)
        sel = self.ids.index(self.selected) if self.selected else None
        self.selected = None
        if not self.pad.alive():
            try:
                self.pad.reconnect()
            except OSError as e:
                self.log("reconnect_failed", error=str(e))
                self.hold("接続切れ")
                return
        if self.idle or self.reason == "完了":
            self.idle = False
            p.jump(self.i_from if sel is None else sel, t, self.sig)
            self.enter_section()
        elif sel is not None:
            p.jump(sel, t, self.sig)
            self.enter_section()
        elif self.reason == "handoff":
            prev = p.cur
            p.press_enter(t)
            p.resync(t, self.sig)
            self.after_advance(prev)
        else:
            p.jump(p.cur, t, self.sig)
            self.enter_section()

    def take(self):
        if self.mode == "auto":
            self.hold("引き取り")

    def tick(self):
        frame = self.live.frame
        if frame is None:
            return
        t = self.now()
        self.sig = {n: self.screens.check(n, frame) for n in SIGNALS}
        self.record_signals(t, frame)
        self.snap_steps(t)
        self.collect_mid(t)
        if self.mode != "auto":
            return
        p = self.player
        if not self.pad.alive():
            try:
                self.pad.reconnect()
                self.log("reconnected")
            except OSError as e:
                self.log("reconnect_failed", error=str(e))
                self.hold("接続切れ")
            return
        if self.exec.error:
            self.log("exec_error", error=repr(self.exec.error))
            self.hold("操作の失敗")
            return
        prev = p.cur
        p.step_done = self.exec.replayed.is_set() or self.exec.done.is_set()
        step_sig = self.sig
        if self.sig.get("menu_open") and t - self.last_press < MENU_OPEN_IGNORE:
            step_sig = {**self.sig, "menu_open": False}
        p.step(t, step_sig)
        if p.cur != prev:
            self.after_advance(prev)
            return
        if self.secs[prev]["until"] == "end" and self.exec.done.is_set():
            p.complete(t)
            self.after_advance(prev)
            return
        limit = self.limits[prev]
        if limit is not None and t - p.records[prev]["start"] > limit + LATENCY:
            self.hold("タイムアウト")

    def snap_steps(self, t):
        """snap: true の区間で、押した0.5秒後と2秒ごとにコマを残す（画面遷移の確認用）。"""
        due, self.snap_due = [d for d in self.snap_due if d[0] <= t], [d for d in self.snap_due if d[0] > t]
        for _, tag in due:
            self.snap(tag)
        i = self.player.cur
        if self.mode == "auto" and i is not None and self.secs[i].get("snap") and t >= self.snap_next:
            self.snap(f"every_{self.ids[i]}")
            self.snap_next = t + 2.0

    def record_signals(self, t, frame):
        """毎コマの平均の明るさと点いている合図を signals.jsonl に残す。明けて1秒後のコマを lit_ で残す。"""
        mean = float(frame.mean())
        if not self.sig["bright"]:
            self.lit_since = None
        elif self.lit_since is None:
            self.lit_since = t
        row = {"t": round(t, 2), "mean": round(mean, 1), "on": [n for n in SIGNALS if self.sig[n]],
               "section": self.ids[self.player.cur] if self.player.cur is not None else None}
        if self.dev:
            with open(self.logdir / "signals.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        if self.lit_pending and self.lit_for() >= LIT_MIN and t - self.lit_start >= LIT_MIN:
            self.snap(f"lit_{self.lit_pending}")
            self.lit_pending = None

    def process_requests(self):
        with self.lock:
            reqs, self.reqs = self.reqs, []
        for kind, arg in reqs:
            if kind == "take":
                self.take()
            elif kind == "back":
                self.back()
            elif kind == "select":
                self.selected = arg  # 選び直しはできるが、解除の操作は無い


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", default="/dev/video0")
    ap.add_argument("--switch", metavar="MAC")
    ap.add_argument("--from", dest="from_", metavar="区間")
    ap.add_argument("--to", metavar="区間")
    ap.add_argument("--lap", type=int, default=1)
    ap.add_argument("--video", action="store_true", help="player_1 と x_x_1 の前後（talk_30〜talk_31）にいる間を mp4 に録画する")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dev", action="store_true", help="開発用の記録（コマ・合図・入力・routes・--video の録画）を出す")
    args = ap.parse_args()

    import nuxbt
    pad = Controller(nuxbt.Nuxbt(log_file_path="logs/nuxbt_run.log"))
    live = Live(pad, args.live)
    print("接続中...", flush=True)
    pad.connect(reconnect=True, address=args.switch)
    print("接続した。", flush=True)
    run = Run(pad, live, args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_live_handler(live, run))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"http://127.0.0.1:{args.port}/ を開く。準備ができたらダッシュボードの「▶ 開始」で始める（端末から起動したときは Enter でも可。q か Ctrl+C で終了）", flush=True)
    try:
        with Keys() as keys:
            while not keys.quit.is_set():
                if keys.enter.is_set():
                    keys.enter.clear()
                    run.request("back")
                run.process_requests()
                run.tick()
                time.sleep(TICK)
    except KeyboardInterrupt:
        pass
    finally:
        run.flush_mid(run.player.cur)
        if run.mode == "hold" and run.reason == "完了":
            run.save_route()
        run.stop_record()
        run.stop_exec()
        pad.release_all()
        server.shutdown()
        server.server_close()
        pad.close()
        print("全部離して終了")


if __name__ == "__main__":
    main()
