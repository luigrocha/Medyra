#!/usr/bin/env python3
"""
Runner rápido de enriquecimiento — procesa múltiples médicos en paralelo.
Diseñado para correr en ventanas cortas (~40s) y ser reanudado con --resume.

Diferencias vs enrich_cr_pa.py:
  - Concurrencia configurable (default 3 médicos a la vez).
  - DDG interval reducido a 2s (agresivo pero práctico para lotes).
  - Sin LLM fallback.
  - Misma salida JSONL → compatible con build_output_excel de enrich_cr_pa.py.

Uso:
    python scripts/enrich_fast.py --country CR --batch 9 --resume
    python scripts/enrich_fast.py --country PA --batch 9 --resume
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medintel.infrastructure.scrapers.ddg import DuckDuckGoScraper
from medintel.inference.confidence import score
from medintel.normalization import emails as email_norm
from medintel.normalization import phones as phone_norm
from medintel.normalization.names import NameParts, parse_latam_name, strip_accents
from medintel.infrastructure.scrapers.generic import extract as generic_extract
from medintel.infrastructure.scrapers.base import ScrapingBlocked

RESULTS_JSONL = ROOT / "data/output/enrichment_cr_pa_results.jsonl"
OUTPUT_DIR    = ROOT / "data/output"

INPUT_FILES = {
    "CR": ROOT / "data/output/medicos-allegra-0226__Costa_Rica_clean.xlsx",
    "PA": ROOT / "data/output/medicos-allegra-0226__Panama_clean.xlsx",
}
NAME_FORMAT = {"CR": "family_first", "PA": "given_first"}

PRIORITY_SPECS = {
    "otorrinolaringologia","dermatologia","cardiologia","pediatria",
    "ginecologia","neurologia","oftalmologia","traumatologia",
    "psiquiatria","alergologia","oncologia","urologia","endocrinologia",
    "gastroenterologia","reumatologia","neumologia","infectologia",
    "nefrologia","hematologia","medicina_interna",
}

COUNTRY_NAME = {
    "CR": "Costa Rica", "PA": "Panama", "EC": "Ecuador",
    "MX": "Mexico", "CO": "Colombia",
}

BAD_DOMAINS = ("datocapital.co.cr", "abctelefonos.com", "pinterest.", "youtube.com/watch")
SKIP_EMAIL_DOMAINS = ("madrid.es", "asmmagazine", "sentry.io", "wixpress", ".png", ".jpg")


def load_done() -> set[str]:
    done: set[str] = set()
    if not RESULTS_JSONL.exists():
        return done
    with RESULTS_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    done.add(json.loads(line).get("physician_name", ""))
                except json.JSONDecodeError:
                    pass
    return done


def load_candidates(country: str, specialty_only: bool) -> list[dict]:
    df = pd.read_excel(INPUT_FILES[country])
    no_phone = df[df["Calidad_Tel"].isin(["FALTA", "invalid_phone"])].copy()
    if specialty_only:
        no_phone = no_phone[no_phone["Especialidad"].str.lower().isin(PRIORITY_SPECS)]
    no_phone["_priority"] = no_phone["Especialidad"].str.lower().apply(
        lambda s: 0 if s in PRIORITY_SPECS else 1
    )
    no_phone = no_phone.sort_values(["_priority", "Apellido_1"]).reset_index(drop=True)

    candidates = []
    for _, row in no_phone.iterrows():
        a1 = str(row.get("Apellido_1","") or "").strip()
        a2 = str(row.get("Apellido_2","") or "").strip()
        nm = str(row.get("Nombres","")   or "").strip()
        if not a1 or not nm:
            continue
        full = f"{a1} {a2} {nm}" if a2 else f"{a1} {nm}"
        candidates.append({
            "full_raw": full,
            "specialty": str(row.get("Especialidad","") or "").lower().strip(),
            "country": country,
        })
    return candidates


def save_result(rec: dict) -> None:
    with RESULTS_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ── Búsqueda + extracción por médico ──────────────────────────────────────

async def enrich_one(
    name_parts: NameParts,
    specialty: str,
    country: str,
    search: DuckDuckGoScraper,
    semaphore: asyncio.Semaphore,
    client: httpx.AsyncClient,
) -> dict:
    async with semaphore:
        result = {
            "physician_name": name_parts.full_name,
            "country": country,
            "pages_fetched": 0,
            "pages_with_name_present": 0,
            "discovered_urls": [],
            "used_llm": False,
            "notes": [],
            "claims": [],
        }

        # ── DDG search ──────────────────────────────────────────────────
        country_str = COUNTRY_NAME.get(country, country)
        fam1 = name_parts.family_name_1
        first = name_parts.given_name.split()[0] if name_parts.given_name else ""
        spec_str = specialty.replace("_", " ") if specialty else ""
        query = f'"{fam1}" "{first}" {spec_str} {country_str}'.strip()
        search_url = search.query_url(query)

        try:
            fr = await search.fetch(search_url, client)
        except ScrapingBlocked:
            result["notes"].append("DDG bloqueado")
            return result
        except Exception as e:
            result["notes"].append(f"DDG error: {str(e)[:80]}")
            return result

        if fr.status != 200 or not fr.html:
            result["notes"].append("DDG sin respuesta")
            return result

        hits = search.parse_results(fr.html)
        if not hits:
            result["notes"].append("DDG 0 resultados")
            return result

        # ── Rank y filtro de hits ────────────────────────────────────────
        family_norm = strip_accents(fam1).lower()
        good = ("medico","doctor","clinica","hospital","salud","consultorio","huli","cima","msp","gob")

        def rank(h):
            u = h.url.lower()
            t = (h.title + h.snippet).lower()
            if any(b in u for b in BAD_DOMAINS): return -1.0
            s = 0.0
            if family_norm and family_norm in strip_accents(t).lower(): s += 2.0
            if any(g in u for g in good): s += 1.0
            if "linkedin.com/in/" in u: s += 0.5
            return s

        ranked = sorted(hits[:10], key=rank, reverse=True)[:5]
        name_tokens = [strip_accents(t).lower() for t in name_parts.full_name.split() if len(t) >= 4]

        # ── Fetch páginas ────────────────────────────────────────────────
        for hit in ranked:
            if any(b in hit.url.lower() for b in BAD_DOMAINS):
                continue
            try:
                page = await search.fetch(hit.url, client)
            except Exception:
                continue
            if page.status != 200 or not page.html:
                continue

            result["pages_fetched"] += 1
            extraction = generic_extract(page.html, url=hit.url, country=country, name_tokens=name_tokens)
            if not extraction.name_present:
                continue

            result["pages_with_name_present"] += 1
            result["discovered_urls"].append(hit.url)

            # ── Tier según dominio ───────────────────────────────────────
            u = hit.url.lower()
            if any(d in u for d in ("msp.gob","ministeriodesalud","colegiomedico","consejotecnicodesalud","medicos.cr",".gov",".gob")):
                tier_sig, tier_int = "source_tier_1", 1
            elif any(d in u for d in ("facebook.com","linkedin.com","twitter.com","instagram.com")):
                tier_sig, tier_int = "source_tier_3", 3
            else:
                tier_sig, tier_int = "source_tier_2", 2

            for val, prox in extraction.emails:
                if any(d in val.lower() for d in SKIP_EMAIL_DOMAINS):
                    continue
                norm_e = email_norm.normalize(val)
                if not norm_e:
                    continue
                sigs = {tier_sig, "single_source"}
                if prox >= 0.85:
                    sigs.add("mx_ok" if not norm_e.is_freemail else "verified_within_180d")
                r = score(sigs, prior=0.20 + 0.30 * prox)
                result["claims"].append({
                    "attribute": "email",
                    "value": val,
                    "value_normalized": norm_e.address,
                    "subkind": "personal" if norm_e.is_freemail else "clinic",
                    "confidence": round(r.probability, 4),
                    "source_url": hit.url,
                    "source_name": f"generic_tier{tier_int}",
                    "explanation": f"Email en página pública (prox={prox:.2f})",
                })

            for val, prox in extraction.phones:
                norm_p = phone_norm.normalize(val, country)
                if not norm_p:
                    continue
                sigs = {tier_sig, "phonenumbers_valid", "single_source"}
                r = score(sigs, prior=0.20 + 0.30 * prox)
                result["claims"].append({
                    "attribute": "phone",
                    "value": val,
                    "value_normalized": norm_p.e164,
                    "subkind": norm_p.line_type or "unknown",
                    "confidence": round(r.probability, 4),
                    "source_url": hit.url,
                    "source_name": f"generic_tier{tier_int}",
                    "explanation": f"Teléfono en página pública (prox={prox:.2f})",
                })

        return result


# ── Batch runner ───────────────────────────────────────────────────────────

async def run_batch(candidates: list[dict], concurrency: int) -> int:
    """Procesa un lote. Devuelve cuántos tuvieron contactos."""
    search = DuckDuckGoScraper()
    search.min_interval_seconds = 2.0  # más agresivo para lotes

    sem = asyncio.Semaphore(concurrency)
    found = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
        tasks = []
        for c in candidates:
            np = parse_latam_name(c["full_raw"], name_format=NAME_FORMAT[c["country"]])
            tasks.append(enrich_one(np, c["specialty"], c["country"], search, sem, client))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            continue
        save_result(res)
        if res["claims"]:
            phones = list(dict.fromkeys(
                cl["value_normalized"] for cl in res["claims"] if cl["attribute"] == "phone"
            ))
            emails = list(dict.fromkeys(
                cl["value_normalized"] for cl in res["claims"] if cl["attribute"] == "email"
            ))
            status = f"✅  tel={phones[:1]}  email={emails[:1]}"
            found += 1
        else:
            status = "—"
        print(f"  {res['physician_name']:<42} {res.get('country','')}  {status}")

    return found


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--country", choices=["CR","PA","ALL"], default="ALL")
    p.add_argument("--batch", type=int, default=9, help="Médicos a procesar en esta corrida")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--specialty-only", action="store_true")
    p.add_argument("--resume", action="store_true")
    args = p.parse_args()

    done = load_done() if args.resume else set()
    countries = ["CR","PA"] if args.country == "ALL" else [args.country]

    all_candidates = []
    for c in countries:
        cands = load_candidates(c, specialty_only=args.specialty_only)
        for cd in cands:
            if cd["full_raw"].upper() not in done:
                all_candidates.append(cd)

    batch = all_candidates[:args.batch]
    remaining_after = len(all_candidates) - len(batch)

    print(f"Procesando {len(batch)} médicos  (pendientes después: {remaining_after})")
    print(f"Concurrencia: {args.concurrency}  |  Ya hechos: {len(done)}")
    print()

    found = asyncio.run(run_batch(batch, args.concurrency))
    print(f"\nEsta corrida: {len(batch)} procesados, {found} con contactos nuevos")
    print(f"Pendientes: {remaining_after}")


if __name__ == "__main__":
    main()
