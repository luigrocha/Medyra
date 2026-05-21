"""Tests que prueban el valor día 1: limpieza correcta del Excel real."""
from __future__ import annotations

import pytest

from medyra.normalization.emails import normalize as normalize_email
from medyra.normalization.names import parse_latam_name, restore_special_chars
from medyra.normalization.phones import normalize as normalize_phone
from medyra.normalization.specialties import normalize_specialty


class TestNameParsing:
    def test_ñ_placeholder_recovery(self) -> None:
        assert restore_special_chars("ZU#IGA") == "ZUÑIGA"
        assert restore_special_chars("YONG PI#AR BERNAL") == "YONG PIÑAR BERNAL"

    def test_two_surnames_plus_given(self) -> None:
        p = parse_latam_name("VARGAS ARREA ROLANDO GERARDO")
        assert p.family_name_1 == "VARGAS"
        assert p.family_name_2 == "ARREA"
        assert p.given_name == "ROLANDO GERARDO"

    def test_compound_given_with_particles(self) -> None:
        p = parse_latam_name("YOCK RUIZ MARIA DE LOS ANGELES")
        assert p.family_name_1 == "YOCK"
        assert p.family_name_2 == "RUIZ"
        assert p.given_name == "MARIA DE LOS ANGELES"

    def test_ñ_recovered_in_parsing(self) -> None:
        p = parse_latam_name("YONG PI#AR BERNAL")
        assert p.family_name_1 == "YONG"
        assert p.family_name_2 == "PIÑAR"
        assert p.given_name == "BERNAL"

    def test_single_token(self) -> None:
        p = parse_latam_name("PEREZ")
        assert p.family_name_1 == "PEREZ"
        assert p.family_name_2 is None
        assert p.given_name == ""

    def test_internal_newlines_and_whitespace(self) -> None:
        p = parse_latam_name("  VEGA   RODRIGUEZ\nADRIANA  ")
        assert p.family_name_1 == "VEGA"
        assert p.family_name_2 == "RODRIGUEZ"
        assert p.given_name == "ADRIANA"

    def test_display_name_format(self) -> None:
        p = parse_latam_name("VARGAS ARREA ROLANDO GERARDO")
        assert p.display_name == "Vargas Arrea, Rolando Gerardo"


class TestPhones:
    def test_cr_mobile_with_dash(self) -> None:
        n = normalize_phone("506 8827-1060", "CR")
        assert n is not None and n.e164 == "+50688271060"
        assert n.line_type == "mobile"

    def test_cr_landline(self) -> None:
        n = normalize_phone("506 2220 4925", "CR")
        assert n is not None and n.e164 == "+50622204925"

    def test_invalid(self) -> None:
        assert normalize_phone("123", "CR") is None

    def test_cr_phone_with_plus_already(self) -> None:
        n = normalize_phone("+50688271060", "CR")
        assert n is not None and n.e164 == "+50688271060"


class TestEmails:
    def test_strip_whitespace_and_newlines(self) -> None:
        n = normalize_email("\navegaro@gmail.com")
        assert n is not None and n.address == "avegaro@gmail.com"

    def test_quoted_email(self) -> None:
        n = normalize_email('"pediamax.villalobos@gmail.com\n"')
        assert n is not None and n.address == "pediamax.villalobos@gmail.com"

    def test_freemail_flag(self) -> None:
        n = normalize_email("foo@gmail.com")
        assert n is not None and n.is_freemail

    def test_detect_domain_typo(self) -> None:
        # `clinicabiblia.com` (con typo) vs `clinicabiblica.com` (real)
        n = normalize_email("luisvilchez@clinicabiblia.com")
        assert n is not None
        assert n.looks_typoed
        assert n.suggested_domain == "clinicabiblica.com"

    def test_invalid(self) -> None:
        assert normalize_email("no-arroba") is None


class TestSpecialties:
    @pytest.mark.parametrize("raw,expected", [
        ("PEDIATRIA", "pediatria"),
        ("Pediatría", "pediatria"),
        ("MEDICINA INTERNA", "medicina_interna"),
        ("OTORRINOLARINGOLOGIA", "otorrinolaringologia"),
        ("ORL", "otorrinolaringologia"),
        ("desconocida", None),
    ])
    def test_normalize(self, raw: str, expected: str | None) -> None:
        assert normalize_specialty(raw) == expected
