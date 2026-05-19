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
    "medicina_general": ("MEDICINA GENERAL", "MEDICO GENERAL", "GENERAL PRACTICE"),
    "pediatria": ("PEDIATRIA", "PEDIATRÍA", "PEDIATRA", "MEDICO DE NIÑOS"),
    "medicina_interna": ("MEDICINA INTERNA", "INTERNISTA"),
    "medicina_familiar": ("MEDICINA FAMILIAR", "MEDICO DE FAMILIA", "MEDICINA DE FAMILIA"),
    "ginecologia": ("GINECOLOGIA", "GINECOLOGÍA", "GINECOLOGO", "GINECOLOGIA Y OBSTETRICIA",
                    "GINECOLOGÍA Y OBSTETRICIA", "OBSTETRICIA"),
    "otorrinolaringologia": ("OTORRINOLARINGOLOGIA", "OTORRINOLARINGOLOGÍA", "ORL", "OTORRINO"),
    "cardiologia": ("CARDIOLOGIA", "CARDIOLOGÍA", "CARDIOLOGO"),
    "dermatologia": ("DERMATOLOGIA", "DERMATOLOGÍA", "DERMATOLOGO", "DERMATOLOGÍA Y VENEREOLOGIA"),
    "neurologia": ("NEUROLOGIA", "NEUROLOGÍA", "NEUROLOGO"),
    "psiquiatria": ("PSIQUIATRIA", "PSIQUIATRÍA", "PSIQUIATRA"),
    "oftalmologia": ("OFTALMOLOGIA", "OFTALMOLOGÍA", "OFTALMOLOGO"),
    "traumatologia": ("TRAUMATOLOGIA", "TRAUMATOLOGÍA", "ORTOPEDIA", "TRAUMATOLOGO",
                      "TRAUMATOLOGIA Y ORTOPEDIA"),
    "urologia": ("UROLOGIA", "UROLOGÍA", "UROLOGO"),
    "endocrinologia": ("ENDOCRINOLOGIA", "ENDOCRINOLOGÍA", "ENDOCRINOLOGO"),
    "gastroenterologia": ("GASTROENTEROLOGIA", "GASTROENTEROLOGÍA", "GASTROENTEROLOGO"),
    "neumologia": ("NEUMOLOGIA", "NEUMOLOGÍA", "NEUMOLOGO", "NEUMÓLOGO"),
    "nefrologia": ("NEFROLOGIA", "NEFROLOGÍA", "NEFROLOGO"),
    "oncologia": ("ONCOLOGIA", "ONCOLOGÍA", "ONCOLOGO", "ONCOLOGÍA MÉDICA"),
    "hematologia": ("HEMATOLOGIA", "HEMATOLOGÍA", "HEMATOLOGO"),
    "reumatologia": ("REUMATOLOGIA", "REUMATOLOGÍA", "REUMATOLOGO"),
    "infectologia": ("INFECTOLOGIA", "INFECTOLOGÍA", "INFECTOLOGO"),
    "cirugia_general": ("CIRUGIA GENERAL", "CIRUGÍA GENERAL", "CIRUJANO GENERAL"),
    "cirugia_plastica": ("CIRUGIA PLASTICA", "CIRUGÍA PLÁSTICA", "CIRUJANO PLASTICO"),
    "cirugia_pediatrica": ("CIRUGIA PEDIATRICA", "CIRUGÍA PEDIÁTRICA"),
    "anestesiologia": ("ANESTESIOLOGIA", "ANESTESIOLOGÍA", "ANESTESIOLOGO", "ANESTESIA"),
    "radiologia": ("RADIOLOGIA", "RADIOLOGÍA", "RADIOLOGO", "IMAGENOLOGIA"),
    "patologia": ("PATOLOGIA", "PATOLOGÍA", "PATOLOGO"),
    "medicina_emergencia": ("MEDICINA DE EMERGENCIA", "EMERGENCIAS", "EMERGENCIOLOGO"),
    "medicina_critica": ("MEDICINA CRITICA", "MEDICINA CRÍTICA", "TERAPIA INTENSIVA", "INTENSIVISTA"),
    "geriatria": ("GERIATRIA", "GERIATRÍA", "GERIATRA"),
    "neonatologia": ("NEONATOLOGIA", "NEONATOLOGÍA", "NEONATOLOGO"),
    "fisiatria": ("FISIATRIA", "FISIATRÍA", "MEDICINA FISICA Y REHABILITACION"),
    "alergologia": ("ALERGOLOGIA", "ALERGOLOGÍA", "ALERGOLOGO",
                    "ALERGOLOGIA / INMUNOLOGIA", "ALERGOLOGIA E INMUNOLOGIA",
                    "INMUNOLOGIA", "INMUNOLOGÍA"),
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
