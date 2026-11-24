#!/usr/bin/env python3
"""fixpoint-lock - 複数のAIエージェントが1つのプロジェクトを安全に共同編集するための調整ツール.

依存ライブラリなし (Python 3.9+ 標準ライブラリのみ)。

使い方:
  python fixpoint_lock.py serve              MCPサーバー (stdio) として起動
  python fixpoint_lock.py status             現在の状況を表示 (人間用)
  python fixpoint_lock.py hook               Claude Code の PreToolUse フック (編集ガード)
  python fixpoint_lock.py release            このエージェントのロックをすべて解放 (SessionEnd用)
  python fixpoint_lock.py unlock-all         全エージェントのロックを強制解放 (人間用・緊急時)

環境変数:
  FIXPOINT_AGENT    エージェント名 (例: claude-1, codex)。必須推奨。
  FIXPOINT_PROJECT  プロジェクトのルート。省略時はカレントディレクトリ。
  FIXPOINT_TTL      ロックの既定有効期限(分)。既定 30。
  FIXPOINT_MODE     フックのモード: auto(既定) / strict / warn
                       auto   = 空いていれば編集時に自動ロック、他者のロック中ならブロック
                       strict = 事前に自分でロックしていないファイルの編集はブロック
                       warn   = ブロックせず警告のみ
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

def project_root() -> Path:
    return Path(os.environ.get("FIXPOINT_PROJECT") or os.getcwd()).resolve()


def agent_name(default: str | None = None) -> str:
    return os.environ.get("FIXPOINT_AGENT") or default or "anonymous"


def default_ttl_min() -> float:
    try:
        return float(os.environ.get("FIXPOINT_TTL", "30"))
    except ValueError:
        return 30.0


def now() -> float:
    return time.time()


def fmt_ago(ts: float) -> str:
    d = int(now() - ts)
    if d < 60:
        return f"{d}秒前"
    if d < 3600:
        return f"{d // 60}分前"
    return f"{d // 3600}時間前"


def fmt_left(ts: float) -> str:
    d = int(ts - now())
    if d <= 0:
        return "期限切れ"
    if d < 60:
        return f"残り{d}秒"
    return f"残り{d // 60}分"


# ---------------------------------------------------------------------------
# パス正規化
# ---------------------------------------------------------------------------

def norm_path(p: str, root: Path | None = None) -> str:
    """プロジェクトルートからの相対パス (スラッシュ区切り) に正規化。
    ディレクトリは末尾に '/' を付ける。Windows では大文字小文字を区別しない。"""
    root = root or project_root()
    raw = p.strip().replace("\\", "/")
    is_dir_hint = raw.endswith("/")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    path = Path(os.path.normpath(str(path)))
    try:
        rel = path.relative_to(root)
    except ValueError:
        # Windows: ドライブ文字の大小差などを吸収
        try:
            rel = Path(os.path.relpath(path, root))
        except ValueError:
            raise ValueError(f"プロジェクト外のパスです: {p}")
    s = rel.as_posix()
    if s.startswith(".."):
        raise ValueError(f"プロジェクト外のパスです: {p}")
    if s == ".":
        s = ""
    if is_dir_hint or path.is_dir():
        s = s.rstrip("/") + "/" if s else "/"
    if os.name == "nt":
        s = s.lower()
    return s


def overlaps(a: str, b: str) -> bool:
    """a と b が同じファイル、または一方が他方を含むディレクトリなら True。"""
    if a == b:
        return True
    if a == "/" or b == "/":
        return True
    if a.endswith("/") and b.startswith(a):
        return True
    if b.endswith("/") and a.startswith(b):
        return True
    return False


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    status TEXT DEFAULT '',
    last_read_msg INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leases (
    path TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    reason TEXT DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT DEFAULT '',
    files TEXT DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',   -- open / claimed / done
    owner TEXT,
    created_by TEXT,
    note TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender TEXT NOT NULL,
    recipient TEXT,                        -- NULL = 全員宛て
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT,
    event TEXT
);
"""


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or project_root()
        d = self.root / ".fixpoint"
        d.mkdir(exist_ok=True)
        gi = d / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")
        self.db = sqlite3.connect(str(d / "coord.db"), timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(SCHEMA)

    # すべての書き込みは BEGIN IMMEDIATE で直列化 = 取り合いが原理的に起きない
    def tx(self):
        store = self

        class _Tx:
            def __enter__(self_inner):
                store.db.execute("BEGIN IMMEDIATE")
                return store.db

            def __exit__(self_inner, et, ev, tb):
                store.db.execute("COMMIT" if et is None else "ROLLBACK")
                return False

        return _Tx()

    def _purge(self, db):
        db.execute("DELETE FROM leases WHERE expires_at <= ?", (now(),))

    def _touch(self, db, agent: str):
        t = now()
        db.execute(
            "INSERT INTO agents(name, first_seen, last_seen) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen",
            (agent, t, t),
        )

    def _log(self, db, agent: str, event: str):
        db.execute("INSERT INTO log(ts, agent, event) VALUES(?,?,?)", (now(), agent, event))

    # ---- ロック -----------------------------------------------------------

    def conflicts(self, db, agent: str, paths: list[str]) -> list[dict]:
        rows = db.execute("SELECT * FROM leases WHERE agent != ?", (agent,)).fetchall()
        out = []
        for p in paths:
            for r in rows:
                if overlaps(p, r["path"]):
                    out.append({"requested": p, "held": r["path"], "by": r["agent"],
                                "reason": r["reason"], "expires_at": r["expires_at"]})
        return out

    def lock(self, agent: str, paths: list[str], reason: str = "", ttl_min: float | None = None):
        ttl = (ttl_min or default_ttl_min()) * 60
        normed = [norm_path(p, self.root) for p in paths]
        with self.tx() as db:
            self._purge(db)
            self._touch(db, agent)
            c = self.conflicts(db, agent, normed)
            if c:
                return {"ok": False, "conflicts": c}
            t = now()
            for p in normed:
                db.execute(
                    "INSERT INTO leases(path, agent, reason, created_at, expires_at) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(path) DO UPDATE SET agent=excluded.agent, reason=excluded.reason, "
                    "expires_at=excluded.expires_at",
                    (p, agent, reason, t, t + ttl),
                )
            self._log(db, agent, f"lock {normed} ({reason})")
            return {"ok": True, "locked": normed, "expires_at": t + ttl}

    def unlock(self, agent: str, paths: list[str] | None = None, force: bool = False):
        with self.tx() as db:
            self._touch(db, agent)
            if not paths:
                if force:
                    n = db.execute("DELETE FROM leases").rowcount
                else:
                    n = db.execute("DELETE FROM leases WHERE agent=?", (agent,)).rowcount
                self._log(db, agent, f"unlock all ({n})" + (" FORCE" if force else ""))
                return {"ok": True, "released": n}
            normed = [norm_path(p, self.root) for p in paths]
            n = 0
            for p in normed:
                n += db.execute("DELETE FROM leases WHERE path=? AND agent=?", (p, agent)).rowcount
            self._log(db, agent, f"unlock {normed}")
            return {"ok": True, "released": n}

    def check(self, agent: str, paths: list[str]):
        normed = [norm_path(p, self.root) for p in paths]
        with self.tx() as db:
            self._purge(db)
            rows = db.execute("SELECT * FROM leases").fetchall()
        res = []
        for p in normed:
            holders = [dict(r) for r in rows if overlaps(p, r["path"])]
            mine = [h for h in holders if h["agent"] == agent]
            others = [h for h in holders if h["agent"] != agent]
            res.append({"path": p,
                        "state": "locked_by_other" if others else ("locked_by_you" if mine else "free"),
                        "holders": [{"path": h["path"], "agent": h["agent"], "reason": h["reason"]} for h in holders]})
        return res

    def guard_edit(self, agent: str, path: str, mode: str = "auto"):
        """フック用: 編集してよいか判定。auto モードでは空いていれば自動ロック。
        戻り値: (allowed: bool, message: str)"""
        try:
            p = norm_path(path, self.root)
        except ValueError:
            return True, ""  # プロジェクト外は対象外
        if p.startswith(".fixpoint/"):
            return True, ""
        ttl = default_ttl_min() * 60
        with self.tx() as db:
            self._purge(db)
            self._touch(db, agent)
            c = self.conflicts(db, agent, [p])
            if c:
                h = c[0]
                return False, (
                    f"[fixpoint-lock] {p} は {h['by']} がロック中です"
                    f"（理由: {h['reason'] or 'なし'}、{fmt_left(h['expires_at'])}）。"
                    f"別の作業をするか、send_message で {h['by']} に連絡して解放を待ってください。"
                )
            own = db.execute("SELECT path FROM leases WHERE agent=?", (agent,)).fetchall()
            covered = [r["path"] for r in own if overlaps(p, r["path"]) and (r["path"] == p or r["path"].endswith("/") and p.startswith(r["path"]))]
            if covered:
                # 自分のロックを延長
                for cp in covered:
                    db.execute("UPDATE leases SET expires_at=? WHERE path=? AND agent=?", (now() + ttl, cp, agent))
                return True, ""
            if mode == "strict":
                return False, (f"[fixpoint-lock] strictモード: {p} を編集する前に lock_files でロックを取得してください。")
            if mode == "auto":
                t = now()
                db.execute(
                    "INSERT INTO leases(path, agent, reason, created_at, expires_at) VALUES(?,?,?,?,?)",
                    (p, agent, "auto (編集時に自動取得)", t, t + ttl),
                )
                self._log(db, agent, f"auto-lock {p}")
            return True, ""

    # ---- タスク -----------------------------------------------------------

    def task_add(self, agent: str, title: str, detail: str = "", files: list[str] | None = None):
        files = [norm_path(f, self.root) for f in (files or [])]
        with self.tx() as db:
            self._touch(db, agent)
            t = now()
            cur = db.execute(
                "INSERT INTO tasks(title, detail, files, status, created_by, created_at, updated_at) "
                "VALUES(?,?,?,'open',?,?,?)",
                (title, detail, json.dumps(files, ensure_ascii=False), agent, t, t),
            )
            self._log(db, agent, f"task_add #{cur.lastrowid} {title}")
            return {"ok": True, "id": cur.lastrowid}

    def task_claim(self, agent: str, task_id: int, lock_files: bool = True):
        with self.tx() as db:
            self._purge(db)
            self._touch(db, agent)
            r = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not r:
                return {"ok": False, "error": f"タスク #{task_id} は存在しません"}
            if r["status"] == "done":
                return {"ok": False, "error": f"タスク #{task_id} は完了済みです"}
            if r["status"] == "claimed" and r["owner"] != agent:
                return {"ok": False, "error": f"タスク #{task_id} は {r['owner']} が担当中です"}
            files = json.loads(r["files"] or "[]")
            if lock_files and files:
                c = self.conflicts(db, agent, files)
                if c:
                    return {"ok": False, "error": "関連ファイルが他エージェントにロックされています", "conflicts": c}
                t = now()
                for p in files:
                    db.execute(
                        "INSERT INTO leases(path, agent, reason, created_at, expires_at) VALUES(?,?,?,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET agent=excluded.agent, reason=excluded.reason, expires_at=excluded.expires_at",
                        (p, agent, f"task #{task_id}", t, t + default_ttl_min() * 60),
                    )
            db.execute("UPDATE tasks SET status='claimed', owner=?, updated_at=? WHERE id=?", (agent, now(), task_id))
            db.execute("UPDATE agents SET status=? WHERE name=?", (f"task #{task_id}: {r['title']}", agent))
            self._log(db, agent, f"task_claim #{task_id}")
            return {"ok": True, "task": {**dict(r), "status": "claimed", "owner": agent}, "locked": files}

    def task_done(self, agent: str, task_id: int, note: str = "", release: bool = True):
        with self.tx() as db:
            self._touch(db, agent)
            r = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not r:
                return {"ok": False, "error": f"タスク #{task_id} は存在しません"}
            if r["owner"] not in (None, agent):
                return {"ok": False, "error": f"タスク #{task_id} は {r['owner']} の担当です"}
            db.execute("UPDATE tasks SET status='done', owner=?, note=?, updated_at=? WHERE id=?", (agent, note, now(), task_id))
            released = 0
            if release:
                for p in json.loads(r["files"] or "[]"):
                    released += db.execute("DELETE FROM leases WHERE path=? AND agent=?", (p, agent)).rowcount
            db.execute("UPDATE agents SET status='' WHERE name=?", (agent,))
            self._log(db, agent, f"task_done #{task_id} {note}")
            return {"ok": True, "released": released}

    def task_release(self, agent: str, task_id: int, note: str = ""):
        with self.tx() as db:
            r = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not r or r["owner"] != agent or r["status"] != "claimed":
                return {"ok": False, "error": "自分が担当中のタスクではありません"}
            db.execute("UPDATE tasks SET status='open', owner=NULL, note=?, updated_at=? WHERE id=?", (note, now(), task_id))
            for p in json.loads(r["files"] or "[]"):
                db.execute("DELETE FROM leases WHERE path=? AND agent=?", (p, agent))
            self._log(db, agent, f"task_release #{task_id}")
            return {"ok": True}

    def task_list(self, status: str | None = None):
        q = "SELECT * FROM tasks"
        args: tuple = ()
        if status and status != "all":
            q += " WHERE status=?"
            args = (status,)
        q += " ORDER BY id"
        rows = self.db.execute(q, args).fetchall()
        return [dict(r) | {"files": json.loads(r["files"] or "[]")} for r in rows]

    # ---- メッセージ -------------------------------------------------------

    def send(self, agent: str, body: str, to: str | None = None):
        with self.tx() as db:
            self._touch(db, agent)
            cur = db.execute("INSERT INTO messages(sender, recipient, body, created_at) VALUES(?,?,?,?)",
                             (agent, to or None, body, now()))
            return {"ok": True, "id": cur.lastrowid}

    def unread(self, agent: str, mark: bool = True):
        with self.tx() as db:
            self._touch(db, agent)
            last = db.execute("SELECT last_read_msg FROM agents WHERE name=?", (agent,)).fetchone()[0] or 0
            rows = db.execute(
                "SELECT * FROM messages WHERE id>? AND sender!=? AND (recipient IS NULL OR recipient=?) ORDER BY id",
                (last, agent, agent),
            ).fetchall()
            if mark and rows:
                db.execute("UPDATE agents SET last_read_msg=? WHERE name=?", (rows[-1]["id"], agent))
            return [dict(r) for r in rows]

    # ---- 全体状況 ---------------------------------------------------------

    def status(self, agent: str | None = None) -> str:
        with self.tx() as db:
            self._purge(db)
            if agent:
                self._touch(db, agent)
            agents = db.execute("SELECT * FROM agents ORDER BY last_seen DESC").fetchall()
            leases = db.execute("SELECT * FROM leases ORDER BY agent, path").fetchall()
            tasks = db.execute("SELECT * FROM tasks WHERE status!='done' ORDER BY id").fetchall()
            unread = 0
            if agent:
                last = db.execute("SELECT last_read_msg FROM agents WHERE name=?", (agent,)).fetchone()[0] or 0
                unread = db.execute(
                    "SELECT COUNT(*) FROM messages WHERE id>? AND sender!=? AND (recipient IS NULL OR recipient=?)",
                    (last, agent, agent)).fetchone()[0]
        L = [f"# fixpoint-lock — {self.root.name}"]
        if agent:
            L.append(f"あなた: {agent}" + (f"  /  未読メッセージ {unread}件 → read_messages で確認" if unread else ""))
        L.append("\n## エージェント")
        for a in agents:
            active = now() - a["last_seen"] < 600
            mark = "●" if active else "○"
            L.append(f"- {mark} {a['name']}（最終活動 {fmt_ago(a['last_seen'])}）{(' — ' + a['status']) if a['status'] else ''}")
        if not agents:
            L.append("- (なし)")
        L.append("\n## ロック中のファイル")
        for r in leases:
            L.append(f"- {r['path'] or '/'} … {r['agent']}（{r['reason'] or '理由なし'}、{fmt_left(r['expires_at'])}）")
        if not leases:
            L.append("- (なし)")
        L.append("\n## 未完了タスク")
        for t in tasks:
            who = f"担当: {t['owner']}" if t["owner"] else "未着手"
            files = json.loads(t["files"] or "[]")
            L.append(f"- #{t['id']} [{t['status']}] {t['title']}（{who}）" + (f" files={files}" if files else ""))
        if not tasks:
            L.append("- (なし)")
        return "\n".join(L)


# ---------------------------------------------------------------------------
# MCP サーバー (JSON-RPC 2.0 over stdio, 改行区切り)
# ---------------------------------------------------------------------------

INSTRUCTIONS = """このプロジェクトは複数のAIエージェントが同時に作業しています。fixpoint-lock で調整してください。
1. 作業開始時に status を呼び、他エージェントの作業中ファイル・タスク・未読メッセージを確認する。
2. ファイルを編集する前に lock_files でロックを取る。ロックが取れなければそのファイルは編集しない（別の作業をするか send_message で相談）。
3. 作業はタスク単位で: task_list → task_claim → 作業 → task_done。同じタスクを二重に取らない。
4. 作業が終わったら unlock_files でロックを解放する。長い作業ではロックを取り直して延長する。
5. ときどき read_messages を確認する。他エージェントに影響する変更 (API変更・共通ファイル) は send_message で知らせる。"""

STR_ARR = {"type": "array", "items": {"type": "string"}}

TOOLS = [
    {"name": "status", "description": "全体の状況（参加エージェント・ロック中ファイル・未完了タスク・未読数）を表示。作業開始時にまず呼ぶ。",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "lock_files", "description": "ファイル/ディレクトリ(末尾 / )のロックを取得。全部取れるか、1つも取れないか(all-or-nothing)。他者と衝突したら誰が持っているかを返す。自分のロックを再取得すると期限延長。",
     "inputSchema": {"type": "object", "properties": {
         "paths": STR_ARR, "reason": {"type": "string", "description": "何のために編集するか"},
         "ttl_minutes": {"type": "number", "description": "有効期限(分)。既定30"}}, "required": ["paths"]}},
    {"name": "unlock_files", "description": "自分のロックを解放。paths 省略で自分のロックをすべて解放。",
     "inputSchema": {"type": "object", "properties": {"paths": STR_ARR}}},
    {"name": "check_files", "description": "ファイルが空いているか、誰がロックしているかを確認（ロックはしない）。",
     "inputSchema": {"type": "object", "properties": {"paths": STR_ARR}, "required": ["paths"]}},
    {"name": "task_add", "description": "タスクを追加。files に関連ファイルを入れておくと、claim 時に自動でロックされる。",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "detail": {"type": "string"}, "files": STR_ARR}, "required": ["title"]}},
    {"name": "task_list", "description": "タスク一覧。status: open / claimed / done / all（既定: open と claimed）。",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string"}}}},
    {"name": "task_claim", "description": "タスクを担当として取得（原子的。他者が先に取っていたら失敗）。関連ファイルも同時にロック。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}},
    {"name": "task_done", "description": "タスクを完了にし、関連ファイルのロックを解放。note に結果の要約を残す。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["id"]}},
    {"name": "task_release", "description": "担当中のタスクを手放して未着手に戻す（諦める・他者に譲る）。",
     "inputSchema": {"type": "object", "properties": {"id": {"type": "integer"}, "note": {"type": "string"}}, "required": ["id"]}},
    {"name": "send_message", "description": "他エージェントにメッセージ。to 省略で全員宛て。",
     "inputSchema": {"type": "object", "properties": {"body": {"type": "string"}, "to": {"type": "string"}}, "required": ["body"]}},
    {"name": "read_messages", "description": "自分宛て・全員宛ての未読メッセージを読む（既読にする）。",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _fmt_conflicts(c: list[dict]) -> str:
    return "\n".join(
        f"- {x['requested']} ← {x['by']} が {x['held']} をロック中（{x['reason'] or '理由なし'}、{fmt_left(x['expires_at'])}）"
        for x in c)


def call_tool(store: Store, agent: str, name: str, a: dict) -> tuple[str, bool]:
    """戻り値: (テキスト, is_error)"""
    if name == "status":
        return store.status(agent), False
    if name == "lock_files":
        r = store.lock(agent, a.get("paths") or [], a.get("reason", ""), a.get("ttl_minutes"))
        if r["ok"]:
            return f"ロック取得: {', '.join(r['locked'])}（{fmt_left(r['expires_at'])}）", False
        return "ロック失敗（何も取得していません）。衝突:\n" + _fmt_conflicts(r["conflicts"]), True
    if name == "unlock_files":
        r = store.unlock(agent, a.get("paths"))
        return f"{r['released']}件のロックを解放しました", False
    if name == "check_files":
        res = store.check(agent, a.get("paths") or [])
        lines = []
        for x in res:
            label = {"free": "空き", "locked_by_you": "あなたがロック中", "locked_by_other": "他者がロック中"}[x["state"]]
            who = ", ".join(f"{h['agent']}({h['path']})" for h in x["holders"])
            lines.append(f"- {x['path']}: {label}" + (f" — {who}" if who else ""))
        return "\n".join(lines), False
    if name == "task_add":
        r = store.task_add(agent, a["title"], a.get("detail", ""), a.get("files"))
        return f"タスク #{r['id']} を追加しました", False
    if name == "task_list":
        st = a.get("status")
        rows = store.task_list(None if st in (None, "", "all") else st)
        if st in (None, ""):
            rows = [t for t in rows if t["status"] != "done"]
        if not rows:
            return "タスクはありません", False
        return "\n".join(
            f"- #{t['id']} [{t['status']}] {t['title']}" + (f"（担当: {t['owner']}）" if t["owner"] else "")
            + (f"\n    {t['detail']}" if t["detail"] else "")
            + (f"\n    files: {', '.join(t['files'])}" if t["files"] else "")
            + (f"\n    note: {t['note']}" if t["note"] else "")
            for t in rows), False
    if name == "task_claim":
        r = store.task_claim(agent, int(a["id"]))
        if r["ok"]:
            t = r["task"]
            msg = f"タスク #{t['id']}「{t['title']}」を担当しました。"
            if r["locked"]:
                msg += f"\nロック: {', '.join(r['locked'])}"
            if t["detail"]:
                msg += f"\n詳細: {t['detail']}"
            return msg, False
        return r["error"] + ("\n" + _fmt_conflicts(r["conflicts"]) if r.get("conflicts") else ""), True
    if name == "task_done":
        r = store.task_done(agent, int(a["id"]), a.get("note", ""))
        return (f"タスク #{a['id']} 完了。{r['released']}件のロックを解放", False) if r["ok"] else (r["error"], True)
    if name == "task_release":
        r = store.task_release(agent, int(a["id"]), a.get("note", ""))
        return ("タスクを未着手に戻しました", False) if r["ok"] else (r["error"], True)
    if name == "send_message":
        store.send(agent, a["body"], a.get("to"))
        return "送信しました", False
    if name == "read_messages":
        msgs = store.unread(agent)
        if not msgs:
            return "未読メッセージはありません", False
        return "\n".join(
            f"[{fmt_ago(m['created_at'])}] {m['sender']}{' → あなた' if m['recipient'] else ' → 全員'}: {m['body']}"
            for m in msgs), False
    return f"未知のツール: {name}", True


def serve():
    store = Store()
    agent = agent_name()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer

    def send(obj):
        stdout.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")
        stdout.flush()

    for raw in stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if mid is None:  # notification
            continue
        try:
            if method == "initialize":
                result = {
                    "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fixpoint-lock", "version": VERSION},
                    "instructions": INSTRUCTIONS + f"\n\nあなたのエージェント名: {agent}",
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                try:
                    text, err = call_tool(store, agent, params.get("name", ""), params.get("arguments") or {})
                except Exception as e:  # ツール内エラーはツール結果として返す
                    text, err = f"エラー: {e}", True
                result = {"content": [{"type": "text", "text": text}], "isError": err}
            else:
                send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
                continue
            send({"jsonrpc": "2.0", "id": mid, "result": result})
        except Exception as e:
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}})


# ---------------------------------------------------------------------------
# Claude Code フック
# ---------------------------------------------------------------------------

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def hook():
    """PreToolUse: 編集対象が他者にロックされていればブロック (exit 2)。"""
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except Exception:
        return 0
    if data.get("tool_name") not in EDIT_TOOLS:
        return 0
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    if not path:
        return 0
    if data.get("cwd") and not os.environ.get("FIXPOINT_PROJECT"):
        os.environ["FIXPOINT_PROJECT"] = data["cwd"]
    sid = (data.get("session_id") or "")[:6]
    agent = agent_name(f"claude-{sid}" if sid else None)
    mode = os.environ.get("FIXPOINT_MODE", "auto")
    ok, message = Store().guard_edit(agent, path, "auto" if mode == "warn" else mode)
    if ok:
        return 0
    if mode == "warn":
        print(message, file=sys.stderr)
        return 0
    sys.stderr.write(message + "\n")
    sys.stderr.flush()
    return 2


def release_cmd():
    try:
        data = json.loads(sys.stdin.read() or "{}") if not sys.stdin.isatty() else {}
    except Exception:
        data = {}
    if data.get("cwd") and not os.environ.get("FIXPOINT_PROJECT"):
        os.environ["FIXPOINT_PROJECT"] = data["cwd"]
    sid = (data.get("session_id") or "")[:6]
    agent = agent_name(f"claude-{sid}" if sid else None)
    r = Store().unlock(agent)
    print(f"{agent}: {r['released']}件のロックを解放", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cmd = argv[1] if len(argv) > 1 else "help"
    if cmd == "serve":
        serve()
        return 0
    if cmd == "status":
        print(Store().status())
        return 0
    if cmd == "hook":
        return hook()
    if cmd == "release":
        return release_cmd()
    if cmd == "unlock-all":
        r = Store().unlock("human", force=True)
        print(f"{r['released']}件のロックを強制解放しました")
        return 0
    if cmd == "log":
        st = Store()
        for r in st.db.execute("SELECT * FROM log ORDER BY id DESC LIMIT 40").fetchall()[::-1]:
            print(f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} {r['agent']}: {r['event']}")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
