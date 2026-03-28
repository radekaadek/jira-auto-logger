import subprocess
import json
import time
import ast
import os
import glob
import lz4.block
from urllib.parse import urlparse

# --- Added Noise Filter for C++/Python Devs ---
EXCLUDE_DIRS = {
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


def get_minikube_services():
    """
    Feature 5: Returns names and namespaces of active Minikube services.
    Uses 'minikube service list -o json' for structured data.
    """
    try:
        # Get services across all namespaces in JSON format
        result = subprocess.run(
            ["minikube", "service", "list", "-o", "json"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,  # Prevent hanging if minikube is unresponsive
        )

        raw_services = json.loads(result.stdout)
        # Simplify the output for ML: focus on Name and Namespace
        # Format: "namespace/service_name"
        services = [f"{svc.get('Namespace')}/{svc.get('Name')}" for svc in raw_services]
        return services
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        # Returns empty list if minikube is stopped or not installed
        return []


def get_docker_status():
    """Feature 4: Returns names and images of running Docker containers."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [line for line in result.stdout.strip().split("\n") if line]
    except:
        return []


def get_active_tmux_sessions():
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_attached} #{session_name}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return [
            line.split(" ", 1)[1]
            for line in result.stdout.strip().split("\n")
            if line.startswith("1")
        ]
    except:
        return []


def get_recent_file_activity(path, lookback_seconds=600):
    if not path or not os.path.exists(path) or path == os.path.expanduser("~"):
        return []
    now = time.time()
    recent_files = []
    try:
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for file in files:
                fpath = os.path.join(root, file)
                try:
                    if now - os.path.getmtime(fpath) < lookback_seconds:
                        recent_files.append(os.path.relpath(fpath, path))
                except:
                    continue
            if root.count(os.sep) - path.count(os.sep) >= 2:
                break
        return recent_files[:20]
    except:
        return []


def get_active_dev_tools():
    watch_list = [
        "node",
        "npm",
        "docker",
        "pytest",
        "python",
        "python3",
        "g++",
        "gcc",
        "clang",
        "cmake",
        "ninja",
        "gdb",
        "make",
        "lldb",
    ]
    try:
        ps_output = subprocess.check_output(["ps", "-A", "-o", "comm="], text=True)
        running = set(ps_output.split())
        return [tool for tool in watch_list if tool in running]
    except:
        return []


def get_firefox_context():
    profiles_path = os.path.expanduser(
        "~/.mozilla/firefox/*.default*/sessionstore-backups/recovery.jsonlz4"
    )
    files = glob.glob(profiles_path)
    if not files:
        return {"titles": [], "domains": []}
    titles, domains = set(), set()
    try:
        with open(files[0], "rb") as f:
            f.read(8)
            data = json.loads(lz4.block.decompress(f.read()))
            for win in data.get("windows", []):
                for tab in win.get("tabs", []):
                    entries = tab.get("entries", [])
                    if entries:
                        url, title = (
                            entries[-1].get("url", ""),
                            entries[-1].get("title", ""),
                        )
                        if title:
                            titles.add(title)
                        if url:
                            domains.add(urlparse(url).netloc)
    except:
        pass
    return {"titles": list(titles), "domains": list(domains)}


def get_focused_window_info():
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
        )
        windows = json.loads(ast.literal_eval(result.stdout.strip())[0])
        for win in windows:
            if win.get("focus"):
                return {
                    "class": win.get("class", ""),
                    "title": win.get("title", ""),
                    "pid": win.get("pid"),
                }
    except:
        pass
    return {"class": "unknown", "title": "unknown", "pid": None}


def collect_snapshot():
    window = get_focused_window_info()
    cwd = os.getcwd()
    fx = get_firefox_context()

    return {
        "timestamp": int(time.time()),
        "focused_app": window["class"],
        "docker_services": get_docker_status(),
        "minikube_services": get_minikube_services(),  # <--- New Feature
        "cwd": cwd,
        "active_dev_tools": get_active_dev_tools(),
        "recent_files": get_recent_file_activity(cwd),
        "browser_domains": fx["domains"],
        "tmux_sessions": get_active_tmux_sessions(),
        "firefox_tabs": fx["titles"],
    }


if __name__ == "__main__":
    print(json.dumps(collect_snapshot(), indent=2))
