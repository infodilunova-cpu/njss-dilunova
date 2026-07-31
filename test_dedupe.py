"""db.dedupe_cases の回帰テスト（追加依存なし・一時DBで実行）。

実行:
  .venv/bin/python test_dedupe.py      # 単体（pytest不要）
  .venv/bin/pytest test_dedupe.py      # pytestがあれば

目的（再現性の担保）:
  - 全角/半角・空白ゆれの同一案件が1件に統合されること（件数カウンターの実数化）。
  - 公告＋調達ポータル落札実績の二重登録が、公告側へ落札者を移して1件になること。
  - 再公告（同名・同機関・締切違い）は締切が先の行が生き残ること。
  - 統合で消える行の申請(applications)が生き残り行へ付け替わること。
  - 別案件（タイトルが異なる）は統合されないこと。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import db

# テストごとに新しい一時DBへ差し替える（本物の denki_bid.db に触らない）
_TMPDIR = tempfile.mkdtemp(prefix="dedupe_test_")


def _fresh_db(name: str) -> None:
    db.DB_PATH = Path(_TMPDIR) / f"{name}.db"
    db.init_db()


def _case(ext: str, title: str, agency: str, **kw) -> dict:
    row = {"source": "官公需API", "external_id": ext, "title": title,
           "agency": agency, "deadline": "", "announced_date": ""}
    row.update(kw)
    return row


def test_normalize_dedupe_key_absorbs_width_and_space():
    """全角/半角・空白・全角括弧のゆれが同一キーになる。"""
    a = db.normalize_dedupe_key("令和８年度　健診業務（一式）", "法務省")
    b = db.normalize_dedupe_key("令和8年度 健診業務(一式)", "法務省")
    assert a == b
    # 別タイトルは別キー
    assert db.normalize_dedupe_key("A工事", "X市") != db.normalize_dedupe_key("B工事", "X市")


def test_zenkaku_hankaku_duplicates_collapse():
    """表記ゆれの同一案件2行が1行に統合され、カウントが実数になる。"""
    _fresh_db("width")
    db.upsert_cases([
        _case("E1", "令和８年度　庁舎清掃業務", "総務省", deadline="2099-01-10"),
        _case("E2", "令和8年度 庁舎清掃業務", "総務省", deadline="2099-01-20",
              budget="1,000,000円", budget_yen=1000000),
    ])
    stats = db.dedupe_cases()
    assert stats["collapsed"] == 1
    assert db.count_cases() == 1
    # 締切が先(2099-01-20)の行が生き残る
    rows = db.list_cases()
    assert rows[0]["deadline"] == "2099-01-20"
    assert rows[0]["budget_yen"] == 1000000


def test_award_row_merges_into_announcement():
    """公告＋落札実績の二重登録 → 実績行は消え、公告に落札者が付く。"""
    _fresh_db("award")
    db.upsert_cases([
        _case("K1", "健診業務委託　一式", "法務省", deadline="2020-01-01"),
        _case("P1", "健診業務委託 一式", "法務省", source=db.AWARD_SOURCE,
              winner="株式会社テスト", win_price="500,000円"),
    ])
    stats = db.dedupe_cases()
    assert stats["award_merged"] == 1
    assert db.count_cases() == 1
    row = db.list_cases()[0]
    assert row["source"] == "官公需API"          # 公告側が生き残る
    assert row["winner"] == "株式会社テスト"     # 落札者は移植される
    assert row["win_price"] == "500,000円"


def test_reannouncement_keeps_latest_deadline():
    """再公告（締切違いの同一案件3行）は最新締切の1行だけ残る。"""
    _fresh_db("reann")
    db.upsert_cases([
        _case("R1", "一般定期健康診断業務委託", "最高裁判所", deadline="2099-02-19"),
        _case("R2", "一般定期健康診断業務委託", "最高裁判所", deadline="2099-03-16"),
        _case("R3", "一般定期健康診断業務委託", "最高裁判所", deadline="2099-03-24"),
    ])
    db.dedupe_cases()
    rows = db.list_cases()
    assert len(rows) == 1
    assert rows[0]["deadline"] == "2099-03-24"


def test_application_is_remapped_to_survivor():
    """統合で消える行に付いていた申請は、生き残り行へ付け替わる。"""
    _fresh_db("apps")
    db.upsert_cases([
        _case("A1", "変電設備更新工事", "国土交通省", deadline="2099-01-01"),
        _case("A2", "変電設備更新工事", "国土交通省", deadline="2099-06-01"),
    ])
    old_id = db.get_case_id_by_external("A1")   # 締切が古い方＝消える側
    db.set_application(old_id, "入札参加申請済み", note="申請メモ")
    db.dedupe_cases()
    survivor_id = db.get_case_id_by_external("A2")
    app = db.get_application(survivor_id)
    assert app is not None and app["status"] == "入札参加申請済み"
    assert app["note"] == "申請メモ"


def test_short_generic_titles_are_not_merged():
    """正規化後6文字未満の短い汎用タイトルは誤統合リスクがあるため触らない。"""
    _fresh_db("short")
    db.upsert_cases([
        _case("S1", "物品購入", "X市", deadline="2099-01-01"),
        _case("S2", "物品購入", "X市", deadline="2099-02-01"),
    ])
    stats = db.dedupe_cases()
    assert stats["removed"] == 0
    assert db.count_cases() == 2


def test_distinct_cases_survive():
    """機関が同じでもタイトルが違えば別案件として両方残る。"""
    _fresh_db("distinct")
    db.upsert_cases([
        _case("D1", "本庁舎電気設備改修工事", "大阪府", deadline="2099-01-01"),
        _case("D2", "本庁舎機械設備改修工事", "大阪府", deadline="2099-01-01"),
    ])
    stats = db.dedupe_cases()
    assert stats["removed"] == 0
    assert db.count_cases() == 2


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
