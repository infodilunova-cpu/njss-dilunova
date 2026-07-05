"""GitHub Release から完成済みDB（網羅版）をダウンロードする（Renderビルド用）。

設計（NJSS脱却＝網羅DBのための役割分担）:
  - 重い網羅取得（全47都道府県×電気＋web業種クエリ）は GitHub Actions が
    `update.py --full` で実行し、生成した denki_bid.db を gzip して Release "data-latest" に
    アップロードする（Actions は無料・時間制限ゆるい）。
  - Render のビルドは本スクリプトでその完成DBを「ダウンロードするだけ」＝数秒で高速・
    タイムアウト無し。失敗したら非0終了し、ビルドコマンド側で `|| update.py --fast` に
    フォールバックする（軽量取得で最低限のDBを必ず用意する）。

取得方法（2通り・自動判定）:
  A) 認証あり（private リポ対応）: `GH_TOKEN` と `GH_REPO`(owner/repo) が設定されていれば
     GitHub API 経由でReleaseアセットを認証ダウンロードする。private リポでも取得可能。
     署名付きリダイレクト先(S3等)へは Authorization を渡さない（渡すと 400 で弾かれる）。
  B) 認証なし（public リポ）: `DB_RELEASE_URL` の直リンクを未認証でダウンロードする。

成功条件: ダウンロード＋解凍に成功し、案件数が下限以上であること。
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import urllib.request

import db

# public リポの直リンク（未認証DL）。private の場合は GH_TOKEN + GH_REPO を使う（下記参照）。
DEFAULT_DB_URL = ("https://github.com/syun3032-tech/dgss/"
                  "releases/download/data-latest/denki_bid.db.gz")
DB_URL = os.environ.get("DB_RELEASE_URL", "").strip() or DEFAULT_DB_URL
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()               # 例: infodilunova-cpu/njss-dilunova
GH_TAG = os.environ.get("GH_RELEASE_TAG", "data-latest").strip()
ASSET_NAME = os.environ.get("DB_ASSET_NAME", "denki_bid.db.gz").strip()
MIN_CASES = 500  # これ未満なら不正とみなしフォールバックさせる
GZ_PATH = db.DB_PATH.with_suffix(".db.gz")


class _NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクト時に Authorization ヘッダを落とす。

    GitHub のアセットDLは api.github.com から署名付きの S3 URL へ 302 する。
    urllib は既定で全ヘッダを引き継いでしまい、S3 は Authorization を嫌って 400 を返す。
    リダイレクト先には認証情報を渡さない（署名付きURL自体が認可済み）。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            new.remove_header("Authorization")
        return new


def _download_authenticated(dest) -> None:
    """GitHub API 経由で Release アセットを認証ダウンロード（private 対応）。"""
    api = f"https://api.github.com/repos/{GH_REPO}/releases/tags/{GH_TAG}"
    hdr = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "njss-dilunova-fetch",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    print(f"[fetch_db] 認証取得: {GH_REPO} tag={GH_TAG} asset={ASSET_NAME}")
    with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=60) as res:
        rel = json.load(res)
    asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET_NAME), None)
    if not asset:
        names = [a.get("name") for a in rel.get("assets", [])]
        raise RuntimeError(f"アセット {ASSET_NAME} がReleaseに無い（存在: {names}）")
    # アセット本体は octet-stream で要求 → 署名付きURLへ302 → そこには認証を渡さない
    dl_hdr = dict(hdr, Accept="application/octet-stream")
    opener = urllib.request.build_opener(_NoAuthRedirect)
    req = urllib.request.Request(asset["url"], headers=dl_hdr)
    with opener.open(req, timeout=180) as res, open(dest, "wb") as f:
        shutil.copyfileobj(res, f)


def _download_direct(dest) -> None:
    """public リポの直リンクを未認証ダウンロード。"""
    print(f"[fetch_db] ダウンロード: {DB_URL}")
    req = urllib.request.Request(DB_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=120) as res, open(dest, "wb") as f:
        shutil.copyfileobj(res, f)


def main() -> int:
    try:
        if GH_TOKEN and GH_REPO:
            _download_authenticated(GZ_PATH)
        else:
            _download_direct(GZ_PATH)
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
        print(f"[fetch_db] ダウンロード失敗: {str(e)[:120]} → フォールバックします。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
