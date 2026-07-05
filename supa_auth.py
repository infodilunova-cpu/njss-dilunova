"""Supabase Auth（GoTrue）クライアント — メール認証＋Google認証。

環境変数:
  SUPABASE_URL       例: https://xxxx.supabase.co
  SUPABASE_ANON_KEY  anon public key（クライアント用の公開鍵）

両方そろったときだけ有効（enabled()）。未設定ならアプリは従来の自作認証(auth.py)を使う。
ユーザーの業種(vertical)は user_metadata に保存する（ログインで行き先が決まる）。

依存は標準ライブラリのみ（urllib）。失敗時は例外を投げず (None, "理由") を返す方針。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_TIMEOUT = 15


def _url() -> str:
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def _anon() -> str:
    return os.environ.get("SUPABASE_ANON_KEY", "").strip()


def enabled() -> bool:
    return bool(_url() and _anon())


def _request(path: str, *, method: str = "GET", body: dict | None = None,
             token: str | None = None) -> tuple[dict | None, str]:
    """GoTrue へのリクエスト。成功で (data, "")、失敗で (None, 理由)。"""
    headers = {"apikey": _anon(), "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_url() + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
            raw = res.read().decode("utf-8")
            return (json.loads(raw) if raw else {}), ""
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8"))
            msg = err.get("msg") or err.get("error_description") or err.get("error") or str(err)
        except Exception:  # noqa: BLE001
            msg = f"HTTP {e.code}"
        return None, msg
    except Exception as e:  # noqa: BLE001
        return None, str(e)[:200]


def sign_up(email: str, password: str, vertical: str = "denki") -> tuple[dict | None, str]:
    """メール＋パスワードで新規登録。業種を user_metadata に保存。"""
    return _request("/auth/v1/signup", method="POST",
                    body={"email": email, "password": password,
                          "data": {"vertical": vertical}})


def sign_in(email: str, password: str) -> tuple[dict | None, str]:
    """メール＋パスワードでログイン。成功で {access_token, refresh_token, user...}。"""
    return _request("/auth/v1/token?grant_type=password", method="POST",
                    body={"email": email, "password": password})


def get_user(token: str) -> tuple[dict | None, str]:
    """アクセストークンからユーザー情報を取得。"""
    return _request("/auth/v1/user", method="GET", token=token)


def update_vertical(token: str, vertical: str) -> tuple[dict | None, str]:
    """ログイン中ユーザーの業種(user_metadata.vertical)を更新。"""
    return _request("/auth/v1/user", method="PUT", token=token,
                    body={"data": {"vertical": vertical}})


def exchange_code(code: str) -> tuple[dict | None, str]:
    """OAuth(PKCE)のcodeをセッションに交換（Google等のコールバック用）。"""
    return _request("/auth/v1/token?grant_type=pkce", method="POST",
                    body={"auth_code": code})


def oauth_url(provider: str, redirect_to: str) -> str:
    """OAuth開始URL（例: provider='google'）。redirect_to にコールバックURL。"""
    q = urllib.parse.urlencode({"provider": provider, "redirect_to": redirect_to})
    return f"{_url()}/auth/v1/authorize?{q}"


def user_to_session(user: dict) -> dict[str, Any]:
    """GoTriのuser dict → アプリ内で使う最小ユーザー情報へ。"""
    meta = user.get("user_metadata") or {}
    return {
        "id": user.get("id"),
        "email": user.get("email", ""),
        "vertical": (meta.get("vertical") or "denki"),
        "ai_enabled": bool(meta.get("ai_enabled", True)),  # 既定でAI可（個別に絞るなら後で）
        "is_admin": bool(meta.get("is_admin", False)),
        "via": "supabase",
    }
