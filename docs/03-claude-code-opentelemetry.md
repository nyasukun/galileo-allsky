# Claude Code の設定

## 目的

Claude Code の Local session または CLI の logs と traces を collector へ送り、Galileo で実行の階層を確認します。

## できること

Claude Desktop の Code タブは macOS と Ubuntu の Local session で設定できます。

Ubuntu の Claude Desktop は beta で、Ubuntu 22.04 以降、x86_64 または arm64 が対象です。

Claude Code CLI は macOS と Ubuntu で同じ環境変数を使えます。

collector は Claude Code の `interaction`、LLM request、tool の trace hierarchy を維持し、Galileo 用の意味属性を補います。

この設定は 2026-07-25 に公式の Claude Code monitoring と Linux Desktop 文書で確認した内容です。

Claude Code を更新したときは、exporter schema と hook、subagent の実機受入を再実行します。

## 最初に確認する条件

この設定は、collector と同じ host かつ同じ network namespace で実行する Local session または CLI process 向けです。

Claude Cowork、Cloud session、別 host の SSH session、別 network namespace の container または VM には、この loopback endpoint を使えません。

## Claude Desktop の例

Claude Desktop の Code タブで、新しい Local session を開始する前に Environment の Local 設定を開きます。

local environment editor に次の値を保存します。

```dotenv
CLAUDE_CODE_ENABLE_TELEMETRY=1
CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1

OTEL_LOGS_EXPORTER=otlp
OTEL_TRACES_EXPORTER=otlp
OTEL_METRICS_EXPORTER=none

OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
OTEL_EXPORTER_OTLP_HEADERS=X-Allsky-Agent=claude-code
```

保存後に新しい Local session を開始します。

`~/.claude/settings.json` の `env` に同じ値を置くこともできます。

Desktop と CLI を別の Log stream に分ける場合は、共有設定ではなく Local environment editor を使います。

Ubuntu の Claude Desktop の導入と beta の機能差は、公式の [Claude Desktop on Linux](https://code.claude.com/docs/en/desktop-linux) を確認します。

## Claude Code CLI の例

CLI では `~/.claude/settings.json` の `env` に次の値を追加します。既存の `env` がある場合は同じ object へ統合します。

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_TRACES_EXPORTER": "otlp",
    "OTEL_METRICS_EXPORTER": "none",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
    "OTEL_EXPORTER_OTLP_HEADERS": "X-Allsky-Agent=claude-code-cli"
  }
}
```

`.bashrc`、`.zshrc` などへ Claude 用の `OTEL_*` を global export しません。同じ shell から起動する Codex や他の OpenTelemetry 対応 process が値を継承し、`X-Allsky-Agent=claude-code-cli` で Claude Code の Log stream へ誤配送されるためです。

一時的な検証で shell environment を使う場合も、値は `env` command で Claude process だけに渡します。

```sh
env \
  CLAUDE_CODE_ENABLE_TELEMETRY=1 \
  CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1 \
  OTEL_LOGS_EXPORTER=otlp \
  OTEL_TRACES_EXPORTER=otlp \
  OTEL_METRICS_EXPORTER=none \
  OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf \
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318 \
  OTEL_EXPORTER_OTLP_HEADERS=X-Allsky-Agent=claude-code-cli \
  claude
```

signal ごとに endpoint を分ける必要がある場合は、完全な path を設定します。

```json
{
  "env": {
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": "http://127.0.0.1:4318/v1/logs",
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://127.0.0.1:4318/v1/traces"
  }
}
```

collector が起動していることを確認し、秘密を含まない task を一つ実行します。

```sh
curl --fail http://127.0.0.1:4318/healthz
```

`/status` の最終成功時刻と Galileo の Log stream に到着が見えれば、接続は完了です。

## 本文を取得する場合

本文は既定で取得しません。

prompt だけを取得する場合は、assistant response を明示的に無効化します。

```json
{
  "env": {
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_ASSISTANT_RESPONSES": "0"
  }
}
```

assistant response、tool detail、tool content まで取得する場合は、必要な項目だけを有効にします。

```json
{
  "env": {
    "OTEL_LOG_USER_PROMPTS": "1",
    "OTEL_LOG_ASSISTANT_RESPONSES": "1",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_LOG_TOOL_CONTENT": "1"
  }
}
```

`OTEL_LOG_TOOL_CONTENT=1` は Read の内容、Bash 出力、tool input、tool output を含み得ます。

collector 側の `ALLSKY_CAPTURE_CONTENT=true` も有効にしてから、[本文を Galileo へ転送する設定](07-content-capture.md)の確認手順を実行します。

## 制約

collector へ接続できるのは、collector と同じ host と network namespace で動く Local session または CLI process です。

Cloud session、別 host の SSH session、remote host、別 network namespace の container、VM はこの loopback endpoint を利用できません。

Claude Cowork は OTel monitoring を使える契約や管理設定があっても、Cowork VM の loopback は利用者の host を指しません。

Cowork を現行 collector へ接続する手順は提供しません。

Claude Code は Bash、hook、MCP server、language server へ `OTEL_*` exporter 設定を引き継ぎません。

子 process 自身の telemetry が必要な場合は、その process に別の exporter 設定を渡します。

managed settings が generic endpoint を配布している環境では、signal 固有 endpoint が起動時に除去される場合があります。

`claude --debug` の警告と組織設定を確認します。

設定を変更した後は既存の Claude process へ反映されないため、新しい CLI process または Local session を開始します。

現在の Claude Code は hook と、Agent tool が起動する subagent の child span も出し得ます。

collector の fixture は `interaction`、LLM request、tool の主要階層を検証しており、hook の詳細な分類はまだ実機受入で確認します。

## 公式資料

- [Claude Code Monitoring](https://code.claude.com/docs/en/monitoring-usage)
- [Claude Desktop on Linux](https://code.claude.com/docs/en/desktop-linux)
- [Claude Code の設定](https://code.claude.com/docs/en/configuration)
