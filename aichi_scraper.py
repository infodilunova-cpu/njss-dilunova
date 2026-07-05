"""e-Aichi（愛知県 電子調達システム ebidPPIPublish）スクレイパー。

CALS/EC の入札情報公開システム（ebidPPIPublish / EjPPIj）。愛知県＋県内市町村など
76機関を1基盤でカバー。入札公告の検索で工種=電気工事を指定して実データを取得する。

操作フロー（実機確認）:
  EjPPIj → メニュー画像「入札公告」をクリック → 検索フォーム
  → 工種 KoujiSyubetu=電気工事、表示件数 ejMaxDisplayRowCount を最大 → 検索(input[type=image])
  → 結果テーブル（列は _COLS）。

結果テーブルの列:
  [0]No [1]案件名称(+案件番号) [2]調達機関 [3]工事場所 [4]工種
  [5]審査方式 [6]公告日 [7]入札締切 [8]開札日
"""

from __future__ import annotations

import re

import db

START_URL = "https://www.chotatsu.e-aichi.jp/ebidPPIPublish/EjPPIj"
PREFECTURE = "愛知県"

_COLS = ("no", "title", "agency", "place", "category",
         "review", "d_koukoku", "d_shimekiri", "d_kaisatu")


def _r_to_iso(text: str) -> str:
    """R08/06/05（令和8年6月5日）を 2026-06-05 に。"""
    m = re.search(r"R(\d+)[/.-](\d+)[/.-](\d+)", text or "")
    if not m:
        return ""
    return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def _clean_title(t: str) -> str:
    """末尾の案件番号(例 2026-160092-000-15)を除去して案件名だけにする。"""
    return re.sub(r"\s*\d{4}\s*-?\s*\d{4,6}\s*-?\s*\d{3}\s*-?\s*\d{2}\s*$", "", t).strip() or t


def fetch(headless: bool = True, timeout_ms: int = 30000) -> list[dict]:
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(START_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(2500)

        menu = next((f for f in page.frames if f.query_selector("img[alt='入札公告']")), None)
        if menu is None:
            browser.close()
            raise RuntimeError("e-Aichi メニューが見つかりません（サイト構造変更の可能性）")
        menu.query_selector("img[alt='入札公告']").click()
        page.wait_for_timeout(3500)

        form = next((f for f in page.frames if f.query_selector("select[name=KoujiSyubetu]")), None)
        if form is None:
            browser.close()
            raise RuntimeError("e-Aichi 検索フォームが見つかりません")
        form.select_option("select[name=KoujiSyubetu]", label="電気工事")
        try:
            opts = form.eval_on_selector_all(
                "select[name=ejMaxDisplayRowCount] option", "o=>o.map(x=>x.text.trim())")
            form.select_option("select[name=ejMaxDisplayRowCount]", label=opts[-1])
        except Exception:
            pass
        form.query_selector("input[type=image]").click()  # 検索
        page.wait_for_timeout(4500)

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
                }).filter(x => x.cells.length >= 7 && /^\\d+$/.test(x.cells[0])
                    && x.cells.some(c => c.includes('電気工事')))""",
            )
        except Exception:
            items = []
        if not items:
            continue
        out: list[dict] = []
        for it in items:
            cells = it["cells"]
            rec = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(_COLS)}
            out.append({
                "source": "愛知県(e-Aichi)",
                "external_id": f"AICHI-{rec['no']}-{rec['title'][:20]}",
                "title": _clean_title(rec["title"]),
                "agency": rec["agency"],
                "agency_type": "都道府県",
                "region": "東海",
                "prefecture": PREFECTURE,
                "category": "電気工事",
                "bid_method": rec["review"],
                "announced_date": _r_to_iso(rec["d_koukoku"]),
                "deadline": _r_to_iso(rec["d_shimekiri"]),
                "detail_url": START_URL,
                "spec_status": db.SPEC_AVAILABLE,  # e-Aichiは設計図書を公開（詳細ページ）
                "spec_reason": "",
                "spec_url": "",
                "budget": "",
                "winner": "",
                "win_price": "",
            })
        return out
    return []


def load() -> int:
    db.init_db()
    rows = fetch()
    rows = [r for r in rows if "電気" in r["category"] or "電気" in r["title"]]
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    print(f"愛知県(e-Aichi): {load()} 件の電気工事を取得・投入しました")
