"""Tests del adapter Doctoralia y del Bing search.

Usan fixtures HTML estáticas — NO golpean la red.
La integración end-to-end con red real se prueba manualmente con
`medyra enrich --limit 5` después de revisar la config.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from medyra.infrastructure.scrapers.base import FetchResult
from medyra.infrastructure.scrapers.bing import BingSearchScraper
from medyra.infrastructure.scrapers.doctoralia import (
    DoctoraliaScraper,
    _slug,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class TestDoctoraliaParser:
    @pytest.fixture
    def scraper(self) -> DoctoraliaScraper:
        return DoctoraliaScraper(country="EC", respect_robots=False)

    @pytest.fixture
    def fetch_result(self) -> FetchResult:
        html = (FIXTURES / "doctoralia_profile.html").read_text()
        return FetchResult(
            url="https://www.doctoralia.com.ec/medico/alfredo-tomala-bermeo",
            status=200, html=html, from_cache=False,
        )

    def test_parses_name(self, scraper: DoctoraliaScraper, fetch_result: FetchResult) -> None:
        prof = scraper.parse_profile(fetch_result)
        assert prof is not None
        assert prof.name == "Dr. Alfredo Tomalá Bermeo"

    def test_parses_specialty(self, scraper: DoctoraliaScraper, fetch_result: FetchResult) -> None:
        prof = scraper.parse_profile(fetch_result)
        assert prof is not None
        assert prof.specialty == "Medicina General"

    def test_extracts_phones(self, scraper: DoctoraliaScraper, fetch_result: FetchResult) -> None:
        prof = scraper.parse_profile(fetch_result)
        assert prof is not None
        assert any("594" in p or "593" in p or "423" in p for p in prof.phones)

    def test_extracts_emails(self, scraper: DoctoraliaScraper, fetch_result: FetchResult) -> None:
        prof = scraper.parse_profile(fetch_result)
        assert prof is not None
        assert "alfredo.tomala@example-clinic.ec" in prof.emails

    def test_extracts_clinic(self, scraper: DoctoraliaScraper, fetch_result: FetchResult) -> None:
        prof = scraper.parse_profile(fetch_result)
        assert prof is not None
        assert len(prof.clinics) >= 1

    def test_rejects_non_profile_html(self, scraper: DoctoraliaScraper) -> None:
        fr = FetchResult(url="x", status=200, html="<html><body>404</body></html>", from_cache=False)
        assert scraper.parse_profile(fr) is None

    def test_rejects_404(self, scraper: DoctoraliaScraper) -> None:
        fr = FetchResult(url="x", status=404, html="", from_cache=False)
        assert scraper.parse_profile(fr) is None

    def test_candidate_urls_generation(self, scraper: DoctoraliaScraper) -> None:
        urls = scraper.candidate_urls("ALFREDO EDUARDO", "TOMALA BERMEO")
        assert urls
        assert all(u.startswith("https://www.doctoralia.com.ec/medico/") for u in urls)

    def test_is_profile_url(self, scraper: DoctoraliaScraper) -> None:
        assert scraper.is_profile_url("https://www.doctoralia.com.ec/medico/juan-perez")
        assert not scraper.is_profile_url("https://www.doctoralia.com.ec/medicina-general/quito")

    def test_slug_handles_accents(self) -> None:
        assert _slug("Tomalá Bermeo") == "tomala-bermeo"
        assert _slug("María del Pilar Núñez") == "maria-del-pilar-nunez"

    def test_unsupported_country(self) -> None:
        with pytest.raises(ValueError):
            DoctoraliaScraper(country="XX")


class TestBingParser:
    def test_extracts_organic_results(self) -> None:
        html = (FIXTURES / "bing_results.html").read_text()
        hits = BingSearchScraper.parse_results(html)
        assert len(hits) == 3
        assert hits[0].url.startswith("https://www.doctoralia.com.ec/medico/")

    def test_filter_by_domain(self) -> None:
        html = (FIXTURES / "bing_results.html").read_text()
        hits = BingSearchScraper.parse_results(html)
        doctoralia_hits = BingSearchScraper.filter_by_domain(hits, "doctoralia.")
        assert len(doctoralia_hits) == 2
        assert all("doctoralia" in h.url for h in doctoralia_hits)

    def test_query_url_quoting(self) -> None:
        scraper = BingSearchScraper(respect_robots=False)
        url = scraper.query_url('"Dr. Juan Pérez" pediatria')
        assert "Dr.+Juan" not in url  # debe usar URL-encoding, no '+'
        assert "%22" in url
