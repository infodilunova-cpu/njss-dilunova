"""認証＋アカウント別のAIモード権限管理（独立モジュール）。

要件:
  - ログイン認証（メール＋パスワード。パスワードはハッシュ保存）
  - **アカウントごとに『AIモード（課金プラン）を使えるか』を管理**する枠組み
    （users.ai_enabled フラグ＋管理者が画面でトグル）

設計（重要）:
  - ユーザーは案件DB(denki_bid.db)とは別の users.db に保存する。
    案件DBは毎日 fetch_db で丸ごと差し替わるため、同居させると消える。
    ※本番(Render無料)はディスクが揮発するので、永続させるには Persistent Disk か
      外部DBが必要（枠組みは先に用意し、永続化は配備時に決める）。
  - 既存 app.py への統合は最小:
        from auth import auth_bp, init_auth_db, login_required, can_use_ai
        init_auth_db(); app.register_blueprint(auth_bp)
    各ビューに @login_required、AIルートで can_use_ai() を確認するだけ。

CLI:
  python auth.py create-admin <email> <password>   # 最初の管理者を作る
  python auth.py grant-ai <email>                   # AIモードを許可
  python auth.py revoke-ai <email>                  # AIモードを取消
  python auth.py list                               # ユーザー一覧
"""

from __future__ import annotations

import functools
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

AUTH_DB = Path(__file__).parent / "users.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    ai_enabled    INTEGER DEFAULT 0,   -- 1ならAIモード(課金プラン)を使える
    is_admin      INTEGER DEFAULT 0,   -- 1なら他ユーザーの権限を管理できる
    vertical      TEXT DEFAULT 'denki',-- 業種テンプレ(denki/web…)。ログインで行き先が決まる
    created_at    TEXT DEFAULT (datetime('now'))
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(AUTH_DB)
    c.row_factory = sqlite3.Row
    return c


def init_auth_db() -> None:
    with _conn() as c:
        c.executescript(_SCHEMA)
        # 後付け列のマイグレーション（既存users.dbにvertical列を足す）
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "vertical" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN vertical TEXT DEFAULT 'denki'")
        c.commit()


# ---- ユーザー操作 -----------------------------------------------------------

def create_user(email: str, password: str, ai_enabled: bool = False,
                is_admin: bool = False, vertical: str = "denki") -> tuple[bool, str]:
    """ユーザー作成。成功で (True, "")、失敗で (False, 理由)。

    vertical でそのアカウントの業種（行き先）を決める。
    最初の1人は自動的に管理者＆AI許可にする（運用開始をスムーズに）。
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "メールアドレスが不正です。"
    if len(password or "") < 6:
        return False, "パスワードは6文字以上にしてください。"
    init_auth_db()
    with _conn() as c:
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if first:
            ai_enabled, is_admin = True, True  # 最初の登録者は管理者
        try:
            c.execute(
                "INSERT INTO users (email, password_hash, ai_enabled, is_admin, vertical) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, generate_password_hash(password),
                 int(ai_enabled), int(is_admin), (vertical or "denki").strip()))
            c.commit()
        except sqlite3.IntegrityError:
            return False, "このメールアドレスは既に登録されています。"
    return True, ""


def set_vertical(email: str, vertical: str) -> bool:
    """アカウントの業種(行き先)を設定する。"""
    with _conn() as c:
        n = c.execute("UPDATE users SET vertical = ? WHERE email = ?",
                      ((vertical or "denki").strip(), email.strip().lower())).rowcount
        c.commit()
    return n > 0


def verify(email: str, password: str) -> dict[str, Any] | None:
    """メール＋パスワードを検証し、合致すればユーザー dict を返す。"""
    email = (email or "").strip().lower()
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if row and check_password_hash(row["password_hash"], password or ""):
        return dict(row)
    return None


def get_user(uid: int) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, email, ai_enabled, is_admin, vertical, created_at "
            "FROM users ORDER BY created_at").fetchall()]


def set_ai_enabled(email: str, enabled: bool) -> bool:
    with _conn() as c:
        n = c.execute("UPDATE users SET ai_enabled = ? WHERE email = ?",
                      (int(enabled), email.strip().lower())).rowcount
        c.commit()
    return n > 0


# ---- セッション / 現在ユーザー ---------------------------------------------

def current_user() -> dict[str, Any] | None:
    """ログイン中ユーザー（未ログインなら None）。リクエスト内でキャッシュ。

    Supabase認証が有効なときは session['sb_user']（メール/業種等）を優先。
    それ以外は従来の自作認証(users.db の uid)。
    """
    if "user" in g:
        return g.user
    sb = session.get("sb_user")
    if sb:
        g.user = sb
        return g.user
    uid = session.get("uid")
    g.user = get_user(uid) if uid else None
    return g.user


def auth_required() -> bool:
    """ログイン認証を強制するか。環境変数 AUTH_REQUIRED=1 のときだけ True。

    既定OFF＝本番(無料ディスク揮発でusers.dbが毎日消える)を壊さない。
    永続ディスク/外部DBを用意したら AUTH_REQUIRED=1 にして有効化する。
    """
    return os.environ.get("AUTH_REQUIRED", "0") == "1"


def can_use_ai() -> bool:
    """現在のアカウントがAIモードを使えるか。

    ① グローバルにAI(Gemini)が設定されている（GEMINI_API_KEY）— 必須
    ② 認証ONなら、ログイン中で ai_enabled=1（このアカウントに許可）も必須。
       認証OFF（既定）なら①だけで判定（従来動作＝鍵があれば誰でも）。
    """
    try:
        import ai_assist
        if not ai_assist.is_enabled():
            return False
    except Exception:  # noqa: BLE001
        return False
    if not auth_required():
        return True
    u = current_user()
    return bool(u and u.get("ai_enabled"))


# ---- デコレータ -------------------------------------------------------------

def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        return view(*a, **kw)
    return wrapped


def admin_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("auth.login", next=request.path))
        if not u.get("is_admin"):
            abort(403)
        return view(*a, **kw)
    return wrapped


def ai_required(view: Callable) -> Callable:
    """AIモードを使えるアカウントだけ通す（ログイン＋ai_enabled＋鍵設定）。"""
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("auth.login", next=request.path))
        if not can_use_ai():
            abort(403)
        return view(*a, **kw)
    return wrapped


# ---- Blueprint（ログイン/登録/ログアウト/管理）------------------------------

auth_bp = Blueprint("auth", __name__, template_folder=str(Path(__file__).parent / "templates"))


def _supa() -> bool:
    """Supabase認証が有効か（URL＋anon key 設定時）。"""
    try:
        import supa_auth
        return supa_auth.enabled()
    except Exception:  # noqa: BLE001
        return False


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email, pw = request.form.get("email", ""), request.form.get("password", "")
        if _supa():
            import supa_auth
            data, err = supa_auth.sign_in(email, pw)
            if data and data.get("access_token") and data.get("user"):
                session["sb_token"] = data["access_token"]
                session["sb_user"] = supa_auth.user_to_session(data["user"])
                return redirect(request.args.get("next") or "/")
            flash("メールアドレスまたはパスワードが違います。", "error")
        else:
            u = verify(email, pw)
            if u:
                session["uid"] = u["id"]
                return redirect(request.args.get("next") or "/")
            flash("メールアドレスまたはパスワードが違います。", "error")
    return render_template("auth/login.html", supa=_supa())


@auth_bp.route("/signup", methods=["GET", "POST"])
def signup():
    try:
        import verticals
        vlist = [(v["id"], v["label"]) for v in verticals.VERTICALS.values()]
    except Exception:  # noqa: BLE001
        vlist = [("denki", "電気工事")]
    if request.method == "POST":
        email, pw = request.form.get("email", ""), request.form.get("password", "")
        vt = request.form.get("vertical", "denki")
        if _supa():
            import supa_auth
            _, err = supa_auth.sign_up(email, pw, vertical=vt)
            if err:
                flash(err, "error")
                return render_template("auth/signup.html", verticals=vlist, supa=True)
            data, _ = supa_auth.sign_in(email, pw)
            if data and data.get("access_token"):
                session["sb_token"] = data["access_token"]
                session["sb_user"] = supa_auth.user_to_session(data["user"])
            return redirect("/")
        ok, msg = create_user(email, pw, vertical=vt)
        if ok:
            u = verify(email, pw)
            session["uid"] = u["id"]
            return redirect("/")
        flash(msg, "error")
    return render_template("auth/signup.html", verticals=vlist, supa=_supa())


@auth_bp.get("/auth/google")
def google_login():
    """Googleログイン開始（Supabase経由）。コールバックで access_token を受け取る。"""
    if not _supa():
        flash("Google認証は未設定です。", "error")
        return redirect(url_for("auth.login"))
    import supa_auth
    cb = url_for("auth.oauth_callback", _external=True)
    return redirect(supa_auth.oauth_url("google", cb))


@auth_bp.route("/auth/callback")
def oauth_callback():
    """OAuthコールバック。?code= があればサーバ交換、無ければ #fragment の
    access_token をJSで拾ってPOSTし直す（Supabaseのimplicitフロー対応）。"""
    import supa_auth
    code = request.args.get("code")
    if code:
        data, err = supa_auth.exchange_code(code)
        if data and data.get("access_token"):
            session["sb_token"] = data["access_token"]
            session["sb_user"] = supa_auth.user_to_session(data["user"])
            return redirect("/")
        flash(f"ログインに失敗しました: {err}", "error")
        return redirect(url_for("auth.login"))
    # implicit: トークンは URL の #fragment にあるのでJSで拾って /auth/token へ
    return render_template("auth/callback.html")


@auth_bp.post("/auth/token")
def oauth_token():
    """#fragment から取った access_token を受けてセッション確立（JSから）。"""
    import supa_auth
    token = (request.get_json(silent=True) or {}).get("access_token", "")
    if not token:
        return jsonify({"ok": False}), 400
    user, err = supa_auth.get_user(token)
    if not user:
        return jsonify({"ok": False, "error": err}), 400
    session["sb_token"] = token
    session["sb_user"] = supa_auth.user_to_session(user)
    return jsonify({"ok": True})


@auth_bp.get("/logout")
def logout():
    session.pop("uid", None)
    session.pop("sb_token", None)
    session.pop("sb_user", None)
    g.pop("user", None)
    return redirect(url_for("auth.login"))


@auth_bp.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    """管理者: 各アカウントのAIモード可否をトグルする画面。"""
    if request.method == "POST":
        email = request.form.get("email", "")
        enabled = request.form.get("ai_enabled") == "on"
        set_ai_enabled(email, enabled)
        flash(f"{email} のAIモードを{'許可' if enabled else '取消'}しました。", "ok")
        return redirect(url_for("auth.admin_users"))
    return render_template("auth/admin_users.html", users=list_users())


# ---- CLI -------------------------------------------------------------------

def _cli() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    cmd = args[0]
    init_auth_db()
    if cmd == "create-admin" and len(args) >= 3:
        ok, msg = create_user(args[1], args[2], ai_enabled=True, is_admin=True)
        print("作成しました（管理者・AI許可）" if ok else f"失敗: {msg}")
    elif cmd == "grant-ai" and len(args) >= 2:
        print("AIモードを許可しました" if set_ai_enabled(args[1], True) else "対象が見つかりません")
    elif cmd == "revoke-ai" and len(args) >= 2:
        print("AIモードを取消しました" if set_ai_enabled(args[1], False) else "対象が見つかりません")
    elif cmd == "create" and len(args) >= 4:  # create <email> <pass> <vertical>
        ok, msg = create_user(args[1], args[2], vertical=args[3])
        print(f"作成しました（業種={args[3]}）" if ok else f"失敗: {msg}")
    elif cmd == "set-vertical" and len(args) >= 3:  # set-vertical <email> <vertical>
        print(f"業種を {args[2]} にしました" if set_vertical(args[1], args[2]) else "対象が見つかりません")
    elif cmd == "list":
        for u in list_users():
            print(f"  {u['email']}  AI={'○' if u['ai_enabled'] else '×'}  "
                  f"admin={'○' if u['is_admin'] else '×'}")
    else:
        print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
