#!/usr/bin/env python3
"""
Consolidador final — base de ginecólogos Colombia.

Fusiona las tres fuentes en un solo Excel entregable:

  1. Doctoralia CO   → quién es ginecólogo (la especialidad), dirección,
                       consultorio, ciudad, EPS, precio, rating.
  2. REPS (MinSalud) → email y teléfono OFICIALES, vía cruce de nombre.
  3. CSV semilla     → los contactos que el cliente ya validó a mano.
                       Mandan sobre todo lo demás.
  4. Enriquecimiento web (opcional) → emails encontrados en sitios propios,
                       solo se usa para llenar huecos que REPS no cubrió.

Precedencia de email:  semilla > REPS(alta) > REPS(media) > web > vacío.
Nunca se mezcla un email de confianza `baja` o `ambiguo` en la columna Correo:
esos van a la hoja `Revision_Manual` para que un humano decida.

Uso:
    python3 scripts/build_colombia_dataset.py
    python3 scripts/build_colombia_dataset.py --seed-csv "/ruta/Book(Ginecología).csv"
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "data" / "output"

LISTING_JSONL = OUTPUT_DIR / "doctoralia_co_listing.jsonl"
PROFILES_JSONL = OUTPUT_DIR / "doctoralia_co_profiles.jsonl"
MATCH_JSONL = OUTPUT_DIR / "reps_match_co.jsonl"
WEB_JSONL = OUTPUT_DIR / "enrichment_co_emails.jsonl"
OUTPUT_XLSX = OUTPUT_DIR / "medicos_colombia_ginecologia.xlsx"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S", handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger(__name__)

_TITULO_RE = re.compile(r"^\s*(Dra?\.|Dr\.|Dra|Dr)\s+", re.IGNORECASE)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def name_key(s: str) -> frozenset[str]:
    """Clave de identidad tolerante: conjunto de tokens significativos.

    El cliente escribe "Alejandra Galofre" y Doctoralia "Dra. Alejandra Galofre
    Cifuentes". Comparar conjuntos de tokens ≥4 permite el match sin fuzzy.
    """
    s = _TITULO_RE.sub("", s or "")
    s = re.sub(r"[^A-Za-zÑñ ]", " ", strip_accents(s)).upper()
    return frozenset(t for t in s.split() if len(t) >= 4)


def split_titulo(nombre_raw: str) -> tuple[str, str]:
    m = _TITULO_RE.match(nombre_raw or "")
    if not m:
        return "", (nombre_raw or "").strip()
    return m.group(1).rstrip("."), _TITULO_RE.sub("", nombre_raw).strip()


# ── Fuentes ───────────────────────────────────────────────────────────────────

def load_doctoralia() -> dict[str, dict]:
    """El listado es la autoridad sobre quién es médico.

    El JSONL de perfiles arrastra clínicas capturadas antes de que el scraper
    filtrara `data-test-entity-type="facility"`. Solo enriquecemos URLs que el
    listado vigente reconoce como personas; el resto se ignora.
    """
    by_url = {r["perfil_url"]: dict(r) for r in read_jsonl(LISTING_JSONL)}
    for p in read_jsonl(PROFILES_JSONL):
        if p["perfil_url"] in by_url:
            by_url[p["perfil_url"]].update(p)
    return by_url


def load_reps() -> dict[str, dict]:
    return {r["perfil_url"]: r for r in read_jsonl(MATCH_JSONL) if r.get("perfil_url")}


def load_web() -> dict[str, dict]:
    out = {}
    for r in read_jsonl(WEB_JSONL):
        if r.get("emails"):
            out[r["perfil_url"]] = r
    return out


def load_seed(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    for enc in ("cp1252", "latin-1", "utf-8"):
        try:
            df = pd.read_csv(path, sep=";", encoding=enc, dtype=str)
            break
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    else:
        log.warning(f"No se pudo leer el CSV semilla: {path}")
        return []

    # El archivo del cliente trae la cabecera corrida (la 4ª columna es un
    # teléfono, no un nombre de campo). Reasignamos por posición.
    df = df.iloc[:, :4]
    df.columns = ["Especialidad", "Nombre", "Correo", "Telefono"]
    df = df.dropna(subset=["Nombre"])
    df = df[df["Nombre"].str.strip() != ""]

    rows = []
    for _, r in df.iterrows():
        rows.append({
            "nombre": str(r["Nombre"]).strip(),
            "email": (str(r["Correo"]).strip() if pd.notna(r["Correo"]) else ""),
            "telefono": (str(r["Telefono"]).strip() if pd.notna(r["Telefono"]) else ""),
        })
    log.info(f"  semilla cliente: {len(rows)} registros")
    return rows


# ── Construcción ──────────────────────────────────────────────────────────────

def build() -> tuple[pd.DataFrame, pd.DataFrame]:
    doct = load_doctoralia()
    reps = load_reps()
    web = load_web()
    hoy = date.today().isoformat()

    log.info(f"  doctoralia: {len(doct)} | reps match: {len(reps)} | web: {len(web)}")

    filas, revision = [], []
    for url, d in doct.items():
        titulo, nombre = split_titulo(d.get("nombre_raw", ""))
        m = reps.get(url, {})
        conf = m.get("confianza", "sin_match")

        tel_doctoralia = " | ".join(d.get("telefonos") or [])
        tel_reps = m.get("telefono_reps", "") if conf in ("alta", "media") else ""

        email, email_fuente, email_conf = "", "", ""
        if conf in ("alta", "media") and m.get("email"):
            email, email_fuente, email_conf = m["email"], "REPS (MinSalud)", conf
        elif url in web:
            top = web[url]["emails"][0]
            email = top["email"]
            email_fuente = f"Web ({top['fuente'][:60]})"
            email_conf = "media" if top["score"] >= 0.9 else "baja"

        if conf in ("baja", "ambiguo"):
            revision.append({
                "Nombre": nombre,
                "Ciudad": (d.get("ciudad_perfil") or d.get("ciudad", "")),
                "Motivo": ("Homónimos en REPS con emails distintos" if conf == "ambiguo"
                           else "Similitud de nombre insuficiente"),
                "Email_Candidato": m.get("email", "") or " | ".join(m.get("emails_alt", [])),
                "Nombre_En_REPS": m.get("nombre_reps", ""),
                "Municipio_REPS": m.get("municipio_reps", ""),
                "Score": m.get("score", ""),
                "Perfil_URL": url,
            })

        filas.append({
            "Especialidad":      d.get("especialidades") or d.get("especialidad", "Ginecología"),
            "Nombre":            nombre,
            "Correo":            email,
            "Telefono":          tel_reps or tel_doctoralia,
            # ── trazabilidad y contexto ──
            "Titulo":            titulo,
            "Email_Fuente":      email_fuente,
            "Email_Confianza":   email_conf,
            "Telefono_Doctoralia": tel_doctoralia,
            "Telefono_REPS":     tel_reps,
            "Consultorio":       d.get("consultorio", ""),
            "Direccion":         d.get("direccion", ""),
            "Ciudad":            (d.get("ciudad_perfil") or d.get("ciudad", "")).split(" | ")[0],
            "Departamento":      (d.get("departamento", "")).split(" | ")[0],
            "EPS_Aceptadas":     d.get("eps", ""),
            "Teleconsulta":      "Sí" if d.get("teleconsulta") else "",
            "Precio_Consulta":   d.get("precio_min"),
            "Rating":            d.get("rating", ""),
            "Opiniones":         d.get("opiniones", ""),
            "Codigo_REPS":       m.get("codigo_reps", "") if conf in ("alta", "media") else "",
            "Nombre_Legal_REPS": m.get("nombre_reps", "") if conf in ("alta", "media") else "",
            "Perfil_URL":        url,
            "Pais":              "Colombia",
            "Fuente":            "Doctoralia CO + REPS MinSalud",
            "Fecha_Captura":     hoy,
        })

    df = pd.DataFrame(filas)

    # ── Semilla del cliente: manda sobre todo ────────────────────────────────
    seed = load_seed(SEED_PATH)
    if seed and not df.empty:
        idx = {name_key(n): i for i, n in enumerate(df["Nombre"])}
        nuevos = 0
        pisados = 0
        for s in seed:
            k = name_key(s["nombre"])
            target = next((i for kk, i in idx.items() if k and (k <= kk or kk <= k)), None)
            if target is not None:
                if s["email"] and s["email"].lower() != "nan":
                    df.at[target, "Correo"] = s["email"]
                    df.at[target, "Email_Fuente"] = "Semilla cliente"
                    df.at[target, "Email_Confianza"] = "verificado"
                    pisados += 1
                if s["telefono"] and not df.at[target, "Telefono"]:
                    df.at[target, "Telefono"] = s["telefono"]
            else:
                nuevos += 1
                df.loc[len(df)] = {
                    **{c: "" for c in df.columns},
                    "Especialidad": "Ginecología", "Nombre": s["nombre"],
                    "Correo": s["email"] if s["email"].lower() != "nan" else "",
                    "Telefono": s["telefono"],
                    "Email_Fuente": "Semilla cliente", "Email_Confianza": "verificado",
                    "Pais": "Colombia", "Fuente": "Semilla cliente", "Fecha_Captura": hoy,
                }
        log.info(f"  semilla aplicada: {pisados} emails sobrescritos, {nuevos} médicos nuevos")

    df = df.sort_values(["Ciudad", "Nombre"]).reset_index(drop=True)
    return df, pd.DataFrame(revision)


def save(df: pd.DataFrame, revision: pd.DataFrame) -> None:
    con_email = df[df["Correo"].fillna("").str.strip() != ""]
    con_contacto = df[
        (df["Correo"].fillna("").str.strip() != "")
        | (df["Telefono"].fillna("").str.strip() != "")
    ]

    calidad = pd.DataFrame([
        {"Métrica": "Ginecólogos totales", "Valor": len(df)},
        {"Métrica": "Con email", "Valor": len(con_email)},
        {"Métrica": "Con teléfono", "Valor": int((df["Telefono"].fillna("").str.strip() != "").sum())},
        {"Métrica": "Con email + teléfono",
         "Valor": int(((df["Correo"].fillna("").str.strip() != "")
                       & (df["Telefono"].fillna("").str.strip() != "")).sum())},
        {"Métrica": "Con dirección de consultorio",
         "Valor": int((df["Direccion"].fillna("").str.strip() != "").sum())},
        {"Métrica": "Pendientes de revisión manual", "Valor": len(revision)},
    ])

    tmp = OUTPUT_DIR / "_co_final_tmp.xlsx"
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        df.to_excel(w, sheet_name="Todos", index=False)
        con_email.to_excel(w, sheet_name="Con_Email", index=False)
        con_contacto.to_excel(w, sheet_name="Con_Contacto", index=False)
        calidad.to_excel(w, sheet_name="Calidad", index=False)
        (df.groupby("Ciudad").size().reset_index(name="Cantidad")
           .sort_values("Cantidad", ascending=False)
           .to_excel(w, sheet_name="Por_Ciudad", index=False))
        (df.groupby("Departamento").size().reset_index(name="Cantidad")
           .sort_values("Cantidad", ascending=False)
           .to_excel(w, sheet_name="Por_Departamento", index=False))
        (df.groupby(["Email_Fuente", "Email_Confianza"]).size().reset_index(name="Cantidad")
           .sort_values("Cantidad", ascending=False)
           .to_excel(w, sheet_name="Por_Fuente_Email", index=False))
        if not revision.empty:
            revision.to_excel(w, sheet_name="Revision_Manual", index=False)
    shutil.move(str(tmp), str(OUTPUT_XLSX))

    log.info("=== Base Colombia consolidada ===")
    log.info(f"  archivo:        {OUTPUT_XLSX}")
    log.info(f"  ginecólogos:    {len(df)}")
    log.info(f"  con email:      {len(con_email)}  ({len(con_email)/len(df):.1%})")
    log.info(f"  con contacto:   {len(con_contacto)}  ({len(con_contacto)/len(df):.1%})")
    log.info(f"  revisión manual:{len(revision)}")


SEED_PATH: Path | None = None


def main() -> None:
    global SEED_PATH
    p = argparse.ArgumentParser(description="Consolida la base de ginecólogos CO")
    p.add_argument("--seed-csv", type=Path, default=None,
                   help="CSV del cliente (Especialidad;Nombre;Correo;Telefono)")
    args = p.parse_args()
    SEED_PATH = args.seed_csv

    df, revision = build()
    if df.empty:
        log.error("Sin datos.")
        return
    save(df, revision)


if __name__ == "__main__":
    main()
