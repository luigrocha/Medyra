"""Confidence Engine basado en log-odds.

Cada señal aporta un "boost" en log-odds. Suma de boosts → sigmoid → probabilidad.
La ventaja vs sumas ponderadas: independencia local entre señales se modela
correctamente; calibrable con regresión logística sobre ground truth.

Los pesos iniciales son priors razonables; se recalibran con `calibrate()`
una vez que tengas >=200 claims etiquetados como correcto/incorrecto.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, log


def logit(p: float) -> float:
    p = max(min(p, 0.9999), 0.0001)
    return log(p / (1 - p))


def sigmoid(z: float) -> float:
    return 1 / (1 + exp(-z))


# Boosts en log-odds (delta z). Calibrados como priors iniciales razonables.
SIGNAL_LOGODDS: dict[str, float] = {
    # Tier de la fuente
    "source_tier_1": +1.6,                    # colegio médico oficial
    "source_tier_2": +0.6,                    # Doctoralia, TopDoctors
    "source_tier_3": +0.1,                    # red social, foro
    # Diversidad / cross-source agreement
    "agreement_2_sources": +0.9,
    "agreement_3plus_sources": +1.5,
    "single_source": -0.3,
    # Validación técnica de email
    "mx_ok": +0.6,
    "catch_all_domain": -0.4,
    "external_validator_pass": +1.3,
    "external_validator_fail": -2.0,
    # Validación técnica de teléfono
    "phonenumbers_valid": +0.7,
    "phone_region_match": +0.3,
    "phone_type_mobile": +0.2,
    # Validación cruzada
    "email_domain_matches_affiliation": +1.0,
    "phone_matches_known_clinic_phone": -0.5,
    # Recencia
    "verified_within_180d": +0.4,
    "verified_within_365d": +0.1,
    "stale_over_2y": -0.6,
    # Inferencia (penaliza certeza si el dato fue inferido)
    "is_inferred_from_pattern": -0.7,
    "is_inferred_from_specialty_signal": -0.5,
    "is_inferred_from_clinic_central": -0.4,
    # Conflictos
    "contradicted_by_tier1": -2.5,
    "contradicted_by_tier2": -1.0,
}

DEFAULT_PRIOR = 0.30


@dataclass
class ConfidenceResult:
    probability: float
    contributions: dict[str, float] = field(default_factory=dict)
    prior_used: float = DEFAULT_PRIOR

    def to_evidence_strength(self) -> dict:
        return {
            "prior": self.prior_used,
            "signals": self.contributions,
            "score": round(self.probability, 4),
        }


def score(signals: set[str], prior: float = DEFAULT_PRIOR) -> ConfidenceResult:
    """Calcula confianza a partir de un conjunto de señales activas."""
    z = logit(prior)
    contributions: dict[str, float] = {}
    for s in signals:
        delta = SIGNAL_LOGODDS.get(s)
        if delta is None or delta == 0.0:
            continue
        z += delta
        contributions[s] = round(delta, 3)
    return ConfidenceResult(probability=sigmoid(z), contributions=contributions, prior_used=prior)


def threshold_bucket(p: float) -> str:
    """Mapea probabilidad → bucket de routing.

    - auto_publish:  >= 0.85
    - human_review:  0.55 <= p < 0.85
    - hypothesis:    < 0.55  (no se exporta como hecho)
    """
    if p >= 0.85:
        return "auto_publish"
    if p >= 0.55:
        return "human_review"
    return "hypothesis"


def calibrate(labeled: list[tuple[set[str], bool]]) -> dict[str, float]:
    """Recalibra `SIGNAL_LOGODDS` desde ground truth.

    `labeled`: lista de (signals_activas, es_correcto).
    Usa regresión logística (sklearn) o implementación manual con GD.
    Stub: implementar cuando haya >=200 etiquetas. Por ahora, devuelve copia.
    """
    if len(labeled) < 200:
        raise ValueError("Necesitas al menos 200 etiquetas para calibración estable.")
    # Cuando se conecte: from sklearn.linear_model import LogisticRegression
    # X = sparse matrix [n_samples, n_signals]; y = [is_correct]
    # Coef → reemplaza SIGNAL_LOGODDS
    raise NotImplementedError("Calibración por GD/sklearn — pendiente.")
