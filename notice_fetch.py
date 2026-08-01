"""公告ページ（HTML）から本文テキストと様式・資料リンクをコードで読み取る。

「様式・フォーマット・元ネタはAIに推測させず、コードで直接読みに行く」ための層。
  - detail_url がHTMLページの場合の本文抽出（従来はPDF直リンクしか読めなかった）
  - ページ内の配布ファイル（様式Word/Excel・公告/仕様書PDF・ZIP）のリンク収集

依存は標準ライブラリのみ。失敗しても例外を投げず空を返す（AI側は従来動作に落ちる）。
"""

from __future__ import annotations

import html
import html.parser
import ipaddress
import re
import urllib.parse
import urllib.request

_TIMEOUT = 20
_MAX_BYTES = 3 * 1024 * 1024  # 3MB（公告ページとして十分・巨大ページは切る）

# 様式（記入して提出するファイル）とみなす拡張子
_FORM_EXTS = (".doc", ".docx", ".xls", ".xlsx", ".rtf", ".jtd", ".jtdc")
_ARCHIVE_EXTS = (".zip", ".lzh")
# ラベルに含まれていたら様式扱いにする語
_FORM_WORDS = ("様式", "申請書", "入札書", "委任状", "誓約書", "調書", "参加表明",
               "届出書", "見積書", "質問書", "提出書")
# 資料（読む書類）のラベル語
_DOC_WORDS = ("公告", "説明書", "仕様書", "図面", "案内", "要領", "質問回答", "公示")


class _Page(html.parser.HTMLParser):
    """本文テキストとリンク（href＋アンカー文字列）を集める最小パーサ。"""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []  # (href, label)
        self._skip = 0          # script/style の中は無視
        self._href: str | None = None
        self._label_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._label_parts = []

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._href:
            label = re.sub(r"\s+", " ", "".join(self._label_parts)).strip()
            self.links.append((self._href, label))
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._href is not None:
            self._label_parts.append(data)
        s = data.strip()
        if s:
            self.text_parts.append(s)


def _safe_url(url: str) -> bool:
    """https のみ・IP直指定は拒否（SSRF/内部アドレス誤アクセス防止）。"""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not p.hostname:
            return False
        try:
            ipaddress.ip_address(p.hostname)
            return False  # IP直指定は読まない
        except ValueError:
            return True   # ホスト名ならOK
    except Exception:  # noqa: BLE001
        return False


def _decode(data: bytes, content_type: str) -> str:
    """charset を Content-Type → meta → UTF-8/CP932 の順で推定してデコード。"""
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    encs = [m.group(1)] if m else []
    head = data[:2048].decode("ascii", "ignore")
    m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    if m2:
        encs.append(m2.group(1))
    encs += ["utf-8", "cp932", "euc-jp"]
    for enc in encs:
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "ignore")


def _classify(url: str, label: str) -> str | None:
    """リンクを form(様式)/doc(資料)/None(対象外) に分類する。"""
    path = urllib.parse.urlparse(url).path.lower()
    is_form_ext = path.endswith(_FORM_EXTS)
    is_pdf = path.endswith(".pdf")
    is_zip = path.endswith(_ARCHIVE_EXTS)
    has_form_word = any(w in label for w in _FORM_WORDS)
    if is_form_ext or (is_zip and has_form_word) or (is_pdf and has_form_word):
        return "form"
    if is_pdf or is_zip or any(w in label for w in _DOC_WORDS):
        return "doc" if (is_pdf or is_zip) else None
    return None


def fetch(url: str) -> dict:
    """公告ページを読み、{"text": 本文, "links": [{label,url,kind}]} を返す。

    HTML以外（PDF等）や取得失敗時は {"text": "", "links": []}。
    """
    empty = {"text": "", "links": []}
    if not _safe_url(url):
        return empty
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as res:
            ctype = res.headers.get("Content-Type", "")
            if "html" not in ctype.lower():
                return empty
            data = res.read(_MAX_BYTES)
    except Exception:  # noqa: BLE001
        return empty

    page = _Page()
    try:
        page.feed(_decode(data, ctype))
    except Exception:  # noqa: BLE001
        return empty

    links: list[dict] = []
    seen: set[str] = set()
    for href, label in page.links:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        absu = urllib.parse.urljoin(url, href)
        if not _safe_url(absu) or absu in seen:
            continue
        kind = _classify(absu, label)
        if not kind:
            continue
        seen.add(absu)
        name = label or urllib.parse.unquote(absu.rsplit("/", 1)[-1])
        links.append({"label": html.unescape(name)[:80], "url": absu, "kind": kind})
        if len(links) >= 30:
            break
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(page.text_parts))
    return {"text": text, "links": links}
