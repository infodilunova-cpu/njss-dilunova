"""データベース強化リサーチ：全国1000発注機関を「使用プラットフォーム」で分類し、
スクレイパー増設の優先順位（カバレッジ／難易度／優先度）を CSV 出力する。

出力:
  research/platform_roadmap.csv … プラットフォーム別の強化ロードマップ
  research/agencies_by_domain.csv … ドメイン別の機関数・案件数（精査用）

実行: python research_sources.py
"""

from __future__ import annotations

import collections
import csv
import os

import agency_import


# ドメイン → プラットフォーム（精緻版）。上から順にマッチ。
def classify(domain: str) -> str:
    d = (domain or "").lower()
    rules = [
        ("i-ppi.jp", "統合PPI"),
        ("efftis.jp", "efftis入札情報公開"),
        ("e-procurement.metro.tokyo", "東京都電子調達"),
        ("e-tokyo.lg.jp", "東京電子自治体共同運営"),
        ("e-aichi", "e-Aichi"),
        ("e-harp", "e-harp(北海道)"),
        ("p-portal.go.jp", "政府電子調達(GEPS)"),
        ("e-bisc.go.jp", "e-bisc(独法調達)"),
        ("bit.courts.go.jp", "裁判所BIT"),
        ("mod.go.jp", "防衛省調達"),
        ("kintoneapp", "kintone公開"),
        ("kumamoto-idc", "PPI_P"),
        ("dennyu.pref.kagawa", "PPI_P"),
        ("keiyaku.city.hiroshima", "PPI_P"),
        ("kagoshima-nyusatsu", "鹿児島共同"),
        ("pref.saitama.lg.jp", "埼玉電子入札"),
        ("cals", "電子入札コアシステム"),
    ]
    for key, name in rules:
        if key in d:
            return name
    return "個別/不明"


# プラットフォームごとの「現状・難易度・優先度・メモ」（DB強化の判断材料）
META = {
    "統合PPI": ("実装済", "—", "—", "ppi_scraper.py。国の機関を全国取得済み"),
    "efftis入札情報公開": ("実装済", "—", "—", "kyoto_scraper.py。京都府の自治体を取得済み"),
    "電子入札コアシステム": ("実装済", "—", "—", "koukai_scraper.py(茨城で実証)。cals系の多くに横展開可"),
    "政府電子調達(GEPS)": ("未対応", "中〜高", "★★★", "国の調達ポータル。中央省庁を一括カバー"),
    "e-Aichi": ("未対応", "中", "★★★", "愛知県＋県内市町村を一括カバー(41機関)"),
    "PPI_P": ("未対応", "中", "★★★", "PPI系。香川/熊本/広島市。efftis流用が効く可能性"),
    "防衛省調達": ("未対応", "高", "★", "PDF掲載が多くコスパ低。電気工事は基地施設系"),
    "東京電子自治体共同運営": ("未対応", "中", "★★", "東京都内の市区町村を一括カバー(37機関)"),
    "裁判所BIT": ("未対応", "中", "★", "物品中心。電気工事は限定的"),
    "埼玉電子入札": ("未対応", "中", "★★", "コアシステム系の可能性。流用で取れるか要確認"),
    "e-harp(北海道)": ("未対応", "中", "★★", "北海道＋道内自治体"),
    "e-bisc(独法調達)": ("未対応", "中", "★", "独立行政法人系"),
    "東京都電子調達": ("未対応", "中", "★★", "東京都本体(案件530で最大単独機関)"),
    "鹿児島共同": ("未対応", "中", "★", "鹿児島県内共同"),
    "kintone公開": ("未対応", "低", "★★", "kintone公開ビュー。取得は容易"),
    "個別/不明": ("非推奨", "高", "—", "機関ごと個別PDF。コスパ低。NJSS直結が現実的"),
}


def main() -> None:
    rows = agency_import.fetch_rows()
    total_cases = sum(r["njss_count"] for r in rows)
    os.makedirs("research", exist_ok=True)

    # プラットフォーム別集計
    plat = collections.defaultdict(lambda: {"n": 0, "cases": 0, "ex": ""})
    for r in rows:
        p = classify(r["domain"])
        plat[p]["n"] += 1
        plat[p]["cases"] += r["njss_count"]
        if not plat[p]["ex"]:
            plat[p]["ex"] = r["bid_url"]

    path1 = "research/platform_roadmap.csv"
    with open(path1, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["プラットフォーム", "機関数", "NJSS案件数", "カバー率%",
                    "現状", "推定難易度", "優先度", "メモ", "例URL"])
        # NJSS直接（最上段：全件カバーの最終手段）
        w.writerow(["NJSS（直接スクレイプ）", len(rows), total_cases, "100.0",
                    "要サブスク/ログイン", "中", "★★★",
                    "全ソースを集約済み＝最大カバー。NJSS無双君の資産流用可。利用規約に留意",
                    "https://www2.njss.info/"])
        for p, v in sorted(plat.items(), key=lambda x: -x[1]["cases"]):
            st, diff, pri, memo = META.get(p, ("未対応", "?", "?", ""))
            w.writerow([p, v["n"], v["cases"], f"{v['cases']/total_cases*100:.1f}",
                        st, diff, pri, memo, v["ex"]])

    # ドメイン別（精査用）
    dom = collections.defaultdict(lambda: {"n": 0, "cases": 0, "ex": ""})
    for r in rows:
        dom[r["domain"]]["n"] += 1
        dom[r["domain"]]["cases"] += r["njss_count"]
        if not dom[r["domain"]]["ex"]:
            dom[r["domain"]]["ex"] = r["bid_url"]
    path2 = "research/agencies_by_domain.csv"
    with open(path2, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ドメイン", "プラットフォーム", "機関数", "NJSS案件数", "例URL"])
        for d, v in sorted(dom.items(), key=lambda x: -x[1]["cases"]):
            w.writerow([d, classify(d), v["n"], v["cases"], v["ex"]])

    print(f"総機関 {len(rows)} / 総案件 {total_cases}")
    print(f"出力: {path1}")
    print(f"出力: {path2}")
    # サマリを標準出力にも
    print("\n=== プラットフォーム別ロードマップ（案件数順）===")
    for p, v in sorted(plat.items(), key=lambda x: -x[1]["cases"]):
        st, diff, pri, _ = META.get(p, ("", "", "", ""))
        print(f" {p:22} 機関{v['n']:>4} 案件{v['cases']:>6} "
              f"({v['cases']/total_cases*100:4.1f}%) {st} 難:{diff} 優:{pri}")


if __name__ == "__main__":
    main()
