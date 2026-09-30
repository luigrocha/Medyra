#!/usr/bin/env python3
"""Scrape completo Cliniweb PA por especialidad (todas las paginas). Guarda RAW."""
import json, time, sys
import httpx, pandas as pd
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "output"
SEED = "639148726507040344"
BASE = "https://directorio.cliniweb.com"
H = {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"}

SPECS = {
    "Alergología":          [18925, 18138],
    "Pediatría":            [58],
    "Medicina_General":     [35],
    "Medicina_Interna":     [36, 20595],
    "Otorrinolaringología": [56, 57],
}

def scrape(label, cids, start_page=1, outfile=None):
    import csv, os
    rows, seen = [], set()
    fields = ["Nombre","Especialidad","Subespecialidad","Email","Telefono","Consultorio","Direccion","SitioWeb","Perfil_URL","Pais","Fuente"]
    write_header = not (outfile and os.path.exists(outfile))
    fh = open(outfile, "a", newline="", encoding="utf-8") if outfile else None
    writer = csv.DictWriter(fh, fieldnames=fields) if fh else None
    if writer and write_header: writer.writeheader()
    for cid in cids:
        page = start_page
        while True:
            url = (f"{BASE}/es/api/buscar/texto//?filters={cid}&page={page}"
                   f"&idTipoConcepto=0&idConcepto=0&seed={SEED}&idEmpresaLocalidades=")
            try:
                r = httpx.get(url, headers=H, timeout=20)
                raw = r.json()
                data = json.loads(raw["Data"]) if isinstance(raw.get("Data"), str) else raw.get("Data", {})
            except Exception as e:
                print(f"  {label} cid={cid} p{page} ERR {e}", flush=True); break
            medicos = data.get("medicos", [])
            if not medicos: break
            for m in medicos:
                nom = (m.get("nom") or "").strip()
                key = nom.upper()
                if not key or key in seen: continue
                seen.add(key)
                locs = m.get("locs") or [{}]
                loc0 = locs[0] if locs else {}
                # tomar el primer telefono disponible entre las localidades
                tel = ""
                for L in locs:
                    t = (L.get("telLoc") or "").strip()
                    if t: tel = t; break
                if not tel: tel = (m.get("telLoc") or "").strip()
                rows.append({
                    "Nombre": nom,
                    "Especialidad": m.get("esp", ""),
                    "Subespecialidad": m.get("subs", ""),
                    "Email": (m.get("email") or "").strip(),
                    "Telefono": tel,
                    "Consultorio": m.get("nomLoc", ""),
                    "Direccion": loc0.get("dirLoc", m.get("dirLoc", "")),
                    "SitioWeb": m.get("sitioWeb", ""),
                    "Perfil_URL": BASE + m.get("nav", "") if m.get("nav") else "",
                    "Pais": "Panama",
                    "Fuente": f"Cliniweb-{label}-{cid}",
                })
                if writer: writer.writerow(rows[-1])
            if fh: fh.flush()
            nresul = data.get("nresul", 0)
            total_pages = max(1, (nresul + 7) // 8)
            print(f"  {label} cid={cid} p{page}/{total_pages} acum={len(rows)}", flush=True)
            if page >= total_pages: break
            page += 1
            time.sleep(0.3)
    return rows

if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else None
    start = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    for label, cids in SPECS.items():
        if only and label != only: continue
        print(f"=== {label} (start p{start}) ===", flush=True)
        p = OUT / f"PA_RAW_{label}.csv"
        rows = scrape(label, cids, start_page=start, outfile=str(p))
        print(f"  -> {p.name}: +{len(rows)} filas (append)", flush=True)
