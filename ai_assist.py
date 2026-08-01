"""AI応募アシスト（課金プラン・オンデマンド／Gemini API）。

設計方針:
  無料プランは AI を一切呼ばない＝ランニングコスト0。ユーザーが案件詳細で
  「AIで応募準備」をタップしたときだけ Gemini を1回呼び、公告本文・必要書類・
  マイ条件（保有資格/エリア/等級）を読み込んで、

    ・この案件はこういう案件です（要約）
    ・あなたはこの資格を持っているので応募できます（参加資格の適合判定）
    ・この案件向けの必要書類はこれです（具体化）
    ・応募の一歩手前までのやることリスト

  を生成する。結果は DB にキャッシュするので、再タップでは課金されない。

有効化:
  環境変数 GEMINI_API_KEY を設定（ローカルは .env／本番は Render の secret）。
  未設定なら機能は休眠（ボタンは出るが、押すと有効化方法を案内するだけ）。

モデル:
  既定 gemini-2.5-flash（無料枠が大きく高速）。GEMINI_MODEL で上書き可。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import procurement

_ENV_PATH = Path(__file__).parent / ".env"
_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
# 全文PDFを読ませる最大文字数（Geminiの入力。3〜7千字が普通なので余裕を持たせる。
# flash は入力トークンが安く1Mコンテキストなので、様式・記載要領まで届くよう広めに取る）。
_PDF_MAX_CHARS = 28000


def _fetch_pdf_text(url: str, timeout: int = 25) -> str:
    """公告PDFを取得しテキスト化（pdftotext→pypdfフォールバック）。失敗時は ""。

    本番(Render)に poppler は無いので、pdftotext が無ければ pip の pypdf で抽出する。
    """
    if not url or not (url.lower().endswith(".pdf")):
        return ""
    # 公開Web(https)のPDFのみ取得。内部アドレス等への誤アクセスとメモリ肥大を防ぐ。
    if not url.lower().startswith("https://"):
        return ""
    _MAX_BYTES = 20 * 1024 * 1024  # 20MB上限（巨大PDFでメモリを食わない）
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read(_MAX_BYTES + 1)
        if len(data) > _MAX_BYTES:
            return ""  # 大きすぎる＝読まない
    except Exception:  # noqa: BLE001
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as f:
        f.write(data)
        f.flush()
        try:  # poppler があれば最良（主にローカル）
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", f.name, "-"],
                                 capture_output=True, timeout=30)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", "ignore")[:_PDF_MAX_CHARS]
        except Exception:  # noqa: BLE001
            pass
        try:  # 本番含むどこでも動く（pure-python）
            import pypdf
            r = pypdf.PdfReader(f.name)
            return "\n".join((p.extract_text() or "") for p in r.pages)[:_PDF_MAX_CHARS]
        except Exception:  # noqa: BLE001
            return ""


def _fetch_notice(url: str, title: str = "") -> tuple[str, list[dict]]:
    """公告の本文と配布ファイルリンクを取得する（コードで直接読みに行く層）。

    優先順: ① PDF直リンクなら全文抽出 ② HTMLページなら本文＋様式/資料リンクを収集し、
    公告/仕様書らしきPDFが貼られていれば1本だけ読み足す。
    【重要ガード】案件名がページ内に見つからないHTMLは「ポータルの汎用ページ」と
    みなし本文採用しない（官公需APIのdetail_urlは汎用ページのことが多く、
    それをAIに読ませると公告本文と誤認してノイズになるため）。
    返り値: (本文テキスト, links[{label,url,kind}])。取れなければ ("", [])。
    """
    text = _fetch_pdf_text(url)
    if text:
        return text, []
    try:
        import notice_fetch
        page = notice_fetch.fetch(url)
    except Exception:  # noqa: BLE001
        return "", []
    text = (page.get("text") or "")[:_PDF_MAX_CHARS]
    links = page.get("links") or []
    if not text and not links:
        return "", []
    # 汎用ページ判定: 案件名（先頭12字・空白無視）がページ内に無ければ公告ではない
    import unicodedata as _ud
    import re as _re2
    probe = _re2.sub(r"\s", "", _ud.normalize("NFKC", title or ""))[:12]
    page_flat = _re2.sub(r"\s", "", _ud.normalize("NFKC", text))
    if probe and probe not in page_flat:
        return "", [l for l in links if l.get("kind") == "form"]
    # 公告・説明書・仕様書らしきPDFがあれば、最も本文らしい1本を読み足す
    import re as _re
    for l in links:
        if l["url"].lower().endswith(".pdf") and _re.search(r"公告|公示|説明|仕様", l["label"]):
            pdf = _fetch_pdf_text(l["url"])
            if pdf:
                text = (text + f"\n\n# リンク先PDF（{l['label']}）\n" + pdf)[:_PDF_MAX_CHARS]
            break
    return text, links


def _links_lines(links: list[dict] | None) -> str:
    """公告ページの配布ファイル一覧をプロンプト用テキストにする（実在リンク＝元ネタ）。"""
    if not links:
        return "（配布ファイルは検出できず）"
    out = []
    for l in links[:20]:
        kind = "様式" if l.get("kind") == "form" else "資料"
        out.append(f"- [{kind}] {l.get('label','')}")
    return "\n".join(out)


def _load_env() -> None:
    """.env（gitignore済）があれば、未設定のキーだけ os.environ に読み込む。

    本番(Render)は環境変数を直接設定するので .env は無くてよい。ローカル開発用。
    """
    if os.environ.get("GEMINI_API_KEY") or not _ENV_PATH.exists():
        return
    try:
        for line in _ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
    except OSError:
        pass


def _api_key() -> str:
    _load_env()
    return os.environ.get("GEMINI_API_KEY", "")


def _model() -> str:
    _load_env()
    return os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def is_enabled() -> bool:
    """AI機能が有効か（Geminiのキーが設定されているか）。"""
    return bool(_api_key())


# Gemini の構造化出力スキーマ（responseSchema）。これで型を保証＝壊れにくい。
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "array", "items": {"type": "string"},
            "description": "この案件の要点を3行で（何を・どこが発注・締切や金額の要点）",
        },
        "eligibility": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "description": "〇/△/✕/不明 のいずれか"},
                "reasons": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "reasons"],
        },
        "documents": {
            "type": "array", "items": {"type": "string"},
            "description": "この案件で実際に要りそうな提出書類を案件に即して具体化",
        },
        "todo": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
            },
            "description": "応募一歩手前までにやることを順番に。最後は『入札書を出す直前』まで。",
        },
        "cautions": {
            "type": "array", "items": {"type": "string"},
            "description": "見落としやすい注意点（締切・資格要件・窓口受領のみ 等）",
        },
    },
    "required": ["summary", "eligibility", "documents", "todo", "cautions"],
}

_SYSTEM = (
    "あなたは日本の公共入札（工事・役務・物品の全業種）に精通した入札支援の専門家です。"
    "与えられた案件の公告本文・確定的に算出済みの必要書類・ユーザーの保有資格(マイ条件)を"
    "読み込み、この事業者がこの案件に『応募する一歩手前』まで到達できるよう具体的に支援します。"
    "一般論ではなく、この案件の実態に即して書くこと。"
    "参加資格の適合判定(verdict)は次のルールで決めること: "
    "【1】マイ条件に保有資格・等級・機関別資格などの登録情報が無い（未設定）場合は、"
    "〇/✕を断定せず必ず △ とし、reasons の先頭で登録情報が未登録のため判定できないことを伝える。"
    "【2】登録情報がある場合: 公告が要求する資格・許可・等級・登録"
    "（例: 要求A等級、建設業許可、警備業認定、ISO、地域要件）のうち自社に無いものが"
    "本文から明確なら verdict を ✕ とし、reasons の先頭に"
    "『不足: ◯◯（要求◯◯／自社◯◯または未保有）』の形で何が無いかを必ず明記する。"
    "【3】要求要件を自社の登録情報が満たしていると確認できる場合のみ 〇。"
    "【4】公告に要件の記載が無い・判断材料が不足する場合は △。"
    "とくに『等級(ランク／格付け：A・B・C等)』は公告本文から読み取り自社等級と照合し"
    "（例: 要求A、自社C＝等級不足で✕）、verdict に関わらず reasons の中に必ず1項目"
    "『等級: 要求◯◯／自社◯◯』を入れること"
    "（公告に等級の記載が無ければ『等級: 公告に記載なし』、自社等級が未設定なら『自社未設定』と書く）。"
    "公告本文に書かれていない要件を推測で創作しないこと。"
    "必要書類は発注機関により異なるため、最終確認は公告に当たるよう注意書きを添えること。"
    "出力は必ず指定のJSONスキーマに従い、日本語で記述すること。"
)


# ---- 参加資格判定のポリシー（決定的・AI出力の補正）-------------------------

_UNREGISTERED_REASON = (
    "マイ条件に保有資格・等級などの登録情報が未登録のため、適合判定できません。"
    "「マイ条件」で資格・等級を登録すると 〇/✕ で判定します。"
)


def profile_registered(profile: dict | None) -> bool:
    """マイ条件に「適合判定の材料になる登録情報」が入っているか。

    経審等級・保有資格・発注機関別の入札参加資格のいずれかが入って初めて
    登録済みとみなす（対応業種はDB既定値が入るため判定材料に数えない。
    会社名だけでも判定はできない）。
    """
    p = profile or {}
    if any(str(p.get(k) or "").strip() for k in ("grade", "quals")):
        return True
    return any(str(q.get("issuer") or "").strip()
               for q in (p.get("qualifications") or []) if isinstance(q, dict))


def apply_verdict_policy(elig: dict | None, registered: bool) -> dict[str, Any]:
    """参加資格判定にポリシーを決定的に適用する（AIの断定しすぎを防ぐ最終関門）。

    - 登録情報が白紙 → 判定は必ず △（材料が無いのに 〇/✕ を出さない）
    - 登録あり → ✕ は「何が無いか」の理由がある時だけ。理由なしの✕や
      『不明』などスキーマ外の値は △ に丸める（UIは〇/△/✕の3状態）
    """
    e = dict(elig or {})
    reasons = [str(r).strip() for r in (e.get("reasons") or []) if str(r).strip()]
    verdict = str(e.get("verdict") or "").strip()
    if not registered:
        e["verdict"] = "△"
        e["reasons"] = [_UNREGISTERED_REASON] + reasons
        return e
    if verdict == "✕" and not reasons:
        verdict = "△"
    if verdict not in ("〇", "△", "✕"):
        verdict = "△"
    e["verdict"] = verdict
    e["reasons"] = reasons or [
        "公告本文から判定材料を特定できませんでした。公告原本で参加資格要件を確認してください。"]
    return e


def _profile_lines(profile: dict | None) -> str:
    p = profile or {}
    parts = []
    if p.get("company"):
        parts.append(f"自社名: {p['company']}")
    if p.get("prefectures"):
        parts.append(f"対応エリア(都道府県): {p['prefectures']}")
    if p.get("categories"):
        parts.append(f"対応業種: {p['categories']}")
    if p.get("grade"):
        parts.append(f"経審等級(全国基準の参考): {p['grade']}")
    if p.get("quals"):
        parts.append(f"保有資格: {p['quals']}")
    if p.get("budget_max"):
        parts.append(f"予算上限の目安: {p['budget_max']}")
    # 発注機関別の等級（資格通知書ベース）。AIはこの案件の発注機関に一致する行を優先して照合する。
    quals = p.get("qualifications") or []
    if quals:
        lines = []
        for q in quals:
            issuer = (q.get("issuer") or "").strip()
            if not issuer:
                continue
            seg = f"{issuer}：{q.get('category') or '工種?'} {q.get('grade') or '等級記載なし'}"
            if q.get("score"):
                seg += f"({q['score']}点)"
            if q.get("number"):
                seg += f" 登録番号:{q['number']}"  # 申請書類の自動入力に使う
            lines.append(seg)
        if lines:
            parts.append(
                "発注機関別の入札参加資格・等級（同じ経審点でも機関で等級が異なる。"
                "この案件の発注機関に一致する行を最優先で等級照合に使うこと）:\n  - "
                + "\n  - ".join(lines))
    return "\n".join(parts) if parts else "（マイ条件は未設定）"


def _requirements_lines(req: dict | None) -> str:
    if not req:
        return "（必要書類の確定情報なし）"
    docs = req.get("documents") or []
    req_docs = [d["label"] for d in docs if d.get("required")]
    opt_docs = [d["label"] for d in docs if not d.get("required")]
    lines = [f"区分: {req.get('procurement_kind', '不明')}"]
    if req_docs:
        lines.append("必須(確定): " + " / ".join(req_docs))
    if opt_docs:
        lines.append("任意/確認(確定): " + " / ".join(opt_docs))
    return "\n".join(lines)


def _build_user_text(case: dict, profile: dict | None, req: dict | None,
                     notice_text: str = "") -> str:
    # 公告本文は「全文PDF（取得できた場合）」を優先。無ければ保存済み説明文(2000字)。
    desc = (notice_text or case.get("description") or "").strip()
    src_label = "公告全文（PDFから取得）" if notice_text else "公告本文（抜粋・2000字まで）"
    return (
        "# 案件\n"
        f"案件名: {case.get('title', '')}\n"
        f"発注機関: {case.get('agency', '')}（{case.get('agency_type', '')}）\n"
        f"都道府県: {case.get('prefecture', '')} / 地方: {case.get('region', '')}\n"
        f"業種: {case.get('category', '')}\n"
        f"入札方式: {case.get('bid_method', '') or '不明'}\n"
        f"公告日: {case.get('announced_date', '') or '不明'} / 申込締切: {case.get('deadline', '') or '不明'}\n"
        f"予定価格: {case.get('budget', '') or '非公表/不明'}\n\n"
        f"# {src_label}\n"
        f"{desc or '（本文なし。公告ページで要確認）'}\n\n"
        "# 確定的に算出済みの必要書類（土台。AIはこれを案件に即して具体化・補強する）\n"
        f"{_requirements_lines(req)}\n\n"
        "# 自社（マイ条件）\n"
        f"{_profile_lines(profile)}\n\n"
        "注意: 上記の公告本文に書かれている事実のみを根拠にし、書かれていない具体値"
        "（等級・面積・金額・日付等）は創作しないこと。本文で確認できない要件は"
        "『公告で確認』と述べること。"
    )


def _call_gemini(user_text: str, *, system: str | None = None,
                 schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Gemini に構造化出力で問い合わせ、JSON dict を返す（依存はstdlibのみ）。

    system/schema を省略すると従来どおり応募アシスト用（_SYSTEM/_SCHEMA）。
    入札準備プラン等、別スキーマの生成でも同じ呼び口を共用する。
    """
    key, model = _api_key(), _model()
    url = f"{_API_BASE}/{model}:generateContent?key={key}"
    body = {
        "systemInstruction": {"parts": [{"text": system if system is not None else _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": schema if schema is not None else _SCHEMA,
            "temperature": 0.3,
        },
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")

    def _once() -> dict[str, Any]:
        with urllib.request.urlopen(req, timeout=90) as res:
            data = json.loads(res.read().decode("utf-8"))
        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or [{}]
        text = parts[0].get("text", "{}")
        return json.loads(text)

    # flash運用の堅牢化: 応答JSONが稀に壊れる（途中切れ等）ため1回だけ再試行する。
    try:
        return _once()
    except (ValueError, KeyError):
        return _once()


def assist(case: dict, profile: dict | None = None,
           requirements: dict | None = None) -> dict[str, Any]:
    """案件1件に対しオンデマンドで AI 応募アシストを生成して返す。

    返り値: {"enabled": bool, "model": str, ...スキーマの各キー}。
    キー未設定なら {"enabled": False} を返す（呼び出し側で案内表示）。
    """
    if not is_enabled():
        return {"enabled": False}

    if requirements is None:
        try:
            requirements = procurement.application_requirements(case)
        except Exception:  # noqa: BLE001 — 土台が無くてもAIは動かす
            requirements = None

    # タップ時に公告（PDF全文 or HTMLページ＋リンク先PDF）をコードで取得してAIに読ませる。
    notice_text, _links = _fetch_notice(case.get("detail_url", ""), case.get("title", ""))
    data = _call_gemini(_build_user_text(case, profile, requirements, notice_text))
    # 判定ポリシーを最終適用（登録情報なし→△固定、根拠なし✕→△。AI任せにしない）
    data["eligibility"] = apply_verdict_policy(
        data.get("eligibility"), profile_registered(profile))
    data["enabled"] = True
    data["model"] = _model()
    data["source"] = "pdf_full" if notice_text else "description"
    return data


# ============================================================
# 入札準備プラン（入札直前まで導く・オンデマンド）
# ============================================================

# Gemini の構造化出力スキーマ（入札準備プラン用）。応募アシストとは別スキーマ。
_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schedule": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string",
                             "description": "YYYY-MM-DD 形式の目安日（幅がある場合は「〜7/20」等も可）"},
                    "action": {"type": "string"},
                },
                "required": ["date", "action"],
            },
            "description": "今日から入札書提出までの逆算スケジュール"
                           "（公告確認→説明会/質問期限→参加申請→仕様書精読→見積・体制→入札書提出）",
        },
        "documents": {
            "type": "array", "items": {"type": "string"},
            "description": "提出書類チェックリスト（この案件に即して具体化）",
        },
        "draft": {
            "type": "string",
            "description": "参加申請書・様式に書く自社紹介文の下書き（150〜300字）",
        },
        "price_hint": {
            "type": "string",
            "description": "入札額の考え方（渡された落札実績統計の要約＋注意）",
        },
        "risks": {
            "type": "array", "items": {"type": "string"},
            "description": "この案件でつまずきやすいポイント",
        },
        "next_action": {
            "type": "string",
            "description": "今日やるべき最初の一歩を1文で",
        },
    },
    "required": ["schedule", "documents", "draft", "price_hint", "risks", "next_action"],
}

_PLAN_SYSTEM = (
    "あなたは日本の公共入札に精通した入札支援の専門家です。"
    "与えられた案件の公告本文・確定的に算出済みの必要書類・落札実績の統計・"
    "自社のマイ条件を読み込み、この事業者が『入札書を提出する直前』まで迷わず"
    "進めるよう、実行順のプランを組み立てます。"
    "schedule は今日の日付と申込締切から逆算し、"
    "公告の確認→現場説明会/質問書の期限→参加申請の提出→仕様書の精読→"
    "見積作成・体制確保→入札書の提出、の順で日付の目安（YYYY-MM-DD）を付けること。"
    "締切が過ぎている・不明な場合はその旨を schedule の action に明記すること。"
    "draft は自社名・保有資格・実績（マイ条件にある事実のみ）から150〜300字で、"
    "参加申請書にそのまま書ける丁寧な文体にすること。マイ条件に無い実績を創作しないこと。"
    "price_hint は渡された落札実績統計（非AIの確定値）の要約に留め、"
    "統計が無い場合は無いと明記し、根拠のない金額を提示しないこと。"
    "公告本文に書かれていない具体値（日付・金額・要件）は創作せず『公告で確認』と述べること。"
    "出力は必ず指定のJSONスキーマに従うJSONのみを日本語で返すこと。"
)


def _price_guide_lines(guide: dict | None) -> str:
    """db.price_guide() の統計をプロンプト用テキストにする（無ければ明示）。"""
    if not guide:
        return "（同カテゴリの落札実績データなし。price_hint では統計が無い旨を伝えること）"
    lines = []
    c = guide.get("category_stats")
    if c:
        lines.append(
            f"同カテゴリ「{guide.get('category', '')}」の落札額: {c['count']}件 / "
            f"中央値 {c['median']:,}円 / 25〜75%範囲 {c['p25']:,}〜{c['p75']:,}円")
    a = guide.get("agency_stats")
    if a:
        lines.append(
            f"同一発注機関「{guide.get('agency', '')}」の同カテゴリ落札額: {a['count']}件 / "
            f"中央値 {a['median']:,}円 / 25〜75%範囲 {a['p25']:,}〜{a['p75']:,}円")
    w = guide.get("win_rate")
    if w:
        lines.append(f"予定価格に対する落札率の中央値: {w['median']:.1%}（{w['count']}件）")
    return "\n".join(lines) if lines else "（統計を算出できる落札実績が不足）"


def _past_awards_lines(past_awards: list | None) -> str:
    """過去の同名・類似案件の落札実績をプロンプト用テキストにする。"""
    if not past_awards:
        return "（同名・類似の過去実績は見つからず）"
    lines = []
    for p in past_awards[:6]:
        kind = "同名(年度違い)" if p.get("kind") == "same" else "類似"
        lines.append(f"- [{kind}] {p.get('title','')}（公告 {p.get('announced_date') or '?'}）"
                     f" 落札者: {p.get('winner','?')} 落札額: {p.get('win_price') or '不明'}")
    return "\n".join(lines)


def _build_plan_text(case: dict, profile: dict | None, req: dict | None,
                     price_guide: dict | None, notice_text: str = "",
                     today: str = "", past_awards: list | None = None) -> str:
    """入札準備プラン生成用のユーザープロンプトを組み立てる（純関数・テスト対象）。"""
    today = today or date.today().isoformat()
    desc = (notice_text or case.get("description") or "").strip()
    src_label = "公告全文（PDFから取得）" if notice_text else "公告本文（抜粋・2000字まで）"
    deadline = case.get("deadline", "") or "不明"
    return (
        f"# 今日の日付\n{today}\n\n"
        "# 案件\n"
        f"案件名: {case.get('title', '')}\n"
        f"発注機関: {case.get('agency', '')}（{case.get('agency_type', '')}）\n"
        f"都道府県: {case.get('prefecture', '')} / 地方: {case.get('region', '')}\n"
        f"業種: {case.get('category', '')}\n"
        f"入札方式: {case.get('bid_method', '') or '不明'}\n"
        f"公告日: {case.get('announced_date', '') or '不明'}\n"
        f"申込締切: {deadline}（schedule はこの締切と今日の日付から逆算すること）\n"
        f"予定価格: {case.get('budget', '') or '非公表/不明'}\n\n"
        f"# {src_label}\n"
        f"{desc or '（本文なし。公告ページで要確認）'}\n\n"
        "# 確定的に算出済みの必要書類（土台。documents はこれを案件に即して具体化する）\n"
        f"{_requirements_lines(req)}\n\n"
        "# 落札実績の統計（非AIの確定値。price_hint はこの数字の要約＋注意に限ること）\n"
        f"{_price_guide_lines(price_guide)}\n\n"
        "# 過去の同名・類似案件の落札実績（毎年出る定例案件なら前回実績が最有力の参考。\n"
        "#  price_hint で必ず言及すること）\n"
        f"{_past_awards_lines(past_awards)}\n\n"
        "# 自社（マイ条件。draft はここにある事実のみで書くこと）\n"
        f"{_profile_lines(profile)}\n\n"
        "注意: 上記に書かれている事実のみを根拠にし、書かれていない具体値"
        "（日付・金額・要件等）は創作しないこと。本文で確認できない事項は"
        "『公告で確認』と述べること。"
    )


def _normalize_plan(data: Any) -> dict[str, Any]:
    """Gemini応答を検証・正規化する。使い物にならない形なら ValueError。

    responseSchema で型はほぼ保証されるが、欠損・空応答でUIが壊れないよう
    最終防衛線としてここで形を確定させる。
    """
    if not isinstance(data, dict):
        raise ValueError("応答がJSONオブジェクトではありません")
    schedule = []
    for s in data.get("schedule") or []:
        if isinstance(s, dict) and str(s.get("action") or "").strip():
            schedule.append({"date": str(s.get("date") or "").strip(),
                             "action": str(s["action"]).strip()})
    out = {
        "schedule": schedule,
        "documents": [str(x).strip() for x in (data.get("documents") or []) if str(x).strip()],
        "draft": str(data.get("draft") or "").strip(),
        "price_hint": str(data.get("price_hint") or "").strip(),
        "risks": [str(x).strip() for x in (data.get("risks") or []) if str(x).strip()],
        "next_action": str(data.get("next_action") or "").strip(),
    }
    if not out["schedule"] and not out["next_action"]:
        raise ValueError("スケジュールが空の応答です")
    return out


def bid_plan(case: dict, profile: dict | None = None,
             requirements: dict | None = None,
             price_guide: dict | None = None,
             past_awards: list | None = None) -> dict[str, Any]:
    """案件1件の「入札直前まで」の準備プランをオンデマンド生成して返す。

    assist() と同じ流儀: キー未設定なら {"enabled": False}。公告PDFの全文を
    読めれば読み、締切逆算スケジュール・提出書類・申請書の下書き・入札額の
    考え方（price_guide は db.price_guide() の非AI統計）・リスク・次の一歩を返す。
    応答のパースに失敗したら安全なエラーdict（enabled + error）を返す。
    """
    if not is_enabled():
        return {"enabled": False}

    if requirements is None:
        try:
            requirements = procurement.application_requirements(case)
        except Exception:  # noqa: BLE001 — 土台が無くてもAIは動かす
            requirements = None

    notice_text, _links = _fetch_notice(case.get("detail_url", ""), case.get("title", ""))
    text = _build_plan_text(case, profile, requirements, price_guide, notice_text,
                            past_awards=past_awards)
    try:
        data = _normalize_plan(
            _call_gemini(text, system=_PLAN_SYSTEM, schema=_PLAN_SCHEMA))
    except ValueError as e:  # JSONDecodeError 含む＝応答が壊れている
        return {"enabled": True, "error": f"AI応答の解析に失敗しました: {e}"[:200]}
    data["enabled"] = True
    data["model"] = _model()
    data["source"] = "pdf_full" if notice_text else "description"
    return data


# ============================================================
# 提出書類ドラフト（1書類ずつ、機関の様式に沿って作る・オンデマンド）
# ============================================================

_DOC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "doc_title": {"type": "string", "description": "作成する書類の正式名称"},
        "official_format": {
            "type": "string",
            "description": "この機関の公式様式についての案内（様式名・番号・入手場所）。"
                           "公告本文から特定できなければ『公式様式は公告ページの様式集で確認。"
                           "以下は一般的な様式に沿った下書き』と明示する",
        },
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "記入欄の名前（例: 商号又は名称）"},
                    "value": {"type": "string",
                              "description": "マイ条件から自動入力できた値。分からなければ空文字"},
                    "todo": {"type": "string",
                             "description": "value が空のとき、ユーザーが何をどこで用意するか1文。"
                                            "value が入っていれば空文字"},
                },
                "required": ["label", "value", "todo"],
            },
            "description": "この書類の記入欄を上から順に。自動入力できた欄も含めて全部列挙",
        },
        "body": {
            "type": "string",
            "description": "そのまま書き写せる書類本文の下書き（宛名・日付欄・記入欄を含む全文）。"
                           "自動入力できた値は埋め、不明な箇所は【◯◯を記入】の形で明示",
        },
        "notes": {"type": "array", "items": {"type": "string"},
                  "description": "提出前の注意（押印・部数・綴じ方・提出先・期限など公告から分かる範囲）"},
    },
    "required": ["doc_title", "official_format", "fields", "body", "notes"],
}

_DOC_SYSTEM = (
    "あなたは日本の公共入札の提出書類（参加申請書・入札書・実績調書等）の作成支援の専門家です。"
    "指定された1つの書類について、公告本文から様式・記載要領を読み取り、その発注機関の"
    "指定様式にできるだけ沿った『そのまま書き写せる下書き』を作ります。"
    "【自動入力】マイ条件にある事実（社名・代表者・住所・法人番号・保有資格・機関別の"
    "入札参加資格の等級や登録番号）は該当欄に必ず埋め込むこと。"
    "【不足分】マイ条件に無い情報は本文中に【◯◯を記入】と明示し、fields の todo に"
    "『何をどこで用意するか』を具体的に書くこと（例: 納税証明書その3の3を税務署で取得）。"
    "【創作禁止】公告本文・マイ条件に無い様式番号・日付・金額・要件を作らないこと。"
    "様式が特定できない場合はその旨を official_format で正直に伝え、官公庁で一般的な"
    "様式に沿って作ること。出力は必ず指定のJSONスキーマに従い、日本語で記述すること。"
)


def _postfill_doc_fields(data: dict[str, Any], profile: dict | None) -> dict[str, Any]:
    """マイ条件にある基本事実を fields に確実に反映する（決定的な補完）。

    flash がたまに自動入力を書き漏らしても、コード側で社名・代表者・住所・
    法人番号を先頭に補う（値が既に fields/body に入っていれば足さない）。
    """
    p = profile or {}
    fields = list(data.get("fields") or [])
    body = data.get("body") or ""

    def _already(v: str) -> bool:
        return any(v in (f.get("value") or "") for f in fields) or (v in body)

    std = [("商号又は名称", str(p.get("company") or "").strip()),
           ("代表者氏名", str(p.get("representative") or "").strip()),
           ("所在地", str(p.get("address") or "").strip()),
           ("法人番号", str(p.get("corp_number") or "").strip())]
    add = [{"label": lb, "value": v, "todo": ""}
           for lb, v in std if v and not _already(v)]
    if add:
        data["fields"] = add + fields
    return data


def doc_draft(case: dict, doc_name: str, profile: dict | None = None,
              requirements: dict | None = None) -> dict[str, Any]:
    """提出書類1件の下書きをオンデマンド生成して返す（assist と同じ流儀）。"""
    if not is_enabled():
        return {"enabled": False}
    doc_name = (doc_name or "").strip()
    if not doc_name:
        return {"enabled": True, "error": "書類名が指定されていません"}

    if requirements is None:
        try:
            requirements = procurement.application_requirements(case)
        except Exception:  # noqa: BLE001 — 土台が無くてもAIは動かす
            requirements = None

    notice_text, links = _fetch_notice(case.get("detail_url", ""), case.get("title", ""))
    desc = (notice_text or case.get("description") or "").strip()
    src_label = "公告全文（PDFから取得）" if notice_text else "公告本文（抜粋）"
    text = (
        f"# 作成する書類（この1つだけ）\n{doc_name}\n\n"
        "# 案件\n"
        f"案件名: {case.get('title', '')}\n"
        f"発注機関: {case.get('agency', '')}（{case.get('agency_type', '')}）\n"
        f"申込締切: {case.get('deadline', '') or '不明'} / 入札方式: {case.get('bid_method', '') or '不明'}\n\n"
        f"# {src_label}\n{desc or '（本文なし。様式は一般形で作成し、公告での確認を促すこと）'}\n\n"
        "# 確定的に算出済みの必要書類の情報\n"
        f"{_requirements_lines(requirements)}\n\n"
        "# 公告ページで実際に配布されているファイル一覧（コードで収集した実在リスト。\n"
        "#  official_format はこの中に該当様式があればそれを名指しし、無ければ無いと書くこと）\n"
        f"{_links_lines(links)}\n\n"
        "# 自社（マイ条件。ここにある事実は該当欄へ自動入力すること）\n"
        f"{_profile_lines(profile)}\n"
    )
    try:
        data = _call_gemini(text, system=_DOC_SYSTEM, schema=_DOC_SCHEMA)
    except ValueError as e:
        return {"enabled": True, "error": f"AI応答の解析に失敗しました: {e}"[:200]}
    if not isinstance(data, dict):
        return {"enabled": True, "error": "AI応答が想定外の形式でした"}
    data = _postfill_doc_fields(data, profile)
    # 様式らしき実在ファイルのリンクを添付（AIの推測ではなくコードで収集したもの）
    data["form_links"] = [l for l in links if l.get("kind") == "form"][:8]
    data["enabled"] = True
    data["model"] = _model()
    data["source"] = "pdf_full" if notice_text else "description"
    return data
