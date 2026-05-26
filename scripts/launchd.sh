#!/usr/bin/env bash
# Wrapper para gestionar el launchd plist del bot CSV automation.
#
# Uso:
#   scripts/launchd.sh install     # copia plist a ~/Library/LaunchAgents/ y lo carga
#   scripts/launchd.sh uninstall   # descarga y borra
#   scripts/launchd.sh restart     # reload completo (unload + load)
#   scripts/launchd.sh status      # muestra PID + ultimo exit
#   scripts/launchd.sh logs        # tail -f de logs/automation.log
#   scripts/launchd.sh launchd-logs# tail -f de logs/launchd-stderr.log (cuando crashea)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.seedtag.csv-automation.plist"
PLIST_SRC="$ROOT/deploy/launchd/$PLIST_NAME"
PLIST_DST="$HOME/Library/LaunchAgents/$PLIST_NAME"
LABEL="com.seedtag.csv-automation"

case "${1:-}" in
  install)
    if [[ ! -f "$PLIST_SRC" ]]; then
      echo "ERROR: no encuentro $PLIST_SRC" >&2; exit 1
    fi
    mkdir -p "$(dirname "$PLIST_DST")"
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "✓ Cargado. Estado:"
    launchctl list | grep "$LABEL" || echo "(no aparece en la lista — ver logs/launchd-stderr.log)"
    ;;

  uninstall)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST" 2>/dev/null || true
      rm "$PLIST_DST"
      echo "✓ Descargado y borrado de ~/Library/LaunchAgents/"
    else
      echo "(no estaba instalado)"
    fi
    ;;

  restart)
    if [[ -f "$PLIST_DST" ]]; then
      launchctl unload "$PLIST_DST"
    fi
    cp "$PLIST_SRC" "$PLIST_DST"
    launchctl load "$PLIST_DST"
    echo "✓ Reload completo. Estado:"
    launchctl list | grep "$LABEL" || echo "(no aparece en la lista)"
    ;;

  status)
    line=$(launchctl list | grep "$LABEL" || true)
    if [[ -z "$line" ]]; then
      echo "✗ No cargado en launchd"
      exit 1
    fi
    pid=$(echo "$line" | awk '{print $1}')
    status=$(echo "$line" | awk '{print $2}')
    if [[ "$pid" == "-" ]]; then
      echo "⚠ Cargado pero NO corriendo (último exit code: $status). Ver logs/launchd-stderr.log"
    else
      echo "✓ PID $pid corriendo (último exit code: $status)"
    fi
    ;;

  logs)
    tail -f "$ROOT/logs/automation.log"
    ;;

  launchd-logs)
    tail -f "$ROOT/logs/launchd-stderr.log"
    ;;

  *)
    echo "Uso: $0 {install|uninstall|restart|status|logs|launchd-logs}" >&2
    exit 1
    ;;
esac
