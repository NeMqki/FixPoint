# FixPoint

複数のAI（Claude Code、Grok Build、Codex CLI、Gemini CLI）が1つのプロジェクトを一緒に触り、**全員が「これでいいんじゃない？」と言うまで**作業を続けるツールです。
名前は「不動点（fixpoint）」から取りました。何度繰り返しても、もう変化しなくなった状態、つまり直すところがなくなった状態のことです。
Python 3.9以上だけで動き、追加のインストールはいりません。

| ファイル | 役割 |
|---|---|
| `fixpoint.py` + `fixpoint_ui.html` | **メイン**。合意するまでラウンドを回すオーケストレーターと、ブラウザで見るUI |
| `fixpoint_lock.py` | おまけ。複数のAIを**同時並行**で動かすときに使うファイルロック用のMCPサーバー（下のほうで説明） |
| `launcher.py` / `updater.py` | 起動と自動更新 |
| `installer/` / `publish.py` | インストーラーとリリース作成（開発者用） |

---

## インストール

**[最新リリース](https://github.com/NeMqki/FixPoint/releases/latest)** の Assets から `fixpoint-setup.cmd` をダウンロードして、ダブルクリックしてください。次の作業を自動で行います（管理者権限は不要です）。
Windows に「PC を保護しました」と表示されたら、「詳細情報」→「実行」を選んでください。

1. Python 3.9 以上を探す（なければ、入れ方を表示して止まります）
2. `%LOCALAPPDATA%\Programs\fixpoint` にファイルをコピーする
3. スタートメニューとデスクトップに「FixPoint」を作る（コンソール画面なしで起動します）
4. `fixpoint` コマンドを使えるようにする（ユーザーの PATH に追加。新しく開いたターミナルから有効）
5. Windows の「インストールされているアプリ」に登録する（ここからアンインストールできます）
6. FixPoint を起動してブラウザを開く

もう一度実行すると上書きインストールになります。設定（`config.json`）はそのまま残ります。

| コマンド | 内容 |
|---|---|
| `fixpoint` | 起動する（ターミナルで開いているフォルダがプロジェクトになります） |
| `fixpoint --project F:\x` | プロジェクトを指定して起動する |
| `fixpoint update` | 今すぐ更新する |
| `fixpoint rollback` | 1つ前のバージョンに戻す |
| `fixpoint version` | バージョンと更新元を表示する |

## 自動更新

- **起動時**と、**起動中は6時間ごと**に、GitHub の最新リリースの `manifest.json` を確認します。
- 新しいバージョンがあれば、全ファイルを SHA-256 で検証してから置き換え、自動で再起動します。1つでもチェックサムが合わなければ、何も変更しません。
- AIが作業中のときは、終わるのを待ってから更新します。画面上部に「新しいバージョンがあります」と表示されます。
- 置き換える前のファイルは `.backup/<旧バージョン>/` に残ります（最新の3世代分）。`fixpoint rollback` で戻せます。
- 設定はインストール先の `config.json` にあります: `update_source`（更新元）、`auto_update`（true/false）、`check_interval_hours`、`port`

### リリースのしかた（開発者用）

`v` から始まるタグを push すると、GitHub Actions（`.github/workflows/release.yml`）が次の作業を自動で行います。

- インストーラーと自動更新用のファイルを作る
- 日本語のリリースページを公開する

タグのメッセージが、リリースページと、アプリに出る更新のお知らせに表示されます。

```powershell
git tag -a v0.3.1 -m "Gemini CLI に対応しました"
```

```powershell
git push origin v0.3.1
```

数分後、インストール済みのPCは次の確認のときに自動で更新されます。
手元で確かめたいときは、`python publish.py --version 0.3.1 --notes "メモ"` を実行すると、`release/` に同じものが作られます。

---

## FixPoint の使い方

```powershell
python fixpoint.py --project F:\your-project
```

ブラウザで `http://127.0.0.1:8765` が開きます。開いたら次の順に操作します。

1. ゴールを書く
2. メンバーを選ぶ（種類、モデル、役割・性格）
3. 「開始」を押す

### ログインは各CLIのものを使う

FixPoint は各AIのCLIを呼び出すだけで、APIキーやパスワードは一切扱いません。使いたいCLIに、あらかじめ自分のアカウントでログインしておいてください。

| AI | 準備 |
|---|---|
| Claude Code | `claude` を起動して `/login`（このPCではすでに使えます） |
| Grok Build | `grok login`（このPCではすでに使えます） |
| Codex CLI | Node.js を入れる → `npm i -g @openai/codex` → `codex login` |
| Gemini CLI | Node.js を入れる → `npm i -g @google/gemini-cli` → `gemini` を起動してログイン |

インストールされているCLIは自動で見つけて、画面左下に「使用可」と表示します。
Claude Codeしかなくても、モデルや役割を変えたClaude同士でチームを組めます（例: Opusが実装、Sonnetが厳しめのレビュアー）。

### 合意までの流れ

```
ラウンド N
 ├─ 実装: 実装担当の1人だけがファイルを編集する（書き手は常に1人なので取り合いが起きない）
 ├─ git チェックポイント: 変更をコミットし、差分をUIに表示する
 ├─ レビュー: 全員が読み取り専用で並列にレビューし、最後に VERDICT: APPROVE / CHANGES を書く
 └─ 判定
     ├─ 全員 APPROVE → 合意成立で終了
     └─ そうでない → CHANGES を出した人が次の実装担当になり、指摘を反映する
```

- レビュアーには「細かい好みの違いでは止めないこと（これでいいと思えるなら APPROVE）」と伝えています。
- 最大ラウンド数に達したら、合意していなくても止まります。「再開」でラウンドを足して続けられます。
- 実行中に「人間からのメモ」を送ると、次のラウンドで指摘として渡されます。メモが残っている間は、合意しても終わりません。
- 「停止」を押すと、実行中のAIプロセスをすぐに止めます。
- ターンごとに `[fixpoint rN] 名前: 要約` という形でコミットされるので、`git log` で追えて、`git reset` で戻せます。
- 開始前にあった未コミットの変更は、最初にスナップショットとしてコミットされます。
- git リポジトリでないフォルダでは `git init` されます。
- ログは `.fixpoint/team/*.jsonl` に残ります（gitには入りません）。

### 実装担当の権限

| 設定 | 実装担当ができること | レビュアーができること |
|---|---|---|
| ファイル編集だけ（いちばん安全） | 読む、編集する | 読む、git diff/log/show |
| 編集＋コマンド実行 | 上に加えて、テストやビルドを実行できる | 上に加えて、テストを実行して動作を確かめられる |
| すべて自動許可（注意） | 制限なし | 編集以外は制限なし |

レビュアーはどの設定でも、ファイルを編集するツールが使えません。

### 画面

- **ヘッダー**: 状態（作業中 / 合意成立 / 停止 / 上限到達）、ラウンド数、今のラウンドで各メンバーが出した判定（緑 = APPROVE、黄 = CHANGES）
- **左**: メンバーごとの状態（実装中 / レビュー中）と最新の判定、メモ送信、停止、再開
- **中央**: ラウンドごとのタイムライン（AIの発言、ツール操作がリアルタイムで流れる）、差分、レビューの並び、判定結果

---

## fixpoint-lock（並列モード用のファイルロック）

FixPoint は書き手を1人に絞るので、ロックはいりません。
**複数のAIをそれぞれ別ターミナルで同時に走らせたい**ときは、`fixpoint_lock.py` をMCPサーバーとして各AIに登録します。こうすると、ファイルロック、タスクの取り合い防止、メッセージの機能が使えます。

```json
// .mcp.json (Claude Code)
{ "mcpServers": { "fixpoint-lock": { "command": "python", "args": ["C:/tools/fixpoint_lock.py", "serve"] } } }
```

```json
// .claude/settings.json : 他のAIがロック中のファイルの編集をブロックし、空いているファイルは自動でロックする
{ "hooks": {
  "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                   "hooks": [{ "type": "command", "command": "python C:/tools/fixpoint_lock.py hook" }] }],
  "SessionEnd": [{ "hooks": [{ "type": "command", "command": "python C:/tools/fixpoint_lock.py release" }] }] } }
```

各AIは `FIXPOINT_AGENT=名前` を付けて起動します。Codex と Gemini は、それぞれの設定ファイルの `env` に書きます。

使えるツールは `status` / `lock_files` / `unlock_files` / `check_files` / `task_add` / `task_list` / `task_claim` / `task_done` / `task_release` / `send_message` / `read_messages` です。
人間は `python fixpoint_lock.py status` / `log` / `unlock-all` で、状況の確認や強制解放ができます。

---

## ライセンス

MIT License（[LICENSE](LICENSE)）
