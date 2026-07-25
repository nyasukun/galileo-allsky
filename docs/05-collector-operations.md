# collector の導入と運用

## 目的

collector を安全に起動し、macOS または Ubuntu で常駐させ、Galileo への送信を切り分けます。

## できること

collector は Python の単一 process として動き、Codex と Claude Code が送る OTLP/HTTP protobuf を受けます。

macOS では `launchd`、Ubuntu では systemd user service を使うことで、ログイン中のユーザー環境に常駐させられます。

`/healthz`、`/readyz`、`/status` で receiver の状態と、本文を含まない診断 counter を確認できます。

## 最初に確認する安全条件

receiver は `127.0.0.1` に固定され、receiver authentication を行いません。

Agent と collector は同じ host と network namespace で動かします。

同一 host の任意 process は許可済みの `X-Allsky-Agent` を指定できるため、信頼できる単一ユーザーのローカル環境で使います。

Cloud session、SSH session、container、VM、Cowork から network 越しに接続する構成には使いません。

## 手動起動の例

Python 3.10 以上 3.15 未満の virtual environment を作ります。

Ubuntu では、選んだ Python に対応する `venv` package が必要です。

```sh
cd /path/to/galileo-allsky
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

開発と test を行う場合は dev dependencies を追加します。

```sh
.venv/bin/python -m pip install -e ".[dev]"
```

秘密情報はリポジトリ外の mode `0600` の file に置きます。

```sh
mkdir -p ~/.config/galileo-allsky
cp .env.example ~/.config/galileo-allsky/collector.env
chmod 600 ~/.config/galileo-allsky/collector.env
```

`collector.env` の `GALILEO_API_KEY` と `GALILEO_PROJECT` を設定してから、構成を検査します。

```sh
/path/to/galileo-allsky/.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env \
  --check-config
```

手動で起動します。

```sh
/path/to/galileo-allsky/.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env
```

別の terminal で receiver を確認します。

```sh
curl --fail http://127.0.0.1:4318/healthz
curl --fail http://127.0.0.1:4318/status
```

`/status` の HTTP 200 は receiver が起動していることを示します。

Galileo への送信成功は、Agent を一度実行した後の `last_success_at` と Galileo の Log stream で確認します。

## macOS で常駐させる

`deploy/com.galileo.allsky.collector.plist.example` を `~/Library/LaunchAgents/com.galileo.allsky.collector.plist` へコピーします。

次の placeholder を実際の絶対 path へ置き換えます。

| placeholder | 置換例 |
| --- | --- |
| `__ALLSKY_COLLECTOR_EXECUTABLE__` | `/Users/example/work/galileo-allsky/.venv/bin/allsky-collector` |
| `__HOME__` | `/Users/example` |

plist の構文を検査してから登録します。

```sh
plutil -lint ~/Library/LaunchAgents/com.galileo.allsky.collector.plist
launchctl bootstrap \
  "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.galileo.allsky.collector.plist
```

状態とログを確認します。

```sh
launchctl print "gui/$(id -u)/com.galileo.allsky.collector"
tail -n 100 ~/Library/Logs/galileo-allsky.error.log
```

plist または env file を変更した場合は、既存 process を入れ替えます。

```sh
launchctl bootout "gui/$(id -u)/com.galileo.allsky.collector"
launchctl bootstrap \
  "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.galileo.allsky.collector.plist
```

## Ubuntu で常駐させる

`deploy/galileo-allsky-collector.service.example` を `~/.config/systemd/user/galileo-allsky-collector.service` へコピーします。

`__ALLSKY_COLLECTOR_EXECUTABLE__`、`__ALLSKY_WORKDIR__`、`__HOME__` を実際の絶対 path へ置き換えます。

```sh
mkdir -p ~/.config/systemd/user
cp deploy/galileo-allsky-collector.service.example \
  ~/.config/systemd/user/galileo-allsky-collector.service

systemctl --user daemon-reload
systemctl --user enable --now galileo-allsky-collector.service
```

状態、receiver、ログを確認します。

```sh
systemctl --user status galileo-allsky-collector.service
curl --fail http://127.0.0.1:4318/healthz
journalctl --user -u galileo-allsky-collector.service -n 100 --no-pager
```

unit または env file を変更した場合は、daemon を再読込して再起動します。

```sh
systemctl --user daemon-reload
systemctl --user restart galileo-allsky-collector.service
```

ログアウト後も user service を動かす必要がある場合は、組織の端末管理方針を確認した上で `loginctl enable-linger <user>` を設定します。

## 構成リファレンス

| 変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GALILEO_API_KEY` | なし | 必須の Galileo API key |
| `GALILEO_PROJECT` | なし | 必須の Project 名 |
| `GALILEO_OTLP_TRACES_ENDPOINT` | Galileo Cloud の direct endpoint | 完全な upstream URL |
| `GALILEO_*_LOG_STREAM` | 送信元 ID | collector の六つの route の Log stream |
| `ALLSKY_LISTEN_PORT` | `4318` | receiver の port |
| `ALLSKY_PSEUDONYM_SECRET` | API key | identity HMAC 用 secret |
| `ALLSKY_CAPTURE_CONTENT` | `false` | redaction 後の本文を送るか |
| `ALLSKY_MAX_CONTENT_CHARS` | `12000` | 一属性の本文上限 |
| `ALLSKY_MAX_REQUEST_BYTES` | `8388608` | 圧縮前と展開後の request 上限 |
| `ALLSKY_MAX_ITEMS_PER_REQUEST` | `10000` | 一 request の log record または span 上限 |
| `ALLSKY_MAX_OUTPUT_BYTES` | `16777216` | 正規化後の trace batch 上限 |
| `ALLSKY_FORWARD_TIMEOUT_SECONDS` | `5` | Galileo 一回分の timeout |
| `ALLSKY_FORWARD_UNIDENTIFIED_LOGS` | `false` | allowlist で event を特定できない log record も span 化するか |
| `ALLSKY_AGGREGATE_TURNS` | `true` | Codex の logs を会話単位で保持し 1 trace = 1 request で送るか |
| `ALLSKY_TURN_IDLE_SECONDS` | `12` | turn を送り出すまでの無通信時間 |
| `ALLSKY_MAX_TURN_RECORDS` | `2000` | 一 turn の record 上限 |
| `ALLSKY_MAX_BUFFERED_RECORDS` | `50000` | 保持できる record の総数 |
| `ALLSKY_LOG_LEVEL` | `info` | `debug`、`info`、`warning`、`error` のログレベル |

dotenv parser は shell を実行せず、`NAME=value`、single quote、double quote、先頭の `export` だけを扱います。

変数展開と command substitution は行いません。

`--env-file` は既存の process 環境変数を上書きしません。

Hermes の dotenv を collector process に読み込ませないでください。

`ALLSKY_ALLOW_INSECURE_UPSTREAM=true` は trusted loopback Galileo stub の test 専用です。

Galileo Cloud では HTTPS を使います。

## 受ける HTTP request

| Method と path | 用途 |
| --- | --- |
| `POST /v1/logs` | `ExportLogsServiceRequest` |
| `POST /v1/traces` | `ExportTraceServiceRequest` |
| `GET /healthz` | process の生存確認 |
| `GET /readyz` | receiver の起動確認と診断 counter |
| `GET /status` | `/readyz` と同じ診断情報 |

POST は `application/x-protobuf` を使います。

gzip、`Content-Length`、上限付き chunked request を受けます。

`X-Allsky-Agent` は route を選ぶ推奨の送信元指定です。

header がない場合は resource 属性から送信元を一意に解決できる request だけを受けます。

いずれの場合も route は client authentication ではありません。

## 障害時の動作

collector が停止しても、Codex と Claude Code のモデル実行、tool 実行、ファイル編集は継続します。

復旧後に既存 Agent process が必ず telemetry を再送する保証はないため、必要に応じて Codex は新しい task、Claude Code は新しい Local session または CLI process を開始します。

collector は変換済み batch を Galileo へ一度送ります。

429、502、503、504、接続失敗は retryable status として Agent exporter へ返し、collector 内で同期 retry を重ねません。

401、404、415、422 を含む非 retryable な upstream rejection は、構成または payload を確認してから修復します。

redirect は追跡せず、Galileo credential を別 origin へ送りません。

v0.1 には durable spool がありません。

process crash、長時間の Galileo 停止、Agent exporter が retry を諦めた後の batch は失われ得ます。

## 診断

`/status` は件数、最終成功時刻、最終 error type を返します。

Codex の相関確認では、`logs.grouping.conversation`、`logs.grouping.scope_inferred`、`logs.suppressed.uncorrelated_trace_id`、`traces_suppressed` を確認できます。

変換 counter は送信元ごとにも記録します。`logs.event.codex.sse_event` と `agent.codex.logs.event.codex.sse_event` は同時に増えます。

送信元ごとの trace の形は次の counter で比較します。

| counter | 意味 |
| --- | --- |
| `logs.event.<event 名>` | 転送した log record の event 別内訳 |
| `logs.suppressed.event.<event 名>` | 抑止した log record の event 別内訳 |
| `logs.kind.<AGENT\|LLM\|TOOL>` | logs から作った span の分類別内訳 |
| `traces.kind.<AGENT\|LLM\|TOOL>` | native traces の span の分類別内訳 |
| `logs.traces_emitted`、`traces.traces_emitted` | 送出した trace 数の累計 |
| `logs.spans_per_request.<範囲>`、`traces.spans_per_request.<範囲>` | 1 request の span 数の分布 |
| `logs.traces_per_request.<範囲>`、`traces.traces_per_request.<範囲>` | 1 request の trace 数の分布 |

event 名は allowlist で検証済みの値だけを使い、範囲は `1`、`2`、`3_5`、`6_10`、`11_25`、`26_100`、`over_100` に丸めます。

`spans_forwarded` を `traces_emitted` で割ると、Log stream 上の 1 trace あたりの span 数になります。この値が 1 に近い送信元は trace が分断されています。

Codex の turn 保持は次の counter で確認します。

| counter | 意味 |
| --- | --- |
| `logs_buffered` | 保持した log record の累計 |
| `turns_released` | 送り出した turn の累計 |
| `turns_released.<turn_start\|idle\|turn_full\|capacity\|drain>` | 送り出した理由の内訳 |
| `turns_dropped` | 送出時に失敗し失われた turn |

`turns_dropped` が増える場合は `last_error_type` を確認します。`deferred_upstream` は Galileo への送信失敗で、Agent へは伝わりません。

`turns_released.capacity` が増える場合は保持上限に達しています。`ALLSKY_MAX_BUFFERED_RECORDS` を上げるか `ALLSKY_TURN_IDLE_SECONDS` を下げます。

allowlist で特定できなかった log record は既定で span 化せず、その形だけを次の counter に残します。

| counter | 意味 |
| --- | --- |
| `logs.suppressed.unidentified` | 特定できず抑止した record 数 |
| `logs.unnamed.event_name.<名前>` | 識別子の形をした event 名。空白を含む値は `unprintable` に丸めます |
| `logs.unnamed.attribute.<キー>` | 付いていた属性の**キー**のみ |
| `logs.unnamed.severity.<レベル>` | severity |

値と body は counter に入りません。

`logs.unnamed.event_name.*` に出た名前が Agent の正式な event であれば、`transform.py` の `_SAFE_EVENT_NAMES` に加えると分類対象になります。

`logs.suppressed.unidentified` が急増し `spans_forwarded` が落ちる場合は、Agent 側の event 名が変わった可能性があります。切り分けの間は `ALLSKY_FORWARD_UNIDENTIFIED_LOGS=true` で従来の挙動へ戻せます。

これらの counter は識別子の値や本文を保持しません。

payload、header、Galileo API key、Log stream の秘密値は status と collector log に出力しません。

## 公式資料

- [Codex の Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
- [Claude Code Monitoring](https://code.claude.com/docs/en/monitoring-usage)
- [OpenTelemetry Protocol の失敗処理](https://opentelemetry.io/docs/specs/otlp/#failures)
