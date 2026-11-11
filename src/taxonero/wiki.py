"""Wikipedia / Wikispecies content for the side panel of the dashboard.

Wikipedia forbids display in an iframe (frame-ancestors 'none'): the text is fetched through the API
(TextExtracts), cleaned with a whitelist of tags without attributes, and displayed
directly in the page.
"""

import re
from html import escape
from html.parser import HTMLParser
from urllib.parse import unquote, urlparse

from .taxonomy import Taxonomy

ALLOWED = {"p", "b", "i", "em", "strong", "ul", "ol", "li", "h2", "h3", "h4", "br", "dfn", "sup", "sub",
           "abbr", "span", "small", "dl", "dt", "dd", "blockquote"}
VOID = {"br"}
# Sections of no interest in a quick-reading panel.
SKIP_SECTIONS = re.compile(r"^(notes?|références|references|bibliographie|liens externes|external links|voir aussi|"
                           r"see also|annexes|articles connexes|further reading|einzelnachweise|weblinks|literatur|"
                           r"primary references|links)\b", re.I)


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skipping = False        # in an ignored section (references, external links...)
        self.heading: str | None = None   # heading tag in progress (h2/h3/h4), its text buffered
        self.heading_text: list[str] = []
        self.drop = 0                # in <script>/<style>: content ignored

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.drop += 1
        elif tag in ("h2", "h3", "h4"):
            self.heading, self.heading_text = tag, []
        elif not self.skipping and self.heading is None and tag in ALLOWED:
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.drop = max(0, self.drop - 1)
        elif tag == self.heading:
            title = escape("".join(self.heading_text).strip())
            self.heading = None
            if tag == "h2":  # a new section starts: decide whether to keep it
                self.skipping = bool(SKIP_SECTIONS.match(title))
            if not self.skipping and title:
                self.out.append(f"<{tag}>{title}</{tag}>")
        elif not self.skipping and self.heading is None and tag in ALLOWED and tag not in VOID:
            self.out.append(f"</{tag}>")

    def handle_data(self, data):
        if self.drop:
            return
        if self.heading is not None:
            self.heading_text.append(data)
        elif not self.skipping:
            self.out.append(escape(data))


def sanitize(html: str, max_chars: int = 20000) -> str:
    p = _Sanitizer()
    p.feed(html)
    text = "".join(p.out)
    text = re.sub(r"<p>\s*</p>", "", text)
    if len(text) > max_chars:
        cut = text.rfind("</p>", 0, max_chars)
        text = text[: cut + 4 if cut > 0 else max_chars]
    return text


def _title(url: str) -> tuple[str, str]:
    u = urlparse(url)
    return f"{u.scheme}://{u.netloc}", unquote(u.path.rsplit("/wiki/", 1)[-1])


def article(tx: Taxonomy, url: str) -> dict:
    """{'title', 'html', 'thumbnail', 'url'} for a Wikipedia or Wikispecies article URL."""
    base, title = _title(url)
    data = tx.get(f"{base}/w/api.php", {"action": "query", "prop": "extracts|pageimages", "titles": title,
                                         "redirects": 1, "pithumbsize": 480, "format": "json"}, ttl=7 * 86400)
    page = next(iter(data.get("query", {}).get("pages", {}).values()), {})
    return {"title": page.get("title", title.replace("_", " ")), "html": sanitize(page.get("extract", "")),
            "thumbnail": (page.get("thumbnail") or {}).get("source"), "url": url}
