"""東京都電子調達システム（e-procurement.metro.tokyo.lg.jp）から発注予定情報を取得する。

官公需APIの東京都庁カバーは384件（IT系23件）しかなく、都の実際の発注（物品・委託
約400件＋工事約450件が常時掲載）との差が大きい。都はモバイル(SP)版に DataTables 用の
JSON API を持っており、HTTPのみ（Playwright不要）で一覧が取れる。

取得フロー（すべて同一セッション）:
  1. GET  /sp/orderplan/orderPlanSearch.html          … セッション確立(JSESSIONID+LB cookie)
  2. POST /sp/orderplan/orderPlanSearchAjax.json      … 一覧JSON（100件×ページング）

【運用上の配慮・必読】サイトの robots.txt はトップ以外を Disallow としている。
公共調達の公告は事業者に届けるために公表される情報だが、自動取得を歓迎する宣言では
ないため、本モジュールは (1) 1日1回の実行前提 (2) リクエスト間隔1秒以上
(3) 連絡先入りUser-Agent (4) 一覧のみで詳細ページは叩かない、の最小構成に留める。
体制が変わったら都(財務局経理部)へ自動取得可否の確認を推奨。
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.parse
import urllib.request

import db
from kkj_scraper import classify_category

BASE = "https://www.e-procurement.metro.tokyo.lg.jp"
SEARCH_PAGE = BASE + "/sp/orderplan/orderPlanSearch.html"
AJAX_URL = BASE + "/sp/orderplan/orderPlanSearchAjax.json"
USER_AGENT = "DiluNova-bid-search/1.0 (daily; contact: info.dilunova@gmail.com)"
REQUEST_INTERVAL_SEC = 1.2  # robots配慮: 連続リクエストの最低間隔
PAGE_SIZE = 100

_WAREKI = {"令和": 2018, "平成": 1988, "昭和": 1925}


def parse_wareki(s: str) -> str:
    """「令和8年7月14日」→ "2026-07-14"。解釈できなければ ""。"""
    m = re.search(r"(令和|平成|昭和)(\d+)年(\d+)月(\d+)日", s or "")
    if not m:
        return ""
    year = _WAREKI[m.group(1)] + int(m.group(2))
    return f"{year:04d}-{int(m.group(3)):02d}-{int(m.group(4)):02d}"


def parse_kibou_deadline(kibou: str, announced_iso: str) -> str:
    """希望申請期間「7月14日 ～7月21日」の終了日をISOで返す（年は公告日から補完）。

    年をまたぐ場合（公告12月・締切1月など）は締切月が公告月より小さければ翌年とする。
    """
    if not announced_iso:
        return ""
    m = re.search(r"～\s*(\d+)月(\d+)日", kibou or "")
    if not m:
        return ""
    month, day = int(m.group(1)), int(m.group(2))
    year = int(announced_iso[:4])
    if month < int(announced_iso[5:7]):
        year += 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _base_form() -> dict:
    """orderPlanSearchAjax.json のフォーム雛形（DataTablesサーバサイド方式）。"""
    return {
        "draw": "1", "start": "0", "length": str(PAGE_SIZE),
        "orderField": "0", "orderValue": "desc",  # 公表日の新しい順
        "ankenName": "", "gyosyuCd": "", "gyosyuNm": "", "syumokuCd": "",
        "syumokuNm": "", "bureauCd": "", "bureauNm": "", "municipalCd": "",
        "municipalNm": "", "kibouStartDateRefer": "", "kibouEndDateRefer": "",
        "clearflg": "0", "initflg": "0", "keiyakuNoGengo": "5",
        "keiyakuNoNendo": "", "keiyakuNoRenban": "", "keiyakuNoUnderGengo": "5",
        "keiyakuNoUnderKoshu": "00", "keiyakuNoUnderRenban": "",
        "kibouStartDate": "", "kibouEndDate": "",
    }


def _open_session(timeout: int = 30) -> urllib.request.OpenerDirector:
    """検索ページをGETしてセッションCookieを確立したopenerを返す。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    req = urllib.request.Request(SEARCH_PAGE, headers={"User-Agent": USER_AGENT})
    opener.open(req, timeout=timeout).read()
    return opener


def _fetch_page(opener, consgoods_type: str, start: int, timeout: int = 30) -> dict:
    form = _base_form()
    form["consgoodsType"] = consgoods_type
    form["start"] = str(start)
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(AJAX_URL, data=data, headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
    })
    raw = opener.open(req, timeout=timeout).read().decode("utf-8")
    return json.loads(raw)


def row_to_case(r: dict, consgoods_type: str) -> dict | None:
    """API 1行 → cases 用 dict。タイトル/案件番号が無い行は捨てる。"""
    title = (r.get("ankenName") or "").strip()
    cont_no = (r.get("contNo") or "").strip()
    if not title or not cont_no:
        return None
    announced = parse_wareki(r.get("pubStDate") or "")
    deadline = parse_kibou_deadline(r.get("kibouKikan") or "", announced)
    section = (r.get("divisionSectionName") or "").strip()
    gyosyu = (r.get("gyosyuNm") or "").strip()
    category = classify_category(gyosyu, title=title, vertical="all")
    if category == "その他" and gyosyu == "情報処理業務":
        # タイトルにIT語が無くても営業種目が情報処理ならIT案件（都の分類を信頼）
        category = "システム・アプリ開発"
    return {
        "source": "東京都電子調達",
        "external_id": f"TOKYO-{cont_no}",
        "title": title,
        "agency": "東京都" + (f"（{section}）" if section else ""),
        "agency_type": "地方公共団体",
        "region": "関東",
        "prefecture": "東京都",
        "category": category,
        "vertical": "denki",
        "procurement_type": "工事" if consgoods_type == "1" else "物品・委託",
        "bid_method": (r.get("bidWayName") or "").strip(),
        "announced_date": announced,
        "deadline": deadline,
        # SP版詳細はPOST必須で直リンク不可のため、検索ページ＋契約番号で辿れるようにする
        "detail_url": SEARCH_PAGE,
        "spec_status": db.SPEC_UNKNOWN,
        "spec_reason": "",
        "spec_url": "",
        "budget": "",
        "budget_yen": 0,
        "winner": "",
        "win_price": "",
        "description": " / ".join(x for x in (
            f"営業種目: {gyosyu}" if gyosyu else "",
            f"格付: {r.get('kakuduke')}" if r.get("kakuduke") else "",
            f"履行期間: {r.get('rikouKikan')}" if r.get("rikouKikan") else "",
            f"希望申請期間: {r.get('kibouKikan')}" if r.get("kibouKikan") else "",
            f"受付: {r.get('receiptCnd')}" if r.get("receiptCnd") else "",
            f"契約番号: {r.get('contNoDisp')}" if r.get("contNoDisp") else "",
        ) if x),
    }


def fetch(max_pages: int = 10) -> list[dict]:
    """掲載中の発注予定情報（物品・委託＋工事）を全ページ取得して返す。

    現状は物品・委託約400件＋工事約450件＝計9リクエスト前後で全件取れる。
    max_pages は暴走防止の上限（1種別あたり）。
    """
    opener = _open_session()
    out: dict[str, dict] = {}
    for goods_type in ("2", "1"):  # 2=物品・委託（IT含む・主目的）, 1=工事
        start = 0
        for _ in range(max_pages):
            time.sleep(REQUEST_INTERVAL_SEC)
            payload = None
            for attempt in range(3):  # 読み取りタイムアウト等の瞬断は再試行
                try:
                    payload = _fetch_page(opener, goods_type, start)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 2:
                        raise
                    time.sleep(3 * (attempt + 1))
            rows = payload.get("data") or []
            for r in rows:
                case = row_to_case(r, goods_type)
                if case:
                    out[case["external_id"]] = case
            total = int(payload.get("recordsFiltered") or 0)
            start += PAGE_SIZE
            if start >= total or not rows:
                break
    return list(out.values())


def load() -> int:
    """取得してDBへ投入（このソースの行だけ入替）。件数を返す。

    取得0件（サイト障害・仕様変更）のときは既存行を消さない。
    """
    db.init_db()
    rows = fetch()
    if not rows:
        return 0
    db.clear_cases("東京都電子調達")
    return db.upsert_cases(rows)


if __name__ == "__main__":
    print(f"東京都電子調達: {load()} 件")
