#!/usr/bin/env python3
"""
Auto-scraper autónomo para Cliniweb Panamá — SIN dependencia de Claude.
Diseñado para correr como cron job o tarea programada.

Continúa desde donde quedó la última corrida (usa un archivo de estado JSON).
Agrega nuevos médicos al Excel existente de forma incremental.

Uso manual:
    python3 scripts/auto_scraper_cliniweb.py
    python3 scripts/auto_scraper_cliniweb.py --pages-per-run 50

Cron (cada noche a las 2am):
    0 2 * * * cd /Users/lgrocha/Documents/Claude/02.\ Data\ scraping && python3 scripts/auto_scraper_cliniweb.py >> logs/scraper.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

try:
    import httpx
    import pandas as pd
    from openpyxl import load_workbook
except ImportError as e:
    print(f"ERROR: Dependencia faltante: {e}")
    print("Instalar con: pip3 install httpx pandas openpyxl")
    sys.exit(1)

# ── Rutas ────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"
LOG_DIR    = ROOT / "logs"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE  = OUTPUT_DIR / "scraper_state.json"
OUTPUT_XLSX = OUTPUT_DIR / "medicos_panama_cliniweb_v2.xlsx"

# ── Config Cliniweb ───────────────────────────────────────────────────────────
SEED = "639148726507040344"

SOURCES = [
    # (subdominio, idEmpresaLocalidades_o_None, nombre_legible)
    ("directorio",                 None, "Directorio General Cliniweb PA"),
    ("directorio-hospitalpaitilla","5870875,593780,5570111,263270,6053193", "Hospital Paitilla"),
    ("directorio-thepanamaclinic", None, "The Panama Clinic"),
    ("directorio-towncenter",      None, "Town Center Medical"),
]

HEADERS = {
    "Accept":           "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


# ── Estado persistente ────────────────────────────────────────────────────────

def load_state() -> dict:
    """Carga el estado de progreso (fuente → última página scrapeada)."""
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Extracción ────────────────────────────────────────────────────────────────

def fetch_page(client: httpx.Client, subdominio: str, page: int,
               id_emp: str | None = None) -> dict:
    url = f"https://{subdominio}.cliniweb.com/es/api/buscar/texto//panama"
    params: dict = {
        "filters": "", "idConcepto": 0, "idTipoConcepto": 0,
        "page": page, "seed": SEED,
    }
    if id_emp:
        params["idEmpresaLocalidades"] = id_emp
    headers = dict(HEADERS)
    headers["Referer"] = f"https://{subdominio}.cliniweb.com/es/buscar"
    resp = client.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Type") != 0:
        return {"medicos": [], "nresul": 0}
    inner = data["Data"]
    if isinstance(inner, str):
        inner = json.loads(inner)
    return inner


def extract_doctor(m: dict, fuente: str) -> dict:
    email = m.get("email") or ""
    if "soporte@cliniweb" in email or "support@cliniweb" in email:
        email = ""
    tel = m.get("telLoc") or ""
    locations = m.get("locations") or []
    tels = [tel] if tel else []
    dirs, emps = [], []
    for loc in locations:
        lt = loc.get("telLoc") or loc.get("tel") or ""
        ld = loc.get("dirLoc") or loc.get("dir") or ""
        le = loc.get("objetoDetalle") or loc.get("emp") or ""
        if lt: tels.append(lt)
        if ld: dirs.append(ld)
        if le: emps.append(le)
    return {
        "Nombre":        m.get("nom", ""),
        "Especialidad":  m.get("esp", ""),
        "Subespecialidad": m.get("subs", ""),
        "Email":         email,
        "Telefono":      " | ".join(dict.fromkeys(t.strip() for t in tels if t.strip())),
        "Consultorio":   " | ".join(dict.fromkeys(e.strip() for e in emps if e.strip())),
        "Direccion":     " | ".join(dict.fromkeys(d.strip() for d in dirs if d.strip())),
        "SitioWeb":      m.get("sitioWeb", "") or "",
        "Perfil_URL":    f"https://app.cliniweb.com{m['nav']}" if m.get("nav") else "",
        "Pais":          "Panama",
        "Fuente":        fuente,
    }


# ── Excel incremental ─────────────────────────────────────────────────────────

def load_existing_excel() -> tuple[pd.DataFrame, set[str]]:
    """Carga el Excel existente y devuelve el DataFrame + nombres ya vistos."""
    if OUTPUT_XLSX.exists():
        df = pd.read_excel(OUTPUT_XLSX, sheet_name="Todos")
        seen = set(df["Nombre"].str.strip().str.upper().dropna())
        return df, seen
    return pd.DataFrame(), set()


def save_excel(df: pd.DataFrame) -> None:
    df_sorted = df.sort_values(["Especialidad", "Nombre"]).reset_index(drop=True)
    with pd.ExcelWriter(OUTPUT_XLSX, engine="openpyxl") as w:
        df_sorted.to_excel(w, sheet_name="Todos", index=False)
        df_con = df_sorted[(df_sorted["Email"] != "") | (df_sorted["Telefono"] != "")]
        df_con.to_excel(w, sheet_name="Con_Contacto", index=False)
        resumen = (df_sorted.groupby("Especialidad").size()
                   .reset_index(name="Cantidad")
                   .sort_values("Cantidad", ascending=False))
        resumen.to_excel(w, sheet_name="Especialidades", index=False)
    log.info(f"Excel guardado: {OUTPUT_XLSX.name}  ({len(df_sorted)} médicos)")


# ── Runner principal ──────────────────────────────────────────────────────────

def run(pages_per_run: int = 100) -> None:
    log.info(f"=== Auto-scraper Cliniweb PA  {datetime.now():%Y-%m-%d %H:%M} ===")
    state = load_state()
    df_existing, seen_names = load_existing_excel()
    new_rows: list[dict] = []

    total_pages_done = 0

    with httpx.Client(follow_redirects=True) as client:
        for subdominio, id_emp, nombre in SOURCES:
            key = subdominio
            start_page = state.get(key, {}).get("next_page", 1)
            total_known = state.get(key, {}).get("total", 99999)

            # Fuente ya completada
            if start_page > total_known and total_known < 99999:
                log.info(f"✓ {nombre}  (ya completa, {total_known} total)")
                continue

            # Presupuesto de páginas para esta fuente en esta corrida
            budget = max(1, pages_per_run - total_pages_done)
            end_page = start_page + budget - 1

            log.info(f"→ {nombre}  páginas {start_page}–{end_page}")

            source_new = 0
            last_page = start_page - 1

            for page in range(start_page, end_page + 1):
                try:
                    data = fetch_page(client, subdominio, page, id_emp)
                except Exception as e:
                    log.warning(f"  p{page} error: {e}")
                    break

                medicos = data.get("medicos") or []
                nresul  = data.get("nresul", 0)

                if page == start_page and nresul:
                    state.setdefault(key, {})["total"] = (nresul // 8) + 2  # páginas estimadas
                    total_known = state[key]["total"]

                if not medicos:
                    # Llegamos al final
                    state.setdefault(key, {})["next_page"] = 99999
                    log.info(f"  Fin de {nombre} en página {page}")
                    break

                last_page = page
                for m in medicos:
                    n = m.get("nom", "").strip().upper()
                    if n and n not in seen_names:
                        seen_names.add(n)
                        new_rows.append(extract_doctor(m, nombre))
                        source_new += 1

                total_pages_done += 1
                if total_pages_done >= pages_per_run:
                    break

            # Actualizar estado para próxima corrida
            if last_page >= start_page:
                state.setdefault(key, {})["next_page"] = last_page + 1
                save_state(state)

            log.info(f"  {nombre}: +{source_new} nuevos (páginas hasta {last_page})")

            if total_pages_done >= pages_per_run:
                log.info(f"Presupuesto de {pages_per_run} páginas agotado. Continuar en próxima corrida.")
                break

    # Guardar si hay novedades
    if new_rows:
        df_new = pd.DataFrame(new_rows)
        df_all = pd.concat([df_existing, df_new], ignore_index=True) if not df_existing.empty else df_new
        save_excel(df_all)
        log.info(f"Total nuevos esta corrida: {len(new_rows)}")
    else:
        log.info("Sin médicos nuevos en esta corrida.")

    save_state(state)
    log.info("=== Corrida completada ===\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Auto-scraper Cliniweb PA (sin Claude)")
    p.add_argument("--pages-per-run", type=int, default=100,
                   help="Páginas a raspar por corrida (default: 100 ≈ 800 médicos)")
    args = p.parse_args()
    run(pages_per_run=args.pages_per_run)


if __name__ == "__main__":
    main()
