"""Mastodon status HTML to plain text, with the stdlib html.parser.

Mastodon serves status bodies as a small, sanitized HTML subset: <p>, <br>,
<a> (links, @mentions, #hashtags), <span class="invisible|ellipsis"> around
shortened URLs, and on newer servers lists, blockquotes, <pre>/<code> and
inline emphasis. Paragraphs become blank-line separated, <br> a newline,
and a link whose visible text is not its target keeps the target in
parentheses. Mentions and hashtags keep their visible text only.
"""

import re
from html.parser import HTMLParser

BLOCKS = {"p", "div", "blockquote", "pre", "ul", "ol", "h1", "h2", "h3", "h4"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []  # stack of (href, start index into parts, is_tag)
        self.urls = []
        self.quotes = []  # start indices into parts of open blockquotes

    def _break(self, newlines):
        text = "".join(self.parts)
        if not text.strip():
            return
        trailing = len(text) - len(text.rstrip("\n"))
        if trailing < newlines:
            self.parts.append("\n" * (newlines - trailing))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "br":
            self.parts.append("\n")
        elif tag in BLOCKS:
            self._break(2)
            if tag == "blockquote":
                self.quotes.append(len(self.parts))
        elif tag == "li":
            self._break(1)
            self.parts.append("- ")
        elif tag == "a":
            classes = (attrs.get("class") or "").split()
            special = "mention" in classes or "hashtag" in classes
            self.links.append((attrs.get("href") or "", len(self.parts), special))

    def handle_endtag(self, tag):
        if tag == "blockquote" and self.quotes:
            start = self.quotes.pop()
            quoted = "".join(self.parts[start:]).strip("\n")
            del self.parts[start:]
            self.parts.append(
                "\n".join("> " + line if line else ">" for line in quoted.split("\n"))
            )
            self._break(2)
        elif tag in BLOCKS:
            self._break(2)
        elif tag == "a" and self.links:
            href, start, special = self.links.pop()
            shown = "".join(self.parts[start:]).strip()
            if href and not special:
                self.urls.append(href)
                if not shown:
                    self.parts.append(href)
                elif _bare(shown) != _bare(href) and not _bare(href).startswith(
                    _bare(shown).rstrip("…")
                ):
                    self.parts.append(f" ({href})")

    def handle_data(self, data):
        self.parts.append(data)


def _bare(url):
    return re.sub(r"^https?://(www\.)?", "", url.strip()).rstrip("/")


def html_to_text(html):
    """(plain text, [link targets]) for a Mastodon HTML body."""
    if not html:
        return "", []
    parser = _Text()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip(), parser.urls
