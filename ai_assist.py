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


def _call_gemini(user_text: str) -> dict[str, Any]:
    """Gemini に構造化出力で問い合わせ、JSON dict を返す（依存はstdlibのみ）。"""
    key, model = _api_key(), _model()
    url = f"{_API_BASE}/{model}:generateContent?key={key}"
    body = {
        "systemInstruction": {"parts": [{"text": _SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SCHEMA,
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
