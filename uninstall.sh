#!/bin/bash
# PowerZoid Sync — Desinstalación
# Uso: bash uninstall.sh

UUID="powerzoid-sync@cleal.cl"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PowerZoid Sync — Desinstalación"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Desactivar extensión
gnome-extensions disable "$UUID" 2>/dev/null || true

# Detener y deshabilitar servicio
systemctl --user stop powerzoid-sync.service 2>/dev/null || true
systemctl --user disable powerzoid-sync.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/powerzoid-sync.service"
systemctl --user daemon-reload

# Eliminar archivos (NO la config ni los logs)
rm -f "$HOME/.local/bin/powerzoid-sync-daemon.py"
rm -rf "$HOME/.local/share/gnome-shell/extensions/$UUID"

echo "  ✓ Desinstalado"
echo ""
echo "  Conservados (eliminar manualmente si quieres):"
echo "  - Config: $HOME/.config/powerzoid-sync/config"
echo "  - Log:    $HOME/.local/share/git-sync/sync.log"
echo "  - Status: $HOME/.local/share/git-sync/status.json"
echo ""
echo "  Para volver a activar git-sync.timer si lo tenías antes:"
echo "  systemctl --user enable --now git-sync.timer"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
