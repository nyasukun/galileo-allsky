# 本文を Galileo へ転送する設定

## 目的

必要な本文だけを選んで Galileo に保存し、既定では本文を保存しない運用を維持します。

## できること

collector 経路では、本文なし、prompt のみ、Claude Code の assistant response と tool content を含む収集を選べます。

| 収集レベル | Codex | Claude Code | collector の設定 |
| --- | --- | --- | --- |
| 本文なし | metadata のみ | metadata のみ | `ALLSKY_CAPTURE_CONTENT=false` |
| prompt のみ | user prompt | user prompt | `ALLSKY_CAPTURE_CONTENT=true` |
| 詳細 | Codex が event に含む tool result | assistant response、tool detail、tool input、tool output | `ALLSKY_CAPTURE_CONTENT=true` |

Codex は `log_user_prompt` を有効にしても assistant の最終回答本文を出しません。

Hermes Agent は collector を通らないため、この文書の `ALLSKY_CAPTURE_CONTENT` は効きません。

Hermes の本文設定は [Hermes Agent の設定](08-hermes-agent.md)で個別に行います。

## 安全上の前提

本文を有効にすると、prompt、source code、file content、command、tool output が Galileo に保存され得ます。

collector は既知の credential、authorization header、cookie、private key、reasoning を置き換え、identity を HMAC で仮名化します。

ただし、自由記述へ埋め込まれた未知形式の秘密値をすべて検出できるわけではありません。

必要な収集範囲を最小にし、対象 Project と Log stream の保持期間とアクセス権を確認してから有効にします。

## collector 側の設定例

リポジトリ外の `collector.env` に次の値を設定します。

```dotenv
ALLSKY_CAPTURE_CONTENT=true
ALLSKY_MAX_CONTENT_CHARS=12000
```

`ALLSKY_MAX_CONTENT_CHARS` を超えた本文は、属性ごとに collector が切り詰めます。

構成を検査します。

```sh
/path/to/galileo-allsky/.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env \
  --check-config
```

出力の `capture_content` が `true` なら collector 側の opt-in は完了です。

## Codex の例

`~/.codex/config.toml` または CLI profile の既存 `[otel]` table で、prompt を有効にします。

```toml
[otel]
log_user_prompt = true
```

新しい Codex task または CLI process を開始します。

endpoint、protocol、`X-Allsky-Agent` header は[Codex の設定](02-codex-opentelemetry.md)の基本設定から変えません。

## Claude Code の例

prompt だけを取得する場合は、assistant response を明示的に無効化します。

```dotenv
OTEL_LOG_USER_PROMPTS=1
OTEL_LOG_ASSISTANT_RESPONSES=0
```

assistant response と tool content まで取得する場合は、必要な項目を有効にします。

```dotenv
OTEL_LOG_USER_PROMPTS=1
OTEL_LOG_ASSISTANT_RESPONSES=1
OTEL_LOG_TOOL_DETAILS=1
OTEL_LOG_TOOL_CONTENT=1
```

Claude Desktop では Local environment editor、CLI では起動 shell または wrapper に設定します。

基本の exporter 設定は[Claude Code の設定](03-claude-code-opentelemetry.md)と併用します。

`OTEL_LOG_RAW_API_BODIES` は会話履歴全体を含むため、この構成では有効にしません。

## 反映と確認

手動起動中の collector は停止して、同じコマンドで起動し直します。

macOS の `launchd` では次を実行します。

```sh
launchctl kickstart -k \
  "gui/$(id -u)/com.galileo.allsky.collector"
```

Ubuntu の systemd user service では次を実行します。

```sh
systemctl --user restart galileo-allsky-collector.service
```

Agent 側の設定を反映した新しい task、session、または CLI process を開始します。

本文を含まない canary task を一つ実行し、`/status` と Galileo の対象 Log stream を確認します。

## 本文転送を停止する

最初に collector の設定を戻して再起動します。

```dotenv
ALLSKY_CAPTURE_CONTENT=false
```

この変更後は、Agent が本文を送っても collector は Galileo へ転送しません。

Agent 側でも収集を止める場合は、Codex の `log_user_prompt` を `false` に戻します。

Claude Code の `OTEL_LOG_USER_PROMPTS`、`OTEL_LOG_ASSISTANT_RESPONSES`、`OTEL_LOG_TOOL_DETAILS`、`OTEL_LOG_TOOL_CONTENT` は削除するか `0` に戻します。

## 公式資料

- [Codex の Observability and telemetry](https://learn.chatgpt.com/docs/config-file/config-advanced#observability-and-telemetry)
- [Claude Code Monitoring](https://code.claude.com/docs/en/monitoring-usage)
