"""CLI principal (Typer). Crece con cada agente.

Comandos:
    medintel ingest --input data/input/CR.xlsx --country CR
    medintel parse-name "ZU#IGA RODRIGUEZ JUAN CARLOS"
    medintel parse-phone "506 8827-1060" --country CR
    medintel enrich --input data/output/...__Ecuador_clean.xlsx --country EC --limit 5
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import typer
from rich.console import Console
from rich.table import Table

from medintel.application.agents.enrichment import EnrichmentAgent
from medintel.infrastructure.llm.extractors import LLMExtractor
from medintel.normalization.names import parse_latam_name
from medintel.normalization.phones import normalize as normalize_phone

app = typer.Typer(add_completion=False, no_args_is_help=True)
console = Console()


@app.command()
def ingest(
    input: Path = typer.Option(..., exists=True, dir_okay=False, help="Excel de entrada"),
    country: str = typer.Option(..., help="CR | PA | ..."),
    sheet: str = typer.Option("0", help="Hoja: nombre o índice"),
) -> None:
    """Limpieza + reporte de calidad del Excel de entrada."""
    script = Path(__file__).resolve().parents[3] / "scripts" / "ingest_excel.py"
    cmd = [sys.executable, str(script), "--input", str(input), "--country", country, "--sheet", sheet]
    subprocess.run(cmd, check=True)


@app.command("parse-name")
def parse_name_cmd(name: str) -> None:
    """Diagnóstico rápido del parser de nombres."""
    p = parse_latam_name(name)
    t = Table(title=f"Parse: {name!r}")
    t.add_column("Campo"); t.add_column("Valor")
    t.add_row("family_name_1", p.family_name_1)
    t.add_row("family_name_2", p.family_name_2 or "—")
    t.add_row("given_name", p.given_name)
    t.add_row("display_name", p.display_name)
    t.add_row("full_name", p.full_name)
    console.print(t)


@app.command("parse-phone")
def parse_phone_cmd(raw: str, country: str = "CR") -> None:
    """Diagnóstico rápido del normalizador de teléfonos."""
    n = normalize_phone(raw, country)
    if not n:
        console.print(f"[red]Inválido[/red] para {country}: {raw!r}")
        raise typer.Exit(1)
    t = Table(title=f"Phone: {raw!r}")
    t.add_column("Campo"); t.add_column("Valor")
    t.add_row("e164", n.e164)
    t.add_row("national", n.national)
    t.add_row("type", n.line_type)
    t.add_row("country", n.country)
    console.print(t)


@app.command()
def enrich(
    input: Path = typer.Option(..., exists=True, dir_okay=False, help="Excel limpio (output de ingest)"),
    country: str = typer.Option(..., help="CR | PA | EC | MX | ..."),
    limit: int = typer.Option(5, help="Cuántos médicos enriquecer (corre controlada)"),
    only_missing: bool = typer.Option(True, help="Solo médicos sin email ni teléfono"),
    use_llm: bool = typer.Option(False, "--use-llm/--no-llm", help="Usar LLM como fallback (requiere ANTHROPIC_API_KEY)"),
    output: Path = typer.Option(Path("data/output/enrichment.json"), help="Path de salida JSON con claims"),
) -> None:
    """Enriquecimiento controlado: Doctoralia + Bing + LLM(opc) → claims con confidence."""
    df = pd.read_excel(input, dtype=str).fillna("")
    if only_missing:
        df = df[(df.get("Correo", "") == "") & (df.get("Telefono", "") == "")]
    df = df.head(limit)
    console.print(f"[bold]Enriqueciendo {len(df)} médicos de {input.name} (país: {country})[/bold]")

    llm = LLMExtractor() if use_llm else None
    if use_llm and (llm is None or not llm.available):
        console.print("[red]LLM no disponible: revisar ANTHROPIC_API_KEY o `pip install -e '.[llm]'`[/red]")
        raise typer.Exit(2)

    agent = EnrichmentAgent(country=country, llm=llm, use_llm_fallback=use_llm)
    all_results: list[dict] = []

    async def _run() -> None:
        for _, row in df.iterrows():
            name_str = str(row.get("Nombre", "") or "")
            specialty = str(row.get("Especialidad", "") or "").lower()
            name_format = "given_first" if country == "PA" else "family_first"
            name_parts = parse_latam_name(name_str, name_format=name_format)
            console.print(f"  → {name_parts.full_name}")
            try:
                result = await agent.enrich(name_parts, specialty_code=specialty)
            except Exception as e:
                console.print(f"    [red]error: {e}[/red]")
                continue
            all_results.append({
                "name": name_parts.full_name,
                "specialty": specialty,
                "pages_fetched": result.pages_fetched,
                "pages_with_name_present": result.pages_with_name_present,
                "discovered_urls": result.discovered_urls,
                "used_llm": result.used_llm,
                "notes": result.notes,
                "claims": [
                    {
                        "attribute": c.attribute, "value": c.value, "subkind": c.subkind,
                        "confidence": c.confidence, "is_inferred": c.is_inferred,
                        "source_url": c.source_url, "explanation": c.explanation,
                    }
                    for c in result.claims
                ],
            })
            console.print(
                f"    [green]pages={result.pages_fetched} "
                f"with_name={result.pages_with_name_present} "
                f"claims={len(result.claims)}{' (LLM)' if result.used_llm else ''}[/green]"
            )

    asyncio.run(_run())

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(all_results, indent=2, ensure_ascii=False))
    console.print(f"\n📑 Resultados: {output}")
    total_claims = sum(len(r["claims"]) for r in all_results)
    found = sum(1 for r in all_results if r["pages_with_name_present"] > 0)
    console.print(f"Resumen: {found}/{len(all_results)} médicos con datos encontrados; {total_claims} claims emitidos.")


if __name__ == "__main__":
    app()
