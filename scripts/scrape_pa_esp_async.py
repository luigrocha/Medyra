#!/usr/bin/env python3
"""Scrape Cliniweb Panama por especialidad — fetch paralelo, sin delays."""
import asyncio, json
import httpx
import pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"
SEED = "639148726507040344"
BASE = "https://directorio.cliniweb.com"
HEADERS = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"}

SPECS = {
    "Alergología":          [{"cid": 18925, "pages": 3}, {"cid": 18138, "pages": 1}],
    "Pediatría":            [{"cid": 58,    "pages": 48}],
    "Medicina_General":     [{"cid": 35,    "pages": 39}],
    "Medicina_Interna":     [{"cid": 36,    "pages": 48}, {"cid": 20595, "pages": 1}],
    "Otorrinolaringología": [{"cid": 56,    "pages": 14}, {"cid": 57,   "pages": 1}],
}

# Cargar existentes
existing_df = pd.read_excel(OUTPUT_DIR / "medicos_panama_cliniweb_v2.xlsx", sheet_name="Todos")
EXISTING = set(existing_df["Nombre"].str.strip().str.upper())
print(f"Existentes: {len(EXISTING)}")

async def fetch_page(client, cid, page):
    url = (f"{BASE}/es/api/buscar/texto//?filters={cid}&page={page}"
           f"&idTipoConcepto=0&idConcepto=0&seed={SEED}&idEmpresaLocalidades=")
    r = await client.get(url, headers=HEADERS, timeout=20)
    raw = r.json()
    data = json.loads(raw["Data"]) if isinstance(raw.get("Data"), str) else raw.get("Data", {})
    return data.get("medicos", [])

def parse_doc(m, cid):
    nom = (m.get("nom") or "").strip()
    locs = m.get("locs") or [{}]
    loc0 = locs[0] if locs else {}
    return {
        "Nombre": nom,
        "Especialidad": m.get("esp", ""),
        "Subespecialidad": m.get("subs", ""),
        "Email": m.get("email", ""),
        "Telefono": loc0.get("telLoc") or m.get("telLoc", ""),
        "Consultorio": m.get("nomLoc", ""),
        "Direccion": loc0.get("dirLoc") or m.get("dirLoc", ""),
        "SitioWeb": m.get("sitioWeb", ""),
        "Perfil_URL": BASE + m.get("nav", "") if m.get("nav") else "",
        "Pais": "Panama",
        "Fuente": f"Cliniweb-Esp-{cid}",
    }

async def scrape_specialty(label, cid_infos):
    async with httpx.AsyncClient() as client:
        tasks = [
            fetch_page(client, info["cid"], p)
            for info in cid_infos
            for p in range(1, info["pages"] + 1)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    docs, seen = [], set()
    for medicos, info in zip(results, [
        (info["cid"], p)
        for info in cid_infos
        for p in range(1, info["pages"] + 1)
    ]):
        if isinstance(medicos, Exception):
            print(f"  Error: {medicos}")
            continue
        cid = info[0]
        for m in medicos:
            nom = (m.get("nom") or "").strip()
            key = nom.upper()
            if key and key not in seen and key not in EXISTING:
                seen.add(key)
                docs.append(parse_doc(m, cid))

    return docs

async def main():
    summary = {}
    for label, cid_infos in SPECS.items():
        print(f"\n=== {label} ===")
        docs = await scrape_specialty(label, cid_infos)
        summary[label] = len(docs)
        print(f"  → {len(docs)} nuevos")
        if docs:
            df = pd.DataFrame(docs)
            out = OUTPUT_DIR / f"PA_{label}_nuevos.csv"
            df.to_csv(out, index=False)
            print(f"  → {out.name}")

    print("\n=== RESUMEN ===")
    for k, v in summary.items():
        print(f"  {k}: {v} nuevos")

asyncio.run(main())
