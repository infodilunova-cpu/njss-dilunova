"""地方区分 ⇔ 都道府県 マッピング（NJSS の案件絞り込みを踏襲）。

NJSS は「地方（8区分）」→「都道府県」の2段で案件を絞り込む。
本ツールも同じ体験にするため、ここに正準のマッピングを置く。

- REGIONS:        地方区分名 → その地方に属する都道府県リスト
- PREF_TO_REGION: 都道府県名 → 地方区分名（逆引き）
- ALL_PREFECTURES: 47都道府県（北→南の順）
"""

from __future__ import annotations

# 8地方区分（NJSS と同じ括り）
REGIONS: dict[str, list[str]] = {
    "北海道・東北": ["北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県"],
    "関東": ["茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県"],
    "甲信越・北陸": ["新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県"],
    "東海": ["岐阜県", "静岡県", "愛知県", "三重県"],
    "近畿": ["滋賀県", "京都府", "大阪府", "兵庫県", "奈良県", "和歌山県"],
    "中国": ["鳥取県", "島根県", "岡山県", "広島県", "山口県"],
    "四国": ["徳島県", "香川県", "愛媛県", "高知県"],
    "九州・沖縄": ["福岡県", "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県"],
}

# 都道府県 → 地方（逆引き）
PREF_TO_REGION: dict[str, str] = {
    pref: region for region, prefs in REGIONS.items() for pref in prefs
}

# 47都道府県をフラットに（北→南）
ALL_PREFECTURES: list[str] = [pref for prefs in REGIONS.values() for pref in prefs]


def region_of(prefecture: str) -> str | None:
    """都道府県名から所属地方を返す。不明なら None。"""
    return PREF_TO_REGION.get(prefecture)


def prefectures_in(region: str) -> list[str]:
    """地方区分に属する都道府県リストを返す。不明なら空リスト。"""
    return REGIONS.get(region, [])
