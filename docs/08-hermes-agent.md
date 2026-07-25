# Hermes Agent を Galileo に接続する

Hermes Agent は `hermes-galileo` plugin を使い、collector を経由せず Galileo へ直接 trace を送ります。

## 目的

Hermes の user turn、LLM request、tool、approval、subagent を、Galileo の trace と native Session として確認します。

同じ Hermes session の複数 turn は一つの Galileo native Session に関連付けられます。

この経路は Codex と Claude Code の collector 経路とは独立しているため、両方を同じ Galileo Project で使えます。

## できること

`hermes-galileo` は Hermes の `hermes.observer.v1` hook を OpenTelemetry GenAI span に変換し、Galileo 公式 SDK で直接 export します。

prompt、response、tool payload は既定で保存せず、user ID と session ID は HMAC で仮名化します。

observer の初期化や Session 解決に失敗しても、Hermes の本体処理を止めない fail-open の構成です。

Hermes plugin の契約は Ubuntu CI で検証されています。

ただし、Hermes upstream がすべての Ubuntu 実行形態を保証するという意味ではありません。

macOS の導入手順は提供しますが、同じ plugin contract の CI 証跡はありません。

macOS を運用対象にする場合は、導入先で短い実機受入を実行します。

この手順は 2026-07-25 に確認した `hermes-galileo` の revision `4efaa2552834` を基準にしています。

Hermes または plugin を更新したときは、`hermes-galileo` 側の test と短い実機受入を再実行します。

## 導入例

まず plugin を導入して有効化します。

```sh
hermes plugins install nyasukun/hermes-galileo --enable
```

installer は plugin source を置きますが、Python dependency は Hermes と同じ Python 環境へ別途導入します。

managed install の既定 interpreter は `~/.hermes/hermes-agent/venv/bin/python` です。

Hermes を別の virtual environment で実行している場合だけ、その interpreter に置き換えます。

次の例は `HERMES_HOME` が未設定で、plugin が `~/.hermes/plugins/hermes_galileo` にある場合です。

`HERMES_HOME` を設定している場合は、`~/.hermes` をその値に置き換えます。

```sh
uv pip install \
  --python ~/.hermes/hermes-agent/venv/bin/python \
  -e ~/.hermes/plugins/hermes_galileo
```

`uv` がない場合は、同じ interpreter の `pip` を使います。

```sh
~/.hermes/hermes-agent/venv/bin/python -m pip install \
  -e ~/.hermes/plugins/hermes_galileo
```

active Hermes profile の `~/.hermes/.env` に、Galileo 用の値を設定します。

`HERMES_HOME` を設定している場合は、`~/.hermes` をその値に置き換えます。

```dotenv
GALILEO_API_KEY=実際のAPIキー
GALILEO_PROJECT=galileo-allsky
GALILEO_LOG_STREAM=hermes-agent
```

custom または self-hosted Galileo を使う場合は、`GALILEO_CONSOLE_URL` と `GALILEO_API_URL` を必ず対で指定します。

挙動を変える場合だけ、template を `config.yaml` としてコピーします。

```sh
cp ~/.hermes/plugins/hermes_galileo/config.yaml.example \
  ~/.hermes/plugins/hermes_galileo/config.yaml
```

稼働中の `hermes gateway` は再起動し、CLI は次回の起動から設定を読みます。

## 本文を保存する場合

本文は既定で保存しません。

privacy 審査済みの profile だけで、`$HERMES_HOME/.env` に必要な opt-in を設定します。

```dotenv
HERMES_GALILEO_CAPTURE_CONTENT=true
HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false
```

会話履歴全体の保存は、現在の turn の本文保存とは別の opt-in です。

必要性を確認できない限り、`HERMES_GALILEO_CAPTURE_CONVERSATION_HISTORY=false` を維持します。

## 確認方法

Hermes を一度実行し、Galileo の `hermes-agent` Log stream に trace が到着することを確認します。

Python API からは `health_snapshot()` を診断に使えます。

```python
from hermes_galileo import health_snapshot

print(health_snapshot())
```

`exporter_ready` は plugin の exporter 初期化と startup buffer の replay が完了したことを示します。

Galileo が batch を永続化したことまでは示さないため、trace の到着を Galileo 側でも確認します。

## 制約と collector との境界

Hermes は `X-Allsky-Agent`、`ALLSKY_CAPTURE_CONTENT`、collector の `collector.env` を使いません。

Hermes の API key、Project、Log stream、pseudonym secret は `config.yaml` に書かず、`$HERMES_HOME/.env` に置きます。

YAML の構文、型、未知 key、secret、routing field が不正な場合は、observability だけを無効化して Hermes 本体を継続します。

`HERMES_GALILEO_PSEUDONYM_SECRET` は production では API key と分離して設定します。

同じ process に複数の Hermes profile を multiplex しません。

別 profile の event は誤配送を避けるため drop されます。

direct SDK 経路の retry、partial success の可視化、durable WAL、tail sampling の保証は collector と共通ではありません。

詳細な運用条件、native Session、sampling、障害時の扱いは [hermes-galileo](https://github.com/nyasukun/hermes-galileo) の README と Operations を参照します。
