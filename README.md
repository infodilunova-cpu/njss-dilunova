# ⚡ Kawanoさん NJSSモドキ（kawano-njss-modoki）

河野さん向け。**全国の電気工事入札**を、**地方→都道府県**で NJSS 風に絞り込む独立ツール。
**SQLite だけで動く**（Supabase / API キー不要）。デプロイ手順は [DEPLOY.md](DEPLOY.md)。

**公開URL: https://kawano-njss-modoki.onrender.com**（Render無料・毎日自動更新）

> 📌 **次回ここから再開**: 現在地・構成・運用・次の一手・再開手順は [STATUS.md](STATUS.md) を参照。

## 実データと自動更新（運用構成）
- **案件データ**: PPI（i-ppi.jp）全国9地方の国の機関 電気設備工事＋京都府の自治体電気工事＝**全国の実データ**
- **監視機関**: クライアント提供スプレッドシートの**全国1000機関**を取り込み（公式入札ページへ導線）
- **自動更新**: GitHub Actions が毎日 `update.py` を実行→DB更新→push→Render自動再デプロイ（**完全無料**）
- 手動更新: `python update.py --reset`（[update.py](update.py)）

## できること（MVP）

- **地方（8区分）→ 都道府県** の2段フィルタ（NJSS の絞り方を踏襲、連動ドロップダウン）
- 業種 / 入札方式 / キーワード / 並び替え（締切が近い順・公告が新しい順）
- **仕様書の取得可否判定**: 案件ごとに「取得可 / 取得不可 / 未判定」を表示。
  取得不可なら **なぜ取れないか**（要ログイン・窓口受領のみ・有料・交付申請・公開終了 等）を明示
- 案件詳細ページ（発注機関・機関種別・締切・仕様書DLリンク 等）

## データソース

クライアント提示の **入札情報サービス PPI（i-ppi.jp / JACIC運営）** を本命に採用予定。
PPI は全国自治体の工事・委託入札を集約した無料サービスで「電気工事 × 地方公共団体 × 仕様書DL」の要望に合致。

現状、i-ppi.jp の検索はフレーム＋ASP.NET（ViewState）の動的描画のため、
ライブ取得は Playwright での実装が必要（`ppi_scraper.py` に方針と差し込み口あり）。
**実装が済むまではサンプルデータで UI を確認できる。**

## 起動

```bash
cd denki-nyusatsu
python3.13 -m venv .venv && source .venv/bin/activate   # ※python3.14は環境破損のため不可
pip install -r requirements.txt
python app.py
# → http://127.0.0.1:5001
```

> 注: このマシンの `python@3.14`（homebrew）は pyexpat の不具合で壊れているため、
> **python3.13 で venv を作成**してください（検証済み）。

初回起動時、DB が空ならサンプル案件を自動投入します。
手動で入れ直す場合: `python seed_data.py`

## ファイル構成

| ファイル | 役割 |
|---|---|
| `app.py` | Flask 本体・ルーティング |
| `regions.py` | 8地方区分 ⇔ 47都道府県 マッピング |
| `db.py` | SQLite データ層・スキーマ・フィルタ付き検索 |
| `seed_data.py` | サンプル電気工事案件（UI確認用ダミー） |
| `ppi_scraper.py` | PPI ライブ取得の差し込み口（仕様書可否の判定ロジック含む） |
| `templates/`, `static/` | 画面・スタイル |

## 要望の対応状況（全要望 実装完了）

| 要望 | 状態 |
|---|---|
| 地方・都道府県で絞る（NJSS風） | ✅ 連動ドロップダウン・実ブラウザ検証済み |
| 仕様書 取れる/取れない＋理由 | ✅ 取得可/不可/未判定＋理由分類（`_classify_spec`） |
| 入札参加申請の管理 | ✅ `/applications`・案件詳細から状況登録・ステータス絞り込み |
| 競合企業を探す（過去落札者） | ✅ `/competitors` 落札件数ランキング＋会社名表記ゆれ吸収、`/competitor/<名>` 実績一覧 |
| 新着がパッと見れる | ✅ `/?new=1`（直近30日）＋ 一覧に「新着」バッジ |
| 保有資格・対応エリアでマッチング | ✅ `/profile` でマイ条件（対応都道府県・業種・予算上限・等級・資格）→ `/matches` で合致案件をマッチ理由つき表示 |
| PPI ライブ取得 | ✅ `fetch_live()` 実装＋**実データ50件取得・検証済み**（日付ISO化50/50・都道府県推定50/50） |

### PPIライブ取得の使い方

```bash
# 国の機関・入札の経過を落札者(競合)込みで取得 → 案件+競合企業に反映
.venv/bin/python ppi_scraper.py --live --kuni --keika --winner
.venv/bin/python ppi_scraper.py --live                   # 地方公共団体・入札公告
```
※ `python -m playwright install chromium` を初回に実行。
※ 検証済み実績: 国の機関 電気設備工事を50件取得、詳細から実落札者9社(谷口電工/サンエス電気通信/大三洋行 等)を自動抽出し競合企業ページに反映。

## データ更新（ランニングコスト 0 円）

`update.py` がローカルで全ソースを取得して SQLite に保存する。
**有料API・クラウド・サブスクは一切不要**（Playwright と SQLite は無料、PCで動かすだけ）。

```bash
.venv/bin/python update.py --reset                 # 関西中心に作り直し（関西サンプル＋PPI近畿の実データ）
.venv/bin/python update.py                          # 追記更新
.venv/bin/python update.py --reset --koukai 茨城県   # 自治体インスタンスも取り込み
```

無料の定期実行（任意）— macOS の cron でPCが起動中に回すだけ:
```
0 7 * * * cd /path/to/denki-nyusatsu && .venv/bin/python update.py --reset >> update.log 2>&1
```

## 関西の自治体 実データを増やすには（今後）

関西の自治体入札は各府県で**別プラットフォーム**：
- 京都府: `kyoto.efftis.jp`（efftis系。発注機関→工事のドリルダウン構造）
- 大阪府: 府独自の電子調達システム
- 兵庫県ほか: 各市町村の入札情報公開システム

それぞれ専用アダプタが必要（茨城で実証した `koukai_scraper` と同じ要領で増設可能）。
現状は **PPI 近畿（国の機関）の実データ＋関西自治体のサンプル**で関西中心に構成。
高ボリュームの関西自治体実データは、上記アダプタを追加すれば取り込める。

## 自治体の電気工事データ（参考）— 入札情報公開システム

PPI本体は自治体の工事掲載が極少。そこで多くの自治体が使う **入札情報公開システム
（電子入札コアシステム/CALS-EC、URLに `KF00x/KK00x` を含む）** を `koukai_scraper.py` で取得する。
**共通ソフトのため base_url 差し替えで横展開可能**。

```bash
.venv/bin/python koukai_scraper.py 茨城県     # 茨城県の電気工事 発注情報を取得・投入
```

- **検証済み**: 茨城県インスタンスで「電気」発注情報 **182件を実取得・投入**
  （工事名/種別=電気工事/入札方式/公開日/開札日/予定価格/課所名）。
- 他自治体は `koukai_scraper.INSTANCES` に `{url, prefecture}` を追加すれば対応
  （埼玉県・京都府・広島市・香川県・epi-cloud 等、同型システムが多数）。
- 設計図書(仕様書)は詳細ページ(`doEdit`)に在り、`_classify_spec` で取得可否判定に拡張可能（次段）。

## PPI（i-ppi.jp）実機調査メモ（重要）

実ブラウザ（Playwright）で i-ppi.jp の工事入札公告検索を解析した結果:

- **操作フローは完全に判明・動作確認済み**: Index.htm のフレームセット経由で
  `__doPostBack('lbtKojiKokoku')` → `Search.aspx` 検索フォーム
  （機関分類 `drpTopKikanInf` / 地域 `drpKojiDistrict` → 都道府県 `drpKojiPrefecture2`（連動postback）/
  工種区分 `drpKojiKbn`＝電気設備工事 / 業種 `drpKojiGyosyu`＝電気工事 / 等級 `drpTokyu`）
  → `btnSearch`（検索開始）→ 結果テーブル。これを `ppi_scraper.fetch_live()` に実装済み。
- **Search.aspx への直リンクは不可**: ViewState/セッションが Index 経由でしか初期化されないため、
  直リンクすると常に0件。必ず Index から入ること（実装済み）。
- **掲載が限定的**: 機関分類「地方公共団体（都道府県）」の中分類に出るのは調査時点で**岐阜県のみ**。
  多くの自治体は独自の電子入札システムを使い PPI に工事入札公告を出していない。
  → **PPI単独では全国の自治体電気工事を網羅できない。** 各県の電子入札システムや
  クライアントが所属する電気工事組合経由の情報など、追加ソースの検討が必要。
