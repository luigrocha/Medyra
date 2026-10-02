#!/usr/bin/env python3
"""
Scraper Doctoralia Colombia — ginecología (y cualquier otra especialidad).

Por qué Doctoralia y no Cliniweb / registro oficial:
  - Cliniweb NO opera en Colombia (probado: /bogota devuelve nresul=0).
  - RETHUS en datos.gov.co (dataset my8c-6xkk) es AGREGADO — solo conteos por
    perfil/departamento, sin nombres individuales. Inservible para contactos.
  - Doctoralia.co sí opera en Colombia: 146 páginas × ~25 = ~3.5k ginecólogos,
    con teléfono y dirección de consultorio en el HTML (sin JS).

Cómo salen los teléfonos sin JS:
  El botón "Mostrar número de teléfono" abre un modal cuyo selector lleva el
  número completo embebido:  data-id="address-73872-6013557209-1-phone".
  Es decir, el número YA está en el DOM; el "gate" es puramente visual.

Dos fases, ambas reanudables:
  1. listado  → /ginecologo?page=N   (nombre, especialidad, ciudad, perfil_url)
  2. perfiles → /perfil/{slug}       (teléfonos, direcciones, depto, EPS, precio)

Uso:
    python3 scripts/scrape_doctoralia_co.py --phase all
    python3 scripts/scrape_doctoralia_co.py --phase listing
    python3 scripts/scrape_doctoralia_co.py --phase profiles --limit 200
    python3 scripts/scrape_doctoralia_co.py --phase export
    python3 scripts/scrape_doctoralia_co.py --specialty dermatologo --phase all

Salida:
    data/output/medicos_colombia_ginecologia.xlsx
    data/output/doctoralia_co_listing.jsonl   (fase 1, reanudable)
    data/output/doctoralia_co_profiles.jsonl  (fase 2, reanudable)
    data/output/scraper_state_co.json
"""
from __future__ import annotations

import argparse
import asyncio
import html as html_mod
import json
import logging
import random
import re
import shutil
import sys
from datetime import date
from pathlib import Path

try:
    import httpx
    import pandas as pd
    import phonenumbers
except ImportError as e:  # pragma: no cover - guardarraíl de entorno
    print(f"ERROR: Dependencia faltante: {e}")
    print("Instalar con: pip3 install httpx pandas openpyxl phonenumbers")
    sys.exit(1)

# ── Rutas ────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = OUTPUT_DIR / "scraper_state_co.json"
LISTING_JSONL = OUTPUT_DIR / "doctoralia_co_listing.jsonl"
PROFILES_JSONL = OUTPUT_DIR / "doctoralia_co_profiles.jsonl"
OUTPUT_XLSX = OUTPUT_DIR / "medicos_colombia_ginecologia.xlsx"

# ── Config ────────────────────────────────────────────────────────────────────
BASE = "https://www.doctoralia.co"
DEFAULT_SPECIALTY = "ginecologo"
FUENTE = "Doctoralia CO"
PAIS = "Colombia"

# El WAF de Doctoralia responde 405 (no 403) a requests con headers "de bot".
# Verificado en vivo: el mismo GET pasa de 405 a 200 al mandar el set completo de
# headers de navegador — Sec-Fetch-*, Upgrade-Insecure-Requests y Accept-Encoding
# son los que importan. NO recortar este diccionario.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": f"{BASE}/",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Estado / JSONL ────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # línea truncada por un kill a mitad de escritura
    return rows


def append_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ── HTTP con reintentos ───────────────────────────────────────────────────────

async def fetch(client: httpx.AsyncClient, url: str, *, retries: int = 3) -> str | None:
    for attempt in range(retries):
        try:
            r = await client.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
            if r.status_code == 200:
                return r.text
            if r.status_code == 404:
                return None
            # 405 es la respuesta del WAF a un request que "huele a bot" o a
            # demasiado ritmo — reintentar con backoff, no descartar.
            if r.status_code in (403, 405, 429, 500, 502, 503):
                wait = (2 ** attempt) * 3 + random.uniform(0, 2)
                log.warning(f"  {r.status_code} en {url} — reintento en {wait:.0f}s")
                await asyncio.sleep(wait)
                continue
            log.warning(f"  HTTP {r.status_code} en {url}")
            return None
        except Exception as e:
            wait = (2 ** attempt) * 2 + random.uniform(0, 2)
            log.warning(f"  error {type(e).__name__} en {url} — reintento en {wait:.0f}s")
            await asyncio.sleep(wait)
    return None


# ── Fase 1: listado ───────────────────────────────────────────────────────────

_CARD_SPLIT = 'data-test-id="result-item"'


def _attr(card: str, name: str) -> str:
    m = re.search(re.escape(name) + r'="([^"]*)"', card)
    return html_mod.unescape(m.group(1)).strip() if m else ""


def parse_listing(page_html: str) -> list[dict]:
    """Cada card del listado trae los datos en data-attributes (sin JS)."""
    out = []
    for card in page_html.split(_CARD_SPLIT)[1:]:
        card = card[:4000]  # el bloque útil está al inicio del card
        url = _attr(card, "data-doctor-url")
        nombre = _attr(card, "data-doctor-name")
        if not url or not nombre:
            continue
        # El listado mezcla médicos y clínicas ("Clínica Marly", "Selika 94").
        # Solo queremos personas.
        if _attr(card, "data-test-entity-type") == "facility":
            continue
        out.append({
            "result_id":     _attr(card, "data-result-id"),
            "nombre_raw":    nombre,
            "perfil_url":    url,
            "especialidad":  _attr(card, "data-eec-specialization-name"),
            "ciudad":        _attr(card, "data-eec-address-cities"),
            "rating":        _attr(card, "data-eec-stars-rating"),
            "opiniones":     _attr(card, "data-eec-opinions-count"),
        })
    return out


_MAXPAGE_RE = re.compile(r"[?&]page=(\d+)")


async def discover_cities(client: httpx.AsyncClient, specialty: str) -> list[str]:
    """El listado nacional corta en 146 páginas; barrer por ciudad recupera la cola.

    Descubrimiento en 2 niveles: el índice nacional lista las ~20 ciudades
    grandes, y cada página de ciudad enlaza sus municipios satélite
    (Bogotá → Chía, Cajicá, Zipaquirá…).
    """
    def city_links(body: str) -> set[str]:
        return {
            c for c in re.findall(
                r'href="(?:https://www\.doctoralia\.co)?/' + specialty + r'/([a-z0-9-]+)"', body
            )
            if c != "online"
        }

    root = await fetch(client, f"{BASE}/{specialty}")
    cities = city_links(root or "")
    log.info(f"  nivel 1: {len(cities)} ciudades")

    bodies = await asyncio.gather(*(fetch(client, f"{BASE}/{specialty}/{c}") for c in sorted(cities)))
    for b in bodies:
        if b:
            cities |= city_links(b)
    log.info(f"  nivel 2: {len(cities)} ciudades")
    return sorted(cities)


async def run_listing_cities(specialty: str, concurrency: int) -> None:
    """Barrido por ciudad — captura los perfiles que el ranking nacional deja fuera."""
    state = load_state()
    key = f"listing_cities_{specialty}"
    done = set(state.get(key, {}).get("done", []))
    seen_urls = {r["perfil_url"] for r in read_jsonl(LISTING_JSONL)}

    async with httpx.AsyncClient(http2=False) as client:
        cities = await discover_cities(client, specialty)
        pending = [c for c in cities if c not in done]
        if not pending:
            log.info(f"Barrido por ciudad: completo ({len(cities)} ciudades)")
            return
        log.info(f"=== Barrido por ciudad: {len(pending)} pendientes de {len(cities)} ===")

        sem = asyncio.Semaphore(concurrency)
        lock = asyncio.Lock()

        async def do_city(city: str) -> None:
            async with sem:
                first = await fetch(client, f"{BASE}/{specialty}/{city}")
            if first is None:
                async with lock:
                    done.add(city)
                return
            pages = [int(p) for p in _MAXPAGE_RE.findall(first)]
            last = max(pages) if pages else 1

            async def one(page: int) -> list[dict]:
                if page == 1:
                    return parse_listing(first)
                async with sem:
                    b = await fetch(client, f"{BASE}/{specialty}/{city}?page={page}")
                    await asyncio.sleep(random.uniform(0.2, 0.6))
                return parse_listing(b) if b else []

            batches = await asyncio.gather(*(one(p) for p in range(1, last + 1)))
            rows = [r for b in batches for r in b]
            async with lock:
                fresh = [r for r in rows if r["perfil_url"] not in seen_urls]
                for r in fresh:
                    seen_urls.add(r["perfil_url"])
                if fresh:
                    append_jsonl(LISTING_JSONL, fresh)
                done.add(city)
                state.setdefault(key, {})["done"] = sorted(done)
                save_state(state)
            log.info(f"  {city}: {last} págs, {len(rows)} cards, +{len(fresh)} nuevos "
                     f"(total {len(seen_urls)})")

        await asyncio.gather(*(do_city(c) for c in pending))

    state.setdefault(key, {})["done"] = sorted(done)
    save_state(state)
    log.info(f"=== Barrido por ciudad terminado: {len(seen_urls)} médicos únicos ===")


async def run_listing(specialty: str, max_pages: int, concurrency: int) -> None:
    state = load_state()
    key = f"listing_{specialty}"
    done_pages = set(state.get(key, {}).get("pages_done", []))
    seen_urls = {r["perfil_url"] for r in read_jsonl(LISTING_JSONL)}

    pages = [p for p in range(1, max_pages + 1) if p not in done_pages]
    if not pages:
        log.info(f"Listado {specialty}: todas las páginas ya scrapeadas ({len(seen_urls)} médicos)")
        return

    log.info(f"=== Fase listado: {specialty} — {len(pages)} páginas pendientes ===")
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    stats = {"new": 0, "empty": 0}

    async def do_page(client: httpx.AsyncClient, page: int) -> None:
        async with sem:
            url = f"{BASE}/{specialty}" + (f"?page={page}" if page > 1 else "")
            body = await fetch(client, url)
            await asyncio.sleep(random.uniform(0.3, 0.9))
        if body is None:
            return
        rows = parse_listing(body)
        if not rows:
            stats["empty"] += 1
            async with lock:
                done_pages.add(page)
            return
        async with lock:
            fresh = [r for r in rows if r["perfil_url"] not in seen_urls]
            for r in fresh:
                seen_urls.add(r["perfil_url"])
            if fresh:
                append_jsonl(LISTING_JSONL, fresh)
                stats["new"] += len(fresh)
            done_pages.add(page)
            state.setdefault(key, {})["pages_done"] = sorted(done_pages)
            save_state(state)
        log.info(f"  p{page}: {len(rows)} cards, +{len(fresh)} nuevos  (total {len(seen_urls)})")

    async with httpx.AsyncClient(http2=False) as client:
        await asyncio.gather(*(do_page(client, p) for p in pages))

    state.setdefault(key, {})["pages_done"] = sorted(done_pages)
    save_state(state)
    log.info(f"=== Listado terminado: {len(seen_urls)} médicos únicos (+{stats['new']}) ===")


# ── Fase 2: perfiles ──────────────────────────────────────────────────────────

# El número completo viaja en el selector del modal: address-{addrId}-{phone}-{n}-phone
_PHONE_RE = re.compile(r"address-\d+-(\d{7,15})-\d+-phone")
_CARD_MARKER = 'class="profile-address-card card card-border'
_STREET_RE = re.compile(r'data-test-id="address-info-street"[^>]*>\s*([^<]{2,200})')
_CITY_RE = re.compile(r'class="city"\s*content="([^"]*)"')
_REGION_RE = re.compile(r'class="province region"\s*content="([^"]*)"')
_NAME_RE = re.compile(r'profile-address-card__name[^>]*>\s*([^<]{2,120})')
_SPEC_RE = re.compile(r'data-test-id="doctor-specializations"[^>]*>(.{0,600}?)</div>', re.S)
# Las EPS solo son confiables dentro del modal "Aseguradoras aceptadas"; fuera de él
# los links /especialidad/ciudad/slug también apuntan a servicios y barrios.
_EPS_MODAL_RE = re.compile(
    r'data-test-id="address-insurances-modal">(.{0,20000}?)</div>\s*</div>\s*</div>\s*</div>', re.S
)
_EPS_LINK_RE = re.compile(r'href="/[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+"[^>]*title="([^"]{4,120})"')
_SERVICES_RE = re.compile(r':services="([^"]{20,})"')


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s)).strip(" ·,·")


def _extract_precio(body: str) -> tuple[int | None, str]:
    """Precio de la consulta destacada (no el de control, que es más barato)."""
    m = _SERVICES_RE.search(body)
    if not m:
        return None, ""
    try:
        services = json.loads(html_mod.unescape(m.group(1)))
    except (json.JSONDecodeError, TypeError):
        return None, ""
    if not isinstance(services, list) or not services:
        return None, ""
    default = next((s for s in services if s.get("isDefaultServiceForHighlight")), services[0])
    return default.get("priceMin"), (default.get("name") or "").strip()


def normalize_phone_co(raw: str) -> str:
    """10 dígitos CO → E.164 legible. Los que no parsean se devuelven crudos."""
    try:
        n = phonenumbers.parse(raw, "CO")
        if phonenumbers.is_valid_number(n):
            return phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
    except phonenumbers.NumberParseException:
        pass
    return raw


def parse_profile(body: str, base: dict) -> dict:
    phones = list(dict.fromkeys(_PHONE_RE.findall(body)))

    consultorios, direcciones, ciudades, deptos = [], [], [], []
    for card in body.split(_CARD_MARKER)[1:]:
        card = card[:8000]  # un card de dirección nunca es más largo que esto
        nm = _NAME_RE.search(card)
        st = _STREET_RE.search(card)
        ct = _CITY_RE.search(card)
        rg = _REGION_RE.search(card)
        if nm:
            consultorios.append(html_mod.unescape(nm.group(1)).strip())
        if st:
            direcciones.append(html_mod.unescape(st.group(1)).strip().rstrip(","))
        if ct:
            ciudades.append(html_mod.unescape(ct.group(1)).strip())
        if rg:
            deptos.append(html_mod.unescape(rg.group(1)).strip())

    # Fallback: si el regex de cards no enganchó, tomar los campos sueltos del documento.
    if not direcciones:
        direcciones = [html_mod.unescape(s).strip().rstrip(",") for s in _STREET_RE.findall(body)]
    if not ciudades:
        ciudades = [html_mod.unescape(c).strip() for c in _CITY_RE.findall(body)]
    if not deptos:
        deptos = [html_mod.unescape(d).strip() for d in _REGION_RE.findall(body)]

    spec_m = _SPEC_RE.search(body)
    especialidades = _clean_text(spec_m.group(1)) if spec_m else base.get("especialidad", "")

    eps: list[str] = []
    for modal in _EPS_MODAL_RE.findall(body):
        eps.extend(html_mod.unescape(e).strip() for e in _EPS_LINK_RE.findall(modal))
    eps = list(dict.fromkeys(eps))
    precio, precio_servicio = _extract_precio(body)

    def joinu(xs: list[str]) -> str:
        return " | ".join(dict.fromkeys(re.sub(r"\s{2,}", " ", x).strip() for x in xs if x))

    return {
        **base,
        "telefonos":     [normalize_phone_co(p) for p in phones],
        "teleconsulta":  any("línea" in c.lower() for c in consultorios),
        "consultorio":   joinu([c for c in consultorios if "línea" not in c.lower()]),
        "direccion":     joinu(direcciones),
        "ciudad_perfil": joinu(ciudades),
        "departamento":  joinu(deptos),
        "especialidades": especialidades,
        "eps":           joinu(eps[:15]),
        "precio_min":    precio,
        "precio_servicio": precio_servicio,
    }


async def run_profiles(limit: int | None, concurrency: int) -> None:
    listing = read_jsonl(LISTING_JSONL)
    if not listing:
        log.error("No hay listado. Corre primero --phase listing")
        return

    done = {r["perfil_url"] for r in read_jsonl(PROFILES_JSONL)}
    pending = [r for r in listing if r["perfil_url"] not in done]
    if limit:
        pending = pending[:limit]

    if not pending:
        log.info(f"Perfiles: nada pendiente ({len(done)} ya procesados)")
        return

    log.info(f"=== Fase perfiles: {len(pending)} pendientes de {len(listing)} ===")
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    buf: list[dict] = []
    counters = {"ok": 0, "fail": 0, "con_tel": 0}

    async def do_profile(client: httpx.AsyncClient, row: dict) -> None:
        async with sem:
            body = await fetch(client, row["perfil_url"])
            await asyncio.sleep(random.uniform(0.2, 0.6))
        if body is None:
            counters["fail"] += 1
            return
        rec = parse_profile(body, row)
        if rec["telefonos"]:
            counters["con_tel"] += 1
        counters["ok"] += 1
        async with lock:
            buf.append(rec)
            if len(buf) >= 25:
                append_jsonl(PROFILES_JSONL, buf)
                buf.clear()
                total = counters["ok"] + len(done)
                log.info(f"  {total} perfiles  ({counters['con_tel']} con teléfono, "
                         f"{counters['fail']} fallos)")

    async with httpx.AsyncClient(http2=False) as client:
        await asyncio.gather(*(do_profile(client, r) for r in pending))

    if buf:
        append_jsonl(PROFILES_JSONL, buf)
    log.info(f"=== Perfiles: {counters['ok']} ok, {counters['con_tel']} con teléfono, "
             f"{counters['fail']} fallos ===")


# ── Fase 3: export ────────────────────────────────────────────────────────────

_TITULO_RE = re.compile(r"^\s*(Dra?\.|Dr\.|Dra|Dr)\s+", re.IGNORECASE)


def split_titulo(nombre_raw: str) -> tuple[str, str]:
    m = _TITULO_RE.match(nombre_raw)
    if not m:
        return "", nombre_raw.strip()
    return m.group(1).rstrip("."), _TITULO_RE.sub("", nombre_raw).strip()


def build_dataframe() -> pd.DataFrame:
    profiles = read_jsonl(PROFILES_JSONL)
    listing = {r["perfil_url"]: r for r in read_jsonl(LISTING_JSONL)}
    hoy = date.today().isoformat()

    # El listado manda sobre quién entra (ya filtra clínicas); el perfil solo
    # aporta contacto. Un perfil sin fila en el listado vigente se ignora.
    by_url: dict[str, dict] = {url: dict(l) for url, l in listing.items()}
    for p in profiles:
        if p["perfil_url"] in by_url:
            by_url[p["perfil_url"]].update(p)

    rows = []
    for url, r in by_url.items():
        titulo, nombre = split_titulo(r.get("nombre_raw", ""))
        tels = r.get("telefonos") or []
        rows.append({
            "Nombre":            nombre,
            "Titulo":            titulo,
            "Especialidad":      r.get("especialidades") or r.get("especialidad", ""),
            "Email":             "",
            "Telefono":          " | ".join(tels),
            "Telefono_Principal": tels[0] if tels else "",
            "Consultorio":       r.get("consultorio", ""),
            "Direccion":         r.get("direccion", ""),
            "Ciudad":            r.get("ciudad_perfil") or r.get("ciudad", ""),
            "Departamento":      r.get("departamento", ""),
            "EPS_Aceptadas":     r.get("eps", ""),
            "Teleconsulta":      "Sí" if r.get("teleconsulta") else "",
            "Precio_Consulta":   r.get("precio_min"),
            "Precio_Servicio":   r.get("precio_servicio", ""),
            "Rating":            r.get("rating", ""),
            "Opiniones":         r.get("opiniones", ""),
            "Perfil_URL":        url,
            "Pais":              PAIS,
            "Fuente":            FUENTE,
            "Fecha_Captura":     hoy,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # `by_url` ya garantiza unicidad por perfil. Aquí solo colapsamos el caso de
    # perfiles duplicados del mismo médico: mismo nombre Y mismo teléfono real.
    # Sin teléfono no se colapsa nada: dos homónimos distintos son dos médicos.
    df["_k"] = df["Nombre"].str.upper().str.strip() + "|" + df["Telefono_Principal"]
    dup = df["Telefono_Principal"].str.strip().ne("") & df["_k"].duplicated()
    df = df[~dup].drop(columns="_k")
    return df.sort_values(["Ciudad", "Nombre"]).reset_index(drop=True)


def save_excel(df: pd.DataFrame) -> None:
    tmp = OUTPUT_DIR / "_medicos_co_tmp.xlsx"
    con_contacto = df[
        (df["Telefono"].fillna("").str.strip() != "")
        | (df["Email"].fillna("").str.strip() != "")
    ]
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Todos", index=False)
        con_contacto.to_excel(w, sheet_name="Con_Contacto", index=False)
        (df.groupby("Ciudad").size().reset_index(name="Cantidad")
           .sort_values("Cantidad", ascending=False)
           .to_excel(w, sheet_name="Por_Ciudad", index=False))
        (df.groupby("Departamento").size().reset_index(name="Cantidad")
           .sort_values("Cantidad", ascending=False)
           .to_excel(w, sheet_name="Por_Departamento", index=False))
        (df.groupby("Fuente").size().reset_index(name="Cantidad")
           .to_excel(w, sheet_name="Por_Fuente", index=False))
    shutil.move(str(tmp), str(OUTPUT_XLSX))
    log.info(f"Excel guardado: {OUTPUT_XLSX.name}  "
             f"({len(df)} médicos, {len(con_contacto)} con contacto)")


def run_export() -> None:
    df = build_dataframe()
    if df.empty:
        log.error("Sin datos para exportar.")
        return
    save_excel(df)


# ── CLI ───────────────────────────────────────────────────────────────────────

async def amain(args: argparse.Namespace) -> None:
    if args.phase in ("listing", "all"):
        await run_listing(args.specialty, args.max_pages, args.concurrency)
    if args.phase in ("cities", "all"):
        await run_listing_cities(args.specialty, args.concurrency)
    if args.phase in ("profiles", "all"):
        await run_profiles(args.limit, args.concurrency)


def main() -> None:
    p = argparse.ArgumentParser(description="Scraper Doctoralia Colombia")
    p.add_argument("--phase", choices=["listing", "cities", "profiles", "export", "all"],
                   default="all")
    p.add_argument("--specialty", default=DEFAULT_SPECIALTY,
                   help="slug de especialidad en doctoralia.co (default: ginecologo)")
    p.add_argument("--max-pages", type=int, default=160)
    p.add_argument("--limit", type=int, default=None, help="máx. perfiles por corrida")
    p.add_argument("--concurrency", type=int, default=6)
    args = p.parse_args()

    if args.phase == "export":
        run_export()
        return

    asyncio.run(amain(args))
    run_export()


if __name__ == "__main__":
    main()
