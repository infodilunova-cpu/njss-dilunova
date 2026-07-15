"""SQLite データ層（独立ツール用・外部依存なし）。

NJSS無双君 が Supabase を必要とするのに対し、本ツールは
ローカル SQLite だけで完結させる（鍵設定不要・すぐ動く）。

主要関数:
  - init_db()             … スキーマ作成
  - upsert_cases(rows)    … 案件を一括投入（external_id で重複排除）
  - list_cases(...)       … 地方/都道府県/業種/仕様書状態/キーワードで絞り込み
  - get_case(case_id)     … 1件取得
  - distinct_values(col)  … フィルタUI用の候補値
  - count_cases()         … 件数
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import supa  # Supabase永続化（入力データをデプロイ揮発から守る・未設定時は無効）

DB_PATH = Path(__file__).parent / "denki_bid.db"

# Supabaseからの復元中は write-through を止める（書き戻しの無限ループ防止）
_restoring = False

# 仕様書の取得可否ステータス（取れる/取れない/不明）
SPEC_AVAILABLE = "available"      # ダウンロード可能
SPEC_UNAVAILABLE = "unavailable"  # 取得不可（理由は spec_reason）
SPEC_UNKNOWN = "unknown"          # 未判定

# 仕様書が「取れない」理由の分類（取れないなら なんで取れないか）
SPEC_REASONS: dict[str, str] = {
    "login_required": "電子入札システムへのログイン／事業者登録が必要",
    "in_person": "窓口・現地での図書受領のみ（郵送/DL不可）",
    "paid": "有料（実費負担・閲覧のみ）",
    "request_form": "交付申請書の提出後に交付",
    "period_closed": "公開期間が終了している",
    "not_published": "仕様書がWeb未公開（発注機関へ要問合せ）",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL DEFAULT 'PPI',   -- 取得元（PPI など）
    external_id   TEXT    UNIQUE,                    -- 取得元での一意ID
    title         TEXT    NOT NULL,                  -- 案件名
    agency        TEXT    DEFAULT '',                -- 発注機関
    agency_type   TEXT    DEFAULT '',                -- 都道府県/市区町村/国/独法 等
    region        TEXT    DEFAULT '',                -- 地方区分（北海道・東北 等）
    prefecture    TEXT    DEFAULT '',                -- 都道府県
    category      TEXT    DEFAULT '',                -- 業種（電気工事 等）
    procurement_type TEXT DEFAULT '',                -- 調達区分（工事/役務/物品）
    bid_method    TEXT    DEFAULT '',                -- 入札方式（一般競争入札 等）
    announced_date TEXT   DEFAULT '',                -- 公告日（ISO）
    deadline      TEXT    DEFAULT '',                -- 申込締切（ISO）
    detail_url    TEXT    DEFAULT '',                -- 案件詳細URL
    spec_status   TEXT    DEFAULT 'unknown',         -- 仕様書 取得可否
    spec_reason   TEXT    DEFAULT '',                -- 取れない理由コード（SPEC_REASONS）
    spec_url      TEXT    DEFAULT '',                -- 仕様書URL（取得可のとき）
    budget        TEXT    DEFAULT '',                -- 予定価格等（表示用テキスト・任意）
    budget_yen    INTEGER DEFAULT 0,                 -- 予定価格（円・数値。金額フィルタ/整列用）
    winner        TEXT    DEFAULT '',                -- 落札者（競合企業分析の核）
    win_price     TEXT    DEFAULT '',                -- 落札価格
    description   TEXT    DEFAULT '',                -- 案件説明（締切抽出元の自由記述）
    vertical      TEXT    DEFAULT 'denki',           -- 業種テンプレ（denki/web …）。案件の所属業種
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cases_pref     ON cases(prefecture);
CREATE INDEX IF NOT EXISTS idx_cases_region   ON cases(region);
CREATE INDEX IF NOT EXISTS idx_cases_vertical ON cases(vertical);
CREATE INDEX IF NOT EXISTS idx_cases_category ON cases(category);
CREATE INDEX IF NOT EXISTS idx_cases_deadline ON cases(deadline);
-- procurement_type / budget_yen の索引は init_db() のマイグレーション後に作成
-- （既存DBでは列追加が先に必要なため）。

CREATE TABLE IF NOT EXISTS profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- 単一行
    company       TEXT DEFAULT '',   -- 自社名（競合一覧から自社を除外する）
    prefectures   TEXT DEFAULT '',   -- 対応エリア（都道府県, カンマ区切り）
    categories    TEXT DEFAULT '電気工事',  -- 対応業種（カンマ区切り）
    budget_max    TEXT DEFAULT '',   -- 予算上限（予定価格がこれ以下）。空=制限なし
    grade         TEXT DEFAULT '',   -- 経審等級（A〜E, 参考）
    quals         TEXT DEFAULT '',   -- 保有資格メモ
    representative TEXT DEFAULT '',  -- 代表者氏名
    address       TEXT DEFAULT '',   -- 本社所在地
    corp_number   TEXT DEFAULT '',   -- 法人番号
    qualifications TEXT DEFAULT '[]',-- 入札参加資格・等級（機関別, JSON配列）
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applications (
    case_id        INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT '参加申請準備前',  -- APP_STATUSES のいずれか
    applied_date   TEXT DEFAULT '',                 -- 申請日（任意）
    note           TEXT DEFAULT '',                 -- メモ
    assignee       TEXT DEFAULT '',                 -- 担当者（社長／金子さん 等）
    apply_deadline TEXT DEFAULT '',                 -- 参加申請期限（ISO・空なら案件の締切を流用）
    bid_deadline   TEXT DEFAULT '',                 -- 入札書提出期限（ISO）
    open_date      TEXT DEFAULT '',                 -- 開札日（ISO）
    submit_method  TEXT DEFAULT '',                 -- 入札タイプ（電子システム／郵送 等）
    work           TEXT DEFAULT '',                 -- 工事カテゴリ（空なら案件のcategory）
    materials      TEXT DEFAULT '',                 -- 資料受取メモ（例: 6/1以降受取）
    flag           TEXT DEFAULT '',                 -- 要確認フラグの一言（例: 資料待ち）
    needs_check    INTEGER DEFAULT 0,               -- 要確認フラグ（0/1）
    bid_plan       INTEGER DEFAULT 0,               -- 入札予定額（円）
    win_amount     INTEGER DEFAULT 0,               -- 落札額（円）
    award_called   INTEGER DEFAULT 0,               -- 落札連絡済み（0/1）
    partner        TEXT DEFAULT '',                 -- 発注先の協力会社（採用見積の会社名）
    partners       TEXT DEFAULT '[]',               -- 協力会社見積（quotes・JSON配列）
    agency_override TEXT DEFAULT '',                -- 元機関(発注機関)の上書き（案件のagency修正用）
    updated_at     TEXT DEFAULT (datetime('now'))
);

-- AI応募アシストの生成結果キャッシュ（external_id=再採番に強い安定キー）。
-- 同じ案件の再タップで再課金しないために保持する（無料プランでは一切書かれない）。
CREATE TABLE IF NOT EXISTS ai_assist (
    external_id TEXT PRIMARY KEY,     -- 取得元の一意ID
    payload     TEXT NOT NULL,        -- 生成結果(JSON)
    model       TEXT DEFAULT '',      -- 使用モデル
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agencies (
    name        TEXT PRIMARY KEY,    -- 発注機関名
    njss_count  INTEGER DEFAULT 0,   -- NJSS案件数（規模の目安）
    top_url     TEXT DEFAULT '',     -- 公式トップURL
    domain      TEXT DEFAULT '',     -- ドメイン（使用プラットフォームの判別に）
    platform_n  INTEGER DEFAULT 0,   -- 共通基盤_機関数
    bid_url     TEXT DEFAULT '',     -- 公式入札情報ページ
    sample_url  TEXT DEFAULT '',     -- NJSS案件URL例
    fetched_at  TEXT DEFAULT ''      -- 取得日時
);

-- 監視機関のホワイトリスト除外（チェックを外した発注機関＝案件一覧に出さない）。
-- 既定は「全機関ON（除外なし）」。ここに入っている機関名の案件だけ非表示にする。
CREATE TABLE IF NOT EXISTS agency_exclusions (
    name TEXT PRIMARY KEY
);

-- 協力会社マスタ（bid-next-eta の X 配列に相当）。
CREATE TABLE IF NOT EXISTS companies (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,            -- 会社名
    area    TEXT DEFAULT '',          -- 対応エリア
    tags    TEXT DEFAULT '[]',        -- 工事カテゴリ（JSON配列）
    tel     TEXT DEFAULT '',          -- 電話番号
    url     TEXT DEFAULT '',          -- 会社URL
    note    TEXT DEFAULT '',          -- メモ／特徴
    partner INTEGER DEFAULT 0,        -- ★よく頼む（0/1）
    rating  INTEGER DEFAULT 0,        -- 評価（0〜5）
    reviews TEXT DEFAULT '[]'         -- 口コミ（JSON配列）
);
"""

# 入札参加申請の進行ステータス（bid-next-eta＝川野電気システムのカンバン列と完全一致）。
APP_STATUSES: list[str] = [
    "参加申請準備前",   # これから参加申請する
    "入札参加申請済み", # 参加申請を提出済み
    "協力会社探し中",   # 見積を出してくれる協力会社を探している
    "見積取得",         # 協力会社の見積を回収中／取得済み
    "入札書提出済み",   # 入札書を提出した（開札待ち）
    "自社落札",         # 自社が落札
    "他社落札",         # 他社が落札（失注）
    "NG",               # 不参加／見送り
    "見積集まらず",     # 見積が集まらず対応困難
]

# カンバン列のアクセント色（bid-next-eta の B マップと一致）。
STATUS_ACCENT: dict[str, str] = {
    "参加申請準備前": "#9aa3ad",
    "入札参加申請済み": "#2563eb",
    "協力会社探し中": "#0891b2",
    "見積取得": "#7c3aed",
    "入札書提出済み": "#db2777",
    "自社落札": "#16a34a",
    "他社落札": "#64748b",
    "NG": "#dc2626",
    "見積集まらず": "#b45309",
}

# 旧ステータス → 現ステータスの読み替え（既存データ・localStorage退避分の救済）。
STATUS_ALIASES: dict[str, str] = {
    # 直前リリースの暫定名
    "検討中": "参加申請準備前",
    "参加申請準備中": "参加申請準備前",
    "参加申請済": "入札参加申請済み",
    "入札書提出済": "入札書提出済み",
    "落札": "自社落札",
    "見送り": "NG",
    # さらに旧い名前
    "申請準備中": "参加申請準備前",
    "申請済": "入札参加申請済み",
    "入札参加済": "入札書提出済み",
    "不参加": "NG",
}

# 担当者（bid-next-eta の G/$）。色も一致。
ASSIGNEES: list[str] = ["社長", "金子さん", "上西さん", "未割当"]
ASSIGNEE_COLOR: dict[str, str] = {
    "社長": "#16a34a", "金子さん": "#2563eb",
    "上西さん": "#ea580c", "未割当": "#a8a29e",
}

# 工事カテゴリの色（bid-next-eta の V マップと一致）。
WORK_COLOR: dict[str, str] = {
    "電気工事": "#2563eb", "空調": "#0891b2", "照明/LED": "#d97706",
    "防犯/カメラ": "#dc2626", "通信/弱電": "#7c3aed", "管工事": "#0d9488",
    "太陽光": "#ca8a04", "高圧受電": "#4338ca", "リフォーム/建築": "#b45309",
    "制御盤": "#475569", "足場": "#65a30d", "清掃": "#16a34a",
    "IT/システム": "#0ea5e9", "商社/卸": "#9333ea", "土木": "#78716c",
}

# 入札タイプ（提出方法）。
SUBMIT_METHODS: list[str] = ["電子システム", "電子", "郵送", "持参", "郵送/持参"]


def normalize_status(status: str) -> str:
    """旧ステータス名を現行名に読み替える。未知の値はそのまま返す。"""
    s = (status or "").strip()
    return STATUS_ALIASES.get(s, s)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """スキーマを作成（既存なら何もしない）＋軽量マイグレーション。"""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # 既存DBに後から追加した列をマイグレーション（無ければ追加）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(profile)")]
        if "company" not in cols:
            conn.execute("ALTER TABLE profile ADD COLUMN company TEXT DEFAULT ''")
        for col, ddl in (
            ("representative", "TEXT DEFAULT ''"),
            ("address",        "TEXT DEFAULT ''"),
            ("corp_number",    "TEXT DEFAULT ''"),
            ("qualifications", "TEXT DEFAULT '[]'"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE profile ADD COLUMN {col} {ddl}")
        # cases の後付け列をマイグレーション（無ければ追加）
        case_cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
        if "vertical" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN vertical TEXT DEFAULT 'denki'")
        if "description" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN description TEXT DEFAULT ''")
        if "procurement_type" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN procurement_type TEXT DEFAULT ''")
        if "budget_yen" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN budget_yen INTEGER DEFAULT 0")
        # 列追加後に索引を作成（新設列のため SCHEMA からは外してある）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_proctype ON cases(procurement_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_budgetyen ON cases(budget_yen)")
        # applications の後付け列をマイグレーション（無ければ追加）。
        # bid-next-eta（川野電気システム）の管理機能を移植するために拡張した列。
        app_cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)")]
        for col, ddl in (
            ("assignee",       "TEXT DEFAULT ''"),
            ("apply_deadline", "TEXT DEFAULT ''"),
            ("bid_deadline",   "TEXT DEFAULT ''"),
            ("open_date",      "TEXT DEFAULT ''"),
            ("submit_method",  "TEXT DEFAULT ''"),
            ("work",           "TEXT DEFAULT ''"),
            ("materials",      "TEXT DEFAULT ''"),
            ("flag",           "TEXT DEFAULT ''"),
            ("needs_check",    "INTEGER DEFAULT 0"),
            ("bid_plan",       "INTEGER DEFAULT 0"),
            ("win_amount",     "INTEGER DEFAULT 0"),
            ("award_called",   "INTEGER DEFAULT 0"),
            ("partner",        "TEXT DEFAULT ''"),
            ("partners",       "TEXT DEFAULT '[]'"),
            ("agency_override", "TEXT DEFAULT ''"),
        ):
            if col not in app_cols:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
        conn.commit()
    # Supabaseの保存内容をSQLiteへ復元（揮発DB対策）。未設定/不通でも黙って続行。
    try:
        supa.init()
        restore_from_supa()
    except Exception:  # noqa: BLE001 — 永続化層の失敗でアプリ起動を妨げない
        pass


def upsert_cases(rows: list[dict[str, Any]]) -> int:
    """案件を一括投入。external_id が衝突したら上書き更新。投入件数を返す。"""
    cols = [
        "source", "external_id", "title", "agency", "agency_type",
        "region", "prefecture", "category", "procurement_type", "bid_method",
        "announced_date", "deadline", "detail_url", "spec_status", "spec_reason",
        "spec_url", "budget", "budget_yen", "winner", "win_price", "description",
        "vertical",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "external_id")
    sql = (
        f"INSERT INTO cases ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(external_id) DO UPDATE SET {updates}"
    )

    def _val(r: dict[str, Any], c: str) -> Any:
        # budget_yen は数値列。未設定や非数値は 0 に正規化する。
        if c == "budget_yen":
            v = r.get(c, 0)
            try:
                return int(v) if v not in ("", None) else 0
            except (TypeError, ValueError):
                return 0
        return r.get(c, "")

    with _connect() as conn:
        conn.executemany(sql, [tuple(_val(r, c) for c in cols) for r in rows])
        conn.commit()
    return len(rows)


def _as_list(v: Any) -> list[str]:
    """str / list / None を、空要素を除いた文字列リストに正規化する。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


def _build_case_filter(
    *,
    region: str | None = None,
    prefecture: str | None = None,
    category: str | list[str] | None = None,
    procurement_type: str | list[str] | None = None,
    bid_method: str | list[str] | None = None,
    spec_status: str | None = None,
    budget_min: int | None = None,
    open_only: bool = False,
    hide_closed: bool = False,
    q: str = "",
    announced_after: str | None = None,
    exclude_agencies: list[str] | set[str] | None = None,
    vertical: str | None = None,
) -> tuple[str, list[Any]]:
    """絞り込み条件から WHERE句とパラメータを組み立てる（list_cases と件数で共用）。"""
    where: list[str] = []
    params: list[Any] = []

    # 業種テンプレ（denki/web…）で案件を分離。指定時はその業種だけ。
    if vertical:
        where.append("vertical = ?")
        params.append(vertical)

    # 監視機関のチェックを外した発注機関は案件一覧から除外する。
    exc = [a for a in (exclude_agencies or []) if a]
    if exc:
        where.append("agency NOT IN (%s)" % ",".join("?" * len(exc)))
        params.extend(exc)

    if prefecture:
        where.append("prefecture = ?")
        params.append(prefecture)
    elif region:
        where.append("region = ?")
        params.append(region)

    # 業種・区分・入札方式は複数選択（OR）に対応
    for col, val in (("category", category), ("procurement_type", procurement_type),
                     ("bid_method", bid_method)):
        vals = _as_list(val)
        if vals:
            where.append(f"{col} IN (%s)" % ",".join("?" * len(vals)))
            params.extend(vals)

    if spec_status:
        where.append("spec_status = ?")
        params.append(spec_status)
    if budget_min:
        where.append("budget_yen >= ?")
        params.append(int(budget_min))
    if open_only:
        # 締切が判明していて、かつ今日以降のものだけ＝実際に応募できる案件
        where.append("deadline != '' AND deadline >= date('now', 'localtime')")
    if hide_closed:
        # 終了(締切が過去)を隠す＝「募集中」タブ＝今申し込める案件だけを見せる。
        # 締切未定('')は "落札者が未確定 かつ 公告45日以内の新しいものだけ" 残す。
        # 古い未定案件は実質終了であることが多く、また落札者(winner)が入っている行は
        # 落札実績＝結果データで応募できないため、締切未定でも必ず隠す
        # （調達ポータル落札実績16,000件超が新着・一覧に紛れ込むのを防ぐ）。
        where.append(
            "(deadline >= date('now', 'localtime') "
            "OR (deadline = '' AND winner = '' "
            "AND announced_date >= date('now', 'localtime', '-45 days')))"
        )
    if announced_after:
        # 新着フィルタ（公告日がこの日以降）。SQL側で行うことで件数も正確・上限の影響を受けない。
        where.append("announced_date != '' AND announced_date >= ?")
        params.append(announced_after)
    # キーワードは空白/カンマ区切りで複数可。各語が title/agency いずれかに一致（語間OR）
    terms = [t for t in q.replace("，", ",").replace("、", ",").replace(",", " ").split() if t]
    if terms:
        ors = " OR ".join("(title LIKE ? OR agency LIKE ?)" for _ in terms)
        where.append(f"({ors})")
        for t in terms:
            params.extend([f"%{t}%", f"%{t}%"])

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def count_list_cases(**filters: Any) -> int:
    """list_cases と同じ絞り込み条件に該当する件数（上限なしの実数）。"""
    # sort/limit/offset は件数に無関係なので除外
    for k in ("sort", "limit", "offset"):
        filters.pop(k, None)
    clause, params = _build_case_filter(**filters)
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM cases {clause}", params).fetchone()[0]


def list_cases(
    *,
    region: str | None = None,
    prefecture: str | None = None,
    category: str | list[str] | None = None,
    procurement_type: str | list[str] | None = None,
    bid_method: str | list[str] | None = None,
    spec_status: str | None = None,
    budget_min: int | None = None,
    open_only: bool = False,
    hide_closed: bool = False,
    q: str = "",
    announced_after: str | None = None,
    exclude_agencies: list[str] | set[str] | None = None,
    vertical: str | None = None,
    sort: str = "deadline",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """条件で案件を絞り込む（NJSS風の段階フィルタ）。

    都道府県が指定されればそれを優先、無ければ地方区分で絞る。
      - category / procurement_type / bid_method: str でも list でも可（list は OR）
      - budget_min: 予定価格(円)がこれ以上（1000万＝10000000 等）
      - open_only: 締切が今日以降の「今応募できる」案件のみ
      - hide_closed: 締切が過去の「終了」案件を隠す（締切不明・今後分は残す）
      - announced_after: 公告日がこの日以降（新着フィルタ）
      - q: 空白/カンマ区切りで複数キーワード可（いずれか一致＝OR）
    """
    clause, params = _build_case_filter(
        region=region, prefecture=prefecture, category=category,
        procurement_type=procurement_type, bid_method=bid_method,
        spec_status=spec_status, budget_min=budget_min, open_only=open_only,
        hide_closed=hide_closed, q=q, announced_after=announced_after,
        exclude_agencies=exclude_agencies, vertical=vertical,
    )
    # 締切が空文字の案件は末尾に回す
    order = {
        "deadline": "CASE WHEN deadline = '' THEN 1 ELSE 0 END, deadline ASC",
        "announced": "announced_date DESC",
        "budget": "budget_yen DESC",
    }.get(sort, "deadline ASC")

    sql = f"SELECT * FROM cases {clause} ORDER BY {order} LIMIT ? OFFSET ?"
    params = list(params) + [limit, offset]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_case(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None


def get_ai_assist(external_id: str) -> dict[str, Any] | None:
    """AI応募アシストのキャッシュ結果を返す（無ければ None）。payload はJSON文字列のまま。"""
    if not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, model, created_at FROM ai_assist WHERE external_id = ?",
            (external_id,)).fetchone()
        return dict(row) if row else None


def set_ai_assist(external_id: str, payload: str, model: str = "") -> None:
    """AI応募アシストの結果をキャッシュに保存（external_id で上書き）。"""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ai_assist (external_id, payload, model, created_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(external_id) DO UPDATE SET
                 payload=excluded.payload, model=excluded.model,
                 created_at=excluded.created_at""",
            (external_id, payload, model))
        conn.commit()


def get_case_id_by_external(external_id: str) -> int | None:
    """external_id（取得元の安定ID）から現在の案件id を引く。

    案件の整数idは日次のDB再構築で採番し直されるが external_id は不変。
    ブラウザ保存した申請をサーバへ復元する際の安定キーとして使う。
    """
    if not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE external_id = ?", (external_id,)).fetchone()
        return row[0] if row else None


def update_winner_by_external(external_id: str, winner: str,
                              win_price: str = "") -> bool:
    """既存案件（external_id 一致）の落札者・落札価格だけを更新する。

    落札結果を「別案件として重複追加」せず既存公告案件へ後付けしたい場合に使う
    最小ヘルパ。external_id が一致する案件が無ければ何もせず False を返す。
    （調達ポータル落札実績は案件番号体系が官公需APIと異なり安全に突合できないため
    現状は別レコードとして upsert している。将来突合可能になった時の更新口として用意。）
    """
    if not external_id or not winner:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE cases SET winner = ?, win_price = ? WHERE external_id = ?",
            (winner, win_price, external_id),
        )
        conn.commit()
        return cur.rowcount > 0


def distinct_values(column: str) -> list[str]:
    """フィルタUIの候補値（指定カラムの非空ユニーク値）。"""
    allowed = {"category", "procurement_type", "bid_method", "prefecture",
               "region", "agency_type"}
    if column not in allowed:
        raise ValueError(f"許可されていないカラム: {column}")
    sql = f"SELECT DISTINCT {column} FROM cases WHERE {column} != '' ORDER BY {column}"
    with _connect() as conn:
        return [r[0] for r in conn.execute(sql).fetchall()]


def count_cases(source: str | None = None) -> int:
    """案件の総数。source 指定でその取得元のみ数える。"""
    with _connect() as conn:
        if source:
            return conn.execute(
                "SELECT COUNT(*) FROM cases WHERE source = ?", (source,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]


def upsert_agencies(rows: list[dict[str, Any]]) -> int:
    """監視対象の発注機関を一括投入（name で重複排除）。"""
    cols = ["name", "njss_count", "top_url", "domain", "platform_n",
            "bid_url", "sample_url", "fetched_at"]
    ph = ", ".join(["?"] * len(cols))
    upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "name")
    sql = (f"INSERT INTO agencies ({', '.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT(name) DO UPDATE SET {upd}")
    with _connect() as conn:
        conn.executemany(sql, [tuple(r.get(c, "") for c in cols) for r in rows])
        conn.commit()
    return len(rows)


# --- 監視機関のホワイトリスト除外（チェックを外した機関の案件を一覧から消す）---

def list_agency_exclusions() -> set[str]:
    """案件一覧から除外する発注機関名の集合。"""
    with _connect() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM agency_exclusions")}


def set_agency_excluded(name: str, excluded: bool) -> None:
    """1機関の除外ON/OFF（excluded=True で除外＝チェックを外す）。"""
    name = (name or "").strip()
    if not name:
        return
    with _connect() as conn:
        if excluded:
            conn.execute("INSERT OR IGNORE INTO agency_exclusions (name) VALUES (?)", (name,))
        else:
            conn.execute("DELETE FROM agency_exclusions WHERE name = ?", (name,))
        conn.commit()
    _push_exclusions()


def replace_agency_exclusions(names: list[str]) -> None:
    """除外リストを丸ごと置き換える（localStorage からの復元用）。"""
    clean = [str(n).strip() for n in (names or []) if str(n).strip()]
    with _connect() as conn:
        conn.execute("DELETE FROM agency_exclusions")
        if clean:
            conn.executemany("INSERT OR IGNORE INTO agency_exclusions (name) VALUES (?)",
                             [(n,) for n in clean])
        conn.commit()
    _push_exclusions()


def list_agencies(q: str = "") -> list[dict[str, Any]]:
    """監視対象の発注機関一覧（案件数の多い順）。"""
    where, params = "", []
    if q:
        where = "WHERE name LIKE ? OR domain LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM agencies {where} ORDER BY njss_count DESC", params).fetchall()]


def count_agencies() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]


@lru_cache(maxsize=4096)
def find_agency_for_case(agency_name: str) -> dict[str, Any] | None:
    """案件の発注機関名から agencies テーブルの機関情報を探す。

    官公需APIの形式 "大阪府吹田市" → agencies "吹田市役所" のようなマッチを行う。
    案件詳細を開くたびに呼ばれるため lru_cache で結果を再利用する（データは
    再デプロイ時のみ変わる＝プロセス再起動でキャッシュも自然に更新される）。

    誤マッチ防止: 短い汎用キーワード（2〜3文字）での当て推量は誤った発注機関＝
    誤った入札ポータルへの誘導につながるため、段階4は4文字以上のときだけ使う。
    """
    if not agency_name:
        return None

    with _connect() as conn:
        # 1) 完全一致
        row = conn.execute(
            "SELECT * FROM agencies WHERE name = ?", (agency_name,)
        ).fetchone()
        if row:
            return dict(row)

        # 2) 部分一致: 案件の機関名が agencies.name に含まれる or 逆
        row = conn.execute(
            "SELECT * FROM agencies WHERE name LIKE ? OR ? LIKE '%' || name || '%' "
            "ORDER BY njss_count DESC LIMIT 1",
            (f"%{agency_name}%", agency_name),
        ).fetchone()
        if row:
            return dict(row)

        # 3) 官公需API形式 "都道府県名+市町村名" → 市町村名で検索
        m = re.search(r'[都道府県](.+?[市町村区])$', agency_name)
        if m:
            city = m.group(1)
            row = conn.execute(
                "SELECT * FROM agencies WHERE name LIKE ? ORDER BY njss_count DESC LIMIT 1",
                (f"%{city}%",),
            ).fetchone()
            if row:
                return dict(row)

        # 4) 省庁名の先頭キーワードでマッチ（4文字以上のときのみ＝誤マッチ防止）
        keyword = agency_name.split("／")[0].split(" ")[0][:8]
        if len(keyword) >= 4:
            row = conn.execute(
                "SELECT * FROM agencies WHERE name LIKE ? ORDER BY njss_count DESC LIMIT 1",
                (f"%{keyword}%",),
            ).fetchone()
            if row:
                return dict(row)

    return None


CSV_COLUMNS = ["id", "source", "prefecture", "region", "agency", "agency_type",
               "title", "category", "bid_method", "announced_date", "deadline",
               "budget", "spec_status", "spec_reason", "winner", "win_price",
               "detail_url", "external_id"]


def export_cases_csv() -> str:
    """全案件を CSV 文字列にして返す（強化済みDBの書き出し用）。"""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    with _connect() as conn:
        for r in conn.execute(
            f"SELECT {', '.join(CSV_COLUMNS)} FROM cases "
            f"ORDER BY prefecture, announced_date DESC"
        ).fetchall():
            w.writerow([r[c] for c in CSV_COLUMNS])
    return out.getvalue()


def clear_cases(source: str | None = None) -> int:
    """案件を削除（source指定でその取得元のみ）。削除件数を返す。

    関連する applications も case 削除で連鎖（ON DELETE CASCADE 相当）するよう
    手動で掃除する。
    """
    with _connect() as conn:
        if source:
            n = conn.execute("DELETE FROM cases WHERE source = ?", (source,)).rowcount
        else:
            n = conn.execute("DELETE FROM cases").rowcount
        # 孤立した applications を掃除
        conn.execute("DELETE FROM applications WHERE case_id NOT IN (SELECT id FROM cases)")
        conn.commit()
    return n


# ============================================================
# 入札参加申請（applications）
# ============================================================

def _normalize_partners(partners: Any) -> str:
    """協力会社見積(quotes)を検証してJSON文字列にする。

    各社 {company, tel, area, amount, requested, replied, feasible, selected, note}。
    会社名が空のものは捨てる。常に妥当なJSON配列文字列を返す。
    """
    import json
    if isinstance(partners, str):
        try:
            items = json.loads(partners or "[]")
        except (ValueError, TypeError):
            items = []
    elif isinstance(partners, list):
        items = partners
    else:
        items = []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        company = str(it.get("company", "")).strip()
        if not company:
            continue
        # feasible は3状態: 1=可 / -1=否(断り) / 0=未回答（bid-next の可否UIに合わせる）
        fe = it.get("feasible")
        feasible = 1 if fe in (1, "1", "yes", True) else (-1 if fe in (-1, "-1", "no") else 0)
        cleaned.append({
            "company": company,
            "tel": str(it.get("tel", "")).strip(),
            "area": str(it.get("area", "")).strip(),
            "amount": str(it.get("amount", "")).strip(),
            "requested": 1 if it.get("requested") else 0,
            "replied": 1 if it.get("replied") else 0,
            "feasible": feasible,
            "selected": 1 if it.get("selected") else 0,
            "note": str(it.get("note", "")).strip(),
        })
    return json.dumps(cleaned, ensure_ascii=False)


# set_application が受け付ける列（case_id/status 以外）。型変換つき。
_APP_TEXT_FIELDS = (
    "applied_date", "note", "assignee", "apply_deadline", "bid_deadline",
    "open_date", "submit_method", "work", "materials", "flag", "partner",
    "agency_override",  # 元機関(発注機関)の手書き上書き（案件のagencyが不正確な時に修正）
)
_APP_INT_FIELDS = ("needs_check", "bid_plan", "win_amount", "award_called")


def delete_application(case_id: int) -> None:
    """案件を申請管理から外す（applications行を削除）。案件(cases)自体は残す。"""
    with _connect() as conn:
        conn.execute("DELETE FROM applications WHERE case_id = ?", (case_id,))
        conn.commit()
    _push_applications()


# ============================================================
# Supabase 永続化（write-through ＋ 起動時復元）
# ============================================================

def _applications_for_supa() -> list[dict[str, Any]]:
    """全申請を external_id つきで取り出す（Supabase保存用・案件ID振り直しに強い）。"""
    sql = """
        SELECT c.external_id AS external_id, a.*
        FROM applications a JOIN cases c ON c.id = a.case_id
        WHERE c.external_id IS NOT NULL AND c.external_id != ''
    """
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    out = []
    for r in rows:
        r.pop("case_id", None)
        r = _hydrate_application(r)  # partners を list に
        out.append(r)
    return out


def _push_applications() -> None:
    if _restoring:
        return
    supa.save("applications", _applications_for_supa())


def _push_companies() -> None:
    if _restoring:
        return
    supa.save("companies", list_companies())


def _push_profile() -> None:
    if _restoring:
        return
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row:
        supa.save("profile", _hydrate_profile(dict(row)))


def _push_exclusions() -> None:
    if _restoring:
        return
    supa.save("agency_exclusions", sorted(list_agency_exclusions()))


def restore_from_supa() -> dict[str, int]:
    """起動時：Supabaseの保存内容を SQLite へ流し込む（Supabaseが真の保存先）。

    すべて失敗しても例外は投げない（アプリは動き続ける）。
    """
    global _restoring
    if not supa.enabled():
        return {}
    counts = {"applications": 0, "companies": 0, "profile": 0, "exclusions": 0}
    _restoring = True
    try:
        # 申請（external_id → 現在の case_id に解決して投入）
        apps = supa.load("applications")
        if isinstance(apps, list):
            for it in apps:
                ext = (it.get("external_id") or "").strip()
                if not ext:
                    continue
                cid = get_case_id_by_external(ext)
                if cid is None:
                    continue
                fields = {k: it.get(k) for k in (
                    "applied_date", "note", "assignee", "apply_deadline", "bid_deadline",
                    "open_date", "submit_method", "work", "materials", "flag",
                    "needs_check", "bid_plan", "win_amount", "award_called", "partner", "partners")}
                try:
                    set_application(cid, it.get("status") or "参加申請準備前", **fields)
                    counts["applications"] += 1
                except ValueError:
                    pass
        # 協力会社（全消し→投入）。空/欠損のときは消さない（誤って全消しする事故を防ぐ）。
        comps = supa.load("companies")
        if isinstance(comps, list) and comps:
            with _connect() as conn:
                conn.execute("DELETE FROM companies")
                conn.commit()
            for c in comps:
                c.pop("id", None)
                upsert_company(c)
                counts["companies"] += 1
        # マイ条件
        prof = supa.load("profile")
        if isinstance(prof, dict) and (prof.get("company") or prof.get("qualifications")):
            save_profile(
                prefectures=prof.get("prefectures", ""),
                categories=prof.get("categories", "電気工事"),
                budget_max=prof.get("budget_max", ""),
                grade=prof.get("grade", ""), quals=prof.get("quals", ""),
                company=prof.get("company", ""), representative=prof.get("representative", ""),
                address=prof.get("address", ""), corp_number=prof.get("corp_number", ""),
                qualifications=prof.get("qualifications", []))
            counts["profile"] = 1
        # 監視機関の除外。空/欠損のときは置換しない（既存を消さない）。
        exc = supa.load("agency_exclusions")
        if isinstance(exc, list) and exc:
            replace_agency_exclusions(exc)
            counts["exclusions"] = len(exc)
    except Exception as e:  # noqa: BLE001 — 復元失敗でアプリを落とさない
        import logging
        logging.getLogger(__name__).warning("supa restore failed: %s", e)
    finally:
        _restoring = False
    return counts


def set_application(case_id: int, status: str, **fields: Any) -> None:
    """案件の入札参加申請ステータスと管理項目を登録・更新する。

    fields には _APP_TEXT_FIELDS / _APP_INT_FIELDS と partners(quotes) を渡せる。
    未指定の列はデフォルト（空文字 / 0 / '[]'）になる。
    """
    status = normalize_status(status)
    if status not in APP_STATUSES:
        raise ValueError(f"不正なステータス: {status}")

    def _int(v: Any) -> int:
        if isinstance(v, bool):
            return 1 if v else 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    cols = ["status"]
    vals: list[Any] = [status]
    for f in _APP_TEXT_FIELDS:
        cols.append(f)
        vals.append(str(fields.get(f, "") or "").strip())
    for f in _APP_INT_FIELDS:
        cols.append(f)
        vals.append(_int(fields.get(f, 0)))
    cols.append("partners")
    vals.append(_normalize_partners(fields.get("partners", [])))

    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    placeholders = ", ".join(["?"] * (len(cols) + 1))  # +case_id
    with _connect() as conn:
        conn.execute(
            f"""INSERT INTO applications (case_id, {', '.join(cols)}, updated_at)
                VALUES ({placeholders}, datetime('now'))
                ON CONFLICT(case_id) DO UPDATE SET
                  {set_clause}, updated_at=datetime('now')""",
            (case_id, *vals),
        )
        conn.commit()
    _push_applications()


def _hydrate_application(row: dict[str, Any]) -> dict[str, Any]:
    """DB行の partners(JSON文字列) を list に展開して返す。"""
    import json
    try:
        row["partners"] = json.loads(row.get("partners") or "[]")
    except (ValueError, TypeError):
        row["partners"] = []
    return row


def get_application(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM applications WHERE case_id = ?", (case_id,)
        ).fetchone()
        return _hydrate_application(dict(row)) if row else None


def list_applications(status: str | None = None) -> list[dict[str, Any]]:
    """申請管理一覧（案件情報をJOINして返す）。新しい更新順。"""
    sql = """
        SELECT a.*,
               c.title,
               COALESCE(NULLIF(a.agency_override, ''), c.agency) AS agency,
               c.agency_type, c.region, c.prefecture,
               c.category, c.deadline, c.announced_date, c.detail_url,
               c.external_id, c.budget, c.winner, c.win_price, c.spec_status
        FROM applications a
        JOIN cases c ON c.id = a.case_id
    """
    params: list[Any] = []
    if status:
        sql += " WHERE a.status = ?"
        params.append(status)
    sql += " ORDER BY a.updated_at DESC"
    with _connect() as conn:
        return [_hydrate_application(dict(r)) for r in conn.execute(sql, params).fetchall()]


# ============================================================
# 協力会社マスタ（companies）
# ============================================================

def _hydrate_company(row: dict[str, Any]) -> dict[str, Any]:
    import json
    for k in ("tags", "reviews"):
        try:
            row[k] = json.loads(row.get(k) or "[]")
        except (ValueError, TypeError):
            row[k] = []
    row["partner"] = bool(row.get("partner"))
    return row


def list_companies() -> list[dict[str, Any]]:
    """協力会社一覧（★よく頼む→評価の高い順）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM companies ORDER BY partner DESC, rating DESC, name ASC"
        ).fetchall()
    return [_hydrate_company(dict(r)) for r in rows]


def upsert_company(data: dict[str, Any]) -> int:
    """協力会社を登録／更新する。id があれば更新。会社IDを返す。"""
    import json
    cid = data.get("id")
    vals = (
        str(data.get("name", "")).strip(),
        str(data.get("area", "")).strip(),
        json.dumps([t for t in (data.get("tags") or []) if t], ensure_ascii=False),
        str(data.get("tel", "")).strip(),
        str(data.get("url", "")).strip(),
        str(data.get("note", "")).strip(),
        1 if data.get("partner") else 0,
        int(data.get("rating") or 0),
        json.dumps([r for r in (data.get("reviews") or []) if r], ensure_ascii=False),
    )
    with _connect() as conn:
        if cid:
            conn.execute(
                """UPDATE companies SET name=?, area=?, tags=?, tel=?, url=?,
                   note=?, partner=?, rating=?, reviews=? WHERE id=?""",
                (*vals, int(cid)),
            )
            conn.commit()
            ret = int(cid)
        else:
            cur = conn.execute(
                """INSERT INTO companies (name, area, tags, tel, url, note, partner, rating, reviews)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", vals,
            )
            conn.commit()
            ret = int(cur.lastrowid)
    _push_companies()
    return ret


def delete_company(company_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM companies WHERE id=?", (company_id,))
        conn.commit()
    _push_companies()


def count_companies() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


# ============================================================
# 競合企業（落札者の分析）
# ============================================================

def normalize_company(name: str) -> str:
    """会社名を集計用に正規化する（表記ゆれを吸収して精度を上げる）。

    例: 「(株)山田電気」「株式会社 山田電気」「山田電気㈱」→「山田電気」
    """
    import re
    n = name.strip()
    # 法人格表記を除去
    n = re.sub(r"株式会社|有限会社|合同会社|\(株\)|（株）|㈱|\(有\)|（有）|㈲", "", n)
    # 空白・全角空白を除去
    n = re.sub(r"[\s　]+", "", n)
    return n


def list_competitors(q: str = "", prefecture: str = "",
                     prefectures: list[str] | None = None,
                     exclude_company: str = "") -> list[dict[str, Any]]:
    """落札者を企業ごとに集計（落札件数の多い順）。

    自社の競合を見るため:
      - prefectures: 自社の対応エリア（複数）に絞る
      - exclude_company: 自社名を一覧から除外（表記ゆれ吸収）
    prefecture（単数）は手動の追加絞り込み用。
    """
    where = ["winner != ''"]
    params: list[Any] = []
    if prefectures:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefectures)))
        params.extend(prefectures)
    if prefecture:
        where.append("prefecture = ?")
        params.append(prefecture)
    clause = "WHERE " + " AND ".join(where)
    sql = f"""
        SELECT winner,
               COUNT(*)                          AS wins,
               COUNT(DISTINCT prefecture)        AS pref_count,
               GROUP_CONCAT(DISTINCT prefecture) AS prefectures,
               GROUP_CONCAT(DISTINCT agency)     AS agencies
        FROM cases {clause}
        GROUP BY winner
        ORDER BY wins DESC, winner ASC
    """
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if exclude_company:
        ex = normalize_company(exclude_company)
        rows = [r for r in rows if normalize_company(r["winner"]) != ex]
    if q:
        nq = normalize_company(q)
        rows = [r for r in rows if nq in normalize_company(r["winner"])]
    return rows


def competitor_cases(winner: str) -> list[dict[str, Any]]:
    """指定した落札者（競合企業）の落札案件一覧。"""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM cases WHERE winner = ? ORDER BY announced_date DESC", (winner,)
        ).fetchall()]


# ============================================================
# 入札額ガイド（落札実績の統計・AI不使用）
# ============================================================

# 統計を出す最小サンプル数。これ未満の層は None（当てにならない数字を出さない）。
_PRICE_GUIDE_MIN = 3


def _win_price_yen(s: str) -> int | None:
    """落札価格の表示文字列（"1,073,500,000円" / "100.50円" 等）を円(int)にする。

    数字が取れない・0円以下は None（統計に混ぜない）。
    ※ yen_to_int は "100.50" を 10050 と誤読するため、小数対応の専用パーサ。
    """
    m = re.search(r"[\d,]+(?:\.\d+)?", s or "")
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    n = int(round(v))
    return n if n > 0 else None


def _percentile(sorted_vals: list, q: float) -> float:
    """昇順リストの分位点 q(0〜1) を線形補間で求める（numpyの既定と同等の単純実装）。"""
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _price_stats(prices: list[int]) -> dict[str, Any] | None:
    """落札額リストの件数・中央値・25/75パーセンタイル。件数不足なら None。"""
    if len(prices) < _PRICE_GUIDE_MIN:
        return None
    s = sorted(prices)
    return {
        "count": len(s),
        "median": int(round(_percentile(s, 0.5))),
        "p25": int(round(_percentile(s, 0.25))),
        "p75": int(round(_percentile(s, 0.75))),
    }


def price_guide(category: str, agency: str) -> dict[str, Any] | None:
    """同カテゴリの落札実績から入札額の目安統計を返す（決定的・AI不使用）。

    cases の落札実績（winner と win_price が入っている行）を母集団に、
      - category_stats … 同カテゴリ全体の 件数/中央値/25/75パーセンタイル
      - agency_stats   … そのうち同一発注機関（agency 完全一致）分の同統計
                          （※機関だけ一致でカテゴリ違いの実績は入札額の参考に
                            ならないため、同カテゴリ内に絞って集計する）
      - win_rate       … 予定価格(budget_yen>0)が分かる実績での 落札額/予定価格
                          の中央値（落札率）。落札実績側は budget_yen=0 が大半
                          なので、比較できるペアが _PRICE_GUIDE_MIN 件未満なら None
    を計算する。件数が _PRICE_GUIDE_MIN 件未満の層は None（誤誘導を防ぐ）。
    どの層も出せなければ全体を None で返す。
    """
    category = (category or "").strip()
    agency = (agency or "").strip()
    if not category:
        return None
    with _connect() as conn:
        rows = conn.execute(
            "SELECT agency, win_price, budget_yen FROM cases "
            "WHERE category = ? AND winner != '' AND win_price != ''",
            (category,)).fetchall()

    cat_prices: list[int] = []
    agc_prices: list[int] = []
    ratios: list[float] = []
    for r in rows:
        yen = _win_price_yen(r["win_price"])
        if yen is None:
            continue
        cat_prices.append(yen)
        if agency and r["agency"] == agency:
            agc_prices.append(yen)
        b = r["budget_yen"] or 0
        if b > 0:
            ratios.append(yen / b)

    win_rate = None
    if len(ratios) >= _PRICE_GUIDE_MIN:
        win_rate = {"count": len(ratios),
                    "median": round(_percentile(sorted(ratios), 0.5), 3)}
    guide = {
        "category": category,
        "agency": agency,
        "category_stats": _price_stats(cat_prices),
        "agency_stats": _price_stats(agc_prices),
        "win_rate": win_rate,
    }
    if not (guide["category_stats"] or guide["agency_stats"] or win_rate):
        return None
    return guide


# ============================================================
# マイ条件（プロフィール）とマッチング
# ============================================================

def yen_to_int(s: str) -> int | None:
    """「166,518,000円」等を整数に。数字が無ければ None。"""
    import re
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def _hydrate_profile(row: dict[str, Any]) -> dict[str, Any]:
    import json
    try:
        row["qualifications"] = json.loads(row.get("qualifications") or "[]")
    except (ValueError, TypeError):
        row["qualifications"] = []
    for k in ("representative", "address", "corp_number"):
        row.setdefault(k, "")
    return row


# 初期値（資格通知書PDFから抽出した川野電気の発注機関別 等級）。
# 本番DBは毎日再生成され profile 行が消えるため、未設定時はこの既定値を表示・AI照合に使う。
# (issuer, (elecCat,grade,score), (pipeCat,grade,score), valid, number, note)
_DEFAULT_QUAL_MAIN = [
    ("国土交通省（建設工事）", ("電気工事", "B", "621"), ("管工事", "B", "559"), "令和9年3月31日", "整理070100053", "国交省の建設工事資格"),
    ("国土交通省（地方整備局・官庁営繕／一元化資格）", ("電気設備工事", "C", "621"), ("暖冷房衛生工事", "C", "559"), "令和9年3月31日", "業者86-0306424", "関東/近畿/中部/中国/四国/九州/東北/北陸整備局・国総研・営繕で共通。維持修繕/受変電/橋梁補修=532"),
    ("法務省", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録60667", "大臣官房施設課"),
    ("財務省 東海財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付770045", "東海財務局/名古屋税関/名古屋国税局"),
    ("財務省 福岡財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付120069", "福岡財務支局/門司・長崎税関/福岡国税局"),
    ("財務省 中国財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付700169", "中国財務局/広島国税局"),
    ("財務省 四国財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付875544", "四国財務局/高松国税局"),
    ("文部科学省", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和7・8年度", "受付770819", "文教施設企画・防災部"),
    ("厚生労働省", ("電気", "D", "621"), ("管", "D", "559"), "令和9年3月31日", "登録115-260059", "全国ブロック。※C相当点でもD等級"),
    ("環境省", ("電気設備工事", "C", "621"), ("機械設備工事", "C", "559"), "令和9年3月31日", "登録001-000448", "全国"),
    ("総務省", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "受付0700101545", ""),
    ("農林水産省（大臣官房）", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録00451", ""),
    ("経済産業省（近畿経済産業局）", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "登録0803170079", "全国地区"),
    ("防衛省", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "登録2-06-04701", "整備計画局"),
    ("沖縄総合事務局", ("電気設備工事", "C", "621"), ("暖冷房衛生設備工事", "C", "559"), "令和9年3月31日", "業者180571", "受変電532"),
    ("衆議院", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録3125", ""),
    ("参議院", ("電気工事", "C", ""), ("管工事", "C", ""), "令和9年3月31日", "登録1562", ""),
    ("最高裁判所", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "業者0062555/受付16889", ""),
    ("独立行政法人 都市再生機構（UR）", ("電気工事", "C", "621"), ("管工事", "C", "559"), "2027年3月31日", "登録0217290", "関西地区"),
    ("独立行政法人 水資源機構", ("電気", "", "621"), ("管", "", "559"), "令和9年3月31日", "業者152064", "等級表記○/順位3381"),
    ("大阪府", ("電気工事", "D", "692"), ("管工事", "D", "593"), "令和9年3月31日", "業者7094979", "等級・点数は令和8/3/31まで有効"),
    ("八尾市", ("電気工事", "D", "592"), ("管工事", "D", "493"), "令和7年度", "市内業者", "格付表"),
]
_DEFAULT_QUAL_NOGRADE = [
    ("農林水産省 各地方農政局", "令和9年3月31日", "登録20250C…", "関東/近畿/九州/中国四国/東海/東北/北陸。電気・管=有資格（格付なし）"),
    ("林野庁（近畿中国森林管理局）", "令和9年3月31日", "登録N09865", "電気・管=有資格（格付なし）"),
    ("外務省", "令和9年3月31日", "登録070800100678", "電気・管=有資格（格付なし）"),
]


def default_qualifications() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for issuer, e, m, valid, num, note in _DEFAULT_QUAL_MAIN:
        out.append({"issuer": issuer, "category": e[0], "grade": e[1], "score": e[2],
                    "valid_until": valid, "number": num, "note": note})
        out.append({"issuer": issuer, "category": m[0], "grade": m[1], "score": m[2],
                    "valid_until": valid, "number": num, "note": ""})
    for issuer, valid, num, note in _DEFAULT_QUAL_NOGRADE:
        out.append({"issuer": issuer, "category": "電気工事/管工事", "grade": "", "score": "",
                    "valid_until": valid, "number": num, "note": note})
    return out


def default_profile() -> dict[str, Any]:
    """初期プロフィール（汎用・空）。会社情報や等級はアカウントごとに後から登録する。

    ※特定クライアント固有の初期値はここに入れない（業種テンプレは汎用）。
    """
    return {
        "id": 1, "company": "", "representative": "", "address": "", "corp_number": "",
        "prefectures": "", "categories": "電気工事", "budget_max": "",
        "grade": "", "quals": "", "qualifications": [],
    }


def get_profile() -> dict[str, Any]:
    """マイ条件を取得。profile行が無ければ汎用の空プロフィールを返す（Supabase等で復元）。"""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if not row:
        return default_profile()
    p = _hydrate_profile(dict(row))
    # 等級が空のままの行は補完しない（汎用・空が正）。
    if False and not p.get("qualifications"):
        d = default_profile()
        p["qualifications"] = d["qualifications"]
        for k in ("company", "representative", "address", "corp_number", "grade"):
            if not p.get(k):
                p[k] = d[k]
    return p


def _normalize_qualifications(quals: Any) -> str:
    """機関別の等級を検証してJSON文字列にする。各行 {issuer, category, grade, score, valid_until, number, note}。"""
    import json
    if isinstance(quals, str):
        try:
            items = json.loads(quals or "[]")
        except (ValueError, TypeError):
            items = []
    elif isinstance(quals, list):
        items = quals
    else:
        items = []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        issuer = str(it.get("issuer", "")).strip()
        if not issuer:
            continue
        out.append({k: str(it.get(k, "")).strip() for k in
                    ("issuer", "category", "grade", "score", "valid_until", "number", "note")})
    return json.dumps(out, ensure_ascii=False)


def save_profile(prefectures: str, categories: str, budget_max: str,
                 grade: str = "", quals: str = "", company: str = "",
                 representative: str = "", address: str = "",
                 corp_number: str = "", qualifications: Any = None) -> None:
    """マイ条件を保存（単一行 upsert）。"""
    quals_json = _normalize_qualifications(qualifications if qualifications is not None else [])
    with _connect() as conn:
        conn.execute(
            """INSERT INTO profile
                 (id, company, prefectures, categories, budget_max, grade, quals,
                  representative, address, corp_number, qualifications, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 company=excluded.company, prefectures=excluded.prefectures,
                 categories=excluded.categories, budget_max=excluded.budget_max,
                 grade=excluded.grade, quals=excluded.quals,
                 representative=excluded.representative, address=excluded.address,
                 corp_number=excluded.corp_number, qualifications=excluded.qualifications,
                 updated_at=datetime('now')""",
            (company, prefectures, categories, budget_max, grade, quals,
             representative, address, corp_number, quals_json),
        )
        conn.commit()
    _push_profile()


def match_cases(profile: dict[str, Any], limit: int = 300) -> list[dict[str, Any]]:
    """マイ条件に合致する案件を、マッチ理由つきで返す（公告が新しい順）。

    マッチ条件:
      - 対応エリア（都道府県）に含まれる
      - 対応業種に category が含まれる（部分一致: 「電気」を含む等）
      - 予算上限が設定されていれば 予定価格 ≤ 上限
    """
    prefs = [p.strip() for p in (profile.get("prefectures") or "").split(",") if p.strip()]
    cats = [c.strip() for c in (profile.get("categories") or "").split(",") if c.strip()]
    budget_max = yen_to_int(profile.get("budget_max") or "")
    if not prefs and not cats:
        return []

    where, params = [], []
    if prefs:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefs)))
        params.extend(prefs)
    if cats:
        where.append("(" + " OR ".join("category LIKE ?" for _ in cats) + ")")
        params.extend(f"%{c}%" for c in cats)
    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = (f"SELECT * FROM cases {clause} "
           f"ORDER BY CASE WHEN announced_date='' THEN 1 ELSE 0 END, announced_date DESC LIMIT ?")
    params.append(limit)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    out = []
    for r in rows:
        price = yen_to_int(r.get("budget") or "")
        if budget_max and price and price > budget_max:
            continue
        reasons = []
        if r.get("prefecture") in prefs:
            reasons.append(f"対応エリア（{r['prefecture']}）")
        if any(c in (r.get("category") or "") for c in cats):
            reasons.append(f"業種一致（{r['category']}）")
        if budget_max and price:
            reasons.append("予算内")
        r["match_reasons"] = reasons
        out.append(r)
    return out
