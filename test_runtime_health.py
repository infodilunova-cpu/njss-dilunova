"""稼働の見張りの回帰テスト（ネット不要・追加依存なし）。

実行:
  .venv/bin/python test_runtime_health.py

データの中身が正しくても、①保存先が死んでいる ②日次更新が止まっている
③画面がエラーを出し続けている なら、お客様の仕事は止まる。
それを**機械が気づける**ことを固定する。
"""
from __future__ import annotations

import os
import sys

os.environ.pop("SUPABASE_DB_URL", None)

import app as appmod  # noqa: E402
import data_expectations as dx  # noqa: E402
import supa  # noqa: E402

_ok = _ng = 0


def check(name: str, cond: bool) -> None:
    global _ok, _ng
    if cond:
        _ok += 1
        print(f"  ok  {name}")
    else:
        _ng += 1
        print(f"  NG  {name}")


# ---- 画面エラー(500)を握り潰さず記録する ----
@appmod.app.route("/__test_boom__")
def _boom():
    raise RuntimeError("テスト用のエラー")


client = appmod.app.test_client()
appmod.app.logger.disabled = True          # テスト出力を汚さない
n_before = len(appmod._recent_errors)
r = client.get("/__test_boom__")
check("想定外エラーは500を返す（握り潰して200にしない）", r.status_code == 500)
check("エラーが記録される（誰にも届かない500にしない）",
      len(appmod._recent_errors) == n_before + 1)
check("利用者には入力が無事だと伝える画面を出す",
      "失われていません" in r.get_data(as_text=True))
check("404は従来どおり404のまま（正規のHTTPエラーを巻き込まない）",
      client.get("/__no_such_page__").status_code == 404)

# ---- 保存先(Supabase)の死活 ----
check("保存先が死んでいたら重大",
      any(f.critical for f in dx.inspect_runtime(
          persist_enabled=True, persist_ok=False, persist_error="接続不可")))
check("原因が理由に含まれる（何を直せばよいか分かる）",
      any("接続不可" in f.message for f in dx.inspect_runtime(
          persist_enabled=True, persist_ok=False, persist_error="接続不可")))
check("保存先が生きていれば何も言わない",
      not dx.inspect_runtime(persist_enabled=True, persist_ok=True))
check("永続化を使っていない構成では判定しない",
      not dx.inspect_runtime(persist_enabled=False, persist_ok=None))

# ---- 日次更新が止まったことの自己申告 ----
import datetime  # noqa: E402
_iso = lambda d: (datetime.date.today() - datetime.timedelta(days=d)).isoformat() + "T00:00:00Z"
check("更新が止まったら重大（監視の仕組みが死んでも気づける）",
      any(f.critical and "自動更新" in f.message
          for f in dx.inspect_runtime(persist_enabled=False, persist_ok=None,
                                      last_deploy=_iso(dx.MAX_DEPLOY_STALE_DAYS + 3))))
check("昨日更新されていれば誤報を出さない（狼少年にしない）",
      not dx.inspect_runtime(persist_enabled=False, persist_ok=None, last_deploy=_iso(1)))
check("日付が壊れていても落ちない",
      dx.inspect_runtime(persist_enabled=False, persist_ok=None, last_deploy="こわれた") == [])

# ---- 画面エラーの多発 ----
check("画面エラーが続いていたら知らせる",
      any("画面エラー" in f.message for f in dx.inspect_runtime(
          persist_enabled=False, persist_ok=None,
          recent_errors=dx.MAX_RECENT_ERRORS + 1)))

# ---- 更新停止の判定材料に、開発機の実行を混ぜないこと ----
# data_baseline.json の "fast" は開発機の毎朝のジョブが書く。本番の更新が
# 止まっていても新しい日付が入りうるので、これを信じると監視が死ぬ。
import json  # noqa: E402
import tempfile  # noqa: E402
from pathlib import Path  # noqa: E402

_tmp = Path(tempfile.mkdtemp())
_real_file = Path(appmod.__file__)
_bak = {}
for name, body in [
    (".last-deploy", "2026-01-01T00:00:00Z"),
    ("data_baseline.json", json.dumps({
        "full": {"updated": "2026-01-02", "total": 1},
        "fast": {"updated": datetime.date.today().isoformat(), "total": 1},   # 開発機が今日書いた
    })),
]:
    f = _real_file.with_name(name)
    _bak[name] = f.read_text(encoding="utf-8") if f.exists() else None
    f.write_text(body, encoding="utf-8")
try:
    got = appmod._last_deploy_stamp()
    check("開発機が書いた fast の日付を、本番の更新とみなさない",
          got[:10] == "2026-01-02")
finally:
    for name, body in _bak.items():
        f = _real_file.with_name(name)
        if body is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(body, encoding="utf-8")

print(f"\n{_ok}/{_ok + _ng} passed")
sys.exit(1 if _ng else 0)
