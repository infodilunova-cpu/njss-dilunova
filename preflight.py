"""取得前の生存確認（カナリア）。多層防御の一番外側。

重い網羅取得（約1000回のAPI呼び出し・30分）を回す**前**に、外部ソースへ実際に
1回だけ繋いで「取れるか」を確かめる。取れないなら、その場で**理由付きで落とす**。

こうしておくと:
  - GitHub Actions が即座に赤くなる → 気づける（メールが飛ぶ）
  - 30分かけて空っぽのDBを作り、それを本番へ配る、という最悪の流れを断てる

2026-07-07、証明書エラーで官公需APIが全滅したのに、ワークフローは毎日「success」で
終わり続けた。取得が0件でも最後まで走り切ってしまう構造だったため。その再発防止。

使い方:
  python preflight.py        # 全ソースを確認。1つでも駄目なら終了コード1
  python preflight.py --warn # 落とさず警告だけ（様子見したいとき）
"""

from __future__ import annotations

import sys


def check_kkj() -> tuple[bool, str]:
    """官公需情報ポータルAPI（主力ソース）に実接続して1件取れるか。

    証明書・DNS・API仕様変更のいずれで壊れてもここで捕まる。
    """
    try:
        import kkj_scraper
        rows = kkj_scraper.fetch(query="電気工事", category="2", count=3, timeout=30)
    except Exception as e:  # noqa: BLE001 — 理由を人に見せるのが仕事
        return False, f"{type(e).__name__}: {str(e)[:180]}"
    if not rows:
        return False, "接続はできたが0件。検索条件かAPI仕様が変わった可能性"
    return True, f"{len(rows)}件取得（最新公告 {max(r['announced_date'] for r in rows)}）"


CHECKS = (("官公需API(kkj.go.jp)", check_kkj),)


def main(argv: list[str]) -> int:
    warn_only = "--warn" in argv
    ng = 0
    for name, fn in CHECKS:
        ok, detail = fn()
        print(f"[事前確認] {'OK  ' if ok else 'NG  '} {name} — {detail}")
        if not ok:
            ng += 1
    if ng and not warn_only:
        print(f"[事前確認] {ng}件のソースに接続できません。重い取得を始めずに中止します。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
