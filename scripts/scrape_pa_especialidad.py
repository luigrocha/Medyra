#!/usr/bin/env python3
"""Scrape Cliniweb Panama filtrado por especialidad, deduplica contra existentes."""
import json, time, sys
import httpx
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"
SEED = "639148726507040344"
BASE = "https://directorio.cliniweb.com"

HEADERS = {
    "Accept": "application/json",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0",
}

SPECS = {
    "Alergología":         [18925, 18138],   # Alergología e Inmunología + Pediátrica
    "Pediatría":           [58],
    "Medicina_General":    [35],
    "Medicina_Interna":    [36, 20595],
    "Otorrinolaringología":[56, 57],
}

# Cargar nombres existentes para deduplicar
existing_df = pd.read_excel(OUTPUT_DIR / "medicos_panama_cliniweb_v2.xlsx", sheet_name="Todos")
existing_names = set(existing_df["Nombre"].str.strip().str.upper())
print(f"Existentes a deduplicar: {len(existing_names)}")

def scrape_specialty(concept_ids: list, label: str):
    all_docs = []
    seen = set()
    for cid in concept_ids:
        page = 1
        while True:
            url = (f"{BASE}/es/api/buscar/texto//?filters={cid}&page={page}"
                   f"&idTipoConcepto=0&idConcepto=0&seed={SEED}&idEmpresaLocalidades=")
            try:
                r = httpx.get(url, headers=HEADERS, timeout=15)
                raw = r.json()
                data = json.loads(raw["Data"]) if isinstance(raw.get("Data"), str) else raw.get("Data", {})
            except Exception as e:
                print(f"  Error p{page}: {e}")
                break

            medicos = data.get("medicos", [])
            if not medicos:
                break

            for m in medicos:
                nom = (m.get("nom") or "").strip()
                key = nom.upper()
                if key and key not in seen and key not in existing_names:
                    seen.add(key)
                    locs = m.get("locs") or [{}]
                    loc0 = locs[0] if locs else {}
                    all_docs.append({
                        "Nombre": nom,
                        "Especialidad": m.get("esp", ""),
                        "Subespecialidad": m.get("subs", ""),
                        "Email": m.get("email", ""),
                        "Telefono": loc0.get("telLoc", m.get("telLoc", "")),
                        "Consultorio": m.get("nomLoc", ""),
                        "Direccion": loc0.get("dirLoc", m.get("dirLoc", "")),
                        "SitioWeb": m.get("sitioWeb", ""),
                        "Perfil_URL": BASE + m.get("nav", "") if m.get("nav") else "",
                        "Pais": "Panama",
                        "Fuente": f"Cliniweb-Esp-{cid}",
                    })

            nresul = data.get("nresul", 0)
            total_pages = max(1, (nresul + 7) // 8)
            print(f"  cid={cid} p{page}/{total_pages} items={len(medicos)} nresul={nresul}")
            if page >= total_pages:
                break
            page += 1
            time.sleep(0.5)

    return all_docs


results = {}
for label, cids in SPECS.items():
    print(f"\n=== {label} ===")
    docs = scrape_specialty(cids, label)
    results[label] = docs
    print(f"  → {len(docs)} nuevos")
    if docs:
        df = pd.DataFrame(docs)
        df.to_csv(OUTPUT_DIR / f"PA_{label}_nuevos.csv", index=False)

print("\nResumen:")
for k, v in results.items():
    print(f"  {k}: {len(v)} nuevos")
