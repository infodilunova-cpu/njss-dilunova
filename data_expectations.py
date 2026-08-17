"""データ検品の「期待仕様」を1か所に集めた、多層防御の芯。

## なぜこれが要るのか（2026-08-17の事故）

官公需API(kkj.go.jp)の取得が証明書エラーで2026-08-09から全滅していたのに、
8日間だれも気づかなかった（54,555件 → 0件）。テストは全部緑、ワークフローも毎日
「success」だった。壊れていたのはコードではなく**入ってくるデータの中身**で、
そこを誰も機械で見ていなかったからである。

しかもこのアプリは既存データを消さずに積むため、**総件数は125,276件のまま**で
一見なんともないように見えていた。件数の合計を眺めるだけでは絶対に気づけない。

「動いたか」ではなく「**入るべきものが入っているか**」を毎回機械で検品する。
そのために、各ソースが**最低これだけは入るはず**という期待値をコードに明文化する。
数字は実測に基づく（下の EXPECT の `why` に根拠を書くこと）。

## 使う側（多層で同じ期待値を参照する）

  1. preflight.py  … 取得前。外部ソースに繋がるかを実接続で確かめる
  2. update.py     … 取得直後。割れていたらビルドを止める（痩せたDBを公開しない）
  3. audit.py      … 毎日の自動監査レポートに載せる
  4. app.py        … 画面上部の警告バナー ＋ /api/data-health（重大なら HTTP 503）
  5. watchdog.yml  … 本番URLを外から叩く（GitHub Actionsを赤くする＝メールが飛ぶ）

**指摘されたズレは、その場でここに追記すること。** 同じ見落としを二度出さないため。
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sqlite3
from dataclasses import dataclass

# ============================================================
# 期待仕様（EXPECT）— 実測に基づく下限。ここが唯一の正本。
# ============================================================

# 鮮度: 最新公告日が今日から何日前までなら正常か。
# 公告は平日にしか出ない。金曜公告を月曜に見ると3日前になるため、
# 3日だと毎週月曜に誤報が出る。連休も考えて4日を正常上限とする。
MAX_STALE_DAYS = 4

# 総件数の下限。実測 --full 173,268件（2026-08-08の正常時）。
#
# 【重要】このアプリの --full は既存データを消さずに積む運用のため、**官公需APIが
# 完全に死んでも総件数は125,276件残っていた**（2026-08-09〜16の実績）。つまり
# 総件数の下限だけでは今回の障害は絶対に検知できない。**取得元別の下限と、
# 主力ソースの鮮度こそが本命**。総件数はあくまで「全部吹き飛んだ」場合の保険。
TOTAL_MIN_FULL = 60_000
TOTAL_MIN_FAST = 10_000


@dataclass(frozen=True)
class SourceExpect:
    """1つの取得元に期待する最低件数と、その根拠。"""

    name: str            # cases.source の値
    min_fast: int        # --fast のときの最低件数
    min_full: int        # --full のときの最低件数
    critical: bool       # True=割れたらビルドを止める / False=注意止まり
    why: str             # なぜこの数字なのか（実測値・役割）


# 【重要】ここに無いソースは検品されない。新しい取得元を足したら必ずここにも足すこと。
EXPECT: tuple[SourceExpect, ...] = (
    SourceExpect(
        name="官公需API",
        min_fast=5_000, min_full=25_000, critical=True,
        why="主力ソース。実測 --full 54,555件（2026-08-08の正常時）。2026-08-09に"
            "kkj.go.jpの証明書更新で0件になり、8日間で約48,000件を失った。"
            "総件数では検知できない（古いデータが残るため）ので、ここが唯一の砦",
    ),
    SourceExpect(
        name="調達ポータル落札実績",
        min_fast=1, min_full=3_000, critical=False,
        why="競合(落札者)分析用。DB実測 15,025件。--fast は直近差分だけなので"
            "0〜数十件が正常＝下限は置けない。--full のみ意味のある下限を持つ",
    ),
)

# 自治体系(PPI/京都府等)は取得が止まっても総件数で拾えるため個別の下限は置かない。


# 前回の正常値に対して、ここまで減ったら異常とみなす割合。
# 絶対値の下限だけでは「44,000件 → 21,000件」のような**半減**を見逃す（下限20,000は
# 超えているため）。取得の一部が静かに壊れていく劣化を捕まえるための相対しきい値。
DROP_CRITICAL = 0.60   # 前回正常値の60%未満＝重大
DROP_WARN = 0.80       # 80%未満＝注意

BASELINE_PATH = pathlib.Path(__file__).with_name("data_baseline.json")


@dataclass(frozen=True)
class Finding:
    """検品の指摘1件。"""

    critical: bool
    message: str

    def __str__(self) -> str:
        return f"{'【重大】' if self.critical else '【注意】'}{self.message}"


def load_baseline(mode: str) -> dict:
    """前回「正常」と判定できたときの件数。無ければ空 dict。"""
    try:
        return json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get(mode, {})
    except (OSError, ValueError):
        return {}


def save_baseline(mode: str, counts: dict[str, int]) -> None:
    """正常だったときだけ呼ぶこと。異常な値を基準にすると静かに下がり続ける。"""
    try:
        data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data[mode] = {"updated": _today().isoformat(), "total": sum(counts.values()),
                  "sources": counts}
    BASELINE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")


def _today() -> datetime.date:
    return datetime.date.today()


def source_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """cases をソース別に数える。"""
    return {r[0]: r[1] for r in
            conn.execute("SELECT source, COUNT(*) FROM cases GROUP BY source")}


def latest_announced(conn: sqlite3.Connection, source: str | None = None) -> str:
    """最新の公告日（YYYY-MM-DD）。無ければ空文字。"""
    sql = "SELECT MAX(announced_date) FROM cases WHERE announced_date != ''"
    args: tuple = ()
    if source:
        sql += " AND source = ?"
        args = (source,)
    return conn.execute(sql, args).fetchone()[0] or ""


def stale_days(latest: str) -> int | None:
    """最新公告日が何日前か。日付として読めなければ None。"""
    try:
        return (_today() - datetime.date.fromisoformat(latest)).days
    except (ValueError, TypeError):
        return None


def inspect(conn: sqlite3.Connection, *, full: bool = False) -> list[Finding]:
    """DBの中身を期待仕様と突き合わせ、指摘リストを返す。

    これが検品の本体。update.py / audit.py / app.py が同じ関数を呼ぶことで、
    「ビルドは通ったのに画面には警告が出る」ようなズレが起きないようにする。
    """
    findings: list[Finding] = []
    mode = "full" if full else "fast"      # 基準値ファイルのキー
    counts = source_counts(conn)
    total = sum(counts.values())

    # 1) 総件数
    floor = TOTAL_MIN_FULL if full else TOTAL_MIN_FAST
    if total < floor:
        findings.append(Finding(
            True, f"総件数が {total:,} 件（--{mode} の下限 {floor:,} 未満）＝取得が大量に"
                  "失敗している。この状態のDBを公開してはいけない"))

    # 2) ソース別の件数
    for e in EXPECT:
        got = counts.get(e.name, 0)
        want = e.min_full if full else e.min_fast
        if got < want:
            findings.append(Finding(
                e.critical,
                f"取得元「{e.name}」が {got:,} 件（--{mode} の下限 {want:,} 未満）。{e.why}"))

    # 3) 鮮度（全体）
    latest = latest_announced(conn)
    if not latest:
        findings.append(Finding(True, "公告日を持つ案件が1件も無い＝取得内容が壊れている"))
    else:
        d = stale_days(latest)
        if d is not None and d > MAX_STALE_DAYS:
            findings.append(Finding(
                False, f"最新の公告が {latest}（{d}日前）で、データ更新が止まっている"
                       f"可能性がある（正常は {MAX_STALE_DAYS} 日以内）"))

    # 4) 主力ソースだけの鮮度。総件数が足りていても、主力が止まって他ソースの
    #    古い案件で件数が埋まっている、という一番わかりにくい壊れ方を捕まえる。
    main_latest = latest_announced(conn, "官公需API")
    if main_latest:
        d = stale_days(main_latest)
        if d is not None and d > MAX_STALE_DAYS:
            findings.append(Finding(
                True, f"主力の「官公需API」の最新公告が {main_latest}（{d}日前）。"
                      "件数が足りていても新着が入っていない＝取得が壊れている"))

    # 5) 前回の正常値との比較。下限は超えているのに半分に減っている、という
    #    「静かな劣化」を捕まえる。絶対値だけ見ていると気づけない層。
    base = load_baseline(mode)
    prev_total = base.get("total", 0)
    if prev_total:
        ratio = total / prev_total
        if ratio < DROP_CRITICAL:
            findings.append(Finding(
                True, f"総件数が前回の正常値から {ratio:.0%} に急減（{prev_total:,} → "
                      f"{total:,} 件・前回 {base.get('updated', '不明')}）。取得の一部が"
                      "壊れている疑いが強い"))
        elif ratio < DROP_WARN:
            findings.append(Finding(
                False, f"総件数が前回の正常値の {ratio:.0%}（{prev_total:,} → {total:,} 件）。"
                       "公告の少ない時期かもしれないが、続くようなら取得を疑うこと"))
        for name, prev in base.get("sources", {}).items():
            got = counts.get(name, 0)
            if prev >= 500 and got < prev * DROP_CRITICAL:
                is_main = any(e.name == name and e.critical for e in EXPECT)
                findings.append(Finding(
                    is_main, f"取得元「{name}」が前回の {got / prev:.0%} に減少"
                             f"（{prev:,} → {got:,} 件）"))

    return findings


def format_report(findings: list[Finding], *, counts: dict[str, int] | None = None) -> str:
    """人が読む検品結果。件数を必ず添える（「異常なし」とだけ言わない）。"""
    lines = []
    if counts:
        lines.append("取得元別の件数:")
        for name, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - {name}: {n:,} 件")
    n_crit = sum(1 for f in findings if f.critical)
    lines.append(f"検品: 指摘 {len(findings)} 件（うち重大 {n_crit} 件）")
    lines += [f"  {f}" for f in findings]
    return "\n".join(lines)
