# 現在地と続き（NJSS DiluNova）

> このファイルを見れば、次回ここから再開できる。最終更新の状態をまとめる。

## ★最新（2026-08-17）: 案件取得が8日間止まっていた障害と、多層防御の導入

**2026-08-09、官公需API(kkj.go.jp)からの取得が全滅していた**（54,555件 → 0件）。
にもかかわらず**総件数は125,276件のまま**で、ワークフローは毎日 success。8日間気づけなかった。

**原因**: kkj.go.jp がサーバ証明書を更新した際、証明書の発行元
（`JPRS DV RSA CA 2024 G1`）とは**別の中間CA**（`JPRS Domain Validation Authority - G4`）を
送る設定ミスをした。ブラウザやcurlは足りない証明書を自分で取りに行くので気づかないが、
Pythonはそれをしないため `CERTIFICATE_VERIFY_FAILED` で毎回失敗していた。

**なぜ見逃したか（ここが本質）**: このアプリの `--full` は既存データを消さずに積むため、
**主力ソースが完全に死んでも総件数は減らない**。件数の合計を監視していても永久に気づけない。
さらに `_fetch_retry` が例外を握り潰し、ログには「取得0件」としか出なかった。

**対策**:
- 正しい中間CAを `certs/kkj_intermediate.pem` に同梱（**検証を切る回避はしない**）。
  さらに**証明書のAIAから中間CAを自動取得して再試行**する自己修復を入れたので、
  CAがまた変わっても自動で復旧する。
- **判定基準を `data_expectations.py` 1ファイルに集約**。update.py / audit.py / 画面バナー /
  `/api/data-health` / watchdog が全部同じ `inspect()` を呼ぶ。**絶対に基準を分岐させないこと**。
- 見る対象を「総件数」から「**取得元別の件数＋主力ソースだけの鮮度**」に変更。これが本命。
- `preflight.py`（重い取得の前に実接続）/ `data_baseline.json`（前回正常時比で静かな半減を検知）/
  `/api/data-health`（重大は HTTP 503）/ `watchdog.yml`（毎日外から叩く）/
  `alert.yml`（失敗をIssueに残し復旧で自動クローズ・追加シークレット不要）。
- 回帰テスト `test_data_expectations.py`（10件）。**しきい値をべた書きせず期待値から導出**する
  （姉妹アプリ dgss と数字が違うため、べた書きすると持ち込んだ瞬間に落ちる）。
- 障害注入（主力0件・総件数そのまま）で全層が実際に止めることを確認済み。

**姉妹アプリ `dgss`（川野電気版）も同じ原因で同日に復旧済み**（1,997件 → 41,889件）。

## 1. 何ができているか（現在地）

- **公開URL**: https://kawano-njss-modoki.onrender.com （Render無料プラン）
- **GitHubリポジトリ**: https://github.com/syun3032-tech/dgss （private）
- **案件データ**: 全国の電気工事入札 **約2000件**（全47都道府県・関西厚め）。すべて実データ。
- **監視機関**: 全国 **1000機関**（公式入札ページへの導線つき）
- **毎日自動更新**: GitHub Actions が毎日 `update.py --fast` を実行 → DB更新 → push → Render自動再デプロイ。**完全無料**。稼働実績あり（2026-06-11 成功）。

### 機能（NJSS相当）
案件検索（地方→都道府県）／新着／マイ条件マッチング（業種・資格 複数選択）／
**自社の競合企業**（落札者・自社除外・エリア絞り）／入札参加申請の管理／
仕様書の取得可否＋理由／監視機関一覧／**CSVダウンロード**（/export.csv）

## 2. データソース（重要）

| ソース | 実装 | 役割 | 取得方式 |
|---|---|---|---|
| **官公需情報ポータルAPI**（中小企業庁 kkj.go.jp）| `kkj_scraper.py` | **主力**。国・地方・独法を全国横断集約。仕様書添付つき | HTTP+XML（Playwright不要） |
| PPI（i-ppi.jp）| `ppi_scraper.py` | 国の機関＋**落札者=競合データ** | Playwright |
| efftis（京都府）| `kyoto_scraper.py` | 京都府の自治体 | Playwright |
| e-Aichi（愛知県）| `aichi_scraper.py` | 愛知県＋県内 | Playwright |
| PPUBC（堺市・明石市）| `ppubc_scraper.py` | 大阪/兵庫の自治体。INSTANCESにbase追加で拡張 | Playwright |
| 電子入札コアシステム（茨城）| `koukai_scraper.py` | KF00x系 | Playwright |
| 監視機関リスト | `agency_import.py` | クライアント提供スプシ(1000機関) | HTTP(CSV) |

- **スプシ版**（自動巡回・新着）: `gas/kkj_to_sheet.gs`（Google Apps Script）。スプレッドシートに貼って「初期設定」実行で、毎日 官公需API→新着追記。サーバー不要・無料。

## 3. 運用（更新の仕組み）

- **毎日自動（無料）**: GitHub Actions `.github/workflows/update.yml` が `update.py --fast`。
  - `--fast` = 官公需API＋監視機関のみ（HTTPのみ・高速・堅牢）。**官公需APIの行だけ入れ替え、PPI競合や自治体詳細は保持**。
- **手動フル更新**（PPI競合・自治体も全部取り直す）:
  ```bash
  cd kawano-njss-modoki
  python3.13 -m venv .venv && source .venv/bin/activate   # ※python3.14は環境破損
  pip install -r requirements-local.txt && python -m playwright install chromium
  python update.py --reset        # 全ソース取得（数分・Playwright使用）
  git add -A && git commit -m "..." && git push   # → Render自動再デプロイ
  ```

## 4. 次の一手（やるとさらに強くなる順）

1. **官公需「落札結果」API/データ併用** → 競合(落札者)を全国分に拡充（今はPPIのみ）。
   p-portal.go.jp に「落札実績オープンデータ」あり（research参照）。
2. **PPUBC他市の追加**：東大阪/加古川/奈良はefftisでも構造差で未対応。`ppubc_scraper`のexecLink/フォーム検出を分岐対応すれば追加可。
3. **申請管理もlocalStorage永続化**（マイ条件は対策済。同方式で申請も消えないようにできる）。
4. **官公需APIの絞り込み精緻化**：Procedure_Type / Certification(等級) / 日付範囲での絞り込み。

## 5. 既知の制約と対策状況
- Render無料：無アクセス時スリープ（次アクセス~20秒）。料金0。←仕様。
- **マイ条件の永続化＝対策済み**：localStorageに保存し、再デプロイでサーバ側が消えても次回アクセス時に自動でサーバへ復元（base.html/profile.htmlのJS、実機検証済）。
- **API取得失敗時の欠損＝対策済み**：先に取得し成功(>0件)時のみ差し替える方式（update.py）。落ちている時は既存維持。
- 申請管理（申請ステータス）はまだ揮発する（マイ条件と同じ方式でlocalStorage化すれば対策可・未対応）。
- 自治体の個別Playwrightスクレイパーはサイト構造変更で壊れ得る（try/exceptでスキップ）。官公需APIが主力（HTTP）なので日々の安定性に影響なし。

## 6. リサーチ成果物
- `research/platform_roadmap.csv` … 全国1000機関の使用システム分析（どの基盤が何機関カバーか）
- `research/kawano_njss_cases.csv` … 現在の全案件CSV
- `research/RESEARCH.md` … データ強化の方向性
