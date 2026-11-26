# FixPoint

いくつかのAIにひとつのプロジェクトを任せて、みんなが「これでいいね」と言うまで作業を続けてもらうツールです。

たとえば Claude が書いて、Grok がレビューして、気になるところがあればまた直す。
誰かひとりでも納得していなければ、ループは続きます。全員が OK を出したところで、作業が終わります。
その様子は、FixPoint のアプリでずっと見ていられます。

## はじめかた

1. [最新リリース](https://github.com/NeMqki/FixPoint/releases/latest) から `fixpoint-setup.exe` をダウンロードして、ダブルクリックします
2. スタートメニューかデスクトップの「FixPoint」から起動します
3. プロジェクトのフォルダを選んで、やってほしいことを書いて送ります

あとは見ているだけで大丈夫です。

> Python はセットアップに入っているので、別に入れなくて大丈夫です。管理者の権限もいりません。
> ダウンロードしたファイルを開いたときに「Windows によって PC が保護されました」と出たら、「詳細情報」→「実行」を押してください。

## 使えるAI

各AIのCLIを、自分のアカウントでログインした状態で使います。FixPoint がパスワードや APIキーを預かることはありません。

| AI | 準備 |
|---|---|
| Claude Code | `claude` を起動して `/login` |
| Grok Build | `grok login` |
| Codex CLI | `npm i -g @openai/codex` のあと `codex login` |
| Gemini CLI | `npm i -g @google/gemini-cli` のあと `gemini` を起動してログイン |

入っているものは自動で見つけてくれます。
1種類しかなくても大丈夫です。たとえば「Opus が書いて、Sonnet が厳しめにレビューする」のように、モデルや役割を変えればチームになります。

## どうやって進むの？

1回のラウンドはこんな流れです。

1. **書く** … 担当のAIがひとりでファイルを編集します。書くのは毎回ひとりだけなので、取り合いになりません
2. **記録する** … 変更を git にコミットします。いつでもそこまで戻れます
3. **見てもらう** … 全員が変更を読んで、`APPROVE`（これでいい）か `CHANGES`（直してほしい）を返します
4. **決める** … 全員が APPROVE なら終わりです。誰かが CHANGES なら、その人が次の担当になって直します

細かい好みの違いで、いつまでも終わらない…とならないように、レビュー役には「これでいいと思えるなら APPROVE して」と伝えてあります。
ラウンド数には上限があるので、それを超えたら合意できていなくても止まります（あとから「再開」で続けられます）。

## 途中で口を出したいとき

- **メモを送る** … 「UIは日本語にして」のように書くと、次のラウンドで指示として渡されます
- **停止** … 動いているAIをすぐに止めます
- **再開** … 終わったあとに、指示を足してもう少し続けられます

## AIにどこまで任せるか

| 設定 | 書く担当ができること | レビュー役ができること |
|---|---|---|
| ファイル編集だけ（いちばん安全） | 読む・編集する | 読む・差分を見る |
| 編集＋コマンド実行 | テストやビルドも実行できる | テストを回して動くか確かめられる |
| すべておまかせ（注意） | 制限なし | 編集以外は制限なし |

どの設定でも、レビュー役はファイルを書き換えられません。

## アップデート

勝手に最新になります。
起動したときと、起動中は6時間ごとに新しいバージョンを確認して、あれば入れ替えて再起動します。AIが作業している間は、終わるまで待ってくれます。

ダウンロードしたファイルは、入れ替える前に全部チェックします。ひとつでもおかしければ、何も変えません。
もし更新で調子が悪くなったら、`fixpoint rollback` でひとつ前に戻せます。

## コマンド

| コマンド | できること |
|---|---|
| `fixpoint` | 起動（ターミナルで開いているフォルダがプロジェクトになります） |
| `fixpoint --project F:\my-app` | フォルダを指定して起動 |
| `fixpoint update` | 今すぐアップデート |
| `fixpoint rollback` | ひとつ前のバージョンに戻す |
| `fixpoint version` | いまのバージョンを表示 |

アンインストールは、Windows の「設定」→「アプリ」→「インストールされているアプリ」から、ほかのアプリと同じようにできます。

---

## おまけ: AIを同時に走らせたい人へ

FixPoint は書く担当をひとりに絞るので、ふだんは気にしなくて大丈夫です。
別々のターミナルで複数のAIを同時に動かしたいときは、`fixpoint_lock.py` を MCP サーバーとして登録すると、「このファイルは今○○が触ってるよ」と教え合えるようになります。

```json
// .mcp.json（Claude Code の場合）
{ "mcpServers": { "fixpoint-lock": { "command": "python", "args": ["C:/tools/fixpoint_lock.py", "serve"] } } }
```

Claude Code なら、フックを入れておくと、ほかのAIが触っているファイルの編集を止めてくれます。

```json
// .claude/settings.json
{ "hooks": {
  "PreToolUse": [{ "matcher": "Edit|Write|MultiEdit|NotebookEdit",
                   "hooks": [{ "type": "command", "command": "python C:/tools/fixpoint_lock.py hook" }] }],
  "SessionEnd": [{ "hooks": [{ "type": "command", "command": "python C:/tools/fixpoint_lock.py release" }] }] } }
```

それぞれのAIは `FIXPOINT_AGENT=名前` を付けて起動してください。
いまの状況は `python fixpoint_lock.py status` で見られます。

## 新しいバージョンを出すとき（開発する人向け）

タグを付けて push すると、GitHub Actions がインストーラーを作って、リリースページまで公開してくれます。
タグに書いたメッセージは、リリースページと、アプリの「新しいバージョンがあります」のお知らせに表示されます。

```powershell
git tag -a v0.6.0 -m "Gemini CLI に対応しました"
```

```powershell
git push origin v0.6.0
```

アプリの窓と起動アニメーションは [Tauri](https://tauri.app/) で作っています（`desktop/`）。
中身の画面と AI のやりとりは、これまでどおり Python（`fixpoint.py`）が担当していて、アプリはそれを表示する窓です。
インストーラーは [Inno Setup](https://jrsoftware.org/isinfo.php) で作っています（`installer/fixpoint.iss`）。

手元で作るときは、Rust と Inno Setup 6 を入れてから、この順に実行すると `dist/fixpoint-setup.exe` ができます。

```powershell
cd desktop; cargo build --release; cd ..
```

```powershell
python publish.py
```

起動アニメーションだけ見たいときは、`desktop/splash/index.html` をブラウザで開いてください。

## ライセンス

MIT License（[LICENSE](LICENSE)）
