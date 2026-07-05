"""PPUBC（電子入札コアシステム 入札情報公開・現行SPA版）スクレイパー。

JACIC の最新版「入札情報公開システム」（URLに /PPI/Public/PPUBC を含む）。
堺市など多数の自治体が採用する共通基盤。base_url を差し替えれば横展開できる。
堺市インスタンスで電気工事の実取得を確認済み。

操作フロー（実機確認）:
  <base>/PPUBC00100 を開く → execLink('PPUBC00400','00')（入札公告情報）
  → 工種 selGyoshu=電気工事 → submit「検索」 → 結果テーブル（列 _COLS）

結果テーブルの列:
  [0]契約番号 [1]件名 [2]工事場所 [3]工種(等級) [4]入札方式 [5]状態 [6]詳細
"""

from __future__ import annotations

import re

import db
import procurement

# 既知の PPUBC 自治体インスタンス（base_url, 都道府県, 機関名, 地方）
# efftis は <市>.efftis.jp/PPI/Public/ パターンで多数の自治体をホスト。base追加で拡張可。
INSTANCES: dict[str, dict] = {
    "堺市":   {"base": "https://sakai.efftis.jp/ebid01/PPI/Public",
               "prefecture": "大阪府", "agency": "堺市", "region": "近畿"},
    "東大阪市": {"base": "https://higashiosaka.efftis.jp/PPI/Public",
               "prefecture": "大阪府", "agency": "東大阪市", "region": "近畿"},
    "明石市":  {"base": "https://akashi.efftis.jp/PPI/Public",
               "prefecture": "兵庫県", "agency": "明石市", "region": "近畿"},
    "加古川市": {"base": "https://kakogawa.efftis.jp/PPI/Public",
               "prefecture": "兵庫県", "agency": "加古川市", "region": "近畿"},
    "奈良市":  {"base": "https://nara.efftis.jp/PPI/Public",
               "prefecture": "奈良県", "agency": "奈良市", "region": "近畿"},
}

_COLS = ("no", "title", "place", "category_grade", "bid_method", "status", "detail")


def fetch(base: str, prefecture: str, agency: str, region: str,
          headless: bool = True, timeout_ms: int = 30000) -> list[dict]:
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(f"{base}/PPUBC00100", wait_until="networkidle", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        page.evaluate("execLink('PPUBC00400','00')")  # 入札公告情報
        page.wait_for_timeout(3000)

        form = next((f for f in page.frames
                     if f.query_selector("select[name='kensakuJoken.selGyoshu']")), page.main_frame)
        try:
            form.select_option("select[name='kensakuJoken.selGyoshu']", label="電気工事")
        except Exception:
            browser.close()
            raise RuntimeError("PPUBC 工種セレクトが見つかりません（サイト構造変更の可能性）")
        btn = form.query_selector("input[type=submit][value*='検']")
        btn.click()
        page.wait_for_timeout(4500)

        rows = _parse(page, prefecture, agency, region, base)
        browser.close()
    return rows


def _parse(page, prefecture: str, agency: str, region: str, base: str) -> list[dict]:
    for fr in page.frames:
        try:
            items = fr.eval_on_selector_all(
                "table tr",
                """els => els.map(r => {
                    const cells = [...(r.cells||[])].map(c => c.innerText.replace(/\\s+/g,' ').trim());
                    return { cells };
                }).filter(x => x.cells.length >= 5
                    && x.cells.some(c => c.includes('電気工事'))
                    && (x.cells[1]||'').length > 4)""",
            )
        except Exception:
            items = []
        if not items:
            continue
        out: list[dict] = []
        for it in items:
            cells = it["cells"]
            rec = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(_COLS)}
            cat = re.sub(r"\(.*?\)|（.*?）", "", rec["category_grade"]).strip() or "電気工事"
            out.append({
                "source": f"{agency}(PPUBC)",
                "external_id": f"PPUBC-{agency}-{rec['no']}",
                "title": rec["title"],
                "agency": agency,
                "agency_type": "市区町村",
                "region": region,
                "prefecture": prefecture,
                "category": cat,
                "procurement_type": procurement.infer_procurement_type(
                    rec["title"], rec["bid_method"]),
                "bid_method": rec["bid_method"],
                "announced_date": "",
                "deadline": "",
                "detail_url": f"{base}/PPUBC00100",
                "spec_status": db.SPEC_AVAILABLE,  # PPUBCは設計図書を公開（詳細）
                "spec_reason": "",
                "spec_url": "",
                "budget": "",
                "winner": "",
                "win_price": "",
            })
        return out
    return []


def load(instance: str = "堺市") -> int:
    conf = INSTANCES[instance]
    db.init_db()
    rows = fetch(conf["base"], conf["prefecture"], conf["agency"], conf["region"])
    rows = [r for r in rows if "電気" in r["category"] or "電気" in r["title"]]
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    import sys
    inst = sys.argv[1] if len(sys.argv) > 1 else "堺市"
    print(f"{inst}(PPUBC): {load(inst)} 件の電気工事を取得・投入しました")
