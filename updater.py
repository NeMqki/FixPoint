"""FixPoint の自動更新。

更新元 (update_source) は次のどちらか:
  - https://… の URL (例: https://raw.githubusercontent.com/<user>/<repo>/main/release)
  - フォルダのパス (共有フォルダなど)
そこに publish.py が作る manifest.json と各ファイルが置いてある前提。

manifest.json:
  {"name": "fixpoint", "version": "0.3.1", "notes": "...",
   "files": [{"path": "fixpoint.py", "sha256": "…", "size": 123}, ...]}

安全のため:
  - すべてのファイルを SHA-256 で検証してから置き換える (1つでも合わなければ何も変えない)
  - manifest のパスはインストール先の中の相対パスだけ許可
  - 置き換え前のファイルは .backup/<旧バージョン>/ に保存 (rollback() で戻せる)
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import urllib.request
from pathlib import Path, PurePosixPath

APP_JSON = "app.json"
CONFIG_JSON = "config.json"
DEFAULT_CONFIG = {
    "update_source": "",
    "auto_update": True,
    "check_interval_hours": 6,
    "port": 8765,
    "last_project": "",
}


def app_dir() -> Path:
    return Path(__file__).resolve().parent


def load_config(d: Path | None = None) -> dict:
    d = d or app_dir()
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads((d / CONFIG_JSON).read_text(encoding="utf-8")))
    except Exception:
        pass
    if os.environ.get("FIXPOINT_UPDATE_SOURCE"):
        cfg["update_source"] = os.environ["FIXPOINT_UPDATE_SOURCE"]
    return cfg


def save_config(cfg: dict, d: Path | None = None):
    d = d or app_dir()
    tmp = d / (CONFIG_JSON + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, d / CONFIG_JSON)


def local_version(d: Path | None = None) -> str:
    d = d or app_dir()
    try:
        return json.loads((d / APP_JSON).read_text(encoding="utf-8"))["version"]
    except Exception:
        return "0.0.0"


def vtuple(v: str) -> tuple:
    out = []
    for p in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def _fetch(source: str, rel: str, timeout: float = 20) -> bytes:
    if source.startswith(("http://", "https://")):
        url = source.rstrip("/") + "/" + rel
        req = urllib.request.Request(url, headers={"User-Agent": "fixpoint-updater", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    return (Path(source) / rel).read_bytes()


def _safe_rel(p: str) -> str:
    pp = PurePosixPath(p.replace("\\", "/"))
    if pp.is_absolute() or ".." in pp.parts or not pp.parts or ":" in pp.parts[0]:
        raise ValueError(f"不正なパス: {p}")
    if pp.parts[0] in (".backup", ".update-staging") or pp.name in (CONFIG_JSON,):
        raise ValueError(f"更新対象にできないパス: {p}")
    return str(pp)


def check(source: str | None = None, d: Path | None = None, timeout: float = 20) -> dict | None:
    """新しいバージョンがあれば manifest を返す。なければ None。"""
    d = d or app_dir()
    source = source if source is not None else load_config(d).get("update_source")
    if not source:
        return None
    m = json.loads(_fetch(source, "manifest.json", timeout).decode("utf-8"))
    if vtuple(m["version"]) > vtuple(local_version(d)):
        return m
    return None


def apply(manifest: dict, source: str | None = None, d: Path | None = None, timeout: float = 60) -> str:
    """manifest のファイルをダウンロード・検証して置き換える。新しいバージョン文字列を返す。"""
    d = d or app_dir()
    source = source if source is not None else load_config(d).get("update_source")
    stage = d / ".update-staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    try:
        files = []
        for f in manifest["files"]:
            rel = _safe_rel(f["path"])
            data = _fetch(source, f.get("asset") or ("files/" + rel), timeout)
            h = hashlib.sha256(data).hexdigest()
            if h != f["sha256"].lower():
                raise ValueError(f"チェックサム不一致: {rel}（ダウンロードが壊れているか、改ざんされています）")
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            files.append(rel)
        # ここまで全部検証できたら置き換える
        old = local_version(d)
        backup = d / ".backup" / old
        backup.mkdir(parents=True, exist_ok=True)
        for rel in files:
            target = d / rel
            if target.exists():
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup / rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage / rel, target)
        (d / APP_JSON).write_text(json.dumps({"name": manifest.get("name", "fixpoint"),
                                              "version": manifest["version"],
                                              "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                              "notes": manifest.get("notes", "")}, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        _prune_backups(d)
        _update_uninstall_entry(manifest["version"])
        return manifest["version"]
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def rollback(d: Path | None = None) -> str | None:
    """直前のバックアップに戻す。戻したバージョンを返す。"""
    d = d or app_dir()
    bdir = d / ".backup"
    if not bdir.exists():
        return None
    vers = sorted((p for p in bdir.iterdir() if p.is_dir()), key=lambda p: vtuple(p.name))
    if not vers:
        return None
    src = vers[-1]
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, d / rel)
    meta = {"name": "fixpoint", "version": src.name, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "notes": "rollback"}
    (d / APP_JSON).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(src, ignore_errors=True)
    return src.name


def _prune_backups(d: Path, keep: int = 3):
    bdir = d / ".backup"
    vers = sorted((p for p in bdir.iterdir() if p.is_dir()), key=lambda p: vtuple(p.name))
    for p in vers[:-keep]:
        shutil.rmtree(p, ignore_errors=True)


def _update_uninstall_entry(version: str):
    if os.name != "nt" or os.environ.get("FIXPOINT_NO_REGISTRY"):
        return
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Uninstall\fixpoint", 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
        winreg.CloseKey(k)
    except OSError:
        pass


def update_if_available(log=print, d: Path | None = None) -> str | None:
    """確認して、あれば適用する。失敗しても例外は出さない (起動を止めないため)。"""
    d = d or app_dir()
    cfg = load_config(d)
    if not cfg.get("auto_update") or not cfg.get("update_source"):
        return None
    try:
        m = check(cfg["update_source"], d, timeout=8)
        if not m:
            return None
        log(f"[更新] {local_version(d)} → {m['version']} をダウンロードしています…")
        v = apply(m, cfg["update_source"], d)
        log(f"[更新] {v} に更新しました。{m.get('notes', '')}")
        return v
    except Exception as e:
        log(f"[更新] 確認に失敗しました（そのまま起動します）: {e}")
        return None


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        m = check()
        print(f"現在 {local_version()} / " + (f"新しいバージョン {m['version']} があります" if m else "最新です"))
    elif cmd == "update":
        print(update_if_available() or "更新はありません")
    elif cmd == "rollback":
        print(rollback() or "戻せるバックアップがありません")
