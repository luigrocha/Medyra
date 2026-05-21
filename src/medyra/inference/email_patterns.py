"""Generación de hipótesis de email a partir de nombre + dominio.

Pipeline (ver `__module__` doc en `confidence.py`):

    name + domain → patterns → MX check → catch-all detect →
    external validator (opcional) → confidence emit

Los priors están calibrados con observación empírica general; conforme
confirmes emails reales con tu equipo, ajusta `PATTERN_PRIOR` por dominio
en la tabla `domain_email_pattern`.
"""
from __future__ import annotations

from dataclasses import dataclass

from medyra.normalization.names import NameParts, strip_accents

# Patrones ordenados por prior aproximado en empresas/instituciones LATAM.
# La suma no necesita ser 1: son priors marginales por patrón.
PATTERN_PRIOR: dict[str, float] = {
    "{first}.{last}": 0.34,
    "{f}{last}": 0.18,
    "{first}{last}": 0.12,
    "{first}_{last}": 0.06,
    "{first}.{last2}": 0.05,
    "{last}.{first}": 0.05,
    "{f}.{last}": 0.04,
    "{first}.{m}.{last}": 0.03,
    "{first}": 0.03,
    "{last}": 0.03,
    "dr.{last}": 0.02,
    "dr{last}": 0.02,
    "{first}-{last}": 0.02,
    "info": 0.01,
    "contacto": 0.01,
}


@dataclass
class EmailHypothesis:
    address: str
    pattern: str
    prior: float
    mx_ok: bool = False
    catch_all: bool = False
    external_score: float | None = None

    def __post_init__(self) -> None:
        # Cualquier hipótesis con `..` o `.@` es inválida en RFC 5322.
        self.address = self.address.replace("..", ".").strip(".")


def _slot_values(name: NameParts) -> dict[str, str]:
    """Slots normalizados (sin acentos, lower) para sustituir en patrones."""
    given = strip_accents(name.given_name).lower().split(" ")
    first = given[0] if given else ""
    middle = given[1] if len(given) > 1 else ""
    last1 = strip_accents(name.family_name_1).lower()
    last2 = strip_accents(name.family_name_2 or name.family_name_1).lower()
    return {
        "first": first,
        "f": first[:1],
        "m": middle[:1],
        "last": last1,
        "last2": last2,
    }


def generate(name: NameParts, domain: str) -> list[EmailHypothesis]:
    """Genera todas las hipótesis no vacías de email para un nombre y dominio."""
    if not domain or not name.family_name_1:
        return []
    slots = _slot_values(name)
    out: list[EmailHypothesis] = []
    seen: set[str] = set()
    for tpl, prior in PATTERN_PRIOR.items():
        try:
            local = tpl.format(**slots)
        except KeyError:
            continue
        local = local.replace("..", ".").strip(".")
        if not local or "@" in local:
            continue
        addr = f"{local}@{domain}".lower()
        if addr in seen:
            continue
        seen.add(addr)
        out.append(EmailHypothesis(address=addr, pattern=tpl, prior=prior))
    # Orden descendente por prior facilita el corte por top-K en validación cara.
    out.sort(key=lambda h: -h.prior)
    return out


def signals_for(h: EmailHypothesis, source_tier: int) -> set[str]:
    """Mapea una hipótesis validada al set de señales para `confidence.score()`."""
    sigs: set[str] = {"is_inferred_from_pattern"}
    sigs.add({1: "source_tier_1", 2: "source_tier_2", 3: "source_tier_3"}[source_tier])
    if h.mx_ok:
        sigs.add("mx_ok")
    if h.catch_all:
        sigs.add("catch_all_domain")
    if h.external_score is not None:
        sigs.add("external_validator_pass" if h.external_score >= 0.7 else "external_validator_fail")
    return sigs
