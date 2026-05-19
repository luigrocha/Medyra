"""Normalización de nombres latinoamericanos.

Aborda los patrones observados en los Excel de origen:
  - Encoding corrupto: `#` reemplaza `Ñ` (e.g. `ZU#IGA` → `ZUÑIGA`).
  - Whitespace embebido y newlines.
  - Mayúsculas inconsistentes.
  - Formato CR/PA: `APELLIDO1 APELLIDO2 NOMBRE1 NOMBRE2[...]`.
  - Particles compuestas en nombres: `DE LOS`, `DE LA`, `DEL`, `DA`.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from unidecode import unidecode

# `#` aparece donde había `Ñ` por corrupción de encoding Latin-1 → ASCII.
# Es seguro: el carácter `#` no aparece legítimamente en nombres propios.
_NEN_PLACEHOLDER = re.compile(r"#")

# Newlines, tabs y múltiples espacios → un espacio.
_WS = re.compile(r"\s+")

# Particles que NO son apellidos por sí solos en nombres compuestos.
_PARTICLES = {"DE", "DEL", "DE LA", "DE LAS", "DE LOS", "DA", "DO", "VAN", "VON", "LA", "LE", "Y"}

# Nombres femeninos comunes que usan particles ("María de los Ángeles", "Ana del Carmen").
_FEMALE_COMPOUND_HINTS = {"MARIA", "MARÍA", "ANA", "JUANA", "ROSA", "ANGELA", "ÁNGELA"}


def restore_special_chars(s: str) -> str:
    """Recupera Ñ desde el placeholder `#`. No agresivo: solo ese carácter."""
    return _NEN_PLACEHOLDER.sub("Ñ", s)


def clean_whitespace(s: str) -> str:
    """Colapsa whitespace, quita newlines embebidos, trim."""
    return _WS.sub(" ", s).strip()


def strip_accents(s: str) -> str:
    """Quita acentos para matching/blocking. Conserva Ñ → N intencionalmente."""
    return unidecode(s)


def base_clean(s: str) -> str:
    """Pipeline mínimo aplicable a cualquier nombre crudo."""
    if not s:
        return ""
    s = restore_special_chars(s)
    s = clean_whitespace(s)
    return s.upper()


@dataclass(frozen=True)
class NameParts:
    """Descomposición canónica de un nombre LATAM.

    - family_name_1, family_name_2: primer y segundo apellido.
    - given_name: nombres de pila concatenados (puede contener particles).
    - full_name: forma canónica reconstruida.
    - tokens_raw: tokens originales tras limpieza.
    """
    family_name_1: str
    family_name_2: str | None
    given_name: str
    full_name: str
    tokens_raw: tuple[str, ...]

    @property
    def display_name(self) -> str:
        """`Pérez González, Juan Carlos` para UI/exports humanos."""
        family = self.family_name_1
        if self.family_name_2:
            family = f"{family} {self.family_name_2}"
        return f"{family}, {self.given_name}".title()


def parse_latam_name(raw: str) -> NameParts:
    """Parsea un nombre en formato `APELLIDO1 APELLIDO2 NOMBRE [NOMBRE2 ...]`.

    Heurística: los 2 primeros tokens son apellidos. Si solo hay 2 tokens, el
    primero es apellido y el segundo nombre. Particles (`DE LOS`, etc.) en la
    parte de nombres se mantienen unidas al token siguiente.

    Casos límite manejados:
        "YOCK RUIZ MARIA DE LOS ANGELES"      → fam1=YOCK, fam2=RUIZ, given=MARIA DE LOS ANGELES
        "VARGAS ARREA ROLANDO GERARDO"        → fam1=VARGAS, fam2=ARREA, given=ROLANDO GERARDO
        "YONG PIÑAR BERNAL"                   → fam1=YONG, fam2=PIÑAR, given=BERNAL
        "PEREZ JUAN"                          → fam1=PEREZ, fam2=None, given=JUAN
        "PEREZ"                               → fam1=PEREZ, fam2=None, given=""
    """
    cleaned = base_clean(raw)
    tokens = tuple(t for t in cleaned.split(" ") if t)
    if not tokens:
        return NameParts("", None, "", "", ())

    if len(tokens) == 1:
        return NameParts(tokens[0], None, "", tokens[0], tokens)
    if len(tokens) == 2:
        return NameParts(tokens[0], None, tokens[1], cleaned, tokens)

    fam1, fam2, *rest = tokens
    given = _reassemble_given(tuple(rest))
    return NameParts(fam1, fam2, given, cleaned, tokens)


def _reassemble_given(tokens: tuple[str, ...]) -> str:
    """Concatena tokens del nombre de pila preservando particles compuestos."""
    if not tokens:
        return ""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        # `DE LOS` / `DE LAS` / `DE LA` → toma 3 tokens
        if t == "DE" and i + 2 < len(tokens) and tokens[i + 1] in {"LOS", "LAS", "LA"}:
            chunk = f"{t} {tokens[i + 1]} {tokens[i + 2]}"
            out.append(chunk)
            i += 3
        # `DEL X` / `DE X` / `LA X` → 2 tokens
        elif t in {"DEL", "DE", "LA", "DA", "DO", "VAN", "VON"} and i + 1 < len(tokens):
            out.append(f"{t} {tokens[i + 1]}")
            i += 2
        else:
            out.append(t)
            i += 1
    return " ".join(out)


def block_key(p: NameParts, country: str, specialty: str | None = None) -> str:
    """Clave de blocking para entity resolution (versión barata)."""
    fam = strip_accents(p.family_name_1).lower()
    spec = (specialty or "").lower()
    return f"{country}|{spec}|{fam[:3]}"


def fuzzy_tokens(p: NameParts) -> list[str]:
    """Tokens normalizados (sin acentos, lower) para comparación fuzzy."""
    full = f"{p.family_name_1} {p.family_name_2 or ''} {p.given_name}"
    return [t for t in strip_accents(full).lower().split(" ") if t]


def initials(p: NameParts) -> str:
    """`R.V.A.` para `Rodríguez Vega Adriana`."""
    parts = [p.family_name_1, p.family_name_2 or "", *p.given_name.split(" ")]
    return ".".join(s[:1] for s in parts if s) + "."
