import ast
import json
import logging
import subprocess
import sqlite3
import time
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlparse

import click
import lz4.block  # pyright: ignore[reportMissingTypeStubs]

# Setup basic logging to replace 'pass' in try-except blocks
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Noise Filter for C++/Python Devs ---
EXCLUDE_DIRS: set[str] = {
    ".git",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    ".venv",
    "venv",
    "obj",
    "venv310",
    "venv311",
    "venv313",
}


class WindowInfo(TypedDict):
    """Type definition for window information."""

    cls: str
    title: str
    pid: int | None


def get_minikube_services() -> list[str]:
    """Returns names and namespaces of active Minikube services."""
    try:
        result = subprocess.run(
            ["/usr/bin/minikube", "service", "list", "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        # Use dict[str, object] instead of Any for type safety
        raw_services: list[dict[str, object]] = json.loads(result.stdout)
        return [f"{s.get('Namespace')}/{s.get('Name')}" for s in raw_services]
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError) as e:
        msg = f"Minikube error: {e}"
        logger.debug(msg)
        return []


def get_docker_status() -> list[str]:
    """Returns names and images of running Docker containers."""
    try:
        result = subprocess.run(
            ["/usr/bin/docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.strip().split("\n") if line]
    except Exception as e:
        msg = f"Docker error: {e}"
        logger.debug(msg)
        return []


def get_active_tmux_sessions() -> list[str]:
    """Returns list of active tmux session names."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/tmux",
                "list-sessions",
                "-F",
                "#{session_attached} #{session_name}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return [
            line.split(" ", 1)[1]
            for line in result.stdout.strip().split("\n")
            if line.startswith("1")
        ]
    except Exception:
        return []


def get_recent_file_activity(path_str: str, lookback_seconds: int = 600) -> list[str]:
    """Walks directory to find recently modified files."""
    path = Path(path_str)
    if not path_str or not path.exists() or path == Path.home():
        return []

    now = time.time()
    recent_files: list[str] = []

    try:
        base_depth = len(path.parts)
        for p in path.rglob("*"):
            if any(part in EXCLUDE_DIRS for part in p.parts):
                continue
            if len(p.parts) - base_depth > 2:
                continue

            try:
                if p.is_file() and (now - p.stat().st_mtime < lookback_seconds):
                    recent_files.append(str(p.relative_to(path)))
            except (PermissionError, FileNotFoundError):
                continue

        return recent_files[:20]
    except Exception:
        return []


def get_active_dev_tools() -> list[str]:
    """Checks for running development processes."""
    watch_list = ["node", "npm", "docker", "pytest", "python", "gcc", "make", "cmake"]
    try:
        ps_output = subprocess.check_output(
            ["/usr/bin/ps", "-A", "-o", "comm="], text=True
        )
        running = set(ps_output.split())
        return [tool for tool in watch_list if tool in running]
    except Exception:
        return []


def get_firefox_context() -> dict[str, list[str]]:
    """Extracts open tabs from Firefox session store."""
    home = Path.home()
    # Search for recovery file
    files = list(
        (home / ".mozilla/firefox").glob("*/sessionstore-backups/recovery.jsonlz4")
    )

    if not files:
        return {"titles": [], "domains": []}

    titles: set[str] = set()
    domains: set[str] = set()

    try:
        with files[0].open("rb") as f:
            _ = f.read(8)  # Skip "mozLz40" magic number (assigned to _ for Pyright)
            decompressed_bytes: bytes = lz4.block.decompress(f.read())
            data: dict[str, object] = json.loads(decompressed_bytes.decode("utf-8"))

            windows: list[dict[str, object]] = data.get("windows", [])
            for win in windows:
                tabs: list[dict[str, object]] = win.get("tabs", [])
                for tab in tabs:
                    entries: list[dict[str, object]] = tab.get("entries", [])
                    if entries:
                        last_entry = entries[-1]
                        url = str(last_entry.get("url", ""))
                        title = str(last_entry.get("title", ""))
                        if title:
                            titles.add(title)
                        if url:
                            domains.add(urlparse(url).netloc)
    except Exception as e:
        msg = f"Firefox data extraction failed: {e}"
        logger.debug(msg)

    return {"titles": list(titles), "domains": list(domains)}


def get_focused_window_info() -> WindowInfo:
    """Uses gdbus to get focused window info on GNOME."""
    default: WindowInfo = {"cls": "unknown", "title": "unknown", "pid": None}
    try:
        result = subprocess.run(
            [
                "/usr/bin/gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell",
                "--object-path",
                "/org/gnome/Shell/Extensions/WindowsExt",
                "--method",
                "org.gnome.Shell.Extensions.WindowsExt.List",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        # Parse GNOME's GVariant output
        raw_output = ast.literal_eval(result.stdout.strip())
        if not raw_output or not isinstance(raw_output, tuple):
            return default

        windows = cast(list[dict[str, object]], json.loads(raw_output[0]))

        for win in windows:
            if win.get("focus"):
                return {
                    "cls": str(win.get("class", "")),
                    "title": str(win.get("title", "")),
                    "pid": cast(int | None, win.get("pid")),
                }
    except Exception as e:
        msg = f"GNOME Window lookup failed: {e}"
        logger.debug(msg)

    return default


def collect_snapshot() -> dict[str, object]:
    """Aggregates all data into a single dictionary."""
    window = get_focused_window_info()
    cwd = str(Path.cwd())
    fx = get_firefox_context()

    return {
        "timestamp": int(time.time()),
        "focused_app": window["cls"],
        "docker_services": get_docker_status(),
        "minikube_services": get_minikube_services(),
        "cwd": cwd,
        "active_dev_tools": get_active_dev_tools(),
        "recent_files": get_recent_file_activity(cwd),
        "browser_domains": fx["domains"],
        "tmux_sessions": get_active_tmux_sessions(),
        "firefox_tabs": fx["titles"],
    }


DB_PATH = "work_activity.db"


def init_db(db_path: str):
    """Initializes the SQLite database and creates the schema."""
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER,
                focused_app TEXT,
                cwd TEXT,
                docker_services TEXT,
                minikube_services TEXT,
                active_dev_tools TEXT,
                recent_files TEXT,
                browser_domains TEXT,
                tmux_sessions TEXT,
                firefox_tabs TEXT,
                jira_ticket TEXT DEFAULT NULL
            )
        """)
        conn.commit()


def log_to_db(db_path: str, data: dict):
    """Inserts a single snapshot into the database."""
    # We convert lists to JSON strings to store them in SQLite TEXT columns
    query = """
        INSERT INTO activity_logs (
            timestamp, focused_app, cwd, docker_services, minikube_services,
            active_dev_tools, recent_files, browser_domains, tmux_sessions, firefox_tabs
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    params = (
        data["timestamp"],
        data["focused_app"],
        data["cwd"],
        json.dumps(data["docker_services"]),
        json.dumps(data["minikube_services"]),
        json.dumps(data["active_dev_tools"]),
        json.dumps(data["recent_files"]),
        json.dumps(data["browser_domains"]),
        json.dumps(data["tmux_sessions"]),
        json.dumps(data["firefox_tabs"]),
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(query, params)


@click.command()
@click.option("--db", default=DB_PATH, help="Path to SQLite database file.")
@click.option("--interval", "-i", default=10, help="Seconds between snapshots.")
def main(db: str, interval: int):
    """Logs system activity to SQLite for Jira task classification."""
    click.secho("🖥️  ML Data Collector Active", fg="cyan", bold=True)
    click.echo(f"Storing data in: {Path(db).absolute()}")

    init_db(db)

    try:
        while True:
            snapshot = collect_snapshot()
            log_to_db(db, snapshot)

            # Subtle heartbeat in the console
            current_time = time.strftime("%H:%M:%S", time.localtime())
            click.echo(
                f"[{current_time}] Snapshot saved (App: {snapshot['focused_app']})"
            )

            time.sleep(interval)
    except KeyboardInterrupt:
        click.secho("\nStopping gracefully. Data is safe.", fg="yellow")


if __name__ == "__main__":
    main()
