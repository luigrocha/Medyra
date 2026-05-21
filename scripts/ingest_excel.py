"""Ingest tu Excel actual → limpieza + reporte de calidad.

Uso (auto-detect: procesa todas las hojas con su config por defecto):
    python scripts/ingest_excel.py --input data/input/medicos-allegra-0226.xlsx

Uso (una sola hoja):
    python scripts/ingest_excel.py --input data/input/CR.xlsx --country CR --sheet "Costa Rica"

Configuración por hoja (SHEET_CONFIG): asocia nombre de hoja → país + formato
de nombre. Editable in-place sin tocar lógica de procesamiento.

Salidas en `data/output/`:
    - <stem>__<sheet>_clean.xlsx     Excel con celdas limpias listo para tu equipo
    - <stem>__<sheet>_issues.xlsx    Solo filas con problemas detectados
    - <stem>_report.json             Métricas agregadas por hoja

NO requiere base de datos. Diseñado para dar valor día 1.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from medyra.normalization import emails as email_norm
from medyra.normalization import phones as phone_norm
from medyra.normalization.names import NameParts, parse_latam_name
from medyra.normalization.specialties import normalize_specialty


# Mapeo flexible de nombres de columnas (case-insensitive).
COLUMN_ALIASES: dict[str, set[str]] = {
    "specialty": {"especialidad", "specialty", "esp"},
    "name":      {"nombre", "name", "doctor", "medico", "médico"},
    "email":     {"correo", "email", "e-mail", "mail"},
    "phone":     {"telefono", "teléfono", "phone", "tel", "celular"},
    "assigned":  {"asignado", "assigned", "owner"},
    "comment":   {"comentario", "comment", "notas", "nota"},
}


# Config por hoja: país (ISO-2) + formato de nombre.
# El parser de nombres detecta automáticamente Title Case vs ALL CAPS,
# pero damos el hint explícito porque es más robusto.
SHEET_CONFIG: dict[str, dict[str, str]] = {
    "Costa Rica": {"country": "CR", "name_format": "family_first"},
    "Panama":     {"country": "PA", "name_format": "given_first"},
    "Panamá":     {"country": "PA", "name_format": "given_first"},
    "Ecuador":    {"country": "EC", "name_format": "family_first"},
    "Mexico":     {"country": "MX", "name_format": "family_first"},
    "México":     {"country": "MX", "name_format": "family_first"},
    "Colombia":   {"country": "CO", "name_format": "family_first"},
}


def _detect_columns(df: pd.DataFrame) -> dict[str, str]:
    lower = {c.lower().strip(): c for c in df.columns}
    out: dict[str, str] = {}
    for key, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in lower:
                out[key] = lower[a]
                break
    missing = {"specialty", "name"} - out.keys()
    if missing:
        raise SystemExit(f"Faltan columnas obligatorias: {missing}. Vistas: {list(df.columns)}")
    return out


@dataclass
class RowResult:
    row_index: int
    raw_name: str
    name_parts: dict
    full_name_canonical: str
    specialty_code: str | None
    specialty_raw: str
    email_clean: str | None
    email_issue: str | None
    email_suggestion: str | None
    phone_e164: str | None
    phone_type: str | None
    phone_issue: str | None
    assigned: str | None
    comment: str | None
    ñ_recovered: bool


@dataclass
class Report:
    file: str
    sheet: str
    country: str
    name_format: str
    rows_total: int = 0
    rows_with_email: int = 0
    rows_with_phone: int = 0
    rows_complete: int = 0
    rows_empty_both: int = 0
    fixes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    specialty_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    institutional_phones: list[str] = field(default_factory=list)
    duplicate_canonical_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fixes"] = dict(d["fixes"])
        d["specialty_counts"] = dict(d["specialty_counts"])
        return d


def process_sheet(df: pd.DataFrame, country: str, name_format: str, sheet: str) -> tuple[list[RowResult], Report]:
    cols = _detect_columns(df)
    results: list[RowResult] = []
    report = Report(file="", sheet=sheet, country=country, name_format=name_format, rows_total=len(df))

    all_phones: list[str] = []
    canonical_seen: Counter[str] = Counter()

    for pos, (_, row) in enumerate(df.iterrows()):
        raw_name = str(row.get(cols["name"], "") or "")
        ñ = "#" in raw_name
        parts: NameParts = parse_latam_name(raw_name, name_format=name_format)
        canonical = parts.full_name
        if canonical:
            canonical_seen[canonical] += 1
        if ñ:
            report.fixes["ñ_recovered"] += 1

        spec_raw = str(row.get(cols["specialty"], "") or "")
        spec_code = normalize_specialty(spec_raw)
        if spec_code:
            report.specialty_counts[spec_code] += 1
        elif spec_raw:
            report.fixes["specialty_unmapped"] += 1
            report.specialty_counts[f"_UNMAPPED:{spec_raw}"] += 1

        # Email
        email_clean: str | None = None
        email_issue: str | None = None
        email_suggestion: str | None = None
        raw_email = str(row.get(cols.get("email", ""), "") or "").strip() if cols.get("email") else ""
        if raw_email:
            had_ws = raw_email != raw_email.strip() or "\n" in raw_email or "  " in raw_email
            norm = email_norm.normalize(raw_email)
            if norm:
                email_clean = norm.address
                if had_ws:
                    report.fixes["email_whitespace_cleaned"] += 1
                if norm.looks_typoed:
                    email_issue = "domain_typo"
                    email_suggestion = norm.suggested_domain
                    report.fixes["email_domain_typo_detected"] += 1
                report.rows_with_email += 1
            else:
                email_issue = "invalid_syntax"
                report.fixes["email_invalid_syntax"] += 1

        # Phone
        phone_e164: str | None = None
        phone_type: str | None = None
        phone_issue: str | None = None
        raw_phone = str(row.get(cols.get("phone", ""), "") or "").strip() if cols.get("phone") else ""
        if raw_phone:
            norm_p = phone_norm.normalize(raw_phone, country)
            if norm_p:
                phone_e164 = norm_p.e164
                phone_type = norm_p.line_type
                all_phones.append(phone_e164)
                report.rows_with_phone += 1
                if raw_phone != norm_p.e164:
                    report.fixes["phone_normalized_to_e164"] += 1
            else:
                phone_issue = "invalid_phone"
                report.fixes["phone_invalid"] += 1

        if email_clean and phone_e164:
            report.rows_complete += 1
        elif not email_clean and not phone_e164:
            report.rows_empty_both += 1

        results.append(RowResult(
            row_index=pos,
            raw_name=raw_name,
            name_parts={
                "family_name_1": parts.family_name_1,
                "family_name_2": parts.family_name_2,
                "given_name": parts.given_name,
            },
            full_name_canonical=canonical,
            specialty_code=spec_code,
            specialty_raw=spec_raw,
            email_clean=email_clean,
            email_issue=email_issue,
            email_suggestion=email_suggestion,
            phone_e164=phone_e164,
            phone_type=phone_type,
            phone_issue=phone_issue,
            assigned=str(row.get(cols.get("assigned", ""), "") or "") if cols.get("assigned") else None,
            comment=str(row.get(cols.get("comment", ""), "") or "") if cols.get("comment") else None,
            ñ_recovered=ñ,
        ))

    inst = phone_norm.detect_institutional(all_phones, threshold=3)
    report.institutional_phones = sorted(inst)
    for r in results:
        if r.phone_e164 and r.phone_e164 in inst:
            r.phone_type = f"{r.phone_type}|clinic_main"

    report.duplicate_canonical_names = [n for n, c in canonical_seen.items() if c > 1]
    return results, report


def write_sheet_outputs(results: list[RowResult], stem: str, sheet: str, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = sheet.replace(" ", "_").replace("/", "-")
    clean_rows = [{
        "Especialidad":    (r.specialty_code or r.specialty_raw or "").upper(),
        "Nombre":          r.full_name_canonical,
        "Apellido_1":      r.name_parts["family_name_1"],
        "Apellido_2":      r.name_parts["family_name_2"] or "",
        "Nombres":         r.name_parts["given_name"],
        "Correo":          r.email_clean or "",
        "Correo_sugerido": (
            r.email_clean.split("@")[0] + "@" + r.email_suggestion
            if (r.email_clean and r.email_suggestion) else ""
        ),
        "Telefono":        r.phone_e164 or "",
        "Tipo_Telefono":   r.phone_type or "",
        "Asignado":        r.assigned or "",
        "COMENTARIO":      r.comment or "",
        "Calidad_Email":   "OK" if r.email_clean and not r.email_issue else (r.email_issue or "FALTA"),
        "Calidad_Tel":     "OK" if r.phone_e164 else (r.phone_issue or "FALTA"),
        "Ñ_Recovered":     r.ñ_recovered,
    } for r in results]

    clean_df = pd.DataFrame(clean_rows)
    clean_df.to_excel(output_dir / f"{stem}__{safe}_clean.xlsx", index=False)

    issues_df = clean_df[
        (clean_df["Calidad_Email"] != "OK") |
        (clean_df["Calidad_Tel"]   != "OK") |
        (clean_df["Correo_sugerido"] != "")
    ]
    if not issues_df.empty:
        issues_df.to_excel(output_dir / f"{stem}__{safe}_issues.xlsx", index=False)


def summarize(report: Report) -> str:
    return (
        f"  📊 {report.sheet:14} ({report.country})  "
        f"rows={report.rows_total:5}  "
        f"email={report.rows_with_email:5}  "
        f"tel={report.rows_with_phone:5}  "
        f"completos={report.rows_complete:5}  "
        f"sin-nada={report.rows_empty_both:5}  "
        f"Ñ={report.fixes.get('ñ_recovered', 0):3}  "
        f"typos={report.fixes.get('email_domain_typo_detected', 0):3}  "
        f"unmapped_esp={report.fixes.get('specialty_unmapped', 0):3}"
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--country", help="Solo si procesas una hoja (override de SHEET_CONFIG)")
    p.add_argument("--name-format", choices=["family_first", "given_first"], help="Override")
    p.add_argument("--sheet", help="Nombre de hoja específica (default: todas)")
    p.add_argument("--output-dir", type=Path, default=Path("data/output"))
    args = p.parse_args()

    if not args.input.exists():
        raise SystemExit(f"No existe: {args.input}")

    xls = pd.ExcelFile(args.input)
    sheets_to_process: list[str] = (
        [args.sheet] if args.sheet else [str(s) for s in xls.sheet_names]
    )

    reports: list[Report] = []
    print(f"📂 {args.input.name}  ({len(sheets_to_process)} hoja(s))")
    for sheet in sheets_to_process:
        cfg: dict[str, str] = SHEET_CONFIG.get(sheet, {})
        country = args.country or cfg.get("country")
        name_format = args.name_format or cfg.get("name_format", "family_first")
        if not country:
            print(f"  ⚠️  '{sheet}': sin config y sin --country. Skip.")
            continue

        df = pd.read_excel(args.input, sheet_name=sheet, dtype=str).fillna("")
        results, report = process_sheet(df, country=country, name_format=name_format, sheet=sheet)
        report.file = args.input.name
        reports.append(report)
        write_sheet_outputs(results, args.input.stem, sheet, args.output_dir)
        print(summarize(report))

    report_path = args.output_dir / f"{args.input.stem}_report.json"
    with report_path.open("w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in reports], f, indent=2, ensure_ascii=False)
    print(f"\n📁 Salidas en {args.output_dir}/")
    print(f"📑 Reporte global: {report_path}")


if __name__ == "__main__":
    main()
