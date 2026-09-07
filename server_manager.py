"""Local Server Manager for projects with smart port allocation and health monitoring."""

from __future__ import annotations

import datetime
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

APP_DIR = Path(os.environ.get("APP_DIR", str(Path.home() / ".git-auto-sync"))).expanduser()
SERVERS_STATE_PATH = APP_DIR / "active_servers.json"
SERVER_LOGS_DIR = APP_DIR / "server_logs"
SERVER_LOGS_DIR.mkdir(parents=True, exist_ok=True)
LAST_ERRORS_PATH = APP_DIR / "last_errors.json"
SERVERS_CONFIG_PATH = Path(os.environ.get("SERVERS_CONFIG_PATH", str(Path(__file__).resolve().parent / "servers.json"))).expanduser()


def load_server_configs() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Loads server configurations and aliases from servers.json or default template."""
    configs: dict[str, dict] = {}
    aliases: dict[str, list[str]] = {}

    candidates = [
        SERVERS_CONFIG_PATH,
        APP_DIR / "servers.json",
        Path.cwd() / "servers.json",
    ]

    target_json: Path | None = None
    for c in candidates:
        if c.is_file():
            target_json = c
            break

    if target_json:
        try:
            with open(target_json, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    raw_configs = data.get("servers", data)
                    raw_aliases = data.get("aliases", {})
                    for k, v in raw_configs.items():
                        if isinstance(v, dict) and k != "aliases":
                            entry = dict(v)
                            entry["cwd"] = Path(entry.get("cwd", ".")).expanduser()
                            configs[k] = entry
                    if isinstance(raw_aliases, dict):
                        for ak, av in raw_aliases.items():
                            if isinstance(av, list):
                                aliases[ak.lower()] = av
        except Exception:
            pass

    if not configs:
        # Clean default template for open source demonstration
        configs = {
            "web_frontend": {
                "title": "Web Application (Frontend - Node/Vite)",
                "project": "web-app",
                "service": "frontend",
                "default_port": 3000,
                "cwd": Path("./frontend"),
                "command": ["npm", "run", "dev", "--", "--port", "{port}"],
                "env": {},
            },
            "api_backend": {
                "title": "API Backend (Python/FastAPI/Django)",
                "project": "web-app",
                "service": "backend",
                "default_port": 8000,
                "cwd": Path("./backend"),
                "command": ["python3", "-m", "http.server", "{port}"],
                "env": {"PYTHONUNBUFFERED": "1"},
            },
        }

    # Automatically map project names and service keys to aliases
    for skey, scfg in configs.items():
        skey_lower = skey.lower()
        if skey_lower not in aliases:
            aliases[skey_lower] = [skey]

        proj = scfg.get("project", "").lower()
        if proj:
            if proj not in aliases:
                aliases[proj] = []
            if skey not in aliases[proj]:
                aliases[proj].append(skey)

    aliases["all"] = list(configs.keys())
    aliases["الكل"] = list(configs.keys())

    return configs, aliases


SERVER_CONFIGS, PROJECT_ALIASES = load_server_configs()


def reload_server_configs() -> None:
    """Reloads server configurations dynamically."""
    global SERVER_CONFIGS, PROJECT_ALIASES
    SERVER_CONFIGS, PROJECT_ALIASES = load_server_configs()


def get_local_network_ip() -> str:
    """Discovers current LAN IP address on Wi-Fi/Ethernet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def is_port_in_use(port: int) -> bool:
    """Checks if a TCP port is currently listening on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def find_available_port(preferred_port: int, max_range: int = 50) -> int:
    """If preferred_port is occupied, smartly finds the next available port."""
    if not is_port_in_use(preferred_port):
        return preferred_port
    for offset in range(1, max_range):
        candidate = preferred_port + offset
        if not is_port_in_use(candidate):
            return candidate
    return preferred_port


def load_active_servers() -> dict[str, dict]:
    """Loads active servers state from disk."""
    if SERVERS_STATE_PATH.exists():
        try:
            with open(SERVERS_STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def save_active_servers(servers: dict[str, dict]) -> None:
    """Persists active servers state to disk."""
    try:
        with open(SERVERS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(servers, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def is_process_alive(pid: int) -> bool:
    """Checks if a PID is alive and running (reaping zombies if child)."""
    if pid <= 0:
        return False
    try:
        wpid, status = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False
    except ChildProcessError:
        pass
    except Exception:
        pass

    try:
        os.kill(pid, 0)
        out = subprocess.check_output(["ps", "-o", "state=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL).strip()
        if not out or "Z" in out:
            return False
        return True
    except (ProcessLookupError, subprocess.CalledProcessError):
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def get_last_log_lines(log_path: str, num_lines: int = 5) -> str:
    """Reads the last N lines of a log file to extract error snippets."""
    p = Path(log_path)
    if not p.exists():
        return ""
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            return "".join(lines[-num_lines:]).strip()
    except Exception:
        return ""


def detect_system_project_servers() -> list[dict]:
    """Scans all listening TCP ports on the system to detect any running project servers."""
    cmd = ["lsof", "-iTCP", "-sTCP:LISTEN", "-n", "-P"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except Exception:
        return []

    listening_map: dict[tuple[int, int], str] = {}
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 9:
            command, pid, user, fd, proto, dev, size, node, name = parts[:9]
            port_str = name.split(":")[-1]
            if port_str.isdigit():
                listening_map[(int(pid), int(port_str))] = command

    lan_ip = get_local_network_ip()
    detected = []
    seen_keys: set[tuple[str, int]] = set()

    for (pid, port), comm in listening_map.items():
        try:
            ps_cmd = subprocess.check_output(["ps", "-o", "command=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL).strip()
            lsof_cwd = subprocess.check_output(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], text=True, stderr=subprocess.DEVNULL).strip()
            cwd = lsof_cwd.replace("n", "", 1) if lsof_cwd else ""

            lstart = subprocess.check_output(["ps", "-o", "lstart=", "-p", str(pid)], text=True, stderr=subprocess.DEVNULL).strip()
            try:
                dt = datetime.datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y")
                started_at = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                started_at = lstart

            combined = (ps_cmd + " " + cwd).lower()

            service_key = None
            title = None
            project = None

            if comm.lower() in ("adb",):
                continue

            # Dynamically match against any configured server
            for skey, scfg in SERVER_CONFIGS.items():
                proj_name = scfg.get("project", "").lower()
                title_str = scfg.get("title", skey)
                pref_port = scfg.get("default_port")
                cwd_str = str(scfg.get("cwd", "")).lower()

                matches_proj = bool(proj_name and (proj_name in combined or proj_name in cwd))
                matches_cwd = bool(cwd_str and (cwd_str in combined or cwd in cwd_str))
                matches_port = bool(pref_port and port == pref_port)

                if matches_proj or matches_cwd or matches_port:
                    service_key = skey
                    title = title_str
                    project = scfg.get("project", skey)
                    break

            if service_key and (service_key, port) not in seen_keys:
                seen_keys.add((service_key, port))
                detected.append({
                    "key": service_key,
                    "title": title,
                    "project": project,
                    "port": port,
                    "pid": pid,
                    "pgid": pid,
                    "local_url": f"http://localhost:{port}",
                    "network_url": f"http://{lan_ip}:{port}",
                    "started_at": started_at,
                    "source": "مباشر من الجهاز",
                })
        except Exception:
            pass

    return detected


def get_all_active_project_servers() -> list[dict]:
    """Returns all running project servers across the system (bot-managed + system-detected)."""
    active_bot = load_active_servers()
    system_servers = detect_system_project_servers()

    all_servers = []
    seen = set()

    for k, info in active_bot.items():
        pid = info.get("pid", 0)
        port = info.get("port", 0)
        if is_process_alive(pid) and is_port_in_use(port):
            seen.add((k, port))
            seen.add(pid)
            info_copy = dict(info)
            info_copy["source"] = "عبر البوت"
            all_servers.append(info_copy)

    for s in system_servers:
        k = s["key"]
        port = s["port"]
        pid = s["pid"]
        if (k, port) not in seen and pid not in seen:
            seen.add((k, port))
            seen.add(pid)
            all_servers.append(s)

    return all_servers


def is_service_already_running(service_key: str) -> tuple[bool, dict | None]:
    """Checks if a service is already running on the system or via bot."""
    all_active = get_all_active_project_servers()
    for s in all_active:
        if s.get("key") == service_key:
            return True, s
    return False, None


def start_service(service_key: str) -> tuple[bool, dict | None, str]:
    """Starts a specific service with smart port shift, avoiding duplicate runs."""
    if service_key not in SERVER_CONFIGS:
        return False, None, f"الخدمة {service_key} غير معروفة."

    # Check if already running on the system or via bot
    is_running, existing = is_service_already_running(service_key)
    if is_running and existing:
        existing_copy = dict(existing)
        existing_copy["is_already_running"] = True
        return True, existing_copy, "الخدمة تعمل بالفعل حالياً."

    cfg = SERVER_CONFIGS[service_key]
    active = load_active_servers()

    # Smart port allocation
    preferred_port = cfg["default_port"]
    port = find_available_port(preferred_port)
    port_shifted = (port != preferred_port)

    log_file_path = SERVER_LOGS_DIR / f"{service_key}.log"
    now_dt = datetime.datetime.now()
    started_at_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

    log_file = open(log_file_path, "a", encoding="utf-8")
    log_file.write(f"\n--- Starting {cfg['title']} at {started_at_str} on port {port} ---\n")
    log_file.flush()

    if callable(cfg.get("cmd_fn")):
        cmd = cfg["cmd_fn"](port)
    elif "command" in cfg:
        raw_cmd = cfg["command"]
        if isinstance(raw_cmd, list):
            cmd = [str(x).replace("{port}", str(port)) for x in raw_cmd]
        else:
            cmd = [str(x).replace("{port}", str(port)) for x in str(raw_cmd).split()]
    else:
        cmd = ["python3", "-m", "http.server", str(port)]

    cwd_path = Path(cfg.get("cwd", ".")).expanduser()
    if not cwd_path.exists():
        try:
            cwd_path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    env = os.environ.copy()
    env["PATH"] = f"/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin:{env.get('PATH', '')}"
    env.update(cfg.get("env", {}))

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd_path),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,  # New process group to kill children cleanly
        )
    except Exception as exc:
        log_file.close()
        return False, None, f"تعذر إطلاق العملية: {exc}"

    # Wait briefly for startup and check if process died immediately
    time.sleep(1.8)
    if proc.poll() is not None:
        log_file.close()
        err_tail = get_last_log_lines(str(log_file_path), 5)
        return False, None, f"توقفت العملية فور تشغيلها (Exit code: {proc.returncode}).\n{err_tail}"

    lan_ip = get_local_network_ip()
    server_info = {
        "key": service_key,
        "title": cfg["title"],
        "project": cfg["project"],
        "service": cfg["service"],
        "pid": proc.pid,
        "pgid": proc.pid,
        "port": port,
        "preferred_port": preferred_port,
        "port_shifted": port_shifted,
        "local_url": f"http://localhost:{port}",
        "network_url": f"http://{lan_ip}:{port}",
        "log_file": str(log_file_path),
        "started_at": started_at_str,
        "started_timestamp": time.time(),
        "is_already_running": False,
        "source": "عبر البوت",
    }

    active[service_key] = server_info
    save_active_servers(active)
    return True, server_info, ""


def stop_service(service_key: str) -> tuple[bool, str]:
    """Gracefully stops a running service (whether started by bot or directly on system) and cleans state."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    active = load_active_servers()

    info = None
    if service_key in active:
        info = active.pop(service_key)
        save_active_servers(active)
    else:
        system_servers = detect_system_project_servers()
        for s in system_servers:
            if s.get("key") == service_key:
                info = s
                break

    if not info:
        return False, f"الخدمة {service_key} لا تعمل حالياً على الجهاز."

    pid = info.get("pid", 0)
    pgid = info.get("pgid", pid)
    port = info.get("port", 0)

    # 1. Kill process group
    if pgid > 0:
        try:
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.5)
            if is_process_alive(pid):
                os.killpg(pgid, signal.SIGKILL)
        except Exception:
            pass

    # 2. Kill by PID directly if still lingering
    if pid > 0 and is_process_alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if is_process_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except Exception:
            pass

    # 3. Release port if still held by any child
    if port > 0 and is_port_in_use(port):
        try:
            pids = subprocess.check_output(["lsof", "-ti", f":{port}"], text=True).strip().split()
            for p in pids:
                if p.isdigit():
                    os.kill(int(p), signal.SIGKILL)
        except Exception:
            pass

    return True, f"تم إيقاف {info['title']} وتحرير البورت {port} بنجاح.\n⏱ *توقيت الإيقاف:* `{now_str}`"


def stop_all_services() -> list[str]:
    """Stops all running managed and system project servers."""
    all_active = get_all_active_project_servers()
    results = []
    seen_keys = set()
    for s in all_active:
        k = s.get("key")
        if k and k not in seen_keys:
            seen_keys.add(k)
            ok, msg = stop_service(k)
            if ok:
                results.append(msg)
    return results


def check_and_cleanup_crashes() -> list[dict]:
    """Checks for unexpected process crashes or released ports and returns crash details."""
    active = load_active_servers()
    crashed = []
    keys = list(active.keys())
    now = time.time()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for k in keys:
        info = active[k]
        pid = info.get("pid", 0)
        port = info.get("port", 0)
        started_ts = info.get("started_timestamp", 0)
        is_starting = (now - started_ts) < 10.0

        process_dead = not is_process_alive(pid)
        port_dead = (not is_port_in_use(port)) if not is_starting else False

        # If process died OR port closed unexpectedly
        if process_dead or port_dead:
            err_tail = get_last_log_lines(info.get("log_file", ""), 5)
            crashed_info = {
                "key": k,
                "title": info.get("title", k),
                "port": port,
                "pid": pid,
                "error_snippet": err_tail,
                "reason": "توقفت العملية البرمجية (Process terminated)" if process_dead else "تم إغلاق البورت (Port closed)",
                "detected_at": now_str,
            }
            crashed.append(crashed_info)
            save_last_error(k, crashed_info["title"], err_tail, crashed_info["reason"])
            active.pop(k, None)

    if crashed:
        save_active_servers(active)

    return crashed


def save_last_error(key: str, title: str, snippet: str, reason: str = "") -> None:
    """Stores the latest crash error snippet for AI diagnostics."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    errors = {}
    if LAST_ERRORS_PATH.exists():
        try:
            with open(LAST_ERRORS_PATH, "r", encoding="utf-8") as f:
                errors = json.load(f)
        except Exception:
            pass
    entry = {
        "key": key,
        "title": title,
        "snippet": snippet,
        "reason": reason,
        "timestamp": now_str,
    }
    errors[key] = entry
    errors["_latest"] = entry
    try:
        with open(LAST_ERRORS_PATH, "w", encoding="utf-8") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_last_error(key: str = "") -> dict | None:
    """Retrieves the stored error snippet for a key or the latest."""
    if not LAST_ERRORS_PATH.exists():
        return None
    try:
        with open(LAST_ERRORS_PATH, "r", encoding="utf-8") as f:
            errors = json.load(f)
            if key and key in errors:
                return errors[key]
            return errors.get("_latest")
    except Exception:
        return None


def restart_service(service_key: str) -> tuple[bool, dict | None, str]:
    """Gracefully stops and restarts a specific service, recording exact timestamps."""
    stop_ok, stop_msg = stop_service(service_key)
    time.sleep(1.0)
    start_ok, info, start_msg = start_service(service_key)
    if not start_ok or not info:
        return False, None, f"فشل إعادة تشغيل {service_key}: {start_msg}"
    return True, info, stop_msg


def restart_project(target: str) -> list[dict]:
    """Restarts all services belonging to a target project (e.g. 'web-app', 'api-backend', 'all')."""
    services = resolve_services_from_input(target)
    if not services:
        return []
    results = []
    for svc in services:
        ok, info, msg = restart_service(svc)
        results.append({
            "service_key": svc,
            "ok": ok,
            "info": info,
            "message": msg,
        })
    return results


def get_system_health() -> dict:
    """Collects macOS system resource metrics (RAM, CPU, Disks, Uptime, Active Servers)."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    total_ram_gb = 0.0
    used_ram_gb = 0.0
    ram_percent = 0.0
    try:
        mem_bytes = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip())
        total_ram_gb = mem_bytes / (1024 ** 3)
        vm_out = subprocess.check_output(["vm_stat"], text=True)
        page_size = 4096
        pages_active = pages_wired = pages_comp = 0
        for line in vm_out.splitlines():
            if "page size of" in line:
                m = re.search(r"page size of (\d+) bytes", line)
                if m:
                    page_size = int(m.group(1))
            elif "Pages active:" in line:
                pages_active = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages wired down:" in line:
                pages_wired = int(line.split(":")[1].strip().rstrip("."))
            elif "Pages occupied by compressor:" in line:
                pages_comp = int(line.split(":")[1].strip().rstrip("."))

        used_bytes = (pages_active + pages_wired + pages_comp) * page_size
        used_ram_gb = used_bytes / (1024 ** 3)
        ram_percent = (used_ram_gb / total_ram_gb) * 100 if total_ram_gb > 0 else 0.0
    except Exception:
        pass

    load_str = "N/A"
    cpu_percent = 0.0
    ncpu = 4
    try:
        ncpu = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"], text=True).strip())
        load_raw = subprocess.check_output(["sysctl", "-n", "vm.loadavg"], text=True).strip()
        parts = load_raw.strip("{} ").split()
        if parts:
            load_str = f"1m: {parts[0]} | 5m: {parts[1]} | 15m: {parts[2]}"
            load_1m = float(parts[0])
            cpu_percent = min(100.0, (load_1m / ncpu) * 100)
    except Exception:
        pass

    disks = []
    try:
        check_paths = ["/"]
        custom_base = os.environ.get("SYNC_BASE_DIR")
        if custom_base and Path(custom_base).exists():
            check_paths.append(str(Path(custom_base).resolve()))
        elif Path("/Volumes/Data").exists():
            check_paths.append("/Volumes/Data")

        seen_mounts = set()
        for p in check_paths:
            try:
                df_out = subprocess.check_output(["df", "-h", p], text=True)
                lines = df_out.strip().splitlines()
                if len(lines) >= 2:
                    parts = lines[1].split()
                    if len(parts) >= 6:
                        size, used, avail, cap, mount = parts[1], parts[2], parts[3], parts[4], parts[-1]
                        if mount in seen_mounts:
                            continue
                        seen_mounts.add(mount)
                        name = "قرص النظام (/)" if mount == "/" else f"قرص البيانات ({mount})"
                        disks.append({
                            "name": name,
                            "mount": mount,
                            "size": size,
                            "used": used,
                            "avail": avail,
                            "capacity": cap,
                        })
            except Exception:
                pass
    except Exception:
        pass

    uptime_str = "N/A"
    try:
        up_out = subprocess.check_output(["uptime"], text=True).strip()
        if "up " in up_out:
            parts = up_out.split("up ")[1].split(",")
            uptime_str = f"{parts[0].strip()}, {parts[1].strip()}"
    except Exception:
        pass

    active_servers = get_all_active_project_servers()

    return {
        "timestamp": now_str,
        "total_ram_gb": round(total_ram_gb, 1),
        "used_ram_gb": round(used_ram_gb, 1),
        "ram_percent": round(ram_percent, 1),
        "ncpu": ncpu,
        "load_str": load_str,
        "cpu_percent": round(cpu_percent, 1),
        "disks": disks,
        "uptime": uptime_str,
        "active_servers_count": len(active_servers),
    }


def resolve_services_from_input(target: str) -> list[str]:
    """Maps a user input (e.g. 'web-app', 'api-backend', 'all') to concrete service keys."""
    if not target:
        return []
    clean = target.strip().lower()

    # 1. Exact alias match
    if clean in PROJECT_ALIASES:
        return PROJECT_ALIASES[clean]

    # 2. Exact service key match
    if clean in SERVER_CONFIGS:
        return [clean]

    # 3. Dynamic partial match against configured projects
    wants_backend = any(x in clean for x in ("back", "api", "خلفي", "باك"))
    wants_frontend = any(x in clean for x in ("front", "ui", "web", "واجهة", "فرونت"))

    for proj, s_keys in PROJECT_ALIASES.items():
        if proj and proj in clean:
            if wants_backend:
                matched = [k for k in s_keys if "back" in k.lower() or "api" in k.lower()]
                if matched:
                    return matched
            elif wants_frontend:
                matched = [k for k in s_keys if "front" in k.lower() or "ui" in k.lower() or "web" in k.lower()]
                if matched:
                    return matched
            return s_keys

    # 4. Check title or service key substring matches
    for skey, scfg in SERVER_CONFIGS.items():
        title_lower = scfg.get("title", "").lower()
        if clean in title_lower or clean in skey.lower():
            return [skey]

    return []
