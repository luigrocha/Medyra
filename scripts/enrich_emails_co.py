#!/usr/bin/env python3
"""
Enriquecimiento de emails — ginecólogos Colombia.

Doctoralia da nombre + teléfono + consultorio, pero NUNCA email. El email vive
en el sitio propio del médico o de su clínica. Este script cierra ese hueco:

    DDG search → top-N páginas no-agregadoras → extractor genérico →
    scoring por proximidad al nombre → JSONL reanudable.

La evidencia de que funciona está en el CSV semilla del cliente: los emails que
él ya tenía (hola@ginecoalegalofre.com, contacto@ginecologobogota.com,
pacientes@cecolfes.com) son exactamente lo que devuelve esta ruta.

Rate limit: DDG tolera ~1 query/4s. 2.9k médicos ≈ 3.5 h en una sola corrida.
Por eso el JSONL es reanudable y el orden es por prioridad (más opiniones =
más presencia web = mayor probabilidad de email).

Uso:
    python3 scripts/enrich_emails_co.py --limit 100      # prueba
    python3 scripts/enrich_emails_co.py                  # todo (background)
    python3 scripts/enrich_emails_co.py --min-opiniones 5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medyra.infrastructure.scrapers.ddg import DuckDuckGoScraper  # noqa: E402
from medyra.infrastructure.scrapers.generic import extract as generic_extract  # noqa: E402

OUTPUT_DIR = ROOT / "data" / "output"
LISTING_JSONL = OUTPUT_DIR / "doctoralia_co_listing.jsonl"
PROFILES_JSONL = OUTPUT_DIR / "doctoralia_co_profiles.jsonl"
RESULTS_JSONL = OUTPUT_DIR / "enrichment_co_emails.jsonl"

# Dominios que jamás publican el email del médico: gastar un fetch ahí es ruido.
SKIP_DOMAINS = {
    "doctoralia.co", "www.doctoralia.co", "facebook.com", "www.facebook.com",
    "m.facebook.com", "instagram.com", "www.instagram.com", "youtube.com",
    "www.youtube.com", "m.youtube.com", "twitter.com", "x.com", "tiktok.com",
    "www.tiktok.com", "linkedin.com", "co.linkedin.com", "www.linkedin.com",
    "es.wikipedia.org", "wikipedia.org", "google.com", "www.google.com",
    "maps.google.com", "waze.com", "booking.com", "amazon.com",
}

# Emails de plantilla / infra que aparecen en cualquier sitio y no son contacto.
JUNK_EMAIL_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|example\.|sentry|wixpress|@doctoralia|"
    r"@docplanner|@godaddy|@wordpress|@squarespace|@sitelock|webmaster@|"
    r"postmaster@|abuse@|privacy@|@2x|@3x)",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9",
}

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


# ── Datos ─────────────────────────────────────────────────────────────────────

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
                    continue
    return rows


_TITULO_RE = re.compile(r"^\s*(Dra?\.|Dr\.|Dra|Dr)\s+", re.IGNORECASE)


def load_candidates(min_opiniones: int) -> list[dict]:
    """Prioriza por número de opiniones: correlaciona con presencia web propia."""
    by_url = {r["perfil_url"]: r for r in read_jsonl(LISTING_JSONL)}
    for p in read_jsonl(PROFILES_JSONL):
        by_url[p["perfil_url"]] = {**by_url.get(p["perfil_url"], {}), **p}

    out = []
    for url, r in by_url.items():
        nombre = _TITULO_RE.sub("", r.get("nombre_raw", "")).strip()
        if not nombre:
            continue
        try:
            op = int(r.get("opiniones") or 0)
        except (TypeError, ValueError):
            op = 0
        if op < min_opiniones:
            continue
        out.append({
            "nombre": nombre,
            "perfil_url": url,
            "ciudad": r.get("ciudad_perfil") or r.get("ciudad", ""),
            "opiniones": op,
            "consultorio": r.get("consultorio", ""),
        })
    out.sort(key=lambda x: -x["opiniones"])
    return out


# ── Enriquecimiento ───────────────────────────────────────────────────────────

def name_tokens(nombre: str) -> list[str]:
    return [t for t in re.split(r"\s+", nombre) if len(t) >= 4]


def useful_hits(hits: list, top_n: int) -> list[str]:
    urls: list[str] = []
    seen_hosts: set[str] = set()
    for h in hits:
        host = urlparse(h.url).netloc.lower()
        if host in SKIP_DOMAINS or not host:
            continue
        if host in seen_hosts:  # una página por dominio basta
            continue
        seen_hosts.add(host)
        urls.append(h.url)
        if len(urls) >= top_n:
            break
    return urls


async def enrich_one(
    doc: dict, ddg: DuckDuckGoScraper, client: httpx.AsyncClient, top_n: int
) -> dict:
    toks = name_tokens(doc["nombre"])
    ciudad = doc["ciudad"].split(" | ")[0] if doc["ciudad"] else ""
    query = f'"{doc["nombre"]}" ginecologo {ciudad} contacto correo'

    rec: dict = {
        "nombre": doc["nombre"], "perfil_url": doc["perfil_url"],
        "ciudad": ciudad, "query": query,
        "urls_visitadas": [], "emails": [], "sitios": [], "error": None,
    }

    try:
        fr = await ddg.fetch(ddg.query_url(query), client=client)
        hits = ddg.parse_results(fr.html or "")
    except Exception as e:
        rec["error"] = f"ddg: {type(e).__name__}: {str(e)[:80]}"
        return rec

    urls = useful_hits(hits, top_n)
    rec["sitios"] = urls

    async def one_page(url: str) -> list[tuple[str, float, str]]:
        try:
            r = await client.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                return []
        except Exception:
            return []
        try:
            ext = generic_extract(r.text, url, country="CO", name_tokens=toks)
        except Exception:
            return []
        return [
            (e, score, url) for e, score in ext.emails
            if not JUNK_EMAIL_PATTERNS.search(e) and score > 0
        ]

    batches = await asyncio.gather(*(one_page(u) for u in urls))
    rec["urls_visitadas"] = urls

    best: dict[str, tuple[float, str]] = {}
    for b in batches:
        for email, score, src in b:
            e = email.lower()
            if e not in best or score > best[e][0]:
                best[e] = (score, src)
    rec["emails"] = [
        {"email": e, "score": round(s, 2), "fuente": src}
        for e, (s, src) in sorted(best.items(), key=lambda kv: -kv[1][0])
    ][:5]
    return rec


async def run(limit: int | None, min_opiniones: int, top_n: int) -> None:
    candidates = load_candidates(min_opiniones)
    done = {r["perfil_url"] for r in read_jsonl(RESULTS_JSONL)}
    pending = [c for c in candidates if c["perfil_url"] not in done]
    if limit:
        pending = pending[:limit]

    if not pending:
        log.info(f"Nada pendiente ({len(done)} ya procesados)")
        return

    log.info(f"=== Enriquecimiento emails CO: {len(pending)} pendientes "
             f"(de {len(candidates)} candidatos, {len(done)} hechos) ===")

    ddg = DuckDuckGoScraper()
    hallados = 0
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        with RESULTS_JSONL.open("a", encoding="utf-8") as out:
            for i, doc in enumerate(pending, 1):
                rec = await enrich_one(doc, ddg, client, top_n)
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                out.flush()
                if rec["emails"]:
                    hallados += 1
                    log.info(f"  [{i}/{len(pending)}] ✅ {doc['nombre'][:38]:<38} "
                             f"{rec['emails'][0]['email']}")
                elif i % 10 == 0:
                    log.info(f"  [{i}/{len(pending)}] … {hallados} emails hasta ahora")

    log.info(f"=== Terminado: {hallados}/{len(pending)} con email "
             f"({hallados / len(pending):.0%}) ===")


def main() -> None:
    p = argparse.ArgumentParser(description="Enriquecimiento de emails — ginecólogos CO")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--min-opiniones", type=int, default=0,
                   help="solo médicos con al menos N opiniones (proxy de presencia web)")
    p.add_argument("--top-n", type=int, default=4, help="páginas a visitar por médico")
    args = p.parse_args()
    asyncio.run(run(args.limit, args.min_opiniones, args.top_n))


if __name__ == "__main__":
    main()
