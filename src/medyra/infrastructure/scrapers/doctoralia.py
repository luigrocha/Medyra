"""Doctoralia adapter (CR, PA, EC, MX, ...).

Doctoralia publica perfiles **autopublicados por el médico** y los expone
en URLs estables del tipo:
    https://www.doctoralia.com.{tld}/medico/{slug}

Datos extraíbles del perfil (vía CSS sin JS):
    - nombre, especialidad
    - clínica(s) con dirección y teléfono
    - email (raro, frecuentemente oculto detrás de "Mostrar")
    - educación / experiencia (texto libre — candidato a LLM)

Nota legal: los perfiles son self-published y públicos. Respetamos
robots.txt y rate limit conservador. Si Doctoralia introduce un challenge
anti-bot, `base.py` lo detecta y pausa el job.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

from selectolax.parser import HTMLParser
from unidecode import unidecode

from medyra.infrastructure.scrapers.base import BaseScraper, FetchResult

# Mapeo país → TLD oficial de Doctoralia (verificar antes de habilitar otros).
TLD_BY_COUNTRY: dict[str, str] = {
    "EC": "com.ec",
    "CR": "co.cr",
    "PA": "com.pa",
    "MX": "com.mx",
    "CO": "com.co",
    "AR": "com.ar",
    "PE": "com.pe",
    "CL": "cl",
    "ES": "es",
}


@dataclass
class DoctoraliaProfile:
    """Datos extraídos de una página de perfil de Doctoralia."""
    url: str
    name: str | None = None
    specialty: str | None = None
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    clinics: list[dict] = field(default_factory=list)
    bio_text: str | None = None
    raw_html_len: int = 0


def _slug(s: str) -> str:
    """Slug 'Alfredo Tomalá' → 'alfredo-tomala' al estilo Doctoralia."""
    s = unidecode(s).lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


class DoctoraliaScraper(BaseScraper):
    name = "doctoralia"
    min_interval_seconds = 3.0  # conservador para no irritar al host

    def __init__(self, country: str, **kw) -> None:
        tld = TLD_BY_COUNTRY.get(country)
        if not tld:
            raise ValueError(f"Doctoralia TLD desconocido para país {country!r}")
        self.country = country
        self.tld = tld
        self.base_url = f"https://www.doctoralia.{tld}"
        super().__init__(**kw)

    def candidate_urls(self, given: str, family: str) -> list[str]:
        """Hipótesis de URL de perfil. Devuelve varias para probar en orden."""
        given_slug = _slug(given)
        family_slug = _slug(family)
        first = given_slug.split("-")[0] if given_slug else ""
        last_main = family_slug.split("-")[0] if family_slug else ""
        candidates = [
            f"{self.base_url}/medico/{given_slug}-{family_slug}",
            f"{self.base_url}/medico/{first}-{family_slug}",
            f"{self.base_url}/medico/{first}-{last_main}",
        ]
        # dedupe preservando orden
        seen: set[str] = set()
        out: list[str] = []
        for u in candidates:
            if u in seen:
                continue
            seen.add(u)
            out.append(u)
        return out

    def search_url(self, query: str) -> str:
        """URL de buscador interno de Doctoralia."""
        return f"{self.base_url}/buscar?q={query.replace(' ', '+')}"

    def parse_profile(self, fr: FetchResult) -> DoctoraliaProfile | None:
        """Extrae datos estructurados desde HTML de perfil. None si no es perfil válido."""
        if fr.status != 200 or not fr.html:
            return None
        tree = HTMLParser(fr.html)
        prof = DoctoraliaProfile(url=fr.url, raw_html_len=len(fr.html))

        # Nombre: h1 con itemprop="name" o data-id="doctor-name"
        name_node = tree.css_first('h1[itemprop="name"]') or tree.css_first('[data-id="doctor-name"]')
        if name_node:
            prof.name = name_node.text(strip=True)

        # Especialidad: link a /especialidad/...
        spec_node = tree.css_first('a[href*="/especialidad/"], [itemprop="medicalSpecialty"]')
        if spec_node:
            prof.specialty = spec_node.text(strip=True)

        # Teléfonos: itemprop="telephone" o data-phone-id
        for node in tree.css('[itemprop="telephone"], [data-phone-id], a[href^="tel:"]'):
            href = node.attributes.get("href", "")
            text = node.text(strip=True)
            phone = href.removeprefix("tel:").strip() if href else text
            if phone and len(phone) >= 7:
                prof.phones.append(phone)

        # Emails: mailto links (raros pero existen)
        for node in tree.css('a[href^="mailto:"]'):
            email = (node.attributes.get("href", "") or "").removeprefix("mailto:").strip()
            if email:
                prof.emails.append(email)

        # Clínicas: bloques itemtype="MedicalOrganization" o data-id="address"
        for node in tree.css('[itemtype*="MedicalOrganization"], [data-id="address"], .address'):
            text = node.text(strip=True, separator=" | ")
            if text:
                prof.clinics.append({"raw": text})

        # Bio: descripción larga (candidata a LLM extractor)
        bio_node = tree.css_first('[itemprop="description"], .doctor-description')
        if bio_node:
            prof.bio_text = bio_node.text(strip=True)

        # Si no encontramos NI nombre NI especialidad, probablemente no es perfil.
        if not prof.name and not prof.specialty:
            return None

        # Dedupe simple
        prof.phones = list(dict.fromkeys(prof.phones))
        prof.emails = list(dict.fromkeys(prof.emails))
        return prof

    def is_profile_url(self, url: str) -> bool:
        path = urlparse(url).path
        return path.startswith("/medico/") and path.count("/") >= 2
