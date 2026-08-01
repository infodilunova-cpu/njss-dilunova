"""ユーザー別データ分離（マルチテナント）の回帰テスト。

実行:
  python test_multitenant.py      # 単体（pytest不要・ネット不要）

目的（再現性の担保）:
  - マイ条件・申請管理・AI判定キャッシュ・監視機関除外・協力会社が
    ユーザー（メールアドレス）ごとに完全に分離されること。
  - 旧スキーマ（user_email列なし）のDBが init_db で自動移行され、
    既存データは ''（共有バケット）に引き継がれること。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import db

_TMPDIR = tempfile.mkdtemp(prefix="mt_test_")

A, B = "a@example.com", "b@example.com"


def _fresh_db(name: str) -> None:
    db.DB_PATH = Path(_TMPDIR) / f"{name}.db"
    db.init_db()


def _case(ext: str, title: str) -> dict:
    return {"source": "官公需API", "external_id": ext, "title": title,
            "agency": "テスト機関", "deadline": "2099-01-01"}


def test_profile_is_isolated_per_user():
    _fresh_db("prof")
    db.save_profile("大阪府", "電気工事", "", grade="C", user=A)
    db.save_profile("東京都", "警備", "", grade="A", user=B)
    assert db.get_profile(A)["prefectures"] == "大阪府"
    assert db.get_profile(B)["prefectures"] == "東京都"
    assert db.get_profile(A)["grade"] == "C"
    # 未ログイン('')は白紙のまま
    assert db.get_profile("")["prefectures"] == ""


def test_applications_are_isolated_per_user():
    _fresh_db("apps")
    db.upsert_cases([_case("C1", "庁舎改修工事")])
    cid = db.get_case_id_by_external("C1")
    db.set_application(cid, "入札参加申請済み", user=A, note="Aのメモ")
    db.set_application(cid, "NG", user=B, note="Bのメモ")
    assert db.get_application(cid, user=A)["status"] == "入札参加申請済み"
    assert db.get_application(cid, user=B)["status"] == "NG"
    assert db.get_application(cid, user="") is None
    assert len(db.list_applications(user=A)) == 1
    assert db.list_applications(user=A)[0]["note"] == "Aのメモ"
    db.delete_application(cid, user=A)
    assert db.get_application(cid, user=A) is None
    assert db.get_application(cid, user=B) is not None  # Bの申請は残る


def test_ai_cache_is_isolated_per_user():
    """判定はマイ条件依存なので、キャッシュがユーザー間で混ざらないこと。"""
    _fresh_db("ai")
    db.set_ai_assist("X1", '{"v":"A"}', user=A)
    db.set_ai_assist("X1", '{"v":"B"}', user=B)
    assert db.get_ai_assist("X1", user=A)["payload"] == '{"v":"A"}'
    assert db.get_ai_assist("X1", user=B)["payload"] == '{"v":"B"}'
    assert db.get_ai_assist("X1", user="") is None


def test_exclusions_and_companies_are_isolated():
    _fresh_db("misc")
    db.set_agency_excluded("大阪市", True, user=A)
    assert db.list_agency_exclusions(user=A) == {"大阪市"}
    assert db.list_agency_exclusions(user=B) == set()
    db.upsert_company({"name": "A社"}, user=A)
    db.upsert_company({"name": "B社"}, user=B)
    assert [c["name"] for c in db.list_companies(user=A)] == ["A社"]
    assert [c["name"] for c in db.list_companies(user=B)] == ["B社"]


def test_saved_outputs_roundtrip_and_isolation():
    """AIアウトプットの保存が申請に載り、ユーザー間で混ざらず、他項目更新でも消えない。"""
    _fresh_db("outputs")
    db.upsert_cases([_case("O1", "AI出力保存テスト案件")])
    cid = db.get_case_id_by_external("O1")
    outs = [{"kind": "plan", "title": "入札準備プラン", "content": "スケジュール…", "saved_at": "2026-08-01"}]
    db.set_application(cid, "参加申請準備前", user=A, saved_outputs=outs)
    got = db.get_application(cid, user=A)
    assert got["saved_outputs"][0]["title"] == "入札準備プラン"
    assert db.get_application(cid, user=B) is None  # Bには存在しない
    # 既存値を渡して他項目を更新すれば保存分は残る（apply/save-outputの引き継ぎ規約）
    db.set_application(cid, "入札参加申請済み", user=A,
                       note="更新", saved_outputs=got["saved_outputs"])
    got2 = db.get_application(cid, user=A)
    assert got2["note"] == "更新" and len(got2["saved_outputs"]) == 1
    # 不正kindは弾かれる
    db.set_application(cid, "入札参加申請済み", user=A,
                       saved_outputs=[{"kind": "hack", "title": "x", "content": "y"}])
    assert db.get_application(cid, user=A)["saved_outputs"] == []


def test_user_key_is_normalized():
    """メールは大文字小文字・前後空白を吸収して同一ユーザー扱い。"""
    _fresh_db("norm")
    db.save_profile("京都府", "", "", user=" A@Example.COM ")
    assert db.get_profile("a@example.com")["prefectures"] == "京都府"


def test_legacy_schema_migrates_to_shared_bucket():
    """旧スキーマ（user_email無し）のDBは自動移行し、既存行は共有('')に残る。"""
    path = Path(_TMPDIR) / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cases (id INTEGER PRIMARY KEY AUTOINCREMENT,
                            external_id TEXT UNIQUE, title TEXT NOT NULL,
                            prefecture TEXT DEFAULT '', region TEXT DEFAULT '',
                            vertical TEXT DEFAULT '', category TEXT DEFAULT '',
                            deadline TEXT DEFAULT '');
        INSERT INTO cases (external_id, title) VALUES ('L1', '旧案件');
        CREATE TABLE profile (id INTEGER PRIMARY KEY CHECK (id=1),
                              company TEXT DEFAULT '', prefectures TEXT DEFAULT '',
                              categories TEXT DEFAULT '', budget_max TEXT DEFAULT '',
                              grade TEXT DEFAULT '', quals TEXT DEFAULT '');
        INSERT INTO profile (id, company, prefectures) VALUES (1, '旧会社', '奈良県');
        CREATE TABLE applications (case_id INTEGER PRIMARY KEY,
                                   status TEXT NOT NULL DEFAULT '参加申請準備前',
                                   note TEXT DEFAULT '');
        INSERT INTO applications (case_id, status, note) VALUES (1, 'NG', '旧メモ');
        CREATE TABLE ai_assist (external_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                                model TEXT DEFAULT '');
        CREATE TABLE agency_exclusions (name TEXT PRIMARY KEY);
        INSERT INTO agency_exclusions (name) VALUES ('旧機関');
    """)
    conn.commit()
    conn.close()
    db.DB_PATH = path
    db.init_db()
    # 旧データは共有バケット('')へ
    assert db.get_profile("")["company"] == "旧会社"
    app = db.get_application(1, user="")
    assert app and app["status"] == "NG" and app["note"] == "旧メモ"
    assert db.list_agency_exclusions("") == {"旧機関"}
    # 新規ユーザーは白紙から
    assert db.get_profile(A)["company"] == ""


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
