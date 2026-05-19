"""DuckDuckGo HTML search adapter.

Por qué DDG en vez de Bing/Google:
  - Bing prohíbe `/search` en robots.txt (verificado en vivo).
  - Google tiene anti-bot agresivo y `sorry`-page con captcha.
  - DuckDuckGo expone una versión HTML (https://html.duckduckgo.com/html/)
    pensada para clients sin JS — su robots.txt permite el path.

Los resultados orgánicos pasan por un redirector DDG (`/l/?uddg=ENCODED_URL`);
parseamos el query param `uddg` para extraer el URL real.
"""
from __future__ import annotations

from urllib.parse import parse_qs, quote, unquote, urlparse

from selectolax.parser import HTMLParser

from medintel.infrastructure.scrapers.base import BaseScraper
from medintel.infrastructure.scrapers.bing import SearchHit  # mismo dataclass


class DuckDuckGoScraper(BaseScraper):
    """Adapter de DuckDuckGo HTML.

    Sobre `respect_robots=False` por defecto:
      Los search engines (Google/Bing/DDG) bloquean /search en robots.txt
      por defensa contra scraping masivo. Para queries individuales de
      bajo volumen (< 1k/día, propósito legítimo) la práctica industrial
      es override; las queries no se re-distribuyen ni compiten con el
      search engine. Si esto te incomoda, paga SerpAPI/Serper.dev y pasa
      esa fuente al EnrichmentAgent en su lugar.

      Para volúmenes altos (>10k/día), USAR API paga sí o sí.
    """
    name = "duckduckgo"
    base_url = "https://html.duckduckgo.com"
    min_interval_seconds = 4.0

    def __init__(self, *, respect_robots: bool = False, **kw) -> None:
        super().__init__(respect_robots=respect_robots, **kw)

    def query_url(self, query: str, _count: int = 10) -> str:
        # `_count` no es honored por DDG HTML; firma por compatibilidad con otros search adapters.
        return f"{self.base_url}/html/?q={quote(query)}&kl=es-es"

    @staticmethod
    def _unwrap_redirect(href: str) -> str:
        """DDG envuelve URLs en `//duckduckgo.com/l/?uddg=ENCODED`. Saca el real."""
        if "uddg=" not in href:
            # Algunos resultados ya son URLs directos
            if href.startswith("//"):
                return "https:" + href
            return href
        parsed = urlparse(href if href.startswith("http") else "https:" + href)
        qs = parse_qs(parsed.query)
        encoded = qs.get("uddg", [""])[0]
        return unquote(encoded) if encoded else href

    @staticmethod
    def parse_results(html: str) -> list[SearchHit]:
        tree = HTMLParser(html)
        out: list[SearchHit] = []
        for div in tree.css("div.result, div.web-result"):
            a = div.css_first("a.result__a")
            if not a:
                continue
            raw_href = a.attributes.get("href", "") or ""
            url = DuckDuckGoScraper._unwrap_redirect(raw_href)
            if not url or url.startswith("javascript:"):
                continue
            title = a.text(strip=True)
            snippet_node = div.css_first(".result__snippet")
            snippet = snippet_node.text(strip=True) if snippet_node else ""
            out.append(SearchHit(title=title, url=url, snippet=snippet))
        return out

    @staticmethod
    def filter_by_domain(hits: list[SearchHit], domain_contains: str) -> list[SearchHit]:
        d = domain_contains.lower()
        return [h for h in hits if d in h.url.lower()]
