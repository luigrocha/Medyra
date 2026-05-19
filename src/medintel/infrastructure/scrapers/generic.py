"""Extractor genérico: cualquier HTML → emails + teléfonos.

Para países donde Doctoralia no opera (CR, PA, EC), el adapter primario es
Bing search → cualquier dominio plausible (clínica, hospital, FB page,
páginas amarillas) → este extractor saca contactos del texto.

Estrategia:
  - Parsing HTML con selectolax (rápido).
  - Texto plano → regex para emails.
  - Texto plano → phonenumbers.PhoneNumberMatcher (más robusto que regex).
  - Heurística de proximidad: contactos cerca del nombre del médico
    en el DOM tienen confianza mayor que los del footer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import phonenumbers
from selectolax.parser import HTMLParser

from medintel.normalization.names import strip_accents

_EMAIL_RE = re.compile(
    r"(?<![A-Z0-9_.+-])([A-Z0-9_.+-]+@[A-Z0-9-]+\.[A-Z0-9.-]+)(?![A-Z0-9_.+-])",
    re.IGNORECASE,
)

# Dominios de imágenes/assets que no son emails reales.
_EMAIL_BLOCKLIST_SUBS = (
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".webp",
    "sentry.io", "wixpress.com", "@2x.", "@3x.",
)

# Texto chatarra que aparece en footers / scripts.
_TEXT_NOISE_KEYWORDS = ("cookie", "javascript", "©", "google-analytics", "gtag")


@dataclass
class GenericExtraction:
    url: str
    page_title: str | None = None
    name_present: bool = False
    emails: list[tuple[str, float]] = field(default_factory=list)  # (value, proximity_score)
    phones: list[tuple[str, float]] = field(default_factory=list)


def _is_plausible_email(s: str) -> bool:
    low = s.lower()
    if any(b in low for b in _EMAIL_BLOCKLIST_SUBS):
        return False
    if low.count("@") != 1:
        return False
    local, domain = low.split("@")
    if len(local) > 64 or len(domain) > 253:
        return False
    return "." in domain


def _name_in_text(text: str, name_tokens: list[str]) -> bool:
    """¿Aparecen los tokens del nombre del médico en el texto?

    Heurística simple: contar tokens del apellido (>= 4 chars) que estén
    presentes en el texto sin acentos, case-insensitive. Si ≥1 → presente.
    """
    norm_text = strip_accents(text).lower()
    significant = [strip_accents(t).lower() for t in name_tokens if len(t) >= 4]
    return any(tok in norm_text for tok in significant)


def extract(
    html: str,
    url: str,
    *,
    country: str,
    name_tokens: list[str],
) -> GenericExtraction:
    """Extrae contactos de una página arbitraria con scoring de proximidad."""
    tree = HTMLParser(html)
    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else None
    text_full = tree.body.text(separator=" ", strip=True) if tree.body else ""

    result = GenericExtraction(url=url, page_title=title)
    result.name_present = _name_in_text(text_full, name_tokens) if name_tokens else False

    # Emails desde texto + atributos href (mailto:)
    candidates: set[str] = set()
    for m in _EMAIL_RE.finditer(text_full):
        candidates.add(m.group(1))
    for node in tree.css('a[href^="mailto:"]'):
        href = node.attributes.get("href", "") or ""
        addr = href.removeprefix("mailto:").split("?")[0].strip()
        if addr:
            candidates.add(addr)

    for raw in candidates:
        if not _is_plausible_email(raw):
            continue
        proximity = _proximity_score(text_full, raw, name_tokens)
        result.emails.append((raw, proximity))

    # Teléfonos vía phonenumbers (más robusto que regex casero)
    seen_phones: set[str] = set()
    for match in phonenumbers.PhoneNumberMatcher(text_full, country):
        try:
            num = match.number
            if not phonenumbers.is_valid_number(num):
                continue
            e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
            if e164 in seen_phones:
                continue
            seen_phones.add(e164)
            # Reconstruye la subcadena para proximity
            snippet = text_full[max(0, match.start - 80): match.end + 80]
            proximity = _proximity_score(snippet, "", name_tokens) if name_tokens else 0.3
            result.phones.append((e164, proximity))
        except phonenumbers.NumberParseException:
            continue

    # Orden por proximity desc para que el caller tome el top-k confiable.
    result.emails.sort(key=lambda x: -x[1])
    result.phones.sort(key=lambda x: -x[1])
    return result


def _proximity_score(text: str, target: str, name_tokens: list[str]) -> float:
    """Proximidad entre la primera aparición de `target` y cualquier token del nombre.

    Si name_tokens vacío → 0.3 (señal débil sin contexto).
    Si target ausente → 0.0.
    Si name token aparece en ventana ±200 chars → 0.9.
    Si name token aparece en la misma página pero lejos → 0.5.
    """
    if not name_tokens:
        return 0.3
    norm = strip_accents(text).lower()
    significant = [strip_accents(t).lower() for t in name_tokens if len(t) >= 4]
    if not significant:
        return 0.3
    if target:
        idx = norm.find(target.lower())
        if idx < 0:
            return 0.0
        window = norm[max(0, idx - 200): idx + 200]
        if any(tok in window for tok in significant):
            return 0.9
    # name aparece en la página pero no cerca del target
    if any(tok in norm for tok in significant):
        return 0.5
    return 0.0
