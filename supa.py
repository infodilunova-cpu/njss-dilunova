"""Supabase(Postgres) 永続化レイヤ — 入力データだけをデプロイ揮発から守る。

Render無料プランはディスクが揮発し、デプロイのたびに SQLite(denki_bid.db) が作り直される。
案件(cases)は再取得で復元できるが、ユーザーが入れた

  - 申請管理(applications) … external_id をキーに保持（案件IDの振り直しに強い）
  - 協力会社(companies)
  - マイ条件(profile)
  - 監視機関の除外(agency_exclusions)

は消えると困る。これらを Supabase の単一KVテーブル `kawano_kv`(key, data JSONB) に
「丸ごとJSONで」保存する。データは小さいので毎回まるごと書いても十分。

方針:
  - 接続情報は環境変数 SUPABASE_DB_URL（Renderに設定）。未設定ならこの層は無効＝SQLiteのみ。
  - すべての関数は失敗しても例外を投げない（Supabase不通でもアプリは動き続ける＝安全網）。
  - 起動時に load_all() で SQLite へ流し込み、各保存時に save() で書き戻す（write-through）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_TIMEOUT = 5  # 保存時に長くブロックしないよう短め（不通でもSQLite＋localStorageで担保）


def _url() -> str:
    return os.environ.get("SUPABASE_DB_URL", "").strip()


def enabled() -> bool:
    return bool(_url())


def _connect():
    import psycopg2
    return psycopg2.connect(_url(), connect_timeout=_TIMEOUT)


def init() -> None:
    """KVテーブルを用意（無ければ作成）。失敗しても黙って続行。"""
    if not enabled():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS kawano_kv ("
                " key TEXT PRIMARY KEY,"
                " data JSONB NOT NULL,"
                " updated_at TIMESTAMPTZ DEFAULT now())"
            )
            conn.commit()
        log.info("supa: init OK")
    except Exception as e:  # noqa: BLE001
        log.warning("supa: init failed: %s", e)


def save(key: str, obj: Any) -> bool:
    """key にデータ(JSON可能な値)を丸ごと保存。成功で True。"""
    if not enabled():
        return False
    try:
        from psycopg2.extras import Json
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kawano_kv (key, data, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at=now()",
                (key, Json(obj)),
            )
            conn.commit()
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("supa: save %s failed: %s", key, e)
        return False


def diagnose() -> dict:
    """接続診断（一時的なヘルスチェック用）。例外は文字列で返す。"""
    info = {"enabled": enabled(), "url_set": bool(_url()),
            "url_host": "", "connected": False, "rw_ok": False, "error": ""}
    if not enabled():
        info["error"] = "SUPABASE_DB_URL未設定"
        return info
    try:
        # ホストだけ（パスワードは出さない）
        import re
        m = re.search(r"@([^:/]+)", _url())
        info["url_host"] = m.group(1) if m else "?"
    except Exception:
        pass
    try:
        import psycopg2
    except Exception as e:  # noqa: BLE001
        info["error"] = "psycopg2 import失敗: " + repr(e)[:150]
        return info
    try:
        conn = _connect()
        info["connected"] = True
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS kawano_kv (key TEXT PRIMARY KEY, data JSONB NOT NULL, updated_at TIMESTAMPTZ DEFAULT now())")
            from psycopg2.extras import Json
            cur.execute("INSERT INTO kawano_kv (key,data,updated_at) VALUES (%s,%s,now()) ON CONFLICT (key) DO UPDATE SET data=EXCLUDED.data, updated_at=now()", ("__healthcheck__", Json({"ok": 1})))
            conn.commit()
            cur.execute("SELECT data FROM kawano_kv WHERE key=%s", ("__healthcheck__",))
            info["rw_ok"] = cur.fetchone() is not None
        conn.close()
    except Exception as e:  # noqa: BLE001
        info["error"] = repr(e)[:300]
    return info


def load(key: str) -> Any:
    """key のデータを取得（無ければ None）。JSONBはそのまま python の list/dict で返る。"""
    if not enabled():
        return None
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT data FROM kawano_kv WHERE key = %s", (key,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:  # noqa: BLE001
        log.warning("supa: load %s failed: %s", key, e)
        return None
