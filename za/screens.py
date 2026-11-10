"""画面の合図を判定する。範囲は za/templates.json（template_picker で囲んだもの）。

使い方: .venv/bin/python za/screens.py <画像パス...>
"""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES_JSON = ROOT / "za" / "templates.json"
TEMPLATES_DIR = ROOT / "za" / "templates"
REFS_NPZ = ROOT / "za" / "refs.npz"  # 見本の輝度の配列（templates/ の PNG から作る）

BLACKOUT_MEAN = 12
BRIGHT_MEAN = 20     # 画面の平均の明るさがこれ以上なら明るい（12〜20 は暗転の続き。暗転の途中の薄暗い読み込みの画面）
BLUE_RATIO = 0.2
GREEN_RATIO = 0.3    # バトルの範囲で緑の画素がこれを超えたらバトル開始（HPが満タンの間だけ）
GREEN_TOL = 15      # 緑の各色の許容差（基準 BGR(116,215,47) から各色 ±15。手本 (116,210,36) と実機 (117,220,58) の両方に入る）
TALK_WHITE = 0.1
B_BACK_NCC = 0.75    # B戻るの範囲が見本と形（明るさの上下）でこれ以上相関するなら「B 戻る」が出ている。背景の明るさが違っても拾う（実測: 出ている 0.78〜0.99、会話の最中の他の場面は 0.73 以下）
FACE_SAME = 0.9
NAME_SAME = 0.95     # 名前入力の範囲が見本とこれ以上同じなら名前入力の画面（文字入力の最中は 0.956 以上、ほかの場面は最大 0.941 で実測）
PROFILE_SAME = 0.9
MENU2_SAME = 0.9

BACK_NCC = 0.9       # 右下の「戻る」の見本と形がこれ以上相関するなら back（実測: 出ている 0.98〜1.0、「閉じる」は 0.78〜0.79）
CLOSE_NCC = 0.95     # 右下の「閉じる」の見本と形がこれ以上相関するならメニュー画面 menu_open（実測: 1.0、青枠は 0.9）
FRAME_BGR = (100, 64, 40)  # 会話の青い枠の平均の色（実測 B97〜102 G58〜67 R31〜47）
FRAME_TOL = 15
CURSOR_WHITE = 0.5   # ボックスの範囲で白い画素（明るさ200以上）がこれを超えたらカーソルがボックスにある

# 白枠・青枠（画面下の同じ長方形 x288〜992 y558〜688 に出る）。範囲は (x0, x1, y0, y1)、両端を含む
BOX_EDGE = ((300, 979, 558, 560), (300, 979, 686, 688), (289, 291, 570, 679), (989, 991, 570, 679))
BOX_OUTER = ((300, 979, 548, 553), (300, 979, 692, 697), (278, 283, 570, 679), (996, 1001, 570, 679))
BOX_HIT = 0.9        # ふちがこの割合以上で枠の色
BOX_OUTER_MAX = 0.6  # 外側が枠の色でこの割合未満
BLUE_EDGE_BGR = (80, 38, 22)
BLUE_TOL = 20

SIGNALS = ("blackout", "skip", "talk", "battle", "b_back", "face_screen", "name_screen", "profile", "menu2", "bright",
           "back", "menu_open", "frame", "back_only", "cursor_box", "white_box", "blue_box")
REF_PNGS = {
    "B戻る": "B戻る.png",
    "顔選択": "顔選択.png",
    "名前入力": "名前入力画面.png",
    "プロフィール": "プロフィール.png",
    "メニュー2": "メニュー2.png",
    "戻る": "戻る.png",
    "閉じる": "閉じる.png",
}


def load_boxes(path=TEMPLATES_JSON):
    """templates.json を {種類名: {frame, x, y, w, h}} で返す。"""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _crop(img, box):
    return img[box["y"]:box["y"] + box["h"], box["x"]:box["x"] + box["w"]]


def _ratio(mask):
    return float(mask.mean())


def _blue(bgr):
    b, g, r = (bgr[..., i].astype(int) for i in range(3))
    return (b > 120) & (b - r > 60)


def _green(bgr):
    """HPバーの緑 BGR(116,215,47) にほぼ一致する画素（手本 (116,210,36)・実機 (117,220,58) の両方に入る）。"""
    return (np.abs(bgr.astype(int) - (116, 215, 47)).max(axis=2) <= GREEN_TOL)


def _pixels(frame, areas):
    return np.concatenate([frame[y0:y1 + 1, x0:x1 + 1].reshape(-1, 3) for x0, x1, y0, y1 in areas]).astype(int)


def _white_at(px, low):
    return float((px.min(axis=1) >= low).mean())


def _color_at(px, bgr):
    return float((np.abs(px - bgr).max(axis=1) <= BLUE_TOL).mean())


def _box_size_ok(frame):
    return frame.shape[:2] == (720, 1280)


class Screens:
    def __init__(self, boxes=None):
        self.boxes = boxes or load_boxes()
        self.refs = {}
        with np.load(REFS_NPZ) as refs:
            for name in REF_PNGS:
                if name in self.boxes:
                    self.refs[name] = refs[name].astype(np.float32)

    def blackout(self, frame):
        return float(frame.mean()) < BLACKOUT_MEAN

    def bright(self, frame):
        return float(frame.mean()) >= BRIGHT_MEAN

    def skip(self, frame):
        box = self.boxes.get("スキップ")
        return bool(box) and _ratio(_blue(_crop(frame, box))) > BLUE_RATIO

    def talk(self, frame):
        box = self.boxes.get("会話")
        if not box:
            return False
        gray = cv2.cvtColor(_crop(frame, box), cv2.COLOR_BGR2GRAY)
        return _ratio(gray >= 200) > TALK_WHITE

    def battle(self, frame):
        box = self.boxes.get("バトル")
        return bool(box) and _ratio(_green(_crop(frame, box))) > GREEN_RATIO

    def _same(self, frame, name, same):
        """範囲 name の輝度が見本とほぼ同じか（見本との差の平均を 1 から引いた値が same 以上）。"""
        ref = self.refs.get(name)
        if ref is None:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        diff = np.abs(_crop(gray, self.boxes[name]).astype(np.float32) - ref).mean() / 255
        return 1 - diff >= same

    def b_back(self, frame):
        """B戻るの範囲と見本の相関（平均を引いて割る）。背景の明るさが変わっても形で当てる。"""
        ref = self.refs.get("B戻る")
        if ref is None:
            return False
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        crop = _crop(gray, self.boxes["B戻る"]).astype(np.float32)
        a, b = crop - crop.mean(), ref - ref.mean()
        denom = np.sqrt((a * a).sum() * (b * b).sum())
        return denom > 0 and float((a * b).sum() / denom) >= B_BACK_NCC

    def _ncc(self, frame, name):
        ref = self.refs.get(name)
        if ref is None:
            return 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        a = _crop(gray, self.boxes[name]).astype(np.float32)
        a, b = a - a.mean(), ref - ref.mean()
        denom = np.sqrt((a * a).sum() * (b * b).sum())
        return float((a * b).sum() / denom) if denom > 0 else 0.0

    def back(self, frame):
        return self._ncc(frame, "戻る") >= BACK_NCC

    def menu_open(self, frame):
        return self._ncc(frame, "閉じる") >= CLOSE_NCC

    def frame(self, frame):
        """会話の青い枠が出ているか（枠の中の文字は問わない。範囲の平均の色が枠の色）。"""
        box = self.boxes.get("枠")
        if not box:
            return False
        mean = _crop(frame, box).reshape(-1, 3).mean(axis=0)
        return bool(np.abs(mean - FRAME_BGR).max() <= FRAME_TOL)

    def white_box(self, frame):
        """白の台詞枠がはっきり出ているか（出入りの途中は False）。"""
        if not _box_size_ok(frame):
            return False
        return (_white_at(_pixels(frame, BOX_EDGE), 230) >= BOX_HIT
                and _white_at(_pixels(frame, BOX_OUTER), 230) < BOX_OUTER_MAX)

    def blue_box(self, frame):
        """青のシステム枠がはっきり出ているか（出入りの途中は False）。"""
        if not _box_size_ok(frame):
            return False
        return (_color_at(_pixels(frame, BOX_EDGE), BLUE_EDGE_BGR) >= BOX_HIT
                and _color_at(_pixels(frame, BOX_OUTER), BLUE_EDGE_BGR) < BOX_OUTER_MAX)

    def back_only(self, frame):
        return self.back(frame) and not self.blue_box(frame)

    def cursor_box(self, frame):
        """メニューのカーソルがボックスにあるか（カーソルのある項目は白くなる。メニュー画面の時だけ）。"""
        box = self.boxes.get("ボックス")
        if not box or not self.menu_open(frame):
            return False
        gray = cv2.cvtColor(_crop(frame, box), cv2.COLOR_BGR2GRAY)
        return _ratio(gray >= 200) > CURSOR_WHITE

    def face_screen(self, frame):
        return self._same(frame, "顔選択", FACE_SAME)

    def name_screen(self, frame):
        return self._same(frame, "名前入力", NAME_SAME)

    def profile(self, frame):
        return self._same(frame, "プロフィール", PROFILE_SAME)

    def menu2(self, frame):
        """メニュー2（last_menu の途中の画面）が出ているか。"""
        return self._same(frame, "メニュー2", MENU2_SAME)

    def check(self, name, frame):
        return bool(getattr(self, name)(frame))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", type=Path, nargs="+")
    args = ap.parse_args()

    s = Screens()
    for path in args.images:
        img = cv2.imread(str(path))
        if img is None:
            print(f"{path}: 読めない")
            continue
        on = [n for n in SIGNALS if s.check(n, img)]
        print(f"{path}: {','.join(on) or 'なし'}")


if __name__ == "__main__":
    main()
