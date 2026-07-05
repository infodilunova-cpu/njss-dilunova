"""サンプル電気工事データの投入（関西中心）。

クライアントの拠点が関西のため、近畿（滋賀・京都・大阪・兵庫・奈良・和歌山）の
自治体電気工事入札を模した現実的なサンプルを投入する。
実データ（PPI 近畿・各自治体の入札情報公開システム）が入るまでの土台。

※ これはダミーデータです。実データは ppi_scraper / koukai_scraper で置き換える。
"""

from __future__ import annotations

from datetime import date, timedelta

import db
from regions import region_of

# (都道府県, 発注機関, 機関種別, 案件名, 入札方式, 締切までの日数,
#  仕様書ステータス, 取れない理由コード)
_RAW: list[tuple] = [
    ("大阪府", "大阪市", "市区町村", "市立小学校 受変電設備更新電気工事", "一般競争入札", 13, db.SPEC_AVAILABLE, ""),
    ("大阪府", "堺市", "市区町村", "市庁舎 非常用自家発電設備更新工事", "一般競争入札", 21, db.SPEC_AVAILABLE, ""),
    ("大阪府", "東大阪市", "市区町村", "市民会館 舞台照明設備改修電気工事", "指名競争入札", 7, db.SPEC_UNAVAILABLE, "login_required"),
    ("大阪府", "大阪府", "都道府県", "府営住宅 共用部LED照明改修工事", "一般競争入札", 16, db.SPEC_AVAILABLE, ""),
    ("大阪府", "枚方市", "市区町村", "浄水場 電気計装設備更新工事", "一般競争入札", 28, db.SPEC_AVAILABLE, ""),
    ("京都府", "京都市", "市区町村", "市営住宅 共用灯LED化電気工事", "指名競争入札", 6, db.SPEC_AVAILABLE, ""),
    ("京都府", "京都府", "都道府県", "府立高校 電気設備改修工事", "一般競争入札", 10, db.SPEC_UNAVAILABLE, "in_person"),
    ("京都府", "宇治市", "市区町村", "下水処理場 受変電設備改修電気工事", "一般競争入札", 19, db.SPEC_AVAILABLE, ""),
    ("兵庫県", "神戸市", "市区町村", "市立病院 電気設備改修工事", "一般競争入札", 4, db.SPEC_UNAVAILABLE, "request_form"),
    ("兵庫県", "姫路市", "市区町村", "市立中学校 体育館照明更新電気工事", "一般競争入札", 14, db.SPEC_AVAILABLE, ""),
    ("兵庫県", "西宮市", "市区町村", "市民体育館 太陽光発電設備設置工事", "一般競争入札", 30, db.SPEC_AVAILABLE, ""),
    ("兵庫県", "兵庫県", "都道府県", "県庁舎 高圧受変電設備更新工事", "一般競争入札", 23, db.SPEC_AVAILABLE, ""),
    ("兵庫県", "尼崎市", "市区町村", "ポンプ場 電気設備改修工事", "指名競争入札", 8, db.SPEC_UNAVAILABLE, "paid"),
    ("奈良県", "奈良市", "市区町村", "市庁舎 受変電設備更新電気工事", "一般競争入札", 17, db.SPEC_AVAILABLE, ""),
    ("奈良県", "奈良県", "都道府県", "県立図書館 電気設備改修工事", "一般競争入札", 11, db.SPEC_UNAVAILABLE, "not_published"),
    ("奈良県", "橿原市", "市区町村", "学校給食センター 受電設備工事", "指名競争入札", 15, db.SPEC_AVAILABLE, ""),
    ("滋賀県", "大津市", "市区町村", "市立小学校 空調用電源設備工事", "一般競争入札", 9, db.SPEC_AVAILABLE, ""),
    ("滋賀県", "滋賀県", "都道府県", "県道トンネル 照明設備更新電気工事", "一般競争入札", 22, db.SPEC_AVAILABLE, ""),
    ("滋賀県", "草津市", "市区町村", "公民館 電気設備改修工事", "指名競争入札", 5, db.SPEC_UNAVAILABLE, "period_closed"),
    ("和歌山県", "和歌山市", "市区町村", "市民会館 電気設備改修工事", "一般競争入札", 20, db.SPEC_AVAILABLE, ""),
    ("和歌山県", "和歌山県", "都道府県", "県営ダム 監視制御電気設備工事", "一般競争入札", 24, db.SPEC_AVAILABLE, ""),
    ("大阪府", "豊中市", "市区町村", "クリーンセンター 電気計装設備工事", "一般競争入札", 27, db.SPEC_UNAVAILABLE, "login_required"),
    ("大阪府", "吹田市", "市区町村", "市立図書館 LED照明改修電気工事", "指名競争入札", 12, db.SPEC_AVAILABLE, ""),
    ("京都府", "亀岡市", "市区町村", "浄化センター 受変電設備更新工事", "一般競争入札", 18, db.SPEC_AVAILABLE, ""),
    ("兵庫県", "明石市", "市区町村", "市立学校 受変電設備更新電気工事", "一般競争入札", 25, db.SPEC_AVAILABLE, ""),
]

# 過去落札の競合企業サンプル（落札者名, 落札価格）。関西の電気工事業者を想定。
# 一部企業を複数案件で落札させ、競合分析（落札件数ランキング）が成立するようにする。
_WINNERS: dict[int, tuple[str, str]] = {
    1: ("関西電設工業株式会社", "29,600,000円"),
    2: ("株式会社 大阪電業社", "58,300,000円"),
    4: ("関西電設工業株式会社", "18,400,000円"),    # 関西電設: 2勝
    6: ("京都電気工事株式会社", "21,700,000円"),
    8: ("京都電気工事株式会社", "33,900,000円"),    # 京都電気工事: 2勝
    10: ("神戸電設株式会社", "26,800,000円"),
    11: ("株式会社 阪神電気", "44,500,000円"),
    12: ("神戸電設株式会社", "62,800,000円"),       # 神戸電設: 2勝
    14: ("奈良電業株式会社", "19,200,000円"),
    16: ("奈良電業株式会社", "15,600,000円"),       # 奈良電業: 2勝
    17: ("近江電気工事株式会社", "23,400,000円"),
    18: ("近江電気工事株式会社", "48,100,000円"),    # 近江電気工事: 2勝
    20: ("株式会社 紀州電工", "27,300,000円"),
    21: ("株式会社 紀州電工", "51,900,000円"),       # 紀州電工: 2勝
    23: ("関西電設工業株式会社", "16,200,000円"),    # 関西電設: 3勝（トップ競合）
    24: ("京都電気工事株式会社", "38,700,000円"),    # 京都電気工事: 3勝
}


def build_rows() -> list[dict]:
    """サンプル定義を DB 行 dict のリストに変換する。"""
    today = date.today()
    rows: list[dict] = []
    for i, (pref, agency, atype, title, method, days, spec_status, reason) in enumerate(_RAW, start=1):
        deadline = (today + timedelta(days=days)).isoformat()
        announced = (today - timedelta(days=7)).isoformat()
        winner, win_price = _WINNERS.get(i, ("", ""))
        rows.append({
            "source": "SAMPLE",
            "external_id": f"SAMPLE-KANSAI-{i:04d}",
            "title": title,
            "agency": agency,
            "agency_type": atype,
            "region": region_of(pref) or "",
            "prefecture": pref,
            "category": "電気工事",
            "bid_method": method,
            "announced_date": announced,
            "deadline": deadline,
            "detail_url": "",
            "spec_status": spec_status,
            "spec_reason": reason,
            "spec_url": "",
            "budget": "",
            "winner": winner,
            "win_price": win_price,
        })
    return rows


def seed() -> int:
    """サンプルデータを投入し、件数を返す。"""
    db.init_db()
    rows = build_rows()
    return db.upsert_cases(rows)


if __name__ == "__main__":
    n = seed()
    print(f"関西サンプル {n} 件を投入しました（DB: {db.DB_PATH}）")
