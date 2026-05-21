"""Tests del EnrichmentAgent contra fixtures.

Doctoralia opera realmente en ES, MX, CO, PE, CL — usamos MX para tests del path
Doctoralia. Para países sin Doctoralia (CR, PA, EC) el agente va Bing-first
con extractor genérico; ese path se testea con un fixture HTML simulando
una página de clínica.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from medyra.application.agents.enrichment import EnrichmentAgent
from medyra.infrastructure.scrapers.base import FetchResult
from medyra.infrastructure.scrapers.bing import BingSearchScraper
from medyra.infrastructure.scrapers.doctoralia import DoctoraliaScraper
from medyra.normalization.names import parse_latam_name

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class _StubDoctoralia(DoctoraliaScraper):
    def __init__(self, country: str, html: str, status_per_url: dict[str, int] | None = None) -> None:
        super().__init__(country=country, respect_robots=False)
        self._html = html
        self._status_per_url = status_per_url or {}

    async def fetch(self, url: str, client=None) -> FetchResult:
        status = self._status_per_url.get(url, 200)
        html = self._html if status == 200 else ""
        return FetchResult(url=url, status=status, html=html, from_cache=False)


class _StubBing(BingSearchScraper):
    def __init__(self, search_html: str = "", page_html_per_url: dict[str, str] | None = None) -> None:
        super().__init__(respect_robots=False)
        self._search_html = search_html
        self._pages = page_html_per_url or {}

    async def fetch(self, url: str, client=None) -> FetchResult:
        if "bing.com/search" in url:
            return FetchResult(url=url, status=200, html=self._search_html, from_cache=False)
        html = self._pages.get(url, "")
        return FetchResult(url=url, status=200 if html else 404, html=html, from_cache=False)


# ──────────────────────────────────────────────────────────────────────
# Doctoralia path (MX)
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_doctoralia_path_finds_profile_via_url_guess_mx() -> None:
    html = (FIXTURES / "doctoralia_profile.html").read_text()
    name = parse_latam_name("TOMALA BERMEO ALFREDO EDUARDO")
    agent = EnrichmentAgent(
        country="MX",
        doctoralia=_StubDoctoralia(country="MX", html=html),
        search=_StubBing(),
        use_llm_fallback=False,
    )
    result = await agent.enrich(name, specialty_code="medicina_general")
    assert result.pages_with_name_present >= 1
    assert result.discovered_urls
    attrs = {c.attribute for c in result.claims}
    assert {"email", "phone"} <= attrs


@pytest.mark.asyncio
async def test_claims_have_confidence_and_evidence() -> None:
    html = (FIXTURES / "doctoralia_profile.html").read_text()
    name = parse_latam_name("TOMALA BERMEO ALFREDO EDUARDO")
    agent = EnrichmentAgent(
        country="MX",
        doctoralia=_StubDoctoralia(country="MX", html=html),
        search=_StubBing(),
        use_llm_fallback=False,
    )
    result = await agent.enrich(name, specialty_code="medicina_general")
    assert result.claims
    for c in result.claims:
        assert 0.0 <= c.confidence <= 1.0
        assert c.evidence_strength
        assert c.source_url and c.explanation
        assert c.is_inferred is False


# ──────────────────────────────────────────────────────────────────────
# Bing + generic path (EC, sin Doctoralia)
# ──────────────────────────────────────────────────────────────────────
_FAKE_CLINIC_HTML = """\
<!DOCTYPE html><html><head><title>Centro Médico Salud y Vida</title></head>
<body>
  <h1>Equipo Médico</h1>
  <div class="doctor">
    <h2>Dr. Alfredo Tomalá Bermeo</h2>
    <p>Medicina General · 12 años de experiencia.</p>
    <p>Contacto: <a href="mailto:alfredo.tomala@saludyvida.ec">alfredo.tomala@saludyvida.ec</a></p>
    <p>Tel: +593 99 423 2959</p>
  </div>
</body></html>
"""

_FAKE_BING_FOR_EC = """\
<html><body><ol id="b_results">
  <li class="b_algo">
    <h2><a href="https://www.saludyvida.ec/equipo">Dr. Alfredo Tomalá Bermeo - Salud y Vida</a></h2>
    <div class="b_caption"><p>Médico General en Quito</p></div>
  </li>
</ol></body></html>
"""


@pytest.mark.asyncio
async def test_bing_generic_path_for_ecuador() -> None:
    """EC va por Bing-first y extracción genérica (regex + selectolax)."""
    name = parse_latam_name("TOMALA BERMEO ALFREDO EDUARDO")
    bing = _StubBing(
        search_html=_FAKE_BING_FOR_EC,
        page_html_per_url={"https://www.saludyvida.ec/equipo": _FAKE_CLINIC_HTML},
    )
    agent = EnrichmentAgent(country="EC", search=bing, use_llm_fallback=False)
    result = await agent.enrich(name, specialty_code="medicina_general")

    assert result.pages_fetched >= 1
    assert result.pages_with_name_present >= 1, "El extractor no detectó el nombre del médico"
    attrs = {c.attribute for c in result.claims}
    assert "email" in attrs, "No emitió claim de email"
    assert "phone" in attrs, "No emitió claim de phone"

    # Proximity alta porque email/teléfono están en el mismo bloque que el nombre
    for c in result.claims:
        prox = c.evidence_strength.get("proximity", 0.0)
        assert prox >= 0.5, f"Proximity inesperadamente baja: {prox} ({c.attribute}={c.value})"


@pytest.mark.asyncio
async def test_bing_generic_skips_pages_without_name() -> None:
    """Si la página no menciona al médico, no emite claims."""
    name = parse_latam_name("INEXISTENTE PERSONA RARA")
    bing = _StubBing(
        search_html=_FAKE_BING_FOR_EC,
        page_html_per_url={"https://www.saludyvida.ec/equipo": _FAKE_CLINIC_HTML},
    )
    agent = EnrichmentAgent(country="EC", search=bing, use_llm_fallback=False)
    result = await agent.enrich(name, specialty_code="medicina_general")
    assert result.pages_with_name_present == 0
    assert result.claims == []
