"""官公需情報ポータルサイト 検索API（中小企業庁・kkj.go.jp）クライアント。

国・地方公共団体・独立行政法人の入札公告を**全国横断で一元集約**した公式・無料API。
HTTP + XML だけ（Playwright不要）なので、デプロイ先でも動く＝最強のデータソース。

API: http://www.kkj.go.jp/api/  （GET, XML, 認証不要）
  Query          検索キーワード（必須。AND/OR等可）
  Category       1=物品 2=工事 3=役務
  LG_Code        都道府県コード(JIS X0401, 2桁, カンマ区切りで複数)
  Procedure_Type 1=一般競争 2=簡易公募型競争 3=簡易公募型指名
  Count          最大件数（既定10, 最大1000）
  CFT_Issue_Date 公告日（期間 YYYY-MM-DD/ 形式）
レスポンス: <Results><SearchResults><SearchResult>... 各案件。添付ファイル=設計図書(仕様書)。
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date as _date, timedelta as _timedelta

import db
from regions import region_of

# https を使う（http はネットワークによってはタイムアウト/遅延しやすく、Renderの
# ビルド時間（約15分上限）を圧迫してデプロイ失敗の原因になっていた）。
API_URL = "https://www.kkj.go.jp/api/"

# 都道府県名 → JIS X0401 コード（LG_Code 用）。関西を厚くする時に使う。
PREF_CODE = {
    "北海道": "01", "青森県": "02", "岩手県": "03", "宮城県": "04", "秋田県": "05",
    "山形県": "06", "福島県": "07", "茨城県": "08", "栃木県": "09", "群馬県": "10",
    "埼玉県": "11", "千葉県": "12", "東京都": "13", "神奈川県": "14", "新潟県": "15",
    "富山県": "16", "石川県": "17", "福井県": "18", "山梨県": "19", "長野県": "20",
    "岐阜県": "21", "静岡県": "22", "愛知県": "23", "三重県": "24", "滋賀県": "25",
    "京都府": "26", "大阪府": "27", "兵庫県": "28", "奈良県": "29", "和歌山県": "30",
    "鳥取県": "31", "島根県": "32", "岡山県": "33", "広島県": "34", "山口県": "35",
    "徳島県": "36", "香川県": "37", "愛媛県": "38", "高知県": "39", "福岡県": "40",
    "佐賀県": "41", "長崎県": "42", "熊本県": "43", "大分県": "44", "宮崎県": "45",
    "鹿児島県": "46", "沖縄県": "47",
}
KANSAI_CODES = ["25", "26", "27", "28", "29", "30"]  # 滋賀/京都/大阪/兵庫/奈良/和歌山

_PROC = {"1": "一般競争入札", "2": "簡易公募型競争入札", "3": "簡易公募型指名競争入札"}


def _text(el, tag: str) -> str:
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


# ============================================================
# 締切（deadline）抽出
# ============================================================
# 官公需APIの構造化タグ（OpeningTendersEvent 等）はほぼ空のため、
# ProjectName + ProjectDescription の自由記述から和暦の締切日を拾う。

# 全角→半角（数字のみ）変換テーブル
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")

# 締切キーワード。提出/締切系を優先し、開札系はフォールバック。
# 「公開終了日」は官公需上の掲載終了日＝事実上の応募締切相当なので最優先で拾う。
_DEADLINE_KEYWORDS_PRIMARY = (
    "公開終了日", "入札書提出期限", "提出期限", "申込期限", "申請期限",
    "受付期限", "締め切り", "締切",
)
# 期間系: 「YYYY〜YYYY」の終端（最後の日付）を採る。
_DEADLINE_KEYWORDS_PERIOD = ("入札受付期間", "参加申込", "受付期間", "申込")
_DEADLINE_KEYWORDS_FALLBACK = ("開札日時", "開札", "入札日時", "入札日", "期限")

# 本文から拾う締切の許容上限（公告日から何日後まで妥当とみなすか）。
# これを超える日付は工期末・履行期限等の誤抽出とみなして採らない。
_DEADLINE_MAX_DAYS_AFTER = 150

# 令和N年M月D日（年月日それぞれ全角/半角混在可）
_REIWA_RE = re.compile(r"令和\s*(\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 西暦YYYY年M月D日
_SEIREKI_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 年の無い M月D日（近傍に年がある場合のみ採用）
_MD_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
# 近傍に現れる年（令和 or 西暦）。M月D日 の年補完に使う。
_YEAR_NEAR_RE = re.compile(r"令和\s*(\d{1,2})\s*年|(20\d{2})\s*年")


def _iso_or_empty(year: int, month: int, day: int) -> str:
    """年月日が妥当なら ISO 文字列、不正なら ""。"""
    if not (1 <= month <= 12 and 1 <= day <= 31 and 2000 <= year <= 2099):
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def _date_near_keyword(text: str, keyword_pos: int, window: int = 40) -> str:
    """キーワード位置の直後ウィンドウ内から最初の日付を ISO で返す。

    令和 → 西暦 → （年が近傍にあれば）M月D日 の順で探す。
    """
    seg = text[keyword_pos:keyword_pos + window]
    m = _REIWA_RE.search(seg)
    if m:
        return _iso_or_empty(2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _SEIREKI_RE.search(seg)
    if m:
        return _iso_or_empty(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    # 年無し M月D日：キーワード前後の近傍に年があれば補完、無ければ諦める
    m = _MD_RE.search(seg)
    if m:
        ctx = text[max(0, keyword_pos - 30):keyword_pos + window]
        ym = _YEAR_NEAR_RE.search(ctx)
        if ym:
            year = 2018 + int(ym.group(1)) if ym.group(1) else int(ym.group(2))
            return _iso_or_empty(year, int(m.group(1)), int(m.group(2)))
    return ""


def _last_date_near_keyword(text: str, keyword_pos: int, window: int = 60) -> str:
    """期間表記「開始日〜終了日」想定で、ウィンドウ内の最後の妥当日付を採る。

    受付期間など範囲で書かれる項目の「終端＝締切」を拾うため。
    """
    seg = text[keyword_pos:keyword_pos + window]
    best = ""
    for m in _REIWA_RE.finditer(seg):
        iso = _iso_or_empty(2018 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            best = iso
    if best:
        return best
    for m in _SEIREKI_RE.finditer(seg):
        iso = _iso_or_empty(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if iso:
            best = iso
    return best


def parse_deadline_from_text(text: str, announced_date: str = "") -> str:
    """ProjectName+ProjectDescription から締切日（ISO: YYYY-MM-DD）を抽出する。

    優先順位: 提出/締切/公開終了系 → 受付期間（範囲の終端）→ 開札系。
    年が特定できない場合（裸の M月D日 など）は推測せず "" を返す。
    複数候補があってもキーワードに紐づく妥当日付のみ採用する（推測しない）。

    announced_date が与えられた場合、それより前の日付は締切ではない（過去案件の
    参照日や回答掲載日等の誤抽出）とみなして棄却する＝誤った締切より空を返す。
    """
    if not text:
        return ""
    norm = text.translate(_ZEN2HAN)
    ann = announced_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", announced_date or "") else ""
    ann_d = None
    if ann:
        try:
            ann_d = _date.fromisoformat(ann)
        except ValueError:
            ann_d = None

    def _ok(iso: str) -> bool:
        if not iso:
            return False
        if ann_d is None:
            return True
        try:
            d = _date.fromisoformat(iso)
        except ValueError:
            return False
        # 公告日より後 かつ 現実的な範囲内のみ採用。工期末・履行期限・回答掲載日
        # などの誤抽出を弾く（誤った締切は閉じた案件を「応募可」に出すため空より有害）。
        return ann_d < d <= ann_d + _timedelta(days=_DEADLINE_MAX_DAYS_AFTER)

    def _scan(keywords, finder, window):
        """各キーワードの『全出現箇所』を順に試す。

        従来は最初の1箇所しか見ず、見出しの「提出期限」等に当たって近傍に日付が
        無いと諦めていた（取りこぼしの主因）。全出現を試して取得率を上げる。
        """
        for kw in keywords:
            start = 0
            while True:
                pos = norm.find(kw, start)
                if pos < 0:
                    break
                iso = finder(norm, pos + len(kw), window)
                if _ok(iso):
                    return iso
                start = pos + len(kw)
        return ""

    # 1) 提出/締切/公開終了系 → 2) 受付期間(終端) → 3) 開札系（この優先順は維持）
    return (_scan(_DEADLINE_KEYWORDS_PRIMARY, _date_near_keyword, 50)
            or _scan(_DEADLINE_KEYWORDS_PERIOD, _last_date_near_keyword, 60)
            or _scan(_DEADLINE_KEYWORDS_FALLBACK, _date_near_keyword, 50))


def _valid_iso(s: str) -> str:
    """構造化タグの値が YYYY-MM-DD 形式なら返す、でなければ ""。"""
    s = (s or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s
    return ""


# ============================================================
# 予定価格（budget）抽出
# ============================================================
# 官公需APIに金額フィールドは無いが、ProjectDescription 本文に「予定価格
# 18,100,000円」等が書かれている案件がある（特に市発注は事前公表が多い）。
# 拾えた分だけ数値化して金額フィルタ・整列に使う。拾えなければ 0（非公表扱い）。
_BUDGET_RE = re.compile(
    r"(予定価格|予定金額|設計金額|契約金額|委託料|参考価格|予算額|予算)"
    r"[^0-9]{0,12}([0-9,]{4,})\s*円"
)


def parse_budget_from_text(text: str) -> tuple[int, str]:
    """本文から予定価格を抽出して (円・整数, 表示用テキスト) を返す。

    複数候補があれば最大額を採る（基本額より総額を優先）。
    妥当域（10万〜1000億円）外は採らない。拾えなければ (0, "")。
    """
    if not text:
        return (0, "")
    norm = text.translate(_ZEN2HAN).replace("，", ",")
    best = 0
    for m in _BUDGET_RE.finditer(norm):
        try:
            v = int(m.group(2).replace(",", ""))
        except ValueError:
            continue
        if 100_000 <= v <= 100_000_000_000:
            best = max(best, v)
    if not best:
        return (0, "")
    return (best, f"{best:,}円")


# 官公需 Category コード → 調達区分名
_PROC_TYPE = {"1": "物品", "2": "工事", "3": "役務"}


# ============================================================
# 業種分類（category）
# ============================================================
# 官公需APIには業種が無く Category=工事/物品/役務 のみ。従来は全件 "電気工事"
# を固定していたが実際は空調/舗装/トイレ改修等の非電気案件が約4割混ざる。
# ProjectName+ProjectDescription のキーワードで実態に近い業種へ分類する。
#
# 互換性方針（重要）: db.match_cases() は profile の categories（既定 '電気工事'）を
# カンマ分割し各語で `category LIKE '%語%'` する。既定では `LIKE '%電気工事%'` に
# なるため、電気系カテゴリ名には必ず部分文字列 "電気工事" を内包させる
# （"電気工事-受変電" 等）。こうすれば profile も match_cases も無改修で、
# 新カテゴリの電気案件が既定フィルタにそのまま乗る。
# 非電気（空調/その他）は "電気工事" を含めないので LIKE フィルタから自然に外れる。
# 表示上のサブ業種は "電気工事-◯◯" のサフィックスで区別する。

# (出力カテゴリ名, キーワード) を優先順位順に評価。
# 役務は電気・管以外（塗装/防水/清掃 等）も拾いたい要望のため、全業種を分類する。
# 電気系カテゴリは "電気工事" を内包させ、profile 既定(LIKE '%電気工事%')と互換を保つ。
# 評価はタイトル優先（説明文の付帯記述による誤検出を防ぐ＝精度優先）。専門工事の種別を
# 上に置き、電気はその後（例「外壁塗装(電気設備含む)」は塗装と判定）。
# Web/IT・制作・広報を中心に、関連する役務(サービス)を幅広く分類する。
# タイトル優先で評価。ホームページ制作系を最上位に置く。
_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("ホームページ制作", ("ホームページ", "ホームページ制作", "ホームページ作成", "ＨＰ制作",
                          "ウェブサイト", "Ｗｅｂサイト", "Webサイト", "webサイト",
                          "サイト制作", "サイト構築", "サイトリニューアル", "ウェブサイト構築",
                          "公式サイト", "ポータルサイト", "Webページ", "ウェブページ")),
    ("Web制作・運用", ("Web制作", "ＷＥＢ制作", "ウェブ制作", "Web構築", "Web開発",
                       "ランディングページ", "ＬＰ", "Web運用", "サイト運用", "Web改修",
                       "ウェブ", "ホームページ運用", "ＣＭＳ", "CMS")),
    ("システム・アプリ開発", ("システム開発", "システム構築", "システム改修", "システム更改",
                              "アプリ開発", "アプリケーション", "ソフトウェア", "ソフトウエア",
                              "プログラム開発", "業務システム", "情報システム", "電子申請",
                              "予約システム", "管理システム", "DX", "ＤＸ")),
    ("ネットワーク・インフラ", ("ネットワーク", "サーバ", "サーバー", "クラウド", "データセンター",
                                "仮想化", "情報基盤", "通信環境", "Wi-Fi", "無線LAN", "ＬＡＮ")),
    ("デザイン・印刷", ("デザイン", "印刷", "パンフレット", "チラシ", "ポスター", "リーフレット",
                        "冊子", "製本", "ロゴ", "ポスター", "封筒", "名刺", "グラフィック")),
    ("動画・映像・写真", ("動画", "映像", "撮影", "ビデオ", "プロモーション映像", "ＰＶ",
                          "写真", "フォト", "VR", "ドローン")),
    ("広報・PR・広告", ("広報", "ＰＲ", "PR", "広告", "プロモーション", "シティプロモーション",
                        "情報発信", "ブランディング", "メディア")),
    ("SNS・マーケティング", ("SNS", "ＳＮＳ", "マーケティング", "デジタルマーケ", "運用支援",
                            "アクセス解析", "ＳＥＯ", "SEO", "Web広告", "リスティング")),
    ("コンテンツ・編集・翻訳", ("ライティング", "記事", "コンテンツ", "編集", "翻訳", "原稿",
                               "テキスト", "校正", "電子書籍")),
    ("データ・電子化・調査", ("データ入力", "デジタル化", "電子化", "データ整備", "アンケート",
                             "調査", "集計", "スキャニング", "OCR", "ＯＣＲ")),
    ("イベント・企画", ("イベント", "企画運営", "セミナー", "ワークショップ", "展示")),
    ("保守・運用・委託", ("保守", "運用", "業務委託", "委託", "管理業務", "サポート",
                         "ヘルプデスク", "コールセンター", "運営")),
]


_ALL_RULES_CACHE: tuple | None = None


def _all_category_rules() -> tuple:
    """全業種の分類ルールを (タイトル用, 説明文用) で返す（結果はキャッシュ）。

    タイトル用＝denki(工事全トレード＋電気)＋web(IT)の合体。
    説明文用＝denkiのみ。web(IT)ルールは title_only 設計（説明文には
    「詳細は市ホームページを参照」等の定型文が頻出し、無関係の工事案件まで
    ホームページ制作等に誤分類するため、説明文スキャンには使わない）。
    """
    global _ALL_RULES_CACHE
    if _ALL_RULES_CACHE is None:
        try:
            import verticals as _v
            d = list(_v.get("denki").get("category_rules") or [])
            w = list(_v.get("web").get("category_rules") or [])
            _ALL_RULES_CACHE = (d + w, d)
        except Exception:  # noqa: BLE001
            _ALL_RULES_CACHE = (list(_CATEGORY_RULES), list(_CATEGORY_RULES))
    return _ALL_RULES_CACHE


def classify_category(text: str, title: str = "", vertical: str | None = None) -> str:
    """案件名(+説明)から業種を判定する（業種テンプレの分類ルールを使用・タイトル優先）。

    vertical を渡すとその業種テンプレ(verticals.py)の category_rules で分類する。
    未指定ならこのモジュールの _CATEGORY_RULES（既定）。
    """
    if not text and not title:
        return "その他"
    rules = _CATEGORY_RULES
    desc_rules = None  # 説明文パス専用ルール（None なら rules と同じ）
    title_only = False
    if vertical == "all":
        # 全業種統合：電気(denki)＝建築/電気/清掃/警備/土木ほか全トレード＋web(IT)の
        # 分類ルールを合体して使う。denki を先にして工事系のタイ勝ちを優先。
        # 説明文パスは denki ルールのみ（web は title_only 設計）。
        rules, desc_rules = _all_category_rules()
    elif vertical:
        try:
            import verticals as _v
            cfg = _v.get(vertical)
            rules = cfg.get("category_rules") or _CATEGORY_RULES
            title_only = bool(cfg.get("title_only"))
        except Exception:  # noqa: BLE001
            pass
    t = title or text.split("\n", 1)[0]
    for name, keywords in rules:        # 1) タイトル優先（最も信頼できる）
        if any(k in t for k in keywords):
            return name
    if title_only:                       # 説明文ノイズが多い業種はタイトルのみで判定
        return "その他"
    for name, keywords in (desc_rules if desc_rules is not None else rules):
        if any(k in text for k in keywords):  # 2) 中立なら説明文も見る
            return name
    return "その他"


def is_electrical(category: str) -> bool:
    """互換用（旧称）。Web版では常に False でよい。"""
    return False


def fetch(query: str = "電気工事", category: str = "2",
          lg_codes: list[str] | None = None, count: int = 1000,
          timeout: int = 40, vertical: str | None = None,
          issue_date: str | None = None) -> list[dict]:
    """官公需APIを叩いて案件の生 dict リストを返す。vertical で業種分類＆タグ付け。

    issue_date: 公告日の期間指定（API の CFT_Issue_Date、"YYYY-MM-DD/" =以降、
    "/YYYY-MM-DD" =以前、"開始/終了" =範囲）。ヒットが1000件超のクエリでは
    未指定だと古い案件が枠を食い潰し新しい案件を取りこぼすため、網羅取得では必ず指定する。
    """
    params = {"Query": query, "Category": category, "Count": str(count)}
    if lg_codes:
        params["LG_Code"] = ",".join(lg_codes)
    if issue_date:
        params["CFT_Issue_Date"] = issue_date
    url = API_URL + "?" + urllib.parse.urlencode(params, encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        raw = res.read().decode("utf-8")
    root = ET.fromstring(raw)
    err = root.find("Error")
    if err is not None:
        raise RuntimeError(f"官公需APIエラー: {err.text}")

    out: list[dict] = []
    for sr in root.iter("SearchResult"):
        pref = _text(sr, "PrefectureName")
        # 添付ファイル（設計図書＝仕様書）
        att_uri = ""
        att = sr.find("Attachments")
        if att is not None:
            a = att.find("Attachment")
            if a is not None:
                att_uri = _text(a, "Uri")
        org = _text(sr, "OrganizationName")
        title = _text(sr, "ProjectName")
        desc = _text(sr, "ProjectDescription")
        text = f"{title}\n{desc}"
        announced = _text(sr, "CftIssueDate")[:10]
        # 締切: 構造化タグに妥当な ISO 日付があれば優先、無ければ自由記述から抽出
        deadline = (_valid_iso(_text(sr, "OpeningTendersEvent"))
                    or _valid_iso(_text(sr, "TenderSubmissionDeadline"))
                    or _valid_iso(_text(sr, "PeriodEndTime"))
                    or parse_deadline_from_text(text, announced))
        budget_yen, budget_txt = parse_budget_from_text(text)
        out.append({
            "source": "官公需API",
            "external_id": f"KKJ-{_text(sr, 'Key') or _text(sr, 'ResultId')}",
            "title": title,
            "agency": org,
            "agency_type": ("国の機関" if any(k in org for k in ("省", "庁", "局", "国立", "機構"))
                            else "地方公共団体"),
            "region": region_of(pref) or "",
            "prefecture": pref,
            "category": classify_category(desc, title=title, vertical=vertical),
            # 業種列は denki/web のみ（"all" 等の分類用の値はそのまま格納しない）。
            # 統合ブラウズは vertical で絞らないので既定 denki で問題ない。
            "vertical": vertical if vertical in ("denki", "web") else "denki",
            "procurement_type": _PROC_TYPE.get(category, ""),
            "bid_method": _PROC.get(_text(sr, "ProcedureType"), _text(sr, "ProcedureType")),
            "announced_date": announced,
            "deadline": deadline,
            "detail_url": _text(sr, "ExternalDocumentURI"),
            "spec_status": db.SPEC_AVAILABLE if att_uri else db.SPEC_UNKNOWN,
            "spec_reason": "",
            "spec_url": att_uri,
            "budget": budget_txt,
            "budget_yen": budget_yen,
            "winner": "",
            "win_price": "",
            "description": desc[:2000],
        })
    return out


def load(query: str = "電気工事", lg_codes: list[str] | None = None) -> int:
    """官公需APIから取得して DB に投入。件数を返す。"""
    db.init_db()
    rows = fetch(query=query, lg_codes=lg_codes)
    rows = [r for r in rows if r["title"]]
    return db.upsert_cases(rows) if rows else 0


# 工事(Cat2)の検索語＝電気工事業者の本業スコープ（受変電・LED・照明 等）。
ELEC_QUERIES = (
    "電気工事", "電気設備", "受変電", "受電", "照明", "LED", "非常用発電",
    "太陽光", "電灯", "動力", "キュービクル", "電気主任技術者",
)

# 役務(Cat3)の検索語＝電気・管以外も拾いたい要望（塗装・防水 等）に対応して広く取る。
# 川野談「役務なので電気と管以外も拾いたい。例えば塗装・防水とか」。
# ※この広い役務クエリは「関西だけ」に適用する。全国へ広げると塗装/清掃/警備の
#   非電気役務が大量に混ざるため、全国は下の ELEC_SERVICE_QUERIES（電気役務のみ）を使う。
SERVICE_QUERIES = (
    "電気", "電気設備保守", "自家用電気工作物保安管理", "受変電", "照明",
    "管", "給排水", "空調", "塗装", "防水", "清掃", "点検", "保守", "保安",
    "設備管理", "維持管理", "運転管理", "警備", "業務委託", "委託",
)

# 全国向けの役務(Cat3)クエリ＝電気工事業者の本業に直結する役務のみ（保安管理・電気保守等）。
# 全国に広い役務を流すと非電気が氾濫するため、電気スコープに限定して取りこぼしだけ拾う。
ELEC_SERVICE_QUERIES = (
    "電気設備保守", "自家用電気工作物保安管理", "電気主任技術者", "受変電",
    "電気設備点検", "電気保安", "非常用発電設備保守", "照明設備保守",
)


def _fetch_retry(query: str, category: str,
                 lg_codes: list[str] | None = None,
                 retries: int = 2, vertical: str | None = None,
                 issue_date: str | None = None) -> list[dict]:
    """fetch() の薄いリトライ版。タイムアウト/DNS瞬断を retries 回まで再試行。

    1クエリの一過性失敗で取りこぼさないための保険。最終的に失敗したら [] を返す。
    """
    import time
    for attempt in range(retries + 1):
        try:
            return fetch(query=query, category=category, lg_codes=lg_codes,
                         vertical=vertical, issue_date=issue_date)
        except Exception:  # noqa: BLE001 — 一過性のネットワーク失敗を再試行
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
    return []


def _fetch_many(specs: list[tuple[str, str, list[str] | None]],
                max_workers: int = 8, vertical: str | None = None,
                checkpoint: str | None = None,
                issue_date: str | None = None) -> list[dict]:
    """(query, category, lg_codes) のリストを並列取得し external_id で一意化して返す。

    各クエリは独立かつ I/O 待ちが大半なので、スレッドプールで同時実行する。
    逐次だと全国20クエリ×数十秒＝10分超でRenderのビルド時間を超過しデプロイ失敗していた。
    並列化で数分に短縮する。失敗クエリは _fetch_retry が [] を返すので全体は止まらない。

    checkpoint にファイルパスを渡すと、完了したクエリの結果を JSONL で逐次保存し、
    中断後の再実行では保存済みクエリをスキップして続きから再開する（数千クエリ×
    数時間の網羅取得がプロセス強制終了で丸ごと無駄になるのを防ぐ）。
    ※失敗して [] になったクエリも「完了」と記録される（再開時に再試行しない）。
    """
    import json
    import os
    from concurrent.futures import ThreadPoolExecutor

    def _key(s: tuple) -> str:
        return f"{s[0]}|{s[1]}|{','.join(s[2] or [])}"

    seen: dict[str, dict] = {}
    done_keys: set[str] = set()
    ck_file = None
    if checkpoint:
        if os.path.exists(checkpoint):
            with open(checkpoint, encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except Exception:  # noqa: BLE001 — 途中killで欠けた最終行は捨てる
                        continue
                    done_keys.add(rec["k"])
                    for r in rec["rows"]:
                        if r.get("title") and r["external_id"] not in seen:
                            seen[r["external_id"]] = r
            print(f"  [checkpoint] 既取得 {len(done_keys)} クエリを再利用"
                  f"（累計 {len(seen)} 件から再開）", flush=True)
        ck_file = open(checkpoint, "a", encoding="utf-8")

    todo = [s for s in specs if _key(s) not in done_keys]
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = ex.map(
                lambda s: (s, _fetch_retry(query=s[0], category=s[1], lg_codes=s[2],
                                           vertical=vertical, issue_date=issue_date)),
                todo)
            for spec, rows in results:
                done += 1
                if done % 200 == 0:  # 長時間の網羅取得でも進捗が見えるように
                    print(f"  …{done}/{len(todo)} クエリ完了・累計 {len(seen)} 件", flush=True)
                if ck_file:
                    ck_file.write(json.dumps({"k": _key(spec), "rows": rows},
                                             ensure_ascii=False) + "\n")
                    if done % 20 == 0:
                        ck_file.flush()
                for r in rows:
                    if r.get("title") and r["external_id"] not in seen:
                        seen[r["external_id"]] = r
    finally:
        if ck_file:
            ck_file.close()
    return list(seen.values())


def fetch_for_vertical(vertical: str, per_pref: bool = False) -> list[dict]:
    """業種テンプレ(verticals.py)のクエリで全国取得し、vertical を付与して返す。

    per_pref=False（既定）: 全国一括（各クエリ最大1000件）＝速い。ローカル/デモ向け。
    per_pref=True: 47都道府県分割＝上限突破で網羅（重い。Actions向け）。
    """
    import verticals as _v
    q = _v.get(vertical).get("queries", {})
    specs: list[tuple[str, str, list[str] | None]] = []
    pref_sets = ([[c] for c in PREF_CODE.values()] if per_pref else [None])
    # 都道府県分割時は分割専用の厳選クエリ(cat3_pref)があればそれを使う（呼び出し回数を抑制）。
    # 無ければ全国用(cat3_nation)にフォールバック。全国一括(per_pref=False)は常にcat3_nation。
    cat3 = q.get("cat3_pref") if (per_pref and q.get("cat3_pref")) else q.get("cat3_nation", ())
    for codes in pref_sets:
        specs += [(query, "2", codes) for query in q.get("cat2", ())]
        specs += [(query, "3", codes) for query in cat3]
        specs += [(query, "1", codes) for query in q.get("cat1", ())]
    # 関西特別枠（cat3_kansai）があれば関西だけ追加
    for query in q.get("cat3_kansai", ()):
        specs.append((query, "3", KANSAI_CODES))
    return _fetch_many(specs, max_workers=10, vertical=vertical)


def load_for_vertical(vertical: str, per_pref: bool = False) -> int:
    """業種テンプレのデータを取得してDBに投入。件数を返す。

    全文検索クエリは無関係案件も拾うため、その業種のカテゴリに当たらない
    （classify結果が "その他"）案件は捨ててノイズを除く。
    """
    db.init_db()
    rows = [r for r in fetch_for_vertical(vertical, per_pref=per_pref)
            if r["title"] and r.get("category") != "その他"]
    return db.upsert_cases(rows) if rows else 0


def fetch_nationwide_electrical() -> list[dict]:
    """全国の電気案件を複数クエリ横断で取得：工事(Cat2)=電気スコープ全語＋役務(Cat3)=電気役務。

    従来は単一クエリ "電気工事"・Count=1000 の1回だけで全国を取っていたため、
    1000件で頭打ち＆受変電/照明/太陽光等を全国で取りこぼしていた（近畿偏りの主因）。
    関西と同じ要領で電気系クエリを横断し、external_id で一意化して取りこぼしを無くす。
    ※役務は非電気の氾濫を避けるため電気役務(ELEC_SERVICE_QUERIES)に限定する。
    """
    specs = ([(q, "2", None) for q in ELEC_QUERIES]
             + [(q, "3", None) for q in ELEC_SERVICE_QUERIES])
    return _fetch_many(specs)


def fetch_kansai_targets(lg_codes: list[str] | None = None) -> list[dict]:
    """関西の対象案件を取得：工事(Cat2)=電気スコープ／役務(Cat3)=広範(塗装防水等含む)。

    1案件は external_id（KKJ-Key）で一意化。役務は業種を広げて取りこぼしを無くす。
    """
    codes = lg_codes or KANSAI_CODES
    specs = ([(q, "2", codes) for q in ELEC_QUERIES]
             + [(q, "3", codes) for q in SERVICE_QUERIES])
    return _fetch_many(specs)


def fetch_comprehensive() -> list[dict]:
    """【網羅モード】全国を都道府県ごとに分割して取得し、1000件/クエリの上限を突破する。

    官公需APIは1クエリ最大1000件・ページング無し。全国一括だと電気工事/照明/受変電など
    13クエリが1000で頭打ちになり大量に取りこぼしていた（電気工事だけで全国一括1000→
    都道府県分割9,436件）。そこで全47都道府県 × 電気クエリで分割取得する。
      - 工事(Cat2): ELEC_QUERIES を全都道府県で
      - 役務(Cat3): 全国は電気役務(ELEC_SERVICE_QUERIES)を全都道府県で
      - 関西(KANSAI_CODES)のみ 役務を広範(SERVICE_QUERIES=塗装/防水等)でも取る（本業要望）
    呼び出し回数が多い（約1000回）ため Render のビルドではなく GitHub Actions 側で実行する。
    external_id で一意化。
    """
    all_codes = list(PREF_CODE.values())
    specs: list[tuple[str, str, list[str] | None]] = []
    for code in all_codes:
        specs += [(q, "2", [code]) for q in ELEC_QUERIES]
        specs += [(q, "3", [code]) for q in ELEC_SERVICE_QUERIES]
    # 関西は広範役務も（塗装・防水・清掃等の役務も拾いたいという本業要望に対応）
    for code in KANSAI_CODES:
        specs += [(q, "3", [code]) for q in SERVICE_QUERIES]
    return _fetch_many(specs, max_workers=10)


# ---- 全業種（統合版）向けの広範クエリ ------------------------------------
# DiluNova は業種で分けず全案件を統合表示する方針のため、電気/webに限らず
# 工事(Cat2)・役務(Cat3)・物品(Cat1)を全業種で広く取る。分類は classify_category
# (vertical=None=_CATEGORY_RULES)が全業種の category へ振り分ける。
ALL_CONSTRUCTION_QUERIES = (  # Cat2 工事：全トレード
    "建築", "土木", "電気工事", "管工事", "舗装", "塗装", "防水", "解体",
    "造園", "設備", "改修", "補修", "機械", "通信", "消防", "受変電",
    "照明", "太陽光", "空調", "給排水",
)
ALL_SERVICE_QUERIES = (  # Cat3 役務：全サービス（IT/web含む）
    "業務委託", "保守", "点検", "清掃", "警備", "運搬", "設計", "調査",
    "印刷", "運営", "管理", "保安", "システム", "開発", "ホームページ",
    "賃貸借", "廃棄物", "給食", "検査", "研修", "翻訳", "映像", "広報", "運送",
)
ALL_GOODS_QUERIES = (  # Cat1 物品
    "購入", "物品", "備品", "機器", "車両", "消耗品", "図書", "医療",
    "食料", "燃料",
)
# IT・Web系の役務(Cat3)クエリ＝ホームページ制作/ソフトウェア開発/インフラ/デジタル系を
# 全部引っ張るための専用語。ALL_SERVICE_QUERIES の「システム/開発/ホームページ」だけでは
# タイトルにその語を含まない案件（例:「公式ウェブサイト構築」「CMS更新」「RPA導入支援」）を
# 丸ごと取りこぼすため、部分一致で他語に含まれない語を1語ずつ押さえる。
ALL_IT_QUERIES = (  # Cat3 役務
    # Web・ホームページ（「サイト」が ○○サイト構築/制作/リニューアルを一括で拾う）
    "ウェブ", "Web", "サイト", "CMS", "ランディングページ",
    # 開発・ソフトウェア（「システム/開発」で拾えない単独語）
    "アプリ", "ソフトウェア", "プログラム",
    # デジタル・DX トレンド
    "デジタル", "DX", "ICT", "オンライン", "電子", "マイナンバー", "スマートシティ",
    # AI・データ活用
    "AI", "RPA", "チャットボット", "GIS", "データ", "OCR",
    # インフラ・セキュリティ
    "クラウド", "サーバ", "ネットワーク", "セキュリティ", "LAN", "GIGA",
    # 運用・サポート
    "ヘルプデスク", "コールセンター",
    # クリエイティブ・マーケ（Web周辺。印刷/映像/広報は ALL_SERVICE_QUERIES 側にあり）
    "動画", "デザイン", "コンテンツ", "SNS",
)
ALL_IT_GOODS_QUERIES = (  # Cat1 物品：IT機器・ソフトウェア調達
    "パソコン", "タブレット", "サーバ", "ソフトウェア", "ライセンス", "端末",
)


def fetch_all_industries() -> list[dict]:
    """【全業種 網羅モード】全47都道府県 × 工事/役務/物品の広範クエリで取得する。

    業種で分けず全案件を1つのDBに集約する統合方針のためのソース。電気・IT も
    このクエリ群に含まれる（電気工事/受変電/照明/システム/ホームページ 等）。
    vertical=None で classify_category が _CATEGORY_RULES により全業種へ分類する。
    IT・Web系（ホームページ制作/ソフトウェア開発/インフラ/AI等）は ALL_IT_QUERIES で
    専用語を都道府県分割にかけ、汎用語では拾えない案件も全部引っ張る。

    【重要】公告日フィルタ（CFT_Issue_Date=直近180日）をAPI側にかける。
    未指定だと全期間（2021年〜）がヒットし、1000件/クエリの枠を古い終了案件が
    食い潰して新しい案件を大量に取りこぼす（例:「ホームページ」は全期間35,604件
    ヒットで直近90日2,556件がほぼ枠外だった）。180日にするのは、公告から締切まで
    数ヶ月ある長期案件（今も申し込める）を拾うため。
    呼び出し数が多い(約4,300)ので GitHub Actions もしくはローカルで実行する。
    """
    from datetime import date, timedelta
    all_codes = list(PREF_CODE.values())
    specs: list[tuple[str, str, list[str] | None]] = []
    for code in all_codes:
        specs += [(q, "2", [code]) for q in ALL_CONSTRUCTION_QUERIES]
        specs += [(q, "3", [code]) for q in ALL_SERVICE_QUERIES]
        specs += [(q, "3", [code]) for q in ALL_IT_QUERIES]
        specs += [(q, "1", [code]) for q in ALL_GOODS_QUERIES]
        specs += [(q, "1", [code]) for q in ALL_IT_GOODS_QUERIES]
    since = (date.today() - timedelta(days=365)).isoformat() + "/"
    # vertical="all" → denki+web 合体ルールで全業種分類。
    # checkpoint: 数時間かかるため、中断されても続きから再開できるよう逐次保存する。
    # 完走したら消す（翌日以降の実行に古いデータを持ち越さない）。
    import os
    ck = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".kkj_checkpoint.jsonl")
    rows = _fetch_many(specs, max_workers=14, vertical="all", checkpoint=ck,
                       issue_date=since)
    try:
        os.remove(ck)
    except OSError:
        pass
    # DB肥大＆陳腐化の防止：「締切が今日以降（＝申し込める）」または
    # 「公告が直近365日以内（＝取得ウィンドウと同じ）」の案件だけ残す。
    # 直近1年の実績はホームページ制作等の希少カテゴリで発注機関リサーチの
    # 材料になるため落とさない（新着HP案件はKKJ上そもそも少なく、短期で切ると
    # 港区議会HPリニューアル級の事例まで消えてカテゴリがほぼ空になる）。
    today = date.today().isoformat()
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    return [r for r in rows
            if (r.get("deadline") or "") >= today
            or (r.get("announced_date") or "") >= cutoff]


# 後方互換エイリアス（update.py 等の既存呼び出し用）
fetch_kansai_electrical = fetch_kansai_targets


def load_kansai_electrical() -> int:
    """関西の工事(電気)＋役務(広範)をまとめて DB 投入。件数を返す。"""
    db.init_db()
    rows = fetch_kansai_targets()
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    import sys
    if "--kansai-elec" in sys.argv:
        print(f"官公需API(関西・電気 工事+役務): {load_kansai_electrical()} 件")
    elif "--kansai" in sys.argv:
        print(f"官公需API(関西): {load(lg_codes=KANSAI_CODES)} 件")
    else:
        print(f"官公需API(全国): {load()} 件")
