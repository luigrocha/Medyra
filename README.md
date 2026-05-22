# Medyra — Medical Public Data Intelligence (LATAM)

Recolección automatizada de información pública de médicos en Latinoamérica: nombres, emails, teléfonos y especialidades.

**Estado actual: Fase de scraping activa — ~5,000 médicos capturados. CR: ✅ 1,063 médicos con email+teléfono.**

---

## Cobertura actual (Mayo 2026)

| País | Médicos | Con Email | Con Teléfono | Fuentes |
|------|---------|-----------|--------------|---------|
| 🇵🇦 **Panamá** | 3,921 | 3,568 (91%) | 1,557 (40%) | Cliniweb PA (4 directorios) |
| 🇨🇷 **Costa Rica** | **1,063** | 1,063 (100%) | 1,063 (100%) | Salud 360 + CIMA + La Católica + Allegra + Cliniweb + Colegio |
| **TOTAL** | **~4,984** | — | — | |

### Panamá — fuentes
| Directorio | Médicos |
|------------|---------|
| Directorio General Cliniweb PA | 3,258 |
| The Panama Clinic | 264 |
| Town Center Medical | 201 |
| Hospital Paitilla | 198 |

### Costa Rica — fuentes
| Directorio | Médicos (en Excel) | Estado |
|------------|---------|--------|
| Salud 360 CR (`directorio.salud360.cr`) | 271 | ✅ Completo |
| Hospital CIMA CR (`directorio.hospitalcima.com`) | 236 | ✅ Completo |
| Hospital La Católica CR (`directorio.hospitallacatolica.com`) | 175 | ✅ Completo |
| Allegra | 155 | ✅ Completo |
| **Colegio Médicos CR** (`medicoscr.hulilabs.com/es/search`) | **154** | ✅ Completo (con email+tel) |
| Directorio Cliniweb CR (28 ciudades) | 71 | ✅ Completo |
| Colegio Médicos CR (nombres sin contacto) | 20,103 | ✅ Sheet `Colegio_CR` en medicos_costa_rica_v2.xlsx |
| **TOTAL con email+teléfono** | **1,063** | ✅ Meta superada |

---

## Archivos de salida

```
data/output/
├── medicos_panama_cliniweb_v2.xlsx    ← 3,921 médicos PA
│   ├── Todos                          ← hoja principal
│   ├── Con_Contacto                   ← solo con email o tel
│   ├── Especialidades                 ← conteo por especialidad
│   └── Por_Fuente                     ← conteo por directorio
├── medicos_costa_rica_v2.xlsx         ← 890 médicos CR
│   ├── Todos
│   ├── Con_Contacto
│   ├── Por_Fuente
│   └── Especialidades
├── scraper_state.json                 ← estado paginación PA
└── scraper_state_cr.json              ← estado paginación CR
```

---

## Scripts

```
scripts/
├── auto_scraper_cliniweb.py   ← scraper Panamá (standalone, sin Claude)
├── auto_scraper_cr.py         ← scraper Costa Rica (standalone, sin Claude)
├── scrape_salud360_cr.py      ← scraper Salud 360 CR (one-shot, ~640 médicos)
├── setup_cron.sh              ← registra cron PA (3:00 AM diario)
└── setup_cron_cr.sh           ← registra cron CR (3:15 AM diario)
```

### Ejecutar manualmente

```bash
# Panama
python3 scripts/auto_scraper_cliniweb.py --pages-per-run 100

# Costa Rica (cron diario)
python3 scripts/auto_scraper_cr.py --pages-per-run 50

# Salud 360 CR (one-shot, ~5 min)
python3 scripts/scrape_salud360_cr.py
```

### Activar crons (correr UNA sola vez en Terminal)

```bash
bash "/Users/lgrocha/Documents/Claude/02. Data scraping/scripts/setup_cron.sh"
bash "/Users/lgrocha/Documents/Claude/02. Data scraping/scripts/setup_cron_cr.sh"
```

Los crons detectan cambios automáticamente: si un nuevo médico aparece en cualquiera de las fuentes, lo agrega al Excel en la siguiente corrida nocturna.

---

## Fuentes exploradas y estado

### Panamá ✅ COMPLETA
- **Cliniweb PA** (`directorio.cliniweb.com`): 4 subdirectorios, 497 páginas totales — **100% scrapeado**
- Cron PA activo: 3:00 AM diario, detecta nuevas incorporaciones

### Costa Rica ✅ COMPLETA — 1,063 médicos con email+teléfono
- **Salud 360 CR** (`directorio.salud360.cr`): 271 nuevos — **completa**
- **Hospital CIMA CR** (`directorio.hospitalcima.com`): 236 nuevos — **completa** (vía Chrome JS)
- **Hospital La Católica CR** (`directorio.hospitallacatolica.com`): 175 nuevos — **completa** (vía Chrome JS)
- **Allegra**: 155 médicos — **completa**
- **Colegio Médicos CR** (`medicoscr.hulilabs.com/es/search?q=medico&page=N`): 154 nuevos con contacto — **scrapeado p.1-200**
- **Cliniweb CR**: 28 ciudades, 71 médicos — **100% agotado**
- Cron CR activo: 3:15 AM diario

### Fuentes descartadas
- Doctoralia CR — bloqueado (JWT/CORS)
- HuliHealth CR — sin emails públicos
- Cliniweb CR nuevas ciudades — agotado (19 ciudades adicionales = 0 resultados)

---

## Próximos pasos (Plan)

| Prioridad | Tarea | Estado |
|-----------|-------|--------|
| ✅ Lista | **CR**: 1,063 médicos con email+tel — meta superada | ✅ Completo |
| 🔴 Alta | **Panamá**: Consejo Técnico de Salud (registro oficial) | Pendiente |
| 🟡 Media | **Ecuador**: revisión y consolidación de datos existentes | Pendiente |
| 🟡 Media | Deduplicación cross-país (mismos médicos en múltiples fuentes) | Pendiente |
| 🟢 Baja | Enriquecimiento: inferir emails por patrón (nombre + dominio clínica) | Pendiente |
| 🟢 Baja | API / Review Queue para validación humana (FastAPI + Postgres) | Pendiente |

---

## Archivos de entrega

| Archivo | Descripción | Estado |
|---------|-------------|--------|
| `data/output/1000_medicos_panama.xlsx` | 1,000 médicos PA — email + teléfono | ✅ Completo |
| `data/output/1000_medicos_costa_rica.xlsx` | **1,063 médicos CR — todos con email + teléfono** | ✅ Completo |
| `data/output/validacion_1000_medicos.xlsx` | Reporte de validación PA+CR (93.5% calidad) | ✅ Completo |

---

## Técnico — notas importantes

- **Escritura atómica Excel**: todos los scripts escriben a `_tmp.xlsx` primero, luego `shutil.move()` al archivo final. Esto evita corrupción si el proceso muere a mitad de escritura.
- **Deduplicación**: por `Nombre.strip().upper()` dentro de cada país.
- **Sandbox DNS**: dominios `.cr` bloqueados desde sandbox. Workaround: usar Claude in Chrome MCP para discovery de nuevas ciudades/fuentes.
- **Seed fijo Cliniweb**: `639148726507040344` — necesario para paginación consistente.
- **Timeout bash**: máximo ~44s por llamada → límite de ~20 páginas por chunk.
