"""CLI principal (Typer). Stub que crecerá con cada agente.

Por ahora expone:
    medintel ingest --input data/input/CR.xlsx --country CR
    medintel test-normalize "ZU#IGA RODRIGUEZ JUAN CARLOS"
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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


if __name__ == "__main__":
    app()
