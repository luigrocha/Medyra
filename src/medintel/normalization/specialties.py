"""Mapeo de especialidades médicas a códigos canónicos.

La fuente de verdad final es la tabla `specialty` + `specialty_synonym`
en Postgres. Este módulo es la versión in-memory para uso desde scripts
y tests sin DB.
"""
from __future__ import annotations

from rapidfuzz import process

from medintel.normalization.names import strip_accents

# code → variantes observadas (mayúsculas, sin acentos opcionales)
SPECIALTY_VARIANTS: dict[str, tuple[str, ...]] = {
    "pediatria": ("PEDIATRIA", "PEDIATRÍA", "PEDIATRA", "MEDICO DE NIÑOS"),
    "medicina_interna": ("MEDICINA INTERNA", "INTERNISTA"),
    "otorrinolaringologia": ("OTORRINOLARINGOLOGIA", "OTORRINOLARINGOLOGÍA", "ORL", "OTORRINO"),
    "ginecologia": ("GINECOLOGIA", "GINECOLOGÍA", "GINECOLOGO", "GINECOLOGIA Y OBSTETRICIA"),
    "cardiologia": ("CARDIOLOGIA", "CARDIOLOGÍA", "CARDIOLOGO"),
    "dermatologia": ("DERMATOLOGIA", "DERMATOLOGÍA", "DERMATOLOGO"),
    "neurologia": ("NEUROLOGIA", "NEUROLOGÍA", "NEUROLOGO"),
    "psiquiatria": ("PSIQUIATRIA", "PSIQUIATRÍA", "PSIQUIATRA"),
    "oftalmologia": ("OFTALMOLOGIA", "OFTALMOLOGÍA", "OFTALMOLOGO"),
    "traumatologia": ("TRAUMATOLOGIA", "TRAUMATOLOGÍA", "ORTOPEDIA", "TRAUMATOLOGO"),
}

# Índice plano accent-stripped → code
_INDEX: dict[str, str] = {
    strip_accents(v).upper(): code
    for code, variants in SPECIALTY_VARIANTS.items()
    for v in variants
}


def normalize_specialty(raw: str) -> str | None:
    """Devuelve el code canónico o None si no hay match razonable."""
    if not raw:
        return None
    key = strip_accents(raw).upper().strip()
    if key in _INDEX:
        return _INDEX[key]
    match = process.extractOne(key, list(_INDEX.keys()), score_cutoff=88)
    return _INDEX[match[0]] if match else None
