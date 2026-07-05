"""awards_scraper の回帰テスト（追加依存なし・ネットワーク不要）。

実行:
  .venv/bin/python test_awards.py   # 単体（pytest不要）
  .venv/bin/pytest test_awards.py   # pytestがあれば

目的（再現性の担保）:
  - 落札実績オープンデータCSV(8列)を正しく案件dictへ変換する（落札者/落札価格/機関/方式）。
  - 電気工事系のみ採用し、"電気の購入"等の小売電力契約は除外することを固定する。
  - external_id が "PPORTAL-<案件番号>" で一意化されることを固定する。
  - ダウンロードURL/ファイル名の組み立てを固定する（エンドポイント退行防止）。
"""

from __future__ import annotations

import datetime

import awards_scraper as a

# 落札実績オープンデータCSVの1行サンプル（列順は仕様2.2準拠の8列）。
# 0:案件番号 1:案件名称 2:落札決定日 3:落札価格 4:府省コード 5:入札方式コード 6:商号 7:法人番号
_ELEC_ROW = [
    "0000000000000554903", "Ｒ８関東本局電気通信設備保守運転監視業務",
    "2026-04-01", "161000000.00", "S1", "8002040", "株式会社ケーネス",
    "6010601062093",
]
_DENKI_KOUNYU_ROW = [  # 小売電力（"使用する電気の購入"）＝電気工事ではない→除外対象
    "0000000000000548803", "神戸航空交通管制部で使用する電気の購入",
    "2026-04-01", "76743905.00", "S1", "8002010", "株式会社エフオン",
    "9010001028064",
]
_LED_ROW = [
    "0000000000000600001", "庁舎ＬＥＤ照明改修工事",
    "2026-05-10", "12345678.00", "O1", "8002010", "サンプル電気工業株式会社",
    "1234567890123",
]


def test_download_url_and_filenames():
    """ダウンロードURL/ファイル名の組み立て（実エンドポイント退行防止）。"""
    assert a.all_filename(2026) == "successful_bid_record_info_all_2026.zip"
    assert (a.diff_filename(datetime.date(2026, 6, 21))
            == "successful_bid_record_info_diff_20260621.zip")
    url = a.download_url(a.all_filename(2026))
    assert url.startswith("https://api.p-portal.go.jp/pps-web-biz/UAB03/OAB0301")
    assert "fileversion=v001" in url
    assert "successful_bid_record_info_all_2026.zip" in url


def test_format_price():
    """落札価格: 小数0は整数カンマ表示。空/0/不正は ""。"""
    assert a._format_price("161000000.00") == "161,000,000円"
    assert a._format_price("100.50") == "100.50円"
    assert a._format_price("") == ""
    assert a._format_price("0.00") == ""
    assert a._format_price("abc") == ""


def test_row_to_case_electrical():
    """電気工事系の行 → 落札者/価格/機関/方式が埋まった案件dict。"""
    c = a._row_to_case(_ELEC_ROW)
    assert c is not None
    assert c["external_id"] == "PPORTAL-0000000000000554903"
    assert c["source"] == "調達ポータル落札実績"
    assert c["winner"] == "株式会社ケーネス"
    assert c["win_price"] == "161,000,000円"
    assert c["agency"] == "国土交通省"           # S1 → 国土交通省
    assert c["agency_type"] == "国の機関"
    assert c["bid_method"] == "一般競争入札・総合評価"  # 8002040
    assert "電気工事" in c["category"]            # profile既定(LIKE '%電気工事%')と互換
    assert c["announced_date"] == "2026-04-01"


def test_row_to_case_excludes_power_purchase():
    """"使用する電気の購入"（小売電力契約）は電気工事ではないので除外＝None。"""
    assert a._row_to_case(_DENKI_KOUNYU_ROW) is None


def test_row_to_case_short_or_empty():
    """列不足・必須欠落は None（不正データで落ちない）。"""
    assert a._row_to_case(["only", "three", "cols"]) is None
    no_winner = list(_ELEC_ROW)
    no_winner[a._COL_WINNER] = ""
    assert a._row_to_case(no_winner) is None


def test_parse_rows_dedup_and_filter():
    """電気系のみ採用し、同一案件番号は一意化される。"""
    rows = [_ELEC_ROW, _DENKI_KOUNYU_ROW, _LED_ROW, list(_ELEC_ROW)]  # 末尾は重複
    out = a.parse_rows(rows)
    ids = {c["external_id"] for c in out}
    assert ids == {"PPORTAL-0000000000000554903", "PPORTAL-0000000000000600001"}
    assert len(out) == 2  # 電気購入は除外、重複は1件に


def _run_all():
    tests = [v for n, v in sorted(globals().items())
             if n.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
