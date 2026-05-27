#!/usr/bin/env bash
# Wrapper para gestionar los launchd plists del bot y del dashboard.
#
# Bot (por defecto):
#   scripts/launchd.sh install | uninstall | restart | status | logs | launchd-logs
# Dashboard (prefijo 'dashboard'):
#   scripts/launchd.sh dashboard install | uninstall | restart | status | logs
#
# Nota: 'restart' sin prefijo reinicia SOLO el bot (lo usa el dashboard vía
# /api/restart). El dashboard se gestiona aparte con el prefijo 'dashboard'.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Selección de servicio: "dashboard <verb>" apunta al panel; si no, al bot.
if [[ "${1:-}" == "dashboard" ]]; then
  LABEL="com.seedtag.csv-dashboard"
  LOGFILE="$ROOT/logs/dashboard-stderr.log"
  shift
else
  LABEL="com.seedtag.csv-automation"
  LOGFILE="$ROOT/logs/automation.log"
fi
PLIST_NAME="$LABEL.plist"
PLIST_SRC="$ROOT/deploy/launchd/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"

case "${1:-}" in
  install)
    if [[ ! -f "$PLIST_SRC" ]]; then
      echo "ERROR: no encuentro $PLIST_SRC" >&2; exit 1
    fi
    mkdir -p "$(dirname "$PLIST_DST")"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "✓ Cargado ($LABEL). Estado:"
    launchctl list | grep "$LABEL" || echo "(no aparece — ver logs/${LABEL##*.}-stderr.log)"
    ;;

  uninstall)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      rm "$PLIST_DST"
      echo "✓ Descargado y borrado ($LABEL)"
    else
      echo "(no estaba instalado: $LABEL)"
    fi
    ;;

  restart)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST"
    fi
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "✓ Reload completo ($LABEL). Estado:"
    launchctl list | grep "$LABEL" || echo "(no aparece en la lista)"
    ;;

  status)
    line=$(launchctl list | grep "$LABEL" || true)
    if [[ -z "$line" ]]; then
      echo "✗ No cargado en launchd ($LABEL)"
      exit 1
    fi
    pid=$(echo "$line" | awk '{print $1}')
    status=$(echo "$line" | awk '{print $2}')
    if [[ "$pid" == "-" ]]; then
      echo "⚠ Cargado pero NO corriendo ($LABEL, último exit: $status)"
    else
      echo "✓ PID $pid corriendo ($LABEL, último exit: $status)"
    fi
    ;;

  logs)
    tail -f "$LOGFILE"
    ;;

  launchd-logs)
    tail -f "$ROOT/logs/launchd-stderr.log"
    ;;

  *)
    echo "Uso: $0 [dashboard] {install|uninstall|restart|status|logs|launchd-logs}" >&2
    exit 1
    ;;
esac
