"""Enrichment Agent: discovery + extracción + scoring.

Estrategia por país:

  - **Países con Doctoralia operando** (ES, MX, CO, PE, CL):
        Doctoralia URL guess → Doctoralia parse → Bing fallback.

  - **Países SIN Doctoralia** (CR, PA, EC, y otros LATAM):
        Bing search → top-N hits → extractor genérico (regex + selectolax) →
        proximity scoring (¿el contacto está cerca del nombre del médico?).

El LLM extractor es opcional y entra como fallback cuando:
  - se descargó HTML pero no se extrajo nada con CSS/regex, o
  - la página tiene texto libre denso (bio, "about me") donde
    selectores estructurales no aplican.

NO escribe a DB. Devuelve EnrichmentResult con claims puros.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import structlog

from medyra.infrastructure.llm.extractors import LLMExtractor
from medyra.infrastructure.scrapers.bing import SearchHit
from medyra.infrastructure.scrapers.ddg import DuckDuckGoScraper
from medyra.infrastructure.scrapers.doctoralia import (
    TLD_BY_COUNTRY as DOCTORALIA_TLDS,
    DoctoraliaProfile,
    DoctoraliaScraper,
)
from medyra.infrastructure.scrapers.generic import GenericExtraction, extract as generic_extract
from medyra.inference.confidence import score
from medyra.normalization import emails as email_norm
from medyra.normalization import phones as phone_norm
from medyra.normalization.names import NameParts, strip_accents

log = structlog.get_logger()

# Doctoralia tiene presencia REAL en estos países. Para el resto, va Bing-first.
DOCTORALIA_COUNTRIES = {"ES", "MX", "CO", "PE", "CL"}


@dataclass
class EnrichmentClaim:
    attribute: str
    value: str
    value_normalized: str
    subkind: str | None
    confidence: float
    is_inferred: bool
    source_url: str
    source_name: str
    evidence_strength: dict
    explanation: str


@dataclass
class EnrichmentResult:
    physician_name: str
    country: str
    discovered_urls: list[str] = field(default_factory=list)
    pages_fetched: int = 0
    pages_with_name_present: int = 0
    claims: list[EnrichmentClaim] = field(default_factory=list)
    used_llm: bool = False
    notes: list[str] = field(default_factory=list)


class EnrichmentAgent:
    def __init__(
        self,
        country: str,
        *,
        doctoralia: DoctoraliaScraper | None = None,
        search: DuckDuckGoScraper | None = None,
        llm: LLMExtractor | None = None,
        use_llm_fallback: bool = True,
        search_top_n: int = 5,
    ) -> None:
        self.country = country
        self.search = search or DuckDuckGoScraper()
        # alias para tests legacy
        self.bing = self.search
        self.bing_top_n = search_top_n
        self.search_top_n = search_top_n
        self.llm = llm
        self.use_llm_fallback = use_llm_fallback and (llm is not None and llm.available)

        self.has_doctoralia = country in DOCTORALIA_COUNTRIES and country in DOCTORALIA_TLDS
        self.doctoralia: DoctoraliaScraper | None = (
            doctoralia or (DoctoraliaScraper(country=country) if self.has_doctoralia else None)
        )

    async def enrich(
        self,
        name_parts: NameParts,
        specialty_code: str | None,
        client: httpx.AsyncClient | None = None,
    ) -> EnrichmentResult:
        result = EnrichmentResult(physician_name=name_parts.full_name, country=self.country)
        own_client = client is None
        client = client or httpx.AsyncClient(follow_redirects=True, timeout=30.0)
        try:
            if self.has_doctoralia and self.doctoralia is not None:
                await self._enrich_via_doctoralia(name_parts, specialty_code, result, client)
                # Solo recurrimos a Bing genérico si Doctoralia no rindió.
                if not result.claims:
                    await self._enrich_via_bing_generic(name_parts, specialty_code, result, client)
            else:
                await self._enrich_via_bing_generic(name_parts, specialty_code, result, client)
        finally:
            if own_client:
                await client.aclose()
        return result

    # ──────────────────────────────────────────────────────────────────────
    # Doctoralia path
    # ──────────────────────────────────────────────────────────────────────
    async def _enrich_via_doctoralia(
        self,
        name_parts: NameParts,
        _specialty_code: str | None,
        result: EnrichmentResult,
        client: httpx.AsyncClient,
    ) -> None:
        assert self.doctoralia is not None
        family = name_parts.family_name_1
        if name_parts.family_name_2:
            family = f"{family} {name_parts.family_name_2}"
        given = name_parts.given_name
        for url in self.doctoralia.candidate_urls(given, family):
            fr = await self.doctoralia.fetch(url, client)
            if fr.status != 200 or not fr.html:
                continue
            profile = self.doctoralia.parse_profile(fr)
            if profile:
                result.discovered_urls.append(url)
                result.pages_fetched += 1
                result.pages_with_name_present += 1
                result.claims.extend(self._claims_from_doctoralia(profile))
                return

    @staticmethod
    def _claims_from_doctoralia(profile: DoctoraliaProfile) -> list[EnrichmentClaim]:
        out: list[EnrichmentClaim] = []
        tld = profile.url.split("doctoralia.")[1].split("/")[0] if "doctoralia." in profile.url else ""
        country_iso = {v: k for k, v in DOCTORALIA_TLDS.items()}.get(tld, "ES")

        for raw in profile.phones:
            norm = phone_norm.normalize(raw, country_iso)
            if not norm:
                continue
            sigs = {"source_tier_2", "phonenumbers_valid", "single_source"}
            if norm.line_type == "mobile":
                sigs.add("phone_type_mobile")
            r = score(sigs, prior=0.40)
            out.append(EnrichmentClaim(
                attribute="phone", value=raw, value_normalized=norm.e164,
                subkind="clinic_main", confidence=round(r.probability, 4),
                is_inferred=False, source_url=profile.url, source_name="doctoralia",
                evidence_strength=r.to_evidence_strength(),
                explanation="Teléfono mostrado en perfil público de Doctoralia.",
            ))
        for raw in profile.emails:
            norm_e = email_norm.normalize(raw)
            if not norm_e:
                continue
            sigs = {"source_tier_2", "mx_ok" if not norm_e.is_freemail else "single_source"}
            r = score(sigs, prior=0.40)
            out.append(EnrichmentClaim(
                attribute="email", value=raw, value_normalized=norm_e.address,
                subkind="personal" if norm_e.is_freemail else "clinic",
                confidence=round(r.probability, 4),
                is_inferred=False, source_url=profile.url, source_name="doctoralia",
                evidence_strength=r.to_evidence_strength(),
                explanation=f"Email en perfil público de Doctoralia (dominio: {norm_e.domain}).",
            ))
        return out

    # ──────────────────────────────────────────────────────────────────────
    # Bing-first path (genérico, ideal para CR/PA/EC)
    # ──────────────────────────────────────────────────────────────────────
    async def _enrich_via_bing_generic(
        self,
        name_parts: NameParts,
        specialty_code: str | None,
        result: EnrichmentResult,
        client: httpx.AsyncClient,
    ) -> None:
        hits = await self._bing_search(name_parts, specialty_code, client)
        if not hits:
            result.notes.append("Bing devolvió 0 resultados.")
            return

        name_tokens = self._name_tokens(name_parts)
        # Fetch top-N hits con filtros de calidad
        ranked = self._rank_hits(hits[: self.bing_top_n * 2], name_parts)[: self.bing_top_n]
        for hit in ranked:
            try:
                fr = await self.bing.fetch(hit.url, client)
            except Exception as e:
                log.warning("enrichment.fetch_fail", url=hit.url, error=str(e))
                continue
            if fr.status != 200 or not fr.html:
                continue
            result.pages_fetched += 1
            extraction = generic_extract(
                fr.html, url=hit.url,
                country=self.country,
                name_tokens=name_tokens,
            )
            if extraction.name_present:
                result.pages_with_name_present += 1
                result.discovered_urls.append(hit.url)
                result.claims.extend(self._claims_from_generic(extraction, hit))

    @staticmethod
    def _name_tokens(name_parts: NameParts) -> list[str]:
        full = f"{name_parts.family_name_1} {name_parts.family_name_2 or ''} {name_parts.given_name}"
        return [t for t in strip_accents(full).split() if t]

    async def _bing_search(
        self, name_parts: NameParts, specialty: str | None, client: httpx.AsyncClient
    ) -> list[SearchHit]:
        # Query construida para alta precisión: nombre entrecomillado + esp + país
        country_name = {
            "EC": "Ecuador", "CR": "Costa Rica", "PA": "Panama",
            "MX": "Mexico", "CO": "Colombia", "PE": "Peru", "CL": "Chile",
            "AR": "Argentina", "ES": "Spain",
        }.get(self.country, self.country)
        display = name_parts.display_name  # "Apellidos, Nombres"
        q_parts = [f'"{display.split(",")[0].strip()}"']  # buscar por apellidos primero
        if name_parts.given_name:
            first = name_parts.given_name.split()[0]
            q_parts.append(f'"{first}"')
        if specialty:
            q_parts.append(specialty.replace("_", " "))
        q_parts.append(country_name)
        url = self.bing.query_url(" ".join(q_parts))
        fr = await self.bing.fetch(url, client)
        if fr.status != 200:
            return []
        return self.bing.parse_results(fr.html)

    def _rank_hits(self, hits: list[SearchHit], name_parts: NameParts) -> list[SearchHit]:
        """Heurística: prioriza dominios con keywords médicas o que mencionan apellido."""
        family = strip_accents(name_parts.family_name_1).lower()
        good_domains = ("medico", "doctor", "clinica", "hospital", "salud", "consultorio", "med.", "msp.")
        bad_domains = ("facebook.com/marketplace", "pinterest", "youtube.com/watch", "linkedin.com/jobs")

        def score_hit(h: SearchHit) -> float:
            url_l = h.url.lower()
            text = (h.title + " " + h.snippet).lower()
            s = 0.0
            if any(b in url_l for b in bad_domains):
                return -1.0
            if family and family in text:
                s += 2.0
            if any(g in url_l for g in good_domains):
                s += 1.0
            if "linkedin.com/in/" in url_l:
                s += 0.5
            return s

        return sorted(hits, key=score_hit, reverse=True)

    @staticmethod
    def _claims_from_generic(extraction: GenericExtraction, _hit: SearchHit) -> list[EnrichmentClaim]:
        out: list[EnrichmentClaim] = []
        # Tier según dominio
        url_l = extraction.url.lower()
        if any(d in url_l for d in ("msp.gob", "ministeriodesalud", "colegiomedico", "consejotecnicodesalud", "medicos.cr", ".gov", ".gob")):
            tier_sig = "source_tier_1"
            tier_int = 1
        elif any(d in url_l for d in ("facebook.com", "linkedin.com", "twitter.com", "instagram.com")):
            tier_sig = "source_tier_3"
            tier_int = 3
        else:
            tier_sig = "source_tier_2"
            tier_int = 2

        for value, proximity in extraction.emails:
            norm_e = email_norm.normalize(value)
            if not norm_e:
                continue
            sigs: set[str] = {tier_sig, "single_source"}
            if proximity >= 0.85:
                sigs.add("mx_ok" if not norm_e.is_freemail else "verified_within_180d")
            r = score(sigs, prior=0.20 + 0.30 * proximity)
            out.append(EnrichmentClaim(
                attribute="email", value=value, value_normalized=norm_e.address,
                subkind="personal" if norm_e.is_freemail else "clinic",
                confidence=round(r.probability, 4),
                is_inferred=False,
                source_url=extraction.url, source_name=f"generic_tier{tier_int}",
                evidence_strength={**r.to_evidence_strength(), "proximity": proximity},
                explanation=f"Email encontrado en página pública (proximidad al nombre: {proximity:.2f}). Título: {extraction.page_title!r}.",
            ))

        for value, proximity in extraction.phones:
            sigs: set[str] = {tier_sig, "phonenumbers_valid", "single_source"}
            r = score(sigs, prior=0.20 + 0.30 * proximity)
            out.append(EnrichmentClaim(
                attribute="phone", value=value, value_normalized=value,
                subkind="unknown",
                confidence=round(r.probability, 4),
                is_inferred=False,
                source_url=extraction.url, source_name=f"generic_tier{tier_int}",
                evidence_strength={**r.to_evidence_strength(), "proximity": proximity},
                explanation=f"Teléfono encontrado en página pública (proximidad al nombre: {proximity:.2f}).",
            ))
        return out
