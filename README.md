# switch-auto

PC から Nintendo Switch を操作し、『Pokémon LEGENDS Z-A』を新しいユーザーで始めて「ふしぎなおくりもの」を受け取るまでを自動で進めるツールです。

PC はキャプチャボードで画面を見て、Bluetooth でコントローラのふりをしてボタンを押します。ブラウザのダッシュボードで進み具合を見張り、自動にできない場面の手前では止まって人に渡します。

## 免責事項（必ずお読みください）

- このソフトウェアは無保証です。使ったことで起きたいかなる損害（セーブデータの消失、本体やアカウントの利用制限、機器の故障などを含む）についても、作者は責任を負いません。すべて自己責任で使ってください。
- **オンラインでは使わないでください。** オンラインでの対戦・交換・その他のオンライン機能で、自動操作を使うことを禁止します。
- 自動操作は、ゲームやサービスの利用規約に反するおそれがあります。利用規約を確認し、自分の判断で使ってください。
- このプロジェクトは、任天堂株式会社、株式会社ポケモン、株式会社ゲームフリーク、株式会社クリーチャーズとは関係のない、非公式のものです。
- Nintendo Switch は任天堂の商標です。ポケモン・Pokémon は任天堂・クリーチャーズ・ゲームフリークの登録商標です。その他の名前は各社の商標です。
- 判定に使う見本（`za/refs.npz`）はゲーム画面の一部から作っています。著作権は権利者にあります。

### Disclaimer

- This software is provided "as is", without warranty of any kind. The author is not responsible for any damage caused by its use, including loss of save data, restrictions on your console or account, or hardware failure. Use it at your own risk.
- **Do not use it online.** Using this automation for online battles, trades or any other online features is prohibited.
- Automated input may violate the terms of service of the game or related services. Check them and decide for yourself.
- This is an unofficial project and is not affiliated with Nintendo, The Pokémon Company, GAME FREAK or Creatures.
- Nintendo Switch is a trademark of Nintendo. Pokémon is a registered trademark of Nintendo, Creatures and GAME FREAK. Other names are trademarks of their respective owners.

## できること

| 機能 | 中身 | 区間（`za/flow.yaml`） |
|---|---|---|
| Switch を PC から操作する | ダッシュボードで、PC のキーボードから Switch を操作する | `common/` |
| ふしぎなおくりものの手前まで進める（周回） | 言語を選ぶ画面から、ふしぎなおくりものを受け取れるところまで | `language_1` 〜 `talk_31` |
| ふしぎなおくりものを受け取る（オプション） | 受け取って、ボックスに置き、セーブするまで | `last_menu_1` 〜 `end_1` |

「使い方」では、周回の回し方を説明します。

ゲーム内の選択は決め打ちです。名前は「ああああ」、最初のパートナーはポカブ、見た目は初期値です。ゲームの言語は日本語を前提にしています。

## 用意するもの

- Linux の PC（Ubuntu で動作を確かめています）
- NUXBT が使える Bluetooth アダプタ
- HDMI のキャプチャボード（1280×720 で取り込めるもの。`/dev/video0` などで見えるもの）
- Nintendo Switch と『Pokémon LEGENDS Z-A』
- Python 3（3.14 で確かめています）

## 導入

大まかな流れだけ書きます。環境によって細かいところが違うので、詰まったら ChatGPT や Claude などの LLM に、この README とエラーの文面を渡して聞いてください。

1. このリポジトリを clone する。
2. Python のパッケージを入れる。NUXBT は依存を全部入れると使わない GUI などのビルドで止まりやすいので、Bluetooth に要る部分だけ apt で入れ、NUXBT 自体は依存なしで入れる（Ubuntu の場合）。

   ```
   sudo apt install python3-venv python3-dbus python3-gi
   python3 -m venv --system-site-packages .venv
   .venv/bin/pip install -r requirements.txt
   .venv/bin/pip install --no-deps nuxbt
   ```
3. NUXBT の README（https://github.com/hannahbee91/nuxbt）に沿って、Bluetooth まわりを用意する。root を使わずに動かすなら、仮想環境の python に権限（setcap）を付ける。
4. Switch を PC につなぐ。初回は Switch で「コントローラー」→「持ちかた/順番を変える」を開いた状態で、PC から待ち受ける。2回目からは Switch のアドレスを指定して、PC から接続できる。
5. Switch の映像をドック → キャプチャボード → PC の順につなぎ、PC で映像が取れることを確かめる。

まずダッシュボードだけで Switch を動かせるか確かめると、切り分けが楽です。

```
.venv/bin/python common/dashboard.py --live /dev/video0 --switch AA:BB:CC:DD:EE:FF
```

`http://127.0.0.1:8000/` を開くと Switch の映像が出て、キーボードで操作できます。

## 使い方

### 起動

1. Switch の電源を入れ、キャプチャボードを通して PC で Switch の画面が映ることを確かめる。
2. Switch で新しいユーザーを作り、ゲームを起動して、言語を選ぶ画面まで進めておく。
3. 次のコマンドで起動する（`AA:BB:CC:DD:EE:FF` は自分の Switch のアドレス）。

```
.venv/bin/python za/run.py --switch AA:BB:CC:DD:EE:FF --to talk_31
```

4. 「接続した。」と出たら、ブラウザで `http://127.0.0.1:8000/` を開き、ページを1回クリックする（音を鳴らすため）。
5. ダッシュボードの「開始」か、端末の Enter で始まる。
6. 終わるときは、端末で `q` か Ctrl+C。押しているボタンをすべて離してから終わる。

主なオプション:

| オプション | 意味 |
|---|---|
| `--switch MAC` | つなぐ Switch のアドレス |
| `--live DEV` | キャプチャのデバイス（省略すると `/dev/video0`） |
| `--from 区間` / `--to 区間` | その区間から始める／その区間で終える |
| `--port N` | ダッシュボードのポート（省略すると 8000） |

### ダッシュボードの見方

- **映像**：Switch の画面がそのまま出る。
- **コントローラの図**：押されているボタンが光る。赤は人が押しているもの、青は PC が押しているもの。
- **区間の一覧**：`za/flow.yaml` の区間が並び、今いる区間と経過時間が出る。
- **合図**：画面から読み取った合図（暗転・会話の枠など）のうち、今出ているもの。
- **ボタン**：
  - 「PC の操作を止める」：PC の操作を止め、ボタンをすべて離して人に渡す。
  - 「開始」：最初の開始と、人の番から PC に戻すときに押す。区間をクリックしてから押すと、その区間の先頭から続ける。

人に渡すときは、通知と音でお知らせします。

### PC と人の分担

| 担当 | やること |
|---|---|
| PC | 会話・ムービー・メニュー・名前入力・バトル・記録を再生する移動 |
| 人 | 周の準備（ユーザー作成から言語を選ぶ画面まで）と、自動にできない移動（`kind: handoff` の区間） |

人の番になると PC はボタンをすべて離して待ちます。人がダッシュボードのキーボード操作で進め、終わったら「開始」か Enter で PC に戻します。

移動の区間は、人が歩いたときの操作の記録を同じ時間どおりに再生しています。ゲームには毎回同じにならない要素（歩いている人やポケモンの位置、読み込みの長さなど）があるため、引っかかったり目的の場所に着かなかったりして失敗することがあります。失敗すると、上限の時間を過ぎたところで人に渡されます。その場合は手で次の区間の頭まで進め、その区間をクリックして「開始」で戻してください。

### キーボードの割り当て

ダッシュボードを開いたブラウザで使います。押している間だけ押した扱いになります。

| Switch | キー |
|---|---|
| 左スティック | 矢印 |
| 右スティック | Shift＋矢印 |
| 十字キー | 8 2 4 6 |
| A B X Y | a b x y |
| L / R | l / r |
| ZL / ZR | z / c |
| L3 / R3（押し込み） | v / n |
| + / − / ホーム | p / - / h |

タブから離れたとき、ブラウザからの知らせが1秒途切れたとき、PC の番になったときは、すべて離します。

### うまくいかないとき

| 起きたこと | 見え方 | やること |
|---|---|---|
| 移動で引っかかった、目的の場所に着かなかった、進まない | 合図が出ず、上限の時間を過ぎて人に渡される | キーボードで次の区間の頭まで進め、その区間をクリックして「開始」 |
| 早く進みすぎた | まだ会話中なのに次の区間に変わった | すぐ「PC の操作を止める」。今の画面に合う区間をクリックして「開始」 |
| 押し間違えた | 名前の文字違い、メニューの選び違いなど | 「PC の操作を止める」。手で直し、次の区間をクリックして「開始」 |
| 接続が切れた | 「接続切れ」で人に渡される | `q` で終わり、つなぎ直してから `--from <区間>` で起動し直す |

判定はキャプチャボードの色合いや遅れの影響を受けます。合わないときは `za/screens.py` のしきい値や `za/flow.yaml` の `limit` を調整してください。

## ファイル構成

```
common/              ゲームに依らない部分
  controller.py        NUXBT の包み（押す・倒す・全部離す・再接続）
  frames.py            キャプチャと動画のコマ
  dashboard.py         ブラウザのダッシュボード
  keys.py notify.py    端末のキー入力・デスクトップ通知
za/                  Pokémon LEGENDS Z-A 用
  flow.yaml            1周の区間の並び（書き方はファイルの頭のコメント）
  flowplay.py          flow.yaml を読み、合図で区間を進める
  run.py               実機で回す本体
  screens.py           画面の合図の判定としきい値
  templates.json       合図を見る範囲
  refs.npz             判定の見本（輝度の配列）
  routes/              移動の区間で再生する、人の操作の記録
```

## 使っているもの

- [NUXBT](https://github.com/hannahbee91/nuxbt)（MIT License）：Switch のコントローラのふりをする
- OpenCV、NumPy、PyYAML

## ライセンス

MIT License（`LICENSE` を参照）
