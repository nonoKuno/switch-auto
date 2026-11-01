import subprocess


def notify(title: str, body: str) -> None:
    try:
        subprocess.run(["notify-send", "-h", "boolean:suppress-sound:true", "--", title, body], check=False)
    except OSError:
        pass  # 通知が出せなくても本体は止めない
