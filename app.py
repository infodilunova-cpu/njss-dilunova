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

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for

import db
import procurement
import ai_assist
import auth
import verticals
from regions import ALL_PREFECTURES, REGIONS, prefectures_in


def current_vertical() -> str:
    """このリクエストの業種テンプレ。ログイン中はそのアカウントの業種を最優先。
    未ログインは ?vertical= で切替可（sessionに保持し以降のページでも維持）、
    無ければ環境変数 DEFAULT_VERTICAL（さらに無ければ既定）。"""
    try:
        u = auth.current_user()
        if u and u.get("vertical") in verticals.VERTICALS:
            return u["vertical"]
    except Exception:  # noqa: BLE001
        pass
    # 未ログイン：業種トグル(?vertical=)での切替に対応。選んだ業種はsessionで維持。
    try:
        v = (request.args.get("vertical") or "").strip()
        if v in verticals.VERTICALS:
            session["vertical"] = v
            return v
        sv = session.get("vertical")
        if sv in verticals.VERTICALS:
            return sv
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
# api_data_health は外形監視（機械）が叩くため認証不要。集計値しか返さない。
_PUBLIC_ENDPOINTS = {"auth.login", "auth.signup", "static", "healthz", "api_data_health"}


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
    return {"vertical": vt, "brand": verticals.get(vt),
            "verticals_all": verticals.VERTICALS}


@app.context_processor
def inject_persist_on():
    """Supabase永続化が有効か。有効時はブラウザlocalStorageの自動復元を止める
    （ユーザー別のサーバ保存が真の保存先になるため。共有PCでの他人データ混入も防ぐ）。"""
    try:
        import supa
        return {"persist_on": supa.enabled()}
    except Exception:  # noqa: BLE001
        return {"persist_on": False}


@app.context_processor
def inject_profile_set():
    """無料ホストはディスク揮発のためマイ条件が消える。ブラウザ保存→自動復元の判定用に、
    サーバにマイ条件があるかを全テンプレへ渡す。"""
    try:
        return {"profile_set": bool(db.get_profile(user=auth.current_email()).get("prefectures"))}
    except Exception:  # noqa: BLE001
        return {"profile_set": False}


def _data_health() -> dict:
    """データの鮮度・件数・検品結果を1つの dict にまとめる。

    判定基準は data_expectations.py に集約してある（update.py の安全弁・毎日の監査・
    外形監視と**同じ期待値**を使う）。ここだけ独自のしきい値を持つと「ビルドは通ったのに
    画面には警告が出る／その逆」というズレが起きるため、絶対に基準を分岐させないこと。
    読み取りのみで軽量。
    """
    import data_expectations as dx

    info = {"total": 0, "latest": "", "stale": False, "stale_reason": "",
            "sources": {}, "critical": 0}
    try:
        with db._connect() as conn:
            counts = dx.source_counts(conn)
            # 画面に出るのは常に --fast 基準（厳しい方で誤報を出さない）
            findings = dx.inspect(conn, full=False)
            info["latest"] = dx.latest_announced(conn)
        info["total"] = sum(counts.values())
        info["sources"] = counts
        info["critical"] = sum(1 for f in findings if f.critical)
        if findings:
            info["stale"] = True
            info["stale_reason"] = "／".join(f.message for f in findings)
    except Exception:  # noqa: BLE001 — ヘルス表示の失敗で画面を落とさない
        pass
    return info


@app.context_processor
def inject_data_health():
    """データの鮮度・件数を全テンプレへ渡し、画面上部で警告できるようにする。"""
    return {"data_health": _data_health()}


@app.context_processor
def inject_save_health():
    """永続化(Supabase)への直近の保存が失敗していたら警告を全テンプレへ渡す。

    Render無料はディスク揮発のため、保存はSupabaseが本当の保管先。保存が無言で
    失敗すると次のデプロイでその変更が消える。運用者・利用者が気づけるようバナーを出す。
    """
    try:
        import supa  # このモジュールでは関数内importが慣例（先頭にimportが無い）
        down = supa.enabled() and not supa.last_save_ok()
        reason = supa.last_save_error() if down else ""
    except Exception:  # noqa: BLE001
        down, reason = False, ""
    return {"save_alert": down, "save_alert_reason": reason}


@app.route("/api/data-health")
def api_data_health():
    """データの健全性をJSONで返す（外形監視用・DBの読み取りだけ）。

    GitHub Actions の watchdog がこれを外から叩き、異常なら**ワークフローを赤くする**。
    画面のバナーは人が見に来ないと気づけないため、機械が毎日見る口を用意する。
    HTTPステータスも変える（正常200／重大503）ので、curl だけでも監視できる。
    返すのは件数などの集計値だけで、案件の中身や個人情報は含めない。
    """
    h = _data_health()
    body = {
        "ok": not h["stale"],
        "total": h["total"],
        "latest_announced": h["latest"],
        "critical": h["critical"],
        "sources": h["sources"],
        "reason": h["stale_reason"],
        "checked_at": date.today().isoformat(),
    }
    return jsonify(body), (503 if h["critical"] else 200)


@app.route("/healthz")
def healthz():
    """キープアライブ・死活監視用（認証不要・DBに軽く触って生存確認）。

    persist は Supabase永続化の状態（on=有効 / off=未設定）。設定直後の疎通確認に使う。
    """
    try:
        import supa
        return jsonify({"ok": True, "cases": db.count_cases(),
                        "persist": "on" if supa.enabled() else "off"})
    except Exception:  # noqa: BLE001 — 監視応答は落とさない
        return jsonify({"ok": False}), 500


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
        exclude_agencies=db.list_agency_exclusions(user=auth.current_email()),
        # 業種で分けず全件を統合表示（電気＋Web＋…を1つの盤面で横断検索）
        vertical=None,
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
    _vlabel = ""  # 業種で分けず全件統合表示のため、文言に業種名は入れない（→「案件」）
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
    application = db.get_application(case_id, user=auth.current_email())
    if application:
        application.setdefault("deadline", case.get("deadline", ""))
        application = _enrich_application(application)
    return render_template(
        "case_detail.html",
        c=case,
        # 過去の同名・類似案件の落札実績（毎年出る案件の前回額・落札者。非AI）
        past_awards=db.similar_past_awards(case),
        today=date.today().isoformat(),
        spec_reasons=db.SPEC_REASONS,
        application=application,
        app_statuses=db.APP_STATUSES,
        submit_methods=db.SUBMIT_METHODS,
        status_class=STATUS_CLASS,
        agency_info=agency_info,
        guide=guide,
        requirements=requirements,
        ai_enabled=auth.can_use_ai(),
        ai_cached=bool(db.get_ai_assist(case.get("external_id", ""), user=auth.current_email())),
        plan_cached=bool(db.get_ai_assist(
            _PLAN_CACHE_PREFIX + case.get("external_id", ""), user=auth.current_email())),
        # 入札額ガイド（落札実績の統計・非AI）。AIの結果と並記するためサーバ側でも渡す。
        price_guide=db.price_guide(case.get("category", ""), case.get("agency", "")),
    )


def _log_ai_usage(kind: str, result: dict) -> None:
    """AI生成1回を課金カウンターへ記録（キャッシュヒット・失敗は記録しない）。"""
    try:
        import supa
        if result.get("enabled") and not result.get("error"):
            u = result.get("usage") or {}
            supa.log_ai_usage(auth.current_email(), kind, result.get("model", ""),
                              u.get("in", 0), u.get("out", 0))
    except Exception:  # noqa: BLE001 — 記録失敗で本体機能を止めない
        logging.getLogger(__name__).warning("ai usage log failed", exc_info=True)


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
        cached = db.get_ai_assist(ext, user=auth.current_email())
        if cached:
            data = json.loads(cached["payload"])
            # 旧世代のキャッシュにも現行の判定ポリシーを適用して返す
            # （登録情報が無いのに〇/✕断定していた過去の結果を△に補正）。
            data["eligibility"] = ai_assist.apply_verdict_policy(
                data.get("eligibility"),
                ai_assist.profile_registered(db.get_profile(user=auth.current_email())))
            data["cached"] = True
            return jsonify(data)

    if not ai_assist.is_enabled():
        return jsonify({"enabled": False})

    try:
        requirements = procurement.application_requirements(case)
        result = ai_assist.assist(case, db.get_profile(user=auth.current_email()), requirements)
    except Exception as e:  # noqa: BLE001 — AI失敗で500にせず画面で案内
        logging.getLogger(__name__).warning("ai assist failed", exc_info=True)
        return jsonify({"enabled": True, "error": str(e)[:200]}), 200

    if result.get("enabled") and ext:
        db.set_ai_assist(ext, json.dumps(result, ensure_ascii=False),
                         result.get("model", ""), user=auth.current_email())
    _log_ai_usage("assist", result)
    result["cached"] = False
    return jsonify(result)


# 入札準備プランのキャッシュキー接頭辞。AI応募アシストと同じ ai_assist テーブルに
# 同居させつつ、判定結果（素の external_id）とは別枠で保持する。
_PLAN_CACHE_PREFIX = "plan:"
# 提出書類ドラフトのキャッシュキー接頭辞（書類名を含めて書類ごとに別キャッシュ）。
_DOC_CACHE_PREFIX = "doc:"


@app.route("/case/<int:case_id>/doc-draft", methods=["POST"])
def case_doc_draft(case_id: int):
    """【課金プラン・オンデマンド】提出書類1件の下書きを様式に沿って生成して返す。

    マイ条件にある情報（社名・代表者・住所・法人番号・資格/登録番号）は自動入力し、
    足りない欄は【◯◯を記入】＋ToDoで示す。キャッシュはユーザー×案件×書類名。
    """
    import json
    if not auth.can_use_ai():
        return jsonify({"enabled": False,
                        "reason": "このアカウントではAIモードが有効化されていません。"})
    case = db.get_case(case_id)
    if not case:
        abort(404)
    doc = str((request.get_json(silent=True) or {}).get("doc", "")).strip()[:120]
    if not doc:
        return jsonify({"enabled": True, "error": "書類名が指定されていません"}), 400
    ext = case.get("external_id", "")
    refresh = request.args.get("refresh") == "1"
    key = _DOC_CACHE_PREFIX + doc + ":" + ext

    if not refresh and ext:
        cached = db.get_ai_assist(key, user=auth.current_email())
        if cached:
            data = json.loads(cached["payload"])
            data["cached"] = True
            return jsonify(data)

    if not ai_assist.is_enabled():
        return jsonify({"enabled": False})

    try:
        requirements = procurement.application_requirements(case)
        result = ai_assist.doc_draft(case, doc,
                                     db.get_profile(user=auth.current_email()),
                                     requirements)
    except Exception as e:  # noqa: BLE001 — AI失敗で500にせず画面で案内
        logging.getLogger(__name__).warning("doc draft failed", exc_info=True)
        return jsonify({"enabled": True, "error": str(e)[:200]}), 200

    if result.get("enabled") and not result.get("error") and ext:
        db.set_ai_assist(key, json.dumps(result, ensure_ascii=False),
                         result.get("model", ""), user=auth.current_email())
    _log_ai_usage("doc", result)
    result["cached"] = False
    return jsonify(result)


@app.route("/case/<int:case_id>/notice-assets", methods=["POST"])
def case_notice_assets(case_id: int):
    """公告ページから様式・資料ファイルのリンクをコードで収集して返す（非AI）。

    結果は案件共有でキャッシュ（誰が取っても同じ）。?refresh=1 で再収集。
    """
    import json
    case = db.get_case(case_id)
    if not case:
        abort(404)
    ext = case.get("external_id", "")
    refresh = request.args.get("refresh") == "1"
    key = "assets:" + ext
    if not refresh and ext:
        cached = db.get_ai_assist(key, user="")
        if cached:
            data = json.loads(cached["payload"])
            data["cached"] = True
            return jsonify(data)
    import notice_fetch
    url = case.get("detail_url", "")
    page = notice_fetch.fetch(url) if url else {"text": "", "links": []}
    result = {"ok": True, "links": page.get("links") or [],
              "source_url": url,
              "note": ("" if page.get("links")
                       else "この案件の公告ページには配布ファイルの直接リンクが"
                            "載っていませんでした（元データがポータルの案内ページの"
                            "場合はここでは取れません）。公告原本は上の"
                            "「公告を開く／ウェブで探す」から確認してください。"
                            "書類の下書き自体は、入札準備プランの各書類の"
                            "「この書類を作る」でAIが作成できます。")}
    if ext:
        db.set_ai_assist(key, json.dumps(result, ensure_ascii=False), user="")
    result["cached"] = False
    return jsonify(result)


@app.route("/case/<int:case_id>/doc-status", methods=["GET", "POST"])
def case_doc_status(case_id: int):
    """提出書類のチェック状態（これ提出した/まだ）のユーザー別 取得・保存。"""
    case = db.get_case(case_id)
    if not case:
        abort(404)
    ext = case.get("external_id", "")
    u = auth.current_email()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        doc = str(body.get("doc", "")).strip()[:120]
        if not doc:
            return jsonify({"ok": False}), 400
        state = db.get_doc_state(ext, user=u)
        state[doc] = bool(body.get("checked"))
        db.set_doc_state(ext, state, user=u)
        return jsonify({"ok": True, "states": state})
    return jsonify({"ok": True, "states": db.get_doc_state(ext, user=u)})


@app.route("/case/<int:case_id>/bid-plan", methods=["POST"])
def case_bid_plan(case_id: int):
    """【課金プラン・オンデマンド】入札準備プラン（入札直前までの段取り）を生成して返す。

    認可・キャッシュの流儀は /ai-assist と同じ（タップ時のみ課金、?refresh=1 で再生成）。
    キャッシュは "plan:" 接頭辞で応募アシストと別枠。入札額ガイド(price_guide)は
    非AIの確定値なので、キャッシュの有無に関わらず毎回サーバ側で計算して並記する。
    """
    import json
    if not auth.can_use_ai():
        return jsonify({"enabled": False,
                        "reason": "このアカウントではAIモードが有効化されていません。"})
    case = db.get_case(case_id)
    if not case:
        abort(404)
    ext = case.get("external_id", "")
    refresh = request.args.get("refresh") == "1"
    # 落札実績の統計（非AI）。AIの price_hint と並記して数字の裏付けにする。
    guide = db.price_guide(case.get("category", ""), case.get("agency", ""))

    if not refresh and ext:
        cached = db.get_ai_assist(_PLAN_CACHE_PREFIX + ext, user=auth.current_email())
        if cached:
            data = json.loads(cached["payload"])
            data["cached"] = True
            data["price_guide"] = guide
            data["budget_yen"] = case.get("budget_yen") or 0
            return jsonify(data)

    if not ai_assist.is_enabled():
        return jsonify({"enabled": False})

    try:
        requirements = procurement.application_requirements(case)
        result = ai_assist.bid_plan(case, db.get_profile(user=auth.current_email()), requirements, guide,
                                    past_awards=db.similar_past_awards(case))
    except Exception as e:  # noqa: BLE001 — AI失敗で500にせず画面で案内
        logging.getLogger(__name__).warning("bid plan failed", exc_info=True)
        return jsonify({"enabled": True, "error": str(e)[:200]}), 200

    # 解析失敗（error付き）はキャッシュしない＝再タップで再挑戦できる。
    if result.get("enabled") and not result.get("error") and ext:
        db.set_ai_assist(_PLAN_CACHE_PREFIX + ext,
                         json.dumps(result, ensure_ascii=False),
                         result.get("model", ""), user=auth.current_email())
    _log_ai_usage("plan", result)
    result["cached"] = False
    result["price_guide"] = guide
    result["budget_yen"] = case.get("budget_yen") or 0
    return jsonify(result)


# save-output で既存申請から引き継ぐ項目（フォーム管理項目すべて）
_APP_KEEP_FIELDS = (
    "applied_date", "note", "assignee", "apply_deadline", "bid_deadline",
    "open_date", "submit_method", "work", "materials", "flag", "needs_check",
    "bid_plan", "win_amount", "award_called", "partner", "partners",
    "agency_override")


@app.route("/case/<int:case_id>/save-output", methods=["POST"])
def case_save_output(case_id: int):
    """AIアウトプット（判定/プラン/書類下書き）を案件管理（申請）に保存する。

    申請行が無ければ既定ステータスで自動作成する（＝案件管理に載る）。
    同じ kind+title は上書き。{delete: true} で削除。
    保存内容は申請データと一緒に Supabase へ永続化される（デプロイ跨ぎで残る）。
    """
    if not db.get_case(case_id):
        abort(404)
    u = auth.current_email()
    body = request.get_json(silent=True) or {}
    kind = str(body.get("kind", "")).strip()
    title = str(body.get("title", "")).strip()[:120]
    content = str(body.get("content", ""))[:30000]
    delete = bool(body.get("delete"))
    if kind not in ("assist", "plan", "doc") or not title or (not content and not delete):
        return jsonify({"ok": False, "error": "kind/title/content が不正です"}), 400

    cur = db.get_application(case_id, user=u) or {}
    outputs = [o for o in (cur.get("saved_outputs") or [])
               if not (o.get("kind") == kind and o.get("title") == title)]
    if not delete:
        outputs.append({"kind": kind, "title": title, "content": content,
                        "saved_at": date.today().isoformat()})
    fields = {k: cur.get(k) for k in _APP_KEEP_FIELDS}
    fields["saved_outputs"] = outputs
    status = cur.get("status") or "参加申請準備前"
    try:
        db.set_application(case_id, status, user=u, **fields)
    except ValueError:
        return jsonify({"ok": False, "error": "保存に失敗しました"}), 400
    return jsonify({"ok": True, "count": len(outputs), "created": not cur})


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
    cur = db.get_application(case_id, user=auth.current_email()) or {}
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
        # 保存したAIアウトプットはフォーム管理外＝常に既存値を引き継ぐ（消さない）
        "saved_outputs": cur.get("saved_outputs") or [],
    }
    is_ajax = bool(f.get("ajax") or request.headers.get("X-Requested-With") == "fetch")
    try:
        db.set_application(case_id, status, user=auth.current_email(), **fields)
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
    db.delete_application(case_id, user=auth.current_email())
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
        db.set_application(case_id, status, user=auth.current_email(), **fields)
        restored += 1
    return jsonify({"restored": restored})


@app.route("/applications")
def applications():
    """入札・工程＆協力会社 管理（bid-next-eta 互換のカンバン型・4タブ）。

    クライアント(JS)アプリにデータと設定をJSONで渡してレンダリングする。
    案件は applications テーブル（=管理に登録された案件）が母集団。
    """
    brand = verticals.get(current_vertical())
    rows = [_enrich_application(r) for r in db.list_applications(None, user=auth.current_email())]
    config = {
        "statuses": [{"id": s, "accent": db.STATUS_ACCENT.get(s, "#94a3b8")}
                     for s in db.APP_STATUSES],
        "assignees": [{"id": a, "color": db.ASSIGNEE_COLOR.get(a, "#a8a29e")}
                      for a in db.ASSIGNEES],
        "works": brand.get("work_color", db.WORK_COLOR),
        "submit_methods": db.SUBMIT_METHODS,
        "today": date.today().isoformat(),
        "company_name": db.get_profile(user=auth.current_email()).get("company", "") or brand["label"],
    }
    return render_template(
        "applications.html",
        cases=rows,
        config=config,
        companies=db.list_companies(user=auth.current_email()),
    )


@app.route("/companies", methods=["POST"])
def company_save():
    """協力会社の登録／更新（協力会社タブから JSON で呼ぶ）。"""
    data = request.get_json(silent=True) or {}
    if not str(data.get("name", "")).strip():
        return jsonify({"error": "会社名は必須です"}), 400
    cid = db.upsert_company(data, user=auth.current_email())
    return jsonify({"id": cid, "companies": db.list_companies(user=auth.current_email())})


@app.route("/companies/<int:company_id>/delete", methods=["POST"])
def company_delete(company_id: int):
    db.delete_company(company_id, user=auth.current_email())
    return jsonify({"companies": db.list_companies(user=auth.current_email())})


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
            db.upsert_company(it, user=auth.current_email())
            n += 1
    return jsonify({"restored": n, "companies": db.list_companies(user=auth.current_email())})


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
        # 業種は選んだものだけ保存（未選択なら空のまま。旧版の「電気工事」強制は廃止）
        db.save_profile(prefectures, ",".join(categories),
                        budget_max, grade, ",".join(quals), company=company,
                        representative=request.form.get("representative", "").strip(),
                        address=request.form.get("address", "").strip(),
                        corp_number=request.form.get("corp_number", "").strip(),
                        qualifications=qualifications,
                        user=auth.current_email())
        flash("マイ条件を保存しました。マッチ案件・AI判定の等級照合に反映されます。", "ok")
        # 等級を編集して保存した時は、そのままマイ条件に留まる（連続編集しやすく）
        if request.form.get("stay"):
            return redirect(url_for("profile"))
        return redirect(url_for("matches"))

    prof = db.get_profile(user=auth.current_email())
    return render_template(
        "profile.html",
        prof=prof,
        selected_prefs=[p for p in prof["prefectures"].split(",") if p],
        selected_cats=[c for c in prof["categories"].split(",") if c],
        selected_quals=[q for q in prof["quals"].split(",") if q],
        # 案件は全業種を統合表示しているため、対応業種・工種・資格の選択肢も全業種版を使う
        biz_types=verticals.ALL_BIZ_TYPES,
        qual_options=verticals.ALL_QUAL_OPTIONS,
        regions=REGIONS,
        grades=["", "A", "B", "C", "D", "E"],
    )


@app.route("/matches")
def matches():
    """マイ条件に合致する案件を、マッチ理由つきで表示。"""
    prof = db.get_profile(user=auth.current_email())
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
    prof = db.get_profile(user=auth.current_email())
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
    excluded = db.list_agency_exclusions(user=auth.current_email())
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
    db.set_agency_excluded(name, excluded=not included, user=auth.current_email())  # 含めない＝除外
    return jsonify({"name": name, "included": included,
                    "excluded": sorted(db.list_agency_exclusions(user=auth.current_email()))})


@app.route("/agencies/exclusions/restore", methods=["POST"])
def agency_exclusions_restore():
    """localStorage に退避した除外リストをサーバへ復元（揮発DB対策）。"""
    data = request.get_json(silent=True) or {}
    names = data.get("excluded") or []
    db.replace_agency_exclusions(names, user=auth.current_email())
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
