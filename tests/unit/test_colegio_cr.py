"""Tests del adapter Colegio Médico CR.

Usa fixtures JSON estáticas — NO golpea la red.
"""
from __future__ import annotations

import json

import pytest

from medintel.infrastructure.scrapers.base import FetchResult
from medintel.infrastructure.scrapers.colegio_cr import ColegioCRScraper
from medintel.normalization.names import parse_latam_name


_AUTOCOMPLETE_FIXTURE = json.dumps([
    {
        "oid": 25346, "tid": 4,
        "name": "Natalia Patricia Zapata Aguilar",
        "hint": "<em>Zapata</em> <em>Aguilar</em>",
        "desc": "Neonatología, Pediatría",
        "path": "/doctor/natalia-patricia-zapata-aguilar?ref=sb",
    },
    {
        "oid": 99999, "tid": 4,
        "name": "Otro Médico Random",
        "hint": "Otro",
        "desc": "Cardiología",
        "path": "/doctor/otro?ref=sb",
    },
])


class _StubColegio(ColegioCRScraper):
    def __init__(self, response_body: str = _AUTOCOMPLETE_FIXTURE) -> None:
        super().__init__(respect_robots=False)
        self._body = response_body

    async def fetch(self, url: str, _client=None) -> FetchResult:
        return FetchResult(url=url, status=200, html=self._body, from_cache=False)


@pytest.mark.asyncio
async def test_autocomplete_parses_results() -> None:
    scraper = _StubColegio()
    matches = await scraper.autocomplete("Zapata Aguilar")
    assert len(matches) == 2
    top = matches[0]
    assert top.canonical_name == "Natalia Patricia Zapata Aguilar"
    assert top.oid == 25346
    assert "Pediatría" in top.specialties_list
    assert top.profile_path == "/doctor/natalia-patricia-zapata-aguilar"
    assert top.profile_url.startswith("https://medicoscr.hulilabs.com/")


@pytest.mark.asyncio
async def test_best_match_returns_top_above_threshold() -> None:
    scraper = _StubColegio()
    name = parse_latam_name("ZAPATA AGUILAR NATALIA")
    match = await scraper.best_match(name, min_score=0.7)
    assert match is not None
    assert match.canonical_name == "Natalia Patricia Zapata Aguilar"
    assert match.score >= 0.7


@pytest.mark.asyncio
async def test_best_match_returns_none_below_threshold() -> None:
    scraper = _StubColegio()
    name = parse_latam_name("INEXISTENTE TOTAL FANTASMA")
    match = await scraper.best_match(name, min_score=0.95)
    assert match is None


@pytest.mark.asyncio
async def test_autocomplete_returns_empty_on_bad_json() -> None:
    scraper = _StubColegio(response_body="<html>not json</html>")
    matches = await scraper.autocomplete("anything")
    assert matches == []


@pytest.mark.asyncio
async def test_results_ordered_by_score() -> None:
    scraper = _StubColegio()
    matches = await scraper.autocomplete("Zapata Aguilar")
    scores = [m.score for m in matches]
    assert scores == sorted(scores, reverse=True)


def test_strict_score_rejects_token_set_false_positive() -> None:
    """Caso real: 'VARGAS RAMIREZ JOSE GERARDO' NO debe matchear con 'Maria Jose Vargas Vargas'.

    token_set_ratio da 1.0; strict_name_score debe dar bajo porque Ramírez ≠ Vargas.
    """
    from medintel.infrastructure.scrapers.colegio_cr import strict_name_score
    np = parse_latam_name("VARGAS RAMIREZ JOSE GERARDO")
    score = strict_name_score(np, "Maria Jose Vargas Vargas")
    assert score < 0.7, f"Falso positivo no detectado: score={score}"


def test_strict_score_accepts_true_match() -> None:
    from medintel.infrastructure.scrapers.colegio_cr import strict_name_score
    np = parse_latam_name("ZAPATA AGUILAR NATALIA")
    score = strict_name_score(np, "Natalia Patricia Zapata Aguilar")
    assert score >= 0.85, f"True match rechazado: score={score}"


def test_strict_score_rejects_different_last_names() -> None:
    """'VEGA RODRIGUEZ ADRIANA' vs 'Adriana María Vega Borbón' → distinta persona."""
    from medintel.infrastructure.scrapers.colegio_cr import strict_name_score
    np = parse_latam_name("VEGA RODRIGUEZ ADRIANA")
    score = strict_name_score(np, "Adriana María Vega Borbón")
    assert score < 0.7, f"Falso positivo no detectado: score={score}"


def test_specialties_list_parses_csv() -> None:
    from medintel.infrastructure.scrapers.colegio_cr import CRPhysicianMatch
    m = CRPhysicianMatch(
        oid=1, canonical_name="X", raw_specialties="Pediatría, Neonatología, Genética",
        profile_path="/doctor/x", score=1.0,
    )
    assert m.specialties_list == ["Pediatría", "Neonatología", "Genética"]
