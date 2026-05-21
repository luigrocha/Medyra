"""Identity resolution / deduplicación de médicos.

Pipeline:
    BLOCKING  (reduce el espacio de comparación)
       ↓
    PAIR_SCORE (señales por par)
       ↓
    P_MATCH    (regresión logística; calibrable con `tests/fixtures/labeled_pairs.json`)
       ↓
    CLUSTER   (union-find sobre aristas con p >= threshold)
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import exp

from rapidfuzz import fuzz

from medyra.normalization.names import NameParts, strip_accents


@dataclass
class PhysicianCandidate:
    """Vista plana de un médico para matching. Construida desde DB o staging."""
    id: str
    name: NameParts
    country: str
    specialty: str | None = None
    cities: set[str] = field(default_factory=set)
    universities: set[str] = field(default_factory=set)
    hospitals: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)       # E.164
    emails: set[str] = field(default_factory=set)       # normalized
    name_embedding: list[float] | None = None


def block_keys(c: PhysicianCandidate) -> list[str]:
    """Múltiples claves: queremos recall alto en blocking, precisión vendrá del scorer."""
    last1 = strip_accents(c.name.family_name_1).lower()
    last2 = strip_accents(c.name.family_name_2 or "").lower()
    first = strip_accents(c.name.given_name.split(" ")[0] if c.name.given_name else "").lower()
    spec = (c.specialty or "").lower()
    keys = [
        f"{c.country}|{spec}|{last1[:3]}",
        f"{c.country}|{last1}",
        f"{c.country}|{last1}|{first[:1]}",
    ]
    if last2:
        keys.append(f"{c.country}|{last2[:3]}|{first[:1]}")
    return keys


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def pair_signals(a: PhysicianCandidate, b: PhysicianCandidate) -> dict[str, float]:
    name_a = strip_accents(a.name.full_name).lower()
    name_b = strip_accents(b.name.full_name).lower()
    sigs = {
        "name_token_sort": fuzz.token_sort_ratio(name_a, name_b) / 100,
        "name_token_set":  fuzz.token_set_ratio(name_a, name_b) / 100,
        "specialty_match": 1.0 if (a.specialty and a.specialty == b.specialty) else 0.0,
        "city_match":      _jaccard(a.cities, b.cities),
        "edu_overlap":     _jaccard(a.universities, b.universities),
        "affil_overlap":   _jaccard(a.hospitals, b.hospitals),
        "phone_overlap":   1.0 if a.phones & b.phones else 0.0,
        "email_overlap":   1.0 if a.emails & b.emails else 0.0,
    }
    if a.name_embedding and b.name_embedding:
        sigs["name_embed_cos"] = _cosine(a.name_embedding, b.name_embedding)
    return sigs


def _cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=True))
    nu = sum(x * x for x in u) ** 0.5
    nv = sum(x * x for x in v) ** 0.5
    return dot / (nu * nv + 1e-9)


# Coeficientes iniciales — recalibrar con tu Excel + tests/fixtures/labeled_pairs.json
COEFS = {
    "name_token_sort": 2.1,
    "name_token_set":  1.1,
    "name_embed_cos":  2.8,
    "specialty_match": 1.5,
    "city_match":      1.0,
    "edu_overlap":     1.2,
    "affil_overlap":   1.4,
    "phone_overlap":   4.0,
    "email_overlap":   5.0,
}
BIAS = -3.5


def p_match(sigs: dict[str, float]) -> float:
    z = BIAS + sum(COEFS[k] * sigs[k] for k in COEFS if k in sigs)
    return 1 / (1 + exp(-z))


def cluster(pairs: list[tuple[str, str, float]], threshold: float = 0.85) -> dict[str, str]:
    """Union-find sobre aristas (id_a, id_b, p). Devuelve id → cluster_repr."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b, p in pairs:
        if p >= threshold:
            union(a, b)
    return {x: find(x) for x in parent}


def candidate_pairs(candidates: list[PhysicianCandidate]) -> list[tuple[PhysicianCandidate, PhysicianCandidate]]:
    """Genera pares dentro del mismo block. Evita duplicados (a,b) y (b,a)."""
    by_block: dict[str, list[PhysicianCandidate]] = defaultdict(list)
    for c in candidates:
        for k in block_keys(c):
            by_block[k].append(c)
    seen: set[tuple[str, str]] = set()
    pairs: list[tuple[PhysicianCandidate, PhysicianCandidate]] = []
    for bucket in by_block.values():
        for i, a in enumerate(bucket):
            for b in bucket[i + 1:]:
                key = tuple(sorted([a.id, b.id]))
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((a, b))
    return pairs
