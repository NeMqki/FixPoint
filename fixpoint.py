#!/usr/bin/env python3
"""fixpoint - 複数のAI (Claude Code / Codex CLI / Gemini CLI) がひとつのプロジェクトで
「お互いがこれでいいんじゃない？」と言うまで作業を続けるためのオーケストレーター + Web UI。

  python fixpoint.py --project F:/your-project      → http://127.0.0.1:8765 を開く

依存なし (Python 3.9+ 標準ライブラリのみ)。各AIのログインはそれぞれのCLIで済ませておく。

進め方 (ラウンド制):
  1. 実装担当 (1人だけが書き手) がゴールに向けて作業 → git にチェックポイントを記録
  2. 全員が読み取り専用でレビューし、VERDICT: APPROVE / CHANGES を出す (並列)
  3. 全員 APPROVE なら合意成立で終了。そうでなければ CHANGES を出した人が次の実装担当になり、指摘を反映
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

VERSION = "0.3.0"
HERE = Path(__file__).resolve().parent
# コンソールなし (pythonw) で動かしても git 等の黒い窓が出ないように
NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}


def app_version() -> str:
    try:
        import updater
        v = updater.local_version(HERE)
        return VERSION if v == "0.0.0" else v
    except Exception:
        return VERSION


RUNNING_VERSION = app_version()  # このプロセスが読み込んだバージョン (更新後はディスク上の app.json と食い違う)

# ---------------------------------------------------------------------------
# CLI 検出
# ---------------------------------------------------------------------------

KINDS = {
    "claude": {"label": "Claude Code", "bin": "claude", "login": "ターミナルで `claude` を起動して /login"},
    "codex": {"label": "Codex CLI", "bin": "codex", "login": "`npm i -g @openai/codex` → `codex login`"},
    "gemini": {"label": "Gemini CLI", "bin": "gemini", "login": "`npm i -g @google/gemini-cli` → `gemini` を起動してログイン"},
    "grok": {"label": "Grok Build", "bin": "grok", "login": "ターミナルで `grok login`"},
    "mock": {"label": "Mock (テスト用)", "bin": None, "login": "ログイン不要"},
}


def detect_clis() -> dict:
    out = {}
    for kind, meta in KINDS.items():
        if meta["bin"] is None:
            out[kind] = {"label": meta["label"], "installed": True, "version": "built-in", "login": meta["login"]}
            continue
        path = shutil.which(meta["bin"])
        ver = ""
        if path:
            try:
                r = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=20,
                                   encoding="utf-8", errors="replace", env=child_env(), **NOWIN)
                ver = (r.stdout or r.stderr).strip().splitlines()[0] if (r.stdout or r.stderr).strip() else ""
            except Exception as e:
                ver = f"(バージョン取得失敗: {e})"
        out[kind] = {"label": meta["label"], "installed": bool(path), "path": path, "version": ver, "login": meta["login"]}
    return out


def child_env() -> dict:
    env = dict(os.environ)
    # Claude Code の中から起動された場合でも、子の claude が独立したセッションとして動くように
    for k in list(env):
        if k == "CLAUDECODE" or k.startswith("CLAUDE_CODE_") or k.startswith("CLAUDE_AGENT_SDK"):
            env.pop(k, None)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# ---------------------------------------------------------------------------
# エージェント実行アダプタ
# ---------------------------------------------------------------------------

class Stopped(Exception):
    pass


class AgentRunner:
    """1回のターン (プロンプト → 応答) を実行する。各CLIの出力をテキスト/ツール呼び出しに変換してコールバック。"""

    def __init__(self, kind: str, model: str = "", permission: str = "acceptEdits"):
        self.kind = kind
        self.model = model.strip()
        self.permission = permission
        self.proc: subprocess.Popen | None = None
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True, **NOWIN)
                else:
                    os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                p.kill()

    # ---- コマンド組み立て ----
    def build_cmd(self, writer: bool) -> list[str]:
        k = self.kind
        if k == "claude":
            mode = "bypassPermissions" if self.permission == "bypassPermissions" else "acceptEdits"
            cmd = [shutil.which("claude") or "claude", "-p", "--output-format", "stream-json", "--verbose",
                   "--permission-mode", mode if writer else "default"]
            if self.model:
                cmd += ["--model", self.model]
            run_cmds = self.permission in ("editsAndCommands", "bypassPermissions")
            if writer and self.permission == "editsAndCommands":
                cmd += ["--allowedTools", "Bash,PowerShell"]
            if not writer:
                allowed = "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*),Bash(git status:*)"
                if run_cmds:
                    allowed = "Read,Grep,Glob,Bash,PowerShell"
                cmd += ["--disallowedTools", "Edit,Write,MultiEdit,NotebookEdit", "--allowedTools", allowed]
            return cmd
        if k == "codex":
            cmd = [shutil.which("codex") or "codex", "exec", "--json", "--skip-git-repo-check"]
            cmd += (["--full-auto"] if writer else ["--sandbox", "read-only"])
            if self.permission == "bypassPermissions" and writer:
                cmd += ["--dangerously-bypass-approvals-and-sandbox"]
            if self.model:
                cmd += ["-m", self.model]
            cmd += ["-"]  # プロンプトは stdin から
            return cmd
        if k == "grok":
            cmd = [shutil.which("grok") or "grok", "--output-format", "streaming-json", "--no-memory",
                   "--no-subagents"]
            edit_tools = "search_replace,write"
            if writer:
                if self.permission == "bypassPermissions":
                    cmd += ["--permission-mode", "bypassPermissions"]
                elif self.permission == "editsAndCommands":
                    cmd += ["--always-approve"]
                else:  # 編集だけ: コマンド実行ツール自体を外したうえで自動承認
                    # (grok のヘッドレスでは acceptEdits でも write が cancelled になるため)
                    cmd += ["--always-approve", "--disallowed-tools", "run_terminal_command"]
            else:
                if self.permission == "acceptEdits":
                    cmd += ["--permission-mode", "default", "--disallowed-tools", edit_tools + ",run_terminal_command"]
                else:  # 編集ツールは外したうえで、テスト等のコマンドは自動許可
                    cmd += ["--always-approve", "--disallowed-tools", edit_tools]
            if self.model:
                cmd += ["-m", self.model]
            return cmd  # プロンプトは run() で --prompt-file として付ける
        if k == "gemini":
            cmd = [shutil.which("gemini") or "gemini"]
            if writer:
                cmd += ["--yolo"]
            if self.model:
                cmd += ["-m", self.model]
            return cmd
        raise ValueError(f"unknown kind {k}")

    def run(self, prompt: str, cwd: str, writer: bool, on_text, on_tool) -> dict:
        if self.kind == "mock":
            return self._run_mock(prompt, cwd, writer, on_text, on_tool)
        cmd = self.build_cmd(writer)
        prompt_file = None
        if self.kind == "grok":
            pdir = Path(cwd) / ".fixpoint" / "team" / "prompts"
            pdir.mkdir(parents=True, exist_ok=True)
            prompt_file = pdir / f"{uuid.uuid4().hex}.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            cmd += ["--cwd", cwd, "--prompt-file", str(prompt_file)]
            prompt = ""
        kwargs = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        else:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, env=child_env(), **kwargs)
        except FileNotFoundError:
            raise RuntimeError(f"{KINDS[self.kind]['label']} が見つかりません。インストールしてください: {KINDS[self.kind]['login']}")
        err_buf: list[str] = []

        def read_err():
            for line in self.proc.stderr:
                err_buf.append(line.decode("utf-8", "replace"))
        te = threading.Thread(target=read_err, daemon=True)
        te.start()
        try:
            self.proc.stdin.write(prompt.encode("utf-8"))
            self.proc.stdin.close()
        except Exception:
            pass

        final_text = ""
        texts: list[str] = []
        meta: dict = {}
        parse = {"claude": self._parse_claude, "codex": self._parse_codex, "grok": self._parse_grok,
                 "gemini": None}[self.kind]
        for raw in self.proc.stdout:
            if self.cancelled:
                break
            line = raw.decode("utf-8", "replace")
            if parse is None:
                texts.append(line)
                on_text(line)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                texts.append(line + "\n")
                on_text(line + "\n")
                continue
            r = parse(ev, on_text, on_tool, texts, meta)
            if r is not None:
                final_text = r
        rc = self.proc.wait()
        te.join(timeout=2)
        if prompt_file:
            try:
                prompt_file.unlink()
            except OSError:
                pass
        if self.cancelled:
            raise Stopped()
        if not final_text:
            final_text = "".join(texts).strip()
        if rc != 0 and not final_text:
            raise RuntimeError(f"{self.kind} が終了コード {rc} で失敗:\n" + "".join(err_buf)[-2000:])
        meta["exit_code"] = rc
        if meta.get("error"):
            final_text += f"\n\n（注意: このターンは途中で止まりました: {meta['error']}）"
        return {"text": final_text, "meta": meta}

    # ---- 出力パーサ ----
    @staticmethod
    def _parse_claude(ev, on_text, on_tool, texts, meta):
        t = ev.get("type")
        if t == "assistant":
            for c in (ev.get("message") or {}).get("content") or []:
                if c.get("type") == "text" and c.get("text"):
                    texts.append(c["text"] + "\n")
                    on_text(c["text"] + "\n")
                elif c.get("type") == "tool_use":
                    on_tool(c.get("name", "?"), _tool_detail(c.get("input") or {}))
        elif t == "result":
            meta["cost_usd"] = ev.get("total_cost_usd")
            meta["duration_ms"] = ev.get("duration_ms")
            meta["session_id"] = ev.get("session_id")
            if ev.get("is_error"):
                meta["error"] = ev.get("result") or ev.get("subtype")
            return ev.get("result") or ""
        return None

    @staticmethod
    def _parse_grok(ev, on_text, on_tool, texts, meta):
        # grok --output-format streaming-json: text はトークン単位の断片、tool_call、最後に end
        t = ev.get("type")
        if t == "text":
            d = ev.get("data") or ""
            texts.append(d)
            on_text(d)
        elif t == "tool_call":
            if texts and not texts[-1].endswith("\n"):
                texts.append("\n")
                on_text("\n")
            on_tool(ev.get("toolName") or ev.get("title") or "?", _tool_detail(ev.get("rawInput") or {}))
        elif t == "end":
            meta["session_id"] = ev.get("sessionId")
            meta["usage"] = ev.get("usage")
            if ev.get("stopReason") not in (None, "end_turn"):
                meta["error"] = ev.get("stopReason")
            return "".join(texts).strip()
        elif t == "error":
            meta["error"] = str(ev.get("message") or ev.get("data") or ev)
        return None

    @staticmethod
    def _parse_codex(ev, on_text, on_tool, texts, meta):
        # codex exec --json の形式はバージョンで変わるので防御的に解釈
        item = ev.get("item") or ev.get("msg") or ev
        it = item.get("type") or item.get("item_type") or ev.get("type")
        if it in ("agent_message", "assistant_message"):
            txt = item.get("text") or item.get("message") or ""
            if txt:
                texts.append(txt + "\n")
                on_text(txt + "\n")
                return txt if ev.get("type") in ("item.completed", None) else None
        elif it in ("command_execution", "exec_command_begin", "local_shell_call"):
            if ev.get("type") != "item.completed":
                on_tool("shell", str(item.get("command") or item.get("cmd") or "")[:200])
        elif it in ("file_change", "patch_apply_begin"):
            ch = item.get("changes") or item.get("files") or ""
            on_tool("edit", str(ch)[:200])
        elif ev.get("type") == "turn.completed":
            meta["usage"] = ev.get("usage")
        elif ev.get("type") in ("error", "turn.failed"):
            meta["error"] = str(ev.get("message") or ev.get("error"))
        return None

    # ---- テスト用モック ----
    def _run_mock(self, prompt, cwd, writer, on_text, on_tool):
        m = re.search(r"ラウンド (\d+)", prompt)
        rnd = int(m.group(1)) if m else 1
        name = re.search(r"「(.+?)」", prompt).group(1)
        delay = float(os.environ.get("FIXPOINT_MOCK_DELAY", "0.3"))
        for w in ["考え中…", "ファイルを確認…", "まとめ中…"]:
            if self.cancelled:
                raise Stopped()
            on_text(w + "\n")
            time.sleep(delay)
        if writer:
            f = Path(cwd) / "MOCK_NOTES.md"
            with f.open("a", encoding="utf-8") as fh:
                fh.write(f"- round {rnd}: {name} が追記\n")
            on_tool("Edit", "MOCK_NOTES.md")
            return {"text": f"MOCK_NOTES.md にラウンド{rnd}の行を追記しました。", "meta": {}}
        need = int(os.environ.get("FIXPOINT_MOCK_APPROVE_AT", "2"))
        if rnd >= need:
            return {"text": "ゴールは満たしていると思います。\nVERDICT: APPROVE", "meta": {}}
        return {"text": f"- まだ足りない ({name} より)\nVERDICT: CHANGES", "meta": {}}


def _tool_detail(inp: dict) -> str:
    for k in ("file_path", "target_file", "path", "command", "pattern", "target_directory", "notebook_path", "url", "description"):
        if inp.get(k):
            return str(inp[k])[:200]
    return json.dumps(inp, ensure_ascii=False)[:200]


VERDICT_RE = re.compile(r"VERDICT\s*[:：]\s*\**\s*(APPROVE|CHANGES)", re.I)


def parse_verdict(text: str) -> str:
    ms = VERDICT_RE.findall(text or "")
    return ms[-1].upper() if ms else "UNKNOWN"


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

def git(cwd, *args, check=False) -> str:
    r = subprocess.run(["git", "-c", "core.quotepath=false", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", **NOWIN)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def is_git_repo(cwd) -> bool:
    r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=cwd, capture_output=True, text=True, **NOWIN)
    return r.returncode == 0 and r.stdout.strip() == "true"


GIT_ID = ["-c", "user.name=fixpoint", "-c", "user.email=fixpoint@localhost"]


def git_checkpoint(cwd, message) -> str | None:
    git(cwd, "add", "-A")
    if not git(cwd, "status", "--porcelain").strip():
        return None
    r = subprocess.run(["git", *GIT_ID, "commit", "-q", "--no-verify", "-m", message], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", **NOWIN)
    if r.returncode != 0:
        raise RuntimeError("git commit 失敗: " + r.stderr.strip())
    return git(cwd, "rev-parse", "HEAD").strip()


def head(cwd) -> str | None:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, **NOWIN)
    return r.stdout.strip() if r.returncode == 0 else None


# ---------------------------------------------------------------------------
# セッション (合意ループ)
# ---------------------------------------------------------------------------

def clip(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n // 2] + f"\n…（{len(s) - n}文字省略）…\n" + s[-n // 2:]


class Session:
    def __init__(self, project: str, goal: str, agents: list[dict], max_rounds: int = 6,
                 permission: str = "acceptEdits", use_git: bool = True):
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.project = str(Path(project).resolve())
        self.goal = goal
        self.agents = agents  # [{id, name, kind, model, role}]
        self.max_rounds = max_rounds
        self.permission = permission
        self.use_git = use_git
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.status = "idle"  # idle / running / consensus / stopped / max_rounds / error
        self.round = 0
        self.notes: list[str] = []
        self.history: list[str] = []
        self.runners: dict[str, AgentRunner] = {}
        self.stop_flag = False
        self.thread: threading.Thread | None = None
        self.next_driver: str | None = None
        self.pending_feedback: list[tuple[str, str]] = []
        self.base_commit: str | None = None
        self.log_path = Path(self.project) / ".fixpoint" / "team" / f"{self.id}.jsonl"
        # ツール自身のログは git に入れない
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        gi = Path(self.project) / ".fixpoint" / ".gitignore"
        if not gi.exists():
            gi.write_text("*\n", encoding="utf-8")

    # ---- イベント ----
    def emit(self, type_: str, **data):
        with self.cond:
            ev = {"seq": len(self.events), "ts": time.time(), "type": type_, **data}
            self.events.append(ev)
            self.cond.notify_all()
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass
        return ev

    def set_status(self, s: str, **extra):
        self.status = s
        self.emit("status", status=s, round=self.round, **extra)

    # ---- 制御 ----
    def start(self):
        self.thread = threading.Thread(target=self._main, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        for r in list(self.runners.values()):
            r.cancel()

    def add_note(self, text: str):
        self.notes.append(text)
        self.history.append(f"- 人間からのメモ: {text}")
        self.emit("note", text=text)

    def continue_(self, extra_rounds: int, note: str = ""):
        if self.thread and self.thread.is_alive():
            return False
        if note:
            self.add_note(note)
            self.pending_feedback.append(("人間", note))
            self.notes.clear()
        self.max_rounds = self.round + extra_rounds
        self.stop_flag = False
        self.start()
        return True

    # ---- プロンプト ----
    def team_desc(self) -> str:
        return "\n".join(f"- {a['name']}（{KINDS[a['kind']]['label']}{' / ' + a['model'] if a.get('model') else ''}）"
                         + (f": {a['role']}" if a.get("role") else "") for a in self.agents)

    def history_text(self) -> str:
        if not self.history:
            return "（まだありません。これが最初のラウンドです）"
        return clip("\n\n".join(self.history[-14:]), 14000)

    def build_prompt(self, a: dict) -> str:
        fb = ""
        if self.pending_feedback:
            fb = "\n## 対応してほしい指摘\n" + "\n\n".join(f"### {who}\n{clip(t, 3000)}" for who, t in self.pending_feedback)
        return f"""あなたは、複数のAIでひとつのプロジェクトを仕上げるチームのメンバー「{a['name']}」です。{('役割: ' + a['role']) if a.get('role') else ''}
今はラウンド {self.round} で、あなたが実装担当です。今このプロジェクトを編集しているのはあなただけで、他のメンバーは待っています。

## チーム
{self.team_desc()}

## ゴール
{self.goal}

## これまでの経緯
{self.history_text()}
{fb}

## 作業のしかた
- 作業ディレクトリ ({self.project}) のファイルを直接編集して、ゴールに近づけてください。
- 指摘がある場合は、それに対応することを優先してください。同意できない指摘は、理由を説明すれば対応しなくてもかまいません。
- このラウンドで無理なく終わる量にしてください。
- 最後に「やったこと」「残っている課題」「レビュアーに見てほしい点」を短くまとめてください。git commit はしなくてかまいません（ツール側で記録します）。
"""

    def review_prompt(self, a: dict, driver: dict, summary: str, diff: str, stat: str) -> str:
        return f"""あなたは、複数のAIでひとつのプロジェクトを仕上げるチームのメンバー「{a['name']}」です。{('役割: ' + a['role']) if a.get('role') else ''}
今はラウンド {self.round} のレビューです。{driver['name']} が実装を担当しました。

## チーム
{self.team_desc()}

## ゴール
{self.goal}

## これまでの経緯
{self.history_text()}

## {driver['name']} の報告
{clip(summary, 4000)}

## 今回の変更
{stat or '（ファイルの変更はありません）'}
```diff
{clip(diff, 24000) if diff else '（差分なし）'}
```

## あなたの仕事
- ファイルは編集しないでください。読む・検索する・git diff / log / show を使うことはできます。{'テストや実行で動作を確かめることもできます（ファイルを書き換えるコマンドは使わないでください）。' if self.permission != 'acceptEdits' else ''}
- ゴールに照らして、プロジェクト全体が今の状態で十分かを判断してください。
- 「これでいいんじゃない？」と思えるなら APPROVE にしてください。細かい好みの違いで止めないでください。
- 直すべき点がある場合は、具体的な修正点を箇条書きで挙げてください。
- 最後の行には、次のどちらかを必ずそのまま書いてください:
VERDICT: APPROVE
VERDICT: CHANGES
"""

    # ---- ターン実行 ----
    def run_turn(self, a: dict, phase: str, prompt: str) -> dict:
        tid = f"r{self.round}-{phase}-{a['id']}"
        runner = AgentRunner(a["kind"], a.get("model", ""), self.permission)
        self.runners[tid] = runner
        self.emit("turn_start", turn=tid, round=self.round, phase=phase, agent=a["id"])
        t0 = time.time()
        try:
            res = runner.run(prompt, self.project, phase == "build",
                             on_text=lambda s: self.emit("chunk", turn=tid, text=s),
                             on_tool=lambda n, d: self.emit("tool", turn=tid, name=n, detail=d))
        except Stopped:
            self.emit("turn_end", turn=tid, agent=a["id"], phase=phase, stopped=True, text="", verdict=None)
            raise
        except Exception as e:
            self.emit("turn_end", turn=tid, agent=a["id"], phase=phase, error=str(e), text="", verdict=None)
            return {"text": "", "error": str(e), "verdict": "ERROR"}
        finally:
            self.runners.pop(tid, None)
        verdict = parse_verdict(res["text"]) if phase == "review" else None
        self.emit("turn_end", turn=tid, agent=a["id"], phase=phase, text=res["text"], verdict=verdict,
                  meta=res.get("meta"), secs=round(time.time() - t0, 1))
        return {**res, "verdict": verdict}

    def _main(self):
        try:
            if self.round == 0:
                self.emit("session", id=self.id, project=self.project, goal=self.goal, agents=self.agents,
                          max_rounds=self.max_rounds, permission=self.permission, use_git=self.use_git)
            self.set_status("running", max_rounds=self.max_rounds)
            if self.round == 0:
                if self.use_git:
                    if not is_git_repo(self.project):
                        git(self.project, "init", "-q", check=True)
                        self.emit("info", text="git リポジトリを初期化しました")
                    c = git_checkpoint(self.project, "[fixpoint] 開始時点のスナップショット")
                    self.base_commit = head(self.project)
                    if c:
                        self.emit("info", text=f"開始前の未コミット変更をスナップショットしました ({c[:7]})")
                self.next_driver = self.agents[0]["id"]
            byid = {a["id"]: a for a in self.agents}
            while self.round < self.max_rounds:
                if self.stop_flag:
                    raise Stopped()
                self.round += 1
                driver = byid[self.next_driver]
                self.emit("round", round=self.round, driver=driver["id"])
                # 1) 実装
                before = head(self.project) if self.use_git else None
                res = self.run_turn(driver, "build", self.build_prompt(driver))
                diff = stat = ""
                if self.use_git:
                    sha = git_checkpoint(self.project, f"[fixpoint r{self.round}] {driver['name']}: {clip(res['text'], 300).splitlines()[0] if res['text'] else 'update'}")
                    if sha:
                        rng = f"{before}..{sha}" if before else sha
                        stat = git(self.project, "diff", "--stat", rng) if before else git(self.project, "show", "--stat", "--format=", sha)
                        diff = git(self.project, "diff", rng) if before else git(self.project, "show", "--format=", sha)
                    self.emit("commit", round=self.round, sha=sha, stat=stat, diff=clip(diff, 60000))
                self.history.append(f"### ラウンド {self.round}: {driver['name']} が実装\n{clip(res.get('text') or res.get('error', ''), 1500)}")
                self.pending_feedback = []
                # 2) 全員レビュー (並列・読み取り専用)
                if self.stop_flag:
                    raise Stopped()
                results: dict[str, dict] = {}

                def rv(a):
                    try:
                        results[a["id"]] = self.run_turn(a, "review", self.review_prompt(a, driver, res.get("text", ""), diff, stat))
                    except Stopped:
                        results[a["id"]] = {"verdict": "STOPPED", "text": ""}
                ths = [threading.Thread(target=rv, args=(a,), daemon=True) for a in self.agents]
                for t in ths:
                    t.start()
                for t in ths:
                    t.join()
                if self.stop_flag:
                    raise Stopped()
                verdicts = {aid: r["verdict"] for aid, r in results.items()}
                for a in self.agents:
                    r = results[a["id"]]
                    self.history.append(f"- {a['name']} のレビュー: {r['verdict']}\n{clip(r.get('text') or r.get('error', ''), 900)}")
                approved = [aid for aid, v in verdicts.items() if v == "APPROVE"]
                self.emit("tally", round=self.round, verdicts=verdicts, approved=len(approved), total=len(self.agents))
                if len(approved) == len(self.agents) and not self.notes:
                    self.set_status("consensus", text=f"不動点に到達: 全員が APPROVE しました（ラウンド {self.round}）。もう直すところはありません")
                    return
                # 3) 次の実装担当: 現担当の次から順に、CHANGES を出した人
                order = [a["id"] for a in self.agents]
                i = order.index(driver["id"])
                rotated = order[i + 1:] + order[: i + 1]
                dissent = [aid for aid in rotated if verdicts.get(aid) != "APPROVE"]
                self.next_driver = dissent[0] if dissent else rotated[0]
                self.pending_feedback = [(byid[aid]["name"], results[aid].get("text") or results[aid].get("error", ""))
                                         for aid in order if verdicts.get(aid) != "APPROVE"]
                self.pending_feedback += [("人間", n) for n in self.notes]
                self.notes.clear()
            self.set_status("max_rounds", text=f"最大ラウンド数 ({self.max_rounds}) に達しました。合意には至っていません")
        except Stopped:
            self.set_status("stopped", text="人間が停止しました")
        except Exception as e:
            import traceback
            self.emit("error", text=str(e), trace=traceback.format_exc()[-3000:])
            self.set_status("error", text=str(e))


# ---------------------------------------------------------------------------
# Web サーバー
# ---------------------------------------------------------------------------

class App:
    def __init__(self, project: str, port: int = 8765):
        self.project = project
        self.port = port
        self.session: Session | None = None
        self.clis = detect_clis()
        self.lock = threading.Lock()
        self.server = None
        self.update = {"available": None, "notes": "", "checked_at": None, "error": None, "applying": False}

    def busy(self) -> bool:
        s = self.session
        return bool(s and s.thread and s.thread.is_alive())

    # ---- 自動更新 ----
    def update_enabled(self) -> tuple[bool, dict]:
        try:
            import updater
        except Exception:
            return False, {}
        cfg = updater.load_config(HERE)
        installed = (HERE / "app.json").exists()
        return bool(installed and cfg.get("auto_update") and cfg.get("update_source")), cfg

    def update_loop(self):
        import updater
        while True:
            ok, cfg = self.update_enabled()
            interval = max(10.0, float(cfg.get("check_interval_hours") or 6) * 3600) if cfg else 6 * 3600
            time.sleep(interval)
            ok, cfg = self.update_enabled()
            if not ok:
                continue
            try:
                m = updater.check(cfg["update_source"], HERE, timeout=15)
                self.update.update(available=m["version"] if m else None, notes=(m or {}).get("notes", ""),
                                   checked_at=time.time(), error=None)
            except Exception as e:
                self.update.update(checked_at=time.time(), error=str(e))
                continue
            if m:
                # AI が作業中なら終わるまで待ってから適用
                while self.busy():
                    time.sleep(20)
                self.apply_update_and_restart(m, cfg["update_source"])

    def apply_update_and_restart(self, manifest: dict, source: str):
        import updater
        self.update["applying"] = True
        try:
            v = updater.apply(manifest, source, HERE)
            print(f"[更新] {v} に更新しました。再起動します")
        except Exception as e:
            self.update.update(applying=False, error=f"更新に失敗: {e}")
            print(f"[更新] 失敗: {e}")
            return False
        self.restart()
        return True

    def restart(self):
        args = [sys.executable, str(HERE / "launcher.py"), "--no-browser", "--no-update", "--restarted",
                "--project", self.project, "--port", str(self.port)]
        kw = {"close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        subprocess.Popen(args, cwd=str(HERE), **kw)
        self.exit_soon()

    def exit_soon(self, delay: float = 0.4):
        def _bye():
            time.sleep(delay)
            if self.session:
                self.session.stop()
            os._exit(0)
        threading.Thread(target=_bye, daemon=True).start()

    def remember_project(self, project: str):
        try:
            import updater
            if (HERE / "app.json").exists():
                cfg = updater.load_config(HERE)
                cfg["last_project"] = project
                updater.save_config(cfg, HERE)
        except Exception:
            pass


APP: App | None = None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows の SO_REUSEADDR は同じポートの二重バインドを許してしまうので使わない
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        import socket
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _guard(self) -> bool:
        # 他サイトからの CSRF 対策: カスタムヘッダー必須 (ブラウザはクロスオリジンでは preflight が必要になり、ここでは許可しない)
        if self.headers.get("X-Fixpoint") != "1":
            self._json({"error": "forbidden"}, 403)
            return False
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            self._json({"error": "bad host"}, 403)
            return False
        return True

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            b = (HERE / "fixpoint_ui.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)
            return
        if u.path == "/api/info":
            s = APP.session
            git_ok = is_git_repo(APP.project) if Path(APP.project).exists() else False
            upd_on, _ = APP.update_enabled()
            return self._json({"app": "fixpoint", "version": RUNNING_VERSION, "project": APP.project,
                               "project_exists": Path(APP.project).exists(),
                               "is_git": git_ok, "clis": APP.clis, "update": {**APP.update, "enabled": upd_on},
                               "session": None if not s else {"id": s.id, "status": s.status, "round": s.round}})
        if u.path == "/api/events":
            return self._sse()
        self._json({"error": "not found"}, 404)

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        s = APP.session
        sid = s.id if s else None
        try:
            pos = int(self.headers.get("Last-Event-ID", "-1")) + 1
        except ValueError:
            pos = 0
        self.wfile.write(f"event: hello\ndata: {json.dumps({'session': sid})}\n\n".encode())
        self.wfile.flush()
        last_ping = time.time()
        try:
            while True:
                s = APP.session
                if s is None or s.id != sid:
                    if s is not None:  # 新しいセッションに切り替わった → クライアントに再読込させる
                        self.wfile.write(b"event: reset\ndata: {}\n\n")
                        self.wfile.flush()
                        return
                    time.sleep(1)
                    continue
                with s.cond:
                    if pos >= len(s.events):
                        s.cond.wait(timeout=15)
                    batch = s.events[pos:]
                for ev in batch:
                    self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    pos = ev["seq"] + 1
                if batch:
                    self.wfile.flush()
                if time.time() - last_ping > 15:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_POST(self):
        if not self._guard():
            return
        u = urlparse(self.path)
        try:
            body = self._body()
        except Exception:
            return self._json({"error": "bad json"}, 400)
        if u.path == "/api/shutdown":
            self._json({"ok": True})
            APP.exit_soon()
            return
        if u.path == "/api/update_now":
            if APP.busy():
                return self._json({"error": "AIが作業中です。終わってから更新してください"}, 409)
            ok, cfg = APP.update_enabled()
            if not ok:
                return self._json({"error": "自動更新が設定されていません（更新元が未設定か、インストール版ではありません）"}, 400)
            try:
                import updater
                m = updater.check(cfg["update_source"], HERE, timeout=15)
            except Exception as e:
                return self._json({"error": f"更新の確認に失敗: {e}"}, 502)
            APP.update.update(available=m["version"] if m else None, checked_at=time.time(), error=None)
            if not m:
                return self._json({"ok": True, "updated": False, "version": app_version()})
            self._json({"ok": True, "updated": True, "version": m["version"]})
            threading.Thread(target=APP.apply_update_and_restart, args=(m, cfg["update_source"]), daemon=True).start()
            return
        with APP.lock:
            s = APP.session
            if u.path == "/api/start":
                if s and s.thread and s.thread.is_alive():
                    return self._json({"error": "実行中のセッションがあります。先に停止してください"}, 409)
                project = (body.get("project") or APP.project).strip()
                if not Path(project).is_dir():
                    return self._json({"error": f"フォルダが見つかりません: {project}"}, 400)
                goal = (body.get("goal") or "").strip()
                agents = body.get("agents") or []
                if not goal:
                    return self._json({"error": "ゴールを入力してください"}, 400)
                if not agents:
                    return self._json({"error": "エージェントを1人以上追加してください"}, 400)
                names = set()
                for i, a in enumerate(agents):
                    if a.get("kind") not in KINDS:
                        return self._json({"error": f"不明な種類: {a.get('kind')}"}, 400)
                    if not APP.clis.get(a["kind"], {}).get("installed"):
                        return self._json({"error": f"{KINDS[a['kind']]['label']} がインストールされていません"}, 400)
                    a["id"] = f"a{i + 1}"
                    a["name"] = (a.get("name") or f"{a['kind']}-{i + 1}").strip()
                    if a["name"] in names:
                        return self._json({"error": f"名前が重複しています: {a['name']}"}, 400)
                    names.add(a["name"])
                perm = body.get("permission") if body.get("permission") in ("acceptEdits", "editsAndCommands", "bypassPermissions") else "acceptEdits"
                APP.project = project
                APP.remember_project(project)
                APP.session = Session(project, goal, agents, int(body.get("max_rounds") or 6), perm, bool(body.get("use_git", True)))
                APP.session.start()
                return self._json({"ok": True, "id": APP.session.id})
            if not s:
                return self._json({"error": "セッションがありません"}, 400)
            if u.path == "/api/stop":
                s.stop()
                return self._json({"ok": True})
            if u.path == "/api/note":
                t = (body.get("text") or "").strip()
                if t:
                    s.add_note(t)
                return self._json({"ok": True})
            if u.path == "/api/continue":
                ok = s.continue_(int(body.get("rounds") or 3), (body.get("note") or "").strip())
                return self._json({"ok": ok} if ok else {"error": "まだ実行中です"}, 200 if ok else 409)
        self._json({"error": "not found"}, 404)


def main():
    global APP
    ap = argparse.ArgumentParser(description="複数AIの合意ループ + Web UI")
    ap.add_argument("--project", default=os.getcwd(), help="作業するプロジェクトのフォルダ")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT") or 8765))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    APP = App(str(Path(args.project).resolve()), args.port)
    srv = None
    for i in range(40):  # 更新後の再起動では、前のプロセスがポートを離すのを少し待つ
        try:
            srv = Server(("127.0.0.1", args.port), Handler)
            break
        except OSError:
            if i == 39:
                raise
            time.sleep(0.5)
    APP.server = srv
    threading.Thread(target=APP.update_loop, daemon=True).start()
    url = f"http://127.0.0.1:{args.port}"
    print(f"fixpoint {app_version()}  →  {url}   (プロジェクト: {APP.project})")
    for k, v in APP.clis.items():
        print(f"  {v['label']:<16} {'OK ' + v['version'] if v['installed'] else '未インストール'}")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        if APP.session:
            APP.session.stop()


if __name__ == "__main__":
    main()
