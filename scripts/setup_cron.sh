#!/bin/bash
# Configura el cron job para auto_scraper_cliniweb.py
# Ejecutar UNA VEZ: bash scripts/setup_cron.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON="$(which python3)"
LOG="$PROJECT_DIR/logs/scraper.log"

CRON_LINE="0 3 * * * cd \"$PROJECT_DIR\" && $PYTHON scripts/auto_scraper_cliniweb.py --pages-per-run 100 >> \"$LOG\" 2>&1"

echo "Configurando cron job..."
echo "Proyecto: $PROJECT_DIR"
echo "Python:   $PYTHON"
echo "Log:      $LOG"
echo ""

# Agregar al crontab si no existe ya
(crontab -l 2>/dev/null | grep -v "auto_scraper_cliniweb"; echo "$CRON_LINE") | crontab -

echo "✅ Cron configurado. Se ejecutará todos los días a las 3:00 AM."
echo ""
echo "Para verificar: crontab -l"
echo "Para ver logs:  tail -f \"$LOG\""
echo "Para correr ahora manualmente:"
echo "  cd \"$PROJECT_DIR\" && python3 scripts/auto_scraper_cliniweb.py --pages-per-run 100"
echo ""
echo "Páginas pendientes del directorio general: ~335 páginas (≈2,659 médicos más)"
echo "Con 100 páginas/corrida se completa en ~4 noches."
