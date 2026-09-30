#!/usr/bin/env python3
"""
Scraper: Ecuador — Médicos Generales + Pediatras por provincia (Azuay, Loja, Carchi)

Fuentes (validadas 2026-09-30):
  - masquemedicos.ec   /{medicos-generales,pediatras}_{canton}/page/N/   (schema.org, con teléfono)
  - doctoranytime.ec   /s/{Medico-general,Pediatra}/{canton}              (JSON-LD, solo pág. 1 estática, sin teléfono)
  Descartadas: ecuamedical (ahora solo Quito), Doctoralia (no opera en EC),
  citamedica (6 médicos en Cuenca), ACESS/datosabiertos (403, sin provincia).

Flujo:
  1. scrape  → data/output/EC_raw_{fuente}.json   (cache: re-correr no vuelve a pedir si existe; --refresh)
  2. build   → dedupe entre fuentes, filtro por provincia, cruce con la base Ecuador existente
               (medicos-allegra-0226__Ecuador_clean.xlsx) y cap por provincia.
  Output: data/output/ecuador_azuay_loja_carchi.xlsx (hojas Azuay, Loja, Carchi, Resumen)

Uso:
  python scripts/scrape_ecuador_provincias.py --data-dir "<repo>/data"
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
import pandas as pd
from selectolax.parser import HTMLParser

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from medintel.normalization.phones import normalize as normalize_phone  # noqa: E402

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "es-EC,es;q=0.9",
}
SLEEP = 0.8

# Provincia → cantones (slug, nombre). Toda la provincia, no solo la capital.
PROVINCIAS: dict[str, list[tuple[str, str]]] = {
    "Azuay": [("cuenca", "Cuenca"), ("gualaceo", "Gualaceo"), ("paute", "Paute"), ("sigsig", "Sígsig"),
              ("santa-isabel", "Santa Isabel"), ("giron", "Girón"), ("chordeleg", "Chordeleg"),
              ("camilo-ponce-enriquez", "Camilo Ponce Enríquez"), ("nabon", "Nabón")],
    "Loja": [("loja", "Loja"), ("catamayo", "Catamayo"), ("cariamanga", "Cariamanga"), ("macara", "Macará"),
             ("saraguro", "Saraguro"), ("alamor", "Alamor"), ("catacocha", "Catacocha"), ("zapotillo", "Zapotillo")],
    "Carchi": [("tulcan", "Tulcán"), ("san-gabriel", "San Gabriel"), ("el-angel", "El Ángel"), ("mira", "Mira"),
               ("huaca", "Huaca"), ("bolivar", "Bolívar")],
}
ESPECIALIDADES = {
    "MEDICINA_GENERAL": {"masquemedicos": "medicos-generales", "doctoranytime": "Medico-general"},
    "PEDIATRIA": {"masquemedicos": "pediatras", "doctoranytime": "Pediatra"},
}
TITLES = {"DR", "DRA", "DOCTOR", "DOCTORA", "MD", "MG", "ESP", "MSC", "LIC"}
# Fichas que son establecimientos, no personas
NON_PERSON = re.compile(r"\b(CLINICA|CENTRO|CONSULTORIO|HOSPITAL|MEDICAL|MEDICO|MEDICA|DISPENSARIO|LABORATORIO|SALUD|FARMACIA|POLICLINICO|SERVICIOS|CIA|S\.?A\.?)\b")


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def name_tokens(name: str) -> list[str]:
    s = strip_accents(name).upper()
    s = re.sub(r"[^A-ZÑ ]", " ", s)
    return [t for t in s.split() if t not in TITLES and len(t) > 1]


def canton_to_prov() -> dict[str, tuple[str, str]]:
    out = {}
    for prov, cantones in PROVINCIAS.items():
        for _, nombre in cantones:
            out[strip_accents(nombre).upper()] = (prov, nombre)
    return out


# --------------------------------------------------------------------------- masquemedicos
def scrape_masquemedicos(session: httpx.Client) -> list[dict]:
    rows: list[dict] = []
    for prov, cantones in PROVINCIAS.items():
        for slug, canton in cantones:
            for esp, paths in ESPECIALIDADES.items():
                base = f"https://masquemedicos.ec/{paths['masquemedicos']}_{slug}/"
                page, last = 1, 1
                while page <= last:
                    url = base if page == 1 else f"{base}page/{page}/"
                    try:
                        r = session.get(url, headers=HEADERS)
                    except httpx.HTTPError as e:
                        print(f"    ! {url}: {e}")
                        break
                    if r.status_code != 200:
                        break
                    soup = HTMLParser(r.text)
                    h1 = soup.css_first("h1")
                    # Sin resultados propios el sitio redirige a una página genérica
                    if page == 1 and not soup.css_first("#info_num_results"):
                        break
                    if page == 1:
                        nums = [int(m) for m in re.findall(rf"/{re.escape(paths['masquemedicos'])}_{re.escape(slug)}/page/(\d+)/", r.text)]
                        last = max(nums, default=1)
                        print(f"  [masquemedicos] {canton} {esp}: {last} pág. ({h1.text(separator=' ', strip=True) if h1 else ''})")
                    # Cada ficha es div.negocio; el itemtype varía (physician / MedicalClinic)
                    cards = soup.css("div.negocio")
                    for c in cards:
                        a = c.css_first('a[itemprop="url"]')
                        name_el = c.css_first('[itemprop="name"]')
                        if not name_el:
                            continue
                        street = c.css_first('[itemprop="streetAddress"]')
                        loc = c.css_first('[itemprop="addressLocality"]')
                        tel = c.css_first('[itemprop="telephone"]')
                        href = (a.attributes.get("href") or "") if a else ""
                        rows.append({
                            "Fuente": "masquemedicos",
                            "Nombre": name_el.text(separator=" ", strip=True),
                            "Especialidad": esp,
                            "Canton_busqueda": canton,
                            "Provincia_busqueda": prov,
                            "Ciudad": loc.text(strip=True) if loc else "",
                            "Direccion": street.text(separator=" ", strip=True) if street else "",
                            "Telefono_raw": (tel.attributes.get("content") or tel.text(strip=True)) if tel else "",
                            "URL": "https://masquemedicos.ec" + href if href.startswith("/") else href,
                        })
                    page += 1
                    time.sleep(SLEEP)
    return rows


# --------------------------------------------------------------------------- doctoranytime
def _iter_physicians(obj):
    if isinstance(obj, dict):
        if obj.get("@type") == "Physician":
            yield obj
        for v in obj.values():
            yield from _iter_physicians(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_physicians(v)


def scrape_doctoranytime(session: httpx.Client) -> list[dict]:
    rows: list[dict] = []
    for prov, cantones in PROVINCIAS.items():
        for slug, canton in cantones:
            for esp, paths in ESPECIALIDADES.items():
                url = f"https://www.doctoranytime.ec/s/{paths['doctoranytime']}/{slug}"
                try:
                    r = session.get(url, headers=HEADERS)
                except httpx.HTTPError as e:
                    print(f"    ! {url}: {e}")
                    continue
                if r.status_code != 200:
                    continue
                n = 0
                for block in re.findall(r'<script type="application/ld(?:\+|&#x2B;)json">(.*?)</script>', r.text, re.S):
                    try:
                        data = json.loads(block)
                    except json.JSONDecodeError:
                        continue
                    for p in _iter_physicians(data):
                        addrs = p.get("address") or []
                        if isinstance(addrs, dict):
                            addrs = [addrs]
                        a0 = addrs[0] if addrs else {}
                        urls = p.get("url")
                        rows.append({
                            "Fuente": "doctoranytime",
                            "Nombre": re.sub(r"\b(Dra?\.?)\s*$", "", p.get("name", "")).strip(),
                            "Especialidad": esp,
                            "Canton_busqueda": canton,
                            "Provincia_busqueda": prov,
                            "Ciudad": a0.get("addressLocality", ""),
                            "Region": a0.get("addressRegion", ""),
                            "Direccion": a0.get("streetAddress", ""),
                            "Telefono_raw": p.get("telephone", ""),
                            "URL": (urls[0] if isinstance(urls, list) and urls else urls) or p.get("@id", "").split("#")[0],
                        })
                        n += 1
                print(f"  [doctoranytime] {canton} {esp}: {n}")
                time.sleep(SLEEP)
    return rows


# --------------------------------------------------------------------------- build
def assign_provincia(row: dict, c2p: dict) -> str | None:
    """Provincia real según la dirección, no según la búsqueda (los sitios mezclan 'cercanos' y teleconsulta)."""
    region = strip_accents(row.get("Region") or "").upper()
    for prov in PROVINCIAS:
        if region == prov.upper():
            return prov
    ciudad = strip_accents(row.get("Ciudad") or "").upper().strip()
    if ciudad in c2p:
        return c2p[ciudad][0]
    if ciudad in {p.upper() for p in PROVINCIAS}:
        return ciudad.title()
    if not ciudad and row["Fuente"] == "masquemedicos":
        return row["Provincia_busqueda"]
    return None


def load_allegra(data_dir: Path) -> dict[str, list[set[str]]]:
    f = data_dir / "output" / "medicos-allegra-0226__Ecuador_clean.xlsx"
    if not f.exists():
        print(f"  (sin base Ecuador existente en {f})")
        return {}
    df = pd.read_excel(f)
    df = df[df["Especialidad"].isin(ESPECIALIDADES)]
    idx: dict[str, list[set[str]]] = {}
    for _, r in df.iterrows():
        toks = set(name_tokens(str(r.get("Nombre", ""))))
        ap1 = name_tokens(str(r.get("Apellido_1", "") or ""))
        if ap1 and toks:
            idx.setdefault(ap1[0], []).append(toks)
    return idx


def in_allegra(tokens: list[str], idx: dict[str, list[set[str]]]) -> bool:
    ts = set(tokens)
    for t in tokens:
        for cand in idx.get(t, []):
            common = ts & cand
            # apellido + al menos un nombre (≥2 tokens), y ≥ 2/3 del nombre más corto
            if len(common) >= 2 and len(common) >= 0.66 * min(len(ts), len(cand)):
                return True
    return False


def build(raw: list[dict], data_dir: Path, cap: int) -> pd.DataFrame:
    c2p = canton_to_prov()
    recs = []
    for r in raw:
        prov = assign_provincia(r, c2p)
        if not prov:
            continue
        toks = name_tokens(r["Nombre"])
        if len(toks) < 2 or NON_PERSON.search(strip_accents(r["Nombre"]).upper()):
            continue
        ph = normalize_phone(str(r.get("Telefono_raw") or ""), "EC")
        recs.append({**r, "Provincia": prov, "_tokens": toks,
                     "Telefono": ph.e164 if ph else "", "Tipo_Telefono": ph.line_type if ph else ""})

    # Dedupe: misma provincia + especialidad, tokens del nombre corto ⊂ nombre largo. Preferir masquemedicos (trae tel.)
    recs.sort(key=lambda x: (x["Fuente"] != "masquemedicos", -len(x["_tokens"])))
    kept: list[dict] = []
    for r in recs:
        ts = set(r["_tokens"])
        dup = None
        for k in kept:
            if k["Provincia"] == r["Provincia"] and k["Especialidad"] == r["Especialidad"]:
                ks = set(k["_tokens"])
                small, big = (ts, ks) if len(ts) <= len(ks) else (ks, ts)
                if small <= big:
                    dup = k
                    break
        if dup:
            if r["Fuente"] not in dup["Fuente"]:
                dup["Fuente"] += f"+{r['Fuente']}"
                dup["URL_2"] = r["URL"]
            if not dup["Telefono"] and r["Telefono"]:
                dup["Telefono"], dup["Tipo_Telefono"] = r["Telefono"], r["Tipo_Telefono"]
        else:
            kept.append(r)

    allegra = load_allegra(data_dir)
    for r in kept:
        r["Existente_Allegra"] = "SI" if in_allegra(r["_tokens"], allegra) else "NO"

    df = pd.DataFrame(kept)
    df["_score"] = (df["Telefono"] != "").astype(int)
    print("  disponibles antes del cap:", df.groupby(["Provincia", "Especialidad"]).size().to_dict())
    # Pediatras son escasos → se priorizan; luego registros con teléfono
    df = (df.sort_values(["Provincia", "Especialidad", "_score"], ascending=[True, False, False])
            .groupby("Provincia", group_keys=False).head(cap))
    cols = ["Provincia", "Ciudad", "Especialidad", "Nombre", "Telefono", "Tipo_Telefono", "Direccion",
            "Fuente", "Existente_Allegra", "URL", "URL_2"]
    for c in cols:
        if c not in df:
            df[c] = ""
    return df[cols].fillna("").sort_values(["Provincia", "Especialidad", "Ciudad", "Nombre"])


def write_excel(df: pd.DataFrame, out: Path, meta: int) -> None:
    resumen = []
    for prov in PROVINCIAS:
        d = df[df.Provincia == prov]
        resumen.append({
            "Provincia": prov,
            "Medicina General": int((d.Especialidad == "MEDICINA_GENERAL").sum()),
            "Pediatría": int((d.Especialidad == "PEDIATRIA").sum()),
            "Total": len(d),
            "Meta": meta,
            "Brecha": max(meta - len(d), 0),
            "Con teléfono": int((d.Telefono != "").sum()),
            "Nuevos (no en Allegra)": int((d.Existente_Allegra == "NO").sum()),
            "Fuentes": ", ".join(f"{k}: {v}" for k, v in d.Fuente.value_counts().items()),
        })
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame(resumen).to_excel(xw, sheet_name="Resumen", index=False)
        for prov in PROVINCIAS:
            df[df.Provincia == prov].to_excel(xw, sheet_name=prov, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
            ws.freeze_panes = "A2"
    print(pd.DataFrame(resumen).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--cap", type=int, default=500)
    ap.add_argument("--refresh", action="store_true", help="ignorar cache JSON y volver a scrapear")
    args = ap.parse_args()
    out_dir = args.data_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = httpx.Client(follow_redirects=True, timeout=20)
    raw: list[dict] = []
    for name, fn in [("masquemedicos", scrape_masquemedicos), ("doctoranytime", scrape_doctoranytime)]:
        cache = out_dir / f"EC_raw_{name}.json"
        if cache.exists() and not args.refresh:
            rows = json.loads(cache.read_text())
            print(f"  cache {cache.name}: {len(rows)}")
        else:
            rows = fn(session)
            cache.write_text(json.dumps(rows, ensure_ascii=False, indent=1))
        raw += rows

    df = build(raw, args.data_dir, args.cap)
    out = out_dir / "ecuador_azuay_loja_carchi.xlsx"
    write_excel(df, out, args.cap)
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
