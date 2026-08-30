#!/bin/bash
# PowerZoid Sync — Script de instalación
# Uso: bash install.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
UUID="powerzoid-sync@cleal.cl"
EXT_DIR="$HOME/.local/share/gnome-shell/extensions/$UUID"
BIN_DIR="$HOME/.local/bin"
SYSTEMD_DIR="$HOME/.config/systemd/user"
CONFIG_DIR="$HOME/.config/powerzoid-sync"
LOG_DIR="$HOME/.local/share/git-sync"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  PowerZoid Sync — Instalación"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# [1] Directorios
echo "[1/5] Creando directorios..."
mkdir -p "$BIN_DIR" "$SYSTEMD_DIR" "$CONFIG_DIR" "$LOG_DIR" "$HOME/Proyectos"

# [2] Daemon
echo "[2/5] Instalando daemon..."
cp "$SCRIPT_DIR/daemon/powerzoid-sync-daemon.py" "$BIN_DIR/powerzoid-sync-daemon.py"
chmod +x "$BIN_DIR/powerzoid-sync-daemon.py"

# [3] Systemd: desactivar git-sync.timer si existe (el daemon lo reemplaza)
echo "[3/5] Configurando systemd..."
if systemctl --user list-unit-files git-sync.timer &>/dev/null 2>&1; then
    if systemctl --user is-active git-sync.timer &>/dev/null; then
        echo "  → Deteniendo git-sync.timer (reemplazado por powerzoid-sync)..."
        systemctl --user stop git-sync.timer 2>/dev/null || true
        systemctl --user disable git-sync.timer 2>/dev/null || true
    fi
fi

cp "$SCRIPT_DIR/systemd/powerzoid-sync.service" "$SYSTEMD_DIR/"
systemctl --user daemon-reload
systemctl --user enable powerzoid-sync.service
systemctl --user restart powerzoid-sync.service

# [4] Extensión GNOME
echo "[4/5] Instalando extensión GNOME Shell..."
mkdir -p "$EXT_DIR"
cp -r "$SCRIPT_DIR/extension/$UUID/." "$EXT_DIR/"

# [5] Activar extensión
echo "[5/5] Activando extensión..."
if gnome-extensions enable "$UUID" 2>/dev/null; then
    echo "  → Extensión activada"
else
    echo "  → La extensión se activará en el próximo inicio de sesión (Wayland)"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ Instalación completada"
echo ""
echo "  Próximos pasos:"
echo "  1. Cierra sesión y vuelve a entrar (Wayland)"
echo "  2. Haz CLIC DERECHO en '● Sync' en la barra"
echo "     → Ingresa tu carpeta y el GitHub Token"
echo "  3. La primera sync ocurre 2 min tras iniciar sesión"
echo ""
echo "  Estado del daemon:"
systemctl --user status powerzoid-sync.service --no-pager -n 5 2>/dev/null || true
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
