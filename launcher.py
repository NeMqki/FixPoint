"""FixPoint の起動口 (スタートメニュー・fixpoint コマンドから呼ばれる)。

1. すでに起動中ならブラウザを開くだけ
2. 更新があれば適用 (updater.py)
3. FixPoint を起動
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
# fixpoint を読み込む前から、カレントディレクトリを実行ファイル検索に含めない
if os.name == "nt":
    os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"


def _setup_log():
    # pythonw (コンソールなし) で起動されたときはログファイルへ
    if sys.stdout is None or sys.stderr is None:
        f = open(HERE / "fixpoint.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f


def running_on(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "fixpoint"
    except Exception:
        return False


def main():
    _setup_log()
    import updater

    cfg = updater.load_config()
    ap = argparse.ArgumentParser(prog="fixpoint")
    ap.add_argument("--project", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-update", action="store_true")
    ap.add_argument("--restarted", action="store_true", help=argparse.SUPPRESS)  # 更新後の自動再起動
    ap.add_argument("--idle-exit", action="store_true", help=argparse.SUPPRESS)  # 専用ウィンドウで起動していた
    ap.add_argument("--browser", action="store_true", help="専用のウィンドウではなく、いつものブラウザで開く")
    ap.add_argument("command", nargs="?", choices=["update", "rollback", "version"], help="update=今すぐ更新 / rollback=1つ前に戻す / version")
    a = ap.parse_args()
    port = a.port or int(cfg.get("port") or 8765)
    window = cfg.get("app_window", True) and not a.browser

    if a.command == "version":
        print(f"fixpoint {updater.local_version()}  (更新元: {cfg.get('update_source') or '未設定'})")
        return
    if a.command == "update":
        m = updater.check(timeout=15) if cfg.get("update_source") else None
        print(f"{updater.apply(m)} に更新しました" if m else f"最新です ({updater.local_version()})")
        return
    if a.command == "rollback":
        print(updater.rollback() or "戻せるバックアップがありません")
        return

    if not a.restarted and running_on(port):
        print(f"FixPoint はすでに起動しています → http://127.0.0.1:{port}")
        if not a.no_browser:
            if window:
                import fixpoint
                fixpoint.open_window(f"http://127.0.0.1:{port}")
            else:
                webbrowser.open(f"http://127.0.0.1:{port}")
        return

    if not a.no_update:
        updater.update_if_available(log=print)

    # 優先順: --project > ターミナルで開いているフォルダ > 前回のプロジェクト > ホーム
    cwd = Path(os.getcwd()).resolve()
    from_terminal = sys.stdin is not None and sys.stdin.isatty() and cwd not in (Path.home().resolve(), HERE)
    project = a.project or (str(cwd) if from_terminal else None) or cfg.get("last_project") or str(Path.home())
    argv = ["fixpoint", "--project", project, "--port", str(port)]
    if a.no_browser:
        argv.append("--no-browser")
    if window and not a.no_browser:
        argv.append("--window")
    if a.idle_exit:
        argv.append("--idle-exit")
    sys.argv = argv
    import fixpoint  # 更新後のコードを読み込む
    fixpoint.main()


if __name__ == "__main__":
    main()
