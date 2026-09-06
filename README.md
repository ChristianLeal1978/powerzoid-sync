# PowerZoid Sync — GNOME Shell Extension

Descarga automática (solo lectura) de los repos dentro de `~/Proyectos/` desde GitHub. Para los repos de proyecto el daemon **nunca hace commit ni push**: solo trae (`fetch` + fast-forward) los cambios que ya existen en GitHub hacia el equipo local. Opcionalmente también sincroniza los `.env.local` de cada proyecto entre equipos, cifrados, a través de un repo Git privado aparte (ver [Secretos](#secretos-envlocal-entre-equipos) — ese repo sí recibe commit/push del propio daemon). Indicador visual en tiempo real en la barra superior.

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
- **GitHub** es la única fuente de verdad para los repos de proyecto: el daemon solo lee de ahí, nunca escribe.
- Los cambios locales (commits, push) de los repos de proyecto siguen siendo responsabilidad manual del usuario.
- Los `.env.local` son la única excepción: viven en un repo Git privado aparte (`powerzoid-secrets`) que el daemon sí escribe (commit + push), siempre cifrados con `age`.

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

## Carpetas descontinuadas

Al principio de cada sync, el daemon borra automáticamente las carpetas de proyectos que fueron renombradas a `powerzoid-*` pero cuyo repo de GitHub original sigue existiendo (por lo que, sin esto, el paso de "clonar repos de GitHub no presentes localmente" las volvería a traer):

| Carpeta obsoleta | Reemplazada por |
|---|---|
| `claude-usage-extension` | `powerzoid-claude` |
| `ram-monitor-gnome` | `powerzoid-memory` |
| `whatsapp-sidebar` | `powerzoid-messenger` |
| `spotify-now-playing-gnome` | `powerzoid-music` |

También borra `~/.config/whatsapp-sidebar` (sesión/caché de WhatsApp Web de la versión vieja).

Esta lista vive en `STALE_PROJECT_DIRS` / `STALE_PATHS` al inicio de `daemon/powerzoid-sync-daemon.py`. Como el daemon corre desde la copia instalada en `~/.local/bin/`, un equipo que ya tenga el servicio corriendo necesita **reinstalar** (`bash install.sh`) después de traer este cambio para que la poda tome efecto — el `git pull` de este repo por sí solo no actualiza el daemon en ejecución.

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

## Secretos (`.env.local`) entre equipos

Sincroniza los `.env.local` de cada proyecto entre tus equipos (ej. casa ↔ oficina), fusionando por `key` en vez de sobrescribir el archivo completo:

- Si una misma `key` tiene valores distintos en cada equipo, gana el valor del archivo **modificado más recientemente**.
- Si una `key` existe en un lado y no en el otro, se agrega al otro — ambos quedan con el mismo contenido.
- Nunca se sube nada en texto plano: cada `.env.local` se cifra con [`age`](https://github.com/FiloSottile/age) antes de salir del equipo, hacia un **repo Git privado aparte** (nunca uno de tus proyectos) que solo contiene blobs cifrados.

### Setup (una vez por cuenta)

**1. Instalar `age`** en cada equipo:

```bash
sudo dnf install age
```

**2. Crear el repo privado de secretos** (una sola vez, desde cualquier equipo):

```bash
gh repo create powerzoid-secrets --private
```

**3. Activar la sincronización** en Configuración de la extensión (clic derecho en el indicador → ⚙ Configuración… → sección "Secretos (.env.local)"):
   - Activa **"Sincronizar .env.local entre equipos"**.
   - Verifica que el campo **"Repo Git privado de secretos"** apunte al repo creado en el paso 2 (por defecto `git@github.com:ChristianLeal1978/powerzoid-secrets.git`).
   - Guarda. En la primera sync, el daemon genera una **identidad `age`** en `~/.config/powerzoid-sync/age-identity.txt` y lo indica en el log.

**4. Copiar la identidad `age` a tus otros equipos** (paso manual, igual que ya haces con la llave SSH):

```bash
scp ~/.config/powerzoid-sync/age-identity.txt otro-equipo:~/.config/powerzoid-sync/age-identity.txt
```

   Repite los pasos 1 y 3 en cada equipo adicional, usando la **misma identidad copiada** (no generes una nueva ahí, o no podrá descifrar lo que suban los demás equipos).

> ⚠️ El archivo `age-identity.txt` es una clave privada: **nunca se sube al repo `powerzoid-secrets`** ni a ningún otro repo. Trátalo como tratarías tu llave SSH privada.

### Cómo funciona por dentro

Por cada proyecto con `.env.local`, el repo `powerzoid-secrets` guarda:

```
<proyecto>.env.local.age   ← contenido cifrado
<proyecto>.meta.json       ← solo { "mtime": ... }, sin datos sensibles
```

En cada sync, el daemon descifra la versión remota, la compara por `key` contra la local (usando el `mtime` del archivo local vs. el guardado en `meta.json`), aplica la fusión descrita arriba, y si algo cambió reescribe el `.env.local` local y/o cifra + commitea + pushea la nueva versión al repo de secretos.

### Desactivar

Apaga el switch en Configuración, o borra `env_sync=1` de `~/.config/powerzoid-sync/config`. El repo local (`~/.local/share/powerzoid-sync/secrets-repo`) y la identidad no se tocan.

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

### `env-sync: no se pudo descifrar <proyecto>.env.local.age`

La identidad `age` de este equipo (`~/.config/powerzoid-sync/age-identity.txt`) no es la misma que la usada para cifrar ese archivo. Copia la identidad correcta desde el equipo que la generó (ver [Secretos](#secretos-envlocal-entre-equipos)) — no se puede recuperar el contenido sin ella.

### `env-sync: falta instalar 'age'`

```bash
sudo dnf install age
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
- `age` (`sudo dnf install age`) — solo si usas la sincronización de `.env.local`

---

## Licencia

GPL-2.0
