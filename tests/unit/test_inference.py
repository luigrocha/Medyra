"""Tests del Inference & Confidence Engine."""
from __future__ import annotations

import pytest

from medintel.inference.confidence import (
    SIGNAL_LOGODDS,
    score,
    threshold_bucket,
)
from medintel.inference.email_patterns import EmailHypothesis, generate, signals_for
from medintel.inference.identity import (
    PhysicianCandidate,
    block_keys,
    cluster,
    p_match,
    pair_signals,
)
from medintel.normalization.names import parse_latam_name


class TestConfidence:
    def test_higher_tier_yields_higher_score(self) -> None:
        low = score({"source_tier_3", "single_source"}).probability
        high = score({"source_tier_1", "agreement_3plus_sources", "mx_ok"}).probability
        assert high > low

    def test_contradiction_drops_score(self) -> None:
        base = score({"source_tier_2", "mx_ok"}).probability
        contradicted = score({"source_tier_2", "mx_ok", "contradicted_by_tier1"}).probability
        assert contradicted < base

    def test_threshold_bucket(self) -> None:
        assert threshold_bucket(0.90) == "auto_publish"
        assert threshold_bucket(0.70) == "human_review"
        assert threshold_bucket(0.40) == "hypothesis"

    def test_inferred_penalizes(self) -> None:
        observed = score({"source_tier_2", "mx_ok"}).probability
        inferred = score({"source_tier_2", "mx_ok", "is_inferred_from_pattern"}).probability
        assert inferred < observed

    def test_unknown_signals_ignored(self) -> None:
        r = score({"source_tier_2", "bogus_signal"})
        assert "bogus_signal" not in r.contributions
        assert "source_tier_2" in r.contributions

    def test_all_signals_in_logodds_table_are_finite(self) -> None:
        assert all(isinstance(v, (int, float)) for v in SIGNAL_LOGODDS.values())


class TestEmailPatterns:
    def test_generates_for_known_pattern(self) -> None:
        name = parse_latam_name("VARGAS BRIZUELA JOSE RICARDO")
        out = generate(name, "clinicabiblica.com")
        addrs = {h.address for h in out}
        assert "jose.vargas@clinicabiblica.com" in addrs
        assert "jvargas@clinicabiblica.com" in addrs

    def test_returns_sorted_by_prior_descending(self) -> None:
        name = parse_latam_name("VARGAS BRIZUELA JOSE RICARDO")
        out = generate(name, "clinicabiblica.com")
        priors = [h.prior for h in out]
        assert priors == sorted(priors, reverse=True)

    def test_empty_domain_returns_empty(self) -> None:
        name = parse_latam_name("VARGAS BRIZUELA JOSE")
        assert generate(name, "") == []

    def test_signals_for_hypothesis(self) -> None:
        h = EmailHypothesis("jose.vargas@x.com", "{first}.{last}", 0.34, mx_ok=True, catch_all=False)
        sigs = signals_for(h, source_tier=2)
        assert "is_inferred_from_pattern" in sigs
        assert "mx_ok" in sigs
        assert "source_tier_2" in sigs


class TestIdentity:
    def _cand(self, id_: str, name: str, **kw) -> PhysicianCandidate:
        return PhysicianCandidate(id=id_, name=parse_latam_name(name), country=kw.pop("country", "CR"), **kw)

    def test_same_name_probable_match_without_embeddings(self) -> None:
        # Sin embeddings cargados el modelo es conservador a propósito:
        # match perfecto + mismo país/esp cae en bucket "human_review", no auto.
        a = self._cand("1", "VARGAS BRIZUELA JOSE RICARDO", specialty="pediatria")
        b = self._cand("2", "VARGAS BRIZUELA JOSE RICARDO", specialty="pediatria")
        p = p_match(pair_signals(a, b))
        assert 0.65 < p < 0.90

    def test_different_specialty_lowers_match(self) -> None:
        # Mismo nombre + especialidad distinta → probablemente personas distintas.
        a = self._cand("1", "VARGAS BRIZUELA JOSE RICARDO", specialty="pediatria")
        b = self._cand("2", "VARGAS BRIZUELA JOSE RICARDO", specialty="cardiologia")
        p = p_match(pair_signals(a, b))
        assert p < 0.55

    def test_completely_different(self) -> None:
        a = self._cand("1", "VARGAS BRIZUELA JOSE", specialty="pediatria")
        b = self._cand("2", "MORALES VILLALOBOS MAX", specialty="cardiologia")
        assert p_match(pair_signals(a, b)) < 0.1

    def test_shared_phone_strong_signal(self) -> None:
        a = self._cand("1", "PEREZ GOMEZ JUAN",  phones={"+50612345678"})
        b = self._cand("2", "PEREZ GOMEZ JUAN A", phones={"+50612345678"})
        p = p_match(pair_signals(a, b))
        assert p > 0.95

    def test_blocking_overlaps_similar_names(self) -> None:
        a = self._cand("1", "VARGAS BRIZUELA JOSE", specialty="pediatria")
        b = self._cand("2", "VARGAS ARREA ROLANDO", specialty="pediatria")
        assert set(block_keys(a)) & set(block_keys(b))

    def test_cluster_union_find(self) -> None:
        pairs = [("a", "b", 0.9), ("b", "c", 0.9), ("d", "e", 0.4)]
        result = cluster(pairs, threshold=0.85)
        # a, b, c terminan en el mismo cluster; d, e separados
        assert result["a"] == result["b"] == result["c"]
        assert "d" not in result or result.get("d") != result.get("a")


@pytest.mark.parametrize("raw_name,domain,expected_address", [
    ("VARGAS BRIZUELA JOSE RICARDO", "clinicabiblica.com", "jose.vargas@clinicabiblica.com"),
    ("VILLALOBOS MORALES MAX",       "gmail.com",          "max.villalobos@gmail.com"),
])
def test_email_inference_integration(raw_name: str, domain: str, expected_address: str) -> None:
    """E2E mini: parse → generate → señales → score."""
    name = parse_latam_name(raw_name)
    hyps = generate(name, domain)
    matching = [h for h in hyps if h.address == expected_address]
    assert matching, f"Patrón esperado no generado: {expected_address}"

    h = matching[0]
    h.mx_ok = True  # simulado para test
    sigs = signals_for(h, source_tier=2)
    sigs.add("email_domain_matches_affiliation")
    result = score(sigs)
    assert 0.5 < result.probability < 0.85  # inferido + MX = probable, no certeza
