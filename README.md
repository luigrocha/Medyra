# medintel — Medical Public Data Intelligence (LATAM)

Plataforma para automatizar la recolección, limpieza, enriquecimiento y
deduplicación de información pública de médicos en Latinoamérica.

Estado: **MVP — Día 1**. La capa de normalización + Inference & Confidence
Engine ya está funcional y testeada. Scrapers, agentes y API se siembran
en sprints siguientes.

---

## Quick start (sin Docker)

```bash
# 1. Entorno
python -m venv .venv && source .venv/bin/activate
pip install -e .[dev]

# 2. Tests (sanity check)
pytest -q

# 3. Limpia tu Excel actual (NO necesita DB)
cp /ruta/a/tu/CR.xlsx data/input/CR.xlsx
python scripts/ingest_excel.py --input data/input/CR.xlsx --country CR

# Salidas en data/output/:
#   CR_clean.xlsx     ← Excel limpio para tu equipo
#   CR_issues.xlsx    ← solo filas con problemas detectados
#   CR_report.json    ← métricas + fixes aplicados
```

## Stack con Docker (Postgres + Redis)

```bash
cp .env.example .env
docker compose up -d
# schema.sql se carga automáticamente en el primer arranque
```

---

## Estructura

```
src/medintel/
├── domain/            # Entidades puras (Pydantic): Claim, PhysicianCore
├── normalization/     # names (Ñ recovery), phones (E.164), emails, specialties
├── inference/         # confidence (log-odds), email_patterns, identity (entity resolution)
├── infrastructure/    # persistence (SQLAlchemy), scrapers (TODO), llm (TODO)
├── application/       # agents: discovery / enrichment / validation / dedup / export (TODO)
└── interfaces/        # cli.py (Typer)
scripts/ingest_excel.py    # punto de entrada día 1
schema.sql                 # esquema Postgres + pgvector
tests/unit/                # tests reales con tu Excel como ground truth
```

---

## Capacidades hoy (probadas con `pytest`)

- Recuperación de Ñ desde `#` (encoding corrupto en tu Excel).
- Parser de nombres LATAM (`APELLIDO1 APELLIDO2 NOMBRE [...]`) con
  manejo de particles (`DE LOS`, `DEL`).
- Normalización de teléfonos CR/PA a E.164 con detección de tipo
  (mobile/landline/voip) y de **números institucionales** (compartidos
  por ≥3 médicos = central, no personal).
- Validación de emails + detección de typos de dominio contra catálogo
  de clínicas conocidas (`clinicabiblia.com` → `clinicabiblica.com`).
- Mapeo de especialidades a códigos canónicos (con fuzzy fallback).
- **Confidence Engine** basado en log-odds con explicabilidad
  (`evidence_strength` JSON adjunto a cada claim).
- **Email inference** por patrones con priors calibrados.
- **Identity resolution**: blocking + signals + regresión logística +
  union-find para clustering de duplicados.

---

## Roadmap inmediato

| Sprint | Entregable |
|---|---|
| S0 ✅ | Ingest+limpieza Excel; normalización; inference engine |
| S1 | Scraper Colegio Médico CR (Tier-1) y match con tu Excel |
| S2 | Doctoralia adapter + enrichment de teléfonos/clínicas |
| S3 | LLM extractor (Claude Haiku) + Validation Agent |
| S4 | Persistencia Postgres + Review Queue (FastAPI) |
| S5 | Replicación Panamá (Consejo Técnico de Salud) |

---

## Diagnóstico rápido desde CLI

```bash
medintel parse-name "YONG PI#AR BERNAL"
medintel parse-phone "506 8827-1060" --country CR
medintel ingest --input data/input/CR.xlsx --country CR
```
