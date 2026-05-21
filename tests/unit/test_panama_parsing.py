"""Tests del modo `given_first` (formato Panama)."""
from __future__ import annotations

import pytest

from medyra.normalization.names import parse_latam_name


@pytest.mark.parametrize("raw,fam1,fam2,given", [
    ("Marta Ceballos Rodriguez",            "CEBALLOS",  "RODRIGUEZ", "MARTA"),
    ("Dario Antonio Vallarino De Sanctis",  "VALLARINO", "DE SANCTIS", "DARIO ANTONIO"),
    ("Antonio Aversa",                       "AVERSA",   None,         "ANTONIO"),
    ("Caridad Samaniego",                    "SAMANIEGO", None,        "CARIDAD"),
    ("Fidel González",                       "GONZÁLEZ",  None,        "FIDEL"),
    ("Ashley Aysel Carrillo Prado",          "CARRILLO", "PRADO",      "ASHLEY AYSEL"),
])
def test_given_first_parser(raw: str, fam1: str, fam2: str | None, given: str) -> None:
    p = parse_latam_name(raw, name_format="given_first")
    assert p.family_name_1 == fam1
    assert p.family_name_2 == fam2
    assert p.given_name == given


def test_given_first_with_newline_prefix() -> None:
    p = parse_latam_name("\nMarta Ceballos Rodriguez", name_format="given_first")
    assert p.family_name_1 == "CEBALLOS"
    assert p.family_name_2 == "RODRIGUEZ"
    assert p.given_name == "MARTA"


def test_family_first_still_works_default() -> None:
    p = parse_latam_name("VARGAS BRIZUELA JOSE RICARDO")
    assert p.family_name_1 == "VARGAS"
    assert p.family_name_2 == "BRIZUELA"
    assert p.given_name == "JOSE RICARDO"
