import ast
import json
import logging
import os
import re
import subprocess
import sqlite3
import sys
import time
import requests
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlparse

import click
import lz4.block  # pyright: ignore[reportMissingTypeStubs]

# Setup basic logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("WorkTracker")

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

EXCLUDE_FILES: set[str] = {
    "work_activity.db",
    ".zsh_history",
    ".bash_history",
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
            ["minikube", "service", "list", "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        raw_services: list[dict[str, object]] = json.loads(result.stdout)
        return [f"{s.get('Namespace')}/{s.get('Name')}" for s in raw_services]
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError) as e:
        logger.debug(f"Minikube error: {e}")
        return []


def get_docker_status() -> list[str]:
    """Returns names and images of running Docker containers."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return [line for line in result.stdout.strip().split("\n") if line]
    except Exception as e:
        logger.debug(f"Docker error: {e}")
        return []


def get_active_tmux_sessions() -> list[str]:
    """Returns list of active tmux session names."""
    try:
        result = subprocess.run(
            [
                "tmux",
                "list-sessions",
                "-F",
                "#{session_attached} #{session_name}",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return [
            line.split(" ", 1)[1]
            for line in result.stdout.strip().split("\n")
            if line.startswith("1")
        ]
    except Exception:
        return []


def get_active_cwd(pid: int | None, app_class: str) -> str:
    """Reads the precise working directory, with explicit support for Tmux."""
    if app_class.lower() in [
        "kitty",
        "alacritty",
        "gnome-terminal",
        "xterm",
        "wezterm",
    ]:
        try:
            tmux_path = subprocess.run(
                ["tmux", "display-message", "-p", "#{pane_current_path}"],
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            if tmux_path and Path(tmux_path).exists():
                return tmux_path
        except Exception:
            pass

    if not pid:
        return str(Path.home())

    current_pid = str(pid)
    known_shells = {"bash", "zsh", "fish", "sh"}

    try:
        while True:
            children_file = Path(f"/proc/{current_pid}/task/{current_pid}/children")
            if not children_file.exists():
                break

            children = children_file.read_text().strip().split()
            if not children:
                break

            next_pid = None
            for child in children:
                try:
                    comm = Path(f"/proc/{child}/comm").read_text().strip()
                    if comm in known_shells:
                        next_pid = child
                        break
                except OSError:
                    continue

            if not next_pid:
                next_pid = children[0]

            current_pid = next_pid

        return os.readlink(f"/proc/{current_pid}/cwd")
    except OSError:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return str(Path.home())


def get_recent_file_activity(
    path_str: str, last_time: float, current_time: float
) -> list[str]:
    """
    Walks directory to find files modified between snapshots using precise timestamps.
    FIXED: Prevents recursive open file descriptor stacking.
    """
    path = Path(path_str)
    if not path_str or not path.exists():
        return []

    recent_files: list[tuple[float, str]] = []

    def scan_dir(current_path: str, depth: int):
        if depth > 2:  # Limit depth
            return

        dirs_to_scan = []
        try:
            # Context manager closes properly before we dive into subdirectories
            with os.scandir(current_path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in EXCLUDE_DIRS:
                            dirs_to_scan.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        if entry.name in EXCLUDE_FILES:
                            continue
                        try:
                            stat = entry.stat()
                            if last_time <= stat.st_mtime <= current_time:
                                rel_path = os.path.relpath(entry.path, path_str)
                                recent_files.append((stat.st_mtime, rel_path))
                        except OSError:
                            continue
        except OSError:
            pass

        # Recursively scan found directories outside the open context
        for d in dirs_to_scan:
            scan_dir(d, depth + 1)

    scan_dir(path_str, 0)

    recent_files.sort(key=lambda x: x[0], reverse=True)
    return [f[1] for f in recent_files[:10]]


def get_active_dev_tools() -> list[str]:
    """Checks for running development processes."""
    watch_list = ["node", "npm", "docker", "pytest", "python", "gcc", "make", "cmake"]
    try:
        ps_output = subprocess.check_output(
            ["ps", "-A", "-o", "comm="], text=True, timeout=5
        )
        running = set(ps_output.split())
        return [tool for tool in watch_list if tool in running]
    except Exception:
        return []


def get_firefox_context() -> dict[str, list[str]]:
    """Extracts open tabs from Firefox session store."""
    home = Path.home()
    files = list(
        (home / ".mozilla/firefox").glob("*/sessionstore-backups/recovery.jsonlz4")
    )

    if not files:
        return {"titles": [], "domains": []}

    titles: set[str] = set()
    domains: set[str] = set()

    try:
        latest_file = max(files, key=lambda p: p.stat().st_mtime)

        with latest_file.open("rb") as f:
            f.read(8)  # Skip magic number
            decompressed_bytes: bytes = lz4.block.decompress(f.read())
            data: dict[str, object] = json.loads(decompressed_bytes.decode("utf-8"))

            windows: list[dict[str, object]] = data.get("windows", [])  # type: ignore
            for win in windows:
                tabs: list[dict[str, object]] = win.get("tabs", [])  # type: ignore
                for tab in tabs:
                    entries: list[dict[str, object]] = tab.get("entries", [])  # type: ignore
                    if entries:
                        last_entry = entries[-1]
                        url = str(last_entry.get("url", ""))
                        title = str(last_entry.get("title", ""))
                        if title:
                            titles.add(title)
                        if url:
                            domains.add(urlparse(url).netloc)
    except Exception as e:
        logger.debug(f"Firefox extraction failed: {e}")

    return {"titles": list(titles), "domains": list(domains)}


def get_focused_window_info() -> WindowInfo:
    """Uses gdbus to get focused window info on GNOME."""
    default: WindowInfo = {"cls": "unknown", "title": "unknown", "pid": None}
    try:
        result = subprocess.run(
            [
                "gdbus",
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
            timeout=5,
        )
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
        logger.debug(f"GNOME Window lookup failed: {e}")

    return default


def extract_jira_ticket(*sources: str) -> str | None:
    """Attempts to find a Jira ticket key (e.g., PROJ-123) in the provided strings."""
    pattern = re.compile(r"([A-Z]+-\d+)")
    for source in sources:
        if not source:
            continue
        match = pattern.search(source)
        if match:
            return match.group(1)
    return None


def collect_snapshot(last_time: float, current_time: float) -> dict[str, object]:
    """Aggregates all data into a single dictionary."""
    window = get_focused_window_info()
    cwd = get_active_cwd(window["pid"], window["cls"])
    fx = get_firefox_context()
    tmux = get_active_tmux_sessions()

    # Attempt to extract Jira ticket from window title, directory path, or tmux sessions
    ticket = extract_jira_ticket(window["title"], cwd, " ".join(tmux))

    snapshot = {
        "timestamp": int(current_time),
        "focused_app": window["cls"],
        "docker_services": get_docker_status(),
        "minikube_services": get_minikube_services(),
        "cwd": cwd,
        "active_dev_tools": get_active_dev_tools(),
        "recent_files": get_recent_file_activity(cwd, last_time, current_time),
        "browser_domains": fx["domains"],
        "tmux_sessions": tmux,
        "firefox_tabs": fx["titles"],
        "jira_ticket": ticket,
    }

    logger.info(
        f"Successfully extracted snapshot for app: {snapshot['focused_app']} in {cwd}"
    )
    return snapshot


def init_db(db_path: str):
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS activity_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp INTEGER, focused_app TEXT,
                cwd TEXT, docker_services TEXT, minikube_services TEXT, active_dev_tools TEXT,
                recent_files TEXT, browser_domains TEXT, tmux_sessions TEXT, firefox_tabs TEXT,
                jira_ticket TEXT DEFAULT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jira_tasks_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT,
                summary TEXT,
                status TEXT,
                timestamp INTEGER
            )
        """)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, last_run INTEGER)"
        )
        conn.commit()


def log_to_db(db_path: str, data: dict):
    query = """
        INSERT INTO activity_logs (
            timestamp, focused_app, cwd, docker_services, minikube_services,
            active_dev_tools, recent_files, browser_domains, tmux_sessions, firefox_tabs,
            jira_ticket
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        data["jira_ticket"],
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(query, params)


def fetch_and_store_jira_tasks(db_path: str, url: str, token: str) -> bool:
    """Fetches active tasks from Jira and appends state changes to the ledger."""
    api_url = f"{url.rstrip('/')}/rest/api/2/search"
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}

    query = {
        "jql": "assignee = currentUser() AND (status NOT IN (Done, Suspended) OR updated >= -1d)",
        "fields": "summary,status",
    }

    try:
        response = requests.get(api_url, headers=headers, params=query, timeout=15)

        if "application/json" not in response.headers.get("Content-Type", ""):
            logger.error(
                f"Unexpected response content type: {response.headers.get('Content-Type')}"
            )
            return False

        response.raise_for_status()
        data = response.json()
        now = int(time.time())

        issues = data.get("issues", [])
        if not issues:
            return True

        with sqlite3.connect(db_path) as conn:
            # FIXED: Eliminate N+1 by pre-fetching the latest states for all returned keys
            keys = [issue["key"] for issue in issues]
            placeholders = ",".join(["?"] * len(keys))

            # Ordering by timestamp ASC means the last row processed in the comprehension is the latest state
            cursor = conn.execute(
                f"SELECT key, summary, status FROM jira_tasks_ledger WHERE key IN ({placeholders}) ORDER BY timestamp ASC",
                tuple(keys),
            )
            latest_states = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

            for issue in issues:
                key = issue["key"]
                summary = issue["fields"]["summary"]
                status = issue["fields"]["status"]["name"]

                last_state = latest_states.get(key)

                if (
                    not last_state
                    or last_state[0] != summary
                    or last_state[1] != status
                ):
                    conn.execute(
                        """
                        INSERT INTO jira_tasks_ledger (key, summary, status, timestamp) 
                        VALUES (?, ?, ?, ?)
                        """,
                        (key, summary, status, now),
                    )

            conn.execute(
                "INSERT OR REPLACE INTO sync_metadata (key, last_run) VALUES ('jira_sync', ?)",
                (now,),
            )
            conn.commit()

        logger.info(f"Successfully synced {len(issues)} Jira tasks to ledger.")
        return True

    except Exception as e:
        logger.error(f"Jira Sync Failed: {e}")
        return False


def get_last_sync_time(db_path: str) -> float:
    """Helper to initialize the in-memory cache for Jira syncs."""
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT last_run FROM sync_metadata WHERE key = 'jira_sync'"
            ).fetchone()
            return float(row[0]) if row else 0.0
    except sqlite3.OperationalError:
        return 0.0


@click.command()
@click.option("--db", default="work_activity.db", help="Path to SQLite database.")
@click.option("--interval", "-i", default=10, help="Seconds between snapshots.")
@click.option("--jira-url", envvar="JIRA_URL", help="Jira URL.")
@click.option("--jira-token", envvar="JIRA_PAT", help="Jira Personal Access Token.")
@click.option("--sync-hour", default=17, help="Hour to sync (0-23).")
@click.option(
    "--retry-delay", default=600, help="Seconds to wait after a sync failure."
)
def main(db, interval, jira_url, jira_token, sync_hour, retry_delay):
    if jira_url is None or jira_token is None:
        logger.critical("Jira URL or PAT not provided. Exiting.")
        sys.exit(1)

    click.secho("🖥️  ML Data Collector Active", fg="cyan", bold=True)
    init_db(db)

    next_retry_time = 0
    last_sync_time = get_last_sync_time(db)

    # Sync on startup if we haven't synced today
    now = time.time()
    if (now - last_sync_time) > 43200:  # 12 hours
        click.echo(f"[{time.strftime('%H:%M:%S')}] Syncing Jira...")
        if fetch_and_store_jira_tasks(db, jira_url, jira_token):
            last_sync_time = time.time()
        else:
            next_retry_time = time.time() + retry_delay
            logger.error("Failed to sync Jira on startup.")

    last_snapshot_time = time.time() - interval

    try:
        while True:
            try:
                loop_start_time = time.time()

                snapshot = collect_snapshot(last_snapshot_time, loop_start_time)
                log_to_db(db, snapshot)

                last_snapshot_time = loop_start_time
                current_time = time.time()

                current_hour = time.localtime(current_time).tm_hour

                # If it's the sync hour, we haven't synced in the last 12 hours, and we aren't in a retry timeout
                if (
                    current_hour == sync_hour
                    and (current_time - last_sync_time) > 43200
                    and current_time >= next_retry_time
                ):
                    click.echo(f"[{time.strftime('%H:%M:%S')}] Syncing Jira...")
                    if fetch_and_store_jira_tasks(db, jira_url, jira_token):
                        click.secho("Done.", fg="green")
                        last_sync_time = time.time()
                    else:
                        next_retry_time = current_time + retry_delay
                        click.secho(f"Failed. Retry in {retry_delay // 60}m.", fg="red")

            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)

            elapsed_time = time.time() - loop_start_time
            sleep_time = max(0.0, interval - elapsed_time)
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        click.secho("\nStopped by user.", fg="yellow")


if __name__ == "__main__":
    main()
