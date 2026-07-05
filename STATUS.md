# 現在地と続き（Kawanoさん NJSSモドキ）

> このファイルを見れば、次回ここから再開できる。最終更新の状態をまとめる。

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
