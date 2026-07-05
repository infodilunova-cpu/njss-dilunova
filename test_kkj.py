"""kkj_scraper の回帰テスト（追加依存なし）。

実行:
  .venv/bin/python test_kkj.py      # 単体（pytest不要）
  .venv/bin/pytest test_kkj.py      # pytestがあれば

目的（再現性の担保）:
  - 全国取得が「単一クエリ・1000件頭打ち」へ退行していないこと
    （fetch_nationwide_electrical が電気クエリを全国横断＋dedupする）を固定する。
  - 1000万超フィルタが依存する予定価格抽出（parse_budget_from_text）を固定する。
  - 締切抽出の基本挙動を固定する。
"""

from __future__ import annotations

import kkj_scraper as k


# ============================================================
# テスト用のダブル：fetch をモックして API を叩かせない
# ============================================================

def _install_fake_fetch(monkeypatch_target):
    """kkj_scraper.fetch を、呼び出しを記録する偽実装に差し替える。

    戻り値: 呼び出しログ list[(query, category, lg_codes)]
    各クエリは external_id がクエリ名に紐づく1件を返す（dedup検証用に一部重複させる）。
    """
    calls: list[tuple] = []

    def fake_fetch(query="電気工事", category="2", lg_codes=None, count=1000, timeout=40,
                   vertical=None):
        calls.append((query, category, tuple(lg_codes) if lg_codes else None))
        # クエリ名で一意な1件＋全クエリ共通の重複1件（dedup効果を見るため）
        return [
            {"external_id": f"KKJ-{query}", "title": f"{query}案件", "category": "電気工事-電気設備"},
            {"external_id": "KKJ-DUP", "title": "重複案件", "category": "電気工事-電気設備"},
        ]

    k.fetch = fake_fetch  # type: ignore[assignment]
    return calls


def test_nationwide_uses_all_electrical_queries_nationwide():
    """全国取得は電気クエリ群を lg_codes 無し(全国)で横断し、dedupする。"""
    orig = k.fetch
    try:
        calls = _install_fake_fetch(None)
        rows = k.fetch_nationwide_electrical()

        queried = [c[0] for c in calls]
        # 工事(Cat2)の電気クエリが全て使われている（"電気工事"単一に退行していない）
        for q in k.ELEC_QUERIES:
            assert q in queried, f"工事クエリ {q} が全国取得で使われていない"
        # 役務(Cat3)は電気役務に限定（広いSERVICE_QUERIESを全国に流していない）
        for q in k.ELEC_SERVICE_QUERIES:
            assert q in queried, f"電気役務クエリ {q} が使われていない"
        assert "塗装" not in queried and "清掃" not in queried, "全国に広い非電気役務が漏れている"
        # 全クエリが全国（lg_codes=None）で呼ばれている
        assert all(c[2] is None for c in calls), "全国取得なのに都道府県限定で呼ばれている"
        # category は Cat2 と Cat3 の両方が使われている
        cats = {c[1] for c in calls}
        assert cats == {"2", "3"}, f"想定カテゴリで呼ばれていない: {cats}"
        # dedup: KKJ-DUP は1件に集約される
        ids = [r["external_id"] for r in rows]
        assert ids.count("KKJ-DUP") == 1, "external_id の重複排除が効いていない"
        # ユニークなクエリ名ぶんの案件＋共通1件（受変電/電気主任技術者は両群に重複）
        expected = len(set(k.ELEC_QUERIES) | set(k.ELEC_SERVICE_QUERIES)) + 1
        assert len(rows) == expected, f"件数が想定外: {len(rows)} != {expected}"
    finally:
        k.fetch = orig


def test_fetch_retry_recovers_from_transient_error():
    """_fetch_retry は一過性失敗を再試行し、最終的に成功すれば結果を返す。"""
    orig = k.fetch
    try:
        state = {"n": 0}

        def flaky(query="", category="2", lg_codes=None, count=1000, timeout=40,
                  vertical=None):
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("一過性")
            return [{"external_id": "KKJ-OK", "title": "ok"}]

        k.fetch = flaky  # type: ignore[assignment]
        # sleepを潰して高速化
        import time as _t
        orig_sleep = _t.sleep
        _t.sleep = lambda *_a, **_k: None
        try:
            out = k._fetch_retry("電気工事", "2", retries=2)
        finally:
            _t.sleep = orig_sleep
        assert out and out[0]["external_id"] == "KKJ-OK", "リトライ後の成功を拾えていない"
        assert state["n"] == 2, "リトライ回数が想定外"
    finally:
        k.fetch = orig


def test_parse_budget_picks_max_yen():
    """予定価格抽出：本文から円を数値化し、複数あれば最大を採る（1000万フィルタの土台）。"""
    yen, txt = k.parse_budget_from_text("予定価格 12,300,000円 ほか 参考価格 9,000,000円")
    assert yen == 12_300_000, f"最大額を採れていない: {yen}"
    assert txt == "12,300,000円"
    # 妥当域外（10万未満）は採らない
    assert k.parse_budget_from_text("手数料 50,000円")[0] == 0
    # 抽出不能は (0, "")
    assert k.parse_budget_from_text("金額の記載なし") == (0, "")


def test_parse_deadline_reiwa_and_keyword_priority():
    """締切抽出：提出期限(令和)を西暦ISOへ。年無しM月D日は推測しない。"""
    iso = k.parse_deadline_from_text("入札書提出期限 令和8年6月30日 開札 令和8年7月1日")
    assert iso == "2026-06-30", f"提出期限を優先できていない: {iso}"
    # 年が特定できない裸の日付は採らない（誤締切より空）
    assert k.parse_deadline_from_text("締切は6月30日") == ""


def test_parse_deadline_scans_all_occurrences():
    """見出しの「提出期限」に空振りしても、後続の実日付を拾える（全出現探索）。"""
    text = ("５ 入札書の提出期限及び場所\n"
            "(1) 提出期限 電子調達システムにより令和8年7月15日まで")
    assert k.parse_deadline_from_text(text, "2026-06-20") == "2026-07-15"


def test_parse_deadline_rejects_out_of_range():
    """公告日と同日・公告前・現実離れした遠い先（工期末等）は締切として採らない。"""
    # 同日は不可（入札締切が公告当日はあり得ない）
    assert k.parse_deadline_from_text("提出期限 令和8年6月20日", "2026-06-20") == ""
    # 公告より前は不可
    assert k.parse_deadline_from_text("提出期限 令和8年6月1日", "2026-06-20") == ""
    # 150日超（工期末の誤抽出想定）は不可
    assert k.parse_deadline_from_text("提出期限 令和9年3月26日", "2026-06-20") == ""
    # 範囲内は採る
    assert k.parse_deadline_from_text("提出期限 令和8年7月10日", "2026-06-20") == "2026-07-10"


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
