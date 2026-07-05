"""入札情報公開システム（電子入札コアシステム / CALS-EC）スクレイパー。

PPI(i-ppi.jp)は自治体の工事掲載が極少だが、多くの自治体は JACIC の
「電子入札コアシステム」由来の **入札情報公開システム**（URLに `KF00x/KK00x` を含む）を
使って発注情報・入札結果を公開している。本命＝自治体の電気工事はこちらに在る。

このシステムは共通ソフトのため、base_url を差し替えるだけで多くの自治体に横展開できる。
茨城県インスタンスで実データ取得を検証済み（「電気」で 182 件の電気工事 発注情報）。

操作フロー（実機調査で確認）:
  KF001ShowAction（トップ：調達機関を選択）→「工事」リンク → KK000（工事メニュー）
  →「発注情報の検索」→ KK301（検索フォーム: 工事名 koujimei / 表示件数 A300 / 入札方式 A046）
  → 検索 → 結果テーブル（列は _COLS）。詳細は javascript:doEdit('<id>')。

結果テーブルの列:
  [0]工事名 [1]工事番号 [2]入札方式 [3]種別 [4]工事場所 [5]工事概要
  [6]公開年月日 [7]開札年月日 [8]予定価格 [9]課所名
"""

from __future__ import annotations

import re

import db
from regions import region_of

_COLS = ("title", "koji_no", "bid_method", "category", "place",
         "summary", "announced_date", "deadline", "budget", "agency")

# 既知の自治体インスタンス（base_url, 都道府県, 調達機関の既定名）
# base_url 差し替えで他自治体に展開可能。
INSTANCES: dict[str, dict] = {
    "茨城県": {
        "url": "http://ppi.cals-ibaraki.lg.jp/koukai/do/KF001ShowAction",
        "prefecture": "茨城県",
    },
    # 例: 他自治体を追加する場合
    # "埼玉県": {"url": "https://ebidjk2.ebid2.pref.saitama.lg.jp/koukai/do/KF000ShowAction",
    #            "prefecture": "埼玉県"},
}


def _iso(jp: str) -> str:
    m = re.search(r"(20\d\d)[\/.年](\d{1,2})[\/.月](\d{1,2})", jp or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else (jp or "")


def fetch(
    base_url: str,
    prefecture: str,
    *,
    keyword: str = "電気",
    count: str = "100",
    max_pages: int = 3,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> list[dict]:
    """1自治体の入札情報公開システムから電気工事の発注情報を取得する。

    keyword: 工事名に含む語（既定「電気」）。種別(category)でも後段で電気を確認。
    返り値: db スキーマに整形済みの行 dict リスト。
    """
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(base_url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1500)

        # 「工事」へ → 「発注情報の検索」へ
        page.get_by_text("工事", exact=True).first.click()
        page.wait_for_timeout(2500)
        _click_in_frames(page, "発注情報の検索")
        page.wait_for_timeout(2500)

        form = _frame_with(page, "input[name=koujimei]")
        if form is None:
            browser.close()
            raise RuntimeError("発注情報検索フォームが見つかりません（システム構造変更の可能性）")

        form.fill("input[name=koujimei]", keyword)
        try:
            form.select_option("select[name=A300]", label=count)
        except Exception:
            pass
        form.query_selector("input[value='検索']").click()
        page.wait_for_timeout(3500)

        for _ in range(max_pages):
            # 結果は doEdit リンクを持つフレームに在る（左ナビ等の table と区別）
            page_rows: list[dict] = []
            for fr in page.frames:
                try:
                    page_rows = _parse_rows(fr, prefecture, base_url)
                except Exception:
                    page_rows = []
                if page_rows:
                    break
            if not page_rows:
                break
            rows.extend(page_rows)
            # 次ページ（「次へ」リンク）。無ければ終了。
            moved = _click_in_frames(page, "次へ") or _click_in_frames(page, "»")
            if not moved:
                break
            page.wait_for_timeout(3000)

        browser.close()

    # 重複除去（工事番号ベース）
    seen, uniq = set(), []
    for r in rows:
        key = r["external_id"]
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    return uniq


def _parse_rows(frame, prefecture: str, base_url: str = "") -> list[dict]:
    items = frame.eval_on_selector_all(
        "table tr",
        """els => els.map(r => {
            const a = r.querySelector('a');
            const cells = [...(r.cells||[])].map(c => c.innerText.replace(/\\s+/g,' ').trim());
            const oc = a ? (a.getAttribute('href')||a.getAttribute('onclick')||'') : '';
            return { cells, oc };
        }).filter(x => x.cells.length >= 9 && x.oc.includes('doEdit'))""",
    )
    out: list[dict] = []
    for it in items:
        cells = it["cells"]
        rec = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(_COLS)}
        m = re.search(r"doEdit\('([^']+)'\)", it["oc"])
        ext = m.group(1) if m else rec["koji_no"]
        out.append({
            "source": "入札情報公開",
            "external_id": f"KOUKAI-{prefecture}-{rec['koji_no'] or ext}",
            "title": rec["title"],
            "agency": rec["agency"],
            "agency_type": "地方公共団体",
            "region": region_of(prefecture) or "",
            "prefecture": prefecture,
            "category": rec["category"] or "電気工事",
            "bid_method": rec["bid_method"],
            "announced_date": _iso(rec["announced_date"]),
            "deadline": _iso(rec["deadline"]),
            "detail_url": base_url,  # 元の入札情報公開システム（詳細は doEdit(JS) 遷移）
            "spec_status": db.SPEC_UNKNOWN,  # 設計図書は詳細ページ。次段で判定可能
            "spec_reason": "",
            "spec_url": "",
            "budget": rec["budget"],
            "winner": "",
            "win_price": "",
        })
    return out


def _frame_with(page, selector: str):
    for f in page.frames:
        try:
            if f.query_selector(selector):
                return f
        except Exception:
            pass
    return None


def _click_in_frames(page, text: str) -> bool:
    for f in page.frames:
        try:
            el = f.get_by_text(text, exact=False)
            if el.count():
                el.first.click()
                return True
        except Exception:
            pass
    return False


def load(instance: str = "茨城県", **kwargs) -> int:
    """指定インスタンスから取得して DB に投入し、件数を返す。"""
    conf = INSTANCES[instance]
    db.init_db()
    rows = fetch(conf["url"], conf["prefecture"], **kwargs)
    # 電気工事のみに限定（種別で再フィルタして精度UP）
    rows = [r for r in rows if "電気" in r["category"] or "電気" in r["title"]]
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    import sys
    inst = sys.argv[1] if len(sys.argv) > 1 else "茨城県"
    n = load(inst)
    print(f"{inst}: 入札情報公開システムから {n} 件の電気工事を取得・投入しました")
