#!/usr/bin/env python3
"""
Scraper: Ecuador — Médicos Generales, Pediatras e Internistas por provincia (Azuay, Loja, Carchi)

Grupos de entrega (una hoja cada uno): la provincia pedida + vecinas que la complementan
  Azuay | Loja (+ El Oro, Zamora Chinchipe) | Carchi (+ Imbabura)
Cada fila lleva su Provincia real y Tipo = "Provincia pedida" / "Provincia vecina".

Fuentes (validadas 2026-09-30):
  - masquemedicos.ec   /{medicos-generales,pediatras,medicos-internistas}_{canton}/page/N/  (schema.org, con teléfono)
  - doctoranytime.ec   /s/{Medico-general,Pediatra}/{canton}?p=N   (JSON-LD, sin teléfono; sin categoría de interna)
  - ecuadoctor.com     /modules/directorio?qry={4,2,17}&cty=CIUDAD   (teléfono + email, pocos registros)
  Descartadas: ecuamedical (ahora solo Quito), Doctoralia (no opera en EC), citamedica (casi vacío),
  ACESS/datosabiertos (403, sin provincia), directorios de clínicas detrás de formularios.

Flujo:
  1. scrape  → data/output/EC_raw_{fuente}.json   (cache: re-correr no vuelve a pedir si existe; --refresh)
  2. build   → dedupe entre fuentes, provincia según dirección real, cruce con la base Ecuador existente
               (medicos-allegra-0226__Ecuador_clean.xlsx), tope opcional por grupo (--cap).
  Output: data/output/ecuador_azuay_loja_carchi.xlsx (hojas Resumen, Azuay, Loja, Carchi)

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
             ("saraguro", "Saraguro"), ("alamor", "Alamor"), ("catacocha", "Catacocha"), ("zapotillo", "Zapotillo"),
             ("celica", "Celica")],
    "Carchi": [("tulcan", "Tulcán"), ("san-gabriel", "San Gabriel"), ("el-angel", "El Ángel"), ("mira", "Mira"),
               ("huaca", "Huaca"), ("bolivar", "Bolívar")],
    # Provincias vecinas: complementan a Carchi (Imbabura) y a Loja (El Oro, Zamora Chinchipe)
    "Imbabura": [("ibarra", "Ibarra"), ("otavalo", "Otavalo"), ("cotacachi", "Cotacachi"),
                 ("atuntaqui", "Atuntaqui"), ("pimampiro", "Pimampiro"), ("urcuqui", "Urcuquí")],
    "El Oro": [("machala", "Machala"), ("pasaje", "Pasaje"), ("santa-rosa", "Santa Rosa"),
               ("huaquillas", "Huaquillas"), ("pinas", "Piñas"), ("zaruma", "Zaruma"),
               ("el-guabo", "El Guabo"), ("arenillas", "Arenillas"), ("portovelo", "Portovelo")],
    "Zamora Chinchipe": [("zamora", "Zamora"), ("yantzaza", "Yantzaza"), ("zumba", "Zumba")],
}
# Provincia real → grupo de entrega (hoja del Excel)
GRUPO = {"Azuay": "Azuay", "Loja": "Loja", "Carchi": "Carchi",
         "Imbabura": "Carchi", "El Oro": "Loja", "Zamora Chinchipe": "Loja"}
GRUPOS = ["Azuay", "Loja", "Carchi"]
ESPECIALIDADES = {
    "MEDICINA_GENERAL": {"masquemedicos": "medicos-generales", "doctoranytime": "Medico-general"},
    "PEDIATRIA": {"masquemedicos": "pediatras", "doctoranytime": "Pediatra"},
    # doctoranytime.ec no tiene categoría de medicina interna
    "MEDICINA_INTERNA": {"masquemedicos": "medicos-internistas", "doctoranytime": None},
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
                if not paths["doctoranytime"]:
                    continue
                base = f"https://www.doctoranytime.ec/s/{paths['doctoranytime']}/{slug}"
                n, page = 0, 1
                while page <= 15:
                    url = base if page == 1 else f"{base}?p={page}"
                    try:
                        r = session.get(url, headers=HEADERS)
                    except httpx.HTTPError as e:
                        print(f"    ! {url}: {e}")
                        break
                    if r.status_code != 200:
                        break
                    page_rows = []
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
                            page_rows.append({
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
                    rows += page_rows
                    n += len(page_rows)
                    # Para cantones chicos el sitio rellena con médicos "cerca de" (la capital):
                    # solo se pagina mientras la página sea mayormente del cantón buscado
                    in_canton = [x for x in page_rows
                                 if strip_accents(x["Ciudad"]).upper() == strip_accents(canton).upper()]
                    if len(page_rows) < 20 or len(in_canton) < len(page_rows) / 2:
                        break
                    page += 1
                    time.sleep(SLEEP)
                print(f"  [doctoranytime] {canton} {esp}: {n}")
                time.sleep(SLEEP)
    return rows


# --------------------------------------------------------------------------- ecuadoctor
ECUADOCTOR_QRY = {"MEDICINA_GENERAL": 4, "PEDIATRIA": 2, "MEDICINA_INTERNA": 17}
# Ciudades que el sitio tiene en su selector, dentro de las 3 provincias
ECUADOCTOR_CTY = {"CUENCA": "Azuay", "GUALACEO": "Azuay", "LOJA": "Loja", "CATAMAYO": "Loja",
                  "MACARA": "Loja", "TULCAN": "Carchi",
                  "IBARRA": "Imbabura", "OTAVALO": "Imbabura", "ATUNTAQUI": "Imbabura", "COTACACHI": "Imbabura",
                  "MACHALA": "El Oro", "SANTA ROSA | EL ORO": "El Oro",
                  "YANTZAZA": "Zamora Chinchipe", "YACUAMBI": "Zamora Chinchipe"}
ECUADOCTOR_ESP_OK = {"MEDICINA_GENERAL": re.compile(r"MEDIC(INA|O) GENERAL", re.I),
                     "PEDIATRIA": re.compile(r"PEDIATR", re.I),
                     "MEDICINA_INTERNA": re.compile(r"INTERN", re.I)}


def scrape_ecuadoctor(session: httpx.Client) -> list[dict]:
    rows: list[dict] = []
    for cty, prov in ECUADOCTOR_CTY.items():
        for esp, qry in ECUADOCTOR_QRY.items():
            url = "https://www.ecuadoctor.com/modules/directorio"
            try:
                r = session.get(url, params={"qry": qry, "cty": cty}, headers=HEADERS)
            except httpx.HTTPError as e:
                print(f"    ! {url}: {e}")
                continue
            soup = HTMLParser(r.text)
            seen, n = set(), 0
            for card in soup.css("div#filaped"):
                m = re.search(r"abremedico\((\d+)", card.attributes.get("onclick") or "")
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                esp_el = card.css_first("#nombremed2")
                esp_txt = esp_el.text(strip=True) if esp_el else ""
                if not ECUADOCTOR_ESP_OK[esp].search(strip_accents(esp_txt)):
                    continue
                a = card.css_first("#nombremed a")
                dire = card.css_first("#direcmed p")
                fono = card.css_first("#fonomed p")
                mail = card.css_first("#mailmed p")
                addr = dire.text(separator=" ", strip=True) if dire else ""
                phones = re.findall(r"\(?0\d\)?[\s-]?\d{6,8}|\b0?9\d{8}\b|\b[2-7]\d{6}\b", fono.text() if fono else "")
                rows.append({
                    "Fuente": "ecuadoctor",
                    "Nombre": re.sub(r"^(Dra?\.?)\s+", "", a.text(strip=True) if a else "").strip(),
                    "Especialidad": esp,
                    "Canton_busqueda": cty.split("|")[0].strip().title(),
                    "Provincia_busqueda": prov,
                    "Ciudad": addr.rsplit("|", 1)[-1].strip().title() if "|" in addr else cty.split("|")[0].strip().title(),
                    "Direccion": addr,
                    "Telefono_raw": phones[0] if phones else "",
                    "Email_raw": (mail.text(strip=True) if mail else ""),
                    "URL": f"https://www.ecuadoctor.com/modules/medico?id={m.group(1)}",
                })
                n += 1
            print(f"  [ecuadoctor] {cty} {esp}: {n}")
            time.sleep(SLEEP)
    return rows


# --------------------------------------------------------------------------- LOTAIP
# Directorios institucionales publicados por ley (LOTAIP art. 7 literal b1): nombre, puesto, ciudad,
# teléfono y email institucional de cada servidor. Formato estándar en todo el sector público.
LOTAIP_DOCS = [
    # (fuente, fecha, url, archivo cache)
    ("iess", "2023-06", "https://www.iess.gob.ec/informacion/Transparencia/Junio_2023/Literal_b1.pdf",
     "iess_lotaip_b1_2023-06.pdf"),
    # Hospital San Vicente de Paúl (MSP, Ibarra) — carpeta LOTAIP 2024 en hsvp.gob.ec
    ("hsvp_ibarra", "2024", "https://drive.google.com/uc?export=download&id=18eSQRIm6UEfXbyVYPDz4XsyNu9yQCuSh",
     "hsvp_lotaip_b1_2024.pdf"),
]
LOTAIP_PUESTO = [
    (re.compile(r"^MEDICO/A GENERAL\b"), "MEDICINA_GENERAL"),
    (re.compile(r"^MEDICO/A ESPECIALISTA EN PEDIATRIA\b"), "PEDIATRIA"),
    (re.compile(r"^MEDICO/A ESPECIALISTA EN MEDICINA INTERNA\b"), "MEDICINA_INTERNA"),
]
_LOTAIP_PHONE = re.compile(r" (\(593\)\s*0?\d[\d ]{6,10}\d|0\d{8})\b")


def parse_lotaip(pdf: Path, fuente: str, fecha: str, url: str) -> list[dict]:
    from pypdf import PdfReader  # solo estas fuentes lo necesitan

    txt = "\n".join(pg.extract_text() for pg in PdfReader(pdf).pages)
    txt = re.sub(r"No\. Apellidos y Nombres.*?Correo Electr[oó]nico institucional", "", txt, flags=re.S)
    txt = re.sub(r"Art\. 7 de la Ley.*?\n|Literal b1\).*?\n|logotipo institucional imagen jpg", "", txt)
    cantones = [(strip_accents(nombre).upper(), prov, nombre) for prov, lst in PROVINCIAS.items() for _, nombre in lst]
    rows: list[dict] = []
    prev = 0
    # Cada registro termina en su email: se corta el texto email a email
    for m in re.finditer(r"[\w.+-]+@[\w.-]+\.\w+", txt):
        rec = re.sub(r"\s+", " ", txt[prev:m.start()]).strip()
        prev = m.end()
        mm = re.match(r"\d+ (.+?) (M[EÉ]DICO/A .+)$", rec, re.I)
        if not mm:
            continue
        nombre, resto = mm.groups()
        resto_up = strip_accents(resto).upper()
        esp = next((e for rx, e in LOTAIP_PUESTO if rx.search(resto_up)), None)
        if not esp:
            continue
        ph = _LOTAIP_PHONE.search(resto)
        if not ph:
            continue
        antes = resto_up[:ph.start()]
        hit = next(((prov, canon) for c, prov, canon in cantones if antes.endswith(" " + c)), None)
        if not hit:
            continue
        prov, canon = hit
        rows.append({
            "Fuente": fuente,
            "Nombre": nombre.title(),  # formato APELLIDOS NOMBRES
            "Especialidad": esp,
            "Canton_busqueda": canon,
            "Provincia_busqueda": prov,
            "Ciudad": canon,
            "Direccion": resto[:ph.start()],
            "Telefono_raw": re.sub(r"^\(593\)\s*", "", ph.group(1)),
            # el PDF a veces corta el ".ec" final en otra línea
            "Email_raw": m.group(0) + (".ec" if m.group(0).lower().endswith(".gob") else ""),
            "URL": url,
            "Fecha_fuente": fecha,
        })
    return rows


def scrape_lotaip(session: httpx.Client, cache_dir: Path) -> list[dict]:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("  [lotaip] falta pypdf (pip install pypdf) → se omite la fuente")
        return []
    rows: list[dict] = []
    for fuente, fecha, url, fname in LOTAIP_DOCS:
        pdf = cache_dir / fname
        if not pdf.exists():
            print(f"  [lotaip] descargando {fuente}: {url}")
            with session.stream("GET", url, headers=HEADERS, timeout=300) as r:
                r.raise_for_status()
                with pdf.open("wb") as f:
                    for chunk in r.iter_bytes():
                        f.write(chunk)
        got = parse_lotaip(pdf, fuente, fecha, url)
        print(f"  [lotaip] {fuente} ({fecha}): {len(got)} médicos MG/Ped/Interna en las provincias objetivo")
        rows += got
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
        tel = re.sub(r"[^\d]", "", str(r.get("Telefono_raw") or ""))
        if len(tel) == 7:  # fijo sin código de área
            tel = ("06" if prov in ("Carchi", "Imbabura") else "07") + tel
        ph = normalize_phone(tel, "EC")
        email = str(r.get("Email_raw") or "").strip().lower()
        email = email if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", email) else ""
        ciudad_key = strip_accents(r.get("Ciudad") or "").upper().strip()
        ciudad = c2p[ciudad_key][1] if ciudad_key in c2p else (r.get("Ciudad") or "")
        recs.append({**r, "Ciudad": ciudad, "Provincia": prov, "Grupo": GRUPO[prov], "_tokens": toks,
                     "Telefono": ph.e164 if ph else "", "Tipo_Telefono": ph.line_type if ph else "",
                     "Email": email})

    # Dedupe: misma provincia + especialidad, tokens del nombre corto ⊂ nombre largo. Preferir masquemedicos (trae tel.)
    SRC_RANK = {"masquemedicos": 0, "iess": 1, "hsvp_ibarra": 1, "ecuadoctor": 2, "doctoranytime": 3}
    recs.sort(key=lambda x: (SRC_RANK.get(x["Fuente"], 9), -len(x["_tokens"])))
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
            if not dup.get("Email") and r.get("Email"):
                dup["Email"] = r["Email"]
            if not dup["Telefono"] and r["Telefono"]:
                dup["Telefono"], dup["Tipo_Telefono"] = r["Telefono"], r["Tipo_Telefono"]
        else:
            kept.append(r)

    allegra = load_allegra(data_dir)
    for r in kept:
        r["Existente_Allegra"] = "SI" if in_allegra(r["_tokens"], allegra) else "NO"

    df = pd.DataFrame(kept)
    df["_score"] = (df["Telefono"] != "").astype(int) + (df["Email"] != "").astype(int)
    print("  disponibles:", df.groupby(["Provincia", "Especialidad"]).size().to_dict())
    # Orden de prioridad dentro de cada grupo: provincia pedida antes que vecinas,
    # pediatras (escasos) > medicina general > medicina interna, luego con teléfono/email
    df["_vecina"] = (df["Provincia"] != df["Grupo"]).astype(int)
    df["_esp"] = df["Especialidad"].map({"PEDIATRIA": 0, "MEDICINA_GENERAL": 1, "MEDICINA_INTERNA": 2})
    df = df.sort_values(["Grupo", "_vecina", "_esp", "_score"], ascending=[True, True, True, False])
    if cap:
        df = df.groupby("Grupo", group_keys=False).head(cap)
    df["Tipo"] = df["_vecina"].map({0: "Provincia pedida", 1: "Provincia vecina"})
    cols = ["Grupo", "Provincia", "Tipo", "Ciudad", "Especialidad", "Nombre", "Telefono", "Tipo_Telefono", "Email", "Direccion", "Fecha_fuente",
            "Fuente", "Existente_Allegra", "URL", "URL_2"]
    for c in cols:
        if c not in df:
            df[c] = ""
    return df[cols].fillna("").sort_values(["Grupo", "Tipo", "Provincia", "Especialidad", "Ciudad", "Nombre"])


def write_excel(df: pd.DataFrame, out: Path, meta: int) -> None:
    resumen = []
    for grupo in GRUPOS:
        d = df[df.Grupo == grupo]
        pedida = d[d.Provincia == grupo]
        resumen.append({
            "Grupo": grupo,
            "Medicina General": int((d.Especialidad == "MEDICINA_GENERAL").sum()),
            "Pediatría": int((d.Especialidad == "PEDIATRIA").sum()),
            "Medicina Interna": int((d.Especialidad == "MEDICINA_INTERNA").sum()),
            "Total": len(d),
            "Provincia pedida (MG+Ped)": int((pedida.Especialidad != "MEDICINA_INTERNA").sum()),
            "Por provincia": ", ".join(f"{k}: {v}" for k, v in d.Provincia.value_counts().items()),
            "Meta": meta,
            "Brecha": max(meta - len(d), 0),
            "Con teléfono": int((d.Telefono != "").sum()),
            "Con email": int((d.Email != "").sum()),
            "Con tel + email": int(((d.Telefono != "") & (d.Email != "")).sum()),
            "Nuevos (no en Allegra)": int((d.Existente_Allegra == "NO").sum()),
            "Fuentes": ", ".join(f"{k}: {v}" for k, v in d.Fuente.value_counts().items()),
        })
    with pd.ExcelWriter(out, engine="openpyxl") as xw:
        pd.DataFrame(resumen).to_excel(xw, sheet_name="Resumen", index=False)
        # Contacto completo = teléfono Y email (requisito de uso comercial)
        df[(df.Telefono != "") & (df.Email != "")].to_excel(xw, sheet_name="Tel+Email", index=False)
        for grupo in GRUPOS:
            df[df.Grupo == grupo].to_excel(xw, sheet_name=grupo, index=False)
        for ws in xw.book.worksheets:
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
            ws.freeze_panes = "A2"
    print(pd.DataFrame(resumen).to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", type=Path, default=ROOT / "data")
    ap.add_argument("--cap", type=int, default=0, help="máximo por provincia (0 = sin tope)")
    ap.add_argument("--meta", type=int, default=500)
    ap.add_argument("--refresh", action="store_true", help="ignorar cache JSON y volver a scrapear")
    args = ap.parse_args()
    out_dir = args.data_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)

    session = httpx.Client(follow_redirects=True, timeout=20)
    raw: list[dict] = []
    for name, fn in [("masquemedicos", scrape_masquemedicos), ("doctoranytime", scrape_doctoranytime),
                     ("ecuadoctor", scrape_ecuadoctor),
                     ("lotaip", lambda s: scrape_lotaip(s, out_dir))]:
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
    write_excel(df, out, args.meta)
    print(f"\n→ {out}")


if __name__ == "__main__":
    main()
