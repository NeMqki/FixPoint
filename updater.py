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
  - 8.3 短縮名でも config.json / app.json / .backup / .update-staging は上書きしない
  - 置き換え前のファイルは .backup/<旧バージョン>/ に保存 (rollback() で戻せる)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

APP_JSON = "app.json"
CONFIG_JSON = "config.json"
# 更新で書いてはいけない名前。先頭要素がこのディレクトリ、またはファイル名がこのファイル。
_RESERVED_DIRS = (".backup", ".update-staging")
_RESERVED_FILES = (CONFIG_JSON, APP_JSON)
DEFAULT_CONFIG = {
    "update_source": "",
    "auto_update": True,
    "check_interval_hours": 6,
    "port": 8765,
    "last_project": "",
    "app_window": True,  # false にすると、専用ウィンドウではなくいつものブラウザで開く
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


_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
VERSION_RE = re.compile(r"^v?[0-9]+(\.[0-9]+){0,3}([-+][0-9A-Za-z.]{1,20})?$")


def _check_version(v) -> str:
    # バージョンは .backup/<バージョン>/ のフォルダ名にもなるので、パスとして危険な文字を許さない
    if not isinstance(v, str) or not VERSION_RE.match(v) or ".." in v:
        raise ValueError(f"不正なバージョン: {v!r}")
    return v


def _fetch(source: str, rel: str, timeout: float = 20) -> bytes:
    if source.startswith(("http://", "https://")):
        host = (urllib.parse.urlsplit(source).hostname or "").lower()
        if source.startswith("http://") and host not in _LOCAL_HOSTS:
            # 暗号化されていない更新元は途中で書き換えられる (manifest ごと差し替えればチェックサムも通ってしまう)
            raise ValueError("更新元は https:// にしてください (http:// はこのPC内のサーバーだけ使えます)")
        url = source.rstrip("/") + "/" + rel
        req = urllib.request.Request(url, headers={"User-Agent": "fixpoint-updater", "Cache-Control": "no-cache"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if source.startswith("https://") and not r.geturl().startswith("https://"):
                raise ValueError(f"https から http へのリダイレクトは受け付けません: {r.geturl()}")
            return r.read()
    return (Path(source) / rel).read_bytes()


def _win_file_key(name: str) -> str:
    # CreateFile は末尾の '.' と空白を捨てる。config.json. も config.json も同じファイルになる。
    return name.rstrip(". ").lower()


def _safe_rel(p: str) -> str:
    if not isinstance(p, str):
        raise ValueError(f"不正なパス: {p!r}")
    pp = PurePosixPath(p.replace("\\", "/"))
    # ":" はドライブ名 (C:) と NTFS の代替データストリーム (a.py:x) の両方を防ぐため、どの部分にも許さない
    if pp.is_absolute() or ".." in pp.parts or not pp.parts or any(":" in x for x in pp.parts):
        raise ValueError(f"不正なパス: {p}")
    # Windows は大文字小文字を区別しないので小文字で比べる
    if _win_file_key(pp.parts[0]) in _RESERVED_DIRS or _win_file_key(pp.name) in _RESERVED_FILES:
        raise ValueError(f"更新対象にできないパス: {p}")
    return str(pp)


def _same_existing(a: Path, b: Path) -> bool:
    """両方あるときだけ同一ファイルか比べる。無いファイルは別物として扱う。"""
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError as e:
        raise ValueError(f"更新対象のパスを確認できません: {a}") from e


def _reject_reserved_alias(d: Path, rel: str) -> None:
    """8.3 短縮名は末尾の '.' を落とした名前比較をすり抜ける。

    CONFIG~1.JSO のように、番号は固定ではない。GetLongPathName は末尾の '.' を
    残すので使わず、各要素の末尾の '.' と空白を落としたパスで既存ファイルと比べる。
    まだ無いファイルで、祖先も予約ディレクトリでなければ、これまでどおり置く。
    """
    parts = []
    for part in PurePosixPath(rel).parts:
        name = part.rstrip(". ")
        if not name or name in (".", ".."):
            raise ValueError(f"更新対象にできないパス: {rel}")
        parts.append(name)
    reserved = [d / name for name in (_RESERVED_DIRS + _RESERVED_FILES)]
    cur = d
    for name in parts:
        cur = cur / name
        for target in reserved:
            if _same_existing(cur, target):
                raise ValueError(f"更新対象にできないパス: {rel}")
        # ここがまだ無いなら、その先も予約ファイルにはなっていない
        if not cur.exists():
            return


def check(source: str | None = None, d: Path | None = None, timeout: float = 20) -> dict | None:
    """新しいバージョンがあれば manifest を返す。なければ None。"""
    d = d or app_dir()
    source = source if source is not None else load_config(d).get("update_source")
    if not source:
        return None
    m = json.loads(_fetch(source, "manifest.json", timeout).decode("utf-8"))
    if not isinstance(m, dict) or not isinstance(m.get("files"), list):
        raise ValueError("manifest.json の形式が正しくありません")
    _check_version(m.get("version"))
    if vtuple(m["version"]) > vtuple(local_version(d)):
        return m
    return None


def apply(manifest: dict, source: str | None = None, d: Path | None = None, timeout: float = 60) -> str:
    """manifest のファイルをダウンロード・検証して置き換える。新しいバージョン文字列を返す。"""
    d = d or app_dir()
    source = source if source is not None else load_config(d).get("update_source")
    _check_version(manifest.get("version"))
    stage = d / ".update-staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    try:
        files = []
        seen = set()
        for f in manifest["files"]:
            rel = _safe_rel(f["path"])
            # 短縮名の衝突はインストール先に実ファイルがあるときだけ分かる。置く前に見る。
            _reject_reserved_alias(d, rel)
            if rel.lower() in seen:
                raise ValueError(f"manifest に同じパスが2回あります: {rel}")
            seen.add(rel.lower())
            asset = _safe_rel(f["asset"]) if f.get("asset") else "files/" + rel  # asset も更新元の中だけ
            sha = str(f.get("sha256") or "").lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise ValueError(f"チェックサムがありません: {rel}")
            data = _fetch(source, asset, timeout)
            h = hashlib.sha256(data).hexdigest()
            if h != sha:
                raise ValueError(f"チェックサム不一致: {rel}（ダウンロードが壊れているか、改ざんされています）")
            dst = stage / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            files.append(rel)
        # ここまで全部検証できたら置き換える
        old = local_version(d)
        try:
            _check_version(old)
        except ValueError:
            old = "0.0.0"
        backup_root = d / ".backup"
        backup = backup_root / old
        # 短縮名はディレクトリができたあとでないと samefile で見えない。
        # ここで拒否したら、今作った空のバックアップだけ戻す。置き換えは始めない。
        new_backup_root = not backup_root.exists()
        new_version_dir = not backup.exists()
        backup.mkdir(parents=True, exist_ok=True)
        try:
            for rel in files:
                _reject_reserved_alias(d, rel)
        except ValueError:
            if new_backup_root:
                shutil.rmtree(backup_root, ignore_errors=True)
            elif new_version_dir:
                shutil.rmtree(backup, ignore_errors=True)
            raise
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
    import winreg
    # "FixPoint_is1" は setup.exe (Inno Setup) で入れたとき、"fixpoint" は以前の .cmd 版で入れたとき
    for name in ("FixPoint_is1", "fixpoint"):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + name, 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
            winreg.CloseKey(k)
        except OSError:
            pass


def configure(update_source: str = "", d: Path | None = None):
    """インストーラーから呼ばれる。今の設定は残して、更新元だけ最新にする。"""
    d = d or app_dir()
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads((d / CONFIG_JSON).read_text(encoding="utf-8")))
    except Exception:
        pass
    if update_source:
        cfg["update_source"] = update_source
    save_config(cfg, d)


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
    elif cmd == "configure":
        configure(sys.argv[2] if len(sys.argv) > 2 else "")
