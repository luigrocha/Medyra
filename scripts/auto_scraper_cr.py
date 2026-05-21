#!/usr/bin/env python3
"""
Auto-scraper autónomo para Costa Rica — SIN dependencia de Claude.
Fuentes:
  1. Cliniweb CR (directorio.cliniweb.com) — 4 ciudades
  2. Clínica Bíblica (hcb-middleware-citas.onrender.com API)

Continúa desde donde quedó la última corrida (archivo de estado JSON).
Agrega nuevos médicos al Excel existente de forma incremental.

Uso manual:
    python3 scripts/auto_scraper_cr.py

Cron (cada noche a las 3:15am):
    15 3 * * * cd /Users/lgrocha/Documents/Claude/02.\ Data\ scraping && python3 scripts/auto_scraper_cr.py >> logs/scraper_cr.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    import httpx
    import pandas as pd
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

STATE_FILE  = OUTPUT_DIR / "scraper_state_cr.json"
OUTPUT_XLSX = OUTPUT_DIR / "medicos_costa_rica_v2.xlsx"

# ── Config ────────────────────────────────────────────────────────────────────
SEED = "639148726507040344"
CLINIWEB_CITIES = ["san-jose", "alajuela", "puntarenas", "limon"]
CLINICA_BIBLICA_API = "https://hcb-middleware-citas.onrender.com/api/directorio/profesionales"

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
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Estado ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Excel atómico ─────────────────────────────────────────────────────────────

def load_existing() -> tuple[pd.DataFrame, set[str]]:
    if OUTPUT_XLSX.exists():
        df = pd.read_excel(OUTPUT_XLSX, sheet_name="Todos")
        seen = set(df["Nombre"].str.strip().str.upper().dropna())
        return df, seen
    return pd.DataFrame(), set()


def save_excel(df: pd.DataFrame) -> None:
    df = df.sort_values(["Especialidad", "Nombre"]).reset_index(drop=True)
    tmp = OUTPUT_DIR / "_medicos_cr_tmp.xlsx"
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Todos", index=False)
        df_con = df[
            (df["Email"].fillna("").str.strip() != "") |
            (df["Telefono"].fillna("").str.strip() != "")
        ]
        df_con.to_excel(w, sheet_name="Con_Contacto", index=False)
        df.groupby("Fuente").size().reset_index(name="Cantidad").sort_values("Cantidad", ascending=False).to_excel(w, sheet_name="Por_Fuente", index=False)
        df.groupby("Especialidad").size().reset_index(name="Cantidad").sort_values("Cantidad", ascending=False).to_excel(w, sheet_name="Especialidades", index=False)
    shutil.move(str(tmp), str(OUTPUT_XLSX))
    log.info(f"Excel guardado: {OUTPUT_XLSX.name}  ({len(df)} médicos)")


# ── Cliniweb CR ───────────────────────────────────────────────────────────────

def fetch_cliniweb_page(client: httpx.Client, ciudad: str, page: int) -> dict:
    url = f"https://directorio.cliniweb.com/es/api/buscar/texto//{ciudad}"
    params = {"filters": "", "idConcepto": 0, "idTipoConcepto": 0, "page": page, "seed": SEED}
    headers = dict(HEADERS)
    headers["Referer"] = "https://directorio.cliniweb.com/es/buscar"
    resp = client.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if data.get("Type") != 0:
        return {"medicos": [], "nresul": 0}
    inner = data["Data"]
    if isinstance(inner, str):
        inner = json.loads(inner)
    return inner


def extract_cliniweb(m: dict, ciudad: str) -> dict:
    email = m.get("email") or ""
    if "soporte@cliniweb" in email or "support@cliniweb" in email:
        email = ""
    tels = [m["telLoc"]] if m.get("telLoc") else []
    dirs, emps = [], []
    for loc in (m.get("locations") or []):
        if loc.get("telLoc"): tels.append(loc["telLoc"])
        if loc.get("dirLoc"): dirs.append(loc["dirLoc"])
        if loc.get("objetoDetalle"): emps.append(loc["objetoDetalle"])
    return {
        "Nombre":        m.get("nom", ""),
        "Especialidad":  m.get("esp", ""),
        "Subespecialidad": m.get("subs", ""),
        "Email":         email,
        "Telefono":      " | ".join(dict.fromkeys(t.strip() for t in tels if t.strip())),
        "Consultorio":   " | ".join(dict.fromkeys(e for e in emps if e)),
        "Direccion":     " | ".join(dict.fromkeys(d for d in dirs if d)),
        "SitioWeb":      m.get("sitioWeb", "") or "",
        "Perfil_URL":    f"https://app.cliniweb.com{m['nav']}" if m.get("nav") else "",
        "Ciudad":        ciudad,
        "Pais":          "Costa Rica",
        "Fuente":        f"Directorio Cliniweb CR",
    }


def scrape_cliniweb_cr(client: httpx.Client, state: dict, seen: set,
                        pages_budget: int) -> tuple[list[dict], int]:
    new_rows: list[dict] = []
    pages_done = 0

    for ciudad in CLINIWEB_CITIES:
        key = f"cr_{ciudad}"
        start_page = state.get(key, {}).get("next_page", 1)
        total_known = state.get(key, {}).get("total", 99999)

        if start_page > total_known and total_known < 99999:
            log.info(f"✓ Cliniweb CR {ciudad}  (completa)")
            continue

        budget = max(1, pages_budget - pages_done)
        city_new = 0
        last_page = start_page - 1

        for page in range(start_page, start_page + budget):
            try:
                data = fetch_cliniweb_page(client, ciudad, page)
            except Exception as e:
                log.warning(f"  Cliniweb {ciudad} p{page}: {e}")
                break

            medicos = data.get("medicos") or []
            nresul  = data.get("nresul", 0)

            if page == start_page and nresul:
                state.setdefault(key, {})["total"] = (nresul // 8) + 2

            if not medicos:
                state.setdefault(key, {})["next_page"] = 99999
                log.info(f"  Fin Cliniweb CR {ciudad} en p{page}")
                break

            last_page = page
            for m in medicos:
                n = m.get("nom", "").strip().upper()
                if n and n not in seen:
                    seen.add(n)
                    new_rows.append(extract_cliniweb(m, ciudad))
                    city_new += 1

            pages_done += 1
            if pages_done >= pages_budget:
                break

        if last_page >= start_page:
            state.setdefault(key, {})["next_page"] = last_page + 1

        log.info(f"  Cliniweb CR {ciudad}: +{city_new} nuevos")
        if pages_done >= pages_budget:
            break

    return new_rows, pages_done


# ── Clínica Bíblica ───────────────────────────────────────────────────────────

def parse_cb_esp(m: dict) -> str:
    esps = []
    for e in (m.get("especialidades") or []):
        raw = e.get("desc_espec", "")
        try:
            for p in json.loads(raw):
                v = p.get("ESPECIALIDAD") or p.get("desc_espec", "")
                if v:
                    esps.append(v.title())
        except Exception:
            if raw:
                esps.append(raw)
    return " | ".join(dict.fromkeys(esps))


def scrape_clinica_biblica(client: httpx.Client, state: dict, seen: set) -> list[dict]:
    key = "clinica_biblica"
    last_count = state.get(key, {}).get("last_count", 0)

    headers = {
        **HEADERS,
        "Origin":  "https://www.clinicabiblica.com",
        "Referer": "https://www.clinicabiblica.com/es/directorio-medico",
    }

    try:
        resp = client.get(CLINICA_BIBLICA_API, headers=headers, timeout=30)
        resp.raise_for_status()
        items = resp.json().get("items", [])
    except Exception as e:
        log.warning(f"  Clínica Bíblica API error: {e}")
        return []

    new_rows = []
    for m in items:
        nombre = (m.get("NOMBRE_COMPLETO") or m.get("NOM_PROF", "")).strip().title()
        if not nombre:
            continue
        n_upper = nombre.upper()
        if n_upper in seen:
            continue
        seen.add(n_upper)
        esp = parse_cb_esp(m)
        sedes = " | ".join(m.get("sedes") or [])
        perfil = m.get("pagina_de_profesional", "")
        new_rows.append({
            "Nombre":        nombre,
            "Especialidad":  esp,
            "Subespecialidad": "",
            "Email":         "",
            "Telefono":      "+(506) 2522-1000",
            "Consultorio":   f"Clínica Bíblica - {sedes}" if sedes else "Clínica Bíblica",
            "Direccion":     "San José, Costa Rica",
            "SitioWeb":      "https://www.clinicabiblica.com",
            "Perfil_URL":    f"https://www.clinicabiblica.com/es/medicos/{perfil}" if perfil else "",
            "Ciudad":        "san-jose",
            "Pais":          "Costa Rica",
            "Fuente":        "Clínica Bíblica CR",
        })

    state.setdefault(key, {})["last_count"] = len(items)
    state[key]["last_run"] = datetime.now().isoformat()[:19]

    if len(items) != last_count:
        log.info(f"  Clínica Bíblica: +{len(new_rows)} nuevos (total en API: {len(items)})")
    else:
        log.info(f"  Clínica Bíblica: sin cambios ({len(items)} médicos en API)")

    return new_rows


# ── Runner principal ──────────────────────────────────────────────────────────

def run(pages_per_run: int = 50) -> None:
    log.info(f"=== Auto-scraper CR  {datetime.now():%Y-%m-%d %H:%M} ===")
    state = load_state()
    df_existing, seen = load_existing()
    all_new: list[dict] = []

    with httpx.Client(follow_redirects=True) as client:
        # 1. Clínica Bíblica (full refresh cada corrida — es rápido, 1 request)
        cb_rows = scrape_clinica_biblica(client, state, seen)
        all_new.extend(cb_rows)

        # 2. Cliniweb CR (paginado, respeta budget)
        cw_rows, _ = scrape_cliniweb_cr(client, state, seen, pages_per_run)
        all_new.extend(cw_rows)

    if all_new:
        df_new = pd.DataFrame(all_new)
        df_all = pd.concat([df_existing, df_new], ignore_index=True) if not df_existing.empty else df_new
        save_excel(df_all)
        log.info(f"Total nuevos esta corrida: {len(all_new)}")
    else:
        log.info("Sin médicos nuevos.")

    save_state(state)
    log.info("=== Corrida completada ===\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Auto-scraper CR (sin Claude)")
    p.add_argument("--pages-per-run", type=int, default=50)
    args = p.parse_args()
    run(pages_per_run=args.pages_per_run)


if __name__ == "__main__":
    main()
