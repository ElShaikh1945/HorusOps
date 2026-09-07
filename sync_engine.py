"""Core synchronization and Git conflict-safe engine."""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from pathlib import Path
import requests

from env_loader import load_env

load_env()

BASE_DIR = Path(os.environ.get("SYNC_BASE_DIR", str(Path.home() / "projects"))).expanduser()
IGNORED_NAMES = set(
    filter(None, [x.strip() for x in os.environ.get("SYNC_IGNORED_NAMES", "ignore,personal,archive").split(",")])
)
APP_DIR = Path(os.environ.get("APP_DIR", str(Path.home() / ".git-auto-sync"))).expanduser()
LOG_PATH = Path(os.environ.get("SYNC_LOG_PATH", str(APP_DIR / "sync.log"))).expanduser()
COMMIT_ONLY_CONFIG = APP_DIR / "config_commit_only.toml"
BOT_ACTIONS_PATH = APP_DIR / "bot_actions.json"


def load_bot_actions() -> dict:
    if BOT_ACTIONS_PATH.exists():
        try:
            with open(BOT_ACTIONS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"commits": {}, "pushes": {}}


def register_bot_action(action_type: str, repo_name: str, commit_hash: str) -> None:
    """Registers that a commit or push was initiated by the bot, preventing duplicate alerts."""
    if not commit_hash or commit_hash == "-":
        return
    try:
        data = load_bot_actions()
        now_ts = time.time()
        records = data.get(action_type, {})
        # Store by prefix or full hash
        records[f"{repo_name}:{commit_hash[:8]}"] = now_ts
        records[f"{repo_name}:{commit_hash}"] = now_ts
        # Prune older than 3 days
        cleaned = {k: v for k, v in records.items() if (now_ts - v) < 259200}
        data[action_type] = cleaned
        with open(BOT_ACTIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def is_bot_action(action_type: str, repo_name: str, commit_hash: str) -> bool:
    """Checks if a commit or push was initiated by the bot within the last 3 days."""
    if not commit_hash:
        return False
    try:
        data = load_bot_actions()
        records = data.get(action_type, {})
        return (f"{repo_name}:{commit_hash[:8]}" in records) or (f"{repo_name}:{commit_hash}" in records)
    except Exception:
        return False


def get_base_dirs() -> list[Path]:
    raw = os.environ.get("SYNC_BASE_DIR", str(Path.home() / "projects"))
    dirs = []
    for p in raw.split(","):
        p = p.strip()
        if p:
            dirs.append(Path(p).expanduser())
    return dirs or [Path(str(Path.home() / "projects")).expanduser()]


def check_environment() -> tuple[bool, str]:
    base_dirs = get_base_dirs()
    existing = [d for d in base_dirs if d.exists() and d.is_dir()]
    if not existing:
        return False, f"المسارات المحددة {base_dirs} غير متاحة أو القرص غير موصول."

    try:
        r = requests.head("https://github.com", timeout=4)
        if not r.ok and r.status_code >= 500:
            return False, "سيرفرات GitHub لا تستجيب حالياً."
    except Exception:
        return False, "لا يوجد اتصال بشبكة الإنترنت حالياً."

    return True, ""


def discover_repos() -> list[Path]:
    repos = []
    seen = set()
    for base in get_base_dirs():
        if not base.exists() or not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir() and d.name not in IGNORED_NAMES and (d / ".git").exists():
                r_resolved = d.resolve()
                if str(r_resolved) not in seen:
                    seen.add(str(r_resolved))
                    repos.append(r_resolved)
    return repos


def get_github_urls(repo: Path, remote_name: str, branch: str, commit_hash: str) -> tuple[str, str]:
    """Returns (branch_url, commit_url) for GitHub."""
    res = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", remote_name],
        capture_output=True,
        text=True,
        check=False,
    )
    raw_url = res.stdout.strip()
    web_base = ""
    if "github.com" in raw_url:
        cleaned = raw_url.replace("git@github.com:", "https://github.com/")
        if cleaned.endswith(".git"):
            cleaned = cleaned[:-4]
        if not cleaned.startswith("http"):
            cleaned = "https://" + cleaned.lstrip("/")
        web_base = cleaned

    branch_url = f"{web_base}/tree/{branch}" if web_base else ""
    commit_url = f"{web_base}/commit/{commit_hash}" if (web_base and commit_hash and commit_hash != "-") else ""
    return branch_url, commit_url


def resolve_upstream(repo: Path, branch: str) -> tuple[str, str]:
    """Resolves (remote_name, remote_branch) safely."""
    res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "@{u}"],
        capture_output=True,
        text=True,
        check=False,
    )
    upstream = res.stdout.strip()
    if upstream and "/" in upstream and "error" not in upstream.lower():
        remote_name, remote_branch = upstream.split("/", 1)
        return remote_name, remote_branch

    # Fallback: check remote list
    remotes_res = subprocess.run(
        ["git", "-C", str(repo), "remote"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip().splitlines()

    if "wai" in remotes_res and branch == "wai-update":
        return "wai", branch
    if "origin" in remotes_res:
        return "origin", branch
    if remotes_res:
        return remotes_res[0], branch
    return "origin", branch


def commit_local_changes_if_dirty(repo: Path) -> tuple[bool, str]:
    """Checks for uncommitted working-tree changes and commits them cleanly."""
    status_raw = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if not status_raw:
        return True, ""

    # Stage all changes
    add_res = subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        capture_output=True,
        text=True,
        check=False,
    )
    if add_res.returncode != 0:
        return False, f"فشل تجهيز الملفات (git add): {add_res.stderr.strip()}"

    # Safety safeguard: unstage accidental secret/credential files if omitted from .gitignore
    subprocess.run(
        ["git", "-C", str(repo), "reset", "HEAD", "--", "*.env", ".env.*", "*.pem", "*.key", "id_rsa*", "*.p12", "*.secret"],
        capture_output=True,
        check=False,
    )

    # Verify if staged changes exist
    diff_res = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if not diff_res:
        return True, ""

    # Generate a descriptive commit message based on modified files
    changed_files = diff_res.splitlines()
    sample_files = [Path(f).name for f in changed_files[:3]]
    sample_str = ", ".join(sample_files)
    if len(changed_files) > 3:
        sample_str += f" (+{len(changed_files) - 3} ملفات)"

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    commit_msg = f"chore: auto-sync updates ({sample_str}) [{now_str}]"

    commit_res = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
        check=False,
    )
    if commit_res.returncode != 0:
        return False, f"فشل حفظ التعديلات محلياً (git commit): {commit_res.stderr.strip()}"

    try:
        new_head = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        register_bot_action("commit", repo.name, new_head)
    except Exception:
        pass

    return True, commit_msg


def sync_repo(repo: Path) -> dict:
    """Syncs a single repo: commits if dirty, pulls with rebase safely, and pushes to remote."""
    branch_res = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    branch = branch_res.stdout.strip() or "HEAD"
    remote_name, remote_branch = resolve_upstream(repo, branch)

    # 1. First commit any local uncommitted files
    ok, commit_err = commit_local_changes_if_dirty(repo)
    if not ok:
        return {
            "repo": repo.name,
            "branch": branch,
            "status": "error",
            "error": commit_err,
        }

    # 2. Check if there are unpushed commits
    unpushed_cmd = ["git", "-C", str(repo), "log", f"{remote_name}/{remote_branch}..HEAD", "--oneline"]
    unpushed_res = subprocess.run(unpushed_cmd, capture_output=True, text=True, check=False)
    unpushed_raw = unpushed_res.stdout.strip()

    if unpushed_res.returncode != 0:
        # If remote tracking ref is unknown, fallback to @{u}
        fallback_res = subprocess.run(
            ["git", "-C", str(repo), "log", "@{u}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
        )
        unpushed_raw = fallback_res.stdout.strip()

    # 3. If no unpushed commits and working tree clean
    status_raw = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()

    if not status_raw and not unpushed_raw:
        return {"repo": repo.name, "branch": branch, "status": "clean"}

    # 4. Fetch remote tracking updates quietly
    subprocess.run(
        ["git", "-C", str(repo), "fetch", remote_name, remote_branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )

    # 5. Pull with rebase safely (working tree is already committed)
    pull_res = subprocess.run(
        ["git", "-C", str(repo), "pull", "--rebase", remote_name, remote_branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    if pull_res.returncode != 0:
        # Check if we hit conflict
        conflicts = subprocess.run(
            ["git", "-C", str(repo), "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip().splitlines()

        # Abort rebase to prevent corrupted working tree
        subprocess.run(["git", "-C", str(repo), "rebase", "--abort"], capture_output=True, text=True, check=False)

        if conflicts:
            return {
                "repo": repo.name,
                "branch": branch,
                "status": "conflict",
                "conflicts": conflicts,
                "error": "تعارض في الدمج مع تعديلات شخص آخر.",
            }
        return {
            "repo": repo.name,
            "branch": branch,
            "status": "error",
            "error": pull_res.stderr.strip() or "فشل دمج التحديثات القادمة من الخادم (pull rebase).",
        }

    # 6. Push to remote
    push_res = subprocess.run(
        ["git", "-C", str(repo), "push", remote_name, branch],
        capture_output=True,
        text=True,
        check=False,
        timeout=35,
    )

    if push_res.returncode != 0:
        return {
            "repo": repo.name,
            "branch": branch,
            "status": "error",
            "error": push_res.stderr.strip() or push_res.stdout.strip() or "خطأ أثناء محاولة الرفع إلى GitHub.",
        }

    # 7. Extract latest commit and previous commit details
    log_res = subprocess.run(
        ["git", "-C", str(repo), "log", "-2", "--format=%h|%ad|%s", "--date=format:%Y-%m-%d %H:%M"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip().splitlines()

    lines = [l.strip() for l in log_res if "|" in l]
    new_c = lines[0].split("|") if lines else ["-", "-", "تعديلات برمجية"]
    prev_c = lines[1].split("|") if len(lines) >= 2 else ["-", "-", "لا يوجد"]

    branch_url, commit_url = get_github_urls(repo, remote_name, branch, new_c[0])

    try:
        register_bot_action("push", repo.name, new_c[0])
    except Exception:
        pass

    return {
        "repo": repo.name,
        "branch": branch,
        "status": "pushed",
        "new_commit_hash": new_c[0],
        "new_commit_msg": new_c[2],
        "new_commit_date": new_c[1],
        "commit_url": commit_url,
        "branch_url": branch_url,
        "prev_commit_hash": prev_c[0],
        "prev_commit_date": prev_c[1],
        "prev_commit_msg": prev_c[2],
    }


def execute_full_push(target_repo_name: str | None = None) -> dict:
    ok, err_msg = check_environment()
    if not ok:
        return {"ok": False, "error": err_msg, "results": []}

    repos = discover_repos()
    if not repos:
        return {"ok": False, "error": "لم يتم العثور على أي مستودعات نشطة.", "results": []}

    if target_repo_name and target_repo_name.lower() not in {"all", "الكل", "الجميع", "شامل"}:
        t_clean = target_repo_name.strip().lower()
        matched = [r for r in repos if r.name.lower() == t_clean or t_clean in r.name.lower()]
        if not matched:
            return {"ok": False, "error": f"المشروع `{target_repo_name}` غير موجود.", "results": []}
        repos = matched

    results = []
    has_pushed = False
    has_conflicts = False
    has_errors = False

    for r in repos:
        res = sync_repo(r)
        results.append(res)
        if res["status"] == "pushed":
            has_pushed = True
        elif res["status"] == "conflict":
            has_conflicts = True
        elif res["status"] == "error":
            has_errors = True

    return {
        "ok": True,
        "has_pushed": has_pushed,
        "has_conflicts": has_conflicts,
        "has_errors": has_errors,
        "results": results,
    }


