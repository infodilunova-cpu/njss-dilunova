"""入札準備プラン（入札直前まで導く機能）の回帰テスト（追加依存なし・ネット/AI不要）。

実行:
  /usr/bin/python3 test_bid_plan.py   # 単体（pytest不要）

目的（再現性の担保）:
  - db.price_guide の統計計算を固定する（win_priceのパース・パーセンタイル・
    件数不足の層は None・落札率は比較ペア3件以上のときだけ）。
  - ai_assist.bid_plan のプロンプト組み立てを固定する（締切・今日の日付・
    落札実績統計・マイ条件がプロンプトに入ること）。
  - Gemini応答のパース（正常/異常）を固定する。異常時は安全なエラーdictを返す。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import ai_assist
import db


def _check(name: str, cond: bool) -> bool:
    print(("  OK " if cond else "FAIL") + "  " + name)
    return bool(cond)


# ---------------------------------------------------------------------------
# db.price_guide — 落札額の統計（非AI・決定的）
# ---------------------------------------------------------------------------

def test_win_price_parse() -> bool:
    ok = True
    ok &= _check("カンマ+円 をパース", db._win_price_yen("1,073,500,000円") == 1_073_500_000)
    ok &= _check("小数もパース（yen_to_intの10050誤読をしない）",
                 db._win_price_yen("2,500.75円") == 2501)
    ok &= _check("空文字は None", db._win_price_yen("") is None)
    ok &= _check("数字なしは None", db._win_price_yen("非公表") is None)
    ok &= _check("0円は None（統計に混ぜない）", db._win_price_yen("0円") is None)
    return ok


def test_percentile() -> bool:
    ok = True
    ok &= _check("奇数個の中央値", db._percentile([1, 2, 3, 4, 5], 0.5) == 3)
    ok &= _check("25パーセンタイル", db._percentile([1, 2, 3, 4, 5], 0.25) == 2)
    ok &= _check("75パーセンタイル", db._percentile([1, 2, 3, 4, 5], 0.75) == 4)
    ok &= _check("偶数個は線形補間", db._percentile([1, 2, 3, 4], 0.5) == 2.5)
    ok &= _check("1個でも動く", db._percentile([7], 0.5) == 7)
    return ok


def _seed_cases(rows: list[tuple]) -> None:
    """(category, agency, winner, win_price, budget_yen) を一時DBへ投入する。"""
    with db._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cases (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 category TEXT DEFAULT '', agency TEXT DEFAULT '',
                 winner TEXT DEFAULT '', win_price TEXT DEFAULT '',
                 budget_yen INTEGER DEFAULT 0)""")
        conn.executemany(
            "INSERT INTO cases (category, agency, winner, win_price, budget_yen)"
            " VALUES (?, ?, ?, ?, ?)", rows)
        conn.commit()


def test_price_guide_stats() -> bool:
    """一時DBで price_guide の統計計算を検証する（実DBに依存しない）。"""
    ok = True
    orig = db.DB_PATH
    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "test.db"
        try:
            _seed_cases([
                # 電気工事: 5件（大阪府3件・他機関2件）。予定価格は3件のみ判明。
                ("電気工事", "大阪府", "A社", "1,000,000円", 0),
                ("電気工事", "大阪府", "B社", "2,000,000円", 2_500_000),
                ("電気工事", "大阪府", "C社", "3,000,000円", 4_000_000),
                ("電気工事", "八尾市", "D社", "4,000,000円", 5_000_000),
                ("電気工事", "八尾市", "E社", "5,000,000円", 0),
                # 落札者なし＝公告のみ→母集団に入らない
                ("電気工事", "大阪府", "", "9,999,999円", 0),
                # 価格が壊れている行→スキップされる
                ("電気工事", "大阪府", "F社", "非公表", 0),
                # 管工事: 2件のみ→件数不足で層ごと None
                ("管工事", "大阪府", "G社", "1,000,000円", 0),
                ("管工事", "大阪府", "H社", "2,000,000円", 0),
            ])

            g = db.price_guide("電気工事", "大阪府")
            ok &= _check("同カテゴリ統計が返る", g is not None and g["category_stats"] is not None)
            c = g["category_stats"]
            ok &= _check("件数=5（落札者なし・価格不正は除外）", c["count"] == 5)
            ok &= _check("中央値=300万", c["median"] == 3_000_000)
            ok &= _check("25%=200万", c["p25"] == 2_000_000)
            ok &= _check("75%=400万", c["p75"] == 4_000_000)

            a = g["agency_stats"]
            ok &= _check("同一機関（大阪府・完全一致）は3件で統計あり",
                         a is not None and a["count"] == 3)
            ok &= _check("同一機関の中央値=200万", a["median"] == 2_000_000)

            w = g["win_rate"]
            ok &= _check("落札率: 比較ペア3件で算出", w is not None and w["count"] == 3)
            # 比率: 2.0/2.5=0.8, 3.0/4.0=0.75, 4.0/5.0=0.8 → 中央値 0.8
            ok &= _check("落札率の中央値=0.8", w["median"] == 0.8)

            g2 = db.price_guide("電気工事", "八尾市")
            ok &= _check("同一機関2件は件数不足で None", g2["agency_stats"] is None)

            g3 = db.price_guide("管工事", "大阪府")
            ok &= _check("カテゴリ2件は全層不足で全体 None", g3 is None)

            ok &= _check("カテゴリ空は None", db.price_guide("", "大阪府") is None)
            ok &= _check("実績ゼロのカテゴリは None", db.price_guide("警備", "大阪府") is None)
        finally:
            db.DB_PATH = orig
    return ok


def test_price_guide_winrate_insufficient() -> bool:
    """予定価格の分かる実績が3件未満なら落札率は省略（None）。"""
    ok = True
    orig = db.DB_PATH
    with tempfile.TemporaryDirectory() as td:
        db.DB_PATH = Path(td) / "test.db"
        try:
            _seed_cases([
                ("空調", "国土交通省", "A社", "1,000,000円", 1_200_000),
                ("空調", "国土交通省", "B社", "2,000,000円", 2_400_000),
                ("空調", "国土交通省", "C社", "3,000,000円", 0),  # 予定価格なし
            ])
            g = db.price_guide("空調", "国土交通省")
            ok &= _check("カテゴリ統計は3件で算出", g["category_stats"]["count"] == 3)
            ok &= _check("比較ペア2件では落札率を省略", g["win_rate"] is None)
        finally:
            db.DB_PATH = orig
    return ok


# ---------------------------------------------------------------------------
# ai_assist.bid_plan — プロンプト組み立て（純関数・ネット不要）
# ---------------------------------------------------------------------------

_CASE = {
    "title": "庁舎LED照明改修工事", "agency": "大阪府", "agency_type": "都道府県",
    "prefecture": "大阪府", "region": "近畿", "category": "電気工事-照明",
    "bid_method": "一般競争入札", "announced_date": "2026-07-01",
    "deadline": "2026-08-01", "budget": "9,328,000円", "budget_yen": 9_328_000,
    "description": "庁舎の照明をLED化する工事。", "detail_url": "",
}
_PROFILE = {"company": "川野電気株式会社", "prefectures": "大阪府",
            "categories": "電気工事", "grade": "C",
            "quals": "第一種電気工事士", "budget_max": ""}
_GUIDE = {"category": "電気工事-照明", "agency": "大阪府",
          "category_stats": {"count": 10, "median": 5_000_000,
                             "p25": 3_000_000, "p75": 8_000_000},
          "agency_stats": None,
          "win_rate": {"count": 4, "median": 0.85}}


def test_build_plan_text() -> bool:
    text = ai_assist._build_plan_text(_CASE, _PROFILE, None, _GUIDE,
                                      notice_text="", today="2026-07-15")
    ok = True
    ok &= _check("今日の日付が入る", "2026-07-15" in text)
    ok &= _check("締切が入り逆算を指示する",
                 "2026-08-01" in text and "逆算" in text)
    ok &= _check("案件名が入る", "庁舎LED照明改修工事" in text)
    ok &= _check("落札実績統計（中央値）が入る", "5,000,000円" in text)
    ok &= _check("落札率が入る", "85.0%" in text)
    ok &= _check("自社名（マイ条件）が入る", "川野電気株式会社" in text)
    ok &= _check("創作禁止の注意が入る", "創作しない" in text)
    return ok


def test_build_plan_text_no_guide() -> bool:
    """統計なしでも組み立てられ、無い旨をAIに伝える。"""
    text = ai_assist._build_plan_text(_CASE, None, None, None,
                                      notice_text="", today="2026-07-15")
    ok = True
    ok &= _check("統計なしを明示", "落札実績データなし" in text)
    ok &= _check("マイ条件未設定を明示", "マイ条件は未設定" in text)
    return ok


# ---------------------------------------------------------------------------
# レスポンスのパース（正常/異常）
# ---------------------------------------------------------------------------

_GOOD_PLAN = {
    "schedule": [
        {"date": "2026-07-15", "action": "公告本文と入札説明書を確認する"},
        {"date": "2026-07-31", "action": "入札書を提出する"},
    ],
    "documents": ["入札参加資格審査申請書", "納税証明書", ""],
    "draft": "弊社は大阪府を拠点とする電気工事業者です。",
    "price_hint": "同カテゴリの中央値は500万円です。",
    "risks": ["質問期限の見落とし"],
    "next_action": "今日中に公告PDFを読み締切を確認する",
}


def test_normalize_plan_good() -> bool:
    out = ai_assist._normalize_plan(dict(_GOOD_PLAN))
    ok = True
    ok &= _check("scheduleが2件残る", len(out["schedule"]) == 2)
    ok &= _check("schedule各行に date/action",
                 out["schedule"][0] == {"date": "2026-07-15",
                                        "action": "公告本文と入札説明書を確認する"})
    ok &= _check("documentsの空要素は除去", out["documents"] == ["入札参加資格審査申請書", "納税証明書"])
    ok &= _check("draftが残る", out["draft"].startswith("弊社は"))
    ok &= _check("next_actionが残る", "公告PDF" in out["next_action"])
    return ok


def test_normalize_plan_bad() -> bool:
    ok = True
    for name, bad in (("非dict応答", ["not", "a", "dict"]),
                      ("空dict応答", {}),
                      ("action無しのschedule", {"schedule": [{"date": "2026-07-15"}]})):
        try:
            ai_assist._normalize_plan(bad)
            ok &= _check(name + " は ValueError", False)
        except ValueError:
            ok &= _check(name + " は ValueError", True)
    return ok


def test_bid_plan_parse_paths() -> bool:
    """bid_plan の応答パース正常/異常（Gemini呼び出しをスタブ化・ネット不要）。"""
    ok = True
    orig_key = os.environ.get("GEMINI_API_KEY")
    orig_call = ai_assist._call_gemini
    orig_pdf = ai_assist._fetch_pdf_text
    try:
        # キー未設定（空文字で確実に無効化。.env の setdefault にも勝つ）
        os.environ["GEMINI_API_KEY"] = ""
        ok &= _check("キー無しは enabled:False",
                     ai_assist.bid_plan(dict(_CASE)) == {"enabled": False})

        os.environ["GEMINI_API_KEY"] = "test-key"
        ai_assist._fetch_pdf_text = lambda url, timeout=25: ""

        # 正常応答
        ai_assist._call_gemini = lambda text, **kw: dict(_GOOD_PLAN)
        out = ai_assist.bid_plan(dict(_CASE), _PROFILE, None, _GUIDE)
        ok &= _check("正常応答: enabled=True", out.get("enabled") is True)
        ok &= _check("正常応答: errorなし", "error" not in out)
        ok &= _check("正常応答: schedule/draft/next_action が揃う",
                     out["schedule"] and out["draft"] and out["next_action"])
        ok &= _check("正常応答: source=description（PDF無し）",
                     out.get("source") == "description")

        # 異常応答（JSONが壊れている＝json.loads が ValueError）
        def _broken(text, **kw):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        ai_assist._call_gemini = _broken
        out = ai_assist.bid_plan(dict(_CASE), _PROFILE, None, _GUIDE)
        ok &= _check("異常応答: enabledのまま error dict",
                     out.get("enabled") is True and "解析に失敗" in out.get("error", ""))

        # 異常応答（形は取れたが中身が空）
        ai_assist._call_gemini = lambda text, **kw: {}
        out = ai_assist.bid_plan(dict(_CASE), _PROFILE, None, _GUIDE)
        ok &= _check("空応答: error dict", "error" in out)
    finally:
        ai_assist._call_gemini = orig_call
        ai_assist._fetch_pdf_text = orig_pdf
        if orig_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = orig_key
    return ok


def main() -> int:
    tests = [
        test_win_price_parse, test_percentile,
        test_price_guide_stats, test_price_guide_winrate_insufficient,
        test_build_plan_text, test_build_plan_text_no_guide,
        test_normalize_plan_good, test_normalize_plan_bad,
        test_bid_plan_parse_paths,
    ]
    all_ok = True
    for t in tests:
        print(f"\n[{t.__name__}]")
        all_ok &= t()
    print("\n" + ("=== 全テストPASS ===" if all_ok else "=== 失敗あり ==="))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
