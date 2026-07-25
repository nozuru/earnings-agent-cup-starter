# Earnings Agent Cup Starter Kit

Claude Agent SDK または OpenAI Codex SDKを使い、自由なプロンプトから、その日に決算を発表する日本株を調査して、Earnings Agent Cupの注文JSONを作るスターターキットです。APIトークンを設定した場合だけ自動提出します。証券口座への接続や実発注は一切行いません。

## まず動かす

前提は [uv](https://docs.astral.sh/uv/getting-started/installation/) と Python 3.12以上です。`yfmcp` の現行版がPython 3.12以上を必要とするため、SDK単体の最低要件より高くしています。

```sh
git clone https://github.com/nozuru/earnings-agent-cup-starter.git eac-starter-kit
cd eac-starter-kit
uv sync
cp .env.example .env
```

次にClaudeかCodexのどちらか一方を設定します。決算カレンダーが受付中でない日は、ランナーはモデルを起動せず正常に停止します。

### Claudeで動かす

Claude ProまたはMaxの自分のサブスクリプションでログインします。

1. Claude Code CLIをインストールします。
   - macOS / Linux: `npm install -g @anthropic-ai/claude-code`
   - Windows: `winget install Anthropic.ClaudeCode`
2. `claude setup-token` を実行し、ブラウザでログインします。
3. 表示された値を `.env` の `CLAUDE_CODE_OAUTH_TOKEN` に設定します。
4. 実行します。

```sh
./scripts/run_claude.sh \
  "本日の決算銘柄を分析し、決算サプライズが大きいと考える銘柄だけで注文を作って"
```

初回だけ `uvx` がyfmcp一式を取得するため、起動に時間がかかることがあります。ランナーはyfinanceツールを起動時に必ず読み込み、初回ダウンロードを考慮してMCP接続を最大120秒待ちます。

環境に `ANTHROPIC_API_KEY` があると課金APIの認証が優先される可能性があるため、Claude用ラッパーは実行直前に必ず削除します。`.env` に `ANTHROPIC_API_KEY` を書かないでください。

### Codexで動かす

ChatGPT PlusまたはProの自分のサブスクリプションでログインします。

1. Codex CLIをインストールします。
   - macOS / Linux: `npm install -g @openai/codex` または `brew install codex`
   - Windows: `irm https://chatgpt.com/codex/install.ps1 | iex`
2. `codex login` を実行し、「Sign in with ChatGPT」でログインします。
3. `codex login status` でログイン状態を確認します。
4. 実行します。

```sh
./scripts/run_codex.sh \
  "本日の決算銘柄を分析し、決算サプライズが大きいと考える銘柄だけで注文を作って"
```

Codexの認証情報はOSの資格情報ストアまたは `~/.codex/auth.json` に保存されます。パスワードと同様に扱い、表示、共有、コミットをしないでください。Codex SDKには対応するCodex CLIランタイムが同梱され、Pythonランナーは既存のChatGPTログインを利用します。Web検索、yfinance MCP、設定済みならMomonga MCPもランナーが起動時に一時設定するため、利用者の `~/.codex/config.toml` を編集しません。ランナーは一時作業ディレクトリを使い、APIトークンとMomonga APIキーをエージェント本体から隔離します。

## 自動提出

参加登録時に一度だけ表示されるAPIトークンを `.env` に設定します。

```dotenv
EAC_API_TOKEN=32桁のトークン
```

未設定なら、検証済みの手動提出用JSONを `logs/` に保存して終了します。設定済みなら次の順で処理します。

1. `GET /api/events/open` で対象日、締切、決算カレンダーを取得
2. 決算カレンダーをモデルのプロンプトへ追加
3. エージェントの最終JSONをローカル検証
4. 締切前であることを再確認
5. `PUT /api/portfolios/{date}` へ、その日のポートフォリオ全体を置き換える形で提出
6. `GET /api/portfolios/me` で受理内容を照合

ローカル検証では、決算カレンダーに載っている銘柄か、コード形式、重複、1銘柄±20%、合計100%以内、理由500文字以内を確認します。不正なJSONは生応答とエラーだけを保存し、提出しません。空の `orders` は「その日は取引しない」または提出済み注文の全取消として有効です。

出力例:

```json
{
  "orders": [
    {
      "code": "72030",
      "weight_bps": 1500,
      "reason": "会社計画に対する進捗と受注残から上方修正余地がある一方、直近の上昇を考慮して15%に抑える。"
    }
  ],
  "summary": "確度の高いロングだけに絞り、残りは現金とした。"
}
```

`summary` はログ専用です。Earnings Agent CupのAPIへ送るのは `orders` だけです。

## Momonga Search（任意）

未設定でもyfinanceとWeb検索だけで完全に動きます。適時開示、決算短信、有価証券報告書などの一次情報を検索したい場合だけ追加します。

1. [申請ページ](https://app.momongasearch.com/access/request?campaign=friends-202605)で申請し、識別メモに `JPSI + 参加名` を記入します。
2. MCPサーバーを取得します。

```sh
git clone https://github.com/ReiMinamoto/momonga_search_mcp.git
cd momonga_search_mcp
uv sync
```

3. キットの `.env` に設定します。

```dotenv
MOMONGA_SEARCH_API_KEY=ms_live_...
MOMONGA_MCP_DIR=/絶対パス/momonga_search_mcp
```

ClaudeとCodexのどちらでも、2つの値があると自動でMCPを追加します。CodexではAPIキーをモデルの環境変数や `config.toml` に渡さず、専用ランチャーだけが `.env` から読み込みます。このランチャーはMomongaサーバーが固定返却するMCPプロトコル版をクライアントの要求版へ合わせるため、現行Codexとも接続できます。

## モデルを変える

デフォルトはClaudeが `claude-sonnet-5`、Codexが `gpt-5.6-luna`（reasoning effort: low）です。変更する場合だけ `.env` に完全なモデルIDを書きます。

```dotenv
CLAUDE_MODEL=claude-sonnet-5
CODEX_MODEL=gpt-5.6-luna
```

## 毎日の実行

**基本は手動実行です。** 決算カレンダーは**前日22:10 JST**に公開され（掲載目標22:30）、提出締切は**当日08:30 JST**です。

```sh
scripts/run_claude.sh "本日の決算銘柄を分析し、自信のあるものだけで注文判断を出して"
```

Windowsは `scripts\run_claude.bat` を実行します。Codexを使う場合は `run_codex` に変えます。

**22:35以降**に実行してください。22:30より前だと公開の再試行と重なり、対象銘柄が揃っていないことがあります。出力を確認してから寝る、という流れを想定しています。締切前なら何度でも上書きできるので、結果に納得できなければ実行し直して構いません。

1日1回、開催期間は14営業日です。自動化しなくても十分こなせる量なので、まず手動で回してください。

### 実行を忘れそうな人向け（任意）

cronやタスクスケジューラへの登録も用意していますが、**無人運用の手段ではなく実行忘れの保険**として使ってください。

> **PCがスリープしていると実行されません。** macOSのcronは、起動時刻に寝ていた回を後から取り戻しません。電源に接続してフタを開けたままにできる環境でだけ登録してください。ノートPCを持ち歩く場合は手動実行のほうが確実です。

**macOS / Linux** — `crontab -e` に絶対パスで登録します。

```cron
PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin
35 22 * * * /Users/you/eac-starter-kit/scripts/run_claude.sh --skip-if-submitted "本日の決算銘柄を分析し、自信のあるものだけで注文判断を出して" >> /Users/you/eac-starter-kit/logs/cron.log 2>&1
0 7 * * * /Users/you/eac-starter-kit/scripts/run_claude.sh --skip-if-submitted "本日の決算銘柄を分析し、自信のあるものだけで注文判断を出して" >> /Users/you/eac-starter-kit/logs/cron.log 2>&1
```

2本あるのは、22:35が実行できなかった日を翌朝07:00で拾うためです。`--skip-if-submitted` を付けるとその日の提出がすでにあるときはモデルを起動せずに終了するので、二重に登録しても無駄撃ちになりません。

このフラグは定期実行専用です。手動実行では付けないでください。付けなければ締切まで何度でも実行し直して上書きできます。

cronでは環境が最小限になるため、ラッパーがリポジトリへ移動し、`.env` を読み、`.venv` のPythonを絶対パスで起動します。Codexの認証を見つけられない場合は、cron実行ユーザーとログインしたユーザーが同じか確認してください。macOSではcronにフルディスクアクセスが必要な場合があります。

**Windows** — `.bat` をダブルクリックして成功することを先に確認してから、タスクスケジューラの「基本タスクの作成」で設定します。

- 操作: 使用する `.bat` を指定
- 開始（オプション）: キットの絶対パス
- 「ユーザーがログオンしているときのみ実行」を選択
- 「タスクを実行するためにスリープを解除する」と「スケジュールされた開始時刻を過ぎたらタスクをすぐに開始する」を有効化

WSL2は不要です。OAuthのブラウザコールバックやWindows側とのパス差異が増えるため、このキットはWindowsネイティブ実行を前提とします。

### GitHub Actionsなどのクラウド実行について

このキットは**参加者自身の端末での実行を前提**としています。ClaudeとCodexのどちらもサブスクリプション認証を使っており、その扱いが両者で異なるためです。Claudeは `claude setup-token` の長期トークンでCI実行が公式にサポートされていますが、Codexのアクセストークン発行はChatGPT Enterpriseワークスペース限定で、個人のPlus/Proでは利用できません（公式が案内する自動化手段は従量課金のAPIキーです）。

片方のトラックだけクラウド実行できる状態を標準手順にはしません。どうしてもクラウドで動かしたい場合はDiscordで相談してください。

## ログ

各実行は `logs/` にUTC時刻、使ったSDK名、対象日を含むファイルを残します。

- `*-raw.txt`: モデルの生応答。抽出や検証に失敗しても残る
- `*-analysis.json`: 検証済みの注文と全体所感
- `*-orders.json`: 手動提出に使える `{ "orders": [...] }`
- `*-submission.json`: API受理結果と提出後照合
- `*-tools.json`: SDKで観測した実ツール呼び出し名
- `claude-YYYYMMDD.log` / `codex-YYYYMMDD.log`: ラッパーの標準出力

これらは `.gitignore` 済みです。

## トラブルシュート

| 症状 | 確認すること |
|---|---|
| `仮想環境がありません` | キット直下で `uv sync` |
| 受付中の決算カレンダーがない | 開催日を確認。公開は前日22:10 JST（掲載目標22:30）なので、それより前だとまだ出ていません。モデルは起動されません |
| ClaudeがAPI課金側へ接続する | `.env` から `ANTHROPIC_API_KEY` を削除しラッパー経由で実行 |
| Codexが未ログイン | 同じOSユーザーで `codex login status`、必要なら `codex login` |
| yfinance MCPが起動しない | `uvx yfmcp` を単独実行し、Python 3.12以上とネットワークを確認 |
| cronだけ失敗する | PATH、絶対パス、実行ユーザー、`.env`、macOS権限を確認 |
| cronが動いた形跡がない | 実行時刻にPCがスリープしていた可能性が高い。macOSのcronは寝ていた回を後から取り戻しません。手動実行へ切り替えてください |
| `すでに提出済みです` と出て終わる | `--skip-if-submitted` 付きの実行です。やり直すならフラグなしで実行してください |
| JSON検証に失敗する | `logs/*-raw.txt` と `logs/*-error.txt` を確認。提出は行われていません |
| HTTP 401 | `EAC_API_TOKEN` を確認。漏洩時はDiscordで運営へ連絡して再発行 |
| 締切超過 | その日の提出は行われません。PCの時計と実行時刻を確認。締切は当日08:30 JSTです |

## 安全上の注意

- このキットはEarnings Agent Cupのデモトレード用API専用です。出力は投資助言ではありません。
- `.env` とCodexの認証ストレージはパスワード同等です。共有・コミット禁止です。
- サブスクリプション認証は本人が自分の端末で使う範囲に限定し、第三者向けサービスへ流用しないでください。
- yfinanceはYahoo! Financeの非公式ラッパーです。個人利用かつ低頻度で使ってください。
- 提出は締切前なら上書きできますが、常に `logs/` の内容を確認してください。

## 開発者向け検証

```sh
uv sync
uv run python -m unittest discover -s tests
uv run python -m py_compile \
  eac/api.py eac/runtime.py claude_agent/main.py codex_agent/main.py
```
