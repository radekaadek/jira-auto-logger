import ast
import json
import logging
import subprocess
import sqlite3
import time
import requests
from requests.auth import HTTPBasicAuth
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
        raw_services: list[dict[str, object]] = json.loads(result.stdout)
        return [f"{s.get('Namespace')}/{s.get('Name')}" for s in raw_services]
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError) as e:
        logger.debug(f"Minikube error: {e}")
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
        logger.debug(f"Docker error: {e}")
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
    files = list(
        (home / ".mozilla/firefox").glob("*/sessionstore-backups/recovery.jsonlz4")
    )

    if not files:
        return {"titles": [], "domains": []}

    titles: set[str] = set()
    domains: set[str] = set()

    try:
        with files[0].open("rb") as f:
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


def collect_snapshot() -> dict[str, object]:
    """Aggregates all data into a single dictionary."""
    window = get_focused_window_info()
    cwd = str(Path.cwd())
    fx = get_firefox_context()

    snapshot = {
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

    # Success Log
    logger.info(f"Successfully extracted snapshot for app: {snapshot['focused_app']}")
    return snapshot


DB_PATH = "work_activity.db"


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
        conn.execute(
            "CREATE TABLE IF NOT EXISTS jira_tasks (key TEXT PRIMARY KEY, summary TEXT, status TEXT, updated_at INTEGER)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_metadata (key TEXT PRIMARY KEY, last_run INTEGER)"
        )
        conn.commit()


def log_to_db(db_path: str, data: dict):
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


def fetch_and_store_jira_tasks(db_path: str, url: str, email: str, token: str):
    """Fetches active tasks from Jira and updates the local table."""
    jql = 'status NOT IN ("Done", "Closed", "Suspended", "Finished") AND assignee = currentUser()'
    api_url = f"{url.rstrip('/')}/rest/api/3/search"
    auth = HTTPBasicAuth(email, token)
    query = {"jql": jql, "fields": "summary,status"}

    try:
        response = requests.get(
            api_url,
            headers={"Accept": "application/json"},
            params=query,
            auth=auth,
            timeout=15,
        )

        if response.status_code != 200:
            logger.error(f"Jira API error: Received status {response.status_code}")
            return False

        response.raise_for_status()
        data = response.json()

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM jira_tasks")
            for issue in data.get("issues", []):
                conn.execute(
                    "INSERT INTO jira_tasks (key, summary, status, updated_at) VALUES (?, ?, ?, ?)",
                    (
                        issue["key"],
                        issue["fields"]["summary"],
                        issue["fields"]["status"]["name"],
                        int(time.time()),
                    ),
                )
            conn.execute(
                "INSERT OR REPLACE INTO sync_metadata (key, last_run) VALUES ('jira_sync', ?)",
                (int(time.time()),),
            )
            conn.commit()

        logger.info(f"Successfully synced {len(data.get('issues', []))} Jira tasks.")
        return True

    except Exception as e:
        logger.error(f"Jira Sync Failed: {e}")
        return False


def is_sync_due(db_path: str, sync_hour: int) -> bool:
    now = time.localtime()
    target_time = time.mktime(
        (now.tm_year, now.tm_mon, now.tm_mday, sync_hour, 0, 0, 0, 0, -1)
    )
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT last_run FROM sync_metadata WHERE key = 'jira_sync'"
        ).fetchone()
        last_run = row[0] if row else 0
    return (time.time() >= target_time) and (last_run < target_time)


@click.command()
@click.option("--db", default="work_activity.db", help="Path to SQLite database.")
@click.option("--interval", "-i", default=10, help="Seconds between snapshots.")
@click.option("--jira-url", envvar="JIRA_URL", help="Jira URL.")
@click.option("--jira-email", envvar="JIRA_EMAIL", help="Jira account email.")
@click.option(
    "--jira-token", envvar="JIRA_API_TOKEN", help="Jira API Token.", hide_input=True
)
@click.option("--sync-hour", default=17, help="Hour to sync (0-23).")
@click.option(
    "--retry-delay", default=600, help="Seconds to wait after a sync failure."
)
def main(db, interval, jira_url, jira_email, jira_token, sync_hour, retry_delay):
    if jira_url is None:
        logger.critical("Jira URL not provided. Exiting.")
        return
    if jira_email is None:
        logger.critical("Jira email not provided. Exiting.")
        return
    if jira_token is None:
        logger.critical("Jira API token not provided. Exiting.")
        return
    click.secho("🖥️  ML Data Collector Active", fg="cyan", bold=True)
    init_db(db)
    next_retry_time = 0

    # sync jira on startup

    click.echo(f"[{time.strftime('%H:%M:%S')}] Syncing Jira...")
    if fetch_and_store_jira_tasks(db, jira_url, jira_email, jira_token):
        logger.info("Successfully synced Jira on startup.", fg="green")
    else:
        next_retry_time = now + retry_delay
        logger.error("Failed to sync Jira on startup.", fg="red")

    try:
        while True:
            try:
                # Core data collection step
                snapshot = collect_snapshot()
                log_to_db(db, snapshot)
            except Exception as e:
                # Complete Failure Logging
                logger.critical(
                    f"FATAL: Critical extraction failure. Snapshot not saved: {e}",
                    exc_info=True,
                )
                time.sleep(interval)
                continue

            if jira_url and jira_email and jira_token:
                now = time.time()
                if is_sync_due(db, sync_hour) and now >= next_retry_time:
                    click.echo(f"[{time.strftime('%H:%M:%S')}] Syncing Jira...")
                    if fetch_and_store_jira_tasks(db, jira_url, jira_email, jira_token):
                        click.secho("Done.", fg="green")
                    else:
                        next_retry_time = now + retry_delay
                        click.secho(f"Failed. Retry in {retry_delay // 60}m.", fg="red")

            time.sleep(interval)
    except KeyboardInterrupt:
        click.secho("\nStopped by user.", fg="yellow")


if __name__ == "__main__":
    main()
