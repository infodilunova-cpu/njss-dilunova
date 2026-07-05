"""procurement.py の回帰テスト（追加依存なし・AI不使用）。

`python test_procurement.py` で実行。応募導線ロジックが壊れたら気付けるようにする。
これが「コードで自動的に正しさを担保する」仕組みの一部。
"""

from __future__ import annotations

import procurement as p


def _check(name: str, cond: bool) -> bool:
    print(("  OK " if cond else "FAIL") + "  " + name)
    return cond


def test_generic_url_detection() -> bool:
    ok = True
    ok &= _check("調達ポータル検索トップは汎用",
                 p.is_generic_url("https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101"))
    ok &= _check("PPI検索トップは汎用",
                 p.is_generic_url("https://www.i-ppi.jp/IPPI/SearchServices/Web/Index.htm"))
    ok &= _check("実PDFは汎用でない",
                 not p.is_generic_url("https://www.city.suita.osaka.jp/foo.pdf"))
    ok &= _check("空文字は汎用でない", not p.is_generic_url(""))
    return ok


def test_is_real_link() -> bool:
    ok = True
    ok &= _check("実httpリンクは有効", p.is_real_link("https://example.go.jp/koukoku.pdf"))
    ok &= _check("example.comダミーは無効", not p.is_real_link("http://example.com/x"))
    ok &= _check("汎用URLは無効",
                 not p.is_real_link("https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101"))
    ok &= _check("空は無効", not p.is_real_link(""))
    return ok


def test_classify_platform() -> bool:
    ok = True
    ok &= _check("東京都 e-procurement → 東京都電子調達",
                 p.classify_platform("www.e-procurement.metro.tokyo.lg.jp") == "東京都電子調達")
    ok &= _check("p-portal → GEPS", p.classify_platform("www.p-portal.go.jp") == "GEPS")
    ok &= _check("e-aichi → e-Aichi", p.classify_platform("www.chotatsu.e-aichi.jp") == "e-Aichi")
    ok &= _check("cals → 電子入札コアシステム",
                 p.classify_platform("ppi.cals-ibaraki.lg.jp") == "電子入札コアシステム")
    # 誤データ（競輪サイト）は判定できず "個別/不明" になり、ポータルを出さない
    ok &= _check("競輪サイト(誤データ)は個別/不明",
                 p.classify_platform("www.keirinwa.com") == "個別/不明")
    return ok


def test_portal_for() -> bool:
    ok = True
    portal = p.portal_for("www.p-portal.go.jp")
    ok &= _check("GEPSドメインで正規ポータルが返る", portal is not None and portal[1] == "https://www.p-portal.go.jp/")
    # 誤データ・個別ドメインは None（誤誘導しない）
    ok &= _check("誤データドメインは None", p.portal_for("www.keirinwa.com") is None)
    ok &= _check("不明ドメインは None", p.portal_for("") is None)
    return ok


def test_search_urls() -> bool:
    ok = True
    u = p.notice_search_url("吹田市立自然の家大規模改修工事（PDF 221.2 KB）", "大阪府吹田市")
    ok &= _check("公告検索URLはgoogle検索", u.startswith("https://www.google.com/search?q="))
    ok &= _check("PDFノイズが除去される", "PDF" not in p._clean_title("工事名（PDF 221.2 KB）"))
    r = p.register_search_url("大阪府", "吹田市役所")
    ok &= _check("登録検索URLに『入札参加資格』が含まれる", "%E5%85%A5%E6%9C%AD" in r or "入札参加資格" in r)
    return ok


def test_application_guide() -> bool:
    ok = True
    # 実リンクを持つ案件: notice_url が出て、汎用ではない
    case_real = {"title": "受変電設備工事", "agency": "大阪府吹田市", "prefecture": "大阪府",
                 "detail_url": "https://www.city.suita.osaka.jp/x.pdf", "spec_url": ""}
    g = p.application_guide(case_real, {"domain": "", "name": "吹田市役所"})
    ok &= _check("実リンク案件は notice_url が出る", g["notice_url"] is not None)
    ok &= _check("検索フォールバックは常にある", bool(g["notice_search_url"]))

    # 汎用URLしかない案件: notice_url は None（誤誘導しない）、検索で補う
    case_generic = {"title": "明石警察署受変電工事", "agency": "兵庫県", "prefecture": "兵庫県",
                    "detail_url": "https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101", "spec_url": ""}
    g2 = p.application_guide(case_generic, None)
    ok &= _check("汎用URL案件は notice_url=None", g2["notice_url"] is None)
    ok &= _check("汎用URL案件でも検索URLはある", bool(g2["notice_search_url"]))

    # GEPSドメインの機関: portal が出る
    g3 = p.application_guide(case_real, {"domain": "www.p-portal.go.jp", "name": "防衛省"})
    ok &= _check("GEPS機関は portal が出る", g3["portal"] is not None)
    return ok


def _labels(req: dict) -> list[str]:
    return [d["label"] for d in req["documents"]]


def _has(req: dict, keyword: str) -> bool:
    return any(keyword in lbl for lbl in _labels(req))


def _has_required(req: dict, keyword: str) -> bool:
    return any(keyword in d["label"] and d["required"] for d in req["documents"])


def test_normalize_bid_method() -> bool:
    ok = True
    ok &= _check("条件付一般競争入札→一般競争", p.normalize_bid_method("条件付一般競争入札") == "一般競争")
    ok &= _check("制限付き一般競争入札→一般競争", p.normalize_bid_method("制限付き一般競争入札") == "一般競争")
    ok &= _check("簡易公募型指名競争入札→指名競争", p.normalize_bid_method("簡易公募型指名競争入札") == "指名競争")
    ok &= _check("随意契約→随意", p.normalize_bid_method("随意契約") == "随意")
    ok &= _check("委託・役務→委託・役務", p.normalize_bid_method("委託・役務") == "委託・役務")
    ok &= _check("空→不明", p.normalize_bid_method("") == "不明")
    ok &= _check("NEW!!→不明", p.normalize_bid_method("NEW!!") == "不明")
    return ok


def test_procurement_kind() -> bool:
    ok = True
    ok &= _check("procurement_type=工事→工事", p.procurement_kind({"procurement_type": "工事"}) == "工事")
    ok &= _check("procurement_type=役務→役務", p.procurement_kind({"procurement_type": "役務"}) == "役務")
    ok &= _check("空+委託タイトル→役務",
                 p.procurement_kind({"procurement_type": "", "title": "機械警備等業務委託"}) == "役務")
    ok &= _check("空+保安管理→役務",
                 p.procurement_kind({"procurement_type": "", "title": "自家用電気工作物保安管理業務委託"}) == "役務")
    ok &= _check("空+情報なし→不明", p.procurement_kind({"procurement_type": "", "title": "○○整備"}) == "不明")
    # タイトルに「工事」を含めば工事と積極判定（自治体スクレイパの区分空対策）
    ok &= _check("空+工事タイトル→工事",
                 p.procurement_kind({"procurement_type": "", "title": "交通信号機改修工事"}) == "工事")
    return ok


def test_infer_procurement_type() -> bool:
    ok = True
    ok &= _check("工事タイトル→工事", p.infer_procurement_type("西浄化センター電気設備工事") == "工事")
    ok &= _check("委託タイトル→役務", p.infer_procurement_type("電力量計更新業務委託") == "役務")
    # 役務語を優先（"工事"を含んでも保守委託は役務）
    ok &= _check("工事を含む保守委託→役務", p.infer_procurement_type("受変電設備工事の保守業務委託") == "役務")
    # 判別不能は空文字（DB保存で「不明」扱いにできる）
    ok &= _check("判別不能→空", p.infer_procurement_type("○○整備") == "")
    return ok


def test_req_works_national() -> bool:
    """工事×国: 経審が必須、全省庁統一資格は『対象外』と案内、建設業許可が出る。"""
    ok = True
    case = {"procurement_type": "工事", "agency_type": "国の機関",
            "category": "電気工事-受変電", "bid_method": "一般競争入札", "title": "受変電設備工事"}
    req = p.application_requirements(case)
    ok &= _check("工事×国: 区分=工事", req["procurement_kind"] == "工事")
    ok &= _check("工事×国: 経審が必須書類に出る", _has_required(req, "経営事項審査"))
    ok &= _check("工事×国: 電気工事業許可が出る", _has(req, "電気工事業"))
    ok &= _check("工事×国: 電気工事士免状が出る", _has(req, "電気工事士免状"))
    # 頻出要件（実PDF照合: 工事の約80%で契約保証金、約半数で現場代理人）
    ok &= _check("工事×国: 契約保証金が出る（任意）", _has(req, "契約保証"))
    ok &= _check("工事×国: 現場代理人の届出が出る（任意）", _has(req, "現場代理人"))
    # 国の工事は全省庁統一資格の対象外（ステップ本文で明示）
    qstep = next(s for s in req["steps"] if "資格" in s["title"])
    ok &= _check("工事×国: 全省庁統一資格は対象外と案内", "対象外" in qstep["body"])
    return ok


def test_req_works_local() -> bool:
    """工事×地方: 経審必須・指名願いの案内が出る。"""
    ok = True
    case = {"procurement_type": "工事", "agency_type": "地方公共団体",
            "category": "電気工事-電気設備", "bid_method": "条件付一般競争入札", "title": "電気設備改修工事"}
    req = p.application_requirements(case)
    ok &= _check("工事×地方: 経審が必須", _has_required(req, "経営事項審査"))
    qstep = next(s for s in req["steps"] if "資格" in s["title"])
    ok &= _check("工事×地方: 指名願いの案内", "指名願い" in qstep["body"])
    return ok


def test_req_service_cleaning() -> bool:
    """役務×地方(清掃): 経審は出ない、業務実績が出る、電気資格は出ない。"""
    ok = True
    case = {"procurement_type": "役務", "agency_type": "地方公共団体",
            "category": "清掃・廃棄物", "bid_method": "一般競争入札", "title": "庁舎清掃業務委託"}
    req = p.application_requirements(case)
    ok &= _check("役務(清掃): 区分=役務", req["procurement_kind"] == "役務")
    ok &= _check("役務(清掃): 経審は出ない", not _has(req, "経営事項審査"))
    ok &= _check("役務(清掃): 業務実績が出る", _has(req, "同種業務の実績"))
    ok &= _check("役務(清掃): 電気主任技術者は出ない", not _has(req, "電気主任技術者"))
    ok &= _check("役務(清掃): 注意書きに経審対象外", any("対象外" in n for n in req["notes"]))
    # 契約保証金は役務にも出る（落札後の保証）。現場代理人は工事固有なので役務には出さない。
    ok &= _check("役務(清掃): 契約保証金が出る", _has(req, "契約保証"))
    ok &= _check("役務(清掃): 現場代理人は出ない（工事固有）", not _has(req, "現場代理人"))
    return ok


def test_req_service_security() -> bool:
    """役務×地方(警備): 経審は出ない。"""
    ok = True
    case = {"procurement_type": "役務", "agency_type": "地方公共団体",
            "category": "警備", "bid_method": "指名競争入札", "title": "機械警備業務委託"}
    req = p.application_requirements(case)
    ok &= _check("役務(警備): 経審は出ない", not _has(req, "経営事項審査"))
    ok &= _check("役務(警備): 入札方式=指名競争", req["bid_method_norm"] == "指名競争")
    qstep = next(s for s in req["steps"] if "資格" in s["title"])
    ok &= _check("役務(警備): 指名競争の前提案内", "名簿" in qstep["body"])
    return ok


def test_req_electrical_safety() -> bool:
    """役務(電気保安管理): 経審なし、電気主任技術者が必須、保安法人書類が任意で出る。"""
    ok = True
    case = {"procurement_type": "役務", "agency_type": "地方公共団体",
            "category": "電気工事-電気設備", "bid_method": "一般競争入札",
            "title": "自家用電気工作物保安管理業務委託"}
    req = p.application_requirements(case)
    ok &= _check("電気保安: 経審は出ない", not _has(req, "経営事項審査"))
    ok &= _check("電気保安: 電気主任技術者が必須", _has_required(req, "電気主任技術者"))
    ok &= _check("電気保安: 保安法人書類が任意で出る",
                 any("保安管理業務外部委託" in d["label"] and not d["required"] for d in req["documents"]))
    return ok


def test_req_service_national() -> bool:
    """役務×国: 全省庁統一資格が出る、経審は出ない。"""
    ok = True
    case = {"procurement_type": "役務", "agency_type": "国の機関",
            "category": "電気工事-受変電", "bid_method": "一般競争入札", "title": "高圧受電設備点検役務"}
    req = p.application_requirements(case)
    ok &= _check("役務×国: 全省庁統一資格が出る", _has(req, "全省庁統一資格"))
    ok &= _check("役務×国: 経審は出ない", not _has(req, "経営事項審査"))
    return ok


def test_req_random_contract() -> bool:
    """随意契約: 見積書が出て入札書は出ない。"""
    ok = True
    case = {"procurement_type": "工事", "agency_type": "地方公共団体",
            "category": "電気工事", "bid_method": "随意契約", "title": "電気設備修繕"}
    req = p.application_requirements(case)
    ok &= _check("随意: 区分=随意", req["bid_method_norm"] == "随意")
    ok &= _check("随意: 見積書が出る", _has(req, "見積書"))
    ok &= _check("随意: 入札書は出ない", not _has(req, "入札書"))
    return ok


def test_req_non_electrical() -> bool:
    """非電気(塗装)工事: 専門業種を断定せず『公告で確認』とし、電気工事士は出ない。

    実PDF照合で、非電気工事は専門業種でなく建築一式/土木一式が要求されることが
    多いと判明したため、当方は業種を断定しない（誤誘導を避ける）。
    """
    ok = True
    case = {"procurement_type": "工事", "agency_type": "地方公共団体",
            "category": "塗装", "bid_method": "一般競争入札", "title": "外壁塗装工事"}
    req = p.application_requirements(case)
    ok &= _check("非電気: 建設業許可は『公告で指定された業種を確認』", _has(req, "公告で指定された業種を確認"))
    ok &= _check("非電気: 塗装工事業を断定しない", not _has(req, "塗装工事業"))
    ok &= _check("非電気: 電気工事士は出ない", not _has(req, "電気工事士"))
    return ok


def test_req_electrical_license_asserted() -> bool:
    """電気(本業)工事は電気工事業の許可を断定する（実PDFで検証済の確定情報）。"""
    ok = True
    case = {"procurement_type": "工事", "agency_type": "地方公共団体",
            "category": "電気工事-電気設備", "bid_method": "一般競争入札", "title": "受変電設備改修工事"}
    req = p.application_requirements(case)
    ok &= _check("電気: 電気工事業の許可を断定", _has(req, "電気工事業"))
    return ok


def test_req_empty_bidmethod() -> bool:
    """入札方式が空: 不明に畳まれ、注意書きが出る。"""
    ok = True
    case = {"procurement_type": "工事", "agency_type": "地方公共団体",
            "category": "電気工事-照明", "bid_method": "", "title": "照明設備更新工事"}
    req = p.application_requirements(case)
    ok &= _check("空方式: bid_method_norm=不明", req["bid_method_norm"] == "不明")
    ok &= _check("空方式: 注意書きで方式確認を促す", any("方式" in n for n in req["notes"]))
    return ok


def main() -> int:
    tests = [
        test_generic_url_detection, test_is_real_link, test_classify_platform,
        test_portal_for, test_search_urls, test_application_guide,
        test_normalize_bid_method, test_procurement_kind,
        test_infer_procurement_type,
        test_req_works_national, test_req_works_local,
        test_req_service_cleaning, test_req_service_security,
        test_req_electrical_safety, test_req_service_national,
        test_req_random_contract, test_req_non_electrical,
        test_req_electrical_license_asserted,
        test_req_empty_bidmethod,
    ]
    all_ok = True
    for t in tests:
        print(f"\n[{t.__name__}]")
        all_ok &= t()
    print("\n" + ("=== 全テストPASS ===" if all_ok else "=== 失敗あり ==="))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
