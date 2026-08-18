"""お客様の入力が消えないことの回帰テスト（ネット不要・追加依存なし）。

実行:
  .venv/bin/python test_data_loss.py

2026-08-17の「8/3〜8/7の作業情報が全部消えている」というご報告の再現と、修正の検証。

事故の筋:
  1) 案件データ(cases)の取得が壊れ、痩せたDBが本番へ配られる
  2) 起動時の復元で external_id を今の案件IDに解決できず、申請が復元されない
  3) 利用者が1件でも編集すると、欠けた状態でSupabaseが丸ごと上書きされる
  4) お客様の入力が**永久に**失われる

ここでは 1)〜3) を実際に起こし、Supabase側(のダブル)が痩せないことを確かめる。
"""
import os, sys, tempfile
os.environ.pop("SUPABASE_DB_URL", None)
from pathlib import Path
import db, supa

# --- Supabase のダブル（プロセス内KV） ---
_KV = {}
supa.enabled = lambda: True
supa.save = lambda k, v: (_KV.__setitem__(k, v), True)[1]
supa.load = lambda k: _KV.get(k)
_blocked = []
supa.block_save = lambda reason: _blocked.append(reason)

_ok = _ng = 0
def check(name, cond):
    global _ok, _ng
    if cond: _ok += 1; print(f"  ok  {name}")
    else:    _ng += 1; print(f"  NG  {name}")

def fresh_db():
    """Renderの毎デプロイ相当＝まっさらなSQLite。"""
    db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
    db._connect.cache_clear() if hasattr(db._connect, "cache_clear") else None
    db.init_db()
    db._unlinked_apps.clear()
    db._supa_app_count.clear()

# ===== 1日目: 正常。案件があり、お客様が3件入力する =====
fresh_db()
db.upsert_cases([{"source": "官公需API", "external_id": f"KKJ-{i}",
                  "title": f"案件{i}", "agency": "テスト市"} for i in range(3)])
for i in range(3):
    db.set_application(db.get_case_id_by_external(f"KKJ-{i}"), "入札書提出済み",
                       note=f"担当メモ{i}")
check("1日目: 3件がSupabaseに保存される", len(_KV.get("applications") or []) == 3)
saved_day1 = list(_KV["applications"])

# ===== 2日目: 案件取得が壊れ、痩せたDBが配られる（案件1件しか無い） =====
fresh_db()
db.upsert_cases([{"source": "官公需API", "external_id": "KKJ-0",
                  "title": "案件0", "agency": "テスト市"}])
db.restore_from_supa()
n_restored = len(db.list_applications()) if hasattr(db, "list_applications") else None
check("2日目: 案件が無い2件は退避されている（捨てられていない）",
      sum(len(v) for v in db._unlinked_apps.values()) == 2)

# ===== 3日目相当: この状態で利用者が1件編集する（事故の引き金） =====
db.set_application(db.get_case_id_by_external("KKJ-0"), "NG", note="やっぱり見送り")

after = _KV.get("applications") or []
check("編集後もSupabaseは3件のまま（お客様の入力が消えていない）", len(after) == 3)
exts = sorted((a.get("external_id") or "") for a in after)
check("消えたはずの KKJ-1 / KKJ-2 が残っている", exts == ["KKJ-0", "KKJ-1", "KKJ-2"])
kept = {a["external_id"]: a for a in after}
check("退避された申請の入力内容（メモ）も保持されている",
      kept["KKJ-1"].get("note") == "担当メモ1")
check("編集した1件はちゃんと更新されている", kept["KKJ-0"].get("status") == "NG")

# ===== 4日目: 案件データが復旧すれば、全件が画面に戻る =====
fresh_db()
db.upsert_cases([{"source": "官公需API", "external_id": f"KKJ-{i}",
                  "title": f"案件{i}", "agency": "テスト市"} for i in range(3)])
db.restore_from_supa()
with db._connect() as c:
    n = c.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
check("4日目: 案件復旧後に3件すべてが画面へ戻る", n == 3)
check("退避リストは空になる", not any(db._unlinked_apps.values()))

# ===== 最後の砦: 退避の仕組みごと壊れた場合でも、急減する書き戻しは拒否する =====
db._unlinked_apps.clear()
db._supa_app_count = {db._u(""): 100}          # Supabaseには100件ある想定
_blocked.clear()
db._push_applications()           # 手元は3件しかない＝急減
check("急減する書き戻しは中止される（最後の砦）", len(_blocked) == 1)
check("中止の理由が記録される（利用者に知らせられる）",
      _blocked and "急減" in _blocked[0])
check("Supabaseは上書きされていない", len(_KV.get("applications") or []) == 3)

print(f"\n{_ok}/{_ok+_ng} passed")
sys.exit(1 if _ng else 0)
