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
        })
    save_status()

    repos_state:  dict  = {}
    errors:       list  = []
    no_git:       list  = []
    no_remote:    list  = []
    pruned:       list  = []
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

        # ── 2. Repos en GitHub no presentes localmente ────────────────────
        token    = cfg_now.get("token", "").strip()
        skip_raw = cfg_now.get("skip_repos", "")
        skip_set = {s.strip() for s in skip_raw.split(",") if s.strip()}
        github_only: list = []

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
        f"· Podadas: {len(pruned)}")

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
