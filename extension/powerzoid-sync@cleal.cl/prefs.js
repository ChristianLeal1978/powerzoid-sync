/**
 * PowerZoid Sync — Ventana de preferencias (GTK4 / Adwaita)
 * Acceso: clic derecho en el indicador del panel
 *
 * NO usa GSettings. Lee y escribe directamente en:
 *   ~/.config/powerzoid-sync/config
 */

import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';
import { ExtensionPreferences } from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

const CONFIG_PATH = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'powerzoid-sync', 'config',
]);
const CONFIG_DIR = GLib.build_filenamev([
    GLib.get_home_dir(), '.config', 'powerzoid-sync',
]);
const DEFAULT_FOLDER = GLib.build_filenamev([GLib.get_home_dir(), 'Proyectos']);


// ─────────────────────────────────────────────────────────────────────────────
// Clase principal de preferencias
// ─────────────────────────────────────────────────────────────────────────────

export default class SyncPreferences extends ExtensionPreferences {

    fillPreferencesWindow(window) {
        window.set_default_size(600, 520);
        window.set_title('PowerZoid Sync — Configuración');
        this._window = window;

        const cfg = this._readConfig();

        // ── Página ────────────────────────────────────────────────────────
        const page = new Adw.PreferencesPage({
            title: 'Configuración',
            icon_name: 'folder-remote-symbolic',
        });
        window.add(page);

        // ── Grupo: Sincronización ─────────────────────────────────────────
        const syncGroup = new Adw.PreferencesGroup({
            title: 'Sincronización',
            description: 'Define qué carpeta se sincroniza y con qué cuenta de GitHub.',
        });
        page.add(syncGroup);

        // Carpeta de proyectos — fila con botón de explorar
        this._folderRow = new Adw.EntryRow({
            title: 'Carpeta de proyectos',
            text: cfg.folder || DEFAULT_FOLDER,
            show_apply_button: true,
        });
        const browseBtn = new Gtk.Button({
            icon_name:    'folder-open-symbolic',
            valign:       Gtk.Align.CENTER,
            tooltip_text: 'Explorar carpeta…',
            css_classes:  ['flat'],
        });
        browseBtn.connect('clicked', () => this._pickFolder());
        this._folderRow.add_suffix(browseBtn);
        syncGroup.add(this._folderRow);

        // GitHub Personal Access Token — campo de contraseña con reveal button
        this._tokenRow = new Adw.PasswordEntryRow({
            title: 'GitHub Personal Access Token',
            text:  cfg.token || '',
            show_apply_button: true,
        });
        syncGroup.add(this._tokenRow);

        // Link directo para crear el token en GitHub
        const tokenLinkRow = new Adw.ActionRow({
            title:      'Crear un nuevo token en GitHub',
            subtitle:   'Scope necesario: repo (acceso completo)',
            activatable: true,
        });
        tokenLinkRow.add_suffix(new Gtk.Image({
            icon_name: 'external-link-symbolic',
            valign:    Gtk.Align.CENTER,
            css_classes: ['dim-label'],
        }));
        tokenLinkRow.connect('activated', () => {
            Gio.AppInfo.launch_default_for_uri(
                'https://github.com/settings/tokens/new' +
                '?scopes=repo&description=powerzoid-sync',
                null
            );
        });
        syncGroup.add(tokenLinkRow);

        // ── Grupo: Avanzado ───────────────────────────────────────────────
        const advGroup = new Adw.PreferencesGroup({
            title: 'Avanzado',
            description:
                'Repos de GitHub que NO deben clonarse automáticamente.\n' +
                'Útil cuando un repo tiene nombre distinto en local vs GitHub\n' +
                '(ej: "escritorio-bbcl-app" está clonado como "despacho-bbcl-app").',
        });
        page.add(advGroup);

        this._skipRow = new Adw.EntryRow({
            title: 'Repos a ignorar',
            text:  cfg.skip_repos || '',
            show_apply_button: true,
        });
        // Placeholder dentro del campo
        this._skipRow.set_input_purpose(Gtk.InputPurpose.FREE_FORM);
        advGroup.add(this._skipRow);

        // Texto de ayuda como fila adicional
        const skipHintRow = new Adw.ActionRow({
            subtitle:  'Separados por coma.  Ejemplo: escritorio-bbcl-app, repo-viejo',
            sensitive: false,
        });
        advGroup.add(skipHintRow);

        // ── Botón de guardar ──────────────────────────────────────────────
        const saveGroup = new Adw.PreferencesGroup();
        page.add(saveGroup);

        // Adw.ButtonRow (disponible desde libadwaita 1.6 / GNOME 47+)
        const saveRow = new Adw.ButtonRow({ title: 'Guardar y reiniciar daemon' });
        saveRow.add_css_class('suggested-action');
        saveGroup.add(saveRow);
        saveRow.connect('activated', () => this._save());

        // También guardar al presionar Enter dentro de cualquier campo
        this._folderRow.connect('apply', () => this._save());
        this._tokenRow.connect('apply',  () => this._save());
        this._skipRow.connect('apply',   () => this._save());
    }

    // ─────────────────────────────────────────────────────────────────────
    // Selector de carpeta (Gtk.FileDialog, GTK 4.10+)
    // ─────────────────────────────────────────────────────────────────────

    _pickFolder() {
        const dialog = new Gtk.FileDialog({
            title:        'Seleccionar carpeta de proyectos',
            accept_label: 'Seleccionar',
            modal:        true,
        });

        const currentPath = this._folderRow.text.trim();
        if (currentPath) {
            dialog.initial_folder = Gio.File.new_for_path(currentPath);
        }

        dialog.select_folder(this._window, null, (d, result) => {
            try {
                const file = d.select_folder_finish(result);
                this._folderRow.set_text(file.get_path());
            } catch (_e) {
                // Usuario canceló — no hacer nada
            }
        });
    }

    // ─────────────────────────────────────────────────────────────────────
    // Guardar y reiniciar daemon
    // ─────────────────────────────────────────────────────────────────────

    _save() {
        const cfg = {
            folder:     this._folderRow.text.trim(),
            token:      this._tokenRow.text.trim(),
            skip_repos: this._skipRow.text.trim(),
        };

        const ok = this._writeConfig(cfg);

        if (ok) {
            this._restartDaemon();
            this._window.add_toast(new Adw.Toast({
                title:   '✓ Guardado. El daemon se está reiniciando…',
                timeout: 3,
            }));
        } else {
            this._window.add_toast(new Adw.Toast({
                title:   '❌ Error al guardar la configuración',
                timeout: 4,
            }));
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Leer config
    // ─────────────────────────────────────────────────────────────────────

    _readConfig() {
        const result = {};
        try {
            const file = Gio.File.new_for_path(CONFIG_PATH);
            const [ok, bytes] = file.load_contents(null);
            if (ok) {
                const text = new TextDecoder().decode(bytes);
                for (const raw of text.split('\n')) {
                    const line = raw.trim();
                    if (!line || line.startsWith('#')) continue;
                    const idx = line.indexOf('=');
                    if (idx < 0) continue;
                    result[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
                }
            }
        } catch (_e) {
            // El archivo aún no existe; se usarán los valores por defecto
        }
        return result;
    }

    // ─────────────────────────────────────────────────────────────────────
    // Escribir config
    // ─────────────────────────────────────────────────────────────────────

    _writeConfig(cfg) {
        try {
            // Crear directorio si no existe
            const dir = Gio.File.new_for_path(CONFIG_DIR);
            if (!dir.query_exists(null))
                dir.make_directory_with_parents(null);

            const now   = new Date().toLocaleString('es-CL');
            const lines = [
                '# PowerZoid Sync — Configuración',
                `# Guardado: ${now}`,
                '',
            ];

            if (cfg.folder)     lines.push(`folder=${cfg.folder}`);
            if (cfg.token)      lines.push(`token=${cfg.token}`);
            if (cfg.skip_repos) lines.push(`skip_repos=${cfg.skip_repos}`);
            lines.push('');

            const file = Gio.File.new_for_path(CONFIG_PATH);
            file.replace_contents(
                new TextEncoder().encode(lines.join('\n')),
                null,
                false,
                Gio.FileCreateFlags.REPLACE_DESTINATION,
                null
            );

            // Permisos 600 (solo lectura/escritura del propietario)
            const info = new Gio.FileInfo();
            info.set_attribute_uint32('unix::mode', 0o600);
            file.set_attributes_from_info(info, Gio.FileQueryInfoFlags.NONE, null);

            return true;
        } catch (e) {
            logError(e);
            return false;
        }
    }

    // ─────────────────────────────────────────────────────────────────────
    // Reiniciar daemon via systemd
    // ─────────────────────────────────────────────────────────────────────

    _restartDaemon() {
        try {
            Gio.Subprocess.new(
                ['systemctl', '--user', 'restart', 'powerzoid-sync.service'],
                Gio.SubprocessFlags.NONE
            );
        } catch (_e) {
            // El daemon se reiniciará solo en el siguiente ciclo si systemctl falla
        }
    }
}
