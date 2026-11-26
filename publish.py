#!/usr/bin/env python3
"""リリースを作る (開発者用。普段は GitHub Actions が tag の push で自動実行する)。

  python publish.py [--version 0.4.0] [--notes "変更内容"] [--source <更新元>]

作るもの (release/ にすべて平らに置く = GitHub Release の添付ファイルになる):
  fixpoint-setup.exe     配るインストーラー (Python ごと入る。Inno Setup で作る)
  manifest.json          自動更新が最初に見るファイル (バージョンと各ファイルの SHA-256)
  fixpoint.py など        アプリ本体 (自動更新用)
  RELEASE_NOTES.md       リリースページの本文 (日本語)

--source はインストーラーに埋め込まれ、インストールされた FixPoint がその場所を見に行きます。
既定は GitHub の「最新リリースの添付ファイル」: https://github.com/NeMqki/FixPoint/releases/latest/download

インストーラーを作るには Inno Setup 6 (ISCC.exe) と、先にビルドした FixPoint アプリ
(desktop/ で cargo build --release → desktop/target/release/fixpoint-app.exe) が必要です。
無いときは --no-installer で自動更新用だけ作れます。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "NeMqki/FixPoint"
DEFAULT_SOURCE = f"https://github.com/{REPO}/releases/latest/download"
APP_FILES = ["fixpoint.py", "fixpoint_ui.html", "fixpoint_lock.py", "updater.py", "launcher.py", "README.md"]

# 同梱する Python (python.org の embeddable 版)。上げるときは版とハッシュを両方変える
PY_VERSION = "3.14.10"
PY_ZIP = f"python-{PY_VERSION}-embed-amd64.zip"
PY_URL = f"https://www.python.org/ftp/python/{PY_VERSION}/{PY_ZIP}"
PY_SHA256 = "90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159"

# fixpoint コマンド。インストール先がどこでも動くように、自分の場所からの相対パスで書く
FIXPOINT_CMD = '@echo off\r\n"%~dp0..\\python\\python.exe" "%~dp0..\\launcher.py" %*\r\n'


def read_version() -> str:
    m = re.search(r'^VERSION = "([^"]+)"', (ROOT / "fixpoint.py").read_text(encoding="utf-8"), re.M)
    return m.group(1)


def set_version(v: str):
    p = ROOT / "fixpoint.py"
    s = p.read_text(encoding="utf-8")
    p.write_text(re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{v}"', s, count=1, flags=re.M), encoding="utf-8")


def collect() -> dict[str, bytes]:
    files = {f: (ROOT / f).read_bytes() for f in APP_FILES}
    # 以前の .cmd 版で入れた人は、アンインストールにこれを使うので配り続ける
    un = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    files["uninstall.ps1"] = un.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig")
    return files


def get_python(cache: Path) -> Path:
    """embeddable Python を取ってきて、ハッシュを確かめる"""
    cache.mkdir(parents=True, exist_ok=True)
    z = cache / PY_ZIP
    if not z.exists() or hashlib.sha256(z.read_bytes()).hexdigest() != PY_SHA256:
        print(f"  Python {PY_VERSION} をダウンロードしています…")
        req = urllib.request.Request(PY_URL, headers={"User-Agent": "fixpoint-publish"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if hashlib.sha256(data).hexdigest() != PY_SHA256:
            raise SystemExit(f"Python のハッシュが合いません: {PY_URL}")
        z.write_bytes(data)
    return z


def find_iscc() -> str | None:
    cands = [os.environ.get("ISCC", ""), shutil.which("iscc") or "",
             str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/Inno Setup 6/ISCC.exe"),
             str(Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Inno Setup 6/ISCC.exe"),
             str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Inno Setup 6/ISCC.exe")]
    return next((c for c in cands if c and Path(c).is_file()), None)


def build_installer(files: dict[str, bytes], version: str, notes: str, source: str, build: Path, dist: Path,
                    app_exe: Path) -> Path:
    iscc = find_iscc()
    if not iscc:
        raise SystemExit("Inno Setup 6 (ISCC.exe) が見つかりません。入れるか、--no-installer を付けてください")
    if not app_exe.is_file():
        raise SystemExit(f"FixPoint アプリ ({app_exe}) がありません。先に desktop/ で cargo build --release してください")
    if build.exists():
        shutil.rmtree(build)
    app, py = build / "app", build / "python"
    (app / "bin").mkdir(parents=True)
    for name, data in files.items():
        if name != "uninstall.ps1":
            (app / name).write_bytes(data)
    (app / "app.json").write_text(json.dumps({"name": "fixpoint", "version": version, "notes": notes},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
    (app / "bin" / "fixpoint.cmd").write_bytes(FIXPOINT_CMD.encode("ascii"))
    with zipfile.ZipFile(get_python(ROOT / ".cache")) as z:
        z.extractall(py)
    dist.mkdir(parents=True, exist_ok=True)
    out = dist / "fixpoint-setup.exe"
    if out.exists():
        out.unlink()
    subprocess.run([iscc, "/Q", f"/DAppVer={version}", f"/DUpdateSource={source}", f"/DAppDir={app}",
                    f"/DPyDir={py}", f"/DAppExe={app_exe.resolve()}", f"/DOutDir={dist}",
                    str(ROOT / "installer" / "fixpoint.iss")], check=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="更新元 (URL かフォルダ)。インストーラーに埋め込まれる")
    ap.add_argument("--version", help="新しいバージョン (省略時は fixpoint.py の VERSION)")
    ap.add_argument("--notes", default="", help="更新内容のメモ (UIに表示)")
    ap.add_argument("--release-dir", default=str(ROOT / "release"))
    ap.add_argument("--dist-dir", default=str(ROOT / "dist"))
    ap.add_argument("--build-dir", default=str(ROOT / "build"))
    ap.add_argument("--no-installer", action="store_true", help="インストーラーを作らない (自動更新用のファイルだけ)")
    ap.add_argument("--app-exe", default=str(ROOT / "desktop" / "target" / "release" / "fixpoint-app.exe"),
                    help="インストーラーに入れる FixPoint アプリ (Tauri でビルドしたもの)")
    a = ap.parse_args()

    if a.version:
        set_version(a.version)
    version = read_version()
    files = collect()

    # 1. 更新元に置く release/ (GitHub Release の添付ファイルは階層を持てないので平らに置く)
    rel = Path(a.release_dir)
    if rel.exists():
        shutil.rmtree(rel)
    rel.mkdir(parents=True)
    entries = []
    for name, data in files.items():
        asset = name.replace("/", "__")
        (rel / asset).write_bytes(data)
        entries.append({"path": name, "asset": asset, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    manifest = {"name": "fixpoint", "version": version, "notes": a.notes,
                "published_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": entries}
    (rel / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # 2. インストーラー fixpoint-setup.exe (Python 同梱)
    out = None
    if not a.no_installer:
        out = build_installer(files, version, a.notes, a.source, Path(a.build_dir), Path(a.dist_dir), Path(a.app_exe))
        shutil.copy2(out, rel / out.name)

    # 3. リリースページの本文 (日本語)
    notes = a.notes.strip() or "細かな修正をしました。"
    (rel / "RELEASE_NOTES.md").write_text(f"""{notes}

---

**もう使っている人へ**
何もしなくて大丈夫です。次に起動したとき（起動したままなら数時間以内）に、自動でこのバージョンになります。
すぐ試したいときは、ターミナルで `fixpoint update` を実行してください。

**はじめての人へ**
下の Assets にある `fixpoint-setup.exe` をダウンロードして、ダブルクリックするだけです。Python も一緒に入ります。
「Windows によって PC が保護されました」と出たら、「詳細情報」→「実行」を押してください。

ほかのファイルは自動アップデート用なので、ダウンロードしなくて大丈夫です。
""", encoding="utf-8")

    print(f"fixpoint {version}")
    print(f"  更新元に置くもの : {rel}  ({len(entries)} ファイル)")
    if out:
        print(f"  インストーラー   : {out}  ({out.stat().st_size // 1024} KB, Python {PY_VERSION} 同梱)")
    print(f"  埋め込んだ更新元 : {a.source}")


if __name__ == "__main__":
    main()
