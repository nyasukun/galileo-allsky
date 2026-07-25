# galileo-allsky

`galileo-allsky` は、macOS または Ubuntu で動く Codex と Claude Code の OpenTelemetry を、Galileo で読める GenAI trace へ変換するローカル collector です。

## 目的

ローカル AI エージェントの実行を、Galileo の一つの Project で比較、検索、評価できる trace として残します。

Agent が送る OTLP が形式上正しくても、Galileo が必要とする GenAI または OpenInference 属性が不足すると、保存時に一部が拒否されることがあります。

collector は logs を Agent、LLM、Tool span へ正規化し、必要な意味属性を補ってから Galileo へ送ります。

## できること

次の経路を使い分けることで、macOS と Ubuntu の主要なローカル Agent 実行を観測できます。

| Agent surface | macOS の導入手順 | Ubuntu の導入手順 | Galileo への経路 | ガイド |
| --- | --- | --- | --- | --- |
| Codex Desktop | あり | 対象外 | collector | [Codex の設定](docs/02-codex-opentelemetry.md) |
| Codex CLI | あり | あり | collector | [Codex の設定](docs/02-codex-opentelemetry.md) |
| Claude Desktop の Code タブ | あり | あり（Linux beta） | collector | [Claude Code の設定](docs/03-claude-code-opentelemetry.md) |
| Claude Code CLI | あり | あり | collector | [Claude Code の設定](docs/03-claude-code-opentelemetry.md) |
| Hermes Agent | あり | あり | `hermes-galileo` plugin が直接送信 | [Hermes Agent の設定](docs/08-hermes-agent.md) |

この表の「あり」は導入手順を提供する範囲です。

製品ベンダーによる全実行形態の保証や、導入先 OS での実機検証完了を意味しません。

collector の変換は fixture で検証し、Hermes plugin の契約は Ubuntu CI で検証します。

各導入先では、短い[実機受入](docs/04-coverage-and-verification.md)を実行します。

collector 経路では、送信元ごとに Log stream を分け、prompt、response、tool payload を既定で送らず、既知の秘密情報を除去し、識別子を HMAC で仮名化します。

Hermes Agent は collector の七つ目の route ではありません。

`hermes-galileo` が Hermes の observer hook を Galileo 公式 SDK へ直接送るため、collector の環境ファイル、本文設定、配送保証とは別に管理します。

## 先に確認する条件

collector は `127.0.0.1` だけで待ち受け、receiver authentication を行いません。

Codex または Claude Code と collector は、同じホストかつ同じ network namespace で動かします。

そのため、別 host または別 network namespace で動く Cloud session、SSH session、container、VM、Claude Cowork から、利用者のローカル collector へは接続できません。

同一ホストの process は許可済みの route を指定して送信できるため、Log stream の分離は client authentication やアクセス制御を意味しません。

通常の ChatGPT チャットには利用者向け OTLP exporter 設定がないため、この構成の対象外です。

本文を Galileo に保存するには、Agent 側と collector 側の両方で明示的に opt-in します。

## 最短の導入例

最初に Galileo の Project を作り、collector 用の API key を用意します。

```sh
cd /path/to/galileo-allsky
python3 -m venv .venv
.venv/bin/python -m pip install -e .

mkdir -p ~/.config/galileo-allsky
cp .env.example ~/.config/galileo-allsky/collector.env
chmod 600 ~/.config/galileo-allsky/collector.env
```

`~/.config/galileo-allsky/collector.env` の `GALILEO_API_KEY` と `GALILEO_PROJECT` を設定してから、構成を検査します。

```sh
.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env \
  --check-config
```

collector を起動したら、対象 Agent のガイドにある設定を一つだけ適用します。

```sh
.venv/bin/allsky-collector \
  --env-file ~/.config/galileo-allsky/collector.env

curl --fail http://127.0.0.1:4318/healthz
curl --fail http://127.0.0.1:4318/status
```

最後に、秘密を含まない短い task を実行し、`/status` の最終成功時刻と Galileo の Log stream を確認します。

## ガイドの読み順

1. [Galileo の Project と Log stream](docs/01-galileo-account.md)で送信先を決めます。
2. [collector の導入と運用](docs/05-collector-operations.md)で macOS または Ubuntu に常駐させます。
3. [Codex の設定](docs/02-codex-opentelemetry.md)、[Claude Code の設定](docs/03-claude-code-opentelemetry.md)、または [Hermes Agent の設定](docs/08-hermes-agent.md)を選びます。
4. 本文が必要な場合だけ、[本文を Galileo へ転送する設定](docs/07-content-capture.md)を適用します。
5. [対応範囲と検証方針](docs/04-coverage-and-verification.md)と[変換設計](docs/06-design.md)で保証範囲を確認します。

## テストと検証範囲

自動テストは実 Agent と実 Galileo を呼ばず、OTLP protobuf fixture、collector の loopback receiver、Galileo HTTP stub を使います。

```sh
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

fixture は Codex と Claude Code の主要 event、trace hierarchy、本文抑止、秘密情報除去、送信先固定、gzip、chunked request、Galileo の partial success、retryable HTTP error を検証します。

Ubuntu を運用対象にする場合も、導入先の Ubuntu で同じ test suite と実機の短い受入確認を実行します。

## 配送と本文の制約

collector は Galileo への送信を一度だけ試み、retryable な失敗を Agent exporter へ返します。

v0.1 には durable spool がないため、process crash や Agent exporter が再送を諦めた後の配送は保証しません。

Codex の現行 OTel は assistant の最終回答本文を出さないため、collector は不足した出力を生成しません。

Claude Cowork の OTel monitoring は利用条件を満たす場合でも、Cowork VM からこの loopback collector へ到達できないため実接続の対象外です。

Hermes の本文取得、native Session、配送動作は [hermes-galileo](https://github.com/nyasukun/hermes-galileo) の設定と保証に従います。
