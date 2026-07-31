"""参加資格判定ポリシー（ai_assist.apply_verdict_policy）の回帰テスト。

実行:
  python test_ai_policy.py      # 単体（pytest不要・ネット不要・AI不使用）

目的（再現性の担保）:
  - マイ条件が白紙なら verdict は必ず △（〇/✕を断定しない）＋未登録の案内が先頭に付く。
  - 登録情報があるときだけ 〇/✕ をそのまま通す（✕は理由がある場合のみ）。
  - 「不明」などスキーマ外の値・理由なし✕は △ に丸める。
  - profile_registered が「判定材料の有無」を正しく見分ける
    （categories はDB既定値が入るため材料に数えない）。
"""

from __future__ import annotations

import ai_assist as a


# ---- profile_registered ----------------------------------------------------

def test_blank_profile_is_unregistered():
    assert a.profile_registered(None) is False
    assert a.profile_registered({}) is False
    # categories はDB既定値（電気工事）が必ず入るため、判定材料に数えない
    assert a.profile_registered({"categories": "電気工事", "company": "テスト社"}) is False


def test_grade_or_quals_or_qualifications_count_as_registered():
    assert a.profile_registered({"grade": "C"}) is True
    assert a.profile_registered({"quals": "第一種電気工事士"}) is True
    assert a.profile_registered(
        {"qualifications": [{"issuer": "国土交通省", "grade": "B"}]}) is True
    # issuer 空の行だけなら未登録扱い
    assert a.profile_registered({"qualifications": [{"issuer": ""}]}) is False


# ---- apply_verdict_policy --------------------------------------------------

def test_unregistered_forces_triangle():
    """白紙プロフィールでは、AIが〇や✕と言っても必ず△＋未登録案内になる。"""
    for ai_verdict in ("〇", "✕", "△", "不明", ""):
        e = a.apply_verdict_policy(
            {"verdict": ai_verdict, "reasons": ["等級: 公告に記載なし"]}, registered=False)
        assert e["verdict"] == "△"
        assert "未登録" in e["reasons"][0]
        assert "等級: 公告に記載なし" in e["reasons"]  # AIの理由も残す


def test_registered_passes_marubatsu_through():
    """登録済みなら 〇 / 理由つき✕ はそのまま通る。"""
    ok = a.apply_verdict_policy({"verdict": "〇", "reasons": ["等級: 要求C／自社C"]},
                                registered=True)
    assert ok["verdict"] == "〇"
    ng = a.apply_verdict_policy(
        {"verdict": "✕", "reasons": ["不足: 警備業認定（要求あり／自社未保有）"]},
        registered=True)
    assert ng["verdict"] == "✕"
    assert ng["reasons"][0].startswith("不足")


def test_baseless_batsu_and_unknown_become_triangle():
    """理由なしの✕・スキーマ外の値（不明等）は△に丸める。"""
    e = a.apply_verdict_policy({"verdict": "✕", "reasons": []}, registered=True)
    assert e["verdict"] == "△"
    e2 = a.apply_verdict_policy({"verdict": "不明", "reasons": ["材料不足"]}, registered=True)
    assert e2["verdict"] == "△"
    e3 = a.apply_verdict_policy(None, registered=True)
    assert e3["verdict"] == "△"
    assert e3["reasons"]  # 空のままにしない


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
