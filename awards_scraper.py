"""調達ポータル（p-portal.go.jp）「落札実績オープンデータ」取得クライアント。

国（中央省庁・独法）の調達の **落札者（商号）・落札価格** を全国分まとめた公式・
無料のオープンデータ。CSV（ZIP圧縮）を HTTP で1ファイル取得するだけなので、
Playwright 不要・認証不要で動く＝競合（落札者）分析データの拡充に最適。

データ元: https://www.p-portal.go.jp/pps-web-biz/UAB02/OAB0201 （落札実績オープンデータ）
ダウンロードURL（ページ内 JS が組み立てる実エンドポイント）:
  https://api.p-portal.go.jp/pps-web-biz/UAB03/OAB0301?fileversion=v001&filename=<file>
  ・全件:  successful_bid_record_info_all_<YYYY>.zip      （前月末時点・年度別）
  ・差分:  successful_bid_record_info_diff_<YYYYMMDD>.zip  （日次・過去約2ヶ月）

CSV仕様（落札実績オープンデータ ファイル仕様 令和8年3月版）:
  ・文字コード UTF-8(BOM付) / 改行 CRLF / 全データを "" で囲み , 区切り
  ・ヘッダ行なし。1行=1落札実績。列順は下記 _COL_* の通り（全8列）:
     0 調達案件番号 / 1 調達案件名称 / 2 落札決定日(YYYY-MM-DD) /
     3 落札価格 / 4 府省コード / 5 入札方式コード /
     6 商号又は名称(=落札者) / 7 法人番号

設計方針（既存スキーマ尊重）:
  ・落札実績は「落札者(winner)/落札価格(win_price)が埋まった案件」として
    cases に upsert する。external_id は "PPORTAL-<調達案件番号>" で一意化。
  ・官公需API由来の公告案件(external_id=KKJ-…)とは案件番号体系が異なり安全に突合
    できないため、別レコードとして持つ。競合分析（db.list_competitors 等は
    winner!='' を全件横断）にはそのまま乗る。
  ・電気工事系の判定は kkj_scraper の分類器を流用（"電気の購入"等の小売電力契約は
    除外され、電気設備工事/保守・受変電・照明・通信設備等の本業案件が残る）。
  ・国の調達のため prefecture は持たない（府省＝全国）。region/prefecture は空。
    競合一覧は都道府県絞り込み時には出ないが、「全国」表示で集計に反映される。
"""

from __future__ import annotations

import csv
import datetime
import io
import urllib.request
import zipfile

import db
import kkj_scraper

# 落札実績オープンデータ ダウンロードAPI（認証不要・GET）。
_DL_BASE = "https://api.p-portal.go.jp/pps-web-biz/UAB03/OAB0301"
_FILEVERSION = "v001"

# CSV 列インデックス（ヘッダ行は無いので位置で参照する）
_COL_ITEM_NO = 0      # 調達案件番号
_COL_TITLE = 1        # 調達案件名称
_COL_DATE = 2         # 落札決定日 YYYY-MM-DD
_COL_PRICE = 3        # 落札価格（"31389000.00" のような文字列）
_COL_MINISTRY = 4     # 府省コード
_COL_METHOD = 5       # 入札方式コード
_COL_WINNER = 6       # 商号又は名称（落札者）
_COL_CORP_NO = 7      # 法人番号

# 府省コード → 機関名（落札実績オープンデータ ファイル仕様 3.1）。
MINISTRY_NAME: dict[str, str] = {
    "A1": "衆議院", "B1": "参議院", "C1": "国立国会図書館", "D1": "最高裁判所",
    "E1": "会計検査院", "F1": "人事院", "F2": "国家公務員倫理審査会",
    "G1": "内閣官房", "H1": "内閣法制局", "I1": "安全保障会議", "J1": "内閣府",
    "J2": "宮内庁", "J3": "公正取引委員会", "J4": "国家公安委員会", "J5": "警察庁",
    "J6": "金融庁", "J7": "消費者庁", "J8": "個人情報保護委員会",
    "J9": "カジノ管理委員会", "K1": "総務省", "K2": "公害等調整委員会",
    "K3": "消防庁", "L1": "法務省", "L2": "検察庁", "L3": "公安審査委員会",
    "L4": "公安調査庁", "M1": "外務省", "N1": "財務省", "N2": "国税庁",
    "O1": "文部科学省", "O2": "文化庁", "O3": "スポーツ庁", "P1": "厚生労働省",
    "P2": "中央労働委員会", "Q1": "農林水産省", "Q2": "林野庁", "Q3": "水産庁",
    "R1": "経済産業省", "R2": "資源エネルギー庁", "R3": "特許庁",
    "R4": "中小企業庁", "S1": "国土交通省", "S2": "運輸安全委員会", "S3": "観光庁",
    "S4": "気象庁", "S5": "海上保安庁", "T1": "環境省", "T2": "原子力安全庁",
    "U1": "防衛省", "V1": "復興庁", "W1": "デジタル庁", "JA": "こども家庭庁",
    "JB": "サイバー通信情報監理委員会",
}

# 入札方式コード → 名称（落札実績オープンデータ ファイル仕様 3.2）。
BID_METHOD_NAME: dict[str, str] = {
    "8002010": "一般競争入札・最低価格", "8002020": "一般競争入札・最高価格",
    "8002040": "一般競争入札・総合評価", "8002050": "一般競争入札・複数落札",
    "8003010": "指名競争入札・最低価格", "8003020": "指名競争入札・最高価格",
    "8003040": "指名競争入札・総合評価", "8003050": "指名競争入札・複数落札",
    "8004025": "随意契約方式・複数業者", "8001010": "随意契約方式・オープンカウンタ",
    "8004020": "随意契約方式・特定業者", "8004030": "随意契約方式・公募型プロポーザル方式",
    "8014025": "随意契約方式・複数業者・少額",
    "8011010": "随意契約方式・オープンカウンタ・少額",
    "8014020": "随意契約方式・特定業者・少額",
    "8014030": "随意契約方式・公募型プロポーザル方式・少額",
}


def download_url(filename: str) -> str:
    """落札実績オープンデータの直接ダウンロードURLを組み立てる。"""
    return f"{_DL_BASE}?fileversion={_FILEVERSION}&filename={filename}"


def all_filename(year: int) -> str:
    """全件データ（年度別）のファイル名。year は西暦4桁。"""
    return f"successful_bid_record_info_all_{year}.zip"


def diff_filename(date: datetime.date) -> str:
    """差分データ（日次）のファイル名。"""
    return f"successful_bid_record_info_diff_{date:%Y%m%d}.zip"


def _format_price(raw: str) -> str:
    """"31389000.00" → "31,389,000円"（小数以下が0なら整数表示）。空/不正は ""。"""
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        val = float(s)
    except ValueError:
        return ""
    if val <= 0:
        return ""
    if val == int(val):
        return f"{int(val):,}円"
    return f"{val:,.2f}円"


def _fetch_zip_csv(filename: str, timeout: int = 60) -> list[list[str]]:
    """ZIP を取得して中の CSV を行(list[str])のリストとして返す。

    取得・解凍に失敗（404やネット障害）したら例外を投げず [] を返す＝
    呼び出し側（毎日の更新）を止めない。
    """
    url = download_url(filename)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            blob = res.read()
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
            if not names:
                return []
            text = zf.read(names[0]).decode("utf-8-sig")
    except Exception:  # noqa: BLE001 — 一過性失敗/未生成ファイルは空扱いで継続
        return []
    return list(csv.reader(io.StringIO(text)))


def _row_to_case(cols: list[str]) -> dict | None:
    """CSV 1行(8列) → cases 用 dict。電気工事系でなければ None（除外）。"""
    if len(cols) < 8:
        return None
    item_no = (cols[_COL_ITEM_NO] or "").strip()
    title = (cols[_COL_TITLE] or "").strip()
    winner = (cols[_COL_WINNER] or "").strip()
    if not item_no or not title or not winner:
        return None

    category = kkj_scraper.classify_category(title, title=title)
    if not kkj_scraper.is_electrical(category):
        return None  # 電気工事系のみ採用（"電気の購入"等の小売電力契約は除外）

    ministry_cd = (cols[_COL_MINISTRY] or "").strip()
    method_cd = (cols[_COL_METHOD] or "").strip()
    win_price = _format_price(cols[_COL_PRICE])
    return {
        "source": "調達ポータル落札実績",
        "external_id": f"PPORTAL-{item_no}",
        "title": title,
        "agency": MINISTRY_NAME.get(ministry_cd, ministry_cd or "国の機関"),
        "agency_type": "国の機関",
        "region": "",
        "prefecture": "",  # 府省＝全国。都道府県情報は無い。
        "category": category,
        "procurement_type": "",
        "bid_method": BID_METHOD_NAME.get(method_cd, ""),
        "announced_date": (cols[_COL_DATE] or "").strip()[:10],
        "deadline": "",
        "detail_url": "",
        "spec_status": db.SPEC_UNKNOWN,
        "spec_reason": "",
        "spec_url": "",
        "budget": "",
        "budget_yen": 0,
        "winner": winner,
        "win_price": win_price,
        "description": "",
    }


def parse_rows(rows: list[list[str]]) -> list[dict]:
    """CSV 行リスト → 電気工事系の落札実績 dict リスト（external_id で一意化）。"""
    seen: dict[str, dict] = {}
    for cols in rows:
        case = _row_to_case(cols)
        if case is not None:
            seen[case["external_id"]] = case  # 同一案件番号は後勝ちで一意化
    return list(seen.values())


def fetch_year(year: int) -> list[dict]:
    """指定年度（西暦）の全件データから電気工事系の落札実績を取得して返す。"""
    return parse_rows(_fetch_zip_csv(all_filename(year)))


def fetch_recent_years(years: int = 2) -> list[dict]:
    """直近 years 年分の全件データを横断取得して external_id で一意化して返す。"""
    this_year = datetime.date.today().year
    seen: dict[str, dict] = {}
    for y in range(this_year, this_year - years, -1):
        for case in fetch_year(y):
            seen[case["external_id"]] = case
    return list(seen.values())


def fetch_diff_days(days: int = 7) -> list[dict]:
    """直近 days 日分の差分データを取得して電気工事系の落札実績を返す（一意化）。

    日次更新で使う軽量経路。各日のファイルは無い日もあるので失敗は空扱いで継続。
    """
    today = datetime.date.today()
    seen: dict[str, dict] = {}
    for d in range(1, days + 1):
        day = today - datetime.timedelta(days=d)
        for case in parse_rows(_fetch_zip_csv(diff_filename(day))):
            seen[case["external_id"]] = case
    return list(seen.values())


def load(years: int = 2) -> int:
    """直近 years 年分の落札実績を取得して DB に投入。投入件数を返す。

    重い経路（年度ファイルは数百KB×年数）なので update.py の --full 等で呼ぶ想定。
    """
    db.init_db()
    rows = fetch_recent_years(years=years)
    return db.upsert_cases(rows) if rows else 0


def load_diff(days: int = 7) -> int:
    """直近 days 日分の差分から落札実績を取得して DB に投入。投入件数を返す。

    軽量（小さい差分ファイルのみ）なので日次経路にも載せられる。
    """
    db.init_db()
    rows = fetch_diff_days(days=days)
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    import sys
    if "--diff" in sys.argv:
        print(f"調達ポータル落札実績（直近差分）: {load_diff()} 件")
    else:
        print(f"調達ポータル落札実績（直近2年・電気工事系）: {load()} 件")
