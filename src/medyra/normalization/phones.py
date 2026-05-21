"""Normalización y clasificación de teléfonos para CR y PA.

Usa la librería `phonenumbers` (port de libphonenumber de Google) que es
canónica para parsing/validación. Adicionalmente clasifica el tipo
(mobile/landline/voip) y detecta números institucionales repetidos.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumber, PhoneNumberType

_NOISE = re.compile(r"[^\d+]")

_TYPE_LABEL: dict[PhoneNumberType, str] = {
    PhoneNumberType.MOBILE: "mobile",
    PhoneNumberType.FIXED_LINE: "landline",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "mobile_or_landline",
    PhoneNumberType.TOLL_FREE: "tollfree",
    PhoneNumberType.VOIP: "voip",
    PhoneNumberType.PREMIUM_RATE: "premium",
    PhoneNumberType.UNKNOWN: "unknown",
}


@dataclass(frozen=True)
class PhoneNormalized:
    e164: str
    national: str
    country: str
    line_type: str
    valid: bool


def normalize(raw: str, country: str) -> PhoneNormalized | None:
    """Devuelve un teléfono en E.164 o None si es inválido para el país dado.

    `country` es ISO-3166-alpha-2 (CR, PA, MX, ...).
    """
    if not raw:
        return None
    cleaned = _NOISE.sub("", raw)
    # Muchos datos vienen como "506 8827-1060" — añadir + si empieza por código.
    if cleaned.startswith(("506", "507")) and not cleaned.startswith("+"):
        cleaned = "+" + cleaned
    try:
        num: PhoneNumber = phonenumbers.parse(cleaned, country)
    except NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num):
        return None
    e164 = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.E164)
    national = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.NATIONAL)
    label = _TYPE_LABEL.get(phonenumbers.number_type(num), "unknown")
    region = phonenumbers.region_code_for_number(num) or country
    return PhoneNormalized(e164=e164, national=national, country=region, line_type=label, valid=True)


def detect_institutional(claims_phones: list[str], threshold: int = 3) -> set[str]:
    """Devuelve teléfonos que aparecen como contacto de >= `threshold` médicos.

    Esos números son centrales de clínica/hospital, no personales.
    """
    counts = Counter(claims_phones)
    return {p for p, c in counts.items() if c >= threshold}
