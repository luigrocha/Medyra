"""Normalización de nombres latinoamericanos.

Aborda los patrones observados en los Excel de origen:
  - Encoding corrupto: `#` reemplaza `Ñ` (e.g. `ZU#IGA` → `ZUÑIGA`).
  - Whitespace embebido y newlines.
  - Mayúsculas inconsistentes.
  - Particles: `DE LOS`, `DE LA`, `DEL`, `DA`, `DE SANCTIS`, etc.

Soporta dos formatos:
  - `family_first`  (default, usado en CR y Ecuador):
        `APELLIDO1 APELLIDO2 NOMBRE [NOMBRE...]`
        ej. `VARGAS BRIZUELA JOSE RICARDO`
  - `given_first`  (usado en Panama):
        `Nombre [Nombre] Apellido1 [Apellido2]`
        ej. `Marta Ceballos Rodriguez` o `Dario Antonio Vallarino De Sanctis`
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from unidecode import unidecode

# `#` aparece donde había `Ñ` por corrupción de encoding Latin-1 → ASCII.
# Es seguro: el carácter `#` no aparece legítimamente en nombres propios.
_NEN_PLACEHOLDER = re.compile(r"#")

# Newlines, tabs y múltiples espacios → un espacio.
_WS = re.compile(r"\s+")

# Particles que NO se sostienen como token solo. Una partícula + el token siguiente forman
# una unidad léxica (parte de un apellido compuesto o de un nombre compuesto).
_PARTICLES_SINGLE = {"DEL", "DE", "DA", "DO", "VAN", "VON", "LA", "LE", "Y", "SAN", "SANTA"}
# Particles de dos palabras: DE LA, DE LOS, DE LAS, DE SANTA, etc.
_PARTICLES_DOUBLE_HEAD = {"DE"}
_PARTICLES_DOUBLE_TAIL = {"LA", "LAS", "LOS", "SANTA", "SAN"}


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


def parse_latam_name(raw: str, name_format: str = "family_first") -> NameParts:
    """Parsea un nombre latinoamericano.

    `name_format`:
      - `"family_first"`  (default): `APELLIDO1 APELLIDO2 NOMBRE [...]`
            Casos:
              "YOCK RUIZ MARIA DE LOS ANGELES"  → fam1=YOCK, fam2=RUIZ, given=MARIA DE LOS ANGELES
              "VARGAS ARREA ROLANDO GERARDO"    → fam1=VARGAS, fam2=ARREA, given=ROLANDO GERARDO
              "YONG PIÑAR BERNAL"               → fam1=YONG, fam2=PIÑAR, given=BERNAL
              "PEREZ JUAN"                      → fam1=PEREZ, fam2=None, given=JUAN
      - `"given_first"`: `Nombre [Nombre] Apellido1 [Apellido2]`
            Casos:
              "Marta Ceballos Rodriguez"        → fam1=CEBALLOS, fam2=RODRIGUEZ, given=MARTA
              "Dario Antonio Vallarino De Sanctis"
                                                → fam1=VALLARINO, fam2=DE SANCTIS, given=DARIO ANTONIO
              "Fidel González"                  → fam1=GONZALEZ, fam2=None, given=FIDEL
    """
    cleaned = base_clean(raw)
    tokens = tuple(t for t in cleaned.split(" ") if t)
    if not tokens:
        return NameParts("", None, "", "", ())
    if name_format == "given_first":
        return _parse_given_first(tokens, cleaned)
    return _parse_family_first(tokens, cleaned)


def _parse_family_first(tokens: tuple[str, ...], cleaned: str) -> NameParts:
    if len(tokens) == 1:
        return NameParts(tokens[0], None, "", tokens[0], tokens)
    if len(tokens) == 2:
        return NameParts(tokens[0], None, tokens[1], cleaned, tokens)
    fam1, fam2, *rest = tokens
    given = _reassemble(tuple(rest))
    return NameParts(fam1, fam2, given, cleaned, tokens)


def _parse_given_first(tokens: tuple[str, ...], cleaned: str) -> NameParts:
    """Para formato Panama: nombres al inicio, apellidos al final.

    Recorre la cola identificando unidades léxicas (con particles) y toma las
    últimas 1-2 unidades como apellidos. El resto al inicio son nombres.
    """
    if len(tokens) == 1:
        return NameParts(tokens[0], None, "", tokens[0], tokens)
    units = _group_tokens(tokens)
    if len(units) == 1:
        return NameParts(units[0], None, "", cleaned, tokens)
    if len(units) == 2:
        # 2 unidades: la última es apellido, la primera es nombre.
        return NameParts(units[1], None, units[0], cleaned, tokens)
    # ≥3 unidades: últimas 2 son apellidos, el resto son nombres.
    given = " ".join(units[:-2])
    return NameParts(units[-2], units[-1], given, cleaned, tokens)


def _group_tokens(tokens: tuple[str, ...]) -> list[str]:
    """Agrupa tokens consecutivos cuando hay particles. Útil para given_first.

    Ejemplos:
        ["VALLARINO", "DE", "SANCTIS"]      → ["VALLARINO", "DE SANCTIS"]
        ["DE", "LA", "ROSA", "MARTINEZ"]    → ["DE LA ROSA", "MARTINEZ"]
        ["MARIA", "DEL", "PILAR"]           → ["MARIA", "DEL PILAR"]
    """
    out: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        t = tokens[i]
        # Partícula doble: DE LA / DE LOS / DE LAS / DE SAN / DE SANTA
        if t in _PARTICLES_DOUBLE_HEAD and i + 2 < n and tokens[i + 1] in _PARTICLES_DOUBLE_TAIL:
            out.append(f"{t} {tokens[i + 1]} {tokens[i + 2]}")
            i += 3
            continue
        # Partícula simple: DE X / DEL X / DA X / etc.
        if t in _PARTICLES_SINGLE and i + 1 < n:
            out.append(f"{t} {tokens[i + 1]}")
            i += 2
            continue
        out.append(t)
        i += 1
    return out


def _reassemble(tokens: tuple[str, ...]) -> str:
    """Devuelve los tokens unidos manteniendo particles compuestos (para given names)."""
    return " ".join(_group_tokens(tokens))


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
