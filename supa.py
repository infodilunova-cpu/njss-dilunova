"""Supabase(Postgres) 永続化レイヤ — ユーザー入力データとアカウントを揮発から守る。

Render無料プランはディスクが揮発し、デプロイのたびに SQLite(denki_bid.db / users.db) が
作り直される。案件(cases)は再取得で復元できるが、

  - アカウント(メール/パスワード)                 … dln_users テーブル
  - 申請管理(applications) / 協力会社(companies)
    マイ条件(profile) / 監視機関の除外(exclusions) … dln_kv テーブル（ユーザー別キー）

は消えると困るため Supabase Postgres に保存する。
※テーブル名は dln_* 。同じSupabaseプロジェクトを使う川野ツール(kawano_kv)とは
  完全に別テーブルで、データは一切混ざらない（DiluNovaは独立プロダクト）。

方針:
  - 接続情報は環境変数 SUPABASE_DB_URL（Renderに設定）。未設定ならこの層は無効＝SQLiteのみ。
  - KVの関数は失敗しても例外を投げない（Supabase不通でもアプリは動き続ける＝安全網）。
  - アカウント系(get_user/create_user等)は呼び出し側が有効時のみ使う。
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_TIMEOUT = 5  # 保存時に長くブロックしないよう短め（不通でもSQLite＋localStorageで担保）

_KV_TABLE = "dln_kv"
_USERS_TABLE = "dln_users"
_USAGE_TABLE = "dln_ai_usage"


def _url() -> str:
    return os.environ.get("SUPABASE_DB_URL", "").strip()


def enabled() -> bool:
    return bool(_url())


def _connect():
    import psycopg2
    return psycopg2.connect(_url(), connect_timeout=_TIMEOUT)


def init() -> None:
    """KV・ユーザーテーブルを用意（無ければ作成）。失敗しても黙って続行。"""
    if not enabled():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_KV_TABLE} ("
                " key TEXT PRIMARY KEY,"
                " data JSONB NOT NULL,"
                " updated_at TIMESTAMPTZ DEFAULT now())"
            )
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_USERS_TABLE} ("
                " email TEXT PRIMARY KEY,"
                " password_hash TEXT NOT NULL,"
                " ai_enabled INTEGER DEFAULT 0,"
                " is_admin INTEGER DEFAULT 0,"
                " vertical TEXT DEFAULT '',"
                " created_at TIMESTAMPTZ DEFAULT now())"
            )
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {_USAGE_TABLE} ("
                " id BIGSERIAL PRIMARY KEY,"
                " email TEXT DEFAULT '',"
                " kind TEXT DEFAULT '',"      # assist / plan / doc
                " model TEXT DEFAULT '',"
                " tokens_in INTEGER DEFAULT 0,"
                " tokens_out INTEGER DEFAULT 0,"
                " created_at TIMESTAMPTZ DEFAULT now())"
            )
            conn.commit()
        log.info("supa: init OK")
    except Exception as e:  # noqa: BLE001
        log.warning("supa: init failed: %s", e)


# ============================================================
# KV（ユーザー入力データの丸ごとJSON保存）
# ============================================================

# 直近の永続化保存が成功したか（--workers 1 前提でプロセス内共有）。
# 無言の保存失敗＝次のデプロイでお客様の入力が消える、に運用者が気づけるようにする。
_last_save_ok: bool = True
_last_save_error: str = ""


def last_save_ok() -> bool:
    """直近の save() が成功していれば True（enabled 時のみ意味を持つ）。"""
    return _last_save_ok


def last_save_error() -> str:
    """直近の保存失敗（またはブロック）の理由。"""
    return _last_save_error


def save(key: str, obj: Any) -> bool:
    """key にデータ(JSON可能な値)を丸ごと保存。成功で True。"""
    global _last_save_ok, _last_save_error
    if not enabled():
        return False
    try:
        from psycopg2.extras import Json
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_KV_TABLE} (key, data, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
                (key, Json(obj)),
            )
            conn.commit()
        _last_save_ok = True
        _last_save_error = ""
        return True
    except Exception as e:  # noqa: BLE001
        # 無言の消失を防ぐため、失敗を記録して画面バナーで警告できるようにする。
        _last_save_ok = False
        _last_save_error = str(e)[:200]
        log.warning("supa: save %s failed: %s", key, e)
        return False


def load(key: str) -> Any:
    """key のデータを取得（無ければ None）。JSONBはそのまま python の list/dict で返る。"""
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT data FROM {_KV_TABLE} WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        log.warning("supa: load %s failed: %s", key, e)
        return None


def load_prefix(prefix: str) -> dict[str, Any]:
    """prefix で始まる全キーのデータを {key: data} で返す（ユーザー別データの一括復元用）。"""
    if not enabled():
        return {}
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT key, data FROM {_KV_TABLE} WHERE key LIKE %s", (prefix + "%",))
            return {k: d for k, d in cur.fetchall()}
    except Exception as e:  # noqa: BLE001
        log.warning("supa: load_prefix %s failed: %s", prefix, e)
        return {}


# ============================================================
# アカウント（メール＋パスワードの永続ユーザー）
# ============================================================

def get_user(email: str) -> dict[str, Any] | None:
    """メールでユーザーを取得（無ければ None）。"""
    if not enabled() or not email:
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT email, password_hash, ai_enabled, is_admin, vertical "
                f"FROM {_USERS_TABLE} WHERE email = %s", (email,))
            row = cur.fetchone()
        if not row:
            return None
        return {"email": row[0], "password_hash": row[1],
                "ai_enabled": bool(row[2]), "is_admin": bool(row[3]),
                "vertical": row[4] or "", "via": "supa_pg"}
    except Exception as e:  # noqa: BLE001
        log.warning("supa: get_user failed: %s", e)
        return None


def create_user(email: str, password_hash: str, *, ai_enabled: bool = False,
                is_admin: bool = False, vertical: str = "") -> tuple[bool, str]:
    """ユーザー作成。成功 (True,"") / 重複 (False,理由)。"""
    if not enabled():
        return False, "永続化が未設定です"
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_USERS_TABLE} (email, password_hash, ai_enabled, is_admin, vertical) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING",
                (email, password_hash, int(ai_enabled), int(is_admin), vertical))
            inserted = cur.rowcount > 0
            conn.commit()
        return (True, "") if inserted else (False, "このメールアドレスは既に登録されています。")
    except Exception as e:  # noqa: BLE001
        log.warning("supa: create_user failed: %s", e)
        return False, "アカウント作成に失敗しました（時間をおいて再度お試しください）"


def set_password(email: str, password_hash: str) -> bool:
    if not enabled():
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {_USERS_TABLE} SET password_hash = %s WHERE email = %s",
                        (password_hash, email))
            n = cur.rowcount
            conn.commit()
        return n > 0
    except Exception as e:  # noqa: BLE001
        log.warning("supa: set_password failed: %s", e)
        return False


def count_users() -> int:
    if not enabled():
        return 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {_USERS_TABLE}")
            return cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001
        log.warning("supa: count_users failed: %s", e)
        return 0


def list_users() -> list[dict[str, Any]]:
    if not enabled():
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT email, ai_enabled, is_admin, vertical, created_at "
                f"FROM {_USERS_TABLE} ORDER BY created_at")
            return [{"email": r[0], "ai_enabled": bool(r[1]), "is_admin": bool(r[2]),
                     "vertical": r[3] or "", "created_at": str(r[4])}
                    for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001
        log.warning("supa: list_users failed: %s", e)
        return []


def set_ai_enabled(email: str, enabled_flag: bool) -> bool:
    if not enabled():
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE {_USERS_TABLE} SET ai_enabled = %s WHERE email = %s",
                        (int(enabled_flag), email))
            n = cur.rowcount
            conn.commit()
        return n > 0
    except Exception as e:  # noqa: BLE001
        log.warning("supa: set_ai_enabled failed: %s", e)
        return False


# ============================================================
# AI利用カウンター（課金の見える化。生成1回=1行。キャッシュヒットは記録しない）
# ============================================================

def log_ai_usage(email: str, kind: str, model: str,
                 tokens_in: int = 0, tokens_out: int = 0) -> None:
    """AI生成1回を記録する。失敗しても黙って続行（本体機能を止めない）。"""
    if not enabled():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_USAGE_TABLE} (email, kind, model, tokens_in, tokens_out) "
                "VALUES (%s, %s, %s, %s, %s)",
                ((email or "").strip().lower(), kind[:16], model[:64],
                 int(tokens_in or 0), int(tokens_out or 0)))
            conn.commit()
    except Exception as e:  # noqa: BLE001
        log.warning("supa: log_ai_usage failed: %s", e)


def usage_for(email: str) -> dict:
    """1ユーザーの今月のAI利用（回数・トークン）。失敗時はゼロ。"""
    out = {"count": 0, "tokens_in": 0, "tokens_out": 0}
    if not enabled() or not email:
        return out
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0) "
                f"FROM {_USAGE_TABLE} WHERE email = %s "
                "AND created_at >= date_trunc('month', now())",
                ((email or "").strip().lower(),))
            r = cur.fetchone()
            out = {"count": r[0], "tokens_in": int(r[1]), "tokens_out": int(r[2])}
    except Exception as e:  # noqa: BLE001
        log.warning("supa: usage_for failed: %s", e)
    return out


def usage_summary() -> dict:
    """管理者向けのAI利用集計（今月のユーザー別×機能別と全期間合計）。"""
    out = {"month_by_user": [], "month_total": {"count": 0, "tokens_in": 0, "tokens_out": 0},
           "all_total": {"count": 0, "tokens_in": 0, "tokens_out": 0}}
    if not enabled():
        return out
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT email, kind, COUNT(*), COALESCE(SUM(tokens_in),0), "
                f"COALESCE(SUM(tokens_out),0) FROM {_USAGE_TABLE} "
                "WHERE created_at >= date_trunc('month', now()) "
                "GROUP BY email, kind ORDER BY email, kind")
            out["month_by_user"] = [
                {"email": r[0], "kind": r[1], "count": r[2],
                 "tokens_in": int(r[3]), "tokens_out": int(r[4])}
                for r in cur.fetchall()]
            cur.execute(
                f"SELECT COUNT(*), COALESCE(SUM(tokens_in),0), COALESCE(SUM(tokens_out),0), "
                "COUNT(*) FILTER (WHERE created_at >= date_trunc('month', now())), "
                "COALESCE(SUM(tokens_in) FILTER (WHERE created_at >= date_trunc('month', now())),0), "
                "COALESCE(SUM(tokens_out) FILTER (WHERE created_at >= date_trunc('month', now())),0) "
                f"FROM {_USAGE_TABLE}")
            r = cur.fetchone()
            out["all_total"] = {"count": r[0], "tokens_in": int(r[1]), "tokens_out": int(r[2])}
            out["month_total"] = {"count": r[3], "tokens_in": int(r[4]), "tokens_out": int(r[5])}
    except Exception as e:  # noqa: BLE001
        log.warning("supa: usage_summary failed: %s", e)
    return out


def block_save(reason: str) -> None:
    """危険な書き戻しを「あえて行わなかった」ことを記録し、画面バナーで知らせる。

    黙って止めると利用者は保存できたと思い込む。保存していないことを必ず伝える。
    """
    global _last_save_ok, _last_save_error
    _last_save_ok = False
    _last_save_error = reason
    log.error("supa: save blocked: %s", reason)


def diagnose() -> dict:
    """接続診断（ヘルスチェック用）。例外は文字列で返す。"""
    info = {"enabled": enabled(), "connected": False, "rw_ok": False, "error": ""}
    if not enabled():
        info["error"] = "SUPABASE_DB_URL未設定"
        return info
    try:
        import psycopg2  # noqa: F401
    except Exception as e:  # noqa: BLE001
        info["error"] = "psycopg2 import失敗: " + repr(e)[:150]
        return info
    try:
        init()
        conn = _connect()
        info["connected"] = True
        with conn.cursor() as cur:
            from psycopg2.extras import Json
            cur.execute(
                f"INSERT INTO {_KV_TABLE} (key,data,updated_at) VALUES (%s,%s,now()) "
                "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
                ("__healthcheck__", Json({"ok": 1})))
            conn.commit()
            cur.execute(f"SELECT data FROM {_KV_TABLE} WHERE key=%s", ("__healthcheck__",))
            info["rw_ok"] = cur.fetchone() is not None
        conn.close()
    except Exception as e:  # noqa: BLE001
        info["error"] = repr(e)[:300]
    return info
