"""Adapter del Colegio de Médicos y Cirujanos de Costa Rica (Tier-1).

El buscador oficial vive en `medicoscr.hulilabs.com` (Huli Labs hosts la app).
Verificado el 2026-05-19:

  - `GET /api/autocomplete?q={NAME}` → JSON con matches del padrón.
        Schema por item: {oid, tid, name, hint, desc, path, ...}
        `desc` trae las especialidades reconocidas por el Colegio.
        `path` es `/doctor/{slug}` (SPA).
  - Página `/doctor/{slug}` es SPA: el HTML inicial solo trae nombre y
    especialidades. Email/teléfono se renderizan con JS y requieren
    Playwright para extraerse (TODO Sprint 2).

Lo que SÍ provee este adapter HOY:
  - Validación canónica de identidad ("¿este médico está registrado?").
  - Nombre canónico con tildes/ñ correctas.
  - Especialidades autoritativas (Colegio vs las del Excel del cliente).
  - `oid` numérico para deduplicación cross-source.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx
import structlog
from rapidfuzz import fuzz

from medintel.infrastructure.scrapers.base import BaseScraper
from medintel.normalization.names import NameParts, parse_latam_name, strip_accents


def strict_name_score(input_parts: NameParts, candidate_full_name: str) -> float:
    """Score estricto que valida apellidos + primer nombre por separado.

    Caso problemático observado en padrón real:
      Input  : "VARGAS RAMIREZ JOSE GERARDO"
      Cand.  : "Maria Jose Vargas Vargas"   ← MISMA persona NO, falso positivo de token_set
      `token_set_ratio` da 1.0; este scorer da ~0.3.

    Algoritmo:
      - Parsea el nombre canónico del Colegio con el modo `given_first`
        (formato "Nombre Apellido Apellido").
      - Compara: apellido1 ↔ apellido1; apellido2 ↔ apellido2; primer nombre ↔ primer nombre.
      - 70% apellidos / 30% primer nombre. Cada apellido vale 35%.
      - Si un apellido falla por completo (< 0.6), penaliza fuerte.
    """
    # Los nombres del Colegio vienen en formato given_first ("Nombre Apellido Apellido").
    cand = parse_latam_name(candidate_full_name, name_format="given_first")

    def _ratio(a: str, b: str) -> float:
        if not a or not b:
            return 0.0
        return fuzz.token_sort_ratio(strip_accents(a).lower(), strip_accents(b).lower()) / 100.0

    fam1_score = _ratio(input_parts.family_name_1, cand.family_name_1)
    # Si solo hay 1 apellido en alguno, asignar 1.0 para no penalizar.
    if input_parts.family_name_2 and cand.family_name_2:
        fam2_score = _ratio(input_parts.family_name_2, cand.family_name_2)
    else:
        fam2_score = 1.0 if not input_parts.family_name_2 and not cand.family_name_2 else 0.5

    # Primer nombre del input vs primer nombre del candidato.
    input_first = input_parts.given_name.split(" ")[0] if input_parts.given_name else ""
    cand_first = cand.given_name.split(" ")[0] if cand.given_name else ""
    first_score = _ratio(input_first, cand_first)

    # Penalización dura si UN apellido falla por completo
    if fam1_score < 0.6 or fam2_score < 0.6:
        return min(fam1_score, fam2_score) * 0.5

    return 0.35 * fam1_score + 0.35 * fam2_score + 0.30 * first_score

log = structlog.get_logger()


@dataclass
class CRPhysicianMatch:
    """Match contra el padrón del Colegio Médico de Costa Rica."""
    oid: int
    canonical_name: str
    raw_specialties: str         # "Pediatría, Neonatología"
    profile_path: str            # "/doctor/{slug}"
    score: float                 # 0.0–1.0  fuzzy match con el nombre query
    hint_html: str = ""          # con <em> alrededor de los matches

    @property
    def profile_url(self) -> str:
        return f"https://medicoscr.hulilabs.com{self.profile_path}"

    @property
    def specialties_list(self) -> list[str]:
        return [s.strip() for s in self.raw_specialties.split(",") if s.strip()]


class ColegioCRScraper(BaseScraper):
    """Cliente del autocompletado del Colegio Médico CR.

    Hospedado por Huli Labs en `medicoscr.hulilabs.com`. El endpoint
    `/api/autocomplete` es público (no requiere auth) y devuelve JSON.
    Conservador: 2s entre requests aunque el endpoint tolere más.
    """

    name = "colegio_cr"
    base_url = "https://medicoscr.hulilabs.com"
    min_interval_seconds = 3.5  # conservador: el endpoint dio 429 con 2s
    # API público sin restricciones documentadas; robots.txt no menciona /api.
    # Aún así seguimos respetando robots.txt por defecto.

    async def autocomplete(
        self,
        query: str,
        client: httpx.AsyncClient | None = None,
        *,
        scorer_against: NameParts | None = None,
    ) -> list[CRPhysicianMatch]:
        """Devuelve matches del padrón.

        Si `scorer_against` se provee, se usa `strict_name_score` que valida
        apellidos + primer nombre por separado. Recomendado para verificación
        de identidad. Sin él, fallback a `token_set_ratio` (laxo, útil para
        autocompletado libre).
        """
        url = f"{self.base_url}/api/autocomplete?q={quote(query)}"
        fr = await self.fetch(url, client)
        if fr.status != 200 or not fr.html:
            return []
        try:
            import json
            data = json.loads(fr.html)
        except (json.JSONDecodeError, ValueError):
            log.warning("colegio_cr.parse_fail", url=url)
            return []
        if not isinstance(data, list):
            return []

        out: list[CRPhysicianMatch] = []
        target_norm = strip_accents(query).lower()
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or ""
            if not name:
                continue
            if scorer_against is not None:
                score = strict_name_score(scorer_against, name)
            else:
                cand_norm = strip_accents(name).lower()
                score = fuzz.token_set_ratio(target_norm, cand_norm) / 100.0
            out.append(CRPhysicianMatch(
                oid=int(item.get("oid", 0)),
                canonical_name=name,
                raw_specialties=item.get("desc", "") or "",
                profile_path=(item.get("path") or "").split("?")[0],
                score=score,
                hint_html=item.get("hint", "") or "",
            ))
        out.sort(key=lambda m: -m.score)
        return out

    async def best_match(
        self,
        name_parts: NameParts,
        client: httpx.AsyncClient | None = None,
        min_score: float = 0.75,
    ) -> CRPhysicianMatch | None:
        """Devuelve el mejor match para un NameParts si supera `min_score`.

        Estrategia de query:
          1. Probar `{Apellido1} {Primer nombre}` (más selectivo en padrones LATAM).
          2. Si no rinde, probar `{Apellido1} {Apellido2}`.
          3. Si no rinde, probar el full_name.
        """
        first = name_parts.given_name.split(" ")[0] if name_parts.given_name else ""
        fam1 = name_parts.family_name_1
        fam2 = name_parts.family_name_2 or ""

        queries: list[str] = []
        if fam1 and first:
            queries.append(f"{fam1} {first}")
        if fam1 and fam2:
            queries.append(f"{fam1} {fam2}")
        if name_parts.full_name and name_parts.full_name not in queries:
            queries.append(name_parts.full_name)

        for q in queries:
            matches = await self.autocomplete(q, client, scorer_against=name_parts)
            if matches and matches[0].score >= min_score:
                return matches[0]
        return None


def _norm_spec(s: str) -> str:
    """Normaliza una especialidad para comparación: sin acentos, sin separadores, lower."""
    s = strip_accents(s).lower()
    for ch in ("_", "-", ".", "/", ","):
        s = s.replace(ch, " ")
    return " ".join(s.split())  # colapsa whitespace


def _specialty_agrees(excel_spec: str, cr_specs: list[str]) -> bool:
    """¿La especialidad del Excel coincide con alguna del Colegio?

    Heurística: tras normalizar (sin acentos/underscores), basta que el
    nombre del Excel aparezca como substring en alguna especialidad del CR
    (o viceversa). Cubre 'medicina_interna' vs 'Medicina Interna' y
    'MEDICINA_INTERNA' vs 'Infectología, Medicina Interna'.
    """
    e = _norm_spec(excel_spec)
    if not e:
        return False
    for s in cr_specs:
        c = _norm_spec(s)
        if not c:
            continue
        if e == c or e in c or c in e:
            return True
    return False


@dataclass
class CRVerificationResult:
    """Resultado por médico: matched o not. Inputs útiles para enriquecer Excel."""
    input_name: str
    input_specialty: str | None
    matched: bool = False
    canonical_name: str | None = None
    cr_specialties: list[str] = field(default_factory=list)
    profile_url: str | None = None
    oid: int | None = None
    score: float = 0.0
    specialty_agreement: bool | None = None  # ¿coincide la esp. del Excel con la del Colegio?
    notes: list[str] = field(default_factory=list)


async def verify_excel_against_cr(
    physicians: list[tuple[NameParts, str | None]],
    *,
    scraper: ColegioCRScraper | None = None,
    concurrency: int = 1,
) -> list[CRVerificationResult]:
    """Verifica un batch de médicos contra el padrón CR.

    `physicians`: lista de (NameParts, especialidad_excel).
    Concurrency limitada (3) para no estresar el endpoint del Colegio.
    """
    scraper = scraper or ColegioCRScraper()
    sem = asyncio.Semaphore(concurrency)
    results: list[CRVerificationResult] = []

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        async def one(np: NameParts, esp: str | None) -> CRVerificationResult:
            from medintel.infrastructure.scrapers.base import ScrapingBlocked
            async with sem:
                res = CRVerificationResult(input_name=np.full_name, input_specialty=esp)
                try:
                    match = await scraper.best_match(np, client=client)
                except ScrapingBlocked as e:
                    res.notes.append(f"Blocked: {e}")
                    return res
                except Exception as e:  # noqa: BLE001
                    res.notes.append(f"Error: {type(e).__name__}: {str(e)[:80]}")
                    return res
                if not match:
                    res.notes.append("Sin match >= 0.75 en autocomplete")
                    return res
                res.matched = True
                res.canonical_name = match.canonical_name
                res.cr_specialties = match.specialties_list
                res.profile_url = match.profile_url
                res.oid = match.oid
                res.score = match.score
                if esp:
                    res.specialty_agreement = _specialty_agrees(esp, match.specialties_list)
                return res

        tasks = [one(np, esp) for np, esp in physicians]
        for coro in asyncio.as_completed(tasks):
            results.append(await coro)
    return results
