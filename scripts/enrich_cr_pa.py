#!/usr/bin/env python3
"""
Enriquecimiento por lotes: Costa Rica + Panamá
===============================================
Busca teléfonos y emails adicionales para médicos sin contacto directo
usando DuckDuckGo search + extractor genérico de páginas.

Estrategia:
  1. Carga los Excels limpios de CR y PA.
  2. Prioriza especialistas sobre médicos generales (mayor presencia web).
  3. Para cada médico: query DDG → top-5 páginas → extrae contactos.
  4. Guarda resultados incrementalmente en JSONL (reanudable).
  5. Genera Excel final con columnas enriquecidas.

Uso:
    python scripts/enrich_cr_pa.py                    # corre todo
    python scripts/enrich_cr_pa.py --country CR       # solo Costa Rica
    python scripts/enrich_cr_pa.py --country PA       # solo Panamá
    python scripts/enrich_cr_pa.py --limit 50         # prueba con 50
    python scripts/enrich_cr_pa.py --specialty-only   # solo especialistas
    python scripts/enrich_cr_pa.py --resume           # salta ya procesados

Tiempo estimado (DDG 4s/query + 3 páginas por médico ~14s total):
    50 médicos  → ~12 min
    100 médicos → ~24 min
    500 médicos → ~2 hs
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

# Agrega src/ al path para importar medyra
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from medyra.application.agents.enrichment import EnrichmentAgent
from medyra.infrastructure.scrapers.ddg import DuckDuckGoScraper
from medyra.normalization.names import parse_latam_name

# ─── Configuración ────────────────────────────────────────────────────────────

INPUT_FILES = {
    "CR": ROOT / "data/output/medicos-allegra-0226__Costa_Rica_clean.xlsx",
    "PA": ROOT / "data/output/medicos-allegra-0226__Panama_clean.xlsx",
}

NAME_FORMAT = {
    "CR": "family_first",
    "PA": "given_first",
}

OUTPUT_DIR = ROOT / "data/output"
RESULTS_JSONL = OUTPUT_DIR / "enrichment_cr_pa_results.jsonl"
OUTPUT_EXCEL  = OUTPUT_DIR / "enrichment_cr_pa_enriched.xlsx"

# Especialidades que priorizamos (mejor presencia web)
PRIORITY_SPECS = {
    "otorrinolaringologia", "dermatologia", "cardiologia", "pediatria",
    "ginecologia", "neurologia", "oftalmologia", "traumatologia",
    "psiquiatria", "alergologia", "oncologia", "urologia", "endocrinologia",
    "gastroenterologia", "reumatologia", "neumologia", "infectologia",
    "nefrologia", "hematologia", "medicina_interna",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_already_done(path: Path) -> set[str]:
    """Lee el JSONL de resultados y devuelve los nombres ya procesados."""
    done: set[str] = set()
    if not path.exists():
        return done
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done.add(rec.get("physician_name", ""))
            except json.JSONDecodeError:
                pass
    return done


def load_candidates(country: str, specialty_only: bool = False) -> pd.DataFrame:
    """
    Carga médicos que no tienen teléfono (o no tienen ningún contacto real).
    Ordena: primero especialistas, luego por nombre.
    """
    path = INPUT_FILES[country]
    if not path.exists():
        print(f"  ⚠️  No existe {path}. Saltando {country}.")
        return pd.DataFrame()

    df = pd.read_excel(path)
    df["_country"] = country

    # Sin teléfono
    no_phone = df[df["Calidad_Tel"].isin(["FALTA", "invalid_phone"])].copy()

    if specialty_only:
        spec_col = no_phone["Especialidad"].str.lower()
        no_phone = no_phone[spec_col.isin(PRIORITY_SPECS)]

    # Columna de prioridad
    no_phone["_priority"] = no_phone["Especialidad"].str.lower().apply(
        lambda s: 0 if s in PRIORITY_SPECS else 1
    )
    no_phone = no_phone.sort_values(["_priority", "Apellido_1"]).reset_index(drop=True)
    return no_phone


def result_to_dict(result) -> dict:
    """Serializa EnrichmentResult a dict JSON-friendly."""
    return {
        "physician_name": result.physician_name,
        "country": result.country,
        "pages_fetched": result.pages_fetched,
        "pages_with_name_present": result.pages_with_name_present,
        "discovered_urls": result.discovered_urls,
        "used_llm": result.used_llm,
        "notes": result.notes,
        "claims": [
            {
                "attribute": c.attribute,
                "value": c.value,
                "value_normalized": c.value_normalized,
                "subkind": c.subkind,
                "confidence": c.confidence,
                "source_url": c.source_url,
                "source_name": c.source_name,
                "explanation": c.explanation,
            }
            for c in result.claims
        ],
    }


def save_result(path: Path, rec: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ─── Ejecución ────────────────────────────────────────────────────────────────

async def run_enrichment(
    candidates: pd.DataFrame,
    country: str,
    done: set[str],
    limit: int | None,
    verbose: bool,
) -> list[dict]:
    """Corre el EnrichmentAgent sobre `candidates` y guarda resultados."""
    agent = EnrichmentAgent(country, use_llm_fallback=False)
    results: list[dict] = []
    processed = 0
    skipped = 0
    found_new = 0

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        for _, row in candidates.iterrows():
            # Reconstruir nombre canónico
            apellido1 = str(row.get("Apellido_1", "") or "").strip()
            apellido2 = str(row.get("Apellido_2", "") or "").strip()
            nombres   = str(row.get("Nombres",    "") or "").strip()

            if not apellido1 or not nombres:
                continue

            if apellido2:
                full_raw = f"{apellido1} {apellido2} {nombres}"
            else:
                full_raw = f"{apellido1} {nombres}"

            name_parts = parse_latam_name(full_raw, name_format=NAME_FORMAT[country])

            if name_parts.full_name in done:
                skipped += 1
                continue

            if limit and processed >= limit:
                break

            specialty = str(row.get("Especialidad", "") or "").lower().strip()
            processed += 1

            t0 = time.monotonic()
            try:
                result = await agent.enrich(name_parts, specialty or None, client=client)
            except Exception as e:
                result_dict = {
                    "physician_name": name_parts.full_name,
                    "country": country,
                    "pages_fetched": 0,
                    "pages_with_name_present": 0,
                    "discovered_urls": [],
                    "used_llm": False,
                    "notes": [f"Error: {type(e).__name__}: {str(e)[:120]}"],
                    "claims": [],
                }
                save_result(RESULTS_JSONL, result_dict)
                results.append(result_dict)
                if verbose:
                    print(f"  ❌ [{processed}] {name_parts.full_name} — Error: {e}")
                continue

            rec = result_to_dict(result)
            save_result(RESULTS_JSONL, rec)
            results.append(rec)
            done.add(name_parts.full_name)

            elapsed = time.monotonic() - t0
            n_claims = len(rec["claims"])
            if n_claims:
                found_new += 1

            if verbose:
                status = f"✅ {n_claims} contacto(s)" if n_claims else "— sin resultados"
                print(
                    f"  [{processed:4d}] {name_parts.full_name:<40} "
                    f"{specialty:<25} {status}  ({elapsed:.1f}s)"
                )
            elif processed % 10 == 0:
                print(f"  ... procesados {processed}, encontrados {found_new} con contactos")

    print(f"\n  {country}: procesados={processed}, saltados(ya hechos)={skipped}, con_contactos={found_new}")
    return results


def build_output_excel(countries: list[str]) -> None:
    """Lee el JSONL y fusiona los claims en los Excels limpios → Excel final."""
    if not RESULTS_JSONL.exists():
        print("No hay resultados para exportar.")
        return

    # Leer resultados
    records: list[dict] = []
    with RESULTS_JSONL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass

    # Indexar por nombre (tomar el claim de mayor confianza por doctor)
    best_phone: dict[str, dict] = {}
    best_email: dict[str, dict] = {}
    for rec in records:
        name = rec["physician_name"]
        for claim in rec.get("claims", []):
            attr = claim["attribute"]
            conf = claim.get("confidence", 0)
            if attr == "phone":
                if name not in best_phone or conf > best_phone[name]["confidence"]:
                    best_phone[name] = {**claim, "source_url": rec.get("discovered_urls", [""])[0] if rec.get("discovered_urls") else ""}
            elif attr == "email":
                if name not in best_email or conf > best_email[name]["confidence"]:
                    best_email[name] = {**claim, "source_url": rec.get("discovered_urls", [""])[0] if rec.get("discovered_urls") else ""}

    print(f"\n📊 Resumen de enriquecimiento:")
    print(f"   Médicos procesados: {len(records)}")
    print(f"   Con teléfono encontrado: {len(best_phone)}")
    print(f"   Con email encontrado: {len(best_email)}")

    writer = pd.ExcelWriter(OUTPUT_EXCEL, engine="openpyxl")
    for country in countries:
        path = INPUT_FILES.get(country)
        if not path or not path.exists():
            continue
        df = pd.read_excel(path)
        # Reconstruir nombre_clave para merge
        def make_key(row):
            a1 = str(row.get("Apellido_1","") or "").strip()
            a2 = str(row.get("Apellido_2","") or "").strip()
            nm = str(row.get("Nombres","") or "").strip()
            if a2:
                return f"{a1} {a2} {nm}".upper()
            return f"{a1} {nm}".upper()

        df["_key"] = df.apply(make_key, axis=1)

        # Agregar columnas enriquecidas
        df["Tel_Enriquecido"] = df["_key"].map(
            lambda k: best_phone.get(k, {}).get("value_normalized", "")
        )
        df["Tel_Enriquecido_Conf"] = df["_key"].map(
            lambda k: round(best_phone.get(k, {}).get("confidence", 0), 3) or ""
        )
        df["Tel_Enriquecido_URL"] = df["_key"].map(
            lambda k: best_phone.get(k, {}).get("source_url", "")
        )
        df["Email_Enriquecido"] = df["_key"].map(
            lambda k: best_email.get(k, {}).get("value_normalized", "")
        )
        df["Email_Enriquecido_Conf"] = df["_key"].map(
            lambda k: round(best_email.get(k, {}).get("confidence", 0), 3) or ""
        )
        df.drop(columns=["_key"], inplace=True)

        df.to_excel(writer, sheet_name=country, index=False)
        new_phones = (df["Tel_Enriquecido"] != "").sum()
        new_emails = (df["Email_Enriquecido"] != "").sum()
        print(f"   {country}: +{new_phones} teléfonos, +{new_emails} emails enriquecidos")

    writer.close()
    print(f"\n📁 Excel guardado en: {OUTPUT_EXCEL}")


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main_async(args: argparse.Namespace) -> None:
    countries = [args.country] if args.country else ["CR", "PA"]
    done = load_already_done(RESULTS_JSONL) if args.resume else set()

    if done:
        print(f"  ↩️  Reanudando: {len(done)} médicos ya procesados en runs anteriores.")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for country in countries:
        print(f"\n🌎 [{country}] Cargando candidatos...")
        candidates = load_candidates(country, specialty_only=args.specialty_only)
        if candidates.empty:
            continue

        total = len(candidates)
        effective = min(total, args.limit) if args.limit else total
        print(f"  Total sin teléfono: {total}  |  A procesar ahora: {effective}")

        # Desglose por especialidad
        print("  Top especialidades:")
        for spec, cnt in candidates["Especialidad"].value_counts().head(8).items():
            marker = "★" if str(spec).lower() in PRIORITY_SPECS else " "
            print(f"    {marker} {spec:<30} {cnt}")

        print(f"\n  Iniciando enriquecimiento (DDG, ~4s/query)...")
        await run_enrichment(
            candidates, country, done,
            limit=args.limit,
            verbose=args.verbose,
        )

    # Exportar Excel final
    print("\n📝 Generando Excel con resultados...")
    build_output_excel(countries)


def main() -> None:
    p = argparse.ArgumentParser(description="Enriquecimiento de contactos CR + PA vía DDG")
    p.add_argument("--country", choices=["CR", "PA"], help="Solo este país")
    p.add_argument("--limit", type=int, help="Máximo de médicos a procesar (prueba)")
    p.add_argument("--specialty-only", action="store_true", help="Solo especialistas (excluye medicina general)")
    p.add_argument("--resume", action="store_true", help="Salta médicos ya procesados")
    p.add_argument("--verbose", "-v", action="store_true", help="Mostrar detalles por médico")
    p.add_argument("--export-only", action="store_true", help="Solo regenerar el Excel (sin hacer queries)")
    args = p.parse_args()

    if args.export_only:
        countries = [args.country] if args.country else ["CR", "PA"]
        build_output_excel(countries)
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
