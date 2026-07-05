"""GitHub Release から完成済みDB（網羅版）をダウンロードする（Renderビルド用）。

設計（NJSS脱却＝網羅DBのための役割分担）:
  - 重い網羅取得（全47都道府県×電気クエリ＝約1000回のAPI呼び出し）は GitHub Actions が
    `update.py --full` で実行し、生成した denki_bid.db を gzip して Release "data-latest" に
    アップロードする（Actions は無料・時間制限ゆるい）。
  - Render のビルドは本スクリプトでその完成DBを「ダウンロードするだけ」＝数秒で高速・
    タイムアウト無し。失敗したら非0終了し、ビルドコマンド側で `|| update.py --fast` に
    フォールバックする（軽量取得で最低限のDBを必ず用意する）。

成功条件: ダウンロード＋解凍に成功し、案件数が下限以上であること。
"""

from __future__ import annotations

import gzip
import os
import shutil
import sys
import urllib.request

import db

# 公開リポジトリの固定タグ Release（認証不要でダウンロードできる）から完成DBを取得。
# デプロイ先ごとにReleaseの置き場が違うため環境変数 DB_RELEASE_URL で差し替え可能にする。
# 未設定なら既定（電気版デプロイ dgss）を使う。DiluNova独立デプロイでは各自のリポの
# Releaseアセットを指すように Render の Environment で DB_RELEASE_URL を設定する。
DEFAULT_DB_URL = ("https://github.com/syun3032-tech/dgss/"
                  "releases/download/data-latest/denki_bid.db.gz")
DB_URL = os.environ.get("DB_RELEASE_URL", "").strip() or DEFAULT_DB_URL
MIN_CASES = 500  # これ未満なら不正とみなしフォールバックさせる
GZ_PATH = db.DB_PATH.with_suffix(".db.gz")


def main() -> int:
    try:
        print(f"[fetch_db] ダウンロード: {DB_URL}")
        req = urllib.request.Request(DB_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=120) as res, open(GZ_PATH, "wb") as f:
            shutil.copyfileobj(res, f)
        # 解凍して denki_bid.db に展開
        with gzip.open(GZ_PATH, "rb") as gz, open(db.DB_PATH, "wb") as out:
            shutil.copyfileobj(gz, out)
        GZ_PATH.unlink(missing_ok=True)
        n = db.count_cases()
        print(f"[fetch_db] 取得成功: 案件 {n} 件")
        if n < MIN_CASES:
            print(f"[fetch_db] 案件 {n} 件は下限 {MIN_CASES} 未満。フォールバックします。")
            return 1
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[fetch_db] ダウンロード失敗: {str(e)[:100]} → フォールバックします。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
