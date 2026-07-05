# NJSS DiluNova — 新規デプロイ手順

全国向け・認証つき多業種入札SaaS（電気工事 / Web・制作）を **独立リポジトリ**として
GitHub + Render + GitHub Actions で無料デプロイするための手順。

親リポ(njss-soukun)の未追跡フォルダから切り出し、**DiluNova専用のGitHub Release**に
網羅DB（電気＋web業種）を公開して、Renderがそれを取得する構成。

---

## 構成（役割分担）

| 場所 | 役割 |
|---|---|
| **GitHub Actions**（`.github/workflows/update.yml`・毎日6:00 JST） | `update.py --full` で全国網羅DB（電気＋web）を生成 → Release `data-latest` に `denki_bid.db.gz` を公開 → タイムスタンプpushでRender再デプロイ |
| **GitHub Release `data-latest`** | 完成DBの置き場（公開・認証不要でDL可） |
| **Render（web service・free）** | ビルド時に `fetch_db.py` でReleaseのDBをDL（数秒）→ gunicornで起動 |

重い取得はActions側で完結し、Renderは完成DBを落とすだけ＝高速・タイムアウトなし。

---

## 手順

### 1. GitHubリポジトリを作成して push（private 構成）
```bash
cd ~/NJSS無双君/NJSS_DiluNova
gh repo create infodilunova-cpu/njss-dilunova --private --source=. --remote=origin --push
```
> **private の場合**、`fetch_db.py` は Release アセットを GitHub API 経由で **認証DL** する。
> Render に `GH_TOKEN`(repo読取PAT) と `GH_REPO`(owner/repo) を設定すること（手順3）。
> public にするなら `--public` にして、Renderには `DB_RELEASE_URL`(直リンク)だけ設定すればよい。

### 2. 網羅DBを初回生成（Releaseを用意）
Renderが最初のデプロイでDBを取得できるよう、先にActionsを1回走らせてReleaseを作る。
```bash
gh workflow run "データ網羅更新（毎日）" -R infodilunova-cpu/njss-dilunova
# 完了(約30〜50分)後、Release data-latest に denki_bid.db.gz ができる
gh release view data-latest -R infodilunova-cpu/njss-dilunova
```
> Actions は `secrets.GITHUB_TOKEN` で Release 作成・push する（追加設定不要）。

### 3. Render にサービスを作成
- Render ダッシュボード → New → Blueprint → リポジトリ `njss-dilunova` を選択
  （`render.yaml` を自動検出。service名 `njss-dilunova`）
- **Environment 変数**を設定（**private リポ**なので認証DL）:
  | Key | Value |
  |---|---|
  | `GH_TOKEN` | repo読取権のある Personal Access Token（`gh auth token` か GitHub設定で発行・**秘密**） |
  | `GH_REPO` | `infodilunova-cpu/njss-dilunova` |
  | `GH_RELEASE_TAG` | `data-latest`（render.yamlで既定投入済） |
  | `GEMINI_API_KEY` | （AI応募アシストを使う場合のみ・任意） |
  | `AUTH_REQUIRED` | ログイン強制するなら `1`（既定0=無認証で閲覧可） |
  | `SUPABASE_URL` / `SUPABASE_ANON_KEY` | Supabase Auth（Google含む）を使う場合のみ |
  > public リポにした場合はこの3つの代わりに `DB_RELEASE_URL`（Releaseアセット直リンク）を設定。
- `SECRET_KEY`・`FLASK_DEBUG`・`PYTHON_VERSION` は `render.yaml` が自動投入。

### 4. 動作確認
- デプロイ後のURLで `/`（案件一覧）、`/login`、`/signup`（業種選択）を確認。
- 業種切替: URLやアカウントの vertical で `電気工事` / `Web・制作` が切り替わる。

---

## デプロイ後の日次運用
- 以後は毎日6:00(JST)にActionsが網羅DBを再生成→Release更新→Render自動再デプロイ。
- 手動更新: `gh workflow run "データ網羅更新（毎日）" -R infodilunova-cpu/njss-dilunova`

## 注意 / 未完了（本番化の残タスク）
- **ユーザー単位のデータ分離が未実装**（applications/profileが業種内で全ユーザー共有）。
  本格運用前に対応が必要。詳細はメモリ `dilunova-auth-project` 参照。
- 認証ユーザー(users.db)はRender無料の揮発ディスクで消える → Supabase Auth化で永続。
- private リポにする場合の Release DL 認証（上記1の注記）。
