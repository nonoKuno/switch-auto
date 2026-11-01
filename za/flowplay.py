"""フロー再生のロジック。FlowPlayer は動画・ブラウザに依存しない（合図の列を時刻順に与える）。

合図の列は compute_signals() が動画から作り、work/signals.json にキャッシュする。
使う側（dashboard.py）は ROOT を sys.path に入れてから `from za import flowplay` する。
"""
import hashlib
import bisect
import json
import os
import re
from pathlib import Path

import cv2
import yaml

from common.frames import iter_frames
from za.screens import SIGNALS, Screens

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "za" / "flow.yaml"
FRAMES_DIR = ROOT / "work" / "frames"
NOTES = ROOT / "work" / "notes.txt"
SIGNALS_JSON = ROOT / "work" / "signals.json"
MEMO_TOLERANCE = 3.0
BLACKOUT_MIN = 0.5    # 暗転は、この秒数以上続いた時だけ「出た」と数える（0.1秒のちらつきを除く）
BRIGHT_MIN = 0.5      # 明るい状態がこの秒数続くまでは、暗転の途中の一瞬の明るさとして同じ暗転に数える

FRAME_RE = re.compile(r"^(\d{4})_.*?_t(\d+(?:\.\d+)?)")


def load_flow(path=FLOW, video=False):
    """flow.yaml を区間の list にする。video=True（手本の動画）では replay を handoff（enter 待ち）として扱う。until と except を解釈し、frames を (始点, 終点) にする。"""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    sections = []
    for s in raw:
        a, b = (int(x) for x in str(s["frames"]).split("-"))
        if video and s["kind"] == "replay":
            s = {k: v for k, v in s.items() if k != "limit"} | {"kind": "handoff", "until": "enter"}
        sections.append({
            "id": s["id"],
            "kind": s["kind"],
            "frames": (a, b),
            "steps": [str(x) for x in s.get("steps", [])],
            "except": [f"{e['when']} なら {e['press']}" for e in s.get("except", [])],
            "until": str(s["until"]),
            "until_parts": parse_until(str(s["until"])),
            "excepts": [(e["when"], e["press"]) for e in s.get("except", [])],
            "limit": float(s["limit"]) if "limit" in s else None,
            "snap": bool(s.get("snap")),
            "route": s.get("route"),
            "pre": [str(x) for x in s.get("pre", [])],
            "trim": ([[float(x) for x in r] for r in s["trim"]] if isinstance(s["trim"][0], list)
                     else [float(x) for x in s["trim"]]) if "trim" in s else None,
        })
    return sections


def parse_until(text):
    """until の文字列を [(種類, ...)] にする。種類は sig（合図名, 回数）/ sec（秒）/ enter / end / done（steps・replay の範囲を押し終わった）。"""
    parts = []
    for raw in text.split("|"):
        p = raw.strip()
        if p in ("enter", "end", "done"):
            parts.append((p,))
        elif re.fullmatch(r"\d+(\.\d+)?s", p):
            parts.append(("sec", float(p[:-1])))
        else:
            name, _, n = p.partition("×")
            if name not in SIGNALS and name != "blackout_end":
                raise ValueError(f"until が分からない: {text}")
            parts.append(("sig", name, int(n) if n else 1))
    return parts


def frame_times(folder=FRAMES_DIR):
    """work/frames のファイル名から {コマ番号: 動画内の秒} を作る。"""
    out = {}
    for p in Path(folder).iterdir():
        m = FRAME_RE.match(p.name)
        if m:
            out[int(m.group(1))] = float(m.group(2))
    return out


def frame_time_at(times, n):
    """コマ番号 n の秒。無ければ次に近い（大きい）コマの秒。"""
    keys = sorted(times)
    i = bisect.bisect_left(keys, n)
    return times[keys[min(i, len(keys) - 1)]]


def add_memo_times(sections, times):
    """各区間にメモの開始・終了の秒を付ける（終了は次の区間の開始、最後は終点のコマ）。"""
    for i, s in enumerate(sections):
        s["memo_start"] = frame_time_at(times, s["frames"][0])
        if i + 1 < len(sections):
            s["memo_end"] = frame_time_at(times, sections[i + 1]["frames"][0])
        else:
            s["memo_end"] = frame_time_at(times, s["frames"][1])
    return sections


class FlowPlayer:
    """区間を時刻順に進める。step(t, sig) で合図を1コマ分流し、press_enter(t) で人の Enter を入れる。

    sig は {合図名: bool}。「出ていない→出た」に変わった時だけ数える。
    blackout だけは、暗転が BLACKOUT_MIN 秒以上続いた時に1回数える。
    区間が始まったコマの変化は、その区間では数えない（次のコマから）。
    """

    def __init__(self, sections, start_t, defer_end=False):
        self.sections = sections
        self.start_t = start_t
        self.gate = None  # gate(i) が False の間は、区間 i の終わりの合図を数えない（replay が範囲を押し終わるまで）
        self.step_done = False  # steps・replay の範囲を押し終わった（run.py が毎 tick 入れる。until の done 用）
        self.gate_was_open = True
        self.end_gate = None  # end_gate(i) が False の間は、区間 i の blackout_end を数えない（replay が範囲の後半に入るまで）
        self.defer_end = defer_end  # True なら end の区間もすぐ終えず、complete() を待つ（実機用）
        self.prev = None
        self.done = False
        self.cur = None
        self._reset_dark()
        self.counts = {}
        self.records = [
            {"id": s["id"], "start": None, "end": None, "reason": None, "auto": False, "seen": set()}
            for s in sections
        ]
        self._open(0, start_t)

    def _open(self, i, t):
        self.cur = i
        self.counts = {}
        self.step_done = False
        self.gate_was_open = True
        self.records[i]["start"] = t
        if self.sections[i]["until"] == "end" and not self.defer_end:
            self._finish(t, "完了")

    def _finish(self, t, reason, auto=False):
        rec = self.records[self.cur]
        rec["end"], rec["reason"], rec["auto"] = t, reason, auto
        if reason == "完了" or self.cur + 1 >= len(self.sections):
            self.done = True
            self.cur = None
        else:
            self._open(self.cur + 1, t)

    def _satisfied(self, t):
        sec = self.sections[self.cur]
        start = self.records[self.cur]["start"]
        for part in sec["until_parts"]:
            if part[0] == "sig" and self.counts.get(part[1], 0) >= part[2]:
                return "合図"
            if part[0] == "sec" and t - start >= part[1]:
                return "秒数"
            if part[0] == "done" and self.step_done:
                return "済み"
        return None

    def step(self, t, sig):
        if self.done or t < self.start_t or self.prev is None:
            self.prev = sig
            return
        rec = self.records[self.cur]
        open_ = not self.gate or self.gate(self.cur)
        prev = self.prev
        if open_ and not self.gate_was_open:  # gate が開いた時、出ている合図は新しく出たものとして数える
            prev = {n: False for n in prev}
        self.gate_was_open = open_
        for name in SIGNALS:
            if name not in ("blackout", "bright") and sig[name] and not prev[name]:
                rec["seen"].add(name)
                self.counts[name] = self.counts.get(name, 0) + 1
        self._count_dark(t, sig["blackout"], sig["bright"])
        if self.end_gate and not self.end_gate(self.cur):  # 前の区間から続く暗転の明けを捨てる。end_counted は残す
            self.counts.pop("blackout_end", None)
        if not open_:  # 閉じている間も暗転は数えて残す。消すのは会話などほかの合図だけ
            for name in list(self.counts):
                if name not in ("blackout", "blackout_end"):
                    del self.counts[name]
        self.prev = sig
        reason = self._satisfied(t)
        limit = self.sections[self.cur]["limit"]
        if reason is None and limit is not None and t - rec["start"] >= limit:
            reason = "タイムアウト"
        if reason:
            self._finish(t, reason)

    def _reset_dark(self, t=None, on=False):
        """暗転の状態を作り直す。on なら t から暗転中（始まりは分からないので blackout は数えない）。"""
        self.dark_since = t if on else None
        self.dark_last = t if on else None
        self.bright_since = None              # 今続いている明るさ（20以上）の始まり。暗い・薄暗い間は None
        self.ended = False                    # 明るさが BRIGHT_MIN 続いて、暗転が明けた
        self.dark_counted = False
        self.end_counted = False

    def _count_dark(self, t, dark, bright):
        """暗転を数える。dark は平均12未満、bright は平均20以上（12〜20 は暗転の続き）。
        blackout: 暗転が BLACKOUT_MIN 続いた時に1回（区間の開始前から続く暗転は数えない）。
        blackout_end: 暗転（BLACKOUT_MIN 以上）が明けて、明るさが BRIGHT_MIN 続いた時に1回。
        次の暗転を別の暗転にするのは、明けたあとだけ。"""
        rec = self.records[self.cur]
        if dark:
            if self.dark_since is None or self.ended:
                self._reset_dark(t, True)
            self.bright_since = None
            self.dark_last = t
            if (not self.dark_counted and self.dark_since > rec["start"]
                    and t - self.dark_since >= BLACKOUT_MIN - 1e-6):
                self.dark_counted = True
                rec["seen"].add("blackout")
                self.counts["blackout"] = self.counts.get("blackout", 0) + 1
            return
        if self.dark_since is None:
            return
        if not bright:
            self.bright_since = None
            return
        if self.bright_since is None:
            self.bright_since = t
        if t - self.bright_since >= BRIGHT_MIN - 1e-6:
            self.ended = True
            if not self.end_counted and self.dark_last - self.dark_since >= BLACKOUT_MIN - 1e-6:
                self.end_counted = True
                rec["seen"].add("blackout_end")
                self.counts["blackout_end"] = self.counts.get("blackout_end", 0) + 1

    def resync(self, t, sig):
        """step を流していなかった間のあとで、今の合図から続ける。続いている暗転は今の区間に数えない。"""
        self.prev = sig
        self._reset_dark(t, sig["blackout"])

    def jump(self, i, t, sig):
        self.done = False
        self._open(i, t)
        self.records[i]["end"] = self.records[i]["reason"] = None
        self.resync(t, sig)

    def complete(self, t):
        """end の区間を完了にする（defer_end の時）。"""
        self._finish(t, "完了")

    def press_enter(self, t, auto=False):
        """人の Enter。今の区間が enter の終わりなら次へ進む。進んだら True。"""
        if not self.can_enter():
            return False
        self._finish(t, "Enter", auto)
        return True

    def can_enter(self):
        return (not self.done and self.cur is not None
                and any(p[0] == "enter" for p in self.sections[self.cur]["until_parts"]))

    def current_ops(self, t):
        """今の区間で出すべき操作（押さない）。"""
        if self.cur is None:
            return None
        sec = self.sections[self.cur]
        return {
            "id": sec["id"],
            "kind": sec["kind"],
            "steps": sec["steps"],
            "except": sec["except"],
            "until": sec["until"],
            "limit": sec["limit"],
            "elapsed": round(t - self.records[self.cur]["start"], 1),
        }


def replay(sections, start_t, cues, enters, until=None, auto_fn=None):
    """cues は [(t, sig)] を時刻順に。enters は人の Enter の時刻。until 秒までを頭から流す。

    auto_fn(player, t, sig) が True を返したら、そのコマの後で Enter を自動で押す（レポート用）。
    """
    p = FlowPlayer(sections, start_t)
    enters = sorted(e for e in enters if until is None or e <= until)
    ei = 0
    for i, (t, sig) in enumerate(cues):
        if until is not None and t > until:
            break
        p.step(t, sig)
        nxt = cues[i + 1][0] if i + 1 < len(cues) else float("inf")
        while ei < len(enters) and enters[ei] < nxt:
            p.press_enter(enters[ei])
            ei += 1
        if auto_fn and auto_fn(p, t, sig):
            p.press_enter(t, auto=True)
    while ei < len(enters):
        p.press_enter(enters[ei])
        ei += 1
    return p


def _judge_key():
    """判定に使うファイルの中身の指紋。しきい値や見本を変えたら作り直す。"""
    h = hashlib.sha1()
    for p in (ROOT / "za" / "screens.py", ROOT / "za" / "templates.json",
              ROOT / "za" / "templates" / "B戻る.png"):
        h.update(p.read_bytes())
    return h.hexdigest()


def video_step(video, interval=0.1):
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS)
    cap.release()
    return max(1, round(fps * interval))


def compute_signals(video, step, cache=SIGNALS_JSON):
    """動画を step コマごとに読み、[(t, {合図名: bool})] を返す。結果は cache に保存し、同じ条件なら読む。"""
    st = os.stat(video)
    meta = {"video": os.path.realpath(video), "size": st.st_size, "mtime": st.st_mtime,
            "step": step, "judge": _judge_key()}
    cache = Path(cache)
    if cache.exists():
        with open(cache, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("meta") == meta:
            return [(t, dict(zip(SIGNALS, map(bool, on)))) for t, *on in data["rows"]]

    screens = Screens()
    cues, rows = [], []
    for t, frame in iter_frames(str(video), step):
        sig = {name: screens.check(name, frame) for name in SIGNALS}
        cues.append((t, sig))
        rows.append([round(t, 3)] + [int(sig[n]) for n in SIGNALS])
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "signals": list(SIGNALS), "rows": rows}, f, ensure_ascii=False)
    return cues
