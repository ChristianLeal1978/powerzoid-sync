/**
 * PowerZoid Sync — GNOME Shell Extension
 * UUID: powerzoid-sync@cleal.cl
 *
 * Clic izquierdo  → menú de estado (repos, errores, alineación, sync manual)
 * Clic derecho    → abre el mismo menú (alineación accesible desde ambos)
 */

import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import St from 'gi://St';
import Clutter from 'gi://Clutter';
import Soup from 'gi://Soup';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import { Extension } from 'resource:///org/gnome/shell/extensions/extension.js';

const DAEMON_URL   = 'http://127.0.0.1:6790';
const POLL_MS      = 5000;
const SPIN_MS      = 100;
const SPINNER      = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
const VALID_ALIGNS = ['left', 'center', 'right'];

const COLOR = {
    green: '#4caf50',
    amber: '#ff9800',
    red:   '#f44336',
    grey:  '#9e9e9e',
};

const CONFIG_PATH = `${GLib.get_home_dir()}/.config/powerzoid-sync/config`;


// ─────────────────────────────────────────────────────────────────────────────
// Indicador en el panel
// ─────────────────────────────────────────────────────────────────────────────

const SyncIndicator = GObject.registerClass(
class SyncIndicator extends PanelMenu.Button {

    _init(extension, initialAlign = 'left') {
        super._init(0.0, 'PowerZoid Sync');

        this._ext          = extension;
        this._currentAlign = initialAlign;
        this._session      = new Soup.Session();
        this._session.timeout = 3;

        // ── Panel ────────────────────────────────────────────────────────
        this._dot = new St.Label({
            text:    '⠿',
            y_align: Clutter.ActorAlign.CENTER,
            style:   `color: ${COLOR.grey};`,
        });
        this._dot.translation_y = 2;  // Braille sube por su celda tipográfica; lo bajamos
        const wordLabel = new St.Label({
            text:    ' Sync',
            y_align: Clutter.ActorAlign.CENTER,
            style:   'font-size: 0.85em;',
        });
        const box = new St.BoxLayout({ style_class: 'panel-status-menu-box' });
        box.add_child(this._dot);
        box.add_child(wordLabel);
        this.add_child(box);

        // ── Clic derecho: abrir el mismo menú ────────────────────────────
        // PanelMenu solo abre en clic izquierdo; hacemos que el derecho
        // también lo abra para que las opciones de alineación sean accesibles.
        this.connect('button-release-event', (_actor, ev) => {
            if (ev.get_button() === 3) {
                if (!this.menu.isOpen)
                    this.menu.open();
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });

        // ── Menú (compartido por clic izquierdo y derecho) ───────────────
        this._buildMenu();

        // ── Estado interno ────────────────────────────────────────────────
        this._state        = 'unknown';
        this._spinnerIdx   = 0;
        this._spinnerTimer = null;
        this._pollTimer    = null;

        this._startPolling();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Construcción del menú
    // ─────────────────────────────────────────────────────────────────────

    _buildMenu() {
        // Título
        const titleItem = new PopupMenu.PopupMenuItem('PowerZoid Sync', { reactive: false });
        titleItem.label.set_style('font-weight: bold;');
        this.menu.addMenuItem(titleItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Estado de sincronización
        this._statusItem = new PopupMenu.PopupMenuItem('Conectando...', { reactive: false });
        this.menu.addMenuItem(this._statusItem);

        this._lastSyncItem = new PopupMenu.PopupMenuItem('', { reactive: false });
        this._lastSyncItem.visible = false;
        this.menu.addMenuItem(this._lastSyncItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Información de repos (dinámica)
        this._infoSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._infoSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Acciones
        const syncNow = new PopupMenu.PopupMenuItem('↻  Sincronizar ahora');
        syncNow.connect('activate', () => this._triggerSync());
        this.menu.addMenuItem(syncNow);

        const openLog = new PopupMenu.PopupMenuItem('📋  Ver log');
        openLog.connect('activate', () => this._openLog());
        this.menu.addMenuItem(openLog);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // ── Submenú de alineación ─────────────────────────────────────────
        this._posSubMenu = new PopupMenu.PopupSubMenuMenuItem('Posición en barra');
        this.menu.addMenuItem(this._posSubMenu);

        this._alignItems = {};
        [
            ['← Alinear a la izquierda', 'left'],
            ['↔ Alinear al centro',       'center'],
            ['→ Alinear a la derecha',    'right'],
        ].forEach(([label, align]) => {
            const item = new PopupMenu.PopupMenuItem(label);
            this._alignItems[align] = item;
            item.connect('activate', () => this._setAlignment(align));
            this._posSubMenu.menu.addMenuItem(item);
        });

        this._updateAlignMarks(this._currentAlign);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Configuración
        const prefsItem = new PopupMenu.PopupMenuItem('⚙  Configuración…');
        prefsItem.connect('activate', () => this._ext.openPreferences());
        this.menu.addMenuItem(prefsItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        const hideItem = new PopupMenu.PopupMenuItem('🙈  Ocultar esta sesión');
        hideItem.connect('activate', () => this._hideForSession());
        this.menu.addMenuItem(hideItem);
    }

    // Oculta el indicador solo en memoria (sin tocar la config en disco):
    // vuelve a aparecer normalmente en el próximo inicio de sesión.
    _hideForSession() {
        this.menu.close();
        this.hide();
    }

    // ─────────────────────────────────────────────────────────────────────
    // Alineación
    // ─────────────────────────────────────────────────────────────────────

    _setAlignment(align) {
        if (align === this._currentAlign) return;

        // Sacar del box actual
        const parent = this.get_parent();
        if (parent) parent.remove_child(this);

        // Insertar directamente en el box destino con insert_child_at_index.
        // Usamos esto en lugar de addToStatusArea para evitar que el menú
        // se registre dos veces en el menuManager (lo que lo hace cerrarse solo).
        //
        // Posiciones:
        //   left   → 1  (después del primer ítem, típicamente Workspaces/Activities)
        //   center → 0  (antes del reloj)
        //   right  → 0  (antes de los indicadores del sistema)
        const positions = { left: 1, center: 0, right: 0 };
        const boxes = {
            left:   Main.panel._leftBox,
            center: Main.panel._centerBox,
            right:  Main.panel._rightBox,
        };
        const targetBox = boxes[align];
        const idx       = positions[align] ?? 0;
        if (targetBox) targetBox.insert_child_at_index(this, idx);

        this._currentAlign = align;
        this._updateAlignMarks(align);
        this._ext.saveAlignment(align);
    }

    _updateAlignMarks(activeAlign) {
        Object.entries(this._alignItems).forEach(([align, item]) => {
            item.setOrnament(
                align === activeAlign
                    ? PopupMenu.Ornament.DOT
                    : PopupMenu.Ornament.NONE
            );
        });
    }

    // ─────────────────────────────────────────────────────────────────────
    // Polling al daemon
    // ─────────────────────────────────────────────────────────────────────

    _startPolling() {
        this._fetchStatus();
        this._pollTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, POLL_MS, () => {
            this._fetchStatus();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _fetchStatus() {
        let msg;
        try { msg = Soup.Message.new('GET', `${DAEMON_URL}/status`); }
        catch (_e) { this._applyDisconnected(); return; }

        this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, null,
            (session, result) => {
                try {
                    const bytes = session.send_and_read_finish(result);
                    const data  = JSON.parse(new TextDecoder().decode(bytes.get_data()));
                    this._applyStatus(data);
                } catch (_e) {
                    this._applyDisconnected();
                }
            }
        );
    }

    // ─────────────────────────────────────────────────────────────────────
    // Actualización de UI
    // ─────────────────────────────────────────────────────────────────────

    _applyStatus(status) {
        const s = status.state || 'unknown';

        if (s !== this._state) {
            this._state = s;
            if (s === 'syncing') this._startSpinner();
            else { this._stopSpinner(); this._setDot(s); }
        }

        const labels = {
            idle:    '✅  Sincronizado',
            syncing: '↻   Sincronizando...',
            error:   '❌  Error de sincronización',
            unknown: '❓  Estado desconocido',
        };
        this._statusItem.label.set_text(labels[s] || s);

        if (status.last_sync) {
            this._lastSyncItem.label.set_text(`Última sync: ${status.last_sync}`);
            this._lastSyncItem.visible = true;
        }

        this._rebuildInfo(status);
    }

    _applyDisconnected() {
        if (this._state === 'disconnected') return;
        this._state = 'disconnected';
        this._stopSpinner();
        this._setDot('unknown');
        this._statusItem.label.set_text('⚠  Daemon no disponible');
        this._infoSection.removeAll();
    }

    _rebuildInfo(status) {
        this._infoSection.removeAll();

        const repos    = status.repos       || {};
        const errors   = status.errors      || [];
        const noGit    = status.no_git      || [];
        const noRemote = status.no_remote   || [];
        const ghOnly   = status.github_only || [];

        const okCount    = Object.values(repos).filter(r => r.state === 'ok').length;
        const cloneCount = Object.values(repos).filter(r => r.state === 'cloned').length;

        if (okCount > 0 || cloneCount > 0) {
            let txt = `📊 ${okCount} repo${okCount !== 1 ? 's' : ''} sincronizado${okCount !== 1 ? 's' : ''}`;
            if (cloneCount > 0) txt += ` · ${cloneCount} clonado${cloneCount !== 1 ? 's' : ''}`;
            this._addInfo(txt);
        }

        if (errors.length > 0) {
            this._addInfo(`❌ Errores (${errors.length}):`, true);
            errors.slice(0, 6).forEach(e => this._addInfo(`     ${e}`));
            if (errors.length > 6) this._addInfo(`     … y ${errors.length - 6} más`);
        }

        if (noRemote.length > 0) {
            this._addInfo(`⚠  Sin remoto Git (${noRemote.length}):`, true);
            noRemote.slice(0, 5).forEach(n => this._addInfo(`     ${n}`));
        }

        if (noGit.length > 0)
            this._addInfo(`📁 Sin Git (${noGit.length})`, true);

        if (ghOnly.length > 0) {
            this._addInfo(`📥 No clonados de GitHub (${ghOnly.length}):`, true);
            ghOnly.forEach(n => this._addInfo(`     ${n}`));
        }

        const envSynced = status.env_synced || [];
        if (envSynced.length > 0)
            this._addInfo(`🔐 .env.local fusionados (${envSynced.length}): ${envSynced.join(', ')}`);
    }

    _addInfo(text, bold = false) {
        const item = new PopupMenu.PopupMenuItem(text, { reactive: false });
        if (bold) item.label.set_style('font-weight: bold;');
        this._infoSection.addMenuItem(item);
    }

    // ─────────────────────────────────────────────────────────────────────
    // Punto de color y spinner
    // ─────────────────────────────────────────────────────────────────────

    _setDot(state) {
        const c = { idle: COLOR.green, error: COLOR.red }[state] || COLOR.grey;
        this._dot.set_text('⠿');
        this._dot.set_style(`color: ${c};`);
    }

    _startSpinner() {
        if (this._spinnerTimer) return;
        this._spinnerIdx   = 0;
        this._spinnerTimer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SPIN_MS, () => {
            this._spinnerIdx = (this._spinnerIdx + 1) % SPINNER.length;
            this._dot.set_text(SPINNER[this._spinnerIdx]);
            this._dot.set_style(`color: ${COLOR.amber};`);
            return GLib.SOURCE_CONTINUE;
        });
    }

    _stopSpinner() {
        if (!this._spinnerTimer) return;
        GLib.source_remove(this._spinnerTimer);
        this._spinnerTimer = null;
    }

    // ─────────────────────────────────────────────────────────────────────
    // Acciones
    // ─────────────────────────────────────────────────────────────────────

    _triggerSync() {
        let msg;
        try { msg = Soup.Message.new('POST', `${DAEMON_URL}/sync`); }
        catch (_e) { return; }
        this._session.send_and_read_async(msg, GLib.PRIORITY_DEFAULT, null,
            (session, result) => { try { session.send_and_read_finish(result); } catch (_e) {} }
        );
    }

    _openLog() {
        const logPath = `${GLib.get_home_dir()}/.local/share/git-sync/sync.log`;
        try {
            Gio.AppInfo.launch_default_for_uri(`file://${logPath}`, null);
        } catch (_e) {
            try {
                GLib.spawn_command_line_async(
                    `bash -c "gnome-terminal -- bash -c 'tail -80 ${logPath}; read'"`
                );
            } catch (_e2) {}
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Limpieza
    // ─────────────────────────────────────────────────────────────────────

    destroy() {
        if (this._pollTimer) { GLib.source_remove(this._pollTimer); this._pollTimer = null; }
        this._stopSpinner();
        super.destroy();
    }
});


// ─────────────────────────────────────────────────────────────────────────────
// Extension
// ─────────────────────────────────────────────────────────────────────────────

export default class PowerZoidSyncExtension extends Extension {

    enable() {
        const align = this._loadAlignment();
        this._indicator = new SyncIndicator(this, align);
        Main.panel.addToStatusArea('powerzoid-sync', this._indicator, -1, align);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }

    _loadAlignment() {
        try {
            const file = Gio.File.new_for_path(CONFIG_PATH);
            const [, bytes] = file.load_contents(null);
            const text = new TextDecoder().decode(bytes);
            for (const raw of text.split('\n')) {
                const line = raw.trim();
                if (line.startsWith('alignment=')) {
                    const val = line.slice(10).trim();
                    return VALID_ALIGNS.includes(val) ? val : 'left';
                }
            }
        } catch (_e) {}
        return 'left';
    }

    saveAlignment(align) {
        try {
            const file = Gio.File.new_for_path(CONFIG_PATH);
            let existing = '';
            try {
                const [, bytes] = file.load_contents(null);
                existing = new TextDecoder().decode(bytes);
            } catch (_e) {}

            const lines = existing.split('\n')
                .filter(l => !l.trim().startsWith('alignment='));
            lines.push(`alignment=${align}`);

            file.replace_contents(
                new TextEncoder().encode(lines.join('\n')),
                null, false,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                null
            );
        } catch (_e) {}
    }
}
