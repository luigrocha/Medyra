"""Bing search adapter como fallback de discovery.

Por qué Bing en vez de Google:
  - Google tiene anti-bot agresivo (sorry-page con captcha).
  - Bing es más permisivo para queries HTML.
  - Resultados orgánicos en `<li class="b_algo"><h2><a>`.

Si Bing tampoco rinde, considerar DuckDuckGo (HTML version) o SerpAPI (pago).
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from selectolax.parser import HTMLParser

from medintel.infrastructure.scrapers.base import BaseScraper


@dataclass
class SearchHit:
    title: str
    url: str
    snippet: str


class BingSearchScraper(BaseScraper):
    name = "bing"
    base_url = "https://www.bing.com"
    min_interval_seconds = 3.0  # Bing tolera ~1 query / 2-3s

    def query_url(self, query: str, count: int = 10) -> str:
        return f"{self.base_url}/search?q={quote(query)}&count={count}&setlang=es"

    @staticmethod
    def parse_results(html: str) -> list[SearchHit]:
        tree = HTMLParser(html)
        out: list[SearchHit] = []
        for li in tree.css("li.b_algo"):
            a = li.css_first("h2 > a")
            if not a:
                continue
            url = a.attributes.get("href", "")
            if not url:
                continue
            title = a.text(strip=True)
            snippet_node = li.css_first(".b_caption p, .b_lineclamp4, .b_lineclamp2")
            snippet = snippet_node.text(strip=True) if snippet_node else ""
            out.append(SearchHit(title=title, url=url, snippet=snippet))
        return out

    @staticmethod
    def filter_by_domain(hits: list[SearchHit], domain_contains: str) -> list[SearchHit]:
        d = domain_contains.lower()
        return [h for h in hits if d in h.url.lower()]
