"""ダッシュボード（表示だけ。入力は送らない）。

使い方:
  .venv/bin/python common/dashboard.py [--video videos/demo1.mp4] [--port 8000]
  .venv/bin/python common/dashboard.py --headless-report [--video videos/demo1.mp4]
  .venv/bin/python common/dashboard.py --live /dev/video0 [--switch AA:BB:CC:DD:EE:FF] [--port 8000]
"""
import argparse
import bisect
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2

ROOT = Path(__file__).resolve().parents[1]
SCREENS_HTML = ROOT / "work" / "flow_screens.html"
sys.path.insert(0, str(ROOT))

from common import frames  # noqa: E402
from common.controller import Controller  # noqa: E402
from common.keys import Keys  # noqa: E402
from za import flowplay as fp  # noqa: E402
from za.screens import SIGNALS  # noqa: E402

PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZA フロー ダッシュボード</title>
<style>
body{font-family:sans-serif;margin:16px;background:#fff;color:#222}
#wrap{display:flex;gap:16px;flex-wrap:wrap}
#left{flex:1 1 480px;max-width:720px}
#right{flex:1 1 520px}
video{width:100%;background:#000}
button{margin:2px;padding:4px 10px}
button.on{background:#333;color:#fff}
table{border-collapse:collapse;font-size:13px;width:100%}
th,td{border-bottom:1px solid #ddd;padding:3px 5px;text-align:left}
tr.current{background:#ffe27a;font-weight:bold}
tr.pending{color:#999}
.warn{color:#c00;font-weight:bold}
.sig{display:inline-block;padding:2px 8px;margin:2px;border:1px solid #bbb;border-radius:4px}
.sig.on{background:#2a9d4b;color:#fff;border-color:#2a9d4b}
.box{border:1px solid #ccc;padding:8px;margin-top:8px}
ul{margin:4px 0;padding-left:20px}
</style></head><body>
<h3>ZA フロー ダッシュボード（表示だけ。キーは送らない）</h3>
<div id="wrap">
 <div id="left">
  <video id="v" src="/video" controls preload="auto"></video>
  <div>再生速度:
   <button data-r="1" class="rate on">1倍</button>
   <button data-r="2" class="rate">2倍</button>
   <button data-r="4" class="rate">4倍</button>
  </div>
  <div>動画の時刻: <b id="clock">0:00.0</b> / 今の区間: <b id="cur">-</b></div>
  <div class="box">
   <div>今出ている合図:</div>
   <div id="signals"></div>
  </div>
  <div class="box">
   <div>今の区間で出す操作（押さない）:</div>
   <div id="ops">-</div>
   <button id="enter" disabled>次へ（Enter の代わり）</button>
  </div>
  <div class="box">Enter の履歴: <span id="enters">-</span></div>
 </div>
 <div id="right">
  <table>
   <thead><tr><th>区間</th><th>種類</th><th>コマ</th><th>開始</th><th>理由</th><th>終了</th><th>メモ開始</th><th>ずれ</th></tr></thead>
   <tbody id="sections"></tbody>
  </table>
 </div>
</div>
<script>
const v = document.getElementById('v');
let busy = false, again = false, lastT = 0;
const fmt = t => { t = Math.max(0, t); const m = Math.floor(t / 60); return m + ':' + (t % 60).toFixed(1).padStart(4, '0'); };
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));

function render(s) {
  lastT = s.t;
  document.getElementById('clock').textContent = fmt(s.t);
  document.getElementById('cur').textContent = s.done ? '完了' : (s.ops ? s.ops.id : '-');
  document.getElementById('signals').innerHTML = Object.entries(s.signals)
    .map(([k, on]) => `<span class="sig ${on ? 'on' : ''}">${k}</span>`).join('');
  const o = s.ops;
  document.getElementById('ops').innerHTML = o ? (
    `<div>${esc(o.id)}（${esc(o.kind)}）・終わり: ${esc(o.until)}・上限: ${o.limit == null ? 'なし' : o.limit + '秒'}・区間の経過: ${o.elapsed}秒</div>` +
    '<ul>' + o.steps.map(x => `<li>ここで「${esc(x)}」</li>`).join('') +
    o.except.map(x => `<li>割り込み: ${esc(x)}</li>`).join('') + '</ul>'
  ) : '-';
  const btn = document.getElementById('enter');
  btn.disabled = !s.can_enter;
  document.getElementById('enters').textContent = s.enters.length ? s.enters.map(fmt).join(', ') : 'なし';
  document.getElementById('sections').innerHTML = s.sections.map(r => {
    const diff = r.diff == null ? '' : (Math.abs(r.diff) > 3 ? `<span class="warn">※ ${r.diff > 0 ? '+' : ''}${r.diff.toFixed(1)}秒</span>` : `${r.diff.toFixed(1)}秒`);
    const reason = r.reason ? esc(r.reason) + (r.auto ? '（自動）' : '') : '';
    return `<tr class="${r.status}"><td>${esc(r.id)}</td><td>${esc(r.kind)}</td><td>${r.frames}</td>` +
      `<td>${r.start == null ? '' : fmt(r.start)}</td><td>${reason}</td><td>${r.end == null ? '' : fmt(r.end)}</td>` +
      `<td>${fmt(r.memo_start)}</td><td>${diff}</td></tr>`;
  }).join('');
}

async function refresh(seek) {
  if (busy) { again = true; return; }
  busy = true;
  try {
    const r = await fetch(`/api/state?t=${v.currentTime.toFixed(3)}${seek ? '&seek=1' : ''}`);
    if (r.ok) render(await r.json());
  } catch (e) { /* 次の周期で再試行 */ }
  busy = false;
  if (again) { again = false; refresh(false); }
}

v.addEventListener('seeked', () => refresh(v.currentTime < lastT - 0.01));
document.querySelectorAll('.rate').forEach(b => b.addEventListener('click', () => {
  v.playbackRate = Number(b.dataset.r);
  document.querySelectorAll('.rate').forEach(x => x.classList.toggle('on', x === b));
}));
document.getElementById('enter').addEventListener('click', async () => {
  const r = await fetch('/api/enter', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                       body: JSON.stringify({t: v.currentTime})});
  if (r.ok) render(await r.json());
});
setInterval(() => refresh(false), 200);
refresh(false);
</script>
</body></html>
"""

LIVE_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ZA 実機 ダッシュボード</title>
<style>
body{font-family:sans-serif;margin:16px;background:#fff;color:#222}
img{display:block;width:100%;max-width:960px;background:#000}
#held{font-size:18px;font-weight:bold;margin:8px 0;min-height:1.4em}
table{border-collapse:collapse;font-size:13px}
th,td{border-bottom:1px solid #ddd;padding:3px 8px;text-align:left}
#wrap{display:flex;gap:16px;flex-wrap:wrap}
#panel{flex:1 1 420px;max-height:90vh;overflow:auto}
#head{position:sticky;top:0;background:#fff;padding-bottom:4px;z-index:2}
#mode{font-size:18px;font-weight:bold;margin:4px 0}
#mode.hold{color:#c00}
button{margin:2px;padding:6px 14px}
.sig{display:inline-block;padding:2px 8px;margin:2px;border:1px solid #bbb;border-radius:4px}
.sig.on{background:#2a9d4b;color:#fff;border-color:#2a9d4b}
#sections tr{cursor:pointer}
#sections tr.current{background:#ffe27a;font-weight:bold}
#sections tr.pending{color:#999}
#sections tr.selected{outline:2px solid #06c}
#band{padding:6px 10px;color:#fff;font-weight:bold;font-size:18px;margin:4px 0;background:#2a9d4b}
#band.hold{background:#c00}
#lower{display:flex;gap:8px;align-items:flex-start}
#lower>table{flex:1 1 0;min-width:0}
#pic{flex:1 1 0;min-width:0;position:sticky;top:var(--headh,160px);font-size:12px;color:#666}
#pic img{width:100%;max-width:none}
#pad{display:block;width:100%;max-width:575px;margin:8px 0}
#pad [data-k]{fill:#ddd;stroke:#777}
#pad .body{fill:#f4f4f4;stroke:#888}
#pad .base{fill:#bbb;stroke:#777}
#pad .h{fill:#e33}
#pad .p{fill:#27c}
#pad text{font-size:10px;text-anchor:middle;dominant-baseline:central;fill:#333;stroke:none;pointer-events:none}
#pad .ld{fill:none;stroke:#999;stroke-width:.8}
#pad text.kl{text-anchor:end;font-size:11px}
#pad text.kr{text-anchor:start;font-size:11px}
#legend{font-size:12px;color:#666}
</style></head><body>
<h3>ZA 実機 ダッシュボード（PC のキーで Switch を操作する）</h3>
<div id="wrap"><div>
<img src="/stream" alt="実機の映像">
<svg id="pad" viewBox="-80 -20 575 290" xmlns="http://www.w3.org/2000/svg">
<path class="body" d="M70 40 Q210 20 350 40 Q395 60 405 130 L408 215 Q405 255 372 255 Q345 255 335 225 L310 205 H110 L85 225 Q75 255 48 255 Q15 255 12 215 L15 130 Q25 60 70 40Z"/>
<rect data-k="ZL" x="40" y="4" width="75" height="16" rx="7"/><text x="77" y="12">ZL</text>
<rect data-k="L" x="48" y="24" width="80" height="12" rx="5"/><text x="88" y="30">L</text>
<rect data-k="ZR" x="305" y="4" width="75" height="16" rx="7"/><text x="342" y="12">ZR</text>
<rect data-k="R" x="292" y="24" width="80" height="12" rx="5"/><text x="332" y="30">R</text>
<circle data-k="MINUS" cx="175" cy="58" r="7"/><text x="175" y="58">-</text>
<circle data-k="PLUS" cx="245" cy="58" r="7"/><text x="245" y="58">+</text>
<circle data-k="HOME" cx="245" cy="92" r="8"/>
<circle class="base" cx="95" cy="98" r="26"/>
<circle data-k="L3" data-stick="L" cx="95" cy="98" r="15"/>
<rect data-k="DPAD_UP" x="141" y="150" width="18" height="20"/>
<rect data-k="DPAD_DOWN" x="141" y="180" width="18" height="20"/>
<rect data-k="DPAD_LEFT" x="121" y="166" width="20" height="18"/>
<rect data-k="DPAD_RIGHT" x="159" y="166" width="20" height="18"/>
<circle data-k="X" cx="320" cy="76" r="11"/><text x="320" y="76">X</text>
<circle data-k="B" cx="320" cy="120" r="11"/><text x="320" y="120">B</text>
<circle data-k="Y" cx="298" cy="98" r="11"/><text x="298" y="98">Y</text>
<circle data-k="A" cx="342" cy="98" r="11"/><text x="342" y="98">A</text>
<circle class="base" cx="265" cy="175" r="26"/>
<circle data-k="R3" data-stick="R" cx="265" cy="175" r="15"/>
<polyline class="ld" points="40,12 -10,12"/><text class="kl" x="-14" y="12">Z</text>
<polyline class="ld" points="48,30 -10,30"/><text class="kl" x="-14" y="30">L</text>
<polyline class="ld" points="168,58 -10,58"/><text class="kl" x="-14" y="58">-</text>
<polyline class="ld" points="69,98 -10,98"/><text class="kl" x="-14" y="91">矢印キー</text><text class="kl" x="-14" y="105">押し込み: V</text>
<polyline class="ld" points="150,150 150,140 -10,140"/><text class="kl" x="-14" y="140">8</text>
<polyline class="ld" points="121,175 -10,175"/><text class="kl" x="-14" y="175">4</text>
<polyline class="ld" points="150,200 150,215 -10,215"/><text class="kl" x="-14" y="215">2</text>
<polyline class="ld" points="169,184 169,235 -10,235"/><text class="kl" x="-14" y="235">6</text>
<polyline class="ld" points="239,86 225,72 225,-6 430,-6"/><text class="kr" x="434" y="-6">H</text>
<polyline class="ld" points="380,12 430,12"/><text class="kr" x="434" y="12">C</text>
<polyline class="ld" points="372,30 430,30"/><text class="kr" x="434" y="30">R</text>
<polyline class="ld" points="252,58 430,58"/><text class="kr" x="434" y="58">P</text>
<polyline class="ld" points="331,76 430,76"/><text class="kr" x="434" y="76">X</text>
<polyline class="ld" points="353,98 430,98"/><text class="kr" x="434" y="98">A</text>
<polyline class="ld" points="331,120 430,120"/><text class="kr" x="434" y="120">B</text>
<polyline class="ld" points="298,109 298,145 430,145"/><text class="kr" x="434" y="145">Y</text>
<polyline class="ld" points="291,175 430,175"/><text class="kr" x="434" y="168">Shift+矢印</text><text class="kr" x="434" y="182">押し込み: N</text>
</svg>
<div id="legend">赤 = 人が押している ／ 青 = PC が押している</div>
</div>
<div id="panel" hidden>
 <div id="head">
 <div id="band"></div>
 <div id="mode"></div>
 <div id="signals"></div>
 <div id="ops"></div>
 <div><button id="take">PC の操作を止める</button><button id="back">開始</button></div>
 <div style="font-size:12px;color:#666">区間を押すと、PC に渡す時にその区間の先頭から続ける（別の区間を押すと選び直す）</div>
 </div>
 <div id="lower">
  <table><tbody id="sections"></tbody></table>
  <div id="pic" hidden><div>手本（work/flow_screens.html）</div><img id="ref" alt="手本の画面"></div>
 </div>
</div></div>
<script>
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const post = (p, b) => fetch(p, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(b || {})});
document.getElementById('take').addEventListener('click', e => { post('/api/take'); e.target.blur(); });
document.getElementById('back').addEventListener('click', e => { post('/api/back'); e.target.blur(); });
let secKey = '', picSec = undefined, prevMode = null;
const beepAudio = new Audio('/sounds/notify.mp3');
beepAudio.volume = 0.75;
function tone() { try { const c = new AudioContext(), o = c.createOscillator(), g = c.createGain(); o.frequency.value = 880; g.gain.value = 0.2; o.connect(g).connect(c.destination); o.start(); o.stop(c.currentTime + 0.4); o.onended = () => c.close(); } catch (e) {} }
function beep() { try { beepAudio.currentTime = 0; beepAudio.play().catch(tone); } catch (e) { tone(); } }
let humanHeld = [], pcHeld = [];
const KNOB = {L: [95, 98], R: [265, 175]};
function paintPad() {
  const own = n => humanHeld.includes(n) ? 'h' : (pcHeld.includes(n) ? 'p' : '');
  for (const el of document.querySelectorAll('#pad [data-k]')) el.setAttribute('class', own(el.dataset.k));
  for (const side of ['L', 'R']) {
    const knob = document.querySelector('#pad [data-stick="' + side + '"]');
    const n = d => side + 'S_' + d;
    const set = ['UP', 'DOWN', 'LEFT', 'RIGHT'].some(d => humanHeld.includes(n(d))) ? humanHeld : pcHeld;
    const on = d => set.includes(n(d)) ? 1 : 0;
    const dirs = ['UP', 'DOWN', 'LEFT', 'RIGHT'].map(n).concat(side + '3');
    knob.setAttribute('class', dirs.some(k => humanHeld.includes(k)) ? 'h' : (dirs.some(k => pcHeld.includes(k)) ? 'p' : ''));
    knob.setAttribute('cx', KNOB[side][0] + (on('RIGHT') - on('LEFT')) * 10);
    knob.setAttribute('cy', KNOB[side][1] + (on('DOWN') - on('UP')) * 10);
  }
}
const picEl = document.getElementById('ref'), picBox = document.getElementById('pic');
picEl.addEventListener('load', () => { picBox.hidden = false; });
picEl.addEventListener('error', () => { picBox.hidden = true; });
function showPic(id) {
  if (id === picSec) return;
  picSec = id;
  picBox.hidden = true;
  if (id) picEl.src = '/api/frame?sec=' + encodeURIComponent(id); else picEl.removeAttribute('src');
}
async function poll() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    const s = await r.json();
    document.getElementById('panel').hidden = false;
    if (prevMode === 'auto' && s.mode === 'hold') beep();
    prevMode = s.mode;
    const bk = document.getElementById('back');
    bk.textContent = s.mode === 'auto' ? 'PC が操作中' : (s.label.includes('準備') ? '▶ 開始（PC が操作を始める）' : '▶ PC に渡す（続きから再開）');
    bk.disabled = s.mode === 'auto';
    const band = document.getElementById('band');
    band.className = s.mode === 'auto' ? '' : 'hold';
    band.textContent = s.mode === 'auto' ? 'PC の番（PC が操作中）' : '人の番（手で操作できる）';
    const m = document.getElementById('mode');
    m.textContent = `${s.lap}周目 ${s.touched ? '（手出しあり）' : ''}・${s.label}`;
    pcHeld = s.pressing || [];
    if (s.mode === 'auto') humanHeld = [];
    paintPad();
    document.getElementById('wrap').style.setProperty('--headh', document.getElementById('head').offsetHeight + 'px');
    document.getElementById('signals').innerHTML = Object.entries(s.signals)
      .map(([k, on]) => `<span class="sig ${on ? 'on' : ''}">${k}</span>`).join('');
    const o = s.ops;
    document.getElementById('ops').textContent = o ?
      `${o.id}（${o.kind}）終わり: ${o.until}・上限: ${o.limit == null ? 'なし' : o.limit + '秒'}・経過: ${o.elapsed}秒` : '';
    const key = JSON.stringify([s.sections, s.selected]);
    if (key !== secKey) {
      secKey = key;
      document.getElementById('sections').innerHTML = s.sections.map(x =>
        `<tr data-id="${esc(x.id)}" class="${x.status}${x.id === s.selected ? ' selected' : ''}"><td>${esc(x.id)}</td><td>${esc(x.kind)}</td><td>${esc(x.reason || '')}</td></tr>`).join('');
    }
    showPic(s.selected || (o ? o.id : null));
  } catch (e) { /* 次の周期で再試行 */ }
}
document.getElementById('sections').addEventListener('pointerdown', e => {
  const tr = e.target.closest('tr');
  if (tr && e.button === 0) post('/api/select', {id: tr.dataset.id});
});
setInterval(poll, 200);
const BTN = {KeyA: 'A', KeyB: 'B', KeyX: 'X', KeyY: 'Y', KeyL: 'L', KeyR: 'R', KeyZ: 'ZL', KeyC: 'ZR',
  KeyP: 'PLUS', Minus: 'MINUS', KeyH: 'HOME', KeyV: 'L3', KeyN: 'R3',
  Digit8: 'DPAD_UP', Digit2: 'DPAD_DOWN', Digit4: 'DPAD_LEFT', Digit6: 'DPAD_RIGHT'};
const DIR = {ArrowUp: 'UP', ArrowDown: 'DOWN', ArrowLeft: 'LEFT', ArrowRight: 'RIGHT'};
const mapped = c => c in BTN || c in DIR || c === 'ShiftLeft' || c === 'ShiftRight';
const down = new Set();
let chain = Promise.resolve();

function logical() {
  const shift = down.has('ShiftLeft') || down.has('ShiftRight');
  const out = [];
  for (const c of down) {
    if (c in BTN) out.push(BTN[c]);
    else if (c in DIR) out.push((shift ? 'RS_' : 'LS_') + DIR[c]);
  }
  return out;
}

function send() {
  const held = logical();
  const body = JSON.stringify({held: held});
  chain = chain.then(() => fetch('/api/key', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: body}))
    .then(r => r.json()).then(j => { humanHeld = j.held || []; paintPad(); })
    .catch(() => {});
}

document.addEventListener('keydown', e => {
  if (!mapped(e.code)) return;
  e.preventDefault();
  if (e.repeat) return;
  down.add(e.code);
  send();
});
document.addEventListener('keyup', e => {
  if (!mapped(e.code)) return;
  e.preventDefault();
  down.delete(e.code);
  send();
});
function clearAll() { down.clear(); send(); }
window.addEventListener('blur', clearAll);
document.addEventListener('visibilitychange', () => { if (document.hidden) clearAll(); });
setInterval(send, 500);
</script>
</body></html>
"""


class App:
    """サーバーが持つもの: 動画・区間・合図の列・人の Enter の時刻（メモリだけ）。"""

    def __init__(self, video):
        self.video = str(video)
        self.sections = fp.load_flow(video=True)
        fp.add_memo_times(self.sections, fp.frame_times())
        self.start_t = self.sections[0]["memo_start"]
        step = fp.video_step(self.video)
        self.cues = fp.compute_signals(self.video, step)
        self.times = [t for t, _ in self.cues]
        self.enters = []
        self.lock = threading.Lock()

    def sig_at(self, t):
        i = max(0, bisect.bisect_right(self.times, t) - 1)
        if not self.cues or self.times[i] > t:
            return {n: False for n in SIGNALS}
        return self.cues[i][1]

    def state(self, t, enters):
        player = fp.replay(self.sections, self.start_t, self.cues, enters, until=t)
        rows = []
        for i, s in enumerate(self.sections):
            r = player.records[i]
            status = "done" if r["end"] is not None else ("current" if player.cur == i else "pending")
            diff = None if r["start"] is None else r["start"] - s["memo_start"]
            rows.append({
                "id": s["id"], "kind": s["kind"], "frames": "%d-%d" % s["frames"], "status": status,
                "start": r["start"], "end": r["end"], "reason": r["reason"], "auto": r["auto"],
                "memo_start": s["memo_start"], "diff": diff,
            })
        return {
            "t": t,
            "signals": self.sig_at(t),
            "done": player.done,
            "ops": player.current_ops(t),
            "can_enter": player.can_enter(),
            "enters": sorted(enters),
            "sections": rows,
        }


def headless_report(app):
    """動画を頭から最後まで流して結果を出す。handoff の Enter は自動で押す（レポート用）。"""
    sections = app.sections
    darks, start = [], None
    for t, sig in app.cues:
        if sig["blackout"] and start is None:
            start = t
        elif not sig["blackout"] and start is not None:
            if t - start >= fp.BLACKOUT_MIN:
                darks.append((start, t))
            start = None

    def enter_at(nxt):
        """次の区間のメモの始まり。その直前直後に暗転があれば、明けるまで待つ（人は暗転が明けてから押す）。"""
        m = sections[nxt]["memo_start"]
        for a, b in darks:
            if a - 1.0 <= m < b:
                m = b
        return m

    def auto_enter(p, t, sig):
        if not p.can_enter():
            return False
        nxt = p.cur + 1
        if nxt >= len(sections):
            return False
        return t >= enter_at(nxt)

    p = fp.replay(sections, app.start_t, app.cues, [], auto_fn=auto_enter)
    recs = p.records
    out = []
    out.append(f"動画: {app.video}  判定の間隔: {app.cues[1][0] - app.cues[0][0]:.2f}秒  コマ数: {len(app.cues)}")
    out.append("注意: handoff の Enter は自動（レポート用）。次の区間のメモの始まりの時刻に押したことにしている（人がメモの時刻に合わせて押す想定）。")
    out.append("     そのため handoff の直後の区間は、開始時刻がメモの始まりと一致しやすい（ずれで判定しない）。")
    out.append("")
    out.append(f"{'#':>3} {'区間':<12}{'種類':<9}{'コマ':<11}{'開始':>9}  {'理由':<16}{'終了':>9}{'メモ開始':>10}{'ずれ':>9}")
    for i, (s, r) in enumerate(zip(sections, recs)):
        reason = "-" if r["reason"] is None else r["reason"] + ("（自動）" if r["auto"] else "")
        diff = "" if r["start"] is None else f"{r['start'] - s['memo_start']:+.1f}"
        mark = " ※" if r["start"] is not None and abs(r["start"] - s["memo_start"]) > fp.MEMO_TOLERANCE else ""
        out.append(
            f"{i:>3} {s['id']:<12}{s['kind']:<9}{'%d-%d' % s['frames']:<11}"
            f"{fmt_t(r['start']):>9}  {reason:<16}{fmt_t(r['end']):>9}{fmt_t(s['memo_start']):>10}{diff:>8}{mark}")
    out.append("")
    reached = recs[-1]["start"] is not None
    out.append(f"最後の区間（{sections[-1]['id']}）まで進んだか: {'はい' if reached else 'いいえ'}（完了の印まで: {'はい' if p.done else 'いいえ'}）")

    timeouts = [i for i, r in enumerate(recs) if r["reason"] == "タイムアウト"]
    stuck = [i for i in timeouts
             if any(part[0] == "sig" and part[1] not in recs[i]["seen"]
                    for part in sections[i]["until_parts"])]
    early = [i for i, r in enumerate(recs)
             if r["reason"] in ("合図", "秒数") and r["end"] is not None
             and r["end"] - sections[i]["memo_end"] < -fp.MEMO_TOLERANCE]

    def line(i):
        r = recs[i]
        return (f"  {sections[i]['id']}（{sections[i]['frames'][0]}-{sections[i]['frames'][1]}）"
                f" 上限{sections[i]['limit']}秒 終わり={sections[i]['until']}"
                f" 開始{fmt_t(r['start'])} 終了{fmt_t(r['end'])}")

    out.append("")
    out.append("進まない区間（上限まで、終わりの合図が一度も出なかった）:")
    out += [line(i) for i in stuck] or ["  なし"]
    out.append("早く進んだ区間（メモの次の始まりより3秒以上早く終わった）:")
    out += [f"{line(i)}  メモ次の始まり{fmt_t(sections[i]['memo_end'])}" for i in early] or ["  なし"]
    out.append("タイムアウトに頼った区間（上限の時間で次へ）:")
    out += [line(i) for i in timeouts] or ["  なし"]

    notes = set()
    if fp.NOTES.exists():
        for ln in fp.NOTES.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^(\d+)", ln)
            if m:
                notes.add(int(m.group(1)))
    missing = [s["id"] for s in sections if s["frames"][0] not in notes]
    out.append("")
    out.append(f"メモに行が無い区間の始点: {', '.join(missing) or 'なし'}")
    return "\n".join(out)


def fmt_t(t):
    if t is None:
        return "-"
    return f"{int(t // 60)}:{t % 60:04.1f}"


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, status=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                body = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/state":
                q = parse_qs(u.query)
                t = float(q.get("t", ["0"])[0])
                with app.lock:
                    if q.get("seek"):
                        app.enters[:] = [e for e in app.enters if e <= t]
                    enters = list(app.enters)
                self._json(app.state(t, enters))
            elif u.path == "/video":
                self._video()
            else:
                self.send_error(404)

        def do_POST(self):
            if urlparse(self.path).path != "/api/enter":
                self.send_error(404)
                return
            n = int(self.headers.get("Content-Length", "0"))
            t = float(json.loads(self.rfile.read(n) or b"{}")["t"])
            with app.lock:
                before = app.state(t, app.enters)
                if before["can_enter"]:
                    app.enters.append(t)
                enters = list(app.enters)
            self._json(app.state(t, enters))

        def _video(self):
            size = os.path.getsize(app.video)
            start, end, status = 0, size - 1, 200
            m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
            if m:
                a, b = m.groups()
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            with open(app.video, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 16, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

    return Handler


BUTTONS = {"A", "B", "X", "Y", "L", "R", "ZL", "ZR", "PLUS", "MINUS", "HOME", "L3", "R3",
           "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT"}
DIRS = ("UP", "DOWN", "LEFT", "RIGHT")
STICK_KEYS = {f"{s}S_{d}" for s in "LR" for d in DIRS}


def stick_xy(held, side):
    up, down, left, right = (f"{side}S_{d}" in held for d in DIRS)
    return 100 * (right - left), 100 * (up - down)


def screen_images():
    """work/flow_screens.html から {区間 ID: 手本の画像のパス} を作る。1つの行に画像が複数あれば1枚目。画像の無い行は入れない。"""
    if not SCREENS_HTML.exists():
        return {}
    html = SCREENS_HTML.read_text(encoding="utf-8")
    out = {}
    for row in html.split("<tr")[1:]:
        m = re.search(r'<td class="id">\d+\. (\w+)<small', row)
        img = re.search(r'<img[^>]*\ssrc="([^"]+)"', row)
        if m and img:
            out[m.group(1)] = (SCREENS_HTML.parent / img.group(1)).resolve()
    return out


class Live:

    def __init__(self, pad, dev):
        self.pad = pad
        self.frames = frames.iter_capture(dev)
        next(self.frames)
        self.cond = threading.Condition()
        self.jpg, self.count, self.fps = None, 0, 0.0
        self.lock = threading.Lock()
        self.held = set()
        self.sticks = {"L": (0, 0), "R": (0, 0)}
        self.last = time.monotonic()
        self.frame = None
        self.enabled = True
        self.rec, self.rec_start = None, 0.0
        threading.Thread(target=self._capture, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _capture(self):
        t0, n = time.monotonic(), 0
        for _, img in self.frames:
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            self.frame = img
            with self.cond:
                self.jpg, self.count = buf.tobytes(), self.count + 1
                self.cond.notify_all()
            n += 1
            now = time.monotonic()
            if now - t0 >= 1:
                self.fps, t0, n = n / (now - t0), now, 0

    def next_frame(self, last):
        """count が last より進むまで待ち、新しいコマ (JPEG, count) を返す。"""
        with self.cond:
            self.cond.wait_for(lambda: self.count > last)
            return self.jpg, self.count

    def apply(self, held):
        held = {n for n in held if n in BUTTONS or n in STICK_KEYS}
        with self.lock:
            self.last = time.monotonic()
            if not self.enabled:
                return []
            self._sync(held)
        return sorted(held)

    def set_enabled(self, on):
        with self.lock:
            self.enabled = on
            if not on:
                self.held = set()
                self.sticks = {"L": (0, 0), "R": (0, 0)}
                self.pad.release_all()

    def start_recording(self):
        with self.lock:
            self.rec, self.rec_start = [], time.monotonic()

    def stop_recording(self):
        """記録を返す。[{"t": 開始からの秒, "down": [...], "up": [...]}]"""
        with self.lock:
            rec, self.rec = self.rec or [], None
        return rec

    def _sync(self, held):
        """lock の中で呼ぶ。ボタンは押した・離したものだけ、スティックは値が変わった時だけ送る。"""
        if self.rec is not None and held != self.held:
            self.rec.append({"t": round(time.monotonic() - self.rec_start, 3),
                             "down": sorted(held - self.held), "up": sorted(self.held - held)})
        press = (held - self.held) & BUTTONS
        release = (self.held - held) & BUTTONS
        if press:
            self.pad.hold(*press)
        if release:
            self.pad.release(*release)
        for side in "LR":
            xy = stick_xy(held, side)
            if xy != self.sticks[side]:
                self.sticks[side] = xy
                self.pad.stick(side, *xy)
        self.held = held

    def _watch(self):
        while True:
            time.sleep(0.1)
            with self.lock:
                if self.held and time.monotonic() - self.last > 1.0:
                    self.held = set()
                    self.sticks = {"L": (0, 0), "R": (0, 0)}
                    self.pad.release_all()


def make_live_handler(live, ctl=None):
    images = screen_images()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                self._send(LIVE_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/stream":
                self._stream()
            elif path == "/api/state" and ctl:
                self._send(json.dumps(ctl.state(), ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")
            elif path == "/sounds/notify.mp3":
                p = ROOT / "common" / "sounds" / "notify.mp3"
                if p.exists():
                    self._send(p.read_bytes(), "audio/mpeg")
                else:
                    self.send_error(404)
            elif path == "/api/frame":
                q = parse_qs(urlparse(self.path).query)
                p = images.get(q.get("sec", [""])[0])
                try:
                    body = p.read_bytes() if p else None
                except OSError:
                    body = None
                if body is None:
                    self.send_error(404)
                else:
                    self._send(body, "image/jpeg")
            else:
                self.send_error(404)

        def do_POST(self):
            path = urlparse(self.path).path
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            if path == "/api/key":
                body = json.dumps({"held": live.apply(data.get("held", []))}).encode("utf-8")
                self._send(body, "application/json; charset=utf-8")
            elif path in ("/api/take", "/api/back", "/api/select") and ctl:
                ctl.request(path.rsplit("/", 1)[1], data.get("id"))
                self._send(b"{}", "application/json; charset=utf-8")
            else:
                self.send_error(404)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = 0
            try:
                while True:
                    jpg, last = live.next_frame(last)
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


def live_main(dev, addr, port):
    import nuxbt

    pad = Controller(nuxbt.Nuxbt(log_file_path="logs/nuxbt_dash.log"))
    live = Live(pad, dev)
    print("接続中...", flush=True)
    pad.connect(reconnect=True, address=addr)
    print("接続した。", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_live_handler(live))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"http://127.0.0.1:{port}/ を開く（q か Ctrl+C で終わる）", flush=True)
    try:
        with Keys() as keys:
            while not keys.quit.wait(5):
                print(f"映像 {live.fps:.1f}fps", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        pad.release_all()
        server.shutdown()
        server.server_close()
        pad.close()
        print("全部離して終了")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="videos/demo1.mp4")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--headless-report", action="store_true")
    ap.add_argument("--live", metavar="DEV", help="実機のキャプチャ（例 /dev/video0）。Switch に入力を送る")
    ap.add_argument("--switch", metavar="MAC", help="つなぐ Switch のアドレス（省略可）")
    args = ap.parse_args()
    if args.live:
        live_main(args.live, args.switch, args.port)
        return
    video = Path(args.video)
    if not video.is_absolute():
        video = ROOT / video

    app = App(video)
    if args.headless_report:
        print(headless_report(app))
        return
    print(f"判定済みの合図: {len(app.cues)} 件。http://127.0.0.1:{args.port}/ を開く（Ctrl+C で止める）", flush=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(app))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
