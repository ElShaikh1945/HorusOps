"""Environment variable loader for HorusOps / git-auto-sync.

Checks in order:
1. Custom path specified via ENV_PATH environment variable.
2. Local .env in project root directory (next to this script).
3. Local .env in current working directory.
4. Default fallback: ~/.git-auto-sync/.env
"""

from __future__ import annotations

import os
from pathlib import Path


def get_env_file_path() -> Path | None:
    # 1. Custom ENV_PATH
    custom_env = os.environ.get("ENV_PATH")
    if custom_env:
        p = Path(custom_env).expanduser().resolve()
        if p.is_file():
            return p

    # 2. Next to this file
    local_env = Path(__file__).resolve().parent / ".env"
    if local_env.is_file():
        return local_env

    # 3. Current working directory
    cwd_env = Path.cwd() / ".env"
    if cwd_env.is_file():
        return cwd_env

    # 4. Global ~/.git-auto-sync/.env fallback
    global_env = Path.home() / ".git-auto-sync" / ".env"
    if global_env.is_file():
        return global_env

    return None


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = get_env_file_path()

    if path and path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'\"")
                    env[k] = v
                    os.environ[k] = v
        except Exception:
            pass

    # Ensure existing os.environ values are included
    for k, v in os.environ.items():
        if k not in env:
            env[k] = v

    return env
