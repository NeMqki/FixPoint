#!/usr/bin/env python3
"""リリースを作る (開発者用。普段は GitHub Actions が tag の push で自動実行する)。

  python publish.py [--version 0.3.1] [--notes "変更内容"] [--source <更新元>]

作るもの (release/ にすべて平らに置く = GitHub Release の添付ファイルになる):
  manifest.json          自動更新が最初に見るファイル (バージョンと各ファイルの SHA-256)
  fixpoint.py など        アプリ本体
  fixpoint-setup.cmd     配るインストーラー (ダブルクリックで入る)
  RELEASE_NOTES.md       リリースページの本文 (日本語)

--source はインストーラーに埋め込まれ、インストールされた FixPoint がその場所を見に行きます。
既定は GitHub の「最新リリースの添付ファイル」: https://github.com/NeMqki/FixPoint/releases/latest/download
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = "NeMqki/FixPoint"
DEFAULT_SOURCE = f"https://github.com/{REPO}/releases/latest/download"
APP_FILES = ["fixpoint.py", "fixpoint_ui.html", "fixpoint_lock.py", "updater.py", "launcher.py", "README.md"]
BOM_FILES = {"uninstall.ps1"}  # Windows PowerShell 5.1 は BOM なし UTF-8 を正しく読めない

BATCH_HEADER = (
    "@echo off\r\n"
    "setlocal\r\n"
    "set \"FIXPOINT_SETUP_SELF=%~f0\"\r\n"
    "powershell -NoProfile -ExecutionPolicy Bypass -Command \"$t=[IO.File]::ReadAllText($env:FIXPOINT_SETUP_SELF,[Text.Encoding]::UTF8);"
    "$a=$t.IndexOf('#__PS'+'1__');$b=$t.IndexOf('#__PAY'+'LOAD__');iex $t.Substring($a,$b-$a)\"\r\n"
    "exit /b %errorlevel%\r\n"
)


def read_version() -> str:
    m = re.search(r'^VERSION = "([^"]+)"', (ROOT / "fixpoint.py").read_text(encoding="utf-8"), re.M)
    return m.group(1)


def set_version(v: str):
    p = ROOT / "fixpoint.py"
    s = p.read_text(encoding="utf-8")
    p.write_text(re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{v}"', s, count=1, flags=re.M), encoding="utf-8")


def collect() -> dict[str, bytes]:
    files = {f: (ROOT / f).read_bytes() for f in APP_FILES}
    un = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    files["uninstall.ps1"] = un.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8-sig")
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=DEFAULT_SOURCE, help="更新元 (URL かフォルダ)。インストーラーに埋め込まれる")
    ap.add_argument("--version", help="新しいバージョン (省略時は fixpoint.py の VERSION)")
    ap.add_argument("--notes", default="", help="更新内容のメモ (UIに表示)")
    ap.add_argument("--release-dir", default=str(ROOT / "release"))
    ap.add_argument("--dist-dir", default=str(ROOT / "dist"))
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

    # 2. インストーラー dist/fixpoint-setup.cmd (バッチ + PowerShell + zip ペイロードの1ファイル)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
        z.writestr("app.json", json.dumps({"name": "fixpoint", "version": version, "notes": a.notes},
                                          ensure_ascii=False, indent=2))
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    ps = (ROOT / "installer" / "install.ps1").read_text(encoding="utf-8")
    ps = ps.replace("__VERSION__", version).replace("__UPDATE_SOURCE__", a.source.replace("'", "''"))
    body = (BATCH_HEADER + "#__PS1__\r\n" + ps.replace("\r\n", "\n").replace("\n", "\r\n")
            + "\r\n#__PAYLOAD__\r\n" + "\r\n".join(b64[i:i + 100] for i in range(0, len(b64), 100)) + "\r\n")
    dist = Path(a.dist_dir)
    dist.mkdir(parents=True, exist_ok=True)
    out = dist / "fixpoint-setup.cmd"
    out.write_bytes(body.encode("utf-8"))
    shutil.copy2(out, rel / out.name)

    # 3. リリースページの本文 (日本語)
    notes = a.notes.strip() or "（変更内容の記載はありません）"
    (rel / "RELEASE_NOTES.md").write_text(f"""## 変更内容

{notes}

## インストール

1. 下の **Assets** から `fixpoint-setup.cmd` をダウンロード
2. ダブルクリックで実行（管理者権限は不要です）
3. スタートメニューかデスクトップの「FixPoint」から起動

Python 3.9 以上が必要です。入っていなければ、セットアップが入れ方を案内します。
Windows に「PC を保護しました」と表示されたら、「詳細情報」→「実行」を選んでください。

## 自動更新

すでにインストールしている場合は、何もしなくてかまいません。起動時と、起動中は6時間ごとに、このリリースを自動で取り込みます（AIの作業中は、終わるまで待ちます）。
すぐに更新したいときは、ターミナルで `fixpoint update` を実行してください。

## ファイル

| ファイル | 内容 |
|---|---|
| `fixpoint-setup.cmd` | インストーラー（これだけダウンロードすれば OK） |
| `manifest.json` | 自動更新用（バージョンと各ファイルの SHA-256） |
| その他 | 自動更新でダウンロードされるアプリ本体 |
""", encoding="utf-8")

    print(f"fixpoint {version}")
    print(f"  更新元に置くもの : {rel}  ({len(entries)} ファイル)")
    print(f"  インストーラー   : {out}  ({out.stat().st_size // 1024} KB)")
    print(f"  埋め込んだ更新元 : {a.source}")


if __name__ == "__main__":
    main()
