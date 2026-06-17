"""
urls.py — build the `msencdata:` URLs that the msitss decoder consumes. PORTABLE.

Two URL forms confirmed from live debugging (Linear NEX-392):

  content + media : msencdata:!<contentid>/<verb>?gid=<n>&width=&height=
                    e.g. msencdata:!1837194/map?gid=742&width=164&height=...
  direct baggage  : msencdata::baggage/<hexid>.xml
                    e.g. msencdata::baggage/2d648e25.xml

There is also a render-layer scheme handled by msencxml (NOT needed for extraction):
  msencxml://content/<id>?...&xslparam=...

TODO (NEX-392/393): the exact <verb> for fetching an article's BODY is unconfirmed.
Candidates seen/likely: "article", "content", "" (bare). The probe (scripts/probe.py)
determines which verb returns body prose vs. bibliography.
"""
from __future__ import annotations

# Verb to request an article's main content. UNCONFIRMED — probe decides.
# Order = try-order; the first that yields body prose wins.
ARTICLE_VERB_CANDIDATES = ("article", "content", "")


def content_url(content_id: int, verb: str = "article") -> str:
    """msencdata:!<id>/<verb> — fetch an article resource."""
    if verb:
        return f"msencdata:!{content_id}/{verb}"
    return f"msencdata:!{content_id}"


def media_url(content_id: int, gid: int, width: int = 0, height: int = 0,
              verb: str = "map") -> str:
    """msencdata:!<id>/<verb>?gid=<n> — fetch a media resource referenced by an article."""
    url = f"msencdata:!{content_id}/{verb}?gid={gid}"
    if width or height:
        url += f"&width={width}&height={height}"
    return url


def baggage_url(hexid: str) -> str:
    """msencdata::baggage/<hexid>.xml — direct baggage fetch."""
    if not hexid.endswith(".xml"):
        hexid = f"{hexid}.xml"
    return f"msencdata::baggage/{hexid}"
