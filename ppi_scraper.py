"""PPI（入札情報サービス i-ppi.jp）からの案件取得 — 差し込み口。

クライアントが挙げた統合PPI（JACIC 運営）は、全国の自治体（地方公共団体）の
工事・委託入札を集約した無料サービス。電気工事・地方公共団体・仕様書DLという
要望にそのまま合致するため、本ツールの本命データソースとする。

【現状】
i-ppi.jp の検索画面はフレーム + ASP.NET（ViewState）による動的描画で、
単純な HTTP GET ではフォーム/結果を取得できないことを確認済み。
したがってライブ取得は Playwright でのブラウザ操作が必要（下記 TODO）。

このモジュールは「取得 → 正規化 → db.upsert_cases()」の差し込み口を定義する。
実装が完了するまでは fetch_cases() がサンプルデータにフォールバックするため、
アプリ本体（フィルタUI 等）は今すぐ動かして確認できる。

【ライブ取得の実装方針（TODO）】
  1. playwright で https://www.i-ppi.jp/IPPI/SearchServices/Web/Index.htm を開く
  2. 検索条件（地域/都道府県、工種=電気、入札方式、期間）をフォームに入力
  3. 検索実行 → 結果テーブルをページネーションしながら抽出
  4. 各案件の詳細から仕様書/設計図書リンクの有無を判定（_classify_spec 参照）
  5. _normalize() で db スキーマに整形 → db.upsert_cases()
"""

from __future__ import annotations

import db
from regions import region_of

# i-ppi.jp の工種コード等は実装時にここへ定義する（電気 など）
PPI_SEARCH_URL = "https://www.i-ppi.jp/IPPI/SearchServices/Web/Index.htm"


def _classify_spec(detail: dict) -> tuple[str, str, str]:
    """詳細ページの情報から仕様書の取得可否を判定する。

    Returns: (spec_status, spec_reason_code, spec_url)

    判定の考え方（取れる/取れない＋なぜ取れないか）:
      - 直リンクのPDF/ZIPがある            → available
      - 「電子入札システムにて」等の文言     → unavailable / login_required
      - 「窓口にて交付」「現地」            → unavailable / in_person
      - 「有料」「実費」                    → unavailable / paid
      - 「交付申請」                        → unavailable / request_form
      - 公開期間外                          → unavailable / period_closed
      - リンクも文言も無い                  → unknown
    """
    text = (detail.get("body_text") or "")
    spec_links = detail.get("spec_links") or []

    if spec_links:
        return db.SPEC_AVAILABLE, "", spec_links[0]

    rules = [
        (("電子入札", "ログイン", "システムにて"), "login_required"),
        (("窓口", "現地", "持参", "来庁"), "in_person"),
        (("有料", "実費", "閲覧のみ"), "paid"),
        (("交付申請", "申請書"), "request_form"),
        (("公開期間",), "period_closed"),
    ]
    for keywords, code in rules:
        if any(k in text for k in keywords):
            return db.SPEC_UNAVAILABLE, code, ""
    return db.SPEC_UNKNOWN, "", ""


def _normalize(raw: dict) -> dict:
    """PPI の生データ1件を db スキーマの行 dict に整形する。"""
    pref = raw.get("prefecture", "")
    spec_status, spec_reason, spec_url = _classify_spec(raw)
    return {
        "source": "PPI",
        "external_id": f"PPI-{raw.get('id', '')}",
        "title": raw.get("title", ""),
        "agency": raw.get("agency", ""),
        "agency_type": raw.get("agency_type", ""),
        "region": region_of(pref) or "",
        "prefecture": pref,
        "category": raw.get("category", "電気工事"),
        # PPI は「工事入札公告/経過」由来でいずれも建設工事。区分を明示し、
        # 必要書類・ToDo判定やフィルタが正しく効くようにする。
        "procurement_type": "工事",
        "bid_method": raw.get("bid_method", ""),
        "announced_date": raw.get("announced_date", ""),
        "deadline": raw.get("deadline", ""),
        "detail_url": raw.get("detail_url", ""),
        "spec_status": spec_status,
        "spec_reason": spec_reason,
        "spec_url": spec_url,
        "budget": raw.get("budget", ""),
        "winner": raw.get("winner", ""),
        "win_price": raw.get("win_price", ""),
    }


INDEX_URL = "https://www.i-ppi.jp/IPPI/SearchServices/Web/Index.htm"

# 実機調査で判明したフォーム項目（Search.aspx / 工事入札公告 tab=3）
#   drpTopKikanInf   機関大分類: 国の機関 / 地方公共団体（都道府県）/ 地方公共団体（市区町村）
#   drpKojiDistrict  地域大分類: 北海道/東北/関東/北陸/中部/近畿/中国/四国/九州・沖縄
#   drpKojiPrefecture2 都道府県（地域選択後に postback で populate）
#   drpKojiKbn       工種区分: …/電気設備工事/…
#   drpKojiGyosyu    業種: …/電気工事/…
#   btnSearch        「検索開始」
# 注意: PPI は Index.htm のフレームセット経由でセッション/ViewState が初期化される。
#       Search.aspx へ直リンクすると検索が 0 件になるため、必ず Index 経由で入る。


# 実機調査で確認した結果テーブル(dgrSearchList)の列順:
#   [0]連番 [1]発注機関 [2]工事名 [3]入札方式 [4]工種 [5]公告日 [6]締切(開札)日
# 詳細は __doPostBack('dgrSearchList','$N') で開き、■落札者情報(落札者名/落札価格)を含む。
_RESULT_COLS = ("no", "agency", "title", "bid_method", "category", "announced_date", "deadline")

# 国の地方整備局名などから都道府県/地方を推定するためのヒント
_AGENCY_PREF_HINTS = {
    "北海道開発局": "北海道", "東北地方整備局": "宮城県", "関東地方整備局": "東京都",
    "北陸地方整備局": "新潟県", "中部地方整備局": "愛知県", "近畿地方整備局": "大阪府",
    "中国地方整備局": "広島県", "四国地方整備局": "香川県", "九州地方整備局": "福岡県",
}


def fetch_live(
    *,
    keika: bool = False,
    kikan: str = "地方公共団体（都道府県）",
    district: str | None = None,
    prefecture: str | None = None,
    koji_kbn: str = "電気設備工事",
    count: str = "50件",
    with_winner: bool = False,
    max_detail: int = 20,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> list[dict]:
    """Playwright で i-ppi.jp（統合PPI）を検索し、案件の生 dict を返す。

    実機調査で確認・実装した正しい操作フロー:
      Index.htm → contents フレームで __doPostBack(入口) → Search.aspx の検索フォーム
      → 機関分類/地域→都道府県(連動postback)/工種 を選択 → btnSearch
      → 結果テーブル(dgrSearchList) を既知の列順で抽出

    keika=False: 入札公告等(lbtKojiKokoku) / keika=True: 入札の経過(lbtKojiKeika, 落札者付き)

    ※ 地方公共団体の電気工事掲載は限定的（調査時点で都道府県級は岐阜県のみ）。
       国の機関(国交省 等)は多数掲載あり＝実データで動作確認済み。
    """
    from playwright.sync_api import sync_playwright

    entry = "lbtKojiKeika" if keika else "lbtKojiKokoku"
    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())
        page.goto(INDEX_URL, wait_until="networkidle", timeout=timeout_ms)

        next(f for f in page.frames if f.name == "contents").evaluate(
            f"__doPostBack('{entry}','')"
        )
        page.wait_for_timeout(3000)

        def form():  # postback ごとにフレームが差し替わるため毎回取り直す
            return next(
                (f for f in page.frames if f.query_selector("input[name=btnSearch]")), None
            )

        f = form()
        if f is None:
            browser.close()
            raise RuntimeError("PPI 検索フォームが見つかりません（サイト構造変更の可能性）")

        def pick(name: str, label: str, wait: int = 700):
            nonlocal f
            try:
                f.select_option(f"select[name={name}]", label=label)
                page.wait_for_timeout(wait); f = form()
            except Exception:
                pass

        if kikan:
            pick("drpTopKikanInf", kikan, 2000)
        if district:
            pick("drpKojiDistrict", district, 2000)
        if prefecture:
            pick("drpKojiPrefecture2", prefecture, 800)
        if koji_kbn:
            pick("drpKojiKbn", koji_kbn)
        if count:
            pick("drpCount", count, 300)

        f.query_selector("input[name=btnSearch]").click()
        page.wait_for_timeout(5000)

        rows = _parse_result(page, keika, prefecture)
        if with_winner and rows:
            _enrich_winners(page, rows, max_detail)
        browser.close()
    return rows


def _enrich_winners(page, rows: list[dict], max_detail: int) -> None:
    """各案件の詳細(■落札者情報)を開いて落札者名・落札価格を埋める。

    詳細は __doPostBack('dgrSearchList','$idx') で開き、
    「検索結果一覧画面に戻る」リンクで一覧へ復帰する。
    """
    def grid_frame():
        return next(
            (f for f in page.frames if f.evaluate("typeof __doPostBack==='function'")), None
        )

    for n, rec in enumerate(rows[:max_detail]):
        idx = rec.get("_grid_idx", n)
        tgt = grid_frame()
        if tgt is None:
            break
        try:
            tgt.evaluate(f"__doPostBack('dgrSearchList','${idx}')")
            page.wait_for_timeout(3000)
        except Exception:
            continue
        winner, price = _extract_winner(page)
        rec["winner"], rec["win_price"] = winner, price
        # 一覧へ戻る
        back = None
        for fr in page.frames:
            back = fr.query_selector("a:has-text('検索結果一覧画面に戻る')")
            if back:
                break
        try:
            (back.click() if back else page.go_back())
            page.wait_for_timeout(2500)
        except Exception:
            page.go_back(); page.wait_for_timeout(2500)


def _extract_winner(page) -> tuple[str, str]:
    """詳細ページから落札者名／落札価格の値を取り出す（空ラベル誤取得を除去）。"""
    import re
    labels = ("落札者名", "落札価格", "契約情報", "落札者情報", "契約者名", "契約金額")
    for fr in page.frames:
        if not fr.query_selector("body"):
            continue
        t = fr.inner_text("body")
        if "落札者" not in t:
            continue

        def value_after(label: str) -> str:
            m = re.search(label + r"[\s　:：]*\n?([^\n]{1,40})", t)
            if not m:
                return ""
            v = m.group(1).strip()
            # 次のラベル/見出しを拾ってしまった場合は空扱い（=未落札）
            if not v or v.startswith("■") or any(v.startswith(l) for l in labels):
                return ""
            return v

        return value_after("落札者名"), value_after("落札価格")
    return "", ""


def _infer_prefecture(agency: str, fallback: str | None) -> str:
    """発注機関名から都道府県を推定する（精度向上）。"""
    if fallback:
        return fallback
    from regions import ALL_PREFECTURES
    for pref in ALL_PREFECTURES:
        if pref in agency or pref[:-1] in agency:  # 「東京都」「東京」両対応
            return pref
    for hint, pref in _AGENCY_PREF_HINTS.items():
        if hint in agency:
            return pref
    return ""


def _parse_result(page, keika: bool, prefecture: str | None) -> list[dict]:
    """結果テーブル(dgrSearchList)を既知の列順で正確に抽出する。"""
    raw: list[dict] = []
    for fr in page.frames:
        try:
            items = fr.eval_on_selector_all(
                "table tr",
                """els => els.map((r,i) => ({
                    cells: [...(r.cells||[])].map(c => c.innerText.replace(/\\s+/g,' ').trim())
                })).filter(x => x.cells.length >= 6
                    && /^\\d+$/.test(x.cells[0])
                    && /20\\d\\d[\\/.年]/.test(x.cells.join(' ')))""",
            )
        except Exception:
            items = []
        if not items:
            continue
        for idx, it in enumerate(items):
            cells = it["cells"]
            rec = {k: (cells[i] if i < len(cells) else "") for i, k in enumerate(_RESULT_COLS)}
            agency = rec["agency"]
            grid_idx = int(rec["no"]) - 1 if rec["no"].isdigit() else idx
            raw.append({
                "_grid_idx": grid_idx,
                "id": f"{'keika' if keika else 'kokoku'}-{rec['no']}-{rec['announced_date']}",
                "title": rec["title"],
                "agency": agency,
                "agency_type": "国の機関" if "省" in agency else "地方公共団体",
                "prefecture": _infer_prefecture(agency, prefecture),
                "category": "電気工事",
                "bid_method": rec["bid_method"],
                "announced_date": _iso(rec["announced_date"]),
                "deadline": _iso(rec["deadline"]),
                "detail_url": INDEX_URL,  # 詳細はpostback遷移のため一覧URLを格納
                "body_text": " ".join(cells),
                "spec_links": [],
                # 落札者は詳細ページ(■落札者情報)に在る。一覧段階では空。
                "winner": "",
                "win_price": "",
            })
        break  # 結果フレームは1つ
    return raw


def _iso(jp_date: str) -> str:
    """2026/04/28 や 2026.04.28 を 2026-04-28 に。失敗時は原文。"""
    import re
    m = re.search(r"(20\d\d)[\/.年](\d{1,2})[\/.月](\d{1,2})", jp_date)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return jp_date


def fetch_cases(use_sample: bool = True, **live_kwargs) -> int:
    """案件を取得して DB に投入し、件数を返す。

    use_sample=True: サンプルデータ（UI確認用）。
    use_sample=False: PPI からライブ取得（live_kwargs を fetch_live に渡す）。
    """
    db.init_db()
    if use_sample:
        import seed_data
        return seed_data.seed()

    raw_rows = fetch_live(**live_kwargs)
    rows = [_normalize(r) for r in raw_rows]
    return db.upsert_cases(rows) if rows else 0


if __name__ == "__main__":
    import sys
    if "--live" in sys.argv:
        # 例: 国の機関の電気設備工事 入札公告をライブ取得
        #   python ppi_scraper.py --live
        keika = "--keika" in sys.argv
        kikan = "国の機関" if "--kuni" in sys.argv else "地方公共団体（都道府県）"
        with_winner = "--winner" in sys.argv  # 詳細を開いて落札者(競合)も取得
        n = fetch_cases(use_sample=False, keika=keika, kikan=kikan,
                        with_winner=with_winner, max_detail=20)
        print(f"PPIから {n} 件を取得・投入（keika={keika}, kikan={kikan}, winner={with_winner}）")
    else:
        print(f"{fetch_cases(use_sample=True)} 件を投入しました")
