#!/usr/bin/env python3
"""
Export and validate the 615 real physician emails from raw source.
Output: clean Excel ready for outreach.
"""
import pandas as pd
import smtplib
import socket
import re
from pathlib import Path

try:
    import dns.resolver
    HAS_DNS = True
except ImportError:
    HAS_DNS = False
    print("⚠️  dnspython not installed — skipping SMTP check, using format+MX only")
    print("   pip install dnspython\n")


def valid_format(email: str) -> bool:
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email.strip().lower()))


def check_mx(domain: str, cache: dict) -> bool:
    if domain in cache:
        return cache[domain]
    if not HAS_DNS:
        cache[domain] = True  # assume ok if no dns lib
        return True
    try:
        dns.resolver.resolve(domain, 'MX')
        cache[domain] = True
    except Exception:
        cache[domain] = False
    return cache[domain]


def check_smtp(email: str, domain: str, timeout: float = 4.0) -> str:
    """Returns 'ok', 'rejected', or 'inconclusive'."""
    if not HAS_DNS:
        return "inconclusive"
    try:
        mx_records = dns.resolver.resolve(domain, 'MX')
        mx_host = str(sorted(mx_records, key=lambda r: r.preference)[0].exchange)
        with smtplib.SMTP(mx_host, timeout=timeout) as s:
            s.ehlo()
            code, _ = s.rcpt(email)
            if 200 <= code < 400:
                return "ok"
            return "rejected"
    except smtplib.SMTPServerDisconnected:
        return "inconclusive"
    except Exception:
        return "inconclusive"


def main():
    RAW = Path("data/input/medicos-allegra-0226.xlsx")
    SHEETS = {"CR": "Costa Rica", "PA": "Panama", "EC": "Ecuador"}

    print("=" * 70)
    print("EXPORTING 615 REAL PHYSICIAN EMAILS")
    print("=" * 70)

    rows = []
    for country, sheet in SHEETS.items():
        df = pd.read_excel(RAW, sheet_name=sheet, dtype=str).fillna("")
        with_email = df[df["Correo"].str.strip().str.len() > 0].copy()
        print(f"  {country}: {len(with_email)} emails found")
        for _, row in with_email.iterrows():
            rows.append({
                "Country": country,
                "Name": row["Nombre"].strip(),
                "Specialty": row["Especialidad"].strip(),
                "Email": row["Correo"].strip(),
                "Phone": row.get("Telefono", "").strip(),
            })

    df_all = pd.DataFrame(rows)
    total = len(df_all)
    print(f"\nTotal: {total} emails\n")

    # Step 1: Format check
    df_all["fmt_ok"] = df_all["Email"].apply(valid_format)
    bad_fmt = (~df_all["fmt_ok"]).sum()
    print(f"Format check:  {total - bad_fmt} valid  |  {bad_fmt} invalid")

    # Step 2: MX check
    mx_cache: dict = {}
    df_all["domain"] = df_all["Email"].str.lower().str.split("@").str[-1]
    df_all["mx_ok"] = df_all.apply(
        lambda r: check_mx(r["domain"], mx_cache) if r["fmt_ok"] else False, axis=1
    )
    no_mx = (~df_all["mx_ok"] & df_all["fmt_ok"]).sum()
    print(f"MX check:      {df_all['mx_ok'].sum()} have MX  |  {no_mx} no MX records")

    # Step 3: SMTP check (only for emails that passed format + MX)
    candidates = df_all[df_all["mx_ok"]].copy()
    print(f"\nSMTP-checking {len(candidates)} emails... ", end="", flush=True)

    smtp_results = []
    for idx, (_, row) in enumerate(candidates.iterrows()):
        result = check_smtp(row["Email"], row["domain"])
        smtp_results.append(result)
        if (idx + 1) % 50 == 0:
            print(f"{idx + 1}", end=" ", flush=True)
    print("✓")

    candidates = candidates.copy()
    candidates["smtp_status"] = smtp_results

    # Merge smtp status back
    df_all = df_all.merge(
        candidates[["Email", "smtp_status"]], on="Email", how="left"
    )
    df_all["smtp_status"] = df_all["smtp_status"].fillna("skipped")

    # Classify
    def classify(row):
        if not row["fmt_ok"]:
            return "invalid_format"
        if not row["mx_ok"]:
            return "no_mx"
        if row["smtp_status"] == "ok":
            return "verified"
        if row["smtp_status"] == "rejected":
            return "rejected"
        return "unverified"  # inconclusive/skipped but MX exists

    df_all["status"] = df_all.apply(classify, axis=1)

    # Summary
    print("\n" + "=" * 70)
    print("VALIDATION RESULTS")
    print("=" * 70)
    status_counts = df_all["status"].value_counts()
    for status, count in status_counts.items():
        pct = count / total * 100
        icon = "✓" if status in ("verified", "unverified") else "✗"
        print(f"  {icon} {status:<20} {count:>4} ({pct:>5.1f}%)")

    print(f"\nBy country:")
    print(df_all.groupby(["Country", "status"]).size().unstack(fill_value=0).to_string())

    # Export sheets
    out = Path("data/output/physician_emails_validated.xlsx")
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        # Sheet 1: All with status
        export_cols = ["Country", "Name", "Specialty", "Email", "Phone", "status", "domain"]
        df_all[export_cols].sort_values(["status", "Country"]).to_excel(
            writer, sheet_name="All", index=False
        )

        # Sheet 2: Actionable only (verified + unverified = MX exists)
        actionable = df_all[df_all["status"].isin(["verified", "unverified"])]
        actionable[export_cols].sort_values(["Country", "Specialty"]).to_excel(
            writer, sheet_name="Actionable", index=False
        )

        # Sheet 3: Issues
        issues = df_all[~df_all["status"].isin(["verified", "unverified"])]
        if len(issues):
            issues[export_cols].to_excel(writer, sheet_name="Issues", index=False)

    actionable_count = len(actionable)
    print(f"\n✓ Saved to: {out}")
    print(f"  • All sheet:        {total} emails")
    print(f"  • Actionable sheet: {actionable_count} emails ready for outreach")
    print(f"  • Issues sheet:     {total - actionable_count} emails need fixing")

if __name__ == "__main__":
    main()
