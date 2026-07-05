"""京都府 入札情報公開システム（efftis / PPI_P）スクレイパー。

茨城などの電子入札コアシステム(KF00x)とは別系統（efftis製）だが、
「全案件詳細検索」がフラットな検索フォームになっており、工種=電気工事 で
**現在募集中の自治体電気工事**をまとめて取得できる（実機で31件確認）。

操作フロー（実機調査で確認）:
  PiCtBaFi02start.vm（全案件詳細検索）→ 工種 pPI_ITEMCD=電気工事 を選択
  → 表示件数 omeMaxDisplayRowCount を最大に → 「検索」
  → 結果テーブル（列は _COLS）。

結果テーブルの列:
  [0]No [1]調達機関(部局・事務所) [2]案件名称 [3]工事場所 [4]種別
  [5]入札方式 [6]資料配布(期間) [7]申請受付(期間) [8]詳細リンク
"""

from __future__ import annotations

import re

import db
import procurement

START_URL = ("https://kyoto.efftis.jp/26000/CALS/PPI_P/"
             "pages/PPI_P/PiCtBaFi02/PiCtBaFi02start.vm")
PREFECTURE = "京都府"

_COLS = ("no", "agency", "title", "place", "category",
         "bid_method", "haifu", "uketsuke")


def _wareki_to_iso(text: str, last: bool = False) -> str:
    """『令和8年06月10日』を 2026-06-10 に。複数あれば last で最後/最初を選ぶ。"""
    ms = re.findall(r"令和(\d+)年(\d+)月(\d+)日", text or "")
    if not ms:
        return ""
    g = ms[-1] if last else ms[0]
    year = 2018 + int(g[0])  # 令和1=2019 → 2018+N
    return f"{year}-{int(g[1]):02d}-{int(g[2]):02d}"


def fetch(headless: bool = True, timeout_ms: int = 30000) -> list[dict]:
    """京都府の電気工事 募集案件を取得して db スキーマの行 dict で返す。"""
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(START_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)

        form = next((f for f in page.frames
                     if f.query_selector("select[name=pPI_ITEMCD]")), None)
        if form is None:
            browser.close()
            raise RuntimeError("京都府 検索フォームが見つかりません（サイト構造変更の可能性）")

        form.select_option("select[name=pPI_ITEMCD]", label="電気工事")
        try:
            opts = form.eval_on_selector_all(
                "select[name=omeMaxDisplayRowCount] option", "o=>o.map(x=>x.text.trim())")
            form.select_option("select[name=omeMaxDisplayRowCount]", label=opts[-1])
        except Exception:
            pass

        btn = form.query_selector("input[value='検索']") or form.query_selector("input[type=submit]")
        if btn:
            btn.click()
        else:
            form.get_by_text("検索").first.click()
        page.wait_for_timeout(4000)

        rows = _parse(page)
        browser.close()
    return rows


def _parse(page) -> list[dict]:
    for fr in page.frames:
        try:
            items = fr.eval_on_selector_all(
                "table tr",
                """els => els.map(r => {
                    const cells = [...(r.cells||[])].map(c => c.innerText.replace(/\\s+/g,' ').trim());
                    return { cells };
                }).filter(x => x.cells.length >= 6 && /^\\d+$/.test(x.cells[0])
                    && x.cells.some(c => c.includes('電気')))""",
            )
        except Exception:
            items = []
        if not items:
            continue
        out: list[dict] = []
        for it in items:
            cells = it["cells"]
            rec = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(_COLS)}
            announced = _wareki_to_iso(rec["haifu"], last=False)
            deadline = (_wareki_to_iso(rec["uketsuke"], last=True)
                        or _wareki_to_iso(rec["haifu"], last=True))
            # 資料配布(設計図書)があれば取得可（実体は元システムの詳細ページ）
            spec_status = db.SPEC_AVAILABLE if rec["haifu"] else db.SPEC_UNKNOWN
            out.append({
                "source": "京都府入札情報公開",
                "external_id": f"KYOTO-{rec['no']}-{rec['title'][:18]}",
                "title": rec["title"],
                "agency": rec["agency"],
                "agency_type": "都道府県",
                "region": "近畿",
                "prefecture": PREFECTURE,
                "category": rec["category"] or "電気工事",
                "procurement_type": procurement.infer_procurement_type(
                    rec["title"], rec["bid_method"]),
                "bid_method": rec["bid_method"],
                "announced_date": announced,
                "deadline": deadline,
                "detail_url": START_URL,
                "spec_status": spec_status,
                "spec_reason": "",
                "spec_url": "",
                "budget": "",
                "winner": "",
                "win_price": "",
            })
        return out
    return []


def load() -> int:
    """京都府の電気工事を取得して DB に投入し、件数を返す。"""
    db.init_db()
    rows = fetch()
    rows = [r for r in rows if "電気" in r["category"] or "電気" in r["title"]]
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    n = load()
    print(f"京都府: 入札情報公開システムから {n} 件の電気工事を取得・投入しました")
