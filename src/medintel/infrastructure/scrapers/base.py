"""Infraestructura compartida de scraping.

Cumple las reglas operativas y legales:
  - Respeta robots.txt por dominio.
  - Rate limit por host (token bucket).
  - User-Agent rotación honesta (UAs reales de browsers actuales).
  - Cache filesystem: no re-fetch del mismo URL en la ventana TTL.
  - Backoff exponencial con jitter en 429/503.
  - Detección de bloqueo (status, captcha keywords) y pausa del job.

NO incluye:
  - Proxy rotation (caro, agregar solo si bloquean).
  - Bypass de captcha agresivo.
  - Suplantación de fingerprint a nivel TLS.
Esos viven en una capa premium opt-in (`scrapers/anti_block.py`, TODO).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

log = structlog.get_logger()

CACHE_DIR = Path(__file__).resolve().parents[4] / "data" / "snapshots"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# UAs de browsers reales (rotar entre ellos; no inventar strings raros).
USER_AGENTS: tuple[str, ...] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
)

# Palabras-clave en respuestas que indican challenge anti-bot.
_BLOCK_HINTS: tuple[str, ...] = (
    "captcha", "cf-chl", "are you a robot", "verifica que eres humano",
    "access denied", "blocked", "challenge-platform",
)


class ScrapingBlocked(Exception):
    """Lanzada cuando detectamos challenge/anti-bot. Pausar el job."""


@dataclass
class RateLimiter:
    """Token bucket simple por host (in-memory). Para distribución, mover a Redis."""
    min_interval_seconds: float = 2.0
    _last: dict[str, float] = field(default_factory=dict)

    async def wait(self, host: str) -> None:
        now = time.monotonic()
        last = self._last.get(host, 0.0)
        delta = now - last
        if delta < self.min_interval_seconds:
            await asyncio.sleep(self.min_interval_seconds - delta + random.uniform(0, 0.5))
        self._last[host] = time.monotonic()


@dataclass
class RobotsCache:
    """Cachea parsers de robots.txt por host."""
    _parsers: dict[str, robotparser.RobotFileParser] = field(default_factory=dict)

    async def can_fetch(self, url: str, user_agent: str, client: httpx.AsyncClient) -> bool:
        host = urlparse(url).netloc
        if host not in self._parsers:
            rp = robotparser.RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            try:
                resp = await client.get(robots_url, timeout=10.0)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])  # permisivo si robots no está
            except httpx.HTTPError:
                rp.parse([])
            self._parsers[host] = rp
        return self._parsers[host].can_fetch(user_agent, url)


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    from_cache: bool
    fetched_at: float = field(default_factory=time.time)


class BaseScraper:
    """Scraper base. Subclases sobreescriben `parse()` y opcionalmente `discover()`."""

    name: str = "base"
    base_url: str = ""
    min_interval_seconds: float = 2.0
    cache_ttl_seconds: int = 60 * 60 * 24 * 7  # 7 días

    def __init__(
        self,
        cache_dir: Path = CACHE_DIR,
        rate_limiter: RateLimiter | None = None,
        robots: RobotsCache | None = None,
        respect_robots: bool = True,
    ) -> None:
        self.cache_dir = cache_dir / self.name
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.rate = rate_limiter or RateLimiter(self.min_interval_seconds)
        self.robots = robots or RobotsCache()
        self.respect_robots = respect_robots

    def _cache_path(self, url: str) -> Path:
        h = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self.cache_dir / f"{h}.json"

    def _load_cache(self, url: str) -> FetchResult | None:
        p = self._cache_path(url)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if time.time() - data["fetched_at"] > self.cache_ttl_seconds:
                return None
            return FetchResult(**data, from_cache=True) if "from_cache" not in data else FetchResult(**data)
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def _save_cache(self, fr: FetchResult) -> None:
        self._cache_path(fr.url).write_text(json.dumps({
            "url": fr.url, "status": fr.status, "html": fr.html,
            "from_cache": False, "fetched_at": fr.fetched_at,
        }))

    @staticmethod
    def _looks_blocked(html: str, status: int) -> bool:
        if status in (403, 429, 503):
            return True
        lower = html[:5000].lower()
        return any(k in lower for k in _BLOCK_HINTS)

    async def fetch(self, url: str, client: httpx.AsyncClient | None = None) -> FetchResult:
        """Fetch con cache, rate limit, robots, retry. Lanza ScrapingBlocked si nos bloquean."""
        cached = self._load_cache(url)
        if cached:
            log.debug("scraper.cache_hit", url=url, scraper=self.name)
            return cached

        own_client = client is None
        client = client or httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            ua = random.choice(USER_AGENTS)
            if self.respect_robots and not await self.robots.can_fetch(url, ua, client):
                log.warning("scraper.robots_disallow", url=url)
                return FetchResult(url=url, status=999, html="", from_cache=False)

            await self.rate.wait(urlparse(url).netloc)
            try:
                async for attempt in AsyncRetrying(
                    stop=stop_after_attempt(3),
                    wait=wait_random_exponential(multiplier=2, max=30),
                    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
                ):
                    with attempt:
                        resp = await client.get(url, headers={"User-Agent": ua, "Accept-Language": "es-419,es;q=0.9,en;q=0.7"})
                        if resp.status_code >= 500:
                            resp.raise_for_status()
                        if self._looks_blocked(resp.text, resp.status_code):
                            raise ScrapingBlocked(f"Blocked at {url} status={resp.status_code}")
                        result = FetchResult(url=url, status=resp.status_code, html=resp.text, from_cache=False)
                        self._save_cache(result)
                        return result
            except RetryError as e:
                log.error("scraper.retry_exhausted", url=url, error=str(e))
                raise
            raise RuntimeError("unreachable")
        finally:
            if own_client:
                await client.aclose()
