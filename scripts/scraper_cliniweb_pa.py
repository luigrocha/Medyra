#!/usr/bin/env python3
"""
Scraper para directorios médicos de Panamá basados en Cliniweb.

Endpoint descubierto via ingeniería inversa del SPA AngularJS:
  https://{subdominio}.cliniweb.com/es/api/buscar/texto//{ciudad}?page=N&...

Directorios disponibles (cada uno es un hospital/seguro diferente):
  - directorio-hospitalpaitilla  → Hospital Paitilla (~336 médicos)
  - directorio-thepanamaclinic   → The Panama Clinic
  - directorio-towncenter        → Tower Center
  - directorio-hospitalpaitilla  → Hospital Paitilla
  - directorio                   → Directorio general Cliniweb

Uso:
    python scripts/scraper_cliniweb_pa.py
    python scripts/scraper_cliniweb_pa.py --source paitilla --pages 50
    python scripts/scraper_cliniweb_pa.py --all
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data/output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# ── Fuentes conocidas de Cliniweb Panamá ─────────────────────────────────────
# Formato: (subdominio, idEmpresaLocalidades, nombre_legible)
SOURCES = {
    "paitilla": (
        "directorio-hospitalpaitilla",
        "5870875,593780,5570111,263270,6053193",
        "Hospital Paitilla",
    ),
    "thepanamaclinic": (
        "directorio-thepanamaclinic",
        None,   # sin filtro de empresa = todos sus médicos
        "The Panama Clinic",
    ),
    "towncenter": (
        "directorio-towncenter",
        None,
        "Town Center Medical",
    ),
    "general": (
        "directorio",
        None,
        "Directorio General Cliniweb PA",
    ),
    "santafe": (
        "directorio",           # mismo host, distinto accountId configurado
        None,
        "Hospital Santa Fe (via general)",
    ),
}

SEED = "639148726507040344"   # seed observado; puede ser cualquier entero largo
PAGE_SIZE_APPROX = 8          # Cliniweb devuelve ~8 médicos por página


def fetch_page(
    client: httpx.Client,
    subdominio: str,
    page: int,
    id_empresa_localidades: str | None = None,
    ciudad: str = "panama",
    seed: str = SEED,
) -> dict:
    """Hace una llamada a la API de Cliniweb y devuelve el JSON parseado."""
    base = f"https://{subdominio}.cliniweb.com"
    url  = f"{base}/es/api/buscar/texto//{ciudad}"

    params: dict = {
        "filters":              "",
        "idConcepto":           0,
        "idTipoConcepto":       0,
        "page":                 page,
        "seed":                 seed,
    }
    if id_empresa_localidades:
        params["idEmpresaLocalidades"] = id_empresa_localidades

    headers = {
        "Accept":           "application/json, text/plain, */*",
        "Referer":          f"{base}/es/buscar",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent":       "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    }

    resp = client.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()

    data = resp.json()
    if data.get("Type") != 0:
        return {"medicos": [], "nresul": 0, "error": data.get("Data", "Type!=0")}

    inner = data["Data"]
    if isinstance(inner, str):
        inner = json.loads(inner)
    return inner


def extract_doctor(m: dict) -> dict:
    """Extrae los campos útiles de un objeto 'medico' de la API."""
    # Email: descartar el genérico de soporte
    email = m.get("email") or ""
    if "soporte@cliniweb" in email or "support@cliniweb" in email:
        email = ""

    # Teléfono del perfil (puede estar en el campo telLoc o en locations)
    tel = m.get("telLoc") or ""
    locations = m.get("locations") or []

    # Recopilar teléfonos y direcciones de todas las locaciones
    tels  = [tel] if tel else []
    dirs  = []
    emps  = []
    for loc in locations:
        lt = loc.get("telLoc") or loc.get("tel") or ""
        ld = loc.get("dirLoc") or loc.get("dir") or ""
        le = loc.get("objetoDetalle") or loc.get("emp") or ""
        if lt: tels.append(lt)
        if ld: dirs.append(ld)
        if le: emps.append(le)

    # Deduplicar
    tels_str = " | ".join(dict.fromkeys(t.strip() for t in tels if t.strip()))
    dirs_str = " | ".join(dict.fromkeys(d.strip() for d in dirs if d.strip()))
    emps_str = " | ".join(dict.fromkeys(e.strip() for e in emps if e.strip()))

    return {
        "Nombre":        m.get("nom", ""),
        "Especialidad":  m.get("esp", ""),
        "Subespecialidad": m.get("subs", ""),
        "Email":         email,
        "Telefono":      tels_str,
        "Consultorio":   emps_str,
        "Direccion":     dirs_str,
        "SitioWeb":      m.get("sitioWeb", "") or "",
        "Perfil_URL":    f"https://app.cliniweb.com{m['nav']}" if m.get("nav") else "",
        "Pais":          "Panama",
        "Fuente":        "",   # se rellena después
    }


def scrape_source(
    source_key: str,
    max_pages: int = 50,
    verbose: bool = True,
) -> list[dict]:
    """Raspa un directorio completo de Cliniweb hasta agotar páginas."""
    subdominio, id_emp_loc, nombre = SOURCES[source_key]

    print(f"\n🏥  {nombre}  [{source_key}]")
    print(f"    Subdominio: {subdominio}.cliniweb.com")

    doctors: list[dict] = []
    seen_names: set[str] = set()

    with httpx.Client(follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            try:
                data = fetch_page(client, subdominio, page, id_emp_loc)
            except Exception as e:
                print(f"    ⚠️  página {page}: {e}")
                break

            if "error" in data:
                print(f"    API error en página {page}: {data['error']}")
                break

            medicos = data.get("medicos") or []
            total   = data.get("nresul", 0)

            if page == 1:
                print(f"    Total anunciado: {total}")

            new_this_page = 0
            for m in medicos:
                doc = extract_doctor(m)
                doc["Fuente"] = nombre
                nom = doc["Nombre"].strip()
                if nom and nom not in seen_names:
                    seen_names.add(nom)
                    doctors.append(doc)
                    new_this_page += 1

            if verbose:
                print(f"    página {page:3d}: +{new_this_page} nuevos  (total: {len(doctors)})")

            if not medicos or len(doctors) >= total:
                print(f"    ✅ Completado ({len(doctors)} médicos únicos)")
                break

            time.sleep(0.3)   # cortesía

    return doctors


def save_to_excel(all_doctors: list[dict], output_path: Path) -> None:
    if not all_doctors:
        print("⚠️  Sin datos para guardar.")
        return

    df = pd.DataFrame(all_doctors)

    # Estadísticas
    with_email = (df["Email"].str.strip() != "").sum()
    with_phone = (df["Telefono"].str.strip() != "").sum()
    print(f"\n📊 Resumen final:")
    print(f"   Total médicos únicos: {len(df)}")
    print(f"   Con email:    {with_email}")
    print(f"   Con teléfono: {with_phone}")
    print(f"   Especialidades únicas: {df['Especialidad'].nunique()}")

    # Ordenar por especialidad y nombre
    df = df.sort_values(["Especialidad", "Nombre"]).reset_index(drop=True)

    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df.to_excel(writer, sheet_name="Médicos_Panama", index=False)
        # Segunda hoja con solo los que tienen contacto
        df_contact = df[(df["Email"] != "") | (df["Telefono"] != "")]
        df_contact.to_excel(writer, sheet_name="Con_Contacto", index=False)

    print(f"\n📁 Guardado en: {output_path}")
    print(f"   Hoja 'Médicos_Panama': {len(df)} filas")
    print(f"   Hoja 'Con_Contacto':   {len(df_contact)} filas")


def main():
    p = argparse.ArgumentParser(description="Scraper Cliniweb Panama")
    p.add_argument("--source", choices=list(SOURCES.keys()), default="paitilla",
                   help="Fuente a scrapear (default: paitilla)")
    p.add_argument("--all", action="store_true", help="Scrapear todas las fuentes")
    p.add_argument("--pages", type=int, default=50, help="Máximo de páginas por fuente")
    p.add_argument("--output", type=str,
                   default=str(OUTPUT_DIR / "medicos_panama_cliniweb.xlsx"),
                   help="Ruta del Excel de salida")
    args = p.parse_args()

    sources = list(SOURCES.keys()) if args.all else [args.source]
    all_doctors: list[dict] = []
    seen_global: set[str] = set()

    for src in sources:
        docs = scrape_source(src, max_pages=args.pages)
        for d in docs:
            if d["Nombre"] not in seen_global:
                seen_global.add(d["Nombre"])
                all_doctors.append(d)

    save_to_excel(all_doctors, Path(args.output))


if __name__ == "__main__":
    main()
