"""電気入札サーチ — 独立ツール（Flask）。

NJSS無双君 とは完全に独立したアプリ。SQLite だけで動く。

ルート:
  /            案件一覧（地方→都道府県の2段フィルタ、NJSS風）
  /case/<id>   案件詳細（仕様書の取得可否＋理由を表示）
  /api/prefectures  地方→都道府県の連動ドロップダウン用JSON

起動:
  cd denki-nyusatsu
  python app.py        → http://127.0.0.1:5001
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import db
import procurement
import ai_assist
import auth
import verticals
from regions import ALL_PREFECTURES, REGIONS, prefectures_in


def current_vertical() -> str:
    """このリクエストの業種テンプレ。ログイン中はそのアカウントの業種、
    未ログインなら環境変数 DEFAULT_VERTICAL（無ければ既定）。"""
    try:
        u = auth.current_user()
        if u and u.get("vertical") in verticals.VERTICALS:
            return u["vertical"]
    except Exception:  # noqa: BLE001
        pass
    return os.environ.get("DEFAULT_VERTICAL", verticals.DEFAULT_VERTICAL)

# 公告日がこの日付以降なら「新着」とみなす（直近7日）
def _new_threshold() -> str:
    return (date.today() - timedelta(days=7)).isoformat()

# 予定価格の下限フィルタの選択肢（label, 円値）。本文から拾えた予定価格に対して効く。
BUDGET_OPTIONS = [
    ("指定なし", 0),
    ("500万円以上", 5_000_000),
    ("1000万円以上", 10_000_000),
    ("3000万円以上", 30_000_000),
    ("5000万円以上", 50_000_000),
    ("1億円以上", 100_000_000),
]

# 申請ステータスのバッジ色（案件詳細で使用）。db.STATUS_ACCENT を流用。
STATUS_CLASS = db.STATUS_ACCENT


def _days_until(iso: str) -> int | None:
    """ISO日付(YYYY-MM-DD)までの残日数。今日=0、過ぎていれば負。不正なら None。
    bid-next-eta の er(date)=round((date-基準日)/86400000) に相当。"""
    s = (iso or "").strip()
    if not s:
        return None
    try:
        return (date.fromisoformat(s[:10]) - date.today()).days
    except ValueError:
        return None


# 次の締切マイルストーン（bid-next-eta の ei と同一ロジック）。
def _next_milestone(row: dict) -> dict | None:
    st = db.normalize_status(row.get("status", ""))
    apply_dl = row.get("apply_deadline") or row.get("deadline") or ""
    if st == "参加申請準備前":
        return {"label": "参加申請", "date": apply_dl} if apply_dl else None
    if st in ("入札参加申請済み", "協力会社探し中", "見積取得"):
        bid = row.get("bid_deadline") or ""
        return {"label": "入札書提出", "date": bid} if bid else None
    if st == "入札書提出済み":
        op = row.get("open_date") or ""
        return {"label": "開札", "date": op} if op else None
    return None


def _enrich_application(row: dict) -> dict:
    """カンバン表示用に締切・残日数・見積サマリーを補う。"""
    row["eff_apply_deadline"] = row.get("apply_deadline") or row.get("deadline") or ""
    row["apply_days"] = _days_until(row["eff_apply_deadline"])
    row["bid_days"] = _days_until(row.get("bid_deadline") or "")
    ms = _next_milestone(row)
    row["ms_label"] = ms["label"] if ms else ""
    row["ms_date"] = ms["date"] if ms else ""
    row["ms_days"] = _days_until(ms["date"]) if ms else None
    row["work_eff"] = row.get("work") or row.get("category") or ""
    partners = row.get("partners") or []
    row["partner_count"] = len(partners)
    row["partner_replied"] = sum(1 for p in partners if p.get("replied"))
    return row

# 対応業種の選択肢（電気工事業者向けに関連する建設業の業種）
BIZ_TYPES = [
    "電気工事", "電気設備工事", "電気通信工事", "機械器具設置工事",
    "管工事", "消防施設工事", "太陽光発電設備", "土木一式工事", "建築一式工事",
]

# 保有資格・登録の選択肢（複数選択）
QUAL_OPTIONS = [
    "建設業許可（電気工事業）", "第一種電気工事士", "第二種電気工事士",
    "電気主任技術者（電験）", "1級電気工事施工管理技士", "2級電気工事施工管理技士",
    "監理技術者", "経営事項審査（経審）", "入札参加資格登録",
    "ISO9001", "ISO14001", "Pマーク／ISMS",
]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kawano-njss-modoki-local")  # flash用

# gunicorn 等で import された時もテーブルを用意（本番デプロイ対応）
db.init_db()

# 認証＋アカウント別AI権限（auth.py）。ログイン/登録/管理(/login,/signup,/admin/users)。
auth.init_auth_db()
app.register_blueprint(auth.auth_bp)

# ログイン必須ガード（auth系・healthz・staticは除外）。未ログインはログインへ誘導。
_PUBLIC_ENDPOINTS = {"auth.login", "auth.signup", "static"}


@app.before_request
def _require_login():
    # AUTH_REQUIRED=1 のときだけログイン必須（既定OFF＝現状の本番は無認証のまま）。
    if not auth.auth_required():
        return None
    ep = request.endpoint or ""
    if ep in _PUBLIC_ENDPOINTS or ep.startswith("auth."):
        return None
    if not auth.current_user():
        return redirect(url_for("auth.login", next=request.path))
    return None


@app.context_processor
def inject_current_user():
    """テンプレで current_user / can_use_ai を使えるように供給。"""
    return {"current_user": auth.current_user(), "can_use_ai": auth.can_use_ai()}


@app.context_processor
def inject_vertical():
    """業種テンプレのブランディング（タイトル/ロゴ/footer）を全テンプレへ供給。"""
    vt = current_vertical()
    return {"vertical": vt, "brand": verticals.get(vt)}


@app.context_processor
def inject_profile_set():
    """無料ホストはディスク揮発のためマイ条件が消える。ブラウザ保存→自動復元の判定用に、
    サーバにマイ条件があるかを全テンプレへ渡す。"""
    try:
        return {"profile_set": bool(db.get_profile().get("prefectures"))}
    except Exception:  # noqa: BLE001
        return {"profile_set": False}


# データ異常とみなす閾値
_HEALTH_MIN_CASES = 1000   # これ未満は取得失敗の疑い（通常は1万件超）
_HEALTH_STALE_DAYS = 5     # 最新公告がこれ以上前なら更新停止の疑い


@app.context_processor
def inject_data_health():
    """データの鮮度・件数を全テンプレへ渡し、画面上部で警告できるようにする。

    毎日の自動更新がネット障害等で失敗すると件数が激減したり更新が止まる。
    今朝のような「無言の取得失敗」に運用者がすぐ気づけるよう、件数と最新公告日を
    評価して `data_health.stale`（要注意か）と理由を返す。読み取りのみで軽量。
    """
    info = {"total": 0, "latest": "", "stale": False, "stale_reason": ""}
    try:
        with db._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] or 0
            latest = conn.execute(
                "SELECT MAX(announced_date) FROM cases WHERE announced_date != ''"
            ).fetchone()[0] or ""
        info["total"], info["latest"] = total, latest
        reasons = []
        if total < _HEALTH_MIN_CASES:
            reasons.append(f"案件が{total:,}件と異常に少なく、データ取得に失敗している可能性があります")
        if latest:
            try:
                d = date.fromisoformat(latest)
                if (date.today() - d).days >= _HEALTH_STALE_DAYS:
                    reasons.append(f"最新の公告が{latest}で、データ更新が止まっている可能性があります")
            except ValueError:
                pass
        if reasons:
            info["stale"] = True
            info["stale_reason"] = "／".join(reasons)
    except Exception:  # noqa: BLE001 — ヘルス表示の失敗で画面を落とさない
        pass
    return {"data_health": info}


@app.route("/")
def cases():
    # 初期表示は関西中心（クエリ無しのランディング時は近畿をデフォルト）
    if not request.args:
        region = "近畿"
    else:
        region = request.args.get("region", "").strip()
    prefecture = request.args.get("prefecture", "").strip()
    # 業種・区分・入札方式は複数選択（チェック/multiple select）に対応
    category = [c for c in request.args.getlist("category") if c.strip()]
    procurement_type = [p for p in request.args.getlist("procurement_type") if p.strip()]
    bid_method = [b for b in request.args.getlist("bid_method") if b.strip()]
    spec_status = request.args.get("spec_status", "").strip()
    q = request.args.get("q", "").strip()
    # 新着期間: ""=指定なし / today=本日公告 / week=直近7日。nav の new=1 は week 扱い。
    fresh = request.args.get("fresh", "").strip()
    if request.args.get("new") == "1" and not fresh:
        fresh = "week"
    new_only = fresh in ("today", "week")
    open_only = request.args.get("open") == "1"
    # 終了（締切が過去）案件を表示するか。既定は隠す（closed=1 で表示）。
    show_closed = request.args.get("closed") == "1"
    # 金額下限（円）。万円ではなく円で受ける（フォームの選択肢は円値）
    try:
        budget_min = int(request.args.get("budget_min", "") or 0)
    except ValueError:
        budget_min = 0
    sort = request.args.get("sort", "announced" if new_only else "deadline").strip()
    # ページング（1ページ200件）。?page=2 で次の200件。
    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except ValueError:
        page = 1
    per_page = 200

    # 地方が選ばれていて都道府県がその地方に属さない場合は都道府県条件を無視
    if region and prefecture and prefecture not in prefectures_in(region):
        prefecture = ""

    # 新着フィルタはSQL側で行う（後フィルタだと件数が不正確＆200件上限の影響を受けるため）
    threshold = _new_threshold()
    today_iso = date.today().isoformat()
    announced_after = today_iso if fresh == "today" else (threshold if fresh == "week" else None)

    filters = dict(
        region=region or None,
        prefecture=prefecture or None,
        category=category or None,
        procurement_type=procurement_type or None,
        bid_method=bid_method or None,
        spec_status=spec_status or None,
        budget_min=budget_min or None,
        open_only=open_only,
        hide_closed=not show_closed,
        q=q,
        announced_after=announced_after,
        # 監視機関でチェックを外した発注機関の案件は最初から除外する
        exclude_agencies=db.list_agency_exclusions(),
        # このアカウント/設定の業種だけ表示
        vertical=current_vertical(),
    )
    matched = db.count_list_cases(**filters)          # 該当件数（上限なしの実数）
    total_pages = max(1, (matched + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = db.list_cases(sort=sort, limit=per_page, offset=(page - 1) * per_page, **filters)

    # ページング表示用（1始まりの「N〜M件目」）
    pg = {
        "page": page, "per_page": per_page, "total_pages": total_pages,
        "matched": matched,
        "start": (0 if matched == 0 else (page - 1) * per_page + 1),
        "end": min(page * per_page, matched),
        "has_prev": page > 1, "has_next": page < total_pages,
    }
    # 終了タブ／ページャのリンク用に現在のクエリを保持（各々で上書きするキーは除く）
    base_args = {k: request.args.getlist(k) for k in request.args if k != "closed"}
    page_args = {k: request.args.getlist(k) for k in request.args if k != "page"}

    # 画面の文脈ヘッダー（どの絞り込みで見ているかを一目で分かるように）。
    # サイドバーのどの項目を選んでいるか（nav_active）も併せて決める。
    _vlabel = verticals.get(current_vertical())["label"]  # 業種名（電気工事 / Web・制作 …）
    if budget_min > 0:
        man = f"{budget_min // 10000:,}"
        view = {
            "icon": "",
            "title": (f"今応募できる {man}万円以上の案件" if open_only
                      else f"{man}万円以上の案件"),
            "desc": "予定価格の高い順に表示中。" + (
                f"締切が今日以降の{_vlabel}案件にしぼっています。" if open_only
                else "予定価格が分かっている案件のみ対象です。"),
        }
        nav_active = "budget"
    elif fresh == "today":
        view = {"icon": "", "title": "本日の新着案件",
                "desc": f"今日公告された{_vlabel}案件です。"}
        nav_active = "new"
    elif fresh == "week":
        view = {"icon": "", "title": "新着案件（直近1週間）",
                "desc": f"直近7日間に公告された{_vlabel}案件です。"}
        nav_active = "new"
    else:
        view = {"icon": "", "title": "案件を探す",
                "desc": "地方・都道府県・業種・予定価格などで絞り込めます。"}
        nav_active = "cases"

    return render_template(
        "cases.html",
        view=view,
        nav_active=nav_active,
        rows=rows,
        regions=REGIONS,
        # 選択中の地方に応じた都道府県候補（未選択なら全国）
        pref_options=prefectures_in(region) if region else [],
        categories=db.distinct_values("category"),
        procurement_types=db.distinct_values("procurement_type"),
        bid_methods=db.distinct_values("bid_method"),
        budget_options=BUDGET_OPTIONS,
        spec_reasons=db.SPEC_REASONS,
        total=db.count_cases(),
        new_threshold=threshold,
        today=date.today().isoformat(),
        show_closed=show_closed,
        base_args=base_args,
        page_args=page_args,
        pg=pg,
        selected={
            "region": region, "prefecture": prefecture, "category": category,
            "procurement_type": procurement_type, "bid_method": bid_method,
            "spec_status": spec_status, "budget_min": budget_min,
            "open": open_only, "q": q, "sort": sort, "new": new_only,
            "fresh": fresh,
        },
    )


@app.route("/case/<int:case_id>")
def case_detail(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)
    agency_info = db.find_agency_for_case(case.get("agency", ""))
    # 応募導線（どこで申し込むか）は procurement.py が「確実なものだけ」を組み立てる。
    # スプレッドシートの当てにならない bid_url は使わない（誤誘導の原因だったため）。
    guide = procurement.application_guide(case, agency_info)
    # 必要書類・ToDo・応募内容を案件属性から確定的に導出（実行時AIなし）。
    requirements = procurement.application_requirements(case, guide)
    application = db.get_application(case_id)
    if application:
        application.setdefault("deadline", case.get("deadline", ""))
        application = _enrich_application(application)
    return render_template(
        "case_detail.html",
        c=case,
        spec_reasons=db.SPEC_REASONS,
        application=application,
        app_statuses=db.APP_STATUSES,
        submit_methods=db.SUBMIT_METHODS,
        status_class=STATUS_CLASS,
        agency_info=agency_info,
        guide=guide,
        requirements=requirements,
        ai_enabled=auth.can_use_ai(),
        ai_cached=bool(db.get_ai_assist(case.get("external_id", ""))),
    )


@app.route("/case/<int:case_id>/ai-assist", methods=["POST"])
def case_ai_assist(case_id: int):
    """【課金プラン・オンデマンド】AI応募アシストを生成して返す（タップ時のみ課金）。

    キャッシュがあれば即返す（再課金しない）。?refresh=1 で再生成。
    APIキー未設定なら enabled:false を返し、画面で有効化方法を案内する。
    """
    import json
    # このアカウントがAIモードを使えるか（ログイン＋ai_enabled＋鍵設定）を確認。
    if not auth.can_use_ai():
        return jsonify({"enabled": False,
                        "reason": "このアカウントではAIモードが有効化されていません。"})
    case = db.get_case(case_id)
    if not case:
        abort(404)
    ext = case.get("external_id", "")
    refresh = request.args.get("refresh") == "1"

    if not refresh:
        cached = db.get_ai_assist(ext)
        if cached:
            data = json.loads(cached["payload"])
            data["cached"] = True
            return jsonify(data)

    if not ai_assist.is_enabled():
        return jsonify({"enabled": False})

    try:
        requirements = procurement.application_requirements(case)
        result = ai_assist.assist(case, db.get_profile(), requirements)
    except Exception as e:  # noqa: BLE001 — AI失敗で500にせず画面で案内
        logging.getLogger(__name__).warning("ai assist failed", exc_info=True)
        return jsonify({"enabled": True, "error": str(e)[:200]}), 200

    if result.get("enabled") and ext:
        db.set_ai_assist(ext, json.dumps(result, ensure_ascii=False),
                         result.get("model", ""))
    result["cached"] = False
    return jsonify(result)


@app.route("/case/<int:case_id>/apply", methods=["POST"])
def apply_case(case_id: int):
    """案件の入札参加申請ステータスを登録・更新する。"""
    if not db.get_case(case_id):
        abort(404)
    import json
    f = request.form
    status = f.get("status", "").strip()

    # 既存値を起点に、フォームが「管理する」と宣言した項目だけ上書きする。
    # これでカンバンのモーダル（全項目）と案件詳細フォーム（一部）が同じ保存先を
    # 壊さず共有できる（未指定項目は消えない）。managed 未指定なら従来どおり全更新。
    cur = db.get_application(case_id) or {}
    managed_raw = f.get("managed")
    managed = set(s for s in (managed_raw or "").split(",") if s) if managed_raw else None

    def owns(key: str) -> bool:
        return managed is None or key in managed

    def text(key: str) -> str:
        return f.get(key, "").strip() if owns(key) else (cur.get(key) or "")

    def yen(key: str) -> int:
        return (db.yen_to_int(f.get(key, "")) or 0) if owns(key) else int(cur.get(key) or 0)

    def flag(key: str) -> bool:
        return bool(f.get(key)) if owns(key) else bool(cur.get(key))

    if owns("partners"):
        try:
            partners = json.loads(f.get("partners", "[]") or "[]")
        except (ValueError, TypeError):
            partners = []
    else:
        partners = cur.get("partners") or []

    fields = {
        "applied_date": text("applied_date"),
        "note": text("note"),
        "assignee": text("assignee"),
        "apply_deadline": text("apply_deadline"),
        "bid_deadline": text("bid_deadline"),
        "open_date": text("open_date"),
        "submit_method": text("submit_method"),
        "work": text("work"),
        "materials": text("materials"),
        "agency_override": text("agency_override"),
        "flag": text("flag"),
        "needs_check": flag("needs_check"),
        "bid_plan": yen("bid_plan"),
        "win_amount": yen("win_amount"),
        "award_called": flag("award_called"),
        "partner": text("partner"),
        "partners": partners,
    }
    is_ajax = bool(f.get("ajax") or request.headers.get("X-Requested-With") == "fetch")
    try:
        db.set_application(case_id, status, **fields)
    except ValueError:
        # 不正ステータスは「保存できなかった」ことを必ず伝える（AJAXでも握りつぶさない）。
        if is_ajax:
            return jsonify({"error": f"ステータスが不正です: {status}"}), 400
        flash("ステータスが不正です。", "error")
        return redirect(request.form.get("next") or url_for("case_detail", case_id=case_id))
    flash(f"申請状況を「{db.normalize_status(status)}」に更新しました。", "ok")
    if is_ajax:
        return ("", 204)
    return redirect(request.form.get("next") or url_for("case_detail", case_id=case_id))


@app.route("/applications/<int:case_id>/delete", methods=["POST"])
def application_delete(case_id: int):
    """案件を申請管理から削除（カンバンから外す）。案件自体は残る。"""
    db.delete_application(case_id)
    if request.headers.get("X-Requested-With") == "fetch" or request.is_json:
        return ("", 204)
    flash("申請管理から削除しました。", "ok")
    return redirect(url_for("applications"))


@app.route("/applications/restore", methods=["POST"])
def applications_restore():
    """ブラウザ(localStorage)に保存された申請をサーバDBへ復元する。

    サーバの denki_bid.db は毎日のデプロイで丸ごと差し替わり申請が消えるため、
    localStorage を真の保存先とし、ロード時にこのエンドポイントで静かに復元する。
    案件は external_id（再採番に強い安定キー）で現在の id に解決する。
    実際に新規・変更された件数だけ restored で返す（無駄な画面リロードの抑制用）。
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    restored = 0
    for it in items:
        ext = (it.get("external_id") or "").strip()
        status = db.normalize_status((it.get("status") or "").strip())
        if not ext or status not in db.APP_STATUSES:
            continue
        case_id = db.get_case_id_by_external(ext)
        if case_id is None:
            continue  # 現在のDBに該当案件が無い（公開終了等）→スキップ
        fields = dict(
            applied_date=(it.get("applied_date") or "").strip(),
            note=(it.get("note") or "").strip(),
            assignee=(it.get("assignee") or "").strip(),
            apply_deadline=(it.get("apply_deadline") or "").strip(),
            bid_deadline=(it.get("bid_deadline") or "").strip(),
            open_date=(it.get("open_date") or "").strip(),
            submit_method=(it.get("submit_method") or "").strip(),
            work=(it.get("work") or "").strip(),
            materials=(it.get("materials") or "").strip(),
            flag=(it.get("flag") or "").strip(),
            needs_check=bool(it.get("needs_check")),
            bid_plan=db.yen_to_int(str(it.get("bid_plan") or "")) or 0,
            win_amount=db.yen_to_int(str(it.get("win_amount") or "")) or 0,
            award_called=bool(it.get("award_called")),
            partner=(it.get("partner") or "").strip(),
            partners=it.get("partners") or [],
        )
        # localStorage を真の保存先として上書き復元する（揮発DB対策）。
        db.set_application(case_id, status, **fields)
        restored += 1
    return jsonify({"restored": restored})


@app.route("/applications")
def applications():
    """入札・工程＆協力会社 管理（bid-next-eta 互換のカンバン型・4タブ）。

    クライアント(JS)アプリにデータと設定をJSONで渡してレンダリングする。
    案件は applications テーブル（=管理に登録された案件）が母集団。
    """
    brand = verticals.get(current_vertical())
    rows = [_enrich_application(r) for r in db.list_applications(None)]
    config = {
        "statuses": [{"id": s, "accent": db.STATUS_ACCENT.get(s, "#94a3b8")}
                     for s in db.APP_STATUSES],
        "assignees": [{"id": a, "color": db.ASSIGNEE_COLOR.get(a, "#a8a29e")}
                      for a in db.ASSIGNEES],
        "works": brand.get("work_color", db.WORK_COLOR),
        "submit_methods": db.SUBMIT_METHODS,
        "today": date.today().isoformat(),
        "company_name": db.get_profile().get("company", "") or brand["label"],
    }
    return render_template(
        "applications.html",
        cases=rows,
        config=config,
        companies=db.list_companies(),
    )


@app.route("/companies", methods=["POST"])
def company_save():
    """協力会社の登録／更新（協力会社タブから JSON で呼ぶ）。"""
    data = request.get_json(silent=True) or {}
    if not str(data.get("name", "")).strip():
        return jsonify({"error": "会社名は必須です"}), 400
    cid = db.upsert_company(data)
    return jsonify({"id": cid, "companies": db.list_companies()})


@app.route("/companies/<int:company_id>/delete", methods=["POST"])
def company_delete(company_id: int):
    db.delete_company(company_id)
    return jsonify({"companies": db.list_companies()})


@app.route("/companies/restore", methods=["POST"])
def companies_restore():
    """localStorage に退避した協力会社をサーバへ復元（揮発DB対策）。

    サーバに1社も無いときだけ流し込む（重複登録を避ける）。
    """
    if db.count_companies() > 0:
        return jsonify({"restored": 0})
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    n = 0
    for it in items:
        if str(it.get("name", "")).strip():
            it.pop("id", None)  # サーバ側で採番し直す
            db.upsert_company(it)
            n += 1
    return jsonify({"restored": n, "companies": db.list_companies()})


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """マイ条件（対応エリア・業種・予算上限・保有資格）の設定。"""
    if request.method == "POST":
        prefectures = ",".join(request.form.getlist("prefectures"))
        # 業種・保有資格は複数選択。チェックに加え自由記入も結合する。
        categories = request.form.getlist("categories")
        cat_other = request.form.get("categories_other", "").strip()
        if cat_other:
            categories += [c.strip() for c in cat_other.split(",") if c.strip()]
        quals = request.form.getlist("quals")
        qual_other = request.form.get("quals_other", "").strip()
        if qual_other:
            quals += [q.strip() for q in qual_other.split(",") if q.strip()]
        budget_max = request.form.get("budget_max", "").strip()
        grade = request.form.get("grade", "").strip()
        company = request.form.get("company", "").strip()
        import json
        try:
            qualifications = json.loads(request.form.get("qualifications", "[]") or "[]")
        except (ValueError, TypeError):
            qualifications = []
        db.save_profile(prefectures, ",".join(categories) or verticals.get(current_vertical())["default_categories"],
                        budget_max, grade, ",".join(quals), company=company,
                        representative=request.form.get("representative", "").strip(),
                        address=request.form.get("address", "").strip(),
                        corp_number=request.form.get("corp_number", "").strip(),
                        qualifications=qualifications)
        flash("マイ条件を保存しました。マッチ案件・AI判定の等級照合に反映されます。", "ok")
        # 等級を編集して保存した時は、そのままマイ条件に留まる（連続編集しやすく）
        if request.form.get("stay"):
            return redirect(url_for("profile"))
        return redirect(url_for("matches"))

    prof = db.get_profile()
    return render_template(
        "profile.html",
        prof=prof,
        selected_prefs=[p for p in prof["prefectures"].split(",") if p],
        selected_cats=[c for c in prof["categories"].split(",") if c],
        selected_quals=[q for q in prof["quals"].split(",") if q],
        biz_types=verticals.get(current_vertical()).get("biz_types", BIZ_TYPES),
        qual_options=QUAL_OPTIONS,
        regions=REGIONS,
        grades=["", "A", "B", "C", "D", "E"],
    )


@app.route("/matches")
def matches():
    """マイ条件に合致する案件を、マッチ理由つきで表示。"""
    prof = db.get_profile()
    rows = db.match_cases(prof)
    return render_template(
        "matches.html",
        rows=rows,
        prof=prof,
        has_profile=bool(prof.get("prefectures")),
        spec_reasons=db.SPEC_REASONS,
        new_threshold=_new_threshold(),
    )


@app.route("/competitors")
def competitors():
    """自社の競合企業（落札者）の一覧。

    既定では「マイ条件の対応エリア」に絞り、「自社名」を除外して、
    “このシステムを使う会社（自社）の競合になりうる企業”だけを表示する。
    全国を見たい場合は ?all=1。
    """
    prof = db.get_profile()
    q = request.args.get("q", "").strip()
    prefecture = request.args.get("prefecture", "").strip()
    show_all = request.args.get("all") == "1"

    my_prefs = [p for p in (prof.get("prefectures") or "").split(",") if p]
    # 自社の対応エリアで絞る（all=1 か 手動で都道府県指定した時は除く）
    area = None if (show_all or prefecture) else (my_prefs or None)

    rows = db.list_competitors(
        q=q, prefecture=prefecture,
        prefectures=area,
        exclude_company=prof.get("company", ""),
    )
    return render_template(
        "competitors.html",
        rows=rows,
        prefectures=db.distinct_values("prefecture"),
        selected={"q": q, "prefecture": prefecture},
        my_company=prof.get("company", ""),
        my_area=my_prefs,
        scoped=bool(area),
        show_all=show_all,
    )


# 社名に "/" が含まれても拾えるよう path コンバータを使う（通常の <name> だと
# スラッシュでルートが切れて 404 になるため）。
@app.route("/competitor/<path:name>")
def competitor_detail(name: str):
    """1社の落札実績一覧。"""
    cases = db.competitor_cases(name)
    if not cases:
        abort(404)
    return render_template("competitor_detail.html", name=name, cases=cases)


@app.route("/export.csv")
def export_csv():
    """強化済みDB（全案件）をCSVでダウンロード。"""
    from flask import Response
    csv_text = db.export_cases_csv()
    return Response(
        "﻿" + csv_text,  # BOM付きでExcel文字化け防止
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=kawano_njss_cases.csv"},
    )


@app.route("/agencies")
def agencies():
    """監視対象の発注機関（全国）一覧。チェックを外すと案件を探すから除外される。"""
    import agency_import
    q = request.args.get("q", "").strip()
    rows = db.list_agencies(q=q)
    excluded = db.list_agency_exclusions()
    for r in rows:
        r["platform"] = agency_import.platform_of(r.get("domain", ""))
        r["included"] = r["name"] not in excluded  # チェック状態（既定ON）
    return render_template("agencies.html", rows=rows, q=q,
                           total=db.count_agencies(),
                           excluded_count=len(excluded))


@app.route("/agencies/toggle", methods=["POST"])
def agency_toggle():
    """1機関のチェックON/OFF（included=False で案件一覧から除外）。"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    included = bool(data.get("included"))
    if not name:
        return jsonify({"error": "name required"}), 400
    db.set_agency_excluded(name, excluded=not included)  # 含めない＝除外
    return jsonify({"name": name, "included": included,
                    "excluded": sorted(db.list_agency_exclusions())})


@app.route("/agencies/exclusions/restore", methods=["POST"])
def agency_exclusions_restore():
    """localStorage に退避した除外リストをサーバへ復元（揮発DB対策）。"""
    data = request.get_json(silent=True) or {}
    names = data.get("excluded") or []
    db.replace_agency_exclusions(names)
    return jsonify({"restored": len(names)})


@app.route("/api/prefectures")
def api_prefectures():
    """地方→都道府県の連動ドロップダウン用。"""
    region = request.args.get("region", "").strip()
    return jsonify(prefectures_in(region))


@app.template_filter("spec_label")
def spec_label(status: str) -> str:
    return {
        db.SPEC_AVAILABLE: "取得可",
        db.SPEC_UNAVAILABLE: "取得不可",
        db.SPEC_UNKNOWN: "未判定",
    }.get(status, "未判定")


if __name__ == "__main__":
    db.init_db()
    if db.count_cases() == 0:
        import seed_data
        n = seed_data.seed()
        print(f"DBが空だったのでサンプル {n} 件を投入しました。")
    # 環境変数で上書き可（デプロイ時は PORT/HOST が渡る）
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
