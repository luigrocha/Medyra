# Medyra — Medical Public Data Intelligence (LATAM)

Recolección automatizada de información pública de médicos en Latinoamérica: nombres, emails, teléfonos y especialidades.

**Estado actual: Fase de scraping activa — 4,811 médicos capturados.**

---

## Cobertura actual (Mayo 2026)

| País | Médicos | Con Email | Con Teléfono | Fuentes |
|------|---------|-----------|--------------|---------|
| 🇵🇦 **Panamá** | 3,921 | 3,568 (91%) | 1,557 (40%) | Cliniweb PA (4 directorios) |
| 🇨🇷 **Costa Rica** | 890 | 329 (37%) | 681 (77%) | Cliniweb CR (28 ciudades) + Clínica Bíblica + Allegra |
| **TOTAL** | **4,811** | **3,897 (81%)** | **2,238 (47%)** | |

### Panamá — fuentes
| Directorio | Médicos |
|------------|---------|
| Directorio General Cliniweb PA | 3,258 |
| The Panama Clinic | 264 |
| Town Center Medical | 201 |
| Hospital Paitilla | 198 |

### Costa Rica — fuentes
| Directorio | Médicos |
|------------|---------|
| Clínica Bíblica CR | 447 |
| Directorio Cliniweb CR (28 ciudades) | 254 |
| Allegra | 189 |

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
├── setup_cron.sh              ← registra cron PA (3:00 AM diario)
└── setup_cron_cr.sh           ← registra cron CR (3:15 AM diario)
```

### Ejecutar manualmente

```bash
# Panama
python3 scripts/auto_scraper_cliniweb.py --pages-per-run 100

# Costa Rica
python3 scripts/auto_scraper_cr.py --pages-per-run 50
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

### Costa Rica ✅ ACTIVA
- **Cliniweb CR**: 28 ciudades descubiertas via Chrome JS probing — **100% scrapeado**
  - Ciudades: san-jose, alajuela, puntarenas, limon, escazu, perez-zeledon, pococi, guapiles, palmar, san-ramon, naranjo, san-isidro, coronado, goicoechea, acosta, san-pedro, upala, corredores, santo-domingo, san-francisco, san-vicente, mora, san-marcos, el-carmen, concepcion, costa-rica, y más
- **Clínica Bíblica** (`hcb-middleware-citas.onrender.com/api`): 453 médicos — **completa**
- **Allegra**: 189 médicos — **completa**
- Cron CR activo: 3:15 AM diario

### Fuentes bloqueadas / descartadas
- Colegio de Médicos CR — sin acceso público al directorio de contactos
- Hospital CIMA — WordPress sin datos útiles via API
- Doctoralia CR — pendiente para próxima fase

---

## Próximos pasos (Plan)

| Prioridad | Tarea | Estado |
|-----------|-------|--------|
| 🔴 Alta | **Panamá**: Consejo Técnico de Salud (registro oficial) | Pendiente |
| 🔴 Alta | **CR**: Doctoralia Costa Rica adapter | Pendiente |
| 🟡 Media | **Ecuador**: revisión y consolidación de datos existentes | Pendiente |
| 🟡 Media | Deduplicación cross-país (mismos médicos en múltiples fuentes) | Pendiente |
| 🟢 Baja | Enriquecimiento: inferir emails por patrón (nombre + dominio clínica) | Pendiente |
| 🟢 Baja | API / Review Queue para validación humana (FastAPI + Postgres) | Pendiente |

---

## Técnico — notas importantes

- **Escritura atómica Excel**: todos los scripts escriben a `_tmp.xlsx` primero, luego `shutil.move()` al archivo final. Esto evita corrupción si el proceso muere a mitad de escritura.
- **Deduplicación**: por `Nombre.strip().upper()` dentro de cada país.
- **Sandbox DNS**: dominios `.cr` bloqueados desde sandbox. Workaround: usar Claude in Chrome MCP para discovery de nuevas ciudades/fuentes.
- **Seed fijo Cliniweb**: `639148726507040344` — necesario para paginación consistente.
- **Timeout bash**: máximo ~44s por llamada → límite de ~20 páginas por chunk.
