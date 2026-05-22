#!/usr/bin/env python3
"""
Scraper: Salud 360 Costa Rica
https://directorio.salud360.cr/es/doctor
Extrae: nombre, especialidad, teléfonos, emails
Output: data/output/salud360_cr_raw.json + agrega a medicos_costa_rica_v2.xlsx
"""

import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill

BASE = "https://directorio.salud360.cr"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "es-CR,es;q=0.9",
}

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_JSON = ROOT / "data/output/salud360_cr_raw.json"
OUTPUT_XLSX = ROOT / "data/output/medicos_costa_rica_v2.xlsx"


def get_listing_urls(session: requests.Session) -> list[dict]:
    """Get all doctor URLs from paginated listing."""
    all_doctors = []
    seen_urls = set()
    page = 1
    while True:
        url = f"{BASE}/es/doctor" if page == 1 else f"{BASE}/es/doctor/{page}"
        print(f"  Listing page {page}: {url}")
        r = session.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        links = soup.select('a[href*="/es/doctor/"]')
        # Filter out page number links
        doc_links = [
            {"name": a.get_text(strip=True), "url": BASE + a["href"] if a["href"].startswith("/") else a["href"]}
            for a in links
            if not re.search(r"/es/doctor/\d+$", a["href"])
        ]
        new_count = 0
        for d in doc_links:
            if d["url"] not in seen_urls:
                seen_urls.add(d["url"])
                all_doctors.append(d)
                new_count += 1
        print(f"    → {new_count} new doctors (total: {len(all_doctors)})")
        if new_count == 0:
            break
        # Check if there's a next page
        next_link = soup.select_one('a[href*="/es/doctor/"]')
        # Check pagination
        pagination = soup.select('.pagination-elem a.page-number')
        page_numbers = [int(a.get_text()) for a in pagination if a.get_text().isdigit()]
        if not page_numbers or page >= max(page_numbers):
            break
        page += 1
        time.sleep(0.3)
    return all_doctors


def parse_profile(html: str, url: str, fallback_name: str) -> dict:
    """Parse a doctor's profile page."""
    soup = BeautifulSoup(html, "html.parser")
    # Name from title: "Name - Specialty - Salud 360..."
    title = soup.title.get_text() if soup.title else ""
    parts = title.split(" - ")
    name = parts[0].strip() if parts else fallback_name
    # If name looks like site name, use fallback
    if "Salud 360" in name or not name:
        name = fallback_name
    specialty = ""
    if len(parts) > 2:
        specialty = ", ".join(parts[1:-2]).strip()
    elif len(parts) == 2:
        specialty = parts[1].strip()

    # Phone numbers
    phones = list(dict.fromkeys(
        requests.utils.unquote(a["href"].replace("tel:", ""))
        for a in soup.select('[href^="tel:"]')
    ))

    # Emails
    emails = list(dict.fromkeys(
        a["href"].replace("mailto:", "").strip().rstrip("/")
        for a in soup.select('[href^="mailto:"]')
    ))
    # Clean up emails with spaces or slashes
    emails = [e.split(" ")[0].strip() for e in emails if "@" in e]
    emails = list(dict.fromkeys(emails))

    return {
        "Nombre": name,
        "Especialidad": specialty,
        "Telefono": " | ".join(phones),
        "Email": " | ".join(emails),
        "Perfil_URL": url,
        "Pais": "Costa Rica",
        "Fuente": "Salud 360 CR",
    }


def scrape_all_profiles(session: requests.Session, doctors: list[dict]) -> list[dict]:
    """Fetch and parse each doctor profile."""
    results = []
    for i, doc in enumerate(doctors, 1):
        try:
            r = session.get(doc["url"], headers=HEADERS, timeout=15)
            r.raise_for_status()
            result = parse_profile(r.text, doc["url"], doc["name"])
            results.append(result)
            if i % 50 == 0:
                print(f"  Processed {i}/{len(doctors)} profiles...")
        except Exception as e:
            print(f"  Error {doc['url']}: {e}")
            results.append({
                "Nombre": doc["name"], "Especialidad": "", "Telefono": "",
                "Email": "", "Perfil_URL": doc["url"],
                "Pais": "Costa Rica", "Fuente": "Salud 360 CR",
            })
        time.sleep(0.1)  # polite delay
    return results


def update_excel(new_data: list[dict]) -> int:
    """Add new doctors to medicos_costa_rica_v2.xlsx."""
    wb = load_workbook(OUTPUT_XLSX)
    ws = wb.active

    # Read existing names for deduplication
    existing = set()
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[0]:
            existing.add(str(row[0]).strip().upper())

    # Get column headers
    headers = {ws.cell(1, c).value: c for c in range(1, ws.max_column + 1)}
    print(f"  Existing doctors in Excel: {len(existing)}")
    print(f"  Columns: {list(headers.keys())}")

    col_map = {
        "Nombre": headers.get("Nombre", 1),
        "Especialidad": headers.get("Especialidad", 2),
        "Email": headers.get("Email", 3),
        "Telefono": headers.get("Telefono", 4),
        "Fuente": headers.get("Fuente", headers.get("Fuente", ws.max_column)),
    }

    added = 0
    next_row = ws.max_row + 1
    font = Font(name="Arial", size=10)

    for doc in new_data:
        name_key = doc["Nombre"].strip().upper()
        if name_key in existing or not doc["Nombre"].strip():
            continue
        existing.add(name_key)
        for field, col in col_map.items():
            cell = ws.cell(row=next_row, column=col, value=doc.get(field, ""))
            cell.font = font
        # Add source if there's a column for it
        if "Perfil_URL" in headers:
            cell = ws.cell(row=next_row, column=headers["Perfil_URL"], value=doc.get("Perfil_URL", ""))
            cell.font = font
        if "Pais" in headers:
            cell = ws.cell(row=next_row, column=headers["Pais"], value="Costa Rica")
            cell.font = font
        next_row += 1
        added += 1

    wb.save(OUTPUT_XLSX)
    return added


def main():
    print("=== Salud 360 CR Scraper ===")
    session = requests.Session()

    print("\n[1] Collecting doctor listing URLs...")
    doctors = get_listing_urls(session)
    print(f"Total URLs collected: {len(doctors)}")

    print("\n[2] Fetching individual profiles...")
    results = scrape_all_profiles(session, doctors)

    # Stats
    with_phone = sum(1 for r in results if r["Telefono"])
    with_email = sum(1 for r in results if r["Email"])
    with_spec = sum(1 for r in results if r["Especialidad"])
    print(f"\nStats: {len(results)} total | {with_phone} con tel | {with_email} con email | {with_spec} con especialidad")

    print("\n[3] Saving JSON...")
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Saved: {OUTPUT_JSON}")

    print("\n[4] Updating Excel...")
    added = update_excel(results)
    print(f"Added {added} new doctors to Excel")

    print("\n=== DONE ===")
    print(f"Total scraped: {len(results)}")
    print(f"New in Excel: {added}")
    print(f"With phone: {with_phone}")
    print(f"With email: {with_email}")


if __name__ == "__main__":
    main()
