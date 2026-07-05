"""データ品質＆好機 自動監査（ランニングコスト0円）。

毎日 update.py の後に走らせる想定。次の2つを自動チェックして
audit.log に追記＋ audit_report.md に最新版を書き出す。

  1) データ品質監査 … 件数/鮮度/締切充足率/予定価格抽出率/区分・業種分布。
     しきい値を割ったら を立てる（取得0件・データが古い等の異常検知）。
  2) 好機監査 … 川野電気スコープ（電気系）×今応募できる(締切≥今日)×
     1000万以上 の案件を抽出。新規に現れたものは を付ける。

使い方:
  python audit.py … 監査して結果を表示＋ログ追記
  python audit.py --quiet … 表示は最小限（cron/launchd向け）

新規判定: 前回監査時に見えていた external_id を audit_state.json に保存し、
今回との差分を「新着の好機」として検知する（毎日の自動監視の核）。
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = BASE / "denki_bid.db"
LOG_PATH = BASE / "audit.log"
REPORT_PATH = BASE / "audit_report.md"
STATE_PATH = BASE / "audit_state.json"

# 品質しきい値（割ったら警告）
MIN_TOTAL = 500 # 総件数がこれ未満なら取得失敗を疑う
MAX_STALE_DAYS = 3 # 最新公告日が今日からこれ以上前なら「古い」
OPPORTUNITY_YEN = 10_000_000 # 好機の予定価格下限（1000万）
PDF_AUDIT_MIN_ACCURACY = 90 # ToDo精度(PDF実照合)の整合率がこれ未満なら警告


def pdf_audit(sample: int) -> dict | None:
    """ToDo/必要書類が公告PDFの実態と一致するかをサンプル照合する（PDCAのCheck）。

    audit_pdf.py を使い、開いている案件の公告PDFを実取得して当方の生成と突き合わせる。
    poppler/ネット未整備でも監査全体を止めないよう、失敗時は None を返す。
    """
    try:
        import audit_pdf
        return audit_pdf.run(sample=sample, only_open=True)
    except Exception: # noqa: BLE001 — PDF監査の失敗で日次監査を止めない
        return None


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _today() -> datetime.date:
    return datetime.date.today()


def quality_audit(c: sqlite3.Connection) -> tuple[dict, list[str]]:
    """データ品質の指標と警告リストを返す。"""
    q = lambda s: c.execute(s).fetchone()[0]
    total = q("SELECT COUNT(*) FROM cases")
    with_deadline = q("SELECT COUNT(*) FROM cases WHERE deadline != ''")
    with_price = q("SELECT COUNT(*) FROM cases WHERE budget_yen > 0")
    latest = q("SELECT MAX(announced_date) FROM cases WHERE announced_date != ''") or ""
    works = q("SELECT COUNT(*) FROM cases WHERE procurement_type='工事'")
    services = q("SELECT COUNT(*) FROM cases WHERE procurement_type='役務'")

    warns: list[str] = []
    if total < MIN_TOTAL:
        warns.append(f"総件数が少ない（{total} < {MIN_TOTAL}）＝取得失敗の疑い")
    if latest:
        try:
            stale = (_today() - datetime.date.fromisoformat(latest)).days
            if stale > MAX_STALE_DAYS:
                warns.append(f"データが古い（最新公告 {latest} ＝ {stale}日前）。update.py 未実行の疑い")
        except ValueError:
            pass
    if total and with_deadline / total < 0.2:
        warns.append(f"締切充足率が低い（{with_deadline*100//total}%）")

    metrics = {
        "total": total,
        "with_deadline": with_deadline,
        "deadline_rate": round(with_deadline * 100 / total) if total else 0,
        "with_price": with_price,
        "price_rate": round(with_price * 100 / total) if total else 0,
        "latest_announced": latest,
        "works": works,
        "services": services,
    }
    return metrics, warns


def opportunity_audit(c: sqlite3.Connection) -> list[dict]:
    """川野電気スコープ×今応募できる×1000万以上 の案件一覧。

    対象＝(電気工事 の工事・役務) ＋ (役務は業種を問わず：塗装/防水/清掃等も拾う)。
    ＝川野談「役務なので電気と管以外も拾いたい（塗装・防水等）」に対応。
    エリアはマイ条件(profile)の対応エリア（既定は関西6府県）。
    今応募できる＝締切が今日以降。金額順（高い順）。
    """
    today = _today().isoformat()
    # マイ条件の対応エリアで絞る（未設定なら全国）
    prefs: list[str] = []
    try:
        row = c.execute("SELECT prefectures FROM profile WHERE id = 1").fetchone()
        if row and row["prefectures"]:
            prefs = [p.strip() for p in row["prefectures"].split(",") if p.strip()]
    except sqlite3.Error:
        prefs = []

    # 電気の工事 or 役務。役務は塗装/防水/清掃/空調/管/警備等の物理工事系を対象とし、
    # category='その他'（税務システム等のIT/事務系）は川野さんの対象外なので除外する。
    where = ["(category LIKE '%電気工事%' OR procurement_type = '役務')",
             "category != 'その他'",
             "budget_yen >= ?", "deadline != ''", "deadline >= ?"]
    params: list = [OPPORTUNITY_YEN, today]
    if prefs:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefs)))
        params.extend(prefs)
    rows = c.execute(
        f"""
        SELECT external_id, title, agency, prefecture, procurement_type,
               category, budget, budget_yen, deadline, detail_url
        FROM cases
        WHERE {' AND '.join(where)}
        ORDER BY budget_yen DESC
        """,
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def _load_state() -> set[str]:
    if STATE_PATH.exists():
        try:
            return set(json.loads(STATE_PATH.read_text()).get("seen", []))
        except (ValueError, OSError):
            return set()
    return set()


def _save_state(ids: set[str]) -> None:
    STATE_PATH.write_text(json.dumps({"seen": sorted(ids)}, ensure_ascii=False))


def run(quiet: bool = False, pdf_sample: int = 0) -> int:
    """監査を実行。警告があれば 1、無ければ 0 を返す（cron判定用）。

    pdf_sample>0 なら、ToDo精度をサンプル件数だけ公告PDFと実照合する（PDCA）。
    """
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        metrics, warns = quality_audit(c)
        opps = opportunity_audit(c)

    pdf_rep = pdf_audit(pdf_sample) if pdf_sample > 0 else None
    if pdf_rep and pdf_rep["checked"] and pdf_rep["accuracy"] < PDF_AUDIT_MIN_ACCURACY:
        warns.append(f"ToDo精度が低い（PDF実照合 整合率 {pdf_rep['accuracy']}% "
                     f"< {PDF_AUDIT_MIN_ACCURACY}%）。必要書類/区分の生成ロジック要確認")

    seen = _load_state()
    now_ids = {o["external_id"] for o in opps}
    fresh = now_ids - seen # 前回監査に無かった＝新着の好機
    _save_state(now_ids)

    # ---- レポート(md) を書き出し ----
    md = [f"# 自動監査レポート {stamp}\n"]
    md.append("## データ品質")
    md.append(f"- 総件数: {metrics['total']}（工事 {metrics['works']} / 役務 {metrics['services']}）")
    md.append(f"- 最新公告日: {metrics['latest_announced']}")
    md.append(f"- 締切あり: {metrics['with_deadline']}（{metrics['deadline_rate']}%）")
    md.append(f"- 予定価格あり: {metrics['with_price']}（{metrics['price_rate']}%）")
    md.append("- 状態: " + ("正常" if not warns else "要確認"))
    for w in warns:
        md.append(f" - {w}")
    if pdf_rep is not None and pdf_rep.get("checked"):
        md.append(f"\n## ToDo精度（公告PDF実照合）")
        md.append(f"- 照合 {pdf_rep['checked']} 件 / 整合率 **{pdf_rep['accuracy']}%**"
                  f"（不一致 {pdf_rep['with_issues']} 件・読取不可 {pdf_rep['skipped']}）")
        if pdf_rep["by_type"]:
            md.append(f"- 不一致内訳: {pdf_rep['by_type']}")
        for d in pdf_rep["details"][:8]:
            for typ, msg in d["issues"]:
                md.append(f" - {typ}: {d['title']} — {msg}")

    md.append(f"\n## 好機（電気×今応募できる×1000万以上）: {len(opps)} 件 / 新着 {len(fresh)} 件")
    for o in opps[:30]:
        new = "" if o["external_id"] in fresh else ""
        price = o["budget"] or f"{o['budget_yen']:,}円"
        md.append(f"- {new}【{price}】締切{o['deadline']} {o['prefecture']} "
                  f"[{o['procurement_type']}] {o['title'][:40]}")
        if o["detail_url"]:
            md.append(f" - {o['detail_url']}")
    report = "\n".join(md) + "\n"
    REPORT_PATH.write_text(report, encoding="utf-8")

    # ---- ログ(1行サマリ) を追記 ----
    status = "OK" if not warns else "WARN"
    line = (f"{stamp} [{status}] total={metrics['total']} "
            f"latest={metrics['latest_announced']} 好機={len(opps)} 新着={len(fresh)} "
            f"warns={len(warns)}\n")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)

    if not quiet:
        print(report)
    else:
        print(line.strip())
    return 1 if warns else 0


if __name__ == "__main__":
    _pdf_n = 0
    if "--pdf" in sys.argv:
        try:
            _pdf_n = int(sys.argv[sys.argv.index("--pdf") + 1])
        except (ValueError, IndexError):
            _pdf_n = 25
    sys.exit(run(quiet="--quiet" in sys.argv, pdf_sample=_pdf_n))
