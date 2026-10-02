# AGENTS.md — Medyra (Medical Data Intelligence LATAM)

Guía para agentes (Claude Code u otros) que trabajen en este repo. Léela completa antes de ejecutar nada.

## Qué es

Recolección de **información pública de médicos** en Latinoamérica (nombre, especialidad, ciudad, teléfono, email) para
entregables en Excel. Países activos: **Costa Rica, Panamá, Ecuador**; Colombia en rama aparte.
El entregable típico pide **N médicos por ciudad/provincia y especialidad, con teléfono Y email**.
Idioma de trabajo con el usuario: **español**. Entregables con nivel CTO: números reales, fuentes citadas, brechas explícitas.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate     # Python >= 3.11
pip install -e ".[dev]"                                  # incluye pypdf, httpx, selectolax, pandas, playwright…
playwright install chromium   # opcional; alternativa: launch(channel="chrome") usa el Chrome del sistema
cp .env.example .env          # solo hace falta para el pipeline con DB/LLM
docker compose up -d          # opcional: Postgres+pgvector y Redis (schema.sql se carga al iniciar)
pytest                        # tests unitarios (tests/unit)
ruff check src scripts tests
```

- Los **scripts de scraping no necesitan DB ni LLM**; corren solo con el venv.
- `.env`: `DATABASE_URL` (docker-compose usa usuario/db `medintel`), `REDIS_URL`, `ANTHROPIC_API_KEY` (opcional),
  `LLM_MODEL`, `HUNTER_API_KEY` (opcional), `COUNTRIES`.

## Estructura

```
src/medintel/    paquete que usan scripts/ y tests/ (imports `from medintel...`)
src/medyra/      copia renombrada (lo que empaqueta pyproject + CLI `medyra`). Rename a medias:
                 si cambias lógica compartida, cámbiala en AMBOS o consolida primero.
  normalization/ names.py (parse_latam_name, family_first/given_first), phones.py (E.164 via phonenumbers),
                 emails.py, specialties.py
  inference/     confidence, identity, email_patterns
  infrastructure/scrapers/  doctoralia, colegio_cr, bing, ddg, base
  application/agents/enrichment.py, interfaces/cli.py (ingest, parse-name, parse-phone, enrich, verify-cr)
scripts/         scrapers y pipelines por país (standalone, ver abajo)
schema.sql       modelo claim-based (physician, observation, claim, review_task, pgvector)
tests/unit/      pytest; fixtures HTML en tests/fixtures/
data/            NO versionado (ver "Datos")
```

## Datos (`data/` está en .gitignore)

```
data/input/                      Excel fuente del cliente (medicos-allegra-0226.xlsx = base 10.4k médicos CR/PA/EC)
data/input/lotaip/               directorios públicos descargados a mano (PDF/CSV/XLSX) → los lee el scraper EC
data/output/                     entregables + caches crudos (EC_raw_*.json, PDFs LOTAIP)
data/snapshots/                  cache de requests (p.ej. colegio_cr)
```

Al mover el trabajo a otra máquina **hay que copiar `data/` aparte** (git no lo lleva). Mínimo para Ecuador:
`data/input/medicos-allegra-0226.xlsx` y `data/output/medicos-allegra-0226__Ecuador_clean.xlsx`.
La ruta de datos de los scripts se puede apuntar con `--data-dir` cuando corres desde un worktree.

## Pipelines por país

### Ecuador — `scripts/scrape_ecuador_provincias.py`
Médicos General, Pediatría, Interna, Familiar y Emergencias en **Azuay, Loja, Carchi** (+ vecinas: Imbabura→Carchi,
El Oro y Zamora Chinchipe→Loja; cada fila lleva `Provincia` real y `Tipo` pedida/vecina).

```bash
python scripts/scrape_ecuador_provincias.py --data-dir data            # usa caches EC_raw_*.json
python scripts/scrape_ecuador_provincias.py --data-dir data --refresh  # re-scrapea todo
```
Salida: `data/output/ecuador_azuay_loja_carchi.xlsx` → hojas `Resumen`, **`Tel+Email`** (el entregable), `Azuay`, `Loja`, `Carchi`.

Fuentes (validadas 2026-09/10):
| Fuente | Aporta | Notas |
|---|---|---|
| masquemedicos.ec | nombre, dirección, ~30% teléfono | HTML estático, `div.negocio`; sin email |
| doctoranytime.ec | nombre, dirección | JSON-LD (`ld&#x2B;json`), paginar `?p=N`; sin teléfono/email |
| ecuadoctor.com | teléfono + email | pocos registros |
| **LOTAIP (directorios públicos)** | **teléfono + email institucional** | única fuente masiva de emails |

LOTAIP = Ley de Transparencia: cada institución pública publica su directorio (nombre, puesto, ciudad, teléfono, correo).
`LOTAIP_DOCS` en el script: IESS 2023-09 y 2023-06, Hospital San Vicente de Paúl (Ibarra, 2024). Cualquier PDF/CSV/XLSX en
`data/input/lotaip/` se parsea solo (`parse_lotaip` / `parse_lotaip_tabular`). Dedupe por email institucional (gana el más reciente).

**Geo-bloqueo**: `salud.gob.ec` (MSP), `*.dpe.gob.ec` (Portal Nacional de Transparencia), hospitales de Loja/Tulcán/Machala,
municipios de Cuenca/Tulcán y otros `.gob.ec` **solo responden desde IPs de Ecuador** (timeout de conexión, no error HTTP).
El MSP es la fuente grande que falta. Para continuar: correr desde una red ecuatoriana (o Tailscale exit node en Ecuador) y
descargar el "Numeral 2 – Directorio y distributivo personal" del MSP, coordinaciones zonales 1/6/7, distritos y hospitales
de esas provincias a `data/input/lotaip/`, luego re-ejecutar el script.

### Costa Rica
`scripts/auto_scraper_cr.py` (cron diario, `setup_cron_cr.sh`), `scripts/scrape_salud360_cr.py`,
`src/medintel/infrastructure/scrapers/colegio_cr.py` (padrón oficial, `GET /api/autocomplete?q=`; rate limit → 3.5 s/req;
usar `strict_name_score`). Fuentes y conteos en README.md.

### Panamá
`scripts/auto_scraper_cliniweb.py` (cron, `setup_cron.sh`), `scripts/scraper_cliniweb_pa.py`, `scrape_pa_*`.
Cliniweb: seed fijo `639148726507040344` para paginación consistente.

### Colombia (rama `claude/gynecologists-database-colombia-632b80`, aún sin PR)
`scrape_doctoralia_co.py`, `reps_colombia.py` (REPS MinSalud, emails/teléfonos oficiales), `enrich_emails_co.py`,
`build_colombia_dataset.py`.

### Pipeline general (ingesta / enriquecimiento)
`scripts/ingest_excel.py` (limpia Allegra por hoja/país), `scripts/enrich_fast.py`, `scripts/enrich_cr_pa.py`,
`scripts/export_real_emails.py`, CLI `medyra …`.

## Qué NO funciona (no reintentar sin motivo nuevo)

- **Doctoralia no opera en EC, CR ni PA** (sí en MX, CO, PE, CL, ES).
- **Bing / DuckDuckGo**: bloquean/throttlean scraping de búsqueda tras pocas queries.
- **ecuamedical.com**: ahora solo Quito. **citamedica.ec**: casi vacío. **ACESS / datosabiertos.gob.ec**: 403, sin provincia.
- Directorios de clínicas privadas (Santa Inés, Del Río, Monte Sinaí): nombres y especialidad, **sin email**.
- Perfiles de masquemedicos/doctoranytime: no agregan email; masquemedicos no enlaza webs propias.
- Huli (Colegio CR): Playwright no desbloquea contactos.

## Reglas de trabajo

- **Solo datos públicos.** No iniciar sesión, no llenar formularios (p. ej. landing pages que piden datos para "descargar
  directorio"), no evadir captchas ni bloqueos. Pausa ≥ 0.8 s entre requests a un mismo sitio.
- **Nunca inventar contactos.** Emails generados por patrón (`phase2b_email_generation.py`) van en columna aparte marcada
  como NO VERIFICADO; nunca en la columna `Email`.
- Toda fila lleva `Fuente`, `URL` y, si aplica, `Fecha_fuente`. Reportar brechas vs meta en una hoja `Resumen`.
- Provincia/ciudad se asigna por la **dirección real**, no por la ciudad buscada (los sitios mezclan "cercanos"/teleconsulta).
- Antes de sobrescribir un entregable, guardar copia versionada (`_v1`, `_v2`…) en `data/output/`.
- Escritura de Excel atómica (`_tmp.xlsx` + `shutil.move`) en los scrapers con cron.
- Dependencias: usar las del proyecto (httpx, selectolax, pandas, pypdf, playwright). No agregar requests/bs4.

## Git

- `main` es la base. Trabajar en ramas `claude/<tema>` (worktrees en `.claude/worktrees/`). PR a `main` con `gh pr create`.
- Commits: mensaje en inglés estilo conventional (`feat(scraper): …`), explicando el porqué.
- No versionar datos (`data/`), ni `.env`, ni `.kilo/`.
