#!/bin/bash
# Configura el cron job para auto_scraper_cr.py
# Ejecutar UNA VEZ: bash scripts/setup_cron_cr.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(which python3)"
LOG="$PROJECT_DIR/logs/scraper_cr.log"

CRON_LINE="15 3 * * * cd \"$PROJECT_DIR\" && $PYTHON scripts/auto_scraper_cr.py --pages-per-run 50 >> \"$LOG\" 2>&1"

echo "Configurando cron job Costa Rica..."
echo "Proyecto: $PROJECT_DIR"
echo "Python:   $PYTHON"
echo "Log:      $LOG"
echo ""

# Agregar al crontab si no existe ya
(crontab -l 2>/dev/null | grep -v "auto_scraper_cr"; echo "$CRON_LINE") | crontab -

echo "✅ Cron CR configurado. Se ejecutará todos los días a las 3:15 AM."
echo ""
echo "Para verificar: crontab -l"
echo "Para ver logs:  tail -f \"$LOG\""
echo "Para correr ahora manualmente:"
echo "  cd \"$PROJECT_DIR\" && python3 scripts/auto_scraper_cr.py --pages-per-run 50"
echo ""
echo "Estado actual: todas las ciudades CR ya scrapeadas (273 médicos en v1.xlsx)"
echo "El cron detectará médicos nuevos que aparezcan en Cliniweb CR."
