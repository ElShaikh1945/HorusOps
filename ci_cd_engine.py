"""GitHub Actions CI/CD engine for triggering builds, deploys, and downloading APKs."""

from __future__ import annotations

import datetime
import io
import json
import os
import shutil
import time
import zipfile
from pathlib import Path
import requests

from env_loader import load_env

env = load_env()
GITHUB_TOKEN = env.get("GITHUB_TOKEN", "")
BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
APP_DIR = Path(os.environ.get("APP_DIR", str(Path.home() / ".git-auto-sync"))).expanduser()
ARTIFACTS_DIR = Path(os.environ.get("ARTIFACTS_DIR", str(APP_DIR / "artifacts"))).expanduser()
ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
TARGETS_CONFIG_PATH = Path(os.environ.get("TARGETS_CONFIG_PATH", str(Path(__file__).resolve().parent / "targets.json"))).expanduser()

GITHUB_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "git-auto-sync-bot",
}


def load_build_targets() -> dict[str, dict]:
    """Loads CI/CD build targets from targets.json or returns a default template."""
    candidates = [
        TARGETS_CONFIG_PATH,
        APP_DIR / "targets.json",
        Path.cwd() / "targets.json",
    ]

    for c in candidates:
        if c.is_file():
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        targets = data.get("targets", data)
                        if targets:
                            return targets
            except Exception:
                pass

    return {
        "mobile_app": {
            "title": "Mobile App (Android APK)",
            "project": "mobile-app",
            "app": "client",
            "owner": "organization",
            "repo": "mobile-app",
            "workflow": "build-android.yml",
            "branch": "main",
            "inputs": {"app": "client", "build_type": "release"},
            "type": "apk",
        },
        "web_build": {
            "title": "Web Application Build",
            "project": "web-app",
            "app": "frontend",
            "owner": "organization",
            "repo": "web-app",
            "workflow": "build-web.yml",
            "branch": "main",
            "inputs": {"environment": "production"},
            "type": "build",
        },
    }


BUILD_TARGETS = load_build_targets()


def reload_build_targets() -> None:
    global BUILD_TARGETS
    BUILD_TARGETS = load_build_targets()

STEP_TITLES = {
    "Set up job": "تهيئة بيئة خادم البناء",
    "Checkout Repository": "جلب الكود المصدري للمشروع",
    "Set up Java 17": "تجهيز بيئة Java 17",
    "Set up Flutter": "تجهيز بيئة Flutter SDK",
    "Build Customer App APK (arm64)": "بناء تطبيق العميل (Customer APK arm64)",
    "Build Delivery Agent App APK (arm64)": "بناء تطبيق المندوب (Agent APK arm64)",
    "Upload APKs Artifact": "تصدير وتجهيز ملفات الـ APK",
    "Publish APKs to GitHub Release": "نشر الحزم على GitHub Releases",
}


def get_step_title(raw_name: str) -> str:
    for k, v in STEP_TITLES.items():
        if k.lower() in raw_name.lower():
            return v
    return raw_name


def fetch_run_job_steps(owner: str, repo: str, run_id: int) -> tuple[str, list[str]]:
    """Fetches job steps and returns (active_step_title, list_of_formatted_step_lines)."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
    try:
        r = requests.get(url, headers=GITHUB_HEADERS, timeout=10)
        if r.status_code == 200:
            jobs = r.json().get("jobs", [])
            if jobs:
                steps = jobs[0].get("steps", [])
                lines = []
                active_step = ""
                for s in steps:
                    name = s.get("name", "")
                    if name.startswith("Post ") or name == "Complete job":
                        continue
                    title = get_step_title(name)
                    status = s.get("status")
                    conclusion = s.get("conclusion")
                    if status == "completed":
                        if conclusion == "success":
                            lines.append(f"[OK] {title}")
                        elif conclusion == "skipped":
                            lines.append(f"[SKIP] {title} (تم التخطي)")
                        else:
                            lines.append(f"[FAIL] {title} (فشل)")
                    elif status == "in_progress":
                        active_step = title
                        lines.append(f"[RUN] {title} (جارٍ التنفيذ الآن...)")
                    else:
                        lines.append(f"[-] {title}")
                if not active_step and lines:
                    if all(s.get("status") == "completed" for s in steps if not s.get("name", "").startswith("Post ") and s.get("name") != "Complete job"):
                        active_step = "اكتملت جميع الخطوات بنجاح"
                    else:
                        active_step = "جاري البدء..."
                return active_step, lines
    except Exception:
        pass
    return "", []


def fetch_recent_workflow_runs(owner: str, repo: str, per_page: int = 5) -> list[dict]:
    """Fetches the latest workflow runs for a repository."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs?per_page={per_page}"
    try:
        r = requests.get(url, headers=GITHUB_HEADERS, timeout=12)
        if r.status_code == 200:
            return r.json().get("workflow_runs", [])
    except Exception:
        pass
    return []


def trigger_workflow(owner: str, repo: str, workflow: str, ref: str, inputs: dict) -> tuple[bool, str]:
    """Triggers a workflow_dispatch event on GitHub Actions."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches"
    payload = {"ref": ref}
    if inputs:
        payload["inputs"] = inputs

    try:
        r = requests.post(url, headers=GITHUB_HEADERS, json=payload, timeout=20)
        if r.status_code in (200, 204):
            return True, ""
        return False, f"HTTP {r.status_code}: {r.text}"
    except Exception as exc:
        return False, str(exc)


def get_latest_run_id(owner: str, repo: str, workflow: str, after_timestamp: float) -> dict | None:
    """Finds the workflow run triggered after after_timestamp."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/{workflow}/runs?per_page=5"
    for _ in range(12):
        try:
            r = requests.get(url, headers=GITHUB_HEADERS, timeout=15)
            if r.status_code == 200:
                runs = r.json().get("workflow_runs", [])
                for run in runs:
                    created_at_str = run.get("created_at")
                    if created_at_str:
                        dt = datetime.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                        if dt.timestamp() >= (after_timestamp - 30):
                            return run
        except Exception:
            pass
        time.sleep(3)
    return None


def poll_run_completion(owner: str, repo: str, run_id: int, on_progress=None, timeout_secs: int = 900) -> dict:
    """Polls until workflow run is completed, providing real-time step updates."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
    start_time = time.time()
    last_active_step = ""
    last_update_ts = 0.0

    while (time.time() - start_time) < timeout_secs:
        try:
            r = requests.get(url, headers=GITHUB_HEADERS, timeout=15)
            if r.status_code == 200:
                data = r.json()
                status = data.get("status")
                now = time.time()
                elapsed = int(now - start_time)
                elapsed_str = f"{elapsed // 60}m {elapsed % 60}s"

                active_step, step_lines = fetch_run_job_steps(owner, repo, run_id)
                step_changed = active_step != last_active_step
                periodic_update = (now - last_update_ts) >= 15

                if (step_changed or periodic_update or status == "completed") and on_progress:
                    last_active_step = active_step
                    last_update_ts = now
                    on_progress(status, elapsed_str, data.get("html_url", ""), active_step, step_lines)

                if status == "completed":
                    return data
        except Exception:
            pass
        time.sleep(4)

    return {"status": "timed_out", "conclusion": "timed_out"}


def download_artifacts_and_extract_apks(owner: str, repo: str, run_id: int) -> list[Path]:
    """Downloads all artifacts from a run and extracts any .apk files."""
    list_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
    extracted_apks: list[Path] = []

    try:
        r = requests.get(list_url, headers=GITHUB_HEADERS, timeout=20)
        if r.status_code != 200:
            return []
        artifacts = r.json().get("artifacts", [])
        for art in artifacts:
            download_url = art.get("archive_download_url")
            if not download_url:
                continue

            resp = requests.get(download_url, headers=GITHUB_HEADERS, timeout=60, stream=True)
            if resp.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
                    for filename in z.namelist():
                        if filename.endswith(".apk"):
                            target_path = ARTIFACTS_DIR / Path(filename).name
                            with z.open(filename) as src, open(target_path, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extracted_apks.append(target_path)
    except Exception as exc:
        print(f"Error downloading artifacts: {exc}")

    return extracted_apks


def send_telegram_apk(chat_id: int, apk_path: Path, caption: str) -> bool:
    """Sends an APK file via Telegram sendDocument API with retries."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    try:
        size_mb = apk_path.stat().st_size / (1024 * 1024)
        if size_mb > 49.5:
            return False

        for attempt in range(3):
            try:
                with open(apk_path, "rb") as f:
                    files = {"document": (apk_path.name, f, "application/vnd.android.package-archive")}
                    data = {"chat_id": chat_id, "caption": caption}
                    r = requests.post(url, data=data, files=files, timeout=180)
                    if r.ok:
                        return True
                    print(f"Telegram APK upload failed attempt {attempt+1}: {r.text}")
            except Exception as e:
                print(f"Telegram APK upload exception attempt {attempt+1}: {e}")
            time.sleep(3)
        return False
    except Exception as exc:
        print(f"Error sending APK to telegram: {exc}")
        return False
