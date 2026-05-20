#!/usr/bin/env python3
"""
Phase 2B: Email Pattern Generation
Generate candidate emails for 9,815 physicians without emails
using clinic names, specialties, and common patterns
"""
import pandas as pd
from pathlib import Path
from collections import Counter
import re
import sys

def extract_domain_from_clinic(clinic_name: str) -> str:
    """Extract likely domain from clinic name."""
    if not clinic_name or clinic_name.strip() == "":
        return ""

    # Remove common clinic suffixes
    clinic = clinic_name.strip()
    clinic = re.sub(r'\s+(S\.A\.|SPA|LTDA|CLINIC|CLÍNICA|HOSPITAL|CENTRO|INSTITUTE).*$', '', clinic, flags=re.IGNORECASE)
    clinic = clinic.strip()

    if len(clinic) < 3:
        return ""

    # Convert to domain-friendly format
    domain = clinic.lower()
    domain = re.sub(r'[^a-z0-9\s]', '', domain)  # Remove special chars
    domain = re.sub(r'\s+', '', domain)  # Remove spaces
    domain = domain[:30]  # Limit length

    return domain if domain else ""

def generate_email_patterns(first_name: str, family_name1: str, family_name2: str, clinic_domain: str) -> list:
    """Generate 5 most common email patterns."""
    patterns = []

    if not first_name or not family_name1:
        return patterns

    # Clean names
    first = first_name.strip().lower()
    fam1 = family_name1.strip().lower()
    fam2 = family_name2.strip().lower() if family_name2 else ""

    # Remove accents for email patterns
    import unicodedata
    first = ''.join(c for c in unicodedata.normalize('NFD', first) if unicodedata.category(c) != 'Mn')
    fam1 = ''.join(c for c in unicodedata.normalize('NFD', fam1) if unicodedata.category(c) != 'Mn')
    fam2 = ''.join(c for c in unicodedata.normalize('NFD', fam2) if unicodedata.category(c) != 'Mn')

    full_name = f"{fam1}{fam2}{first}".replace(' ', '')

    if clinic_domain:
        domain = f"@{clinic_domain}.com"
        # Common patterns (most likely first)
        patterns.append(f"{first}.{fam1}{domain}")           # john.smith@clinic.com
        patterns.append(f"{first}{fam1}{domain}")             # johnsmith@clinic.com
        patterns.append(f"{first[0]}{fam1}{domain}")          # jsmith@clinic.com
        patterns.append(f"{first}.{fam1}.{fam2}{domain}" if fam2 else f"{first}.{fam1}{domain}")  # john.smith.jones@clinic.com
        patterns.append(f"{full_name}{domain}")               # johnsmithjones@clinic.com
    else:
        # Generic patterns (for physicians without clinic info)
        patterns.append(f"{first}.{fam1}@clinic.com")
        patterns.append(f"{first}{fam1}@clinic.com")
        patterns.append(f"{first[0]}{fam1}@clinic.com")
        patterns.append(f"dr.{first}.{fam1}@clinic.com")
        patterns.append(f"doctor.{first}.{fam1}@clinic.com")

    return patterns

def analyze_clinics(df, country: str):
    """Analyze clinic data for domain extraction."""
    print(f"\n  Analyzing clinic data...")

    # Find clinic column
    clinic_cols = [col for col in df.columns if 'clinic' in col.lower() or 'empresa' in col.lower() or 'institution' in col.lower()]

    if not clinic_cols:
        print(f"    ⚠️  No clinic column found")
        return Counter(), None

    clinic_col = clinic_cols[0]
    clinics = df[clinic_col].dropna()
    clinics = clinics[clinics.str.len() > 0]

    clinic_counts = Counter(clinics)

    print(f"    Found {len(clinic_counts)} unique clinics")
    print(f"    Top 5:")
    for clinic, count in clinic_counts.most_common(5):
        print(f"      {clinic[:50]:<50} ({count} physicians)")

    return clinic_counts, clinic_col

def main():
    data_dir = Path("data/output")

    # Use raw input file — has all real physician names
    RAW_FILE = Path("data/input/medicos-allegra-0226.xlsx")
    SHEET_MAP = {"CR": "Costa Rica", "PA": "Panama", "EC": "Ecuador"}

    print("=" * 70)
    print("PHASE 2B: EMAIL PATTERN GENERATION (from raw source)")
    print("=" * 70)

    all_generated = []

    for country, sheet in SHEET_MAP.items():
        if not RAW_FILE.exists():
            print(f"⚠️  Raw file not found: {RAW_FILE}")
            break

        print(f"\n{country}: sheet '{sheet}'")
        df = pd.read_excel(RAW_FILE, sheet_name=sheet, dtype=str).fillna("")

        # Only process records with a name
        df_named = df[df["Nombre"].str.strip().str.len() > 0].copy()
        no_email = df_named[df_named["Correo"].str.strip().str.len() == 0].copy()
        has_email = df_named[df_named["Correo"].str.strip().str.len() > 0].copy()

        print(f"  Real physicians (have name): {len(df_named)}")
        print(f"  Already have email:          {len(has_email)}")
        print(f"  Need email pattern:          {len(no_email)}")

        clinic_col = None

        # Generate patterns
        print(f"  Generating email patterns...")

        for idx, row in no_email.iterrows():
            # Parse from "Nombre" field (format: APELLIDO APELLIDO NOMBRE or NOMBRE APELLIDO)
            full_name = row.get("Nombre", "").strip()
            if not full_name:
                continue

            parts = full_name.split()
            if len(parts) >= 3:
                fam1 = parts[0]
                fam2 = parts[1]
                first_name = parts[2]
            elif len(parts) == 2:
                fam1 = parts[0]
                fam2 = ""
                first_name = parts[1]
            else:
                continue

            clinic = ""
            specialty = row.get("Especialidad", "")

            # Skip if still no name
            if not (first_name and fam1):
                continue

            # Extract domain from clinic
            clinic_domain = extract_domain_from_clinic(clinic) if clinic else ""

            # Generate patterns
            patterns = generate_email_patterns(first_name, fam1, fam2, clinic_domain)

            for priority, pattern in enumerate(patterns, 1):
                all_generated.append({
                    "Country": country,
                    "Physician_Name": full_name,
                    "First_Name": first_name,
                    "Family_Name_1": fam1,
                    "Family_Name_2": fam2,
                    "Specialty": specialty,
                    "Generated_Email": pattern,
                    "Priority": priority,
                })

        generated_for_country = len([r for r in all_generated if r["Country"] == country])
        print(f"  ✓ Generated {generated_for_country} patterns ({len(no_email)} physicians × 5 patterns)")

    # Save results
    df_generated = pd.DataFrame(all_generated)

    output_file = Path("data/output/email_patterns_generated.xlsx")
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Create Excel with two sheets
    with pd.ExcelWriter(output_file, engine='openpyxl') as writer:
        # Sheet 1: All patterns
        df_generated.to_excel(writer, sheet_name="All_Patterns", index=False)

        # Sheet 2: Top 1 pattern per physician (for quick validation)
        top1 = df_generated[df_generated["Priority"] == 1]
        top1.to_excel(writer, sheet_name="Top_Priority", index=False)

    print("\n" + "=" * 70)
    print("GENERATION SUMMARY")
    print("=" * 70)

    by_country = df_generated.groupby("Country").agg({
        "Physician_Name": "nunique",
        "Generated_Email": "count"
    })

    print(f"{'Country':<10} {'Physicians':<15} {'Patterns':<15}")
    print("-" * 70)
    for country in ["CR", "PA", "EC"]:
        if country in by_country.index:
            physicians = by_country.loc[country, "Physician_Name"]
            patterns = by_country.loc[country, "Generated_Email"]
            print(f"{country:<10} {int(physicians):<15} {int(patterns):<15}")

    total_physicians = df_generated["Physician_Name"].nunique()
    total_patterns = len(df_generated)

    print("-" * 70)
    print(f"{'TOTAL':<10} {total_physicians:<15} {total_patterns:<15}")

    print(f"\n✓ Results saved to: {output_file}")
    print(f"  • All_Patterns sheet: {total_patterns} candidate emails ({total_physicians} physicians)")
    print(f"  • Top_Priority sheet: {total_physicians} top-ranked patterns (1 per physician)")

    print("\n" + "=" * 70)
    print("NEXT STEP: VALIDATION")
    print("=" * 70)
    print(f"Next: Validate {total_patterns} email patterns via:")
    print(f"  • SMTP check on patterns (cheap, free)")
    print(f"  • Or API: Hunter.io, Email-Checker, ZeroBounce (paid)")
    print(f"  • Or Google Search: Verify clinic domain exists")

if __name__ == "__main__":
    main()
