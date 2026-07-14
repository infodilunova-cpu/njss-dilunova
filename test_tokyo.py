"""tokyo_scraper のオフラインテスト（ネット不要・AI不使用）。"""

from __future__ import annotations

import tokyo_scraper as t

_FAILED = 0


def _check(name: str, cond: bool) -> None:
    global _FAILED
    print(("  ok  " if cond else "  NG  ") + name)
    if not cond:
        _FAILED += 1


def test_parse_wareki() -> None:
    _check("令和8年7月14日 → 2026-07-14", t.parse_wareki("令和8年7月14日") == "2026-07-14")
    _check("令和8年12月1日 → 2026-12-01", t.parse_wareki("公表 令和8年12月1日") == "2026-12-01")
    _check("不正入力は空", t.parse_wareki("2026/07/14") == "")


def test_parse_kibou_deadline() -> None:
    _check("同年内: 7/14公告→7/21締切",
           t.parse_kibou_deadline("7月14日 ～7月21日 ", "2026-07-14") == "2026-07-21")
    _check("年またぎ: 12月公告→1月締切は翌年",
           t.parse_kibou_deadline("12月20日 ～1月10日", "2026-12-15") == "2027-01-10")
    _check("公告日なしは空", t.parse_kibou_deadline("7月14日 ～7月21日", "") == "")
    _check("期間なしは空", t.parse_kibou_deadline("", "2026-07-14") == "")


def test_row_to_case() -> None:
    row = {
        "pubStDate": "令和8年7月14日", "contNo": "3539991129291519",
        "contNoDisp": "8-00006",
        "ankenName": "業務サポートシステム機能追加委託（08-00006）",
        "gyosyuNm": "情報処理業務", "rikouKikan": "契約確定の日の翌日から令和9年1月29日まで",
        "bidWayName": "希望制指名競争入札", "kakuduke": "A,B,C",
        "kibouKikan": "7月14日 ～7月21日 ", "receiptCnd": "受付中",
        "divisionSectionName": "収用委員会事務局総務課",
    }
    c = t.row_to_case(row, "2")
    _check("external_idにcontNo", c["external_id"] == "TOKYO-3539991129291519")
    _check("announcedがISO", c["announced_date"] == "2026-07-14")
    _check("deadlineが希望申請終了日", c["deadline"] == "2026-07-21")
    _check("システム案件がIT系に分類", c["category"] == "システム・アプリ開発")
    _check("都道府県=東京都", c["prefecture"] == "東京都")
    _check("区分=物品・委託", c["procurement_type"] == "物品・委託")
    _check("タイトル無し行はNone", t.row_to_case({"contNo": "1"}, "2") is None)


if __name__ == "__main__":
    test_parse_wareki()
    test_parse_kibou_deadline()
    test_row_to_case()
    print(("=== 全テストPASS ===" if _FAILED == 0 else f"=== {_FAILED}件 失敗 ==="))
    raise SystemExit(1 if _FAILED else 0)
