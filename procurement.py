"""応募導線（どこで申し込むか）を「AIなし・完全コード」で決定するモジュール。

設計方針（重要）:
  クライアント提供スプレッドシートの "NJSS公示リンク先"（= agencies.bid_url）は
  「過去の公告の一例」であり、申し込み先ページではない。和歌山県庁が競輪サイト
  (keirinwa.com) になっている等、データ自体に誤りも混ざる。よって bid_url は
  応募導線に使わない。

  代わりに、次の優先順で「確実に正しい導線」だけを出す。
    1) 各案件が持つ実リンク（官公需APIの detail_url / spec_url, 全体の約92%）
       = その案件そのものの公告。応募資格・提出書類・締切・問い合わせが載る本命。
    2) 発注機関ドメインから判定できた「大手・安定の電子入札システム」だけ、
       手当てした正規ポータルURLを提示（KNOWN_PORTALS）。
       判定できないドメイン（誤データ含む）は何も出さず 3) に委ねる。
    3) 案件名・機関名・都道府県での "検索リンク"（常に最新・保守不要）。

  すべて純粋関数。実行時にAI・有料APIを一切使わない。データの鮮度は既存の
  日次更新（update.py --fast）が担う。本モジュールの定数は安定した公的ポータルのみ。
"""

from __future__ import annotations

import re
import urllib.parse

# 個別案件ではなく「検索トップ」等に着地してしまう汎用URL。
# 官公需APIの ExternalDocumentURI が空のとき等に返るランディングページ群。
GENERIC_URL_MARKERS = (
    "p-portal.go.jp/pps-web-biz/UAA01/OAA0101",   # 調達ポータル 検索トップ
    "i-ppi.jp/IPPI/SearchServices/Web/Index.htm",  # PPI 検索トップ
    "PiCtBaFi02start.vm",                          # efftis 入口
    "pubGroupTop.do",                              # efftis 入口
    "PPUBC00100",                                  # efftis/PPUBC 入口
    "EjPPIj",                                      # e-Aichi/supercals 入口
)


def is_generic_url(url: str) -> bool:
    """個別案件ではなく検索トップ等に着地する汎用URLか。"""
    return bool(url) and any(g in url for g in GENERIC_URL_MARKERS)


def is_real_link(url: str) -> bool:
    """実在の個別ページとして使えるURLか（http かつ 汎用/ダミーでない）。"""
    return (bool(url) and url.startswith("http")
            and "example.com" not in url and not is_generic_url(url))


def classify_platform(domain: str) -> str:
    """発注機関ドメインから使用する電子入札システム名を判定する。

    判定できない（＝誤データ・個別サイト）ものは "個別/不明" を返し、
    呼び出し側はポータルURLを出さない（誤誘導しないため）。
    """
    d = (domain or "").lower()
    rules = [
        ("i-ppi.jp", "統合PPI"),
        ("efftis.jp", "efftis"),
        ("e-procurement.metro.tokyo", "東京都電子調達"),
        ("e-tokyo.lg.jp", "東京電子自治体共同運営"),
        ("e-aichi", "e-Aichi"),
        ("e-harp", "e-harp"),
        ("p-portal.go.jp", "GEPS"),
        ("e-bisc.go.jp", "e-bisc"),
        ("bit.courts.go.jp", "裁判所BIT"),
        ("kumamoto-idc", "PPI_P"),
        ("dennyu.pref.kagawa", "PPI_P"),
        ("keiyaku.city.hiroshima", "PPI_P"),
        ("cals", "電子入札コアシステム"),
    ]
    for key, name in rules:
        if key in d:
            return name
    return "個別/不明"


# 大手・安定の電子入札システムだけ、正規の入口URLを手当て。
# ここに載るものは「申し込み先」として確実なものに限る。per-instance（機関ごとに
# URLが違う）系は載せず、検索導線に委ねる（誤URLを出さない方針）。
KNOWN_PORTALS: dict[str, tuple[str, str, str]] = {
    # key: (システム表示名, 入口URL, 補足)
    "GEPS": ("政府電子調達システム（GEPS）", "https://www.p-portal.go.jp/",
             "国の機関。事前に「全省庁統一資格」の取得が必要です。"),
    "東京都電子調達": ("東京都電子調達システム", "https://www.e-procurement.metro.tokyo.lg.jp/",
                  "東京都の競争入札参加資格の登録が必要です。"),
    "e-Aichi": ("あいち電子調達システム（CALS/EC）", "https://www.chotatsu.e-aichi.jp/",
                "愛知県・県内市町村の入札参加資格登録が必要です。"),
    "e-harp": ("北海道電子入札システム e-harp", "https://www.e-harp.jp/",
               "北海道・道内自治体の入札参加資格登録が必要です。"),
    "統合PPI": ("入札情報サービス PPI（i-ppi.jp）", "https://www.i-ppi.jp/",
              "発注機関ごとの入札参加資格登録が必要です。"),
    "裁判所BIT": ("裁判所 電子入札システム（BIT）", "https://www.bit.courts.go.jp/",
               "裁判所の競争入札参加資格の登録が必要です。"),
    "e-bisc": ("独立行政法人 電子調達システム（e-bisc）", "https://www.e-bisc.go.jp/",
               "対象独法の入札参加資格登録が必要です。"),
}


def portal_for(domain: str) -> tuple[str, str, str] | None:
    """ドメインから確実な電子入札ポータル(name, url, note)を返す。無ければ None。"""
    return KNOWN_PORTALS.get(classify_platform(domain))


def _clean_title(title: str) -> str:
    """検索精度を上げるため、タイトル末尾の "（PDF 221.2 KB）" 等を除去する。"""
    return re.sub(r"[（(]\s*PDF[^）)]*[）)]\s*$", "", title or "").strip()


def _search_url(*terms: str) -> str:
    """与えた語でのウェブ検索URL（常に最新・保守不要の確実な導線）。"""
    q = " ".join(t for t in terms if t)
    return "https://www.google.com/search?q=" + urllib.parse.quote(q)


def notice_search_url(title: str, agency: str = "") -> str:
    """その案件の公告ページをウェブで探すための検索URL。"""
    return _search_url(_clean_title(title), agency)


def register_search_url(prefecture: str, agency: str = "") -> str:
    """入札参加資格の登録方法を探すための検索URL。"""
    who = agency or prefecture
    return _search_url(who, "電子入札", "入札参加資格", "登録")


def application_guide(case: dict, agency_info: dict | None) -> dict:
    """案件1件に対する応募導線を組み立てて返す（テンプレートが使う dict）。

    返すキー:
      notice_url        … この案件の公告（実リンク, 無ければ None）
      notice_search_url … 公告をウェブで探す検索URL（常に有る）
      spec_url          … 設計図書の実リンク（無ければ None）
      portal            … (システム名, URL, 補足) 確実な大手ポータルのみ。無ければ None
      register_search   … 入札参加資格 登録方法の検索URL（常に有る）
    """
    detail = (case.get("detail_url") or "")
    spec = (case.get("spec_url") or "")
    domain = (agency_info or {}).get("domain", "")
    return {
        "notice_url": detail if is_real_link(detail) else None,
        "notice_search_url": notice_search_url(case.get("title", ""), case.get("agency", "")),
        "spec_url": spec if is_real_link(spec) else None,
        "portal": portal_for(domain),
        "register_search": register_search_url(case.get("prefecture", ""),
                                               (agency_info or {}).get("name", "")),
    }


# ---------------------------------------------------------------------------
# 必要書類・ToDo の確定的導出（AIなし・純関数）
#
# 設計方針（オーナー要望: 案件とToDo・応募内容を完全一致させる）:
#   案件の構造化属性（procurement_type / agency_type / bid_method / category）から、
#   日本の公共調達制度に照らして必要書類・手順を決定論的に導く。実行時にAI・
#   有料API・ネットを一切使わない。制度知識は一次情報で確認済み（要点）:
#     - 経営事項審査（経審）は「建設工事」を直接請け負う場合の制度。役務・委託は対象外。
#       （国交省: 公共工事を直接請け負うには経審が必須 / 役務提供委託は建設工事を除外）
#     - 国の機関: 役務・物品は「全省庁統一資格」。ただし建設工事と測量・建設コンサルは
#       全省庁統一資格の対象外で、各省庁ごとの建設工事資格審査が必要（内閣府・調達ポータル）。
#     - 地方公共団体: 各自治体の競争入札参加資格（いわゆる指名願い）。
#     - 入札方式: 指名競争は事前の有資格者名簿登載が前提、随意契約は見積中心。
# ---------------------------------------------------------------------------


def normalize_bid_method(bid_method: str) -> str:
    """入札方式の表記ゆれを正規化する。

    返り値は次のいずれかに畳む:
      "一般競争" / "指名競争" / "随意" / "委託・役務" / "不明"
    （「条件付一般競争入札」「制限付き一般競争入札」等のゆれを吸収する）
    """
    m = (bid_method or "").strip()
    if not m or m in ("NEW!!",):
        return "不明"
    # 指名競争（「公募型指名」「簡易公募型指名」等も含む）を一般より先に判定。
    if "指名" in m:
        return "指名競争"
    if "随意" in m or "随契" in m:
        return "随意"
    if "一般" in m:
        return "一般競争"
    if "委託" in m or "役務" in m:
        return "委託・役務"
    if "工事" in m or "設計" in m:
        # 「工事・設計」等、方式が明示されない区分表記。安全に汎用へ。
        return "不明"
    return "不明"


def procurement_kind(case: dict) -> str:
    """案件が建設工事か役務（委託）かを判定する。

    返り値: "工事" / "役務" / "不明"
    procurement_type を最優先し、空のときは category / title から推定する。
    """
    pt = (case.get("procurement_type") or "").strip()
    if pt in ("工事", "役務"):
        return pt
    if pt:
        # 想定外の値はそのまま尊重しつつ不明扱いにしない
        if "工事" in pt:
            return "工事"
        if "役務" in pt or "委託" in pt:
            return "役務"
    # procurement_type が空（自治体スクレイパ等）: タイトル・方式から推定。
    return infer_procurement_type(case.get("title", ""),
                                  case.get("bid_method", "")) or "不明"


def infer_procurement_type(title: str, bid_method: str = "") -> str:
    """タイトル・入札方式から区分（工事/役務）を推定する。

    確証が無ければ "" を返す（DB保存用。呼び出し側で「不明」扱いにできる）。
    役務語を優先判定（"○○工事の保守業務委託" 等で工事に誤判定しないため）。
    次にタイトルに「工事」を含めば工事と積極判定する。
    """
    t = title or ""
    if any(k in t for k in ("委託", "業務委託", "役務", "保守", "点検", "保安管理")):
        return "役務"
    if normalize_bid_method(bid_method) == "委託・役務":
        return "役務"
    if "工事" in t:
        return "工事"
    return ""


def _is_electrical(category: str) -> bool:
    """電気工事系の業種か（受変電・照明・発電・通信・計装・電気設備など）。"""
    c = category or ""
    return c.startswith("電気工事") or "電気" in c


# 電気以外の建設業種 → 必要となる建設業許可の業種名（代表的なもの）。
_CONSTRUCTION_LICENSE_BY_CATEGORY: dict[str, str] = {
    "塗装": "塗装工事業",
    "管工事": "管工事業",
    "空調": "管工事業",          # 空調設備は管工事業が一般的
    "土木・舗装": "土木工事業 / ほ装工事業",
    "防水": "防水工事業",
    "造園・外構": "造園工事業",
    "解体": "解体工事業",
}


def _is_electrical_safety_service(case: dict) -> bool:
    """役務のうち『電気保安管理（自家用電気工作物の保安）』系か。

    電気主任技術者の選任／保安管理業務委託（保安法人）が論点になる案件。
    """
    title = case.get("title", "")
    return any(k in title for k in
               ("保安管理", "電気工作物", "電気主任", "受電設備", "受変電", "高圧受電"))


# 「主たる工事が電気工事」と確証できるタイトル語（建設業許可=電気工事業 を断定する条件）。
# 体育館改造やダム工事の本文に出る「電気設備」等のサブ工事では断定しない（実PDF照合で
# 建築一式/土木一式が要求されると判明）。タイトルが電気工事そのものを示す時だけ断定する。
_STRONG_ELECTRIC_TITLE = (
    "電気工事", "電気設備工事", "電気設備改修", "受変電", "変電設備", "キュービクル",
    "高圧受電", "受電設備", "動力設備", "幹線改修", "幹線設備", "分電盤", "配電設備",
    "照明設備工事", "ＬＥＤ化", "LED化", "太陽光発電設備", "自家用電気工作物",
    "非常用発電設備", "電気主任技術者",
)


def _is_primary_electrical(title: str) -> bool:
    """タイトルから『主たる工事が電気工事』と確証できるか（許可業種の断定条件）。"""
    t = title or ""
    # 複数工事を1公告にまとめた束ね案件（「工事」が3つ以上）は主たる業種を特定できない。
    # 実PDF照合: 電線共同溝/保育所新築/舗装等の混在公告に「電気設備工事」が含まれても、
    # 必要な許可は建築一式・土木一式等であり電気と断定すると誤る。
    if t.count("工事") >= 3:
        return False
    return any(k in t for k in _STRONG_ELECTRIC_TITLE)


def _construction_license(title: str) -> tuple[str, str]:
    """必要な建設業許可の (label, note) を返す。

    タイトルが主として電気工事だと確証できる時だけ「電気工事業」を断定する
    （実PDF照合で検証済の確定情報）。それ以外の工事は、当方の分類だけでは必要な
    許可業種を断定できない（改修/整備/築造/舗装/大規模改造 等は専門業種でなく
    建築一式・土木一式が要求されることが多い）ため、断定せず公告での確認を促す。
    """
    if _is_primary_electrical(title):
        return ("建設業許可証明書（電気工事業）",
                "電気工事は電気工事業の許可。請負金額に応じ一般/特定。")
    return ("建設業許可証明書（公告で指定された業種を確認）",
            "必要な許可業種は案件で異なる。建物の新築・増改築や大規模改造は建築一式、"
            "道路・河川等の土木は土木一式、管・塗装・防水・造園・とび等の"
            "専門工事はその業種が一般的。公告の参加資格で必ず確認。")


def _qualification_step(case: dict, kind: str, bid_norm: str) -> dict:
    """入札参加資格（指名願い／全省庁統一資格）に関する ToDo を案件別に組み立てる。"""
    agency_type = (case.get("agency_type") or "").strip()
    is_national = agency_type == "国の機関"
    if is_national:
        if kind == "工事":
            # 国の建設工事は全省庁統一資格の対象外。各省庁の建設工事資格審査が必要。
            body = ("国の機関の建設工事は『全省庁統一資格』の対象外です。"
                    "発注省庁ごとの<strong>建設工事の競争参加資格審査</strong>を"
                    "事前に受けておく必要があります（受付時期・申請先を公告で確認）。")
        else:
            body = ("国の機関の役務・物品は<strong>全省庁統一資格</strong>"
                    "（役務の提供 等の区分）が必要です。調達ポータルから"
                    "統一資格審査申請を行ってください。")
    else:
        # 地方公共団体・都道府県・市区町村
        kind_word = "建設工事" if kind == "工事" else "役務（委託）"
        body = (f"この発注機関の<strong>競争入札参加資格（指名願い）</strong>の"
                f"登録が必要です（{kind_word}の区分）。受付期間と申請方法を確認してください。")
    if bid_norm == "指名競争":
        body += "　※指名競争入札のため、<strong>事前に有資格者名簿へ登載</strong>されていることが前提です。"
    elif bid_norm == "随意":
        body += "　※随意契約のため、見積依頼を受けられる体制（資格・実績）が重視されます。"
    return {"title": "入札参加資格を登録する（指名願い／資格審査）", "body": body}


def _documents(case: dict, kind: str, bid_norm: str) -> list[dict]:
    """案件属性に応じた必要書類リストを決定論的に組み立てる。"""
    category = case.get("category", "")
    agency_type = (case.get("agency_type") or "").strip()
    is_national = agency_type == "国の機関"
    docs: list[dict] = []

    # 1) 入札参加資格の申請書（共通。表現は国/地方で出し分け）
    if is_national and kind != "工事":
        docs.append({"label": "全省庁統一資格 資格審査結果通知書の写し",
                     "required": True,
                     "note": "国の役務・物品入札の参加資格。調達ポータルで取得。"})
    else:
        docs.append({"label": "入札参加資格審査申請書（指名願い）",
                     "required": True,
                     "note": "未登録なら事前申請が必要。登録済みなら資格者カード等。"})

    if kind == "工事":
        title = case.get("title", "")
        # 建設工事 共通（許可業種は『主たる工事が電気か』をタイトルで判定して断定/保留）
        _lic_label, _lic_note = _construction_license(title)
        docs.append({"label": _lic_label, "required": True, "note": _lic_note})
        docs.append({"label": "経営事項審査結果通知書（経審）",
                     "required": True,
                     "note": "公共工事を直接請け負うには経審が必須（建設工事の制度）。"})
        docs.append({"label": "工事経歴書・同種工事の施工実績調書",
                     "required": True,
                     "note": "発注者が求める同種・同規模の実績を示す。"})
        docs.append({"label": "配置予定技術者の資格証明（主任技術者／監理技術者）",
                     "required": True,
                     "note": "監理技術者は監理技術者資格者証・講習修了が必要な場合あり。"
                             "一定金額以上の公共工事は工事現場ごとの専任が求められる"
                             "（専任要否・要件は公告で確認）。"})
        # 現場代理人の届出は公共工事の多くで求められる（実PDF照合: 工事の約半数で登場）。
        docs.append({"label": "現場代理人・主任技術者等通知書（配置予定の届出）",
                     "required": False,
                     "note": "現場代理人や配置技術者を発注者へ届け出る様式。"
                             "様式・専任/兼任の可否は公告で確認。"})
        if _is_primary_electrical(title):
            docs.append({"label": "電気工事士免状の写し（必要に応じ電気主任技術者免状）",
                         "required": True,
                         "note": "電気工事の従事者資格。第一種/第二種は工事内容による。"})
    else:
        # 役務・委託 共通（※経審は出さない）
        docs.append({"label": "同種業務の実績調書（業務経歴）",
                     "required": True,
                     "note": "発注者が求める同種・同規模の受託実績を示す。"})
        if _is_electrical_safety_service(case):
            docs.append({"label": "電気主任技術者免状の写し（選任予定者）",
                         "required": True,
                         "note": "自家用電気工作物の保安管理。第一種〜第三種は電圧規模による。"})
            docs.append({"label": "保安管理業務外部委託承認に関する書類（保安法人の場合）",
                         "required": False,
                         "note": "電気主任技術者を選任せず外部委託する場合に該当。"})
        elif _is_electrical(category):
            docs.append({"label": "電気工事士免状等 有資格者の証明（業務内容による）",
                         "required": False,
                         "note": "点検・保守等で電気作業を伴う場合に必要となることがある。"})

    # 共通の財務・誓約系
    docs.append({"label": "納税証明書（国税・地方税）",
                 "required": True,
                 "note": "未納がないことの証明。指定様式・有効期限に注意。"})

    # 入札方式による追加
    if bid_norm == "随意":
        docs.append({"label": "見積書",
                     "required": True,
                     "note": "随意契約は見積提出が中心。指定様式・内訳に注意。"})
    else:
        docs.append({"label": "入札書（入札方法は公告で確認）",
                     "required": True,
                     "note": "電子入札か紙入札かは案件で異なる。電子入札なら事前にICカード"
                             "（電子証明書）・利用者登録が必要。紙入札なら持参/郵送の方法に従う。"})
        # 工事は入札（金額）内訳書を求められることが多い（実PDF照合: 約半数で登場）。
        if kind == "工事":
            docs.append({"label": "入札（金額）内訳書",
                         "required": False,
                         "note": "入札書への添付を求められることが多い。指定様式・費目に注意。"})

    # 入札保証金（公共調達の多くで必要・免除規定あり。実PDF照合: 10件中8件で登場）。
    if bid_norm != "随意":
        docs.append({"label": "入札保証金（または入札保証保険）",
                     "required": False,
                     "note": "公共工事・委託の多くで必要（多くは入札額の5%以上）。免除規定がある"
                             "場合も。公告で要否・金額・免除条件を必ず確認。"})
    # 契約保証金（落札後の契約締結時。公共調達の大半で要求・免除規定あり。
    # 実PDF照合: 工事・役務とも約80%の公告で「契約保証金」の記載あり）。
    docs.append({"label": "契約保証金（または履行保証保険・契約保証）",
                 "required": False,
                 "note": "落札・契約締結時に必要（多くは契約金額の10%以上）。"
                         "保証会社の保証・履行保証保険で代替できる場合や免除規定がある"
                         "場合も。公告・契約書で要否・率・方法を確認。"})
    return docs


def _steps(case: dict, kind: str, bid_norm: str, guide: dict | None) -> list[dict]:
    """案件別の ToDo ステップ（応募の流れ）を組み立てる。"""
    steps: list[dict] = []

    # 1. 公告を読む
    steps.append({
        "title": "公告・入札説明書を読む",
        "body": "参加資格・提出書類・締切・問い合わせ先・入札方法が記載されています。"
                "必要書類は発注機関により異なるため、必ず原典で確認してください。",
    })

    # 2. 参加資格
    steps.append(_qualification_step(case, kind, bid_norm))

    # 3. 必要書類の準備（書類の中身は documents が持つ。ここは導出根拠を示す）
    if kind == "工事":
        prep = ("建設業許可・経営事項審査（経審）・同種工事実績・配置予定技術者の資格を"
                "そろえます。")
        if _is_electrical(case.get("category", "")):
            prep += "電気工事のため電気工事士免状等もご用意ください。"
    elif kind == "役務":
        prep = "同種業務の実績と、業務に必要な有資格者を確認します。"
        if _is_electrical_safety_service(case):
            prep += "電気保安管理のため電気主任技術者の選任（または保安法人への委託）が論点です。"
        prep += "役務・委託のため経営事項審査（経審）は対象外です。"
    else:
        prep = ("案件区分（工事／役務）が不明です。公告で区分を確認し、"
                "必要書類をそろえてください。")
    steps.append({"title": "必要書類を準備する", "body": prep})

    # 4. 設計図書・仕様書の入手
    if guide and guide.get("spec_url"):
        spec_body = "この案件の仕様書は上部のダウンロードボタンから取得できます。"
    elif guide and guide.get("notice_url"):
        spec_body = "公告ページから設計図書（仕様書）の入手方法を確認してください。"
    else:
        spec_body = "公告ページを開き、設計図書（仕様書）の配布方法をご確認ください。"
    label = "設計図書（仕様書）を入手する" if kind == "工事" else "仕様書・業務仕様を入手する"
    steps.append({"title": label, "body": spec_body})

    # 5. 入札する
    bid_label = {"一般競争": "一般競争入札", "指名競争": "指名競争入札",
                 "随意": "随意契約", "委託・役務": "委託・役務"}.get(bid_norm, "")
    parts: list[str] = []
    if bid_label:
        parts.append(f"この案件は「{bid_label}」です。")
    deadline = case.get("deadline")
    if deadline:
        parts.append(f"申込締切は {deadline} です。")
    if bid_norm == "随意":
        parts.append("見積書を作成し、発注機関の指示に従って提出します。")
    else:
        parts.append("積算・見積をもとに、電子入札システム等で入札書を提出します。")
    steps.append({"title": "入札（見積）を提出する", "body": "".join(parts)})
    return steps


def _notes(case: dict, kind: str, bid_norm: str) -> list[str]:
    """安全弁となる注意書きを組み立てる。"""
    notes = [
        "必要書類・参加資格は発注機関ごとに異なります。必ず公告・入札説明書で確定してください。",
    ]
    if kind == "役務":
        notes.append("役務・委託は『経営事項審査（経審）』の対象外です（経審は建設工事の制度）。")
    if kind == "不明":
        notes.append("この案件は工事／役務の区分が判定できませんでした。公告で区分をご確認ください。")
    if bid_norm == "不明":
        notes.append("入札方式が公開データに無いため、公告で方式（一般競争／指名／随意）をご確認ください。")
    if kind == "工事":
        notes.append("入札方法（電子／紙）・現場説明会の有無・質問書の受付期限と様式・"
                     "必要な建設業許可の業種・発注機関の等級格付（A〜D等）・"
                     "落札方式（価格競争／総合評価方式）・配置技術者の専任義務・"
                     "入札保証金／契約保証金の要否は、案件ごとに異なります。"
                     "公告本文で必ず確認してください。")
    return notes


def application_requirements(case: dict, guide: dict | None = None) -> dict:
    """案件1件から必要書類・ToDo・注意書きを確定的（純関数）に導出する。

    実行時にAI・有料API・ネットを一切使わない。案件の構造化属性
    （procurement_type / agency_type / bid_method / category）から、日本の
    公共調達制度に照らして決定論的に組み立てる。

    引数:
      case  … cases テーブル1行の dict
      guide … application_guide() の戻り（spec_url 等の有無で仕様書ステップを出し分け）

    返り値の dict:
      documents       … list[{"label", "required": bool, "note"}]
      steps           … list[{"title", "body"}]
      notes           … list[str]（安全弁の注意書き）
      procurement_kind… "工事" / "役務" / "不明"（判定根拠）
      bid_method_norm … 正規化済み入札方式（"一般競争"/"指名競争"/"随意"/"委託・役務"/"不明"）
    """
    kind = procurement_kind(case)
    bid_norm = normalize_bid_method(case.get("bid_method", ""))
    return {
        "documents": _documents(case, kind, bid_norm),
        "steps": _steps(case, kind, bid_norm, guide),
        "notes": _notes(case, kind, bid_norm),
        "procurement_kind": kind,
        "bid_method_norm": bid_norm,
    }
