# PowerZoid Sync — GNOME Shell Extension

Descarga automática (solo lectura) de los repos dentro de `~/Proyectos/` desde GitHub. El daemon **nunca hace commit ni push**: solo trae (`fetch` + fast-forward) los cambios que ya existen en GitHub hacia el equipo local. Indicador visual en tiempo real en la barra superior.

```
🟢 Sync   ← todo sincronizado
🟡⠸ Sync  ← sincronizando (animación)
🔴 Sync   ← error o conflicto
```

---

## Arquitectura

```
fedora-casa ──────────── GitHub ──────────── fedora-oficina
     │                                              │
  daemon:6790                                   daemon:6790
     │                                              │
  extensión GNOME                            extensión GNOME
```

- **Daemon Python** (`~/.local/bin/powerzoid-sync-daemon.py`): descarga cambios cada 30 minutos, expone HTTP en `localhost:6790`.
- **Extensión GNOME**: consulta el daemon cada 5 segundos y muestra el estado.
- **GitHub** es la única fuente de verdad: el daemon solo lee de ahí, nunca escribe.
- Los cambios locales (commits, push) siguen siendo responsabilidad manual del usuario en cada repo.

---

## Instalación

### Paso 0 — Crear un GitHub Personal Access Token

1. Ve a **https://github.com/settings/tokens/new**
2. Nombre: `powerzoid-sync`
3. Scope: **`repo`** (tick en el checkbox principal)
4. Expiration: `No expiration` (o la que prefieras)
5. Clic en **Generate token**
6. Copia el token (`ghp_xxxx...`) — solo se muestra una vez

### Paso 1 — Instalar

```bash
# Clonar o descomprimir el proyecto
cd ~/Proyectos/powerzoid-sync
bash install.sh
```

### Paso 2 — Configurar el token

```bash
nano ~/.config/powerzoid-sync/config
```

Agrega/descomenta la línea `token=`:

```
token=ghp_xxxxxxxxxxxxxxxxxxxx
```

Si tienes repos de GitHub con nombres distintos a su carpeta local (p.ej. `escritorio-bbcl-app` está clonado como `despacho-bbcl-app`), agrégalos a `skip_repos`:

```
skip_repos=escritorio-bbcl-app
```

### Paso 3 — Logout y login

```bash
gnome-session-quit --logout
```

Al iniciar sesión, la extensión aparecerá en la barra izquierda. La primera sync ocurre 2 minutos después.

---

## Comportamiento con carpetas nuevas

| Situación | Comportamiento |
|-----------|---------------|
| Nueva carpeta en `~/Proyectos/` **sin `.git`** | Marcada como "Sin Git" en el menú |
| Carpeta con `.git` pero **sin remote** | Marcada como "Sin remoto" en el menú |
| Repo en GitHub **no presente localmente** | **Clonado automáticamente** en la próxima sync |
| Carpeta existente con `.git` + remote | Detectada y sincronizada automáticamente |

> Los repos nuevos no necesitan configuración adicional. El daemon escanea `~/Proyectos/*/` en cada ciclo.

---

## Menú de la extensión

Haciendo clic en el indicador:

```
PowerZoid Sync
─────────────────────────────
✅  Sincronizado
Última sync: 2026-07-24 10:30
─────────────────────────────
📊 14 repos sincronizados · 1 clonado
⚠  Sin remoto Git (1):
     bbcl-processor
─────────────────────────────
↻  Sincronizar ahora
📋  Ver log
```

---

## Resolución de problemas

### El daemon no responde

```bash
# Ver estado del servicio
systemctl --user status powerzoid-sync.service

# Ver logs
journalctl --user -u powerzoid-sync.service -n 50

# Reiniciar
systemctl --user restart powerzoid-sync.service
```

### Ver el log de sincronización

```bash
tail -50 ~/.local/share/git-sync/sync.log
```

### Ver el estado JSON en bruto

```bash
curl -s localhost:6790/status | python3 -m json.tool
```

### Trigger de sync manual desde terminal

```bash
curl -s -X POST localhost:6790/sync
```

### Un repo no puede descargar cambios (fast-forward no posible)

El daemon solo hace `fetch` + `merge --ff-only`: si el repo local tiene commits propios o cambios sin commitear que chocan con lo que hay en GitHub, no toca nada y lo marca como conflicto. Para resolverlo manualmente:

```bash
cd ~/Proyectos/nombre-repo
git status                           # Ver el estado del repo
git pull origin main                 # O el merge/rebase que prefieras
```

### Repo en GitHub no se clona (error de permisos SSH)

Verifica que el agente SSH esté activo:
```bash
ssh -T git@github.com
# Respuesta esperada: Hi ChristianLeal1978! You've successfully authenticated...
```

---

## Gestión del daemon

```bash
# Estado
systemctl --user status powerzoid-sync.service

# Reiniciar
systemctl --user restart powerzoid-sync.service

# Detener
systemctl --user stop powerzoid-sync.service

# Sync inmediata
systemctl --user start powerzoid-sync.service  # ← si está detenido
curl -s -X POST localhost:6790/sync             # ← si está corriendo
```

---

## Desinstalar

```bash
bash uninstall.sh
```

La config (`~/.config/powerzoid-sync/config`) y los logs se conservan por seguridad.

---

## Requisitos

- Fedora 44 / GNOME Shell 45–50
- Python 3 (incluido en Fedora)
- SSH configurado con GitHub (`~/.ssh/id_ed25519` registrada en GitHub)
- GNOME Keyring activo (incluido en Fedora + GNOME por defecto)

---

## Licencia

GPL-2.0
