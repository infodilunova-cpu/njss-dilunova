"""NJSS「発注機関を探す」全機関リストの取得（ローカル・手動ログイン併用）。

目的: NJSSが持つ全9,044機関を agencies（監視機関DB）に揃える。
既存の research/njss_dokuho_agencies.csv（6,885機関・公式URL解決済み）に
「足りない機関」を追記する形で research/njss_all_organizations.csv を作る。

使い方（ローカル・1回きりの棚卸し）:
  python3 njss_org_scraper.py
    → Chromeウィンドウが開くので、NJSSに普段どおりログインする（2段階認証もOK）。
      ログインを検知したら自動で一覧を巡回し始める（12機関/ページ×約754ページ、
      1.3秒間隔≒20分。チェックポイントで中断再開可）。

設計・配慮:
  - 対象は「発注機関の名前・所在地・件数」という公開ディレクトリ情報のみ。
    案件データ（公告本文等）は取得しない。
  - robots.txt が拒否するのは sort付き検索URLのみ → sortパラメータは使わない。
  - 1.3秒/ページの丁寧なレートで、通常の閲覧と同程度の負荷に抑える。
  - ログインセッション(.njss_state.json)はローカルのみ（gitignore）。
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
STATE = BASE / ".njss_state.json"          # ログインセッション（gitignore済）
CHECKPOINT = BASE / ".njss_org_pages.jsonl"  # ページ単位の巡回チェックポイント
OUT_CSV = BASE / "research" / "njss_all_organizations.csv"
EXISTING_CSV = BASE / "research" / "njss_dokuho_agencies.csv"

LIST_URL = "https://www2.njss.info/organizations/search?page={page}"
PER_PAGE = 100  # ログイン版は100機関/ページ（実測）
INTERVAL_SEC = 1.3


def _parse_orgs(html: str) -> list[dict]:
    """一覧ページHTML（ログイン版）から機関カードを抽出する。

    カード構造（実HTML確認済み）:
      <h2 class="SearchItem__Title"><a href="/organizations/proc/<id>">機関名</a></h2>
      <span class="SearchItem__Address">東京都</span>
      … 受付中 N件 … 登録案件数 <span>N</span> 件 | 入札結果数 <a>N</a>件
    """
    heads = [(m.start(), m.group(1), m.group(2)) for m in re.finditer(
        r'<h2 class="SearchItem__Title"[^>]*>\s*<a href="/organizations/proc/(\d+)"'
        r'[^>]*>(?:<!--\[-->)?\s*([^<>]{2,80}?)\s*(?:<!--\]-->)?</a>', html)]
    out: list[dict] = []
    for i, (pos, oid, name) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(html)
        seg = html[pos:end]
        text = re.sub(r'<[^>]+>', ' ', seg)  # タグを落として件数ラベルを拾いやすく
        m = re.search(r'class="SearchItem__Address"[^>]*>([^<]{1,20})<', seg)
        pref = m.group(1).strip() if m else ""
        m = re.search(r'(国・省庁|地方公共団体|外郭団体等|独立行政法人|国立大学|その他)', text)
        cat = m.group(1) if m else ""

        def num(label: str) -> int:
            mm = re.search(label + r'\s*([0-9,]+)\s*件', text)
            return int(mm.group(1).replace(",", "")) if mm else 0

        out.append({
            "njss_id": oid,
            "name": name.strip(),
            "prefecture": pref,
            "category": cat,
            "opening": num("受付中"),
            "registered": num("登録案件数"),
            "results": num("入札結果数"),
        })
    return out


def crawl() -> None:
    from playwright.sync_api import sync_playwright

    done_pages: set[int] = set()
    if CHECKPOINT.exists():
        for line in CHECKPOINT.read_text().splitlines():
            try:
                done_pages.add(json.loads(line)["page"])
            except (ValueError, KeyError):
                pass
        print(f"[resume] 既取得 {len(done_pages)} ページ分を再利用")

    with sync_playwright() as p:
        # 永続プロファイル: プロセスを止めてもログインセッションがディスクに残る
        # （ユーザーに何度もログインさせない）。プロファイルはローカルのみ(gitignore)。
        ctx = p.chromium.launch_persistent_context(
            str(BASE / ".njss_profile"), channel="chrome", headless=False,
            locale="ja-JP", args=["--window-size=1200,900"])
        browser = ctx
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # --- ログイン画面を開いて「ユーザーがログインし終わる」まで静かに待つ ---
        # ウィンドウは一切遷移させない（ユーザーが操作している画面を邪魔しない）。
        # ログイン済みかの確認は cookie の変化と現在URLだけで行い、30秒ごとに
        # 裏で軽くチェックする。最大30分待つ。
        page.goto("https://www2.njss.info/users/login", timeout=60000,
                  wait_until="domcontentloaded")
        print("\n★ このウィンドウでNJSSにログインしてください。")
        print("  ログイン完了を自動で検知したら巡回を始めます（最大30分待ちます）…")
        deadline = time.time() + 30 * 60
        logged_in = False
        while time.time() < deadline:
            time.sleep(10)
            try:
                url = page.url
                # ログイン後はマイページ等へ遷移する＝loginページから離れたら候補
                if "/users/login" in url:
                    continue
                # 本判定: 別タブで一覧2ページ目がLimitedでないこと
                page2 = ctx.new_page()
                page2.goto(LIST_URL.format(page=2), timeout=60000,
                           wait_until="domcontentloaded")
                page2.wait_for_timeout(2000)
                ok = "LimitedOrganizationSearch" not in page2.content()
                page2.close()
                if ok:
                    logged_in = True
                    break
            except Exception:  # noqa: BLE001
                pass
        if not logged_in:
            print("ログインを検知できませんでした。もう一度実行してください。")
            browser.close()
            sys.exit(1)
        print("[login] ログインを確認。巡回を開始します。", flush=True)

        # --- 総ページ数を得る（全9,044件 ÷ 12/ページ ≒ 754。実値は1ページ目から） ---
        page.goto(LIST_URL.format(page=1), timeout=60000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        m = re.search(r'全\s*([0-9,]+)\s*件', page.content())
        total = int(m.group(1).replace(",", "")) if m else 9044
        last_page = (total + PER_PAGE - 1) // PER_PAGE
        print(f"[plan] 全{total:,}機関 / {last_page}ページ")

        with open(CHECKPOINT, "a") as ck:
            for pg_no in range(1, last_page + 1):
                if pg_no in done_pages:
                    continue
                try:
                    page.goto(LIST_URL.format(page=pg_no), timeout=60000,
                              wait_until="domcontentloaded")
                    page.wait_for_timeout(int(INTERVAL_SEC * 1000))
                    html = page.content()
                    if "LimitedOrganizationSearch" in html:
                        print(f"[warn] p{pg_no}: セッション切れ。再ログインが必要です。")
                        break
                    orgs = _parse_orgs(html)
                    if not orgs:
                        # 範囲外ページ（=全件取得済み）か構造変化。HTMLを保存して終了。
                        dbg = BASE / ".njss_debug_empty.html"
                        dbg.write_text(html)
                        print(f"[warn] p{pg_no}: 抽出0件 → 終端とみなし終了", flush=True)
                        break
                    ck.write(json.dumps({"page": pg_no, "orgs": orgs},
                                        ensure_ascii=False) + "\n")
                    ck.flush()
                    if pg_no % 25 == 0 or pg_no == 1:
                        print(f"  p{pg_no}/{last_page}: {len(orgs)}機関 "
                              f"(例: {orgs[0]['name'] if orgs else '—'})", flush=True)
                except Exception as e:  # noqa: BLE001 — 1ページ失敗はスキップして継続
                    print(f"[skip] p{pg_no}: {str(e)[:60]}")
        browser.close()
    build_csv()


def build_csv() -> None:
    """チェックポイント → 全機関CSV。既存CSVに無い機関が「新規追加分」。"""
    orgs: dict[str, dict] = {}
    for line in CHECKPOINT.read_text().splitlines():
        try:
            for o in json.loads(line)["orgs"]:
                orgs[o["njss_id"]] = o
        except (ValueError, KeyError):
            continue
    existing: set[str] = set()
    if EXISTING_CSV.exists():
        with open(EXISTING_CSV, newline="", encoding="utf-8-sig") as f:
            existing = {r["name"] for r in csv.DictReader(f)}
    OUT_CSV.parent.mkdir(exist_ok=True)
    new_count = 0
    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["name", "njss_count", "top_url", "domain", "platform_n",
                    "bid_url", "sample_url", "fetched_at", "prefecture",
                    "category", "is_new"])
        for o in sorted(orgs.values(), key=lambda x: -x["registered"]):
            is_new = o["name"] not in existing
            new_count += int(is_new)
            w.writerow([o["name"], o["registered"], "", "", 0, "",
                        f"https://www2.njss.info/organizations/proc/{o['njss_id']}",
                        time.strftime("%Y-%m-%d"), o["prefecture"], o["category"],
                        "1" if is_new else "0"])
    print(f"[done] 全{len(orgs):,}機関 → {OUT_CSV}（既存CSVに無い新規 {new_count:,}機関）")


if __name__ == "__main__":
    if "--csv-only" in sys.argv:
        build_csv()
    else:
        crawl()
