#!/usr/bin/env python3
"""
REPS Colombia — emails oficiales para la base de ginecólogos.

Qué es REPS: Registro Especial de Prestadores de Servicios de Salud (MinSalud).
Dataset abierto `c36g-9fc2` en datos.gov.co — 76.821 prestadores, de los cuales
54.657 son **Profesional Independiente** (médicos que se habilitan a su nombre)
y el 99,99% trae `email_prestador` y `telefonoprestador`.

Por qué importa: Doctoralia da nombre + teléfono de consultorio pero NUNCA email.
DDG/Bing se bloquean a escala (verificado: DDG tira challenge tras ~2 queries).
REPS es la fuente oficial, pública, descargable por API y sin rate limit real.

Qué NO tiene REPS: la especialidad del profesional. Por eso el flujo es
    ginecólogos de Doctoralia  ──match por nombre──>  REPS  ──>  email oficial

El matching es deliberadamente conservador: preferimos dejar un médico sin email
antes que asignarle el email de un homónimo. Ver `classify_match()`.

Uso:
    python3 scripts/reps_colombia.py --download        # ~77k filas, ~1 min
    python3 scripts/reps_colombia.py --match           # cruce + reporte
    python3 scripts/reps_colombia.py --download --match
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

try:
    import httpx
    from rapidfuzz import fuzz
except ImportError as e:  # pragma: no cover
    print(f"ERROR: Dependencia faltante: {e}")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

REPS_JSONL = OUTPUT_DIR / "reps_co_prestadores.jsonl"
MATCH_JSONL = OUTPUT_DIR / "reps_match_co.jsonl"
LISTING_JSONL = OUTPUT_DIR / "doctoralia_co_listing.jsonl"
PROFILES_JSONL = OUTPUT_DIR / "doctoralia_co_profiles.jsonl"

SOCRATA = "https://www.datos.gov.co/resource/c36g-9fc2.json"
PAGE = 20000

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Normalización de nombres ──────────────────────────────────────────────────

# Partículas que aparecen o no según la fuente y arruinan el matching exacto.
_PARTICLES = {"DE", "DEL", "LA", "LAS", "LOS", "Y", "DA", "DI", "SAN", "SANTA"}
_TITLE_RE = re.compile(r"^\s*(DRA?\.?|DR\.?|DOCTORA?|MD)\s+", re.IGNORECASE)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def norm_name(s: str) -> str:
    s = _TITLE_RE.sub("", (s or "").strip())
    s = strip_accents(s).upper()
    s = re.sub(r"[^A-ZÑ ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens_of(s: str) -> list[str]:
    return [t for t in norm_name(s).split() if len(t) >= 3 and t not in _PARTICLES]


# ── Descarga REPS ─────────────────────────────────────────────────────────────

def download_reps() -> None:
    """Paginado Socrata. Guardamos crudo: el filtrado se decide en el match."""
    log.info("=== Descargando REPS nacional (c36g-9fc2) ===")
    rows: list[dict] = []
    with httpx.Client(timeout=120) as client:
        offset = 0
        while True:
            r = client.get(SOCRATA, params={
                "$limit": PAGE, "$offset": offset, "$order": "codigoprestador",
            })
            r.raise_for_status()
            batch = r.json()
            if not batch:
                break
            rows.extend(batch)
            log.info(f"  +{len(batch)}  (total {len(rows)})")
            offset += PAGE
            if len(batch) < PAGE:
                break

    # Un prestador puede tener varias sedes → una fila por sede. Colapsamos a
    # prestador: el email/teléfono del prestador es el mismo en todas sus sedes.
    by_code: dict[str, dict] = {}
    for r in rows:
        code = r.get("codigoprestador") or r.get("numeroidentificacion") or ""
        if not code:
            continue
        cur = by_code.setdefault(code, {
            "codigoprestador": code,
            "nombreprestador": r.get("nombreprestador", ""),
            "claseprestador": r.get("claseprestador", ""),
            "tipoid": r.get("tipoid", ""),
            "email": (r.get("email_prestador") or "").strip().lower(),
            "telefono": (r.get("telefonoprestador") or "").strip(),
            "departamento": r.get("departamentoprestadordesc", ""),
            "municipio": r.get("municipioprestadordesc", ""),
            "direccion": r.get("direccionprestador", ""),
            "sedes": [],
            "fecha_corte": r.get("fecha_corte_reps", "")[:10],
        })
        sede = (r.get("nombresede") or "").strip()
        if sede and sede not in cur["sedes"]:
            cur["sedes"].append(sede)

    with REPS_JSONL.open("w", encoding="utf-8") as f:
        for v in by_code.values():
            f.write(json.dumps(v, ensure_ascii=False) + "\n")

    indep = sum(1 for v in by_code.values() if v["claseprestador"] == "Profesional Independiente")
    con_mail = sum(1 for v in by_code.values() if v["email"])
    log.info(f"=== REPS: {len(by_code)} prestadores únicos "
             f"({indep} profesionales independientes, {con_mail} con email) ===")


# ── Matching ──────────────────────────────────────────────────────────────────

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def load_doctoralia() -> list[dict]:
    # El listado ya excluye clínicas (`data-test-entity-type="facility"`); es la
    # autoridad sobre quién es persona. Cruzar una IPS contra REPS produce
    # emails institucionales disfrazados de contacto médico.
    by_url = {r["perfil_url"]: dict(r) for r in read_jsonl(LISTING_JSONL)}
    for p in read_jsonl(PROFILES_JSONL):
        if p["perfil_url"] in by_url:
            by_url[p["perfil_url"]].update(p)
    return list(by_url.values())


def classify_match(doc_toks: list[str], reps_toks: list[str],
                   misma_ciudad: bool) -> tuple[str, float]:
    """Devuelve (confianza, score).

    `alta`  — todos los tokens del nombre de Doctoralia están en REPS
              (típico: Doctoralia usa 3 de los 4 nombres legales) y hay ≥3
              tokens compartidos. Es el caso de identidad prácticamente segura.
    `media` — similitud alta pero sin contención total; corroborada por ciudad.
    `baja`  — similitud alta sin corroboración. Se entrega marcada, no se mezcla
              con los emails confiables.
    """
    if len(doc_toks) < 2 or len(reps_toks) < 2:
        return "descartado", 0.0

    score = float(fuzz.token_set_ratio(" ".join(doc_toks), " ".join(reps_toks)))
    shared = set(doc_toks) & set(reps_toks)
    contained = set(doc_toks) <= set(reps_toks)

    if contained and len(shared) >= 3:
        return "alta", score
    if contained and len(shared) >= 2 and misma_ciudad:
        return "alta", score
    if score >= 93 and len(shared) >= 3:
        return "media" if not misma_ciudad else "alta", score
    if score >= 93 and len(shared) >= 2 and misma_ciudad:
        return "media", score
    if score >= 90 and len(shared) >= 2:
        return "baja", score
    return "descartado", score


def run_match() -> None:
    reps = read_jsonl(REPS_JSONL)
    if not reps:
        log.error("No hay REPS descargado. Corre primero --download")
        return
    docs = load_doctoralia()
    if not docs:
        log.error("No hay base Doctoralia. Corre primero scrape_doctoralia_co.py")
        return

    log.info(f"=== Cruce: {len(docs)} ginecólogos × {len(reps)} prestadores REPS ===")

    # Índice invertido por token para no hacer 2.9k × 74k comparaciones.
    reps_toks = [tokens_of(r["nombreprestador"]) for r in reps]
    index: dict[str, list[int]] = defaultdict(list)
    for i, toks in enumerate(reps_toks):
        for t in set(toks):
            index[t].append(i)

    resultados = []
    stats = {"alta": 0, "media": 0, "baja": 0, "sin_match": 0, "ambiguo": 0}

    for d in docs:
        nombre = d.get("nombre_raw", "")
        dtoks = tokens_of(nombre)
        ciudad_doc = norm_name((d.get("ciudad_perfil") or d.get("ciudad", "")).split(" | ")[0])

        # Candidatos = prestadores que comparten al menos un token del nombre.
        cand: dict[int, int] = defaultdict(int)
        for t in set(dtoks):
            for i in index.get(t, ()):
                cand[i] += 1
        candidatos = [i for i, n in cand.items() if n >= 2]

        # Contención única: el nombre de Doctoralia cabe entero dentro de UN solo
        # prestador REPS. "Nilfran Nottola" ⊂ "Nilfran Javier Nottola Filomena" y
        # no hay otro Nottola en 61k registros → es él, sin necesidad de ciudad.
        # La contención va en los dos sentidos: Doctoralia a veces muestra menos
        # nombres que el registro legal y a veces más ("Nilfran Javier Nottola
        # Filomena" vs. "NILFRAN NOTTOLA" en REPS).
        # En el sentido inverso exigimos 3 tokens: un REPS de 2 tokens contenido
        # en un nombre largo ("JUAN PEREZ" ⊂ "Juan Carlos Pérez Gómez") es
        # coincidencia frecuente, no identidad.
        contenidos = [
            i for i in candidatos
            if reps[i]["email"] and (
                set(dtoks) <= set(reps_toks[i])
                or (len(reps_toks[i]) >= 3 and set(reps_toks[i]) <= set(dtoks))
            )
        ]
        emails_contenidos = {reps[i]["email"] for i in contenidos}
        if len(dtoks) >= 2 and len(emails_contenidos) == 1:
            r = reps[contenidos[0]]
            stats["alta"] += 1
            resultados.append({
                "perfil_url": d.get("perfil_url"), "nombre": nombre,
                "confianza": "alta",
                "score": float(fuzz.token_set_ratio(" ".join(dtoks),
                                                    " ".join(reps_toks[contenidos[0]]))),
                "email": r["email"], "emails_alt": [],
                "telefono_reps": r["telefono"], "nombre_reps": r["nombreprestador"],
                "codigo_reps": r["codigoprestador"], "clase_reps": r["claseprestador"],
                "municipio_reps": r["municipio"], "departamento_reps": r["departamento"],
                "candidatos": len(contenidos), "regla": "contencion_unica",
            })
            continue

        mejor: list[tuple[str, float, dict]] = []
        for i in candidatos:
            r = reps[i]
            if not r["email"]:
                continue
            misma_ciudad = bool(ciudad_doc) and ciudad_doc in norm_name(r["municipio"])
            conf, score = classify_match(dtoks, reps_toks[i], misma_ciudad)
            if conf != "descartado":
                mejor.append((conf, score, r))

        if not mejor:
            stats["sin_match"] += 1
            resultados.append({"perfil_url": d.get("perfil_url"), "nombre": nombre,
                               "confianza": "sin_match", "email": "", "candidatos": 0})
            continue

        rank = {"alta": 3, "media": 2, "baja": 1}
        mejor.sort(key=lambda x: (rank[x[0]], x[1]), reverse=True)
        conf, score, r = mejor[0]

        # Ambigüedad real: dos prestadores igual de buenos con emails distintos.
        emails_top = {m[2]["email"] for m in mejor if rank[m[0]] == rank[conf] and m[1] >= score - 1}
        if len(emails_top) > 1:
            conf = "ambiguo"
            stats["ambiguo"] += 1
        else:
            stats[conf] += 1

        resultados.append({
            "perfil_url":   d.get("perfil_url"),
            "nombre":       nombre,
            "confianza":    conf,
            "score":        round(score, 1),
            "email":        r["email"] if conf != "ambiguo" else "",
            "emails_alt":   sorted(emails_top) if conf == "ambiguo" else [],
            "telefono_reps": r["telefono"],
            "nombre_reps":  r["nombreprestador"],
            "codigo_reps":  r["codigoprestador"],
            "clase_reps":   r["claseprestador"],
            "municipio_reps": r["municipio"],
            "departamento_reps": r["departamento"],
            "candidatos":   len(mejor),
        })

    with MATCH_JSONL.open("w", encoding="utf-8") as f:
        for r in resultados:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    total = len(docs)
    usables = stats["alta"] + stats["media"]
    log.info(f"=== Cruce terminado ===")
    log.info(f"  alta:      {stats['alta']:>5}  ({stats['alta']/total:.1%})")
    log.info(f"  media:     {stats['media']:>5}  ({stats['media']/total:.1%})")
    log.info(f"  baja:      {stats['baja']:>5}  (entregado marcado, no confiable)")
    log.info(f"  ambiguo:   {stats['ambiguo']:>5}  (homónimos con emails distintos)")
    log.info(f"  sin match: {stats['sin_match']:>5}")
    log.info(f"  → EMAILS USABLES (alta+media): {usables} / {total} = {usables/total:.1%}")


def main() -> None:
    p = argparse.ArgumentParser(description="REPS Colombia — emails oficiales")
    p.add_argument("--download", action="store_true")
    p.add_argument("--match", action="store_true")
    args = p.parse_args()
    if not (args.download or args.match):
        p.error("indica --download y/o --match")
    if args.download:
        download_reps()
    if args.match:
        run_match()


if __name__ == "__main__":
    main()
