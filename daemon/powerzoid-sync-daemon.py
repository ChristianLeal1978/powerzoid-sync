#!/usr/bin/env python3
"""
PowerZoid Sync — Daemon de sincronización Git
Puerto HTTP: 6790

Sincroniza ~/Proyectos/ con GitHub vía SSH.
Lee config de ~/.config/powerzoid-sync/config
Escribe status en ~/.local/share/git-sync/status.json
Escribe log en ~/.local/share/git-sync/sync.log
"""

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

# ─────────────────────────────────────────────
# Rutas
# ─────────────────────────────────────────────
HOME = Path.home()
PROYECTOS    = HOME / "Proyectos"
LOG_FILE     = HOME / ".local/share/git-sync/sync.log"
STATUS_FILE  = HOME / ".local/share/git-sync/status.json"
CONFIG_FILE  = HOME / ".config/powerzoid-sync/config"

PORT                  = 6790
SYNC_INTERVAL_SECONDS = 30 * 60   # 30 minutos
INITIAL_DELAY_SECONDS = 120        # 2 minutos tras inicio (da tiempo al keyring)
GITHUB_USER           = "ChristianLeal1978"

# Carpetas de ~/Proyectos reemplazadas por su equivalente powerzoid-*
# (claude-usage-extension → powerzoid-claude, ram-monitor-gnome → powerzoid-memory,
# whatsapp-sidebar → powerzoid-messenger, spotify-now-playing-gnome → powerzoid-music).
# Sus repos de GitHub siguen existiendo, así que además de borrarlas si aparecen
# hay que excluirlas del auto-clone de "repos en GitHub no presentes localmente".
STALE_PROJECT_DIRS: list[str] = [
    "claude-usage-extension",
    "ram-monitor-gnome",
    "whatsapp-sidebar",
    "spotify-now-playing-gnome",
]
# Datos de apps asociados a esas carpetas que tampoco deben persistir.
STALE_PATHS: list[Path] = [
    HOME / ".config" / "whatsapp-sidebar",
]

# Auto-actualización: dónde vive el propio proyecto dentro de ~/Proyectos
# (se sincroniza como cualquier otro repo) y dónde quedan instalados sus
# archivos, para poder compararlos y reinstalar cuando difieran.
REPO_NAME         = "powerzoid-sync"
EXTENSION_UUID    = "powerzoid-sync@cleal.cl"
INSTALLED_DAEMON  = HOME / ".local/bin/powerzoid-sync-daemon.py"
INSTALLED_EXT_DIR = HOME / ".local/share/gnome-shell/extensions" / EXTENSION_UUID
INSTALLED_SERVICE = HOME / ".config/systemd/user/powerzoid-sync.service"

# Sincronización de .env.local entre equipos: repo Git privado aparte
# (nunca uno de los proyectos) que solo contiene blobs cifrados con
# `age`. Se activa con env_sync=1 en la config; ver sync_env_files().
DEFAULT_SECRETS_REPO = f"git@github.com:{GITHUB_USER}/powerzoid-secrets.git"
SECRETS_LOCAL_REPO   = HOME / ".local/share/powerzoid-sync/secrets-repo"
AGE_IDENTITY_FILE    = HOME / ".config/powerzoid-sync/age-identity.txt"

# ─────────────────────────────────────────────
# Estado global
# ─────────────────────────────────────────────
_status: dict = {
    "state":       "idle",   # idle | syncing | error
    "last_sync":   None,
    "repos":       {},       # nombre → {state, message?}
    "errors":      [],       # mensajes de error
    "no_git":      [],       # carpetas sin .git
    "no_remote":   [],       # repos sin remote configurado
    "github_only": [],       # repos en GitHub que no se pudieron clonar
    "pruned":      [],       # carpetas/rutas obsoletas eliminadas en esta sync
    "env_synced":  [],       # proyectos cuyo .env.local se fusionó en esta sync
}
_lock              = threading.Lock()
_sync_in_progress  = False


# ─────────────────────────────────────────────
# Utilidades
# ─────────────────────────────────────────────

def log(msg: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line)
    except Exception:
        pass
    print(line, end="", flush=True)


def save_status() -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        data = dict(_status)
    try:
        with open(STATUS_FILE, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"  [status] Error guardando: {e}")


def read_config() -> dict:
    cfg: dict = {}
    if not CONFIG_FILE.exists():
        return cfg
    try:
        for raw in CONFIG_FILE.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    except Exception:
        pass
    return cfg


def ssh_env() -> dict:
    """Entorno con SSH_AUTH_SOCK correcto aunque systemd no lo herede."""
    env = os.environ.copy()
    if not env.get("SSH_AUTH_SOCK"):
        uid = os.getuid()
        env["SSH_AUTH_SOCK"] = f"/run/user/{uid}/keyring/ssh"
    return env


def run(cmd: str, cwd: Path | None = None, timeout: int = 90) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True,
            env=ssh_env(), timeout=timeout
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 1, "timeout"
    except Exception as e:
        return 1, str(e)


# ─────────────────────────────────────────────
# GitHub API
# ─────────────────────────────────────────────

def github_repos(token: str) -> list[str]:
    """Lista los repos propios (no forks) del usuario en GitHub."""
    names: list[str] = []
    page = 1
    while True:
        url = (
            f"https://api.github.com/user/repos"
            f"?per_page=100&type=owner&page={page}"
        )
        req = urllib.request.Request(url, headers={
            "Authorization": f"token {token}",
            "User-Agent":    "PowerZoid-Sync/1.0",
            "Accept":        "application/vnd.github+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                if not data:
                    break
                for r in data:
                    if not r.get("fork", False):
                        names.append(r["name"])
                page += 1
        except Exception as e:
            log(f"  [GitHub API] Error en página {page}: {e}")
            break
    return names


def local_remote_repo_names(base: Path | None = None) -> set[str]:
    """
    Devuelve el conjunto de nombres de repo en GitHub que ya están clonados
    localmente, aunque el nombre de la carpeta sea distinto (e.g.
    carpeta "despacho-bbcl-app" tiene remote "escritorio-bbcl-app").
    """
    names: set[str] = set()
    root = base or PROYECTOS
    if not root.exists():
        return names
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / ".git").exists():
            continue
        code, url = run("git remote get-url origin", cwd=d)
        if code != 0:
            continue
        # Extrae nombre del repo de la URL SSH o HTTPS
        # SSH:  git@github.com:USER/REPO.git
        # HTTPS: https://github.com/USER/REPO.git
        url = url.strip()
        part = url.rstrip("/")
        if part.endswith(".git"):
            part = part[:-4]
        repo_name = part.split("/")[-1].split(":")[-1]
        if "/" in repo_name:
            repo_name = repo_name.split("/")[-1]
        names.add(repo_name)
    return names


# ─────────────────────────────────────────────
# Limpieza de carpetas descontinuadas
# ─────────────────────────────────────────────

def prune_stale(proyectos: Path) -> list[str]:
    """
    Elimina carpetas de proyectos y datos de apps que quedaron obsoletos
    tras renombrar un proyecto a powerzoid-*. Se corre en cada sync para
    que un equipo que todavía no se había limpiado (p.ej. el de la
    oficina) quede al día automáticamente. Idempotente: si ya no existen,
    no hace nada.
    """
    removed: list[str] = []
    for name in STALE_PROJECT_DIRS:
        path = proyectos / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
            log(f"  [prune] Carpeta obsoleta eliminada: {path}")
    for path in STALE_PATHS:
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
            log(f"  [prune] Datos obsoletos eliminados: {path}")
    return removed


# ─────────────────────────────────────────────
# Auto-actualización del propio daemon
# ─────────────────────────────────────────────

def _files_differ(src: Path, dst: Path) -> bool:
    if not dst.exists():
        return True
    try:
        return src.read_bytes() != dst.read_bytes()
    except Exception:
        return True


def _repo_ahead_of_install(repo_dir: Path) -> bool:
    daemon_src = repo_dir / "daemon" / "powerzoid-sync-daemon.py"
    if not daemon_src.is_file():
        return False
    if _files_differ(daemon_src, INSTALLED_DAEMON):
        return True

    ext_src = repo_dir / "extension" / EXTENSION_UUID
    if ext_src.is_dir():
        for f in ext_src.iterdir():
            if f.is_file() and _files_differ(f, INSTALLED_EXT_DIR / f.name):
                return True

    service_src = repo_dir / "systemd" / "powerzoid-sync.service"
    if service_src.is_file() and _files_differ(service_src, INSTALLED_SERVICE):
        return True

    return False


def self_update(proyectos: Path) -> bool:
    """
    Compara el repo powerzoid-sync (ya sincronizado en el paso 1 de este
    mismo ciclo) con lo instalado en ~/.local/bin, la extensión y la
    unidad systemd. Si algo cambió, reinstala y reinicia el servicio.

    El reinicio se dispara con `systemd-run` en un scope aparte: si se
    hiciera con un `systemctl --user restart` normal desde este mismo
    proceso, systemd mataría todo el cgroup del servicio -incluida esta
    misma copia de archivos a medio terminar- antes de completarla.

    Devuelve True si se disparó una reinstalación (el proceso morirá en
    los próximos segundos).
    """
    repo_dir = proyectos / REPO_NAME
    if not _repo_ahead_of_install(repo_dir):
        return False

    log("  [self-update] Nueva versión en el repo — reinstalando...")
    try:
        INSTALLED_DAEMON.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo_dir / "daemon" / "powerzoid-sync-daemon.py", INSTALLED_DAEMON)
        INSTALLED_DAEMON.chmod(0o755)

        ext_src = repo_dir / "extension" / EXTENSION_UUID
        if ext_src.is_dir():
            INSTALLED_EXT_DIR.mkdir(parents=True, exist_ok=True)
            for f in ext_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, INSTALLED_EXT_DIR / f.name)

        service_src = repo_dir / "systemd" / "powerzoid-sync.service"
        if service_src.is_file():
            INSTALLED_SERVICE.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(service_src, INSTALLED_SERVICE)
            run("systemctl --user daemon-reload")
    except Exception as e:
        log(f"  [self-update] Error copiando archivos, se aborta: {e}")
        return False

    # Recarga la extensión en caliente (no requiere logout: solo hace
    # falta para UUIDs que el shell nunca vio antes).
    try:
        run(f"gnome-extensions disable {EXTENSION_UUID}")
        run(f"gnome-extensions enable {EXTENSION_UUID}")
    except Exception:
        pass

    log("  [self-update] Archivos actualizados, reiniciando servicio...")
    try:
        subprocess.Popen(
            ["systemd-run", "--user", "--quiet", "--",
             "systemctl", "--user", "restart", "powerzoid-sync.service"],
            env=ssh_env(),
            start_new_session=True,
        )
    except Exception as e:
        log(f"  [self-update] Error disparando el reinicio: {e}")
        return False

    return True


# ─────────────────────────────────────────────
# Sincronización de un repo individual
# ─────────────────────────────────────────────

def sync_one(repo_path: Path) -> tuple[str, str]:
    """
    Descarga cambios de GitHub hacia el repo local. Solo lectura:
    nunca hace commit ni push de cambios locales.
    Retorna (state, message):
      state: "ok" | "no_remote" | "error" | "conflict"
    """
    name = repo_path.name

    # ¿Tiene remote configurado?
    code, _ = run("git remote get-url origin", cwd=repo_path)
    if code != 0:
        log(f"  [{name}] Sin remote, omitido")
        return "no_remote", ""

    # Rama actual
    _, branch = run("git rev-parse --abbrev-ref HEAD", cwd=repo_path)
    branch = branch.strip() or "main"

    # Fetch + fast-forward-only: solo descarga, nunca crea commits ni
    # toca cambios locales sin commitear.
    code, out = run("git fetch origin", cwd=repo_path)
    if code != 0:
        return "error", f"fetch: {out[:200]}"

    code, out = run(f"git merge --ff-only origin/{branch}", cwd=repo_path)
    log(f"  [{name}] merge --ff-only → {out[:100]}")
    if code != 0:
        if "not possible to fast-forward" in out.lower() or "diverging" in out.lower():
            return "conflict", f"no se puede hacer fast-forward en {name} (hay commits locales o cambios divergentes)"
        return "error", f"merge: {out[:200]}"

    return "ok", ""


# ─────────────────────────────────────────────
# Sincronización de .env.local (secretos cifrados)
# ─────────────────────────────────────────────
#
# Modelo: un repo Git privado aparte (nunca uno de los proyectos) que
# solo contiene, por proyecto, un archivo "<nombre>.env.local.age"
# (contenido cifrado con age) y "<nombre>.meta.json" (solo la fecha de
# modificación, sin datos sensibles). El daemon es el único que
# escribe en ese repo: hace commit y push él mismo, a diferencia de los
# repos de proyecto que solo se leen (fetch + ff-only).
#
# La identidad age (AGE_IDENTITY_FILE) es la clave privada que permite
# descifrar: se genera sola la primera vez pero JAMÁS se sube al repo.
# Para que dos equipos puedan compartir secretos hay que copiar ese
# archivo entre ellos a mano (misma ruta), igual que ya se hace hoy con
# la llave SSH.
#
# Fusión por proyecto: si ambos lados tienen valores distintos para una
# misma key, gana el archivo modificado más recientemente (mtime local
# vs. mtime guardado en el meta.json remoto); las keys que solo existen
# en un lado se agregan al otro para que ambos queden idénticos.

def _parse_env(text: str) -> dict:
    result: dict = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        result[k.strip()] = v.strip()
    return result


def _apply_merged_env(base_text: str, merged: dict) -> str:
    """Reescribe base_text con los valores de merged, preservando
    comentarios/orden, y agrega al final las keys que no estaban."""
    lines = base_text.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in merged:
                indent = line[: len(line) - len(line.lstrip())]
                out.append(f"{indent}{k}={merged[k]}")
                seen.add(k)
                continue
        out.append(line)
    missing = [k for k in merged if k not in seen]
    if missing:
        if out and out[-1].strip() != "":
            out.append("")
        for k in missing:
            out.append(f"{k}={merged[k]}")
    return "\n".join(out) + "\n"


def _age_available() -> bool:
    return shutil.which("age") is not None and shutil.which("age-keygen") is not None


def _ensure_age_identity() -> str | None:
    """Genera la identidad age la primera vez y devuelve su clave
    pública (recipient). No sube nunca el archivo de identidad."""
    if not AGE_IDENTITY_FILE.exists():
        AGE_IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        code, out = run(f"age-keygen -o {AGE_IDENTITY_FILE}")
        if code != 0:
            log(f"  [env-sync] Error generando identidad age: {out}")
            return None
        AGE_IDENTITY_FILE.chmod(0o600)
        log(f"  [env-sync] Nueva identidad age generada en {AGE_IDENTITY_FILE}")
        log("  [env-sync] Copia ese archivo a tus otros equipos (misma ruta) "
            "para que puedan descifrar los secretos compartidos.")

    code, out = run(f"age-keygen -y {AGE_IDENTITY_FILE}")
    if code != 0:
        log(f"  [env-sync] Error leyendo clave pública age: {out}")
        return None
    return out.strip()


def _age_encrypt(plaintext: bytes, recipient: str) -> bytes | None:
    try:
        r = subprocess.run(
            ["age", "-r", recipient],
            input=plaintext, capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            log(f"  [env-sync] Error cifrando: {r.stderr.decode(errors='replace')[:200]}")
            return None
        return r.stdout
    except Exception as e:
        log(f"  [env-sync] Error cifrando: {e}")
        return None


def _age_decrypt(ciphertext: bytes) -> bytes | None:
    try:
        r = subprocess.run(
            ["age", "-d", "-i", str(AGE_IDENTITY_FILE)],
            input=ciphertext, capture_output=True, timeout=30,
        )
        if r.returncode != 0:
            log(f"  [env-sync] Error descifrando: {r.stderr.decode(errors='replace')[:200]}")
            return None
        return r.stdout
    except Exception as e:
        log(f"  [env-sync] Error descifrando: {e}")
        return None


def _ensure_secrets_repo(repo_url: str) -> bool:
    if not (SECRETS_LOCAL_REPO / ".git").is_dir():
        SECRETS_LOCAL_REPO.parent.mkdir(parents=True, exist_ok=True)
        code, out = run(f"git clone {repo_url} {SECRETS_LOCAL_REPO}", timeout=60)
        if code != 0:
            log(f"  [env-sync] No se pudo clonar el repo de secretos: {out[:200]}")
            return False

    code, out = run("git remote get-url origin", cwd=SECRETS_LOCAL_REPO)
    if code != 0 or out.strip() != repo_url:
        run(f"git remote set-url origin {repo_url}", cwd=SECRETS_LOCAL_REPO)

    code, out = run("git fetch origin", cwd=SECRETS_LOCAL_REPO)
    if code != 0:
        log(f"  [env-sync] Error en fetch del repo de secretos: {out[:200]}")
        return False

    _, branch = run("git rev-parse --abbrev-ref HEAD", cwd=SECRETS_LOCAL_REPO)
    branch = branch.strip() or "main"

    code, _ = run(f"git rev-parse --verify origin/{branch}", cwd=SECRETS_LOCAL_REPO)
    if code != 0:
        return True  # Repo remoto todavía vacío, nada que fusionar

    code, out = run(f"git merge --ff-only origin/{branch}", cwd=SECRETS_LOCAL_REPO)
    if code != 0:
        log(f"  [env-sync] Repo de secretos no pudo hacer fast-forward: {out[:200]}")
        return False
    return True


def _push_secrets_repo(names: list[str]) -> bool:
    run("git add -A", cwd=SECRETS_LOCAL_REPO)
    joined = ", ".join(names)
    code, out = run(
        f'git -c user.email="powerzoid-sync@local" -c user.name="PowerZoid Sync" '
        f'commit -m "Actualiza secretos: {joined}"',
        cwd=SECRETS_LOCAL_REPO,
    )
    if code != 0 and "nothing to commit" not in out.lower():
        log(f"  [env-sync] Error en commit: {out[:200]}")
        return False

    for attempt in range(3):
        code, out = run("git push -u origin HEAD", cwd=SECRETS_LOCAL_REPO, timeout=60)
        if code == 0:
            return True
        log(f"  [env-sync] Push rechazado (intento {attempt + 1}/3): {out[:150]}")
        run("git fetch origin", cwd=SECRETS_LOCAL_REPO)
        _, branch = run("git rev-parse --abbrev-ref HEAD", cwd=SECRETS_LOCAL_REPO)
        branch = branch.strip() or "main"
        run(f"git merge -X ours --no-edit origin/{branch}", cwd=SECRETS_LOCAL_REPO)
    return False


def _project_env_sync(name: str, local_env_path: Path, recipient: str) -> tuple[bool, str]:
    """
    Fusiona el .env.local local de un proyecto con su versión cifrada
    en el repo de secretos. Puede reescribir local_env_path en disco.
    Devuelve (cambió_algo_en_el_repo_de_secretos, mensaje_de_error).
    """
    enc_path  = SECRETS_LOCAL_REPO / f"{name}.env.local.age"
    meta_path = SECRETS_LOCAL_REPO / f"{name}.meta.json"

    local_exists = local_env_path.is_file()
    local_text   = local_env_path.read_text() if local_exists else ""
    local_mtime  = local_env_path.stat().st_mtime if local_exists else None
    local_dict   = _parse_env(local_text)

    remote_exists = enc_path.is_file()
    remote_text   = ""
    remote_mtime  = None
    remote_dict: dict = {}
    if remote_exists:
        plain = _age_decrypt(enc_path.read_bytes())
        if plain is None:
            return False, f"no se pudo descifrar {name}.env.local.age"
        remote_text = plain.decode("utf-8", errors="replace")
        remote_dict = _parse_env(remote_text)
        if meta_path.is_file():
            try:
                remote_mtime = json.loads(meta_path.read_text()).get("mtime")
            except Exception:
                remote_mtime = None

    if not local_exists and not remote_exists:
        return False, ""

    if not remote_exists:
        base_text, merged = local_text, dict(local_dict)
    elif not local_exists:
        base_text, merged = remote_text, dict(remote_dict)
    elif (local_mtime or 0) >= (remote_mtime or 0):
        base_text, merged = local_text, {**remote_dict, **local_dict}
    else:
        base_text, merged = remote_text, {**local_dict, **remote_dict}

    merged_text = _apply_merged_env(base_text, merged)

    if not local_exists or merged_text != local_text:
        local_env_path.parent.mkdir(parents=True, exist_ok=True)
        local_env_path.write_text(merged_text)
        log(f"  [env-sync] {name}: .env.local actualizado localmente")

    if merged != remote_dict:
        cipher = _age_encrypt(merged_text.encode("utf-8"), recipient)
        if cipher is None:
            return False, f"no se pudo cifrar {name}"
        enc_path.write_bytes(cipher)
        meta_path.write_text(json.dumps({"mtime": time.time()}, indent=2))
        return True, ""

    return False, ""


def sync_env_files(proyectos: Path, cfg: dict) -> tuple[list[str], list[str]]:
    """Punto de entrada: sincroniza los .env.local de todos los
    proyectos contra el repo privado de secretos. No hace nada si
    env_sync no está activado en la config."""
    synced: list[str] = []
    errors: list[str] = []

    if cfg.get("env_sync", "").strip().lower() not in ("1", "true", "si", "sí", "yes"):
        return synced, errors

    if not _age_available():
        errors.append("env-sync: falta instalar 'age' (ej. sudo dnf install age)")
        return synced, errors

    recipient = _ensure_age_identity()
    if not recipient:
        errors.append("env-sync: no se pudo preparar la identidad age")
        return synced, errors

    repo_url = cfg.get("secrets_repo", "").strip() or DEFAULT_SECRETS_REPO
    if not _ensure_secrets_repo(repo_url):
        errors.append(f"env-sync: no se pudo sincronizar el repo de secretos ({repo_url})")
        return synced, errors

    names: set[str] = set()
    for p in proyectos.iterdir():
        if p.is_dir() and (p / ".env.local").is_file():
            names.add(p.name)
    for f in SECRETS_LOCAL_REPO.glob("*.env.local.age"):
        names.add(f.name[: -len(".env.local.age")])

    changed_names: list[str] = []
    for name in sorted(names):
        changed, msg = _project_env_sync(name, proyectos / name / ".env.local", recipient)
        if msg:
            errors.append(f"env-sync[{name}]: {msg}")
            continue
        if changed:
            changed_names.append(name)

    if changed_names:
        synced = changed_names
        if not _push_secrets_repo(changed_names):
            errors.append("env-sync: no se pudieron subir los cambios al repo de secretos")

    return synced, errors


# ─────────────────────────────────────────────
# Sincronización completa
# ─────────────────────────────────────────────

def do_sync() -> None:
    """Sincronización completa. Siempre se llama desde un hilo separado."""
    global _sync_in_progress

    with _lock:
        if _sync_in_progress:
            log("  [Sync] Sincronización ya en curso, omitiendo")
            return
        _sync_in_progress = True

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log("──────────────────────────────────────────")
    log("Iniciando sincronización")

    with _lock:
        _status.update({
            "state":       "syncing",
            "last_sync":   ts,
            "repos":       {},
            "errors":      [],
            "no_git":      [],
            "no_remote":   [],
            "github_only": [],
            "pruned":      [],
            "env_synced":  [],
        })
    save_status()

    repos_state:  dict  = {}
    errors:       list  = []
    no_git:       list  = []
    no_remote:    list  = []
    pruned:       list  = []
    env_synced:   list  = []
    final_state         = "idle"

    try:
        # Leer carpeta desde config (puede cambiarse desde las preferencias)
        cfg_now   = read_config()
        proyectos = Path(cfg_now.get('folder', str(PROYECTOS))).expanduser()
        proyectos.mkdir(parents=True, exist_ok=True)

        # ── 0. Podar carpetas descontinuadas ──────────────────────────────
        pruned = prune_stale(proyectos)

        # ── 1. Sincronizar repos locales ──────────────────────────────────
        for repo_path in sorted(proyectos.iterdir()):
            if not repo_path.is_dir():
                continue

            if not (repo_path / ".git").is_dir():
                log(f"  [{repo_path.name}] Sin .git, omitido")
                no_git.append(repo_path.name)
                continue

            state_val, msg = sync_one(repo_path)
            entry: dict = {"state": state_val}
            if msg:
                entry["message"] = msg
            repos_state[repo_path.name] = entry

            if state_val == "no_remote":
                no_remote.append(repo_path.name)
            elif state_val in ("error", "conflict"):
                errors.append(f"[{repo_path.name}] {msg}")
                final_state = "error"
            else:
                log(f"  [{repo_path.name}] ✓ sincronizado")

        # ── 1.5 Auto-actualización del propio daemon ───────────────────────
        # El repo powerzoid-sync ya quedó al día en el paso 1; si trae una
        # versión distinta a la instalada, reinstala y reinicia el servicio.
        # El resto de este ciclo (paso 2) se salta: el proceso morirá en
        # breve y el próximo ciclo lo retoma con el binario nuevo.
        github_only: list = []
        updated = self_update(proyectos)

        # ── 2. Repos en GitHub no presentes localmente ────────────────────
        if not updated:
            token    = cfg_now.get("token", "").strip()
            skip_raw = cfg_now.get("skip_repos", "")
            skip_set = {s.strip() for s in skip_raw.split(",") if s.strip()}

            if token:
                log("  [GitHub] Consultando repositorios via API...")
                gh_repos       = github_repos(token)
                already_cloned = local_remote_repo_names(proyectos)
                local_folders  = {p.name for p in proyectos.iterdir() if p.is_dir()}

                for repo_name in gh_repos:
                    if repo_name in skip_set or repo_name in STALE_PROJECT_DIRS:
                        continue
                    # ¿Ya está clonado (con cualquier nombre de carpeta)?
                    if repo_name in already_cloned:
                        continue
                    # ¿Existe carpeta con ese mismo nombre?
                    if repo_name in local_folders:
                        continue

                    log(f"  [GitHub] '{repo_name}' falta localmente — clonando...")
                    code, out = run(
                        f"git clone git@github.com:{GITHUB_USER}/{repo_name}.git {repo_name}",
                        cwd=proyectos,
                        timeout=120,
                    )
                    if code == 0:
                        log(f"  [GitHub] '{repo_name}' clonado ✓")
                        repos_state[repo_name] = {"state": "cloned"}
                        local_folders.add(repo_name)
                        already_cloned.add(repo_name)
                    else:
                        log(f"  [GitHub] Error clonando '{repo_name}': {out[:150]}")
                        github_only.append(repo_name)
            else:
                log("  [GitHub] Sin token en config — sync solo local")

            # ── 3. Sincronizar .env.local (secretos cifrados) ──────────────
            env_synced, env_errors = sync_env_files(proyectos, cfg_now)
            if env_synced:
                log(f"  [env-sync] .env.local fusionados: {', '.join(env_synced)}")
            if env_errors:
                errors.extend(env_errors)
                final_state = "error"

    except Exception as e:
        log(f"  [Sync] Error inesperado: {e}")
        errors.append(str(e))
        final_state = "error"
    finally:
        with _lock:
            _sync_in_progress = False

    log(f"Sincronización completada — Estado: {final_state}")
    log(f"  Repos: {len(repos_state)} · Errores: {len(errors)} "
        f"· Sin remoto: {len(no_remote)} · Solo GitHub: {len(github_only)} "
        f"· Podadas: {len(pruned)} · .env.local fusionados: {len(env_synced)}")

    with _lock:
        _status.update({
            "state":       final_state,
            "last_sync":   ts,
            "repos":       repos_state,
            "errors":      errors,
            "no_git":      no_git,
            "no_remote":   no_remote,
            "github_only": github_only,
            "pruned":      pruned,
            "env_synced":  env_synced,
        })
    save_status()


# ─────────────────────────────────────────────
# Hilo de sync periódica
# ─────────────────────────────────────────────

def sync_loop() -> None:
    log(f"Sync loop: primera sync en {INITIAL_DELAY_SECONDS // 60} min")
    time.sleep(INITIAL_DELAY_SECONDS)
    while True:
        try:
            do_sync()
        except Exception as e:
            log(f"  [Sync loop] Error: {e}")
            with _lock:
                _status["state"]  = "error"
                _status["errors"] = [str(e)]
            save_status()
        time.sleep(SYNC_INTERVAL_SECONDS)


# ─────────────────────────────────────────────
# HTTP Server
# ─────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:
        pass  # Silenciar logs HTTP en stdout

    def _json(self, code: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/status":
            with _lock:
                data = dict(_status)
            self._json(200, data)

        elif self.path == "/log":
            try:
                lines = LOG_FILE.read_text(errors="replace").splitlines()[-100:]
            except Exception:
                lines = []
            self._json(200, {"lines": lines})

        elif self.path == "/ping":
            self._json(200, {"ok": True, "port": PORT})

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == "/sync":
            t = threading.Thread(target=do_sync, daemon=True)
            t.start()
            self._json(202, {"status": "sync iniciado"})
        else:
            self._json(404, {"error": "not found"})


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main() -> None:
    # Crear directorios necesarios
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROYECTOS.mkdir(parents=True, exist_ok=True)

    # Cargar estado previo (para mostrar algo mientras esperamos la primera sync)
    if STATUS_FILE.exists():
        try:
            prev = json.loads(STATUS_FILE.read_text())
            with _lock:
                _status.update(prev)
                _status["state"] = "idle"  # Siempre empezar como idle
        except Exception:
            pass

    log(f"PowerZoid Sync Daemon v1.0 — puerto {PORT}")
    log(f"Config: {CONFIG_FILE}")

    # Verificar config
    cfg = read_config()
    folder_path = Path(cfg.get("folder", str(PROYECTOS))).expanduser()
    log(f"Proyectos: {folder_path}")
    if cfg.get("token"):
        log("  Token de GitHub: configurado ✓")
    else:
        log("  Token de GitHub: NO configurado (solo sync local)")

    # Hilo de sync periódica
    t = threading.Thread(target=sync_loop, daemon=True)
    t.start()

    # Servidor HTTP
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    log(f"HTTP escuchando en localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Daemon detenido por usuario")


if __name__ == "__main__":
    main()
