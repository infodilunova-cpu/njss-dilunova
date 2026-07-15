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
# 全文PDFを読ませる最大文字数（Geminiの入力。3〜7千字が普通なので余裕を持たせる）。
_PDF_MAX_CHARS = 14000


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
    "あなたは日本の公共入札（電気工事系）に精通した入札支援の専門家です。"
    "与えられた案件の公告本文・確定的に算出済みの必要書類・ユーザーの保有資格(マイ条件)を"
    "読み込み、この事業者がこの案件に『応募する一歩手前』まで到達できるよう具体的に支援します。"
    "一般論ではなく、この案件の実態に即して書くこと。"
    "とくに参加資格の『等級(ランク／格付け：A・B・C等)』を公告本文から読み取り、"
    "自社の経審等級と照合すること。案件の要求等級が自社等級より上位で応募できない"
    "（例: 要求A、自社C＝等級不足）と本文から明確に判断できる場合は、verdict を ✕ とし、"
    "reasons の先頭に『等級不足: 要求◯◯・自社◯◯』の形で具体的な根拠を必ず記載すること。"
    "等級が要件を満たす場合は 〇、本文に等級の記載が無い・判断材料が不足する場合は △ または不明とすること。"
    "なお verdict に関わらず、reasons の中に必ず1項目『等級: 要求◯◯／自社◯◯』を入れること"
    "（公告に等級の記載が無ければ『等級: 公告に記載なし』、自社等級が未設定なら『自社未設定』と書く）。"
    "参加資格適合の判定(verdict)は、等級不足のように本文から明確な場合を除き、確証が無ければ△または不明とし、断定しすぎないこと。"
    "必要書類は発注機関により異なるため、最終確認は公告に当たるよう注意書きを添えること。"
    "出力は必ず指定のJSONスキーマに従い、日本語で記述すること。"
)


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
    with urllib.request.urlopen(req, timeout=60) as res:
        data = json.loads(res.read().decode("utf-8"))
    cand = (data.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or [{}]
    text = parts[0].get("text", "{}")
    return json.loads(text)


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

    # タップ時に公告PDFの全文を取得してAIに読ませる（取れなければ説明文にフォールバック）。
    notice_text = _fetch_pdf_text(case.get("detail_url", ""))
    data = _call_gemini(_build_user_text(case, profile, requirements, notice_text))
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


def _build_plan_text(case: dict, profile: dict | None, req: dict | None,
                     price_guide: dict | None, notice_text: str = "",
                     today: str = "") -> str:
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
             price_guide: dict | None = None) -> dict[str, Any]:
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

    notice_text = _fetch_pdf_text(case.get("detail_url", ""))
    text = _build_plan_text(case, profile, requirements, price_guide, notice_text)
    try:
        data = _normalize_plan(
            _call_gemini(text, system=_PLAN_SYSTEM, schema=_PLAN_SCHEMA))
    except ValueError as e:  # JSONDecodeError 含む＝応答が壊れている
        return {"enabled": True, "error": f"AI応答の解析に失敗しました: {e}"[:200]}
    data["enabled"] = True
    data["model"] = _model()
    data["source"] = "pdf_full" if notice_text else "description"
    return data
