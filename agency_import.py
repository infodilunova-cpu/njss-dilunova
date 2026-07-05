"""監視対象の発注機関リストを Google スプレッドシートから取り込む。

クライアント提供のスプレッドシート（全国の発注機関 × 公式入札ページ × 使用プラットフォーム）
を CSV エクスポートで取得し、agencies テーブルへ保存する。
これが「全国で何を監視しているか」の土台になる。

※ シートは公開（リンクを知っている人が閲覧可）である必要がある。
"""

from __future__ import annotations

import csv
import io
import urllib.request

import db

SHEET_ID = "1fjHP4xaH77FbSdydB6HbCX2W8BcGCkQza5gerzUl-8g"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"


def platform_of(domain: str) -> str:
    """ドメインから使用している入札システム（プラットフォーム）を推定する。"""
    d = (domain or "").lower()
    table = [
        ("i-ppi.jp", "統合PPI"),
        ("efftis.jp", "efftis入札情報公開"),
        ("e-procurement.metro.tokyo", "東京都電子調達"),
        ("e-aichi", "e-Aichi"),
        ("e-harp", "e-harp(北海道)"),
        ("p-portal.go.jp", "政府電子調達(GEPS)"),
        ("kintoneapp", "kintone公開"),
        ("kumamoto-idc", "PPI_P"),
        ("dennyu.pref.kagawa", "PPI_P"),
        ("keiyaku.city.hiroshima", "PPI_P"),
        ("cals", "電子入札コアシステム"),
    ]
    for key, name in table:
        if key in d:
            return name
    return "個別/不明"


def _fetch_csv(retries: int = 3) -> str:
    """スプレッドシートCSVを取得。DNS瞬断/タイムアウト等の一過性失敗は retries 回再試行。

    毎日の自動更新がたまたまネットワーク不調と重なると監視機関リストが0件になり、
    応募ガイドのポータル判定が弱くなる。1回の瞬断で落とさないための保険。
    """
    import time
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": "Mozilla/5.0"})
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001 — 一過性ネットワーク失敗を再試行
            last_err = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last_err if last_err else RuntimeError("CSV取得に失敗")


def fetch_rows() -> list[dict]:
    """スプレッドシートCSVを取得して agencies 行 dict に整形する。"""
    text = _fetch_csv()
    reader = csv.DictReader(io.StringIO(text))
    rows: list[dict] = []
    for r in reader:
        name = (r.get("発注機関") or "").strip()
        if not name:
            continue
        def num(v):
            v = (v or "").strip().replace(",", "")
            return int(v) if v.isdigit() else 0
        rows.append({
            "name": name,
            "njss_count": num(r.get("NJSS案件数")),
            "top_url": (r.get("公式トップURL") or "").strip(),
            "domain": (r.get("ドメイン") or "").strip(),
            "platform_n": num(r.get("共通基盤_機関数")),
            "bid_url": (r.get("NJSS公示リンク先") or "").strip(),
            "sample_url": (r.get("NJSS案件URL例") or "").strip(),
            "fetched_at": (r.get("取得日時") or "").strip(),
        })
    return rows


def load() -> int:
    """取り込んで agencies テーブルに保存。件数を返す。"""
    db.init_db()
    rows = fetch_rows()
    return db.upsert_agencies(rows) if rows else 0


if __name__ == "__main__":
    n = load()
    print(f"監視対象の発注機関 {n} 件を取り込みました")
