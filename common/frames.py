import time

import cv2

WIDTH, HEIGHT = 1280, 720


def iter_frames(path, step=1):
    """動画を先頭から1コマずつ読み、(動画内の秒, 1280x720 のBGR画像) を返す。
    step 個ごとに1コマ返す（間のコマは grab だけして捨てる）。"""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise OSError(f"開けない: {path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    try:
        i = 0
        while True:
            if i % step == 0:
                ok, frame = cap.read()
                if not ok:
                    break
                h, w = frame.shape[:2]
                if h >= HEIGHT and w >= WIDTH and (h, w) != (HEIGHT, WIDTH):
                    frame = frame[:HEIGHT, :WIDTH]
                elif (h, w) != (HEIGHT, WIDTH):
                    frame = cv2.resize(frame, (WIDTH, HEIGHT))
                yield i / fps, frame
            elif not cap.grab():
                break
            i += 1
    finally:
        cap.release()


def iter_capture(dev="/dev/video0"):
    """キャプチャ機器から 1280x720 のコマを止めずに読み、(開始からの秒, BGR画像) を返す。
    開けなければ OSError。"""
    cap = cv2.VideoCapture(dev, cv2.CAP_V4L2)
    if not cap.isOpened():
        raise OSError(f"開けない: {dev}")
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, 30)
    t0 = time.monotonic()
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                raise OSError(f"読めない: {dev}")
            if frame.shape[:2] != (HEIGHT, WIDTH):
                frame = cv2.resize(frame, (WIDTH, HEIGHT))
            yield time.monotonic() - t0, frame
    finally:
        cap.release()
