#!/usr/bin/env python3
"""
D2M Control Panel -- a local dashboard for starting/stopping/restarting the
D2M backend (uvicorn) and frontend (vite) dev servers, with status, logs,
and a few one-click actions (install deps, seed demo data, run tests).

Stdlib only -- no pip install required to run this file itself, since the
whole point is to help with the "pip/python not found" class of problem.
Run it directly:

    python3 control_panel.py

or via the "D2M Control Panel.command" launcher next to it, which does the
same thing and opens the dashboard in your browser automatically.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ---- Configuration ----------------------------------------------------

PANEL_PORT = 8765

BACKEND_DIR = Path(os.path.expanduser("~/Desktop/d2m_core_engine"))
FRONTEND_DIR = Path(os.path.expanduser("~/Desktop/d2m_web"))
# The messaging-framework repo root (npm workspaces) -- `npm run dev:server`
# is a root-level script that runs `npm run dev -w @msg/server`, so this
# needs to be the repo root, not packages/server itself.
MESSAGING_DIR = Path(os.path.expanduser("~/Desktop/messaging-framework"))

BACKEND_PORT = 8000
FRONTEND_PORT = 5173
MESSAGING_PORT = 4000

BACKEND_URL = f"http://127.0.0.1:{BACKEND_PORT}"
FRONTEND_URL = f"http://127.0.0.1:{FRONTEND_PORT}"
MESSAGING_URL = f"http://127.0.0.1:{MESSAGING_PORT}"

LOG_DIR = Path(os.path.expanduser("~/Library/Application Support/D2MControlPanel/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOG_TAIL_LINES = 60

# ---- Public demo link (Cloudflare Tunnel) ---------------------------------
# "Go live" reuses the named tunnel already set up by hand for the messaging
# server (see chat.prashanthsridhar.com's Tunnel DNS record + the existing
# ~/.cloudflared/config.yml) rather than spinning up separate `cloudflared
# tunnel --url` quick tunnels per service -- one persistent tunnel process
# fans out to all three local ports via hostname-based ingress rules, so
# there's one thing to start/stop and the URLs never change between runs.
CLOUDFLARED_TUNNEL_NAME = "msg"
CLOUDFLARED_CONFIG = Path(os.path.expanduser("~/.cloudflared/config.yml"))
CLOUDFLARED_METRICS_PORT = 8766

PUBLIC_HOSTNAME_MSG = "chat.prashanthsridhar.com"
PUBLIC_HOSTNAME_API = "api.prashanthsridhar.com"
PUBLIC_HOSTNAME_APP = "app.prashanthsridhar.com"

# The backend (app/config.py) defaults its CORS allowlist to just the local
# Vite dev origins (localhost:5173 / 127.0.0.1:5173) unless
# D2M_CORS_ALLOWED_ORIGINS is set in its environment. uvicorn is always
# launched as a *child* of this panel process (see backend_start_cmd() /
# ManagedService.start()), which inherits whatever's in this process's own
# environment -- so setting it here, once, at import time, means every
# backend start from this panel (plain local dev or Go live) always allows
# the public app hostname too, rather than that only being true on however
# many terminal sessions someone happened to export it by hand in. Without
# this, every backend restart the panel does silently regresses back to
# CORS blocking https://app.prashanthsridhar.com -- surfaced as the
# frontend's fetch calls failing with a generic network error (browsers
# don't expose the real reason for a CORS failure to JS), e.g. login's
# "Couldn't look that id up right now" for what was actually a CORS block,
# not a real backend problem.
os.environ.setdefault(
    "D2M_CORS_ALLOWED_ORIGINS",
    f"http://localhost:5173,http://127.0.0.1:5173,https://{PUBLIC_HOSTNAME_APP}",
)

# Same class of bug, same fix, for the messaging server -- its own
# messaging-framework/.env has CORS_ORIGIN explicitly set (not left as the
# wildcard "reflect any origin" default; see packages/server/src/config.ts)
# to just the LAN dev origins. `tsx watch` auto-loads that .env file from
# its cwd (which is MESSAGING_DIR, since that's where this panel starts it
# from), and dotenv-style loaders don't override a var that's already
# present in the process environment -- so setting it here first, before
# the child is ever spawned, wins over the .env file's value rather than
# fighting it. Preserves the existing LAN entry (192.168.1.41:5173) from
# that file rather than clobbering it, and adds the public app hostname on
# top -- surfaced as chat messages silently failing to load/send (blocked
# key-bundle and message-sync fetches) with no indication in the UI that it
# was a CORS problem rather than the messaging server being down.
os.environ.setdefault(
    "CORS_ORIGIN",
    f"http://localhost:5173,http://127.0.0.1:5173,http://192.168.1.41:5173,https://{PUBLIC_HOSTNAME_APP}",
)


def _backend_python() -> str:
    """Prefer a venv inside the backend repo if one exists (the setup
    instructions suggest creating one to dodge macOS's "externally managed
    environment" pip error) -- fall back to python3 on PATH otherwise."""
    venv_python = BACKEND_DIR / "venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


# ---- Process management -------------------------------------------------
# Every managed process is started in its own process group (start_new_
# session=True) so Stop/Restart can kill the whole tree in one shot --
# uvicorn --reload and vite dev both spawn child/worker processes, and
# killing only the parent PID leaves orphans holding the port.

class ManagedService:
    def __init__(self, name: str, port: int, health_url: str, health_ok=lambda body: True):
        self.name = name
        self.port = port
        self.health_url = health_url
        self.health_ok = health_ok
        self.process: subprocess.Popen | None = None
        self.log_path = LOG_DIR / f"{name}.log"
        self.lock = threading.Lock()
        # should_run tracks *intent*, for the watchdog below: True once
        # something has asked this service to be up (start()), False once
        # something has explicitly asked it to be down (stop()). The
        # watchdog only ever acts on services where should_run is True but
        # the OS-level process isn't there anymore -- e.g. it crashed on its
        # own (an uncaught exception, a lost DB connection after Postgres
        # restarts, cloudflared's own "accept stream listener" failures seen
        # earlier) -- never on a service someone deliberately stopped.
        self.should_run = False

    # -- health / status --

    def _http_check(self):
        try:
            with urllib.request.urlopen(self.health_url, timeout=1.5) as resp:
                body = resp.read(2048)
                return True, resp.status, body
        except urllib.error.HTTPError as e:
            # A real HTTP response (even an error status) still means
            # something is listening and speaking HTTP.
            return True, e.code, b""
        except Exception:
            return False, None, b""

    def _port_owner_pids(self) -> list[int]:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f"tcp:{self.port}"],
                capture_output=True, text=True, timeout=2,
            )
            return [int(pid) for pid in out.stdout.split() if pid.strip()]
        except Exception:
            return []

    def status(self) -> dict:
        reachable, http_status, body = self._http_check()
        managed_alive = self.process is not None and self.process.poll() is None

        if reachable:
            healthy = self.health_ok(body)
            state = "healthy" if healthy else "degraded"
        elif managed_alive:
            state = "starting"
        elif self._port_owner_pids():
            # Something (not us) is holding the port -- e.g. started
            # manually in another terminal, or from a previous run of this
            # panel before it was restarted. Still controllable via Stop.
            state = "running_unmanaged"
        else:
            state = "stopped"

        return {
            "name": self.name,
            "state": state,
            "port": self.port,
            "url": self.health_url.rsplit("/", 1)[0] if "/health" in self.health_url else self.health_url,
            "managed": managed_alive,
            "pid": self.process.pid if managed_alive else None,
            "log_tail": self._tail_log(),
        }

    def _tail_log(self) -> list[str]:
        if not self.log_path.exists():
            return []
        try:
            with open(self.log_path, "r", errors="replace") as f:
                lines = f.readlines()
            return [line.rstrip("\n") for line in lines[-LOG_TAIL_LINES:]]
        except Exception:
            return []

    # -- lifecycle --

    def start(self, cmd: list[str], cwd: Path) -> str:
        with self.lock:
            self.should_run = True
            if self.process is not None and self.process.poll() is None:
                return f"{self.name} is already running (pid {self.process.pid})."
            if not cwd.exists():
                return f"Can't start {self.name}: {cwd} doesn't exist."

            # self.process is None here, but that only means *this object*
            # never started anything -- e.g. right after the panel itself
            # was restarted (a fresh Python process has no memory of pids
            # it spawned last time), or after a crash. The actual OS
            # process from before can still be alive and holding the port.
            # Left alone, that produces exactly the bug this fixes: the new
            # Popen below either fails to bind (tools using --strictPort)
            # or -- worse, for something like cloudflared -- succeeds at
            # first glance while a stale, orphaned instance goes on
            # answering health checks and real traffic with whatever
            # config/state it loaded at ITS OWN long-ago startup. Adopt and
            # kill anything on the port first so every start() is
            # idempotent regardless of how the panel got here. Only kills
            # the specific pid(s) lsof reports, never a process group --
            # see the identical safety note in stop().
            for pid in self._port_owner_pids():
                self._kill_single(pid)

            log_f = open(self.log_path, "a")
            log_f.write(f"\n----- starting at {time.strftime('%Y-%m-%d %H:%M:%S')} -----\n")
            log_f.write(f"$ {' '.join(cmd)}\n")
            log_f.flush()

            self.process = subprocess.Popen(
                cmd, cwd=str(cwd), stdout=log_f, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return f"Starting {self.name} (pid {self.process.pid})…"

    def stop(self) -> str:
        with self.lock:
            self.should_run = False
            killed_any = False

            if self.process is not None and self.process.poll() is None:
                # We spawned this one ourselves with start_new_session=True,
                # so its pgid is exactly its own pid -- safe to killpg the
                # whole tree (kills uvicorn --reload's / vite's child
                # processes along with the parent).
                self._killpg(self.process.pid)
                killed_any = True

            # Also clean up anything else holding the port that we didn't
            # start ourselves (adopted -- e.g. started manually in a
            # terminal, or left over from a previous panel session). We do
            # NOT know this process's group is exclusively its own, so we
            # deliberately kill only the specific pid(s) lsof reports, never
            # its process group -- an adopted process could share a pgid
            # with an unrelated parent shell, and killpg-ing that would take
            # down more than intended (verified this the hard way in
            # testing: it silently killed the terminal driving the test).
            for pid in self._port_owner_pids():
                self._kill_single(pid)
                killed_any = True

            self.process = None
            return f"Stopped {self.name}." if killed_any else f"{self.name} wasn't running."

    def restart(self, cmd: list[str], cwd: Path) -> str:
        self.stop()
        time.sleep(0.5)
        return self.start(cmd, cwd)

    @staticmethod
    def _killpg(pid: int) -> None:
        """Only ever call this on a pid we ourselves spawned with
        start_new_session=True -- see the safety note in stop() above."""
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    return
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            pass

    @staticmethod
    def _kill_single(pid: int) -> None:
        """Kills exactly one pid, never its process group -- the safe
        default for pids we discovered via lsof rather than spawned
        ourselves (see stop())."""
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            pass


def _backend_health_ok(body: bytes) -> bool:
    try:
        return json.loads(body).get("status") == "ok"
    except Exception:
        return False


def _messaging_health_ok(body: bytes) -> bool:
    try:
        return json.loads(body).get("ok") is True
    except Exception:
        return False


backend = ManagedService("backend", BACKEND_PORT, f"{BACKEND_URL}/health", _backend_health_ok)
frontend = ManagedService("frontend", FRONTEND_PORT, FRONTEND_URL)
messaging = ManagedService("messaging", MESSAGING_PORT, f"{MESSAGING_URL}/health", _messaging_health_ok)
# Health-checked via cloudflared's own --metrics /ready endpoint, which only
# returns 200 once the tunnel has an actual established connection to
# Cloudflare's edge -- a real "is this thing working" signal, not just
# "is the process alive" (which a dead credentials file or bad DNS could
# still pass).
cloudflared = ManagedService(
    "cloudflared", CLOUDFLARED_METRICS_PORT,
    f"http://127.0.0.1:{CLOUDFLARED_METRICS_PORT}/ready",
)


def backend_start_cmd() -> list[str]:
    return [_backend_python(), "-m", "uvicorn", "app.main:app", "--reload",
            "--host", "127.0.0.1", "--port", str(BACKEND_PORT)]


def frontend_start_cmd() -> list[str]:
    # --host 127.0.0.1 pins Vite to the IPv4 loopback explicitly. Without
    # it, Vite binds whatever `localhost` resolves to via Node's own DNS
    # resolution -- on newer Node versions that can land on ::1 (IPv6-only),
    # so the server prints "Local: http://localhost:5173/" and looks fine,
    # but nothing is actually listening on 127.0.0.1. That silently breaks
    # both this panel's own health check (_http_check hits FRONTEND_URL =
    # http://127.0.0.1:5173) and cloudflared's ingress rule for app.* (also
    # points at 127.0.0.1/localhost:5173) -- surfaced as frontend being
    # permanently stuck in "starting" during Go live even though the
    # process was alive and had bound *a* socket successfully.
    return ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"]


def frontend_preview_cmd() -> list[str]:
    """Serves the already-built dist/ (production, minified, code-split)
    instead of running the dev server. Used only for the public demo link
    (see _run_go_live) -- `vite dev` serves hundreds of individual
    unbundled ES module files, which is fine on localhost (~1ms round
    trips) but was the actual cause of the public link feeling slow: each
    of those hundreds of file requests was paying a full Cloudflare Tunnel
    round trip (tens of ms) instead of one. `vite preview` serves the same
    single minified, gzippable bundle a real deployment would.

    --host 127.0.0.1 -- see frontend_start_cmd()'s comment; same IPv4-vs-
    IPv6 binding gap applies here, and matters more for this one since it's
    what the public tunnel and the panel's own health check both depend on."""
    return ["npx", "vite", "preview", "--host", "127.0.0.1", "--port", str(FRONTEND_PORT), "--strictPort"]


def messaging_start_cmd() -> list[str]:
    return ["npm", "run", "dev:server"]


def cloudflared_start_cmd() -> list[str]:
    return ["cloudflared", "tunnel", "--config", str(CLOUDFLARED_CONFIG),
            "--metrics", f"127.0.0.1:{CLOUDFLARED_METRICS_PORT}",
            "run", CLOUDFLARED_TUNNEL_NAME]


def _ensure_cloudflared_config() -> tuple[bool, str]:
    """Idempotently makes sure config.yml's ingress list has an entry for
    every hostname this app needs, pointed at the matching port on
    127.0.0.1 -- and, just as importantly, REWRITES any existing entry that
    still says `localhost` instead of `127.0.0.1`. `tunnel:` name and
    `credentials-file:` path (which encode this one tunnel's UUID, created
    by hand with `cloudflared tunnel create`) are left exactly as found.

    The localhost -> 127.0.0.1 normalization is the actual fix for
    chat.prashanthsridhar.com's recurring "502 Bad Gateway": cloudflared
    resolves the ingress rule's `service:` hostname itself, and on this Mac
    "localhost" can resolve to the IPv6 loopback (::1) first -- the exact
    same dual-stack ambiguity that made Vite bind to the wrong address
    earlier (see frontend_start_cmd()'s comment) -- except here it's
    cloudflared's own outbound connection to the origin that's affected,
    not the origin's bind address, so pinning the origin to 127.0.0.1 (as
    frontend/messaging both already do) doesn't fix it by itself. Pointing
    the ingress rule itself at 127.0.0.1 removes the ambiguity at the one
    place that actually matters."""
    if not CLOUDFLARED_CONFIG.exists():
        return False, (
            f"{CLOUDFLARED_CONFIG} doesn't exist. Create the tunnel once by hand first: "
            f"`cloudflared tunnel create {CLOUDFLARED_TUNNEL_NAME}`, then re-run Go live."
        )

    text = CLOUDFLARED_CONFIG.read_text()
    required = [
        (PUBLIC_HOSTNAME_MSG, MESSAGING_PORT),
        (PUBLIC_HOSTNAME_API, BACKEND_PORT),
        (PUBLIC_HOSTNAME_APP, FRONTEND_PORT),
    ]
    notes = []

    # Normalize first -- fixes configs written by an earlier version of this
    # panel (or edited by hand) that used `localhost` instead of
    # `127.0.0.1`. Only touches the exact ports this app owns, so it can't
    # clobber an unrelated ingress rule that happens to also say localhost.
    for _hostname, port in required:
        old = f"service: http://localhost:{port}"
        new = f"service: http://127.0.0.1:{port}"
        if old in text:
            text = text.replace(old, new)
            notes.append(f"normalized :{port} to 127.0.0.1")

    missing = [(h, p) for h, p in required if h not in text]
    if missing:
        lines = text.splitlines()
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if "http_status:404" in line:
                insert_at = i
                break
        new_lines = []
        for hostname, port in missing:
            new_lines.append(f"  - hostname: {hostname}")
            new_lines.append(f"    service: http://127.0.0.1:{port}")
        lines[insert_at:insert_at] = new_lines
        text = "\n".join(lines) + "\n"
        notes.append("added: " + ", ".join(h for h, _ in missing))

    if not notes:
        return True, "Tunnel config already covers all three hostnames on 127.0.0.1."

    CLOUDFLARED_CONFIG.write_text(text if text.endswith("\n") else text + "\n")
    return True, f"Updated {CLOUDFLARED_CONFIG}: " + "; ".join(notes)


def _cloudflared_cleanup(log) -> None:
    """Purges any stale/orphaned connector registrations Cloudflare's edge
    still has for this tunnel. This is the actual fix for the intermittent
    "everything looks fine locally but the public hostname 404s" symptom:
    cloudflared registers each run as its own Connector ID with Cloudflare's
    edge (see its own startup log line "Generated Connector ID: ..."), and a
    clean SIGTERM handles deregistering those -- but ManagedService.stop()
    only waits ~2s before escalating to SIGKILL, and cloudflared can also
    simply crash on its own (a "accept stream listener encountered a
    failure" has been observed happening spontaneously, unrelated to
    anything this panel did). Either way, a connector can be left registered
    at the edge with nothing local backing it. The edge then load-balances
    incoming requests for a hostname across ALL registered connectors,
    including dead ones -- so some fraction of requests (or, worse, all of
    them from a given edge PoP) get routed to a connector that can't serve
    them, which surfaces as a plain "404 Not Found" straight from Cloudflare
    (not a 502 -- the DNS/route level, not a backend failure), even though
    `curl localhost:5173` and the current cloudflared process both look
    completely healthy. Only ever call this with the LOCAL cloudflared
    process already stopped (see the call sites in go_live/go_local below)
    -- cleanup targets connections with no live process behind them, not
    the one you're about to start.
    """
    log(f"Cleaning up any stale connector registrations for tunnel '{CLOUDFLARED_TUNNEL_NAME}'…")
    try:
        out = subprocess.run(
            ["cloudflared", "tunnel", "cleanup", CLOUDFLARED_TUNNEL_NAME],
            capture_output=True, text=True, timeout=20,
        )
        for stream in (out.stdout, out.stderr):
            if stream and stream.strip():
                log(stream.strip())
    except Exception as e:
        log(f"Couldn't run `cloudflared tunnel cleanup`: {e}")


def _ensure_cloudflared_dns_routes(log) -> None:
    """`tunnel route dns` is safe to re-run -- it just reports the record
    already exists if it does. Runs it for api./app. every time Go live
    fires rather than only once, so a hostname added to config.yml above
    but never routed in DNS (e.g. api. was set up by hand in an earlier
    session, app. never was) gets caught automatically."""
    for hostname in (PUBLIC_HOSTNAME_API, PUBLIC_HOSTNAME_APP):
        log(f"Ensuring DNS route for {hostname}…")
        try:
            out = subprocess.run(
                ["cloudflared", "tunnel", "route", "dns", CLOUDFLARED_TUNNEL_NAME, hostname],
                capture_output=True, text=True, timeout=20,
            )
            for stream in (out.stdout, out.stderr):
                if stream and stream.strip():
                    log(stream.strip())
        except Exception as e:
            log(f"Couldn't run `cloudflared tunnel route dns`: {e}")


# ---- Reset all data --------------------------------------------------------
# Wipes the local dev database so testing can start clean (e.g. after
# accounts/invites get into a confusing state). Only handles the default
# SQLite setup -- resolves DATABASE_URL through the backend's own
# app.config rather than hardcoding /tmp/d2m.db, so it still works if
# D2M_DATABASE_URL has been customized, and refuses (rather than guessing)
# if it points somewhere that isn't SQLite.

def _resolved_sqlite_path():
    """Returns (Path, None) on success, or (None, error_message)."""
    try:
        out = subprocess.run(
            [_backend_python(), "-c", "from app.config import DATABASE_URL; print(DATABASE_URL)"],
            cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=5,
        )
        url = out.stdout.strip()
        if not url:
            return None, f"Couldn't read DATABASE_URL from the backend: {out.stderr.strip()[-300:]}"
    except Exception as e:
        return None, f"Couldn't run the backend to resolve its database path: {e}"

    if not url.startswith("sqlite:///"):
        scheme = url.split("://", 1)[0] if "://" in url else url
        return None, (f"DATABASE_URL is '{scheme}://...', not SQLite -- this button only resets the "
                       f"default local SQLite file. Reset that database manually.")

    raw = url[len("sqlite:///"):]  # 4 slashes total -> abs path (leading "/" survives); 3 -> relative
    path = Path(raw) if raw.startswith("/") else (BACKEND_DIR / raw)
    return path, None


def reset_data() -> str:
    db_path, err = _resolved_sqlite_path()
    if err:
        return err

    was_running = backend.process is not None and backend.process.poll() is None
    # Must stop the backend first -- SQLite holds the file open (and may
    # have -wal/-shm sidecar files mid-write), so deleting out from under a
    # live connection risks a half-broken database rather than a clean one.
    backend.stop()
    time.sleep(0.3)

    deleted = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            try:
                p.unlink()
                deleted.append(p.name)
            except Exception as e:
                return f"Stopped the backend but couldn't delete {p}: {e}"

    if deleted:
        msg = f"Deleted: {', '.join(deleted)}."
    else:
        msg = f"No database file found at {db_path} -- nothing to reset."

    if was_running:
        start_msg = backend.start(backend_start_cmd(), BACKEND_DIR)
        msg += f" {start_msg} A fresh empty database is created automatically on startup."
    else:
        msg += " Start the backend when ready -- a fresh empty database is created automatically."
    return msg


# ---- One-off actions (install deps, seed, test) --------------------------
# These run to completion in a background thread; their output streams into
# a shared "activity" log the dashboard polls, separate from the two
# long-running services above.

activity_lock = threading.Lock()
activity_state = {"running": False, "label": None, "log": []}


def _run_activity(label: str, cmd: list[str], cwd: Path) -> None:
    with activity_lock:
        activity_state["running"] = True
        activity_state["label"] = label
        activity_state["log"] = [f"$ {' '.join(cmd)}"]

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        for line in proc.stdout:
            with activity_lock:
                activity_state["log"].append(line.rstrip("\n"))
                activity_state["log"] = activity_state["log"][-LOG_TAIL_LINES:]
        proc.wait()
        with activity_lock:
            activity_state["log"].append(
                f"-- finished, exit code {proc.returncode} --"
            )
    except FileNotFoundError as e:
        with activity_lock:
            activity_state["log"].append(f"-- failed to run: {e} --")
    finally:
        with activity_lock:
            activity_state["running"] = False


def run_activity_async(label: str, cmd: list[str], cwd: Path) -> str:
    if activity_state["running"]:
        return f"Already running: {activity_state['label']}"
    threading.Thread(target=_run_activity, args=(label, cmd, cwd), daemon=True).start()
    return f"Started: {label}"


ACTIONS = {
    "install_backend": lambda: run_activity_async(
        "Install backend dependencies",
        [_backend_python(), "-m", "pip", "install", "-r", "requirements.txt"],
        BACKEND_DIR,
    ),
    "install_frontend": lambda: run_activity_async(
        "Install frontend dependencies", ["npm", "install"], FRONTEND_DIR,
    ),
    "seed": lambda: run_activity_async(
        "Seed demo data", [_backend_python(), "seed.py"], BACKEND_DIR,
    ),
    "test": lambda: run_activity_async(
        "Run backend tests", [_backend_python(), "-m", "pytest", "-q"], BACKEND_DIR,
    ),
    "git_push": lambda: git_push_all(),
    # The messaging server (Postgres + Redis + coturn) needs its infra up
    # before it'll do anything useful -- `npm run dev:server` will start and
    # then just fail every request/crash-loop against a database that isn't
    # there. These run docker compose directly rather than being folded into
    # messaging.start() itself: infra staying up across a server
    # restart/stop is the right default (a restart shouldn't drop the DB
    # connection pool's backing containers), and any failure here (e.g.
    # Docker Desktop isn't running) shows up in the same activity log as
    # every other one-off action instead of silently gating Start.
    "messaging_infra_up": lambda: run_activity_async(
        "Start messaging infra (docker compose)",
        # --wait blocks until postgres/redis report healthy (both have a
        # healthcheck in docker-compose.yml; coturn doesn't, so it's just
        # waited-for as "started") rather than returning the instant the
        # containers exist. Without it, "Running" doesn't mean "actually
        # accepting connections yet" -- Postgres in particular takes a few
        # seconds after container start to finish initializing, and the
        # messaging server (started right after, outside Docker) would
        # otherwise sometimes race it and crash on ECONNREFUSED before it
        # ever got the chance to open its own port.
        ["docker", "compose", "up", "-d", "--wait", "postgres", "redis", "coturn"], MESSAGING_DIR,
    ),
    "messaging_infra_down": lambda: run_activity_async(
        "Stop messaging infra (docker compose)",
        ["docker", "compose", "down"], MESSAGING_DIR,
    ),
}


# ---- Go live: public demo link (Cloudflare Tunnel) ------------------------
# Single button that takes the whole local-only dev setup public: brings up
# messaging's docker infra, starts backend/messaging/frontend, points the
# frontend's .env at the public hostnames instead of 127.0.0.1 (Vite bakes
# VITE_* vars in at startup, so this requires a restart to take effect, not
# just a file edit), and starts the Cloudflare Tunnel that fans all three
# hostnames out to their local ports. "Go local" only undoes the tunnel +
# .env part -- it deliberately leaves backend/messaging/frontend running,
# since going back to local-only dev shouldn't kill your dev servers.
#
# Reuses the same activity_state the other one-off actions stream into
# (see ACTIONS above) rather than inventing a second log channel -- the
# dashboard's existing "Quick actions" log panel is where this shows up.

FRONTEND_ENV_PATH = FRONTEND_DIR / ".env"

# Tracks which command the watchdog (see _watchdog_loop) should use to
# revive the frontend if it dies: the production preview server while a
# public demo link is live, the hot-reload dev server otherwise. Set at the
# same two places _write_frontend_env's `public` argument is set, so it's
# always in sync with what's actually in FRONTEND_ENV_PATH.
_public_mode = False


def _write_frontend_env(public: bool) -> None:
    if public:
        content = (
            f"VITE_API_BASE_URL=https://{PUBLIC_HOSTNAME_API}\n"
            f"VITE_MSG_SERVER_URL=https://{PUBLIC_HOSTNAME_MSG}\n"
            f"VITE_MSG_WS_URL=wss://{PUBLIC_HOSTNAME_MSG}\n"
        )
    else:
        content = (
            f"VITE_API_BASE_URL={BACKEND_URL}\n"
            f"VITE_MSG_SERVER_URL={MESSAGING_URL}\n"
            f"VITE_MSG_WS_URL=ws://127.0.0.1:{MESSAGING_PORT}\n"
        )
    FRONTEND_ENV_PATH.write_text(content)


def _activity_log(line: str) -> None:
    with activity_lock:
        activity_state["log"].append(line)
        activity_state["log"] = activity_state["log"][-LOG_TAIL_LINES:]


def _run_go_live() -> None:
    with activity_lock:
        activity_state["running"] = True
        activity_state["label"] = "Go live (public demo link)"
        activity_state["log"] = []

    if shutil.which("cloudflared") is None:
        _activity_log("cloudflared isn't installed -- run `brew install cloudflared` first, then try again.")
        with activity_lock:
            activity_state["running"] = False
        return

    ok, msg = _ensure_cloudflared_config()
    _activity_log(msg)
    if not ok:
        with activity_lock:
            activity_state["running"] = False
        return

    _ensure_cloudflared_dns_routes(_activity_log)

    _activity_log("Starting messaging infra (docker compose)…")
    try:
        # --wait -- see the identical comment on ACTIONS["messaging_infra_up"]
        # above. This is the path that actually bit us: the messaging server
        # gets started (outside Docker) right after this call returns, and
        # without --wait it was racing Postgres's own startup and losing.
        out = subprocess.run(
            ["docker", "compose", "up", "-d", "--wait", "postgres", "redis", "coturn"],
            cwd=str(MESSAGING_DIR), capture_output=True, text=True, timeout=60,
        )
        for stream in (out.stdout, out.stderr):
            if stream and stream.strip():
                _activity_log(stream.strip())
    except Exception as e:
        _activity_log(f"Docker compose failed: {e} -- is Docker Desktop running?")

    _activity_log(backend.start(backend_start_cmd(), BACKEND_DIR))
    _activity_log(messaging.start(messaging_start_cmd(), MESSAGING_DIR))

    _activity_log("Pointing the frontend at the public URLs…")
    _write_frontend_env(public=True)

    # Build + serve the production bundle instead of the dev server -- see
    # frontend_preview_cmd()'s docstring. `npm run build` bakes the .env
    # values just written into the bundle (Vite inlines VITE_* at build
    # time), so this must happen after _write_frontend_env above, not
    # before.
    _activity_log("Building production bundle (this can take a little while)…")
    try:
        out = subprocess.run(
            ["npm", "run", "build"], cwd=str(FRONTEND_DIR),
            capture_output=True, text=True, timeout=180,
        )
        for stream in (out.stdout, out.stderr):
            if stream and stream.strip():
                _activity_log(stream.strip()[-2000:])
        if out.returncode != 0:
            _activity_log(f"Build failed (exit {out.returncode}) -- see output above. Not starting the preview server.")
            with activity_lock:
                activity_state["running"] = False
            return
    except Exception as e:
        _activity_log(f"Couldn't run the build: {e}")
        with activity_lock:
            activity_state["running"] = False
        return

    _activity_log("Serving the production build…")
    frontend.stop()
    global _public_mode
    _public_mode = True
    _activity_log(frontend.start(frontend_preview_cmd(), FRONTEND_DIR))

    # Always fully stop + cleanup before starting a fresh cloudflared, even
    # if it looks like nothing's running -- see _cloudflared_cleanup's
    # docstring for why a stale connector can be left registered at
    # Cloudflare's edge with no local process behind it, and why that's the
    # actual cause of "public hostname 404s even though localhost is fine."
    # Cheap and idempotent when there's nothing to clean up, so this runs
    # unconditionally on every Go live rather than only when something looks
    # wrong.
    _activity_log(cloudflared.stop())
    _cloudflared_cleanup(_activity_log)
    _activity_log(cloudflared.start(cloudflared_start_cmd(), Path.home()))

    _activity_log("Waiting for everything to come up…")
    watched = [backend, frontend, messaging, cloudflared]
    deadline = time.time() + 30
    while time.time() < deadline:
        states = {s.name: s.status()["state"] for s in watched}
        if all(v in ("healthy", "running_unmanaged") for v in states.values()):
            break
        time.sleep(1)
    states = {s.name: s.status()["state"] for s in watched}
    _activity_log(f"Status: {states}")
    if all(v in ("healthy", "running_unmanaged") for v in states.values()):
        _activity_log(f"Live: https://{PUBLIC_HOSTNAME_APP}")
    else:
        _activity_log("Not all services are healthy yet -- check each service card's own log above for why.")

    with activity_lock:
        activity_state["running"] = False


def go_live() -> str:
    if activity_state["running"]:
        return f"Already running: {activity_state['label']}"
    threading.Thread(target=_run_go_live, daemon=True).start()
    return "Started: Go live (public demo link)"


def _run_go_local() -> None:
    with activity_lock:
        activity_state["running"] = True
        activity_state["label"] = "Go local (stop public access)"
        activity_state["log"] = []

    _activity_log(cloudflared.stop())
    _cloudflared_cleanup(_activity_log)
    _write_frontend_env(public=False)
    global _public_mode
    _public_mode = False
    _activity_log("Switching the frontend back to the dev server (hot reload)…")
    _activity_log(frontend.restart(frontend_start_cmd(), FRONTEND_DIR))
    _activity_log("Public demo link is down. Backend, messaging, and frontend are still running locally.")

    with activity_lock:
        activity_state["running"] = False


def go_local() -> str:
    if activity_state["running"]:
        return f"Already running: {activity_state['label']}"
    threading.Thread(target=_run_go_local, daemon=True).start()
    return "Started: Go local (stop public access)"


# ---- Watchdog: auto-heal services that die on their own -------------------
# Everything above (docker --wait, --host 127.0.0.1, the orphan-reclaim in
# ManagedService.start(), the ingress localhost->127.0.0.1 normalization,
# the tunnel-cleanup-before-start) removes *known* ways a service fails to
# come up cleanly. None of it covers a service that comes up fine and then
# dies later on its own -- a lost Postgres connection after a container
# restart, an uncaught exception in the messaging server, cloudflared's own
# "accept stream listener encountered a failure" crash (seen for real
# earlier in this same setup). Nothing was watching for that: the service
# would just sit there dead until someone happened to notice and click
# Restart by hand -- which is exactly the "fails almost every single time"
# experience being fixed here. This loop is the general fix: it doesn't
# matter *why* a service died, only that something that should be running
# no longer is.
WATCHDOG_INTERVAL_S = 8
WATCHDOG_MAX_CONSECUTIVE_RESTARTS = 5

_watchdog_failure_counts: dict[str, int] = {}


def _watchdog_targets() -> list[tuple[ManagedService, list[str], Path]]:
    """Recomputed on every tick (rather than a static list) so the frontend
    entry always uses whichever command matches the CURRENT mode (dev server
    locally, preview server while a public demo link is live) -- a stale
    static list would revive a crashed frontend into the wrong mode."""
    return [
        (backend, backend_start_cmd(), BACKEND_DIR),
        (frontend, frontend_preview_cmd() if _public_mode else frontend_start_cmd(), FRONTEND_DIR),
        (messaging, messaging_start_cmd(), MESSAGING_DIR),
    ]


def _watchdog_heal_cloudflared() -> None:
    """cloudflared gets the same stop+cleanup+start sequence Go live/local
    already use by hand -- not a plain restart -- since a crash is exactly
    the case _cloudflared_cleanup exists for (a dead connector left
    registered at Cloudflare's edge)."""
    def log(line: str) -> None:
        with open(cloudflared.log_path, "a") as f:
            f.write(line + "\n")

    log(f"[watchdog] cloudflared isn't running but should be -- cleaning up and restarting…")
    cloudflared.stop()
    _cloudflared_cleanup(log)
    cloudflared.start(cloudflared_start_cmd(), Path.home())


def _watchdog_loop() -> None:
    while True:
        time.sleep(WATCHDOG_INTERVAL_S)
        try:
            targets = _watchdog_targets()
            if cloudflared.should_run:
                targets = targets + [(cloudflared, [], Path.home())]  # cmd/cwd unused for cloudflared, see below

            for svc, cmd, cwd in targets:
                if not svc.should_run:
                    continue
                state = svc.status()["state"]
                if state in ("healthy", "degraded", "running_unmanaged", "starting"):
                    # Degraded/starting are transient or a different class of
                    # problem (wrong response, still booting) -- the process
                    # IS there, so this isn't the "silently died" case this
                    # loop exists for. Any sign of life resets the backoff
                    # counter, so a later, unrelated crash gets the full
                    # retry budget again rather than inheriting an old streak.
                    _watchdog_failure_counts[svc.name] = 0
                    continue
                if state != "stopped":
                    continue

                count = _watchdog_failure_counts.get(svc.name, 0)
                if count >= WATCHDOG_MAX_CONSECUTIVE_RESTARTS:
                    continue  # already logged the give-up message below once

                count += 1
                _watchdog_failure_counts[svc.name] = count
                with open(svc.log_path, "a") as f:
                    f.write(f"\n[watchdog] {svc.name} stopped unexpectedly -- "
                            f"auto-restarting (attempt {count}/{WATCHDOG_MAX_CONSECUTIVE_RESTARTS})…\n")

                if svc is cloudflared:
                    _watchdog_heal_cloudflared()
                else:
                    svc.start(cmd, cwd)

                if count == WATCHDOG_MAX_CONSECUTIVE_RESTARTS:
                    with open(svc.log_path, "a") as f:
                        f.write(f"[watchdog] {svc.name} has failed {count} restarts in a row -- "
                                f"giving up auto-restarting it until it's healthy again or someone "
                                f"restarts it by hand from the panel (check the log above for why "
                                f"it keeps dying).\n")
        except Exception as e:
            # The watchdog itself must never take the panel down -- log to
            # stderr (visible in the terminal running control_panel.py) and
            # keep looping.
            print(f"[watchdog] unexpected error: {e}")


# ---- Push all changes to git -----------------------------------------------
# Commits + pushes both repos in one click. Runs on the user's own machine
# under their own already-configured git identity/credentials/remotes (see
# `git remote -v` in each repo) -- nothing about this touches any sandbox or
# CI credentials, it's just the same `git add/commit/push` the user would
# type themselves, run from a button instead.
#
# Each repo is handled independently and never raises out of its own step:
# a failure committing/pushing the backend still lets the frontend attempt
# run, and the log shows exactly which command produced which output so a
# failure (e.g. remote has diverged and needs a pull/rebase first, or the
# credential helper needs a fresh token) is diagnosable from the panel
# itself rather than a bare "it didn't work." Deliberately does not force-
# push, rebase, or otherwise try to resolve a rejected push automatically --
# that's exactly the kind of history-rewriting action a one-click button
# should never do silently.

def _run_git_cmd(args: list[str], repo_dir: Path, log: list[str]) -> int:
    cmd = ["git", "-C", str(repo_dir)] + args
    log.append(f"$ {' '.join(cmd)}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except Exception as e:
        log.append(f"-- failed to run: {e} --")
        return 1
    for stream in (out.stdout, out.stderr):
        if stream and stream.strip():
            log.extend(stream.rstrip("\n").split("\n"))
    return out.returncode


def _git_push_repo(repo_label: str, repo_dir: Path, commit_message: str) -> list[str]:
    log = [f"\n----- {repo_label} ({repo_dir}) -----"]

    if not repo_dir.exists():
        log.append(f"-- {repo_dir} doesn't exist, skipping --")
        return log

    status_cmd = ["git", "-C", str(repo_dir), "status", "--porcelain"]
    try:
        status_out = subprocess.run(status_cmd, capture_output=True, text=True, timeout=15)
    except Exception as e:
        log.append(f"-- couldn't check git status: {e} --")
        return log

    if status_out.stdout.strip():
        log.append(f"$ git -C {repo_dir} status --porcelain")
        log.extend(status_out.stdout.rstrip("\n").split("\n"))
        rc = _run_git_cmd(["add", "-A"], repo_dir, log)
        if rc == 0:
            _run_git_cmd(["commit", "-m", commit_message], repo_dir, log)
        # Push is attempted below regardless of whether the commit above
        # succeeded -- e.g. a prior local commit from outside the panel
        # might already be sitting there unpushed even with nothing new
        # to commit right now.
    else:
        log.append("Nothing to commit -- working tree clean.")

    # -u/--set-upstream is harmless on a branch that's already tracking
    # (it just reconfirms the same upstream) and is what actually saves a
    # brand-new local branch from "has no upstream branch" -- caught by
    # testing this against a from-scratch repo before wiring it in here.
    try:
        branch = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        branch = ""

    push_args = ["push", "-u", "origin", branch] if branch else ["push"]
    rc = _run_git_cmd(push_args, repo_dir, log)
    log.append(f"{repo_label}: pushed." if rc == 0 else f"{repo_label}: push failed -- see output above.")
    return log


def _run_git_push_all(commit_message: str) -> None:
    with activity_lock:
        activity_state["running"] = True
        activity_state["label"] = "Push all changes to git"
        activity_state["log"] = [f"Committing + pushing {BACKEND_DIR.name}, {FRONTEND_DIR.name}, and {MESSAGING_DIR.name}…"]

    for repo_label, repo_dir in (("backend", BACKEND_DIR), ("frontend", FRONTEND_DIR), ("messaging", MESSAGING_DIR)):
        lines = _git_push_repo(repo_label, repo_dir, commit_message)
        with activity_lock:
            activity_state["log"].extend(lines)
            activity_state["log"] = activity_state["log"][-LOG_TAIL_LINES:]

    with activity_lock:
        activity_state["log"].append("-- done --")
        activity_state["running"] = False


def git_push_all() -> str:
    if activity_state["running"]:
        return f"Already running: {activity_state['label']}"
    commit_message = f"Control panel: sync {time.strftime('%Y-%m-%d %H:%M:%S')}"
    threading.Thread(target=_run_git_push_all, args=(commit_message,), daemon=True).start()
    return "Started: Push all changes to git"


# ---- Accounts console -----------------------------------------------------
# Server-side proxy to GET /admin/accounts on the backend. Done here rather
# than fetched directly from the browser so the dashboard page (served from
# 127.0.0.1:8765) never has to be added to the backend's CORS allowlist
# just for this -- the control panel process makes the request itself and
# just relays the JSON, same pattern as /api/status.

def fetch_accounts() -> dict:
    try:
        with urllib.request.urlopen(f"{BACKEND_URL}/admin/accounts", timeout=2) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"error": "Not available -- /admin/accounts isn't registered "
                              "(this happens if D2M_ENVIRONMENT=production is set)."}
        return {"error": f"Backend returned HTTP {e.code}."}
    except Exception:
        return {"error": "Backend isn't reachable -- start it first."}


def fetch_invite_link(sponsor_id: str) -> dict:
    """
    Server-side proxy to POST /admin/sponsors/{id}/invite-link -- recovers
    a sponsor's claim link after it's been lost client-side (HandoffScreen
    only ever shows it once; there's no login layer to see it again).
    Same CORS-avoidance reasoning as fetch_accounts() above. Adds
    claim_url (frontend-origin /claim/{token} link, ready to paste) on
    success, since the backend only knows the raw token.
    """
    req = urllib.request.Request(
        f"{BACKEND_URL}/admin/sponsors/{sponsor_id}/invite-link",
        method="POST", data=b"",
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = None
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            pass
        if e.code == 409:
            return {"error": detail or "This sponsor's invite has already been claimed."}
        if detail and detail != "Not Found":
            # Our own HTTPException(404, "sponsor not found") -- a real,
            # specific 404, not "route doesn't exist".
            return {"error": detail}
        return {"error": "Not available -- /admin/sponsors/.../invite-link isn't registered "
                          "(this happens if D2M_ENVIRONMENT=production is set)."}
    except Exception:
        return {"error": "Backend isn't reachable -- start it first."}

    body["claim_url"] = f"{FRONTEND_URL}/claim/{body['invite_token']}"
    return body


def fetch_match_diagnostic(primary_a_id: str, primary_b_id: str) -> dict:
    """
    Server-side proxy to GET /admin/match-diagnostic/{a}/{b} -- answers
    "why does A see B but B doesn't see A in Discover" by walking the same
    per-side hard-filter checks the matching engine runs, for this exact
    pair, in both directions. Same CORS-avoidance reasoning as the other
    fetch_* proxies above.
    """
    try:
        with urllib.request.urlopen(
            f"{BACKEND_URL}/admin/match-diagnostic/{primary_a_id}/{primary_b_id}", timeout=3,
        ) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = None
        try:
            detail = json.loads(e.read()).get("detail")
        except Exception:
            pass
        if detail and detail != "Not Found":
            return {"error": detail}
        return {"error": "Not available -- /admin/match-diagnostic isn't registered "
                          "(this happens if D2M_ENVIRONMENT=production is set)."}
    except Exception:
        return {"error": "Backend isn't reachable -- start it first."}


# ---- HTTP server ----------------------------------------------------------

INDEX_HTML_PATH = Path(__file__).parent / "dashboard.html"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet -- errors still surface via /api/status

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            html = INDEX_HTML_PATH.read_text()
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if self.path == "/api/status":
            with activity_lock:
                activity_copy = dict(activity_state)
            self._json({
                "backend": backend.status(),
                "frontend": frontend.status(),
                "messaging": messaging.status(),
                "cloudflared": cloudflared.status(),
                "activity": activity_copy,
                "paths": {"backend": str(BACKEND_DIR), "frontend": str(FRONTEND_DIR), "messaging": str(MESSAGING_DIR)},
                "public": {"app": PUBLIC_HOSTNAME_APP, "api": PUBLIC_HOSTNAME_API, "msg": PUBLIC_HOSTNAME_MSG},
            })
            return

        if self.path == "/api/accounts":
            return self._json(fetch_accounts())

        if self.path.startswith("/api/match-diagnostic/"):
            parts = self.path[len("/api/match-diagnostic/"):].split("/")
            if len(parts) != 2 or not all(parts):
                return self._json({"error": "Need both a primary id and a candidate id."}, status=400)
            return self._json(fetch_match_diagnostic(parts[0], parts[1]))

        self.send_error(404)

    def do_POST(self):
        if self.path == "/api/backend/start":
            return self._json({"message": backend.start(backend_start_cmd(), BACKEND_DIR)})
        if self.path == "/api/backend/stop":
            return self._json({"message": backend.stop()})
        if self.path == "/api/backend/restart":
            return self._json({"message": backend.restart(backend_start_cmd(), BACKEND_DIR)})

        if self.path == "/api/frontend/start":
            return self._json({"message": frontend.start(frontend_start_cmd(), FRONTEND_DIR)})
        if self.path == "/api/frontend/stop":
            return self._json({"message": frontend.stop()})
        if self.path == "/api/frontend/restart":
            return self._json({"message": frontend.restart(frontend_start_cmd(), FRONTEND_DIR)})

        if self.path == "/api/messaging/start":
            return self._json({"message": messaging.start(messaging_start_cmd(), MESSAGING_DIR)})
        if self.path == "/api/messaging/stop":
            return self._json({"message": messaging.stop()})
        if self.path == "/api/messaging/restart":
            return self._json({"message": messaging.restart(messaging_start_cmd(), MESSAGING_DIR)})

        if self.path.startswith("/api/action/"):
            key = self.path.rsplit("/", 1)[-1]
            action = ACTIONS.get(key)
            if action is None:
                return self._json({"message": f"Unknown action: {key}"}, status=404)
            return self._json({"message": action()})

        if self.path.startswith("/api/invite-link/"):
            sponsor_id = self.path.rsplit("/", 1)[-1]
            return self._json(fetch_invite_link(sponsor_id))

        if self.path == "/api/reset-data":
            return self._json({"message": reset_data()})

        if self.path == "/api/go-live":
            return self._json({"message": go_live()})
        if self.path == "/api/go-local":
            return self._json({"message": go_local()})

        self.send_error(404)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def main():
    if not _port_free(PANEL_PORT):
        print(f"Port {PANEL_PORT} is already in use -- the control panel may already be running.")
        print(f"Opening http://127.0.0.1:{PANEL_PORT} in your browser…")
        webbrowser.open(f"http://127.0.0.1:{PANEL_PORT}")
        return

    server = ThreadingHTTPServer(("127.0.0.1", PANEL_PORT), Handler)
    url = f"http://127.0.0.1:{PANEL_PORT}"
    print(f"D2M Control Panel running at {url}")
    print("Press Ctrl+C to stop the panel itself (backend/frontend you started keep running).")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    threading.Thread(target=_watchdog_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nControl panel stopped. Backend/frontend processes it started are still running.")


if __name__ == "__main__":
    main()
