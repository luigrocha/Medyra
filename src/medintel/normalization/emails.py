"""Normalización y validación básica de emails.

Validación profunda (MX, catch-all, validador externo) vive en
`inference.email_validator` para no bloquear el módulo de normalización
con I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from email_validator import EmailNotValidError, validate_email
from rapidfuzz import process

# Whitespace y newlines embebidos en cells de Excel pegadas a mano.
_TRIM = re.compile(r"\s+")

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.es", "hotmail.com",
    "outlook.com", "live.com", "icloud.com", "me.com", "protonmail.com",
}

# Catálogo de dominios institucionales conocidos para detección de typos.
# Crece con cada confirmación humana.
KNOWN_INSTITUTIONAL_DOMAINS: set[str] = {
    "clinicabiblica.com",
    "ccss.sa.cr",
    "ucr.ac.cr",
    "coopesain.sa.cr",
    "pediaclinic.cr",
}


@dataclass(frozen=True)
class EmailNormalized:
    address: str
    local: str
    domain: str
    is_freemail: bool
    looks_typoed: bool
    suggested_domain: str | None


def normalize(raw: str) -> EmailNormalized | None:
    """Limpia y valida sintaxis. No hace MX lookup."""
    if not raw:
        return None
    candidate = _TRIM.sub("", raw).strip(".,;").lower()
    # Algunos cells tienen el email envuelto en comillas: "foo@bar.com"
    candidate = candidate.strip('"').strip("'")
    try:
        info = validate_email(candidate, check_deliverability=False)
    except EmailNotValidError:
        return None
    domain = info.domain.lower()
    suggestion = _suggest_typo_fix(domain)
    return EmailNormalized(
        address=info.normalized.lower(),
        local=info.local_part.lower(),
        domain=domain,
        is_freemail=domain in FREEMAIL_DOMAINS,
        looks_typoed=suggestion is not None,
        suggested_domain=suggestion,
    )


def _suggest_typo_fix(domain: str) -> str | None:
    """Detecta typos contra dominios conocidos (ej. clinicabiblia → clinicabiblica)."""
    if domain in KNOWN_INSTITUTIONAL_DOMAINS or domain in FREEMAIL_DOMAINS:
        return None
    match = process.extractOne(
        domain,
        list(KNOWN_INSTITUTIONAL_DOMAINS | FREEMAIL_DOMAINS),
        score_cutoff=85,
    )
    if not match:
        return None
    suggested, score, _ = match
    return suggested if suggested != domain else None
