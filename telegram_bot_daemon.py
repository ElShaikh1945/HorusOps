#!/usr/bin/env python3
"""Interactive Telegram Bot Daemon for git-auto-sync.

Features:
  - Secured credentials via ~/.git-auto-sync/.env (chmod 600)
  - Mount & Network connectivity guards before syncing
  - Multi-user remote rebase conflict detection & alerts
  - Open-ended conversation memory with /clear command to reset session
  - Live in-place message edit (editMessageText) on Push showing new commit hash & prev commit date
  - Ultra-concise, formal, no-chitchat AI persona
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import time
import warnings
from collections import Counter
from pathlib import Path

warnings.filterwarnings("ignore")

import requests
from env_loader import load_env

env = load_env()
BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")

# Parse authorized user IDs supporting TELEGRAM_USER_ID, TELEGRAM_ADMIN_IDS, and TELEGRAM_CHAT_ID
raw_user_ids = env.get("TELEGRAM_USER_ID", "") or env.get("TELEGRAM_ADMIN_IDS", "") or env.get("TELEGRAM_CHAT_ID", "")
AUTHORIZED_USER_IDS: set[int] = set()
for uid in str(raw_user_ids).split(","):
    uid_clean = uid.strip()
    if uid_clean.isdigit():
        AUTHORIZED_USER_IDS.add(int(uid_clean))

AUTHORIZED_USER_ID = next(iter(AUTHORIZED_USER_IDS)) if AUTHORIZED_USER_IDS else 0

def is_authorized(from_id: int | None) -> bool:
    if not from_id or not AUTHORIZED_USER_IDS:
        return False
    return from_id in AUTHORIZED_USER_IDS

GROQ_API_KEY = env.get("GROQ_API_KEY", "")
GROQ_MODEL = env.get("GROQ_MODEL", "qwen/qwen3.8-27b")

from sync_engine import (
    BASE_DIR,
    IGNORED_NAMES,
    check_environment,
    discover_repos,
    execute_full_push,
    get_github_urls,
    is_bot_action,
    register_bot_action,
)
from server_manager import (
    start_service,
    stop_service,
    stop_all_services,
    restart_service,
    restart_project,
    get_system_health,
    save_last_error,
    get_last_error,
    load_active_servers,
    get_all_active_project_servers,
    check_and_cleanup_crashes,
    resolve_services_from_input,
    get_local_network_ip,
    SERVER_CONFIGS,
)

APP_DIR = Path(os.environ.get("APP_DIR", str(Path.home() / ".git-auto-sync"))).expanduser()
LOG_PATH = APP_DIR / "bot.log"
HISTORY_PATH = APP_DIR / "conversation_history.json"
COMMIT_ONLY_CONFIG = APP_DIR / "config_commit_only.toml"
from server_manager import PROJECT_ALIASES

KEYBOARD = {
    "keyboard": [
        [{"text": "تقرير الإنجاز"}, {"text": "المستودعات"}],
        [{"text": "حفظ (Commit)"}, {"text": "رفع (Push)"}],
    ],
    "resize_keyboard": True,
    "persistent": True,
}


def log(msg: str) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


class ConversationMemory:
    """Maintains open session memory until explicitly cleared."""

    def __init__(self, storage_path: Path, max_messages: int = 30) -> None:
        self.path = storage_path
        self.max_messages = max_messages
        self.lock = threading.Lock()

    def load(self) -> list[dict]:
        with self.lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return data[-self.max_messages :]
            except Exception as e:
                log(f"Error loading history: {e}")
            return []

    def save_turn(self, user_msg: str, bot_reply: str) -> None:
        with self.lock:
            history = []
            if self.path.exists():
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        history = data
                except Exception:
                    pass
            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": bot_reply})
            history = history[-self.max_messages :]
            try:
                self.path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                log(f"Error saving history: {e}")

    def clear(self) -> None:
        with self.lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except Exception as e:
                log(f"Error clearing history: {e}")


memory = ConversationMemory(HISTORY_PATH)


def send_telegram(chat_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> dict:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    else:
        payload["reply_markup"] = {"remove_keyboard": True}

    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=25)
            if not r.ok and "parse_mode" in payload:
                payload.pop("parse_mode", None)
                r = requests.post(url, json=payload, timeout=25)
            if r.ok:
                return r.json()
            log(f"Telegram send error: {r.text}")
        except Exception as exc:
            log(f"Failed to send telegram message (attempt {attempt+1}): {exc}")
            time.sleep(2)
    return {}


def delete_telegram_message(chat_id: int, message_id: int) -> bool:
    """Deletes a message from Telegram chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "message_id": message_id}, timeout=15)
        return r.ok
    except Exception as exc:
        log(f"Failed to delete telegram message {message_id}: {exc}")
        return False


def edit_telegram_message(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = None) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload: dict = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        r = requests.post(url, json=payload, timeout=25)
        if not r.ok and "parse_mode" in payload:
            payload.pop("parse_mode", None)
            r = requests.post(url, json=payload, timeout=25)
        return r.ok
    except Exception as exc:
        log(f"Failed to edit telegram message {message_id}: {exc}")
        return False


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def set_bot_commands() -> None:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands"
    commands = [
        {"command": "server", "description": "تشغيل سيرفر محلي لأي مشروع مع روابط الشبكة"},
        {"command": "servers", "description": "عرض السيرفرات النشطة حالياً وروابطها"},
        {"command": "stop", "description": "إيقاف سيرفر محدد أو كافة السيرفرات"},
        {"command": "restart", "description": "إعادة تشغيل سريعة للسيرفرات مع تحديث الروابط"},
        {"command": "health", "description": "فحص صحة وموارد جهاز الماك (RAM, CPU, Disks)"},
        {"command": "diagnose", "description": "فحص وتشخيص أخطاء السجلات والعمليات"},
        {"command": "build", "description": "بناء تطبيقات أندرويد واستلام ملفات الـ APK"},
        {"command": "report", "description": "إعداد التقرير الإداري اليومي للمدير"},
        {"command": "interval", "description": "ضبط الفاصل الزمني لمراقبة Actions (0 للحظي)"},
        {"command": "push", "description": "مزامنة ورفع التعديلات إلى GitHub"},
        {"command": "commit", "description": "حفظ التعديلات محلياً فقط دون رفع"},
        {"command": "status", "description": "فحص حالة المستودعات والملفات الحالية"},
        {"command": "clear", "description": "تصفير سياق المحادثة وبدء جلسة جديدة"},
        {"command": "help", "description": "دليل الأوامر والاستخدام"},
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=15)
        log("Bot command menu registered without underscores.")
    except Exception as e:
        log(f"Failed to set bot commands: {e}")


def make_build_inline_keyboard() -> dict:
    from ci_cd_engine import BUILD_TARGETS
    buttons = []
    for k, v in BUILD_TARGETS.items():
        title = v.get("title", k)
        buttons.append([{"text": f"[بناء] {title}", "callback_data": f"build:{k}"}])
    buttons.append([{"text": "[بحث] فلترة واختيار التطبيقات", "switch_inline_query_current_chat": "build "}])
    return {"inline_keyboard": buttons}


def make_server_inline_keyboard(action: str = "server") -> dict:
    verb = "إعادة تشغيل" if action == "restart" else "تشغيل"
    icon = "[RESTART]" if action == "restart" else "[RUN]"
    buttons = []
    seen_projs = set()
    for skey, scfg in SERVER_CONFIGS.items():
        proj = scfg.get("project", skey)
        if proj not in seen_projs:
            seen_projs.add(proj)
            title = scfg.get("title", proj)
            buttons.append([{"text": f"{icon} {verb} {title}", "callback_data": f"{action}:{proj}"}])
    if len(seen_projs) > 1 or not seen_projs:
        buttons.append([{"text": f"{icon} {verb} كافة المشاريع (All Projects)", "callback_data": f"{action}:all"}])
    buttons.append([{"text": "[بحث] فلترة واختيار المشروع", "switch_inline_query_current_chat": f"{action} "}])
    return {"inline_keyboard": buttons}


def make_stop_inline_keyboard() -> dict:
    active = get_all_active_project_servers()
    buttons = []
    for s in active:
        buttons.append([{"text": f"[إيقاف] {s['title']} ({s['port']})", "callback_data": f"stop:{s['key']}"}])
    if len(active) > 1:
        buttons.append([{"text": "[إيقاف الكل] إيقاف كافة السيرفرات (Stop All)", "callback_data": "stop:all"}])
    return {"inline_keyboard": buttons}


def make_project_inline_keyboard(action_prefix: str) -> dict:
    repos = discover_repos()
    buttons = []
    row = []
    for r in repos:
        row.append({"text": r.name, "callback_data": f"{action_prefix}:{r.name}"})
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([{"text": "[شامل] كافة المشاريع", "callback_data": f"{action_prefix}:all"}])
    buttons.append([{"text": "[بحث] فلترة واختيار المشاريع", "switch_inline_query_current_chat": f"{action_prefix} "}])
    return {"inline_keyboard": buttons}



def parse_command(raw_text: str) -> tuple[str, str]:
    text = raw_text.strip()
    if not text.startswith("/"):
        return "", ""
    parts = text[1:].split(None, 1)
    cmd_part = parts[0].lower()
    arg_part = parts[1].strip() if len(parts) > 1 else ""

    if ":" in cmd_part:
        sub_cmd, inline_arg = cmd_part.split(":", 1)
        return sub_cmd, (inline_arg + " " + arg_part).strip()

    if arg_part.startswith(":"):
        arg_part = arg_part[1:].strip()

    # Aliases for interval command
    if cmd_part in {"actions", "interval", "watch", "polling"}:
        return "interval", arg_part

    # Handle prefixed shortcuts like /report_project, /build_target
    for prefix, target_cmd in [
        ("report_", "report"),
        ("build_", "build"),
        ("push_", "push"),
        ("status_", "status"),
    ]:
        if cmd_part.startswith(prefix):
            sub_target = cmd_part[len(prefix):].strip()
            # Dynamically resolve alias
            for cano, aliases in PROJECT_ALIASES.items():
                if sub_target.lower() in [cano.lower()] + [a.lower() for a in aliases]:
                    sub_target = cano
                    break
            return target_cmd, sub_target

    return cmd_part, arg_part




def match_repos(query: str, repos: list[Path]) -> list[Path]:
    q = query.lower()
    matched = []
    for r in repos:
        aliases = PROJECT_ALIASES.get(r.name, []) + [r.name.lower()]
        if any(a in q for a in aliases):
            matched.append(r)
    return matched if matched else repos


def get_repo_work_metrics(repo_path: Path) -> dict:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_path), "log", "--format=%ad|%an|%s", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        lines = [l.strip() for l in res.stdout.strip().splitlines() if "|" in l]
        if not lines:
            return {"name": repo_path.name, "error": "لا توجد أي commits مسجلة في هذا المشروع"}

        dates_by_day = {}
        authors = Counter()
        for l in lines:
            parts = l.split("|", 2)
            dt_str = parts[0]
            authors[parts[1]] += 1
            day_str = dt_str.split()[0]
            try:
                dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
                dates_by_day.setdefault(day_str, []).append(dt)
            except Exception:
                pass

        unique_days = sorted(dates_by_day.keys())
        if not unique_days:
            return {"name": repo_path.name, "error": "لا توجد تواريخ مسجلة"}

        estimated_hours = 0.0
        for day, times in dates_by_day.items():
            if len(times) <= 1:
                estimated_hours += 1.5
            else:
                span = (max(times) - min(times)).total_seconds() / 3600.0
                estimated_hours += max(span + 1.0, 2.0)

        first_dt = datetime.date.fromisoformat(unique_days[0])
        latest_dt = datetime.date.fromisoformat(unique_days[-1])
        calendar_span = (latest_dt - first_dt).days + 1

        commit_list = [f"• [{l.split('|')[0]}] {l.split('|')[2]} ({l.split('|')[1]})" for l in lines]

        return {
            "name": repo_path.name,
            "total_commits": len(lines),
            "distinct_work_days_count": len(unique_days),
            "first_commit": unique_days[0],
            "latest_commit": unique_days[-1],
            "calendar_span_days": calendar_span,
            "estimated_work_hours": round(estimated_hours, 1),
            "authors": dict(authors.most_common(4)),
            "all_commit_days": unique_days,
            "commits_sample": commit_list[:15],
        }
    except Exception as e:
        return {"name": repo_path.name, "error": str(e)}


def call_groq_ai(
    prompt: str,
    system_prompt: str | None = None,
    conversation_history: list[dict] | None = None,
) -> str:
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": prompt})

    models = [GROQ_MODEL, "openai/gpt-oss-120b"]
    for model in models:
        try:
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.2,
                },
                headers=headers,
                timeout=35,
            )
            if resp.status_code == 200:
                content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                if content and content.strip():
                    return content.strip()
        except Exception as exc:
            log(f"Groq API error ({model}): {exc}")
    return ""


def get_work_day_start() -> datetime.datetime:
    """Calculates the start of the current work day, assuming the day begins at 03:00 AM."""
    now = datetime.datetime.now()
    today_3am = now.replace(hour=3, minute=0, second=0, microsecond=0)
    return today_3am if now >= today_3am else today_3am - datetime.timedelta(days=1)


def get_daily_work_data(target_repos: list[Path] | None = None) -> tuple[list[dict], bool, str]:
    repos = target_repos if target_repos else discover_repos()
    data = []
    has_any_activity = False
    start_dt = get_work_day_start()
    start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")

    for r in repos:
        branch = subprocess.run(
            ["git", "-C", str(r), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip() or "unknown"

        # 1. Commits made during today's shift (since 3:00 AM)
        log_res = subprocess.run(
            ["git", "-C", str(r), "log", f"--since={start_str}", "--format=%h|%ad|%s", "--date=format:%H:%M"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        today_commits = [l.strip() for l in log_res.splitlines() if "|" in l]

        # 2. Currently uncommitted changes
        status_raw = subprocess.run(
            ["git", "-C", str(r), "status", "--short"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

        # 3. Diff stat
        diff_stat = subprocess.run(
            ["git", "-C", str(r), "diff", "--stat"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

        # 4. Unpushed commits count
        unpushed_raw = subprocess.run(
            ["git", "-C", str(r), "log", "@{u}..HEAD", "--oneline"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()

        has_activity = bool(today_commits) or bool(status_raw) or bool(unpushed_raw)
        if has_activity:
            has_any_activity = True

        data.append({
            "name": r.name,
            "path": str(r),
            "branch": branch,
            "today_commits": today_commits,
            "status": status_raw,
            "unpushed": unpushed_raw,
            "diff_stat": diff_stat,
            "has_activity": has_activity,
        })

    shift_label = f"منذ الساعة 3:00 فجراً ({start_dt.strftime('%d/%m')}) حتى الآن"
    return data, has_any_activity, shift_label


def get_pending_changes_data() -> tuple[list[dict], bool]:
    data, has_any, _ = get_daily_work_data()
    # Map back to pending format for compatibility
    res = []
    for d in data:
        is_dirty = bool(d["status"]) or bool(d["unpushed"])
        res.append({
            "name": d["name"],
            "path": d["path"],
            "branch": d["branch"],
            "status": d["status"],
            "unpushed": d["unpushed"],
            "diff_stat": d["diff_stat"],
            "is_dirty": is_dirty,
        })
    return res, any(r["is_dirty"] for r in res)


def handle_manager_report(chat_id: int, target_project: str | None = None, menu_msg_id: int | None = None) -> None:
    if not target_project:
        repos = discover_repos()
        repo_examples = "\n".join([f"• /report {r.name}" for r in repos[:4]])
        if not repo_examples:
            repo_examples = "• /report <project_name>"
        menu_text = (
            "[REPORT] اختر المشروع المطلوب لإعداد تقرير الإنجاز اليومي:\n"
            "(يحسب كافة الأعمال المنجزة منذ الساعة 3:00 فجراً)\n\n"
            "[NOTE] يمكنك الاختيار من الأزرار أدناه أو كتابة:\n"
            f"{repo_examples}\n"
            "• /report all — تقرير شامل لكافة المشاريع"
        )
        send_telegram(
            chat_id,
            menu_text,
            reply_markup=make_project_inline_keyboard("report"),
        )
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    init_res = send_telegram(chat_id, f"[...] جاري إعداد التقرير الإداري اليومي ({target_project})...")
    progress_msg_id = init_res.get("result", {}).get("message_id")

    data, has_any, shift_label = get_daily_work_data()

    if t_clean := target_project.strip().lower():
        if t_clean not in {"all", "الكل", "الجميع", "شامل"}:
            filtered = [d for d in data if d["name"].lower() == t_clean or t_clean in d["name"].lower()]
            if not filtered:
                if progress_msg_id:
                    delete_telegram_message(chat_id, progress_msg_id)
                send_telegram(chat_id, f"[WARN] لم يتم العثور على مشروع باسم {target_project}.")
                return
            data = filtered
            has_any = any(d["has_activity"] for d in data)
            if not has_any:
                reply = (
                    f"[INFO] مشروع {data[0]['name']}\n"
                    f"لم تسجل فيه أي تعديلات أو أعمال اليوم ({shift_label})."
                )
                memory.save_turn(f"تقرير {target_project}", reply)
                if progress_msg_id:
                    delete_telegram_message(chat_id, progress_msg_id)
                send_telegram(chat_id, reply)
                return
        else:
            if not has_any:
                reply = (
                    f"[INFO] تقرير اليوم الشامل\n"
                    f"لم تسجل أي تعديلات أو أعمال جديدة في كافة المشاريع اليوم ({shift_label})."
                )
                memory.save_turn("تقرير المدير الشامل", reply)
                if progress_msg_id:
                    delete_telegram_message(chat_id, progress_msg_id)
                send_telegram(chat_id, reply)
                return

    summary_lines = []
    for item in data:
        if item["has_activity"]:
            commits_text = "\n".join([f"  - [{c.split('|')[1]}] {c.split('|')[2]}" for c in item["today_commits"]]) if item["today_commits"] else "  - لا توجد تحديثات مرفوعة اليوم بعد"
            uncommitted_text = item["status"] or "لا توجد تعديلات غير محفوظة"
            summary_lines.append(
                f"المشروع: {item['name']}\n"
                f"التحديثات المنجزة اليوم:\n{commits_text}\n"
                f"تعديلات جارية قيد الإنجاز:\n{uncommitted_text}\n"
                f"إحصائيات الملفات:\n{item['diff_stat']}\n"
            )

    raw_summary = "\n---\n".join(summary_lines)

    system_prompt = (
        "أنت مستشار تنفيذي تقني. المطلوب كتابة تقرير إنجاز يومي موجه لمدير غير تقني يغطي دورة العمل اليومية التي تبدأ الساعة 3:00 فجراً.\n"
        "شروط صارمة:\n"
        "1. ممنوع تماماً استخدام أي مصطلحات برمجية جافة (لا تذكر git, commit, branch, diff, hash, terminal).\n"
        "2. اكتب باختصار شديد وبشكل غير مخل: لخص ما تم إنجازه اليوم بنقاط عملية وواضحة (تحسينات الواجهة، إضافة صفحات، معالجة أخطاء، تجهيز بيئة العمل).\n"
        "3. التنسيق بدون أي علامات markdown معقدة (لا تستخدم أقواس أو نجوم):\n"
        "[REPORT] تقرير إنجاز الأعمال اليومية\n"
        f"• دورة العمل: {shift_label}.\n"
        "• ملخص تنفيذي: سطر واحد موجز يوضح تقدم اليوم.\n"
        "• المشاريع المنجزة: اسم المشروع وتحته نقطتان أو ثلاث فقط توضح الأعمال المكتملة بلغة الأعمال.\n"
        "• الحالة: جاهز للمراجعة / قيد العمل."
    )

    report = call_groq_ai(f"البيانات:\n{raw_summary}", system_prompt=system_prompt)
    if not report:
        report = f"[REPORT] تقرير الإنجاز اليومي ({shift_label}):\n\n" + raw_summary

    memory.save_turn(f"طلب تقرير {target_project}", report)
    if progress_msg_id:
        delete_telegram_message(chat_id, progress_msg_id)
    send_telegram(chat_id, report)



def handle_status(chat_id: int, target_project: str | None = None, menu_msg_id: int | None = None) -> None:
    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    data, _ = get_pending_changes_data()
    if target_project and target_project.lower() not in {"all", "الكل", "الجميع", "شامل"}:
        t_clean = target_project.strip().lower()
        data = [d for d in data if d["name"].lower() == t_clean or t_clean in d["name"].lower()]
        if not data:
            send_telegram(chat_id, f"[WARN] لم يتم العثور على مشروع باسم {target_project}.")
            return

    lines = ["[INFO] حالة المستودعات:", ""]
    for item in data:
        if item["is_dirty"]:
            details = []
            if item["status"]:
                details.append(f"{len(item['status'].splitlines())} ملفات معدلة")
            if item["unpushed"]:
                details.append(f"{len(item['unpushed'].splitlines())} غير مرفوعة")
            desc = ", ".join(details)
            lines.append(f"• {item['name']} ({item['branch']}) [WARN] [{desc}]")
        else:
            lines.append(f"• {item['name']} ({item['branch']}) [OK] متزامن")
    reply = "\n".join(lines)
    memory.save_turn("فحص الحالة", reply)
    send_telegram(chat_id, reply)


def handle_commit(chat_id: int, target_project: str | None = None, menu_msg_id: int | None = None) -> None:
    if not target_project:
        send_telegram(
            chat_id,
            "[COMMIT] اختر المشروع لحفظ تعديلاته محلياً (Commit):",
            reply_markup=make_project_inline_keyboard("commit"),
        )
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    init_res = send_telegram(chat_id, f"[...] جاري الحفظ محلياً ({target_project})...")
    progress_msg_id = init_res.get("result", {}).get("message_id")

    from sync_engine import commit_local_changes_if_dirty

    repos = discover_repos()
    if target_project.lower() not in {"all", "الكل", "الجميع", "شامل"}:
        t_clean = target_project.strip().lower()
        repos = [r for r in repos if r.name.lower() == t_clean or t_clean in r.name.lower()]
        if not repos:
            if progress_msg_id:
                delete_telegram_message(chat_id, progress_msg_id)
            send_telegram(chat_id, f"[WARN] لم يتم العثور على مشروع باسم {target_project}.")
            return

    results = []
    for r in repos:
        ok, msg = commit_local_changes_if_dirty(r)
        if ok and msg:
            results.append(f"• {r.name}: تم الحفظ بنجاح ({msg})")
        elif not ok:
            results.append(f"• {r.name}: خطأ ({msg})")

    if not results:
        reply = "[INFO] لا توجد أي تعديلات جديدة تحتاج للحفظ في المشروع المحدد."
    else:
        reply = "[COMMIT] نتيجة الحفظ المحلي:\n" + "\n".join(results)

    memory.save_turn("حفظ محلي", reply)

    if progress_msg_id:
        delete_telegram_message(chat_id, progress_msg_id)

    send_telegram(chat_id, reply)


def handle_push(chat_id: int, target_project: str | None = None, menu_msg_id: int | None = None) -> None:
    if not target_project:
        send_telegram(
            chat_id,
            "[START] اختر المشروع المطلوب رفعه إلى GitHub:",
            reply_markup=make_project_inline_keyboard("push"),
        )
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    label = "كافة المشاريع" if target_project.lower() in {"all", "الكل"} else target_project
    init_res = send_telegram(chat_id, f"[...] جاري فحص ومزامنة ورفع ({label}) إلى GitHub...")
    progress_msg_id = init_res.get("result", {}).get("message_id")

    result = execute_full_push(target_project)

    if progress_msg_id:
        delete_telegram_message(chat_id, progress_msg_id)

    if not result.get("ok"):
        error_text = f"[ERROR] تعذر الرفع:\n{result.get('error', 'خطأ غير معروف')}"
        send_telegram(chat_id, error_text)
        return

    # Check for conflicts
    if result.get("has_conflicts"):
        conflict_lines = ["[ALERT] تعارض في الدمج (Merge Conflict):"]
        for r in result["results"]:
            if r.get("status") == "conflict":
                conflict_lines.append(f"• المشروع: {r['repo']}")
                conflict_lines.append(f"  الملفات المتعارضة: {', '.join(r.get('conflicts', []))}")
        conflict_lines.append("\nتم إيقاف المزامنة لهذا المشروع لتفادي أخطاء الكود. يرجى حل التعارض يدوياً.")
        text = "\n".join(conflict_lines)
        send_telegram(chat_id, text)
        return

    pushed_items = [r for r in result["results"] if r.get("status") == "pushed"]
    error_items = [r for r in result["results"] if r.get("status") == "error"]

    if not pushed_items and not error_items:
        clean_text = f"[OK] مشروع ({label}) متزامن بالفعل مع GitHub.\nلا توجد أي تعديلات جديدة للرفع."
        send_telegram(chat_id, clean_text)
        return

    resp_lines = []
    if pushed_items:
        resp_lines.append("[OK] تم الرفع بنجاح إلى GitHub\n")
        for p in pushed_items:
            resp_lines.append(f"[REPO] مشروع: {p['repo']}")
            resp_lines.append(f"• الـ Commit الجديد: {p['new_commit_hash']} - {p['new_commit_msg']}")
            if p.get("commit_url"):
                resp_lines.append(f"• رابط الـ Commit: {p['commit_url']}")

            resp_lines.append(f"• الفرع: {p['branch']}")
            if p.get("branch_url"):
                resp_lines.append(f"• رابط الفرع: {p['branch_url']}")

            resp_lines.append(f"• الـ Commit السابق: {p['prev_commit_hash']} (تاريخ: {p['prev_commit_date']})")
            resp_lines.append("")

    if error_items:
        resp_lines.append("[WARN] أخطاء في بعض المشاريع:")
        for e in error_items:
            resp_lines.append(f"• {e['repo']}: {e.get('error')}")

    final_text = "\n".join(resp_lines).strip()
    send_telegram(chat_id, final_text)


from ci_cd_engine import (
    BUILD_TARGETS,
    fetch_recent_workflow_runs,
    trigger_workflow,
    get_latest_run_id,
    poll_run_completion,
    download_artifacts_and_extract_apks,
    send_telegram_apk,
)


def _async_build_worker(chat_id: int, target_key: str) -> None:
    try:
        cfg = BUILD_TARGETS[target_key]
        log(f"Starting async build worker for {target_key} ({cfg['title']})")
        init_msg = send_telegram(chat_id, f"[...] جاري إطلاق سير عمل GitHub Actions لبناء {cfg['title']} (arm64)...")
        msg_id = init_msg.get("result", {}).get("message_id")
        start_ts = time.time()

        ok, err = trigger_workflow(cfg["owner"], cfg["repo"], cfg["workflow"], cfg["branch"], cfg["inputs"])
        if not ok:
            log(f"Failed to trigger workflow for {target_key}: {err}")
            if msg_id:
                delete_telegram_message(chat_id, msg_id)
            send_telegram(chat_id, f"[ERROR] فشل إطلاق الـ Workflow:\n{err}")
            return

        log(f"Workflow triggered successfully for {target_key}. Waiting for GitHub runner...")
        if msg_id:
            edit_telegram_message(chat_id, msg_id, "[SYS] تم إطلاق سير العمل بنجاح.\nبانتظار التقاط الـ Run على خوادم GitHub Runners...")

        run = get_latest_run_id(cfg["owner"], cfg["repo"], cfg["workflow"], start_ts)
        if not run:
            log(f"Run was not captured within 1 minute for {target_key}")
            if msg_id:
                delete_telegram_message(chat_id, msg_id)
            send_telegram(chat_id, "[WARN] لم يتم رصد بدء الـ Run على خوادم GitHub خلال دقيقة واحدة. يرجى التحقق من تبويب Actions في المستودع.")
            return

        run_id = run["id"]
        run_url = run.get("html_url", "")
        log(f"Captured run {run_id} for {target_key}: {run_url}")

        def on_progress(status, elapsed, url, active_step, step_lines):
            status_ar = "قيد التنفيذ [...]" if status == "in_progress" else f"في الانتظار ({status})"
            active_desc = active_step if active_step else "جاري معالجة المهام..."
            steps_block = "\n".join(step_lines) if step_lines else "• جاري استدعاء مراحل السير..."
            text = (
                f"[SYS] جاري بناء {cfg['title']}...\n"
                f"• المعمارية: arm64-v8a\n"
                f"• الحالة: {status_ar} (المدة: {elapsed})\n"
                f"• الخطوة الحالية: {active_desc}\n\n"
                f"مراحل سير العمل (GitHub Actions):\n"
                f"{steps_block}\n\n"
                f"[URL] رابط السجلات الحية على GitHub:\n{url}"
            )
            if msg_id:
                edit_telegram_message(chat_id, msg_id, text)

        result = poll_run_completion(cfg["owner"], cfg["repo"], run_id, on_progress=on_progress)
        conclusion = result.get("conclusion")
        log(f"Run {run_id} for {target_key} finished with conclusion: {conclusion}")

        if conclusion == "success":
            if msg_id:
                delete_telegram_message(chat_id, msg_id)

            apks = download_artifacts_and_extract_apks(cfg["owner"], cfg["repo"], run_id)
            log(f"Downloaded {len(apks)} APK(s) for run {run_id}")
            if apks:
                direct_release_url = f"https://github.com/{cfg['owner']}/{cfg['repo']}/releases/download/latest-apk"
                for apk in apks:
                    size_mb = round(apk.stat().st_size / (1024 * 1024), 1)
                    direct_apk_link = f"{direct_release_url}/{apk.name}"
                    caption = (
                        f"[APP] تطبيق جاهز للتثبيت والاستخدام:\n"
                        f"• التطبيق: {cfg['title']}\n"
                        f"• الملف: {apk.name} ({size_mb} MB)\n"
                        f"• المعمارية: arm64-v8a\n"
                        f"• وضع البناء: Production Release\n"
                        f"• تاريخ البناء: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        f"• رابط التنزيل المباشر: {direct_apk_link}\n"
                        f"• رابط البناء: {run_url}"
                    )
                    sent = send_telegram_apk(chat_id, apk, caption)
                    if not sent:
                        send_telegram(
                            chat_id,
                            f"[APP] تطبيق جاهز للتثبيت ({cfg['title']}):\n"
                            f"• الملف: {apk.name} ({size_mb} MB)\n"
                            f"• المعمارية: arm64-v8a\n"
                            f"• وضع البناء: Production Release\n\n"
                            f"[URL] رابط التنزيل المباشر لملف الـ APK:\n{direct_apk_link}\n\n"
                            f"رابط الـ Run على GitHub:\n{run_url}",
                        )
                send_telegram(chat_id, "[OK] تم إنجاز وتسليم ملفات الـ APK بنجاح. يمكنك الآن تثبيتها وتجربتها على هاتفك مباشرة.")
            else:
                send_telegram(
                    chat_id,
                    f"[OK] اكتمل البناء بنجاح ولكن لم يتم العثور على ملفات APK في الـ Artifacts.\nرابط الـ Run على GitHub:\n{run_url}",
                )
        else:
            if msg_id:
                delete_telegram_message(chat_id, msg_id)
            failed_log = f"GitHub Actions workflow run failed with conclusion: {conclusion}.\nRun URL: {run_url}\nWorkflow: {cfg.get('workflow')}\nRepository: {cfg.get('owner')}/{cfg.get('repo')}"
            save_last_error(f"build_{target_key}", f"بناء أندرويد ({cfg['title']})", failed_log, reason=f"GitHub Actions Failed ({conclusion})")
            err_text = (
                f"[ERROR] فشل بناء تطبيق الأندرويد على GitHub Actions:\n"
                f"• النتيجة: {conclusion}\n"
                f"• رابط تفاصيل الخطأ على GitHub:\n{run_url}"
            )
            diag_btn = {"inline_keyboard": [[{"text": "[تشخيص سجل الخطأ]", "callback_data": f"aidiag:build_{target_key}"}]]}
            send_telegram(chat_id, err_text, reply_markup=diag_btn)
    except Exception as exc:
        import traceback
        log(f"Unexpected error in _async_build_worker for {target_key}: {traceback.format_exc()}")
        send_telegram(chat_id, f"[ERROR] حدث خطأ أثناء متابعة عملية البناء:\n{exc}")


def handle_build(chat_id: int, target: str | None = None, menu_msg_id: int | None = None) -> None:
    if not target:
        examples = "\n".join([f"• /build {k} — {v.get('title', k)}" for k, v in list(BUILD_TARGETS.items())[:3]])
        msg = (
            "[APP] بناء التطبيقات عبر GitHub Actions:\n\n"
            "اختر الهدف المطلوب بناؤه عبر الأزرار أدناه، أو اكتب الأمر مباشرة:\n\n"
            f"{examples}\n\n"
            "[NOTE] يمكنك الضغط على زر الفلترة أدناه لاختيار أي هدف متوفر."
        )
        send_telegram(
            chat_id,
            msg,
            reply_markup=make_build_inline_keyboard(),
        )
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    t_clean = target.strip().lower()
    target_key = None
    if t_clean in BUILD_TARGETS:
        target_key = t_clean
    else:
        for k, v in BUILD_TARGETS.items():
            if t_clean in k.lower() or t_clean in v.get("title", "").lower() or t_clean in v.get("app", "").lower():
                target_key = k
                break

    if not target_key:
        opts = ", ".join(list(BUILD_TARGETS.keys()))
        send_telegram(
            chat_id,
            f"[WARN] الهدف {target} غير معروف.\nالخيارات المتاحة: {opts}",
            reply_markup=make_build_inline_keyboard(),
        )
        return

    threading.Thread(target=_async_build_worker, args=(chat_id, target_key), daemon=True).start()


def handle_server(chat_id: int, target: str = "", menu_msg_id: int | None = None) -> None:
    if not target:
        examples = "\n".join([f"• `/server {p}` — تشغيل {scfg.get('title', p)}" for p, scfg in list(SERVER_CONFIGS.items())[:3]])
        msg = (
            "[START] *تشغيل السيرفرات المحلية للمشاريع بالكامل:*\n\n"
            "اختر المشروع المراد تشغيله من القائمة أدناه، أو اكتب الأمر مباشرة:\n\n"
            f"{examples}\n"
            "• `/server all` — تشغيل كافة السيرفرات دفعة واحدة\n\n"
            "[NOTE] *ملاحظة ذكية:* في حال كان المشروع أو أي جزء منه يعمل بالفعل على الجهاز (سواء عبر البوت أو من التيرمينال)، سيتم إعلامك فوراً بروابطه وتوقيت تشغيله ولن يتم تكرار تشغيله."
        )
        send_telegram(chat_id, msg, reply_markup=make_server_inline_keyboard("server"))
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    services = resolve_services_from_input(target)
    if not services:
        send_telegram(
            chat_id,
            f"[WARN] لم يتم التعرف على المشروع: {target}\nيرجى الاختيار من القائمة أدناه:",
            reply_markup=make_server_inline_keyboard("server"),
        )
        return

    for svc in services:
        ok, info, err = start_service(svc)
        if not ok or not info:
            send_telegram(chat_id, f"[ERROR] فشل تشغيل {svc}:\n{err}")
            continue

        if info.get("is_already_running"):
            send_telegram(
                chat_id,
                f"[INFO] *{info.get('title')} يعمل بالفعل حالياً على الجهاز!*\n\n"
                f"⏱ *وقت البدء:* `{info.get('started_at')}`\n"
                f" *البورت:* `{info.get('port')}` | *PID:* `{info.get('pid')}`\n"
                f" *المصدر:* {info.get('source', 'مباشر من الجهاز')}\n\n"
                f"[URL] *روابط الاستخدام:*\n"
                f"• *محلياً (Local):*\n  {info.get('local_url')}\n"
                f"• *على نطاق الشبكة (LAN / Wi-Fi):*\n  {info.get('network_url')}"
            )
            continue

        port_shift_note = ""
        if info.get("port_shifted"):
            port_shift_note = f"[WARN] *ملاحظة ذكية:* البورت الافتراضي ({info.get('preferred_port')}) كان محجوزاً، وتم تحويل السيرفر تلقائياً إلى البورت المتاح: `{info.get('port')}`.\n\n"

        msg = (
            f"[OK] *تم تشغيل السيرفر بنجاح!*\n\n"
            f"⏱ *توقيت التشغيل:* `{info.get('started_at')}`\n"
            f" *الخدمة:* {info.get('title')}\n"
            f" *البورت:* `{info.get('port')}` | *PID:* `{info.get('pid')}`\n\n"
            f"{port_shift_note}"
            f"[URL] *روابط الاستخدام:*\n"
            f"• *محلياً (Local):*\n  {info.get('local_url')}\n"
            f"• *على نطاق الشبكة (LAN / Wi-Fi):*\n  {info.get('network_url')}\n\n"
            f" *ملف السجل (Logs):*\n`{info.get('log_file')}`"
        )
        send_telegram(chat_id, msg)


def handle_servers_status(chat_id: int) -> None:
    active = get_all_active_project_servers()
    if not active:
        send_telegram(
            chat_id,
            "[INFO] لا توجد أي سيرفرات نشطة حالياً لأي من المشاريع على الجهاز.",
        )
        return

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[SYSTEM] *السيرفرات النشطة حالياً على الجهاز ({len(active)}):*\n"]
    for s in active:
        source_badge = f"- *{s.get('title')}* — _{s.get('source', '')}_\n"
        lines.append(
            f"{source_badge}"
            f"• وقت البدء: `{s.get('started_at')}`\n"
            f"• البورت: `{s.get('port')}` | PID: `{s.get('pid')}`\n"
            f"• محلياً: {s.get('local_url')}\n"
            f"• الشبكة: {s.get('network_url')}\n"
        )

    lines.append(f"⏱ *توقيت الفحص:* `{now_str}`\nلإيقاف أي سيرفر، يمكنك الاختيار أدناه:")
    send_telegram(chat_id, "\n".join(lines), reply_markup=make_stop_inline_keyboard())


def handle_stop_server(chat_id: int, target: str = "", menu_msg_id: int | None = None) -> None:
    active = get_all_active_project_servers()
    if not target:
        if not active:
            send_telegram(chat_id, "[INFO] لا توجد أي سيرفرات نشطة حالياً على الجهاز لإيقافها.")
            return
        send_telegram(chat_id, "[STOP] اختر السيرفر المراد إيقافه:", reply_markup=make_stop_inline_keyboard())
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    target_clean = target.strip().lower()
    if target_clean in {"all", "الكل", "كافة"}:
        results = stop_all_services()
        if not results:
            send_telegram(chat_id, "[INFO] لم تكن هناك أي سيرفرات نشطة على الجهاز.")
        else:
            send_telegram(chat_id, "[STOP] *نتائج الإيقاف الشامل:*\n\n" + "\n".join(f"• {r}" for r in results))
        return

    services = resolve_services_from_input(target_clean)
    if not services:
        for s in active:
            if s.get("key") == target_clean:
                services = [target_clean]
                break

    if not services:
        send_telegram(chat_id, f"[WARN] لم يتم العثور على سيرفر نشط يطابق: {target}")
        return

    for svc in services:
        ok, msg = stop_service(svc)
        send_telegram(chat_id, ("[OK] " if ok else "[ERROR] ") + msg)


def server_monitor_worker() -> None:
    """Continuously monitors running local servers and alerts instantly if any crash or close unexpectedly."""
    log("Server health monitor worker started.")
    while True:
        try:
            crashed = check_and_cleanup_crashes()
            for c in crashed:
                err_tail = c.get("error_snippet", "").strip()
                tail_section = f"\n\n *آخر أسطر من السجل (Error Log):*\n```\n{err_tail}\n```" if err_tail else ""
                alert_msg = (
                    f"[WARN] *تنبيه طارئ: توقف سيرفر محلي بشكل مفاجئ!*\n\n"
                    f"⏱ *توقيت الرصد:* `{c.get('detected_at')}`\n"
                    f" *السيرفر:* {c.get('title')}\n"
                    f" *البورت:* `{c.get('port')}` | *PID:* `{c.get('pid')}`\n"
                    f"[STOP] *سبب التوقف:* {c.get('reason')}"
                    f"{tail_section}\n\n"
                    f"[NOTE] تم تنظيف وحذف السيرفر من قائمة السيرفرات النشطة. لإعادة تشغيله اكتب: `/server {c.get('key')}`"
                )
                if AUTHORIZED_USER_ID:
                    diag_btn = {"inline_keyboard": [[{"text": "[تشخيص سجل الخطأ]", "callback_data": f"aidiag:{c.get('key')}"}]]}
                    send_telegram(AUTHORIZED_USER_ID, alert_msg, reply_markup=diag_btn)
        except Exception as e:
            log(f"Error in server_monitor_worker: {e}")
        time.sleep(5)


def handle_restart(chat_id: int, target: str = "", menu_msg_id: int | None = None) -> None:
    """Gracefully restarts a project or all projects with exact timestamps."""
    if not target:
        examples = "\n".join([f"• `/restart {p}` — إعادة تشغيل {scfg.get('title', p)}" for p, scfg in list(SERVER_CONFIGS.items())[:3]])
        msg = (
            "[RESTART] *إعادة التشغيل السريع للسيرفرات المحلية:*\n\n"
            "اختر المشروع المراد إعادة تشغيله (إيقاف أنيق، تحرير البورتات، وإعادة إطلاق فورية):\n\n"
            f"{examples}\n"
            "• `/restart all` — إعادة تشغيل كافة السيرفرات دفعة واحدة"
        )
        send_telegram(chat_id, msg, reply_markup=make_server_inline_keyboard("restart"))
        return

    if menu_msg_id:
        delete_telegram_message(chat_id, menu_msg_id)

    services = resolve_services_from_input(target)
    if not services:
        send_telegram(chat_id, f"[WARN] لم يتم التعرف على المشروع: {target}")
        return

    send_telegram(chat_id, f"[...] جاري إعادة تشغيل {target} (إيقاف وتحرير البورتات ثم إعادة الإطلاق)...")

    for svc in services:
        ok, info, stop_msg = restart_service(svc)
        if not ok or not info:
            send_telegram(chat_id, f"[ERROR] فشل إعادة تشغيل {svc}:\n{stop_msg}")
            continue

        port_shift_note = ""
        if info.get("port_shifted"):
            port_shift_note = f"[WARN] *ملاحظة ذكية:* تم التحويل للبورت المتاح: `{info.get('port')}`.\n\n"

        msg = (
            f"[RESTART] *تم إعادة تشغيل السيرفر بنجاح!*\n\n"
            f" *الخدمة:* {info.get('title')}\n"
            f" *البورت:* `{info.get('port')}` | *PID:* `{info.get('pid')}`\n"
            f"⏱ *توقيت التشغيل الجديد:* `{info.get('started_at')}`\n\n"
            f"{port_shift_note}"
            f"[URL] *روابط الاستخدام:*\n"
            f"• *محلياً (Local):*\n  {info.get('local_url')}\n"
            f"• *على نطاق الشبكة (LAN / Wi-Fi):*\n  {info.get('network_url')}\n\n"
            f" *ملف السجل (Logs):*\n`{info.get('log_file')}`"
        )
        send_telegram(chat_id, msg)


def handle_health(chat_id: int) -> None:
    """Reports macOS system health, hardware load, and resource metrics."""
    h = get_system_health()
    disks_lines = []
    for d in h.get("disks", []):
        disks_lines.append(f"• *{d['name']}:* مستخدم `{d['used']}` من `{d['size']}` (المتبقي: `{d['avail']}` — النسبة: `{d['capacity']}`)")
    disks_str = "\n".join(disks_lines) if disks_lines else "• غير متاح"

    msg = (
        f"[SYSTEM] *تقرير صحة وموارد جهاز الماك (System Health):*\n\n"
        f" *الذاكرة العشوائية (RAM):*\n"
        f"• المستخدم الفعلي: `{h['used_ram_gb']} GB` من `{h['total_ram_gb']} GB` ({h['ram_percent']}%)\n\n"
        f"[SYS] *المعالج (CPU):*\n"
        f"• الحمل الحالي: `{h['cpu_percent']}%` ({h['ncpu']} Cores)\n"
        f"• متوسط الحمل (Load Avg): `{h['load_str']}`\n\n"
        f"[COMMIT] *سعة التخزين (Disks):*\n{disks_str}\n\n"
        f"⏱ *مدة تشغيل الجهاز (Uptime):* {h['uptime']}\n"
        f"[START] *السيرفرات النشطة حالياً:* {h['active_servers_count']} سيرفر(ات)\n\n"
        f"⏱ *توقيت الفحص:* `{h['timestamp']}`"
    )
    send_telegram(chat_id, msg)


def handle_ai_diagnose(chat_id: int, target: str = "") -> None:
    """Analyzes the latest server crash or CI/CD error using Groq AI."""
    target_clean = target.strip().lower()
    err_info = get_last_error(target_clean)
    if not err_info and target_clean:
        err_info = get_last_error("")

    if not err_info or not err_info.get("snippet"):
        send_telegram(chat_id, "[INFO] لا توجد سجلات أخطاء حديثة مسجلة حالياً.")
        return

    wait_msg = send_telegram(chat_id, f"[*] جاري فحص وتشخيص السجل لـ: {err_info.get('title')}...")
    wait_id = wait_msg.get("result", {}).get("message_id") if wait_msg else None

    prompt = (
        f"أنت مهندس أنظمة ومطور Full-Stack خبير.\n"
        f"حدث خطأ أو توقف مفاجئ في الخدمة التالية على جهاز المطور:\n"
        f"• المشروع / الخدمة: {err_info.get('title')}\n"
        f"• سبب التوقف المرصود: {err_info.get('reason')}\n"
        f"• سجل الأخطاء الأخير:\n```\n{err_info.get('snippet')}\n```\n\n"
        f"المطلوب تقديم تحليل موجز جداً، احترافي، ومباشر باللغة العربية بالتنسيق التالي:\n"
        f"1. [FIND] *السبب الجذري (Root Cause):* سطرين لشرح المشكلة بدقة دون تعقيد.\n"
        f"2.  *موقع الخطأ (Location):* اسم الملف والسطر إن توفر في السجل.\n"
        f"3. [FIX] *الحل المقترح (Recommended Fix):* الأمر أو الخطوات الدقيقة لتصحيح المشكلة فوراً."
    )

    reply = call_groq_ai(prompt, system_prompt="أنت محرك تشخيص أخطاء تقني (Diagnostic Engine). ردك تقني، مباشر، موجز، وخالٍ تماماً من أي إيموجيز أو مقدمات أو عبارات ترحيبية. ممنوع أن تظهر كشات أو مساعد ذكي.")
    if wait_id:
        delete_telegram_message(chat_id, wait_id)

    if not reply:
        reply = "[ERROR] تعذر فحص السجل حالياً بسبب تعذر الاتصال بمحرك التحليل."

    header = (
        f"--- DIAGNOSTIC REPORT ---\n\n"
        f" *الخدمة:* {err_info.get('title')}\n"
        f"⏱ *توقيت الخطأ:* `{err_info.get('timestamp')}`\n\n"
    )
    send_telegram(chat_id, header + reply)


REMOTE_AHEAD_STATE_PATH = APP_DIR / "remote_ahead_state.json"


def load_remote_ahead_state() -> dict:
    if REMOTE_AHEAD_STATE_PATH.exists():
        try:
            with open(REMOTE_AHEAD_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_remote_ahead_state(state: dict) -> None:
    try:
        with open(REMOTE_AHEAD_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def check_git_remote_ahead() -> list[dict]:
    """Checks all discovered repos to see if remote tracking branch has new commits ahead of local active branch."""
    repos = discover_repos()
    state = load_remote_ahead_state()
    alerts = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for r in repos:
        repo_name = r.name
        try:
            branch = subprocess.check_output(
                ["git", "-C", str(r), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()

            if not branch or branch == "HEAD":
                continue

            remotes = subprocess.check_output(
                ["git", "-C", str(r), "remote"],
                text=True, stderr=subprocess.DEVNULL
            ).strip().split()
            if "origin" not in remotes:
                continue

            fetch_res = subprocess.run(
                ["git", "-C", str(r), "fetch", "origin", branch, "--quiet"],
                capture_output=True, text=True, check=False
            )
            if fetch_res.returncode != 0:
                continue

            ahead_cnt_str = subprocess.check_output(
                ["git", "-C", str(r), "rev-list", "--count", f"HEAD..origin/{branch}"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()
            if not ahead_cnt_str.isdigit():
                continue

            ahead_count = int(ahead_cnt_str)
            if ahead_count <= 0:
                continue

            remote_hash = subprocess.check_output(
                ["git", "-C", str(r), "rev-parse", f"origin/{branch}"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()

            state_key = f"{repo_name}:{branch}"
            if remote_hash == state.get(state_key):
                continue

            log_out = subprocess.check_output(
                ["git", "-C", str(r), "log", f"HEAD..origin/{branch}", "--pretty=format:• %h: %s (%an, %ar)", "-n", "5"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()

            alerts.append({
                "repo": repo_name,
                "branch": branch,
                "ahead_count": ahead_count,
                "remote_hash": remote_hash,
                "log_summary": log_out,
                "timestamp": now_str,
            })
            state[state_key] = remote_hash
        except Exception:
            continue

    if alerts:
        save_remote_ahead_state(state)
    return alerts


def git_remote_monitor_worker() -> None:
    """Periodically checks if remote repositories have new commits on active branches."""
    log("Git remote ahead monitor worker started.")
    time.sleep(15)
    while True:
        try:
            alerts = check_git_remote_ahead()
            for a in alerts:
                msg = (
                    f"[REMOTE] *تنبيه: تحديثات جديدة في المستودع البعيد (Remote Updates)!*\n\n"
                    f" *المشروع:* `{a['repo']}`\n"
                    f" *الفرع النشط:* `{a['branch']}`\n"
                    f" *عدد التعديلات الجديدة:* {a['ahead_count']} Commit(s)\n\n"
                    f" *آخر التعديلات:*\n{a['log_summary']}\n\n"
                    f"⏱ *توقيت الرصد:* `{a['timestamp']}`\n"
                    f"[NOTE] للمزامنة الفورية اكتب: `/push {a['repo']}` أو اسحب التعديلات (git pull) من جهازك."
                )
                if AUTHORIZED_USER_ID:
                    send_telegram(AUTHORIZED_USER_ID, msg)
        except Exception as e:
            log(f"Error in git_remote_monitor_worker: {e}")
        time.sleep(60)


LOCAL_ACTIVITY_STATE_PATH = APP_DIR / "local_activity_state.json"


def load_local_activity_state() -> dict:
    if LOCAL_ACTIVITY_STATE_PATH.exists():
        try:
            with open(LOCAL_ACTIVITY_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_local_activity_state(state: dict) -> None:
    try:
        with open(LOCAL_ACTIVITY_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def check_git_local_activity(state: dict, is_initial: bool = False) -> tuple[dict, list[str]]:
    """Inspects all local repos for branch switches, commits, pushes, and pulls made outside the bot."""
    repos = discover_repos()
    alerts = []
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for r in repos:
        repo_name = r.name
        try:
            # 1. Active branch
            branch = subprocess.check_output(
                ["git", "-C", str(r), "rev-parse", "--abbrev-ref", "HEAD"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()

            if not branch or branch == "HEAD":
                continue

            # 2. Local HEAD
            head = subprocess.check_output(
                ["git", "-C", str(r), "rev-parse", "HEAD"],
                text=True, stderr=subprocess.DEVNULL
            ).strip()

            # 3. Remote tracking ref
            remote_ref = ""
            try:
                remote_ref = subprocess.check_output(
                    ["git", "-C", str(r), "rev-parse", f"refs/remotes/origin/{branch}"],
                    text=True, stderr=subprocess.DEVNULL
                ).strip()
            except Exception:
                pass

            prev = state.get(repo_name)
            if prev is None or is_initial:
                state[repo_name] = {
                    "branch": branch,
                    "head": head,
                    "remote_ref": remote_ref,
                }
                continue

            old_branch = prev.get("branch", branch)
            old_head = prev.get("head", head)
            old_remote_ref = prev.get("remote_ref", remote_ref)

            # --- Case A: Branch Switched ---
            if branch != old_branch:
                state[repo_name] = {
                    "branch": branch,
                    "head": head,
                    "remote_ref": remote_ref,
                }
                alerts.append(
                    f" *تغيير الفرع النشط في المشروع (Git Branch):*\n\n"
                    f" *المشروع:* `{repo_name}`\n"
                    f" *الفرع الجديد:* `{branch}`\n"
                    f" *الفرع السابق:* `{old_branch}`\n"
                    f"⏱ *توقيت التغيير:* `{now_str}`"
                )
                continue

            # --- Case B: Remote Ref Changed (Push to remote) ---
            if remote_ref and old_remote_ref and remote_ref != old_remote_ref:
                is_push_from_here = False
                try:
                    ahead_remote = subprocess.check_output(
                        ["git", "-C", str(r), "rev-list", "--count", f"{head}..{remote_ref}"],
                        text=True, stderr=subprocess.DEVNULL
                    ).strip()
                    if ahead_remote == "0":
                        is_push_from_here = True
                except Exception:
                    if remote_ref == head:
                        is_push_from_here = True

                if is_push_from_here:
                    if not is_bot_action("push", repo_name, remote_ref):
                        pushed_count = 1
                        log_summary = ""
                        try:
                            cnt_str = subprocess.check_output(
                                ["git", "-C", str(r), "rev-list", "--count", f"{old_remote_ref}..{remote_ref}"],
                                text=True, stderr=subprocess.DEVNULL
                            ).strip()
                            if cnt_str.isdigit() and int(cnt_str) > 0:
                                pushed_count = int(cnt_str)
                            log_summary = subprocess.check_output(
                                ["git", "-C", str(r), "log", f"{old_remote_ref}..{remote_ref}", "--pretty=format:• %h: %s (%an)", "-n", "5"],
                                text=True, stderr=subprocess.DEVNULL
                            ).strip()
                        except Exception:
                            pass

                        if not log_summary:
                            try:
                                log_summary = subprocess.check_output(
                                    ["git", "-C", str(r), "log", "-1", "--pretty=format:• %h: %s (%an)", remote_ref],
                                    text=True, stderr=subprocess.DEVNULL
                                ).strip()
                            except Exception:
                                pass

                        _, commit_url = get_github_urls(r, "origin", branch, remote_ref[:8])
                        url_section = f"\n[URL] [عرض التعديلات على GitHub]({commit_url})" if commit_url else ""

                        alerts.append(
                            f"[START] *تم رفع تعديلات إلى GitHub (Git Push من الجهاز):*\n\n"
                            f" *المشروع:* `{repo_name}`\n"
                            f" *الفرع:* `{branch}`\n"
                            f" *عدد التعديلات المرفوعة:* {pushed_count} Commit(s)\n\n"
                            f" *التعديلات:*\n{log_summary}\n\n"
                            f"⏱ *توقيت الرفع:* `{now_str}`"
                            f"{url_section}"
                        )
                prev["remote_ref"] = remote_ref

            # --- Case C: Local HEAD Changed (Commit or Pull) ---
            if head != old_head:
                if remote_ref and head == remote_ref and old_remote_ref == remote_ref:
                    pull_summary = ""
                    try:
                        pull_summary = subprocess.check_output(
                            ["git", "-C", str(r), "log", "-1", "--pretty=format:• %h: %s (%an)", head],
                            text=True, stderr=subprocess.DEVNULL
                        ).strip()
                    except Exception:
                        pass
                    alerts.append(
                        f"[PULL] *تم سحب وتحديث الكود محلياً (Git Pull من الجهاز):*\n\n"
                        f" *المشروع:* `{repo_name}`\n"
                        f" *الفرع:* `{branch}`\n"
                        f" *آخر تحديث مدمج:*\n{pull_summary}\n\n"
                        f"⏱ *توقيت السحب:* `{now_str}`"
                    )
                else:
                    if not is_bot_action("commit", repo_name, head):
                        commit_count = 1
                        commit_summary = ""
                        try:
                            cnt_str = subprocess.check_output(
                                ["git", "-C", str(r), "rev-list", "--count", f"{old_head}..{head}"],
                                text=True, stderr=subprocess.DEVNULL
                            ).strip()
                            if cnt_str.isdigit() and int(cnt_str) > 0:
                                commit_count = int(cnt_str)
                            commit_summary = subprocess.check_output(
                                ["git", "-C", str(r), "log", f"{old_head}..{head}", "--pretty=format:• %h: %s (%an, %ar)", "-n", "5"],
                                text=True, stderr=subprocess.DEVNULL
                            ).strip()
                        except Exception:
                            pass

                        if not commit_summary:
                            try:
                                commit_summary = subprocess.check_output(
                                    ["git", "-C", str(r), "log", "-1", "--pretty=format:• %h: %s (%an, %ar)", head],
                                    text=True, stderr=subprocess.DEVNULL
                                ).strip()
                            except Exception:
                                pass

                        alerts.append(
                            f"[COMMIT] *تم تسجيل حفظ محلي جديد (Git Commit من الجهاز):*\n\n"
                            f" *المشروع:* `{repo_name}`\n"
                            f" *الفرع:* `{branch}`\n"
                            f" *العدد:* {commit_count} Commit(s)\n\n"
                            f" *تفاصيل الحفظ:*\n{commit_summary}\n\n"
                            f"⏱ *توقيت الحفظ:* `{now_str}`\n"
                            f"[NOTE] لرفع التعديل إلى GitHub يمكنك كتابة: `/push {repo_name}` أو تشغيل `git push` من جهازك."
                        )
                prev["head"] = head

            state[repo_name] = prev
        except Exception:
            continue

    return state, alerts


def git_local_activity_monitor_worker() -> None:
    """Continuously monitors local git repositories for commits, pushes, pulls, and branch changes made outside the bot."""
    log("Git local activity monitor worker started.")
    state = load_local_activity_state()
    is_initial = (len(state) == 0)

    while True:
        try:
            state, alerts = check_git_local_activity(state, is_initial=is_initial)
            if is_initial:
                save_local_activity_state(state)
                is_initial = False
            elif alerts:
                save_local_activity_state(state)
                for msg in alerts:
                    if AUTHORIZED_USER_ID:
                        send_telegram(AUTHORIZED_USER_ID, msg)
        except Exception as e:
            log(f"Error in git_local_activity_monitor_worker: {e}")
        time.sleep(4)


CONFIG_PATH = APP_DIR / "config.json"
ACTIONS_STATE_PATH = APP_DIR / "actions_monitor_state.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"actions_interval": 30}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"Error saving config: {e}")


def load_actions_state() -> dict:
    if ACTIONS_STATE_PATH.exists():
        try:
            with open(ACTIONS_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_actions_state(state: dict) -> None:
    try:
        with open(ACTIONS_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"Error saving actions state: {e}")


def get_monitored_repos() -> list[tuple[str, str, str]]:
    """Returns list of (display_name, owner, repo) for all GitHub remotes across discovered projects."""
    repos = []
    seen = set()
    for local_repo in discover_repos():
        res = subprocess.run(
            ["git", "-C", str(local_repo), "remote", "-v"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in res.stdout.strip().splitlines():
            if "(fetch)" not in line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                raw_url = parts[1].replace(".git", "")
                if "github.com" in raw_url:
                    if "git@github.com:" in raw_url:
                        path_part = raw_url.split("git@github.com:")[1]
                    else:
                        path_part = raw_url.split("github.com/")[1]
                    segs = path_part.split("/")
                    if len(segs) >= 2:
                        owner, rname = segs[0], segs[1]
                        key = f"{owner}/{rname}".lower()
                        if key not in seen:
                            seen.add(key)
                            repos.append((local_repo.name, owner, rname))
    return repos


def actions_monitor_worker() -> None:
    """Background daemon worker to continuously monitor GitHub Actions and push updates."""
    log("Actions monitor background worker started.")
    state = load_actions_state()
    is_initial = (len(state) == 0)

    while True:
        try:
            cfg = load_config()
            interval = cfg.get("actions_interval", 30)
            sleep_duration = 5 if interval == 0 else max(interval, 5)

            monitored = get_monitored_repos()
            for local_name, owner, rname in monitored:
                runs = fetch_recent_workflow_runs(owner, rname, per_page=4)
                for run in runs:
                    run_id = run["id"]
                    run_key = f"{owner}/{rname}/{run_id}"
                    status = run.get("status")          # queued, in_progress, completed
                    conclusion = run.get("conclusion")  # success, failure, etc.
                    html_url = run.get("html_url", "")
                    wf_name = run.get("name", "Workflow")
                    branch = run.get("head_branch", "main")
                    event = run.get("event", "dispatch")

                    prev = state.get(run_key)

                    if is_initial:
                        state[run_key] = {"status": status, "conclusion": conclusion}
                        continue

                    if prev is None:
                        # Brand new run started
                        state[run_key] = {"status": status, "conclusion": conclusion}
                        save_actions_state(state)

                        status_ar = "قيد التنفيذ [...]" if status == "in_progress" else f"في الانتظار ({status})"
                        msg = (
                            f"[SYS] *بدء تشغيل سير عمل جديد على GitHub Actions:*\n"
                            f"• المشروع: *{local_name}* (`{owner}/{rname}`)\n"
                            f"• السير (Workflow): *{wf_name}*\n"
                            f"• الفرع: `{branch}`\n"
                            f"• الحدث: `{event}`\n"
                            f"• الحالة: {status_ar}\n\n"
                            f"[URL] [متابعة السجلات الحية على GitHub]({html_url})"
                        )
                        send_telegram(AUTHORIZED_USER_ID, msg)

                    elif prev.get("status") != status or prev.get("conclusion") != conclusion:
                        # Status/conclusion changed
                        state[run_key] = {"status": status, "conclusion": conclusion}
                        save_actions_state(state)

                        if status == "completed":
                            icon = "[OK]" if conclusion == "success" else "[ERROR]"
                            concl_ar = "اكتمل بنجاح" if conclusion == "success" else f"فشل ({conclusion})"

                            created_at = run.get("created_at")
                            updated_at = run.get("updated_at")
                            dur_str = ""
                            if created_at and updated_at:
                                try:
                                    dt1 = datetime.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                                    dt2 = datetime.datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                                    sec = int((dt2 - dt1).total_seconds())
                                    dur_str = f"\n• المدة المستغرقة: `{sec // 60}m {sec % 60}s`"
                                except Exception:
                                    pass

                            msg = (
                                f"{icon} *تحديث حالة سير عمل GitHub Actions:*\n"
                                f"• المشروع: *{local_name}* (`{owner}/{rname}`)\n"
                                f"• السير (Workflow): *{wf_name}*\n"
                                f"• الفرع: `{branch}`\n"
                                f"• النتيجة: *{concl_ar}*{dur_str}\n\n"
                                f"[URL] [عرض تفاصيل الـ Run على GitHub]({html_url})"
                            )
                            send_telegram(AUTHORIZED_USER_ID, msg)

                            if conclusion == "success":
                                apks = download_artifacts_and_extract_apks(owner, rname, run_id)
                                for apk in apks:
                                    size_mb = round(apk.stat().st_size / (1024 * 1024), 1)
                                    caption = (
                                        f"[APP] *تطبيق أندرويد جاهز للتثبيت والمعاينة:*\n"
                                        f"• الملف: `{apk.name}` ({size_mb} MB)\n"
                                        f"• المعمارية: `arm64-v8a`\n"
                                        f"• المشروع: `{local_name}`\n"
                                        f"• [سجل البناء على GitHub]({html_url})"
                                    )
                                    sent = send_telegram_apk(AUTHORIZED_USER_ID, apk, caption)
                                    if not sent:
                                        send_telegram(
                                            AUTHORIZED_USER_ID,
                                            f"[WARN] ملف `{apk.name}` جاهز ({size_mb} MB)، يمكنك تحميله من [رابط الـ Artifact على GitHub]({html_url}).",
                                        )

                    time.sleep(0.3)

            if is_initial:
                is_initial = False
                save_actions_state(state)

        except Exception as e:
            log(f"Error in actions monitor: {e}")

        time.sleep(sleep_duration)


def handle_interval(chat_id: int, arg: str = "") -> None:
    cfg = load_config()
    current_interval = cfg.get("actions_interval", 30)

    if not arg:
        status_desc = "[FAST] الوضع اللحظي (فحص فوري مستمر)" if current_interval == 0 else f"{current_interval} ثانية"
        msg = (
            f"⏱ *التحكم في الفاصل الزمني لمراقبة GitHub Actions:*\n\n"
            f"• الفاصل الحالي: *{status_desc}*\n\n"
            f"لتغيير الفاصل الزمني، أرسل:\n"
            f"• `/interval 0` — تفعيل الوضع اللحظي (فحص مستمر وتنبيه فوري)\n"
            f"• `/interval 15` — للتحقق كل 15 ثانية\n"
            f"• `/interval 60` — للتحقق كل دقيقة\n\n"
            f"_ملاحظة: يمكنك أيضاً كتابة `/actions 0` أو `/interval 0`._"
        )
        send_telegram(chat_id, msg)
        return

    try:
        val = int(arg.strip())
        if val < 0:
            send_telegram(chat_id, "[WARN] يجب إدخال قيمة موجبة أو 0 للوضع اللحظي.")
            return
        cfg["actions_interval"] = val
        save_config(cfg)
        if val == 0:
            send_telegram(
                chat_id,
                "[FAST] *تم تفعيل الوضع اللحظي لمراقبة GitHub Actions بنجاح.*\n"
                "سيقوم النظام بمراقبة العمليات بشكل فوري وتنبيهك بأي تحديث لحظة وقوعه مباشرة."
            )
        else:
            send_telegram(
                chat_id,
                f"⏱ *تم ضبط الفاصل الزمني للتحقق من GitHub Actions إلى:* `{val}` ثانية."
            )
    except ValueError:
        send_telegram(chat_id, "[WARN] يرجى كتابة عدد الثواني بالأرقام، مثال:\n`/interval 0` أو `/interval 30`")


def handle_help(chat_id: int) -> None:
    msg = (
        "[SYSTEM] *دليل أوامر التحكم والمزامنة وإدارة العمليات:*\n\n"
        "• `/server` - تشغيل السيرفرات المحلية للمشاريع بالكامل مع كشف البورتات والروابط.\n"
        "  _أمثلة:_ `/server <project>` أو `/server all`\n\n"
        "• `/servers` - عرض كافة السيرفرات النشطة حالياً على مستوى جهاز الماك/النظام.\n\n"
        "• `/restart` - إعادة تشغيل سريعة لأي مشروع أو لكافة السيرفرات.\n"
        "  _أمثلة:_ `/restart <project>` أو `/restart all`\n\n"
        "• `/stop` - إيقاف سيرفر محدد أو كافة السيرفرات وتحرير البورتات فوراً.\n"
        "  _أمثلة:_ `/stop <project>` أو `/stop all`\n\n"
        "• `/health` - فحص صحة وموارد جهاز الماك (RAM, CPU, Disks, Uptime).\n\n"
        "• `/diagnose` - فحص سجلات الأخطاء وتقديم تقرير المعالجة الفني.\n\n"
        "• `/build` - بناء التطبيقات عبر GitHub Actions واستلام الملفات مباشرة.\n"
        "  _أمثلة:_ `/build <target>` أو اختيار الهدف من القائمة\n\n"
        "• `/interval` - التحكم بالفاصل الزمني لمراقبة GitHub Actions (0 للحظي).\n\n"
        "• `/report` - إعداد التقرير الإداري اليومي للمدير (المحسوب من 3:00 فجراً).\n\n"
        "• `/push` - مزامنة ورفع التعديلات إلى GitHub.\n"
        "• `/commit` - حفظ التعديلات محلياً فقط دون رفع.\n"
        "• `/status` - فحص حالة الفروع والتعديلات المعلقة.\n"
        "• `/clear` - تصفير سياق الحوار وبدء جلسة جديدة.\n\n"
        "_يمكنك أيضاً الاستعلام مباشرة عن تفاصيل أي ملف أو مسار أو عملية داخل النظام._"
    )
    send_telegram(chat_id, msg)





def handle_clear(chat_id: int) -> None:
    memory.clear()
    send_telegram(chat_id, "[OK] تم تصفير الذاكرة وبدء جلسة جديدة.", reply_markup=KEYBOARD)


def gather_work_context(user_query: str, history: list[dict]) -> str:
    all_repos = discover_repos()
    context_blocks = []
    combined_text = user_query + " " + " ".join(m.get("content", "") for m in history[-4:])
    target_repos = match_repos(combined_text, all_repos)

    # 1. Project list
    status_lines = []
    for r in all_repos:
        b = subprocess.run(["git", "-C", str(r), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
        st = subprocess.run(["git", "-C", str(r), "status", "--short"], capture_output=True, text=True, check=False).stdout.strip()
        status_lines.append(f"• المشروع: {r.name} | الفرع: {b} | الحالة: {'توجد تعديلات (' + str(len(st.splitlines())) + ' ملف)' if st else 'متزامن'}")
    context_blocks.append("المشاريع الحالية:\n" + "\n".join(status_lines))

    # 2. Commit History & Work Metrics
    history_keywords = [
        "كام يوم", "كم يوم", "قد ايه", "قد إيه", "مدة", "تاريخ", "تاريخ العمل",
        "ساعات", "ساعة", "إحصائيات", "احصائيات", "شغال عليه", "اشتغلت عليه",
        "ايام العمل", "أيام العمل", "اخد وقت", "أخد وقت", "استهلك", "إتمامه", "commit history"
    ]
    if any(kw in combined_text.lower() for kw in history_keywords):
        metrics_blocks = []
        for r in target_repos:
            metrics = get_repo_work_metrics(r)
            if "error" not in metrics:
                metrics_blocks.append(
                    f"مشروع [{metrics['name']}]:\n"
                    f"- إجمالي الـ Commits: {metrics['total_commits']}\n"
                    f"- أيام العمل الفعلية المسجلة: {metrics['distinct_work_days_count']} يوم عمل\n"
                    f"- أول Commit: {metrics['first_commit']}\n"
                    f"- آخر Commit: {metrics['latest_commit']}\n"
                    f"- المدى الزمني الكلي: {metrics['calendar_span_days']} يوماً\n"
                    f"- ساعات العمل التقديرية الفعالة: ~{metrics['estimated_work_hours']} ساعة\n"
                    f"- قائمة التحديثات:\n" + "\n".join(metrics["commits_sample"])
                )
        if metrics_blocks:
            context_blocks.append("إحصائيات تاريخ العمل:\n" + "\n\n".join(metrics_blocks))

    # 3. File search
    file_keywords = ["فين", "مكان", "ملف", "فايل", "مسار", "file", "path", "وين"]
    if any(kw in user_query for kw in file_keywords):
        words = [w.strip("؟.,!?\"'") for w in user_query.split() if len(w) >= 3 and w not in {"يسطا", "يا اسطا", "يا باشا", "يا كبير", "هو", "فين", "مكان", "ملف", "فايل", "موجود", "مسار", "عايز", "اعرف", "وين"}]
        found_files = []
        for term in words[:3]:
            res = subprocess.run(
                ["find", str(BASE_DIR), "-maxdepth", "5", "-iname", f"*{term}*", "-not", "-path", "*/.*", "-not", "-path", "*/node_modules/*", "-not", "-path", "*/vendor/*"],
                capture_output=True, text=True, check=False, timeout=6
            ).stdout.strip()
            if res:
                found_files.extend(res.splitlines()[:10])
        if found_files:
            context_blocks.append("مسارات الملفات:\n" + "\n".join(sorted(set(found_files))[:15]))

    # 4. Commits in time range
    time_keywords = ["commit", "تعديلات", "تحديثات", "أسبوع", "اسبوع", "النهاردة", "اليوم", "أيام", "ايام", "امبارح", "أمس", "آخر", "اخر", "شهر"]
    if any(kw in user_query for kw in time_keywords) and not any(kw in combined_text.lower() for kw in ["كام يوم", "قد ايه", "اخد وقت"]):
        since_arg = "7 days ago"
        if "امبارح" in user_query or "أمس" in user_query:
            since_arg = "2 days ago"
        elif "النهاردة" in user_query or "اليوم" in user_query:
            since_arg = "1 day ago"
        elif "شهر" in user_query:
            since_arg = "30 days ago"

        commits_info = []
        for r in target_repos:
            cmd = ["git", "-C", str(r), "log", f"--since={since_arg}", "--oneline", "-n", "10"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()
            if res:
                commits_info.append(f"تحديثات {r.name} ({since_arg}):\n{res}")
        if commits_info:
            context_blocks.append("سجل التحديثات الحديثة:\n" + "\n\n".join(commits_info))

    # 5. Current modified files
    dirty_details = []
    for r in target_repos:
        st = subprocess.run(["git", "-C", str(r), "status", "--short"], capture_output=True, text=True, check=False).stdout.strip()
        if st:
            dirty_details.append(f"تعديلات غير محفوظة في {r.name}:\n{st}")
    if dirty_details:
        context_blocks.append("تعديلات محلية غير محفوظة:\n" + "\n\n".join(dirty_details))

    return "\n\n====================\n\n".join(context_blocks)


def handle_smart_chat(chat_id: int, user_text: str) -> None:
    history = memory.load()
    context = gather_work_context(user_text, history)

    system_prompt = (
        "أنت محرك تحليل واستعلامات تشغيلي لنظام WAISoft-Reports.\n"
        "قواعد صارمة جداً لأسلوب الرد:\n"
        "1. كن رسمياً، مهنياً، ومقتضباً إلى أقصى حد ممكن (بدون إخلال بالمعلومة المطلوبة).\n"
        "2. ادخل في صلب الإجابة مباشرة بنقاط محددة، أرقام دقيقة، ومسارات صريحة.\n"
        "3. ممنوع تماماً أي عبارات ترحيبية، مجاملات، مقدمات، أو مشاعر ودية (المستخدم أكد صراحة: 'هو مش هيصاحبني').\n"
        "4. اعتمد فقط على بيانات المشاريع الحقيقية المرفقة في السياق بدقة تامة.\n"
        "5. راعِ سياق الرسائل السابقة المرفقة في التاريخ للإجابة دون طلب توضيح."
    )

    prompt = (
        f"سؤال المستخدم:\n{user_text}\n\n"
        f"بيانات المشاريع من النظام:\n{context}"
    )

    reply = call_groq_ai(prompt, system_prompt=system_prompt, conversation_history=history)
    if not reply:
        reply = "تعذر استخراج الرد حالياً بسبب ضغط الشبكة."

    memory.save_turn(user_text, reply)
    send_telegram(chat_id, reply)


def process_callback_query(query: dict) -> None:
    query_id = query.get("id", "")
    from_id = query.get("from", {}).get("id")
    message = query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    menu_msg_id = message.get("message_id")
    data = query.get("data", "")

    if not is_authorized(from_id):
        answer_callback_query(query_id, "[ACCESS DENIED] غير مصرح.")
        return

    answer_callback_query(query_id)
    log(f"Processing callback query: {data}")

    if ":" in data:
        action, target = data.split(":", 1)
        if action == "report":
            handle_manager_report(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "build":
            handle_build(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "server":
            handle_server(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "restart":
            handle_restart(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "stop":
            handle_stop_server(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "push":
            handle_push(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "commit":
            handle_commit(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "status":
            handle_status(chat_id, target, menu_msg_id=menu_msg_id)
        elif action == "aidiag":
            handle_ai_diagnose(chat_id, target)


def process_message(msg: dict) -> None:
    from_id = msg.get("from", {}).get("id")
    chat_id = msg.get("chat", {}).get("id")
    raw_text = (msg.get("text") or "").strip()
    text = raw_text.lower()

    if not is_authorized(from_id):
        log(f"Unauthorized message from user {from_id}: {raw_text}")
        if chat_id:
            send_telegram(chat_id, "[ACCESS DENIED] غير مصرح.")
        return

    log(f"Processing message: {raw_text}")

    cmd, arg = parse_command(raw_text)
    if cmd:
        if cmd == "report":
            handle_manager_report(chat_id, arg)
            return
        elif cmd == "build":
            handle_build(chat_id, arg)
            return
        elif cmd in {"server", "run"}:
            handle_server(chat_id, arg)
            return
        elif cmd in {"servers", "running"}:
            handle_servers_status(chat_id)
            return
        elif cmd == "restart":
            handle_restart(chat_id, arg)
            return
        elif cmd in {"health", "mac"}:
            handle_health(chat_id)
            return
        elif cmd in {"diagnose", "diag"}:
            handle_ai_diagnose(chat_id, arg)
            return
        elif cmd == "stop":
            handle_stop_server(chat_id, arg)
            return
        elif cmd == "interval":
            handle_interval(chat_id, arg)
            return
        elif cmd in {"push", "sync"}:
            handle_push(chat_id, arg)
            return
        elif cmd == "commit":
            handle_commit(chat_id, arg)
            return
        elif cmd == "status":
            handle_status(chat_id, arg)
            return
        elif cmd in {"clear", "reset"}:
            handle_clear(chat_id)
            return
        elif cmd in {"help", "start"}:
            handle_help(chat_id)
            return

    # Handle legacy button texts or quick keywords
    if text in {"clear", "امسح", "تصفير", "جلسة جديدة", "جديد"}:
        handle_clear(chat_id)
    elif text in {"help", "مساعدة"}:
        handle_help(chat_id)
    elif text in {"server", "سيرفر", "سيرفرات", "تشغيل", "run"}:
        handle_server(chat_id)
    elif text in {"servers", "السيرفرات", "السيرفرات النشطة"}:
        handle_servers_status(chat_id)
    elif text in {"restart", "اعادة تشغيل", "إعادة تشغيل", "ريستارت"}:
        handle_restart(chat_id)
    elif text in {"health", "mac", "صحة", "الموارد", "الماك", "صحة الماك"}:
        handle_health(chat_id)
    elif text in {"diagnose", "diag", "تشخيص", "فحص الخطأ", "تحليل الخطأ"}:
        handle_ai_diagnose(chat_id)
    elif text in {"stop", "ايقاف", "إيقاف", "وقف"}:
        handle_stop_server(chat_id)
    elif text in {"build", "بناء", "apk"}:
        handle_build(chat_id)
    elif text in {"تقرير", "تقرير المدير", "تقرير الإنجاز"}:
        handle_manager_report(chat_id)
    elif text in {"حالة", "المشاريع", "المستودعات"}:
        handle_status(chat_id)
    elif text in {"حفظ", "حفظ (commit)"}:
        handle_commit(chat_id)
    elif text in {"رفع", "مزامنة", "commit+push", "رفع (push)"}:
        handle_push(chat_id)
    else:
        handle_smart_chat(chat_id, raw_text)


def process_inline_query(iq: dict) -> None:
    query_id = iq.get("id")
    raw_query = (iq.get("query") or "").strip().lower()
    from_id = iq.get("from", {}).get("id")

    if not is_authorized(from_id):
        return

    results = []

    # Build suggestions
    if not raw_query or "build" in raw_query or "بناء" in raw_query:
        sub = raw_query.replace("build", "").replace("بناء", "").strip()
        for k, v in BUILD_TARGETS.items():
            title_text = v.get("title", k)
            if not sub or sub in k.lower() or sub in title_text.lower():
                results.append({
                    "type": "article",
                    "id": f"build_{k}",
                    "title": f"[بناء] {title_text}",
                    "description": f"تشغيل workflow وبناء {v.get('repo', k)}",
                    "input_message_content": {"message_text": f"/build {k}"},
                })

    # Report suggestions
    if not raw_query or "report" in raw_query or "تقرير" in raw_query:
        sub = raw_query.replace("report", "").replace("تقرير", "").strip()
        all_rep = {
            "type": "article",
            "id": "report_all",
            "title": "[تقرير] التقرير الشامل لجميع المشاريع",
            "description": "تقرير إداري تنفيذي ليوم العمل منذ 3:00 ص",
            "input_message_content": {"message_text": "/report all"},
        }
        if not sub or "all" in sub or "شامل" in sub:
            results.append(all_rep)
        for r in discover_repos():
            if not sub or sub in r.name.lower():
                results.append({
                    "type": "article",
                    "id": f"report_{r.name}",
                    "title": f"[مشروع] تقرير مشروع {r.name}",
                    "description": f"حصر الأعمال في {r.name} منذ 3:00 ص",
                    "input_message_content": {"message_text": f"/report {r.name}"},
                })

    # Server suggestions
    if not raw_query or "server" in raw_query or "سيرفر" in raw_query:
        sub = raw_query.replace("server", "").replace("سيرفر", "").strip()
        if not sub or "all" in sub or "كل" in sub:
            results.append({
                "type": "article",
                "id": "server_all",
                "title": "[تشغيل] تشغيل كافة المشاريع (All Projects)",
                "description": "تشغيل كافة السيرفرات المحلية لكافة المشاريع دفعة واحدة",
                "input_message_content": {"message_text": "/server all"},
            })
        seen_srv = set()
        for skey, scfg in SERVER_CONFIGS.items():
            proj = scfg.get("project", skey)
            if proj not in seen_srv:
                seen_srv.add(proj)
                title = scfg.get("title", proj)
                if not sub or sub in proj.lower() or sub in title.lower():
                    results.append({
                        "type": "article",
                        "id": f"server_{proj}",
                        "title": f"[تشغيل] تشغيل {title}",
                        "description": f"تشغيل خوادم مشروع {proj}",
                        "input_message_content": {"message_text": f"/server {proj}"},
                    })

    # Stop suggestions
    if not raw_query or "stop" in raw_query or "ايقاف" in raw_query or "وقف" in raw_query:
        sub = raw_query.replace("stop", "").replace("ايقاف", "").replace("وقف", "").strip()
        if not sub or "all" in sub or "كل" in sub:
            results.append({
                "type": "article",
                "id": "stop_all",
                "title": "[إيقاف الكل] إيقاف كافة السيرفرات (Stop All)",
                "description": "إيقاف جميع السيرفرات النشطة دفعة واحدة وتحرير بورتاتها",
                "input_message_content": {"message_text": "/stop all"},
            })
        seen_stop = set()
        for skey, scfg in SERVER_CONFIGS.items():
            proj = scfg.get("project", skey)
            if proj not in seen_stop:
                seen_stop.add(proj)
                title = scfg.get("title", proj)
                if not sub or sub in proj.lower() or sub in title.lower():
                    results.append({
                        "type": "article",
                        "id": f"stop_{proj}",
                        "title": f"[إيقاف] إيقاف سيرفرات {title}",
                        "description": f"إيقاف خوادم مشروع {proj}",
                        "input_message_content": {"message_text": f"/stop {proj}"},
                    })

    # Restart suggestions
    if not raw_query or "restart" in raw_query or "اعادة" in raw_query or "ريستارت" in raw_query:
        sub = raw_query.replace("restart", "").replace("اعادة", "").replace("ريستارت", "").strip()
        if not sub or "all" in sub or "كل" in sub:
            results.append({
                "type": "article",
                "id": "restart_all",
                "title": "[إعادة تشغيل] إعادة تشغيل كافة المشاريع (Restart All)",
                "description": "إيقاف وتحرير البورتات ثم إعادة تشغيل جميع السيرفرات النشطة",
                "input_message_content": {"message_text": "/restart all"},
            })
        seen_rst = set()
        for skey, scfg in SERVER_CONFIGS.items():
            proj = scfg.get("project", skey)
            if proj not in seen_rst:
                seen_rst.add(proj)
                title = scfg.get("title", proj)
                if not sub or sub in proj.lower() or sub in title.lower():
                    results.append({
                        "type": "article",
                        "id": f"restart_{proj}",
                        "title": f"[إعادة تشغيل] إعادة تشغيل سيرفرات {title}",
                        "description": f"إعادة تشغيل خوادم مشروع {proj}",
                        "input_message_content": {"message_text": f"/restart {proj}"},
                    })

    # Health suggestions
    if not raw_query or "health" in raw_query or "mac" in raw_query or "صحة" in raw_query or "موارد" in raw_query:
        results.append({
            "type": "article",
            "id": "health_mac",
            "title": "[صحة النظام] صحة وموارد النظام (System Health)",
            "description": "فحص RAM، المعالج CPU، الأقراص، Uptime، والسيرفرات النشطة",
            "input_message_content": {"message_text": "/health"},
        })

    # AI Diagnose suggestions
    if not raw_query or "diag" in raw_query or "تشخيص" in raw_query or "خطأ" in raw_query:
        results.append({
            "type": "article",
            "id": "diagnose_error",
            "title": "[DIAG] تشخيص الخطأ الأخير في النظام",
            "description": "تحليل أسباب انهيار السيرفر أو فشل البناء واقتراح حل جذري",
            "input_message_content": {"message_text": "/diagnose"},
        })

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerInlineQuery"
    try:
        requests.post(url, json={"inline_query_id": query_id, "results": results[:20], "cache_time": 1}, timeout=10)
    except Exception as e:
        log(f"Error answering inline query: {e}")


def poll_loop() -> None:
    log("Setting Telegram bot command menu...")
    set_bot_commands()
    log("Starting background Actions monitor worker...")
    threading.Thread(target=actions_monitor_worker, daemon=True).start()
    log("Starting background Server monitor worker...")
    threading.Thread(target=server_monitor_worker, daemon=True).start()
    log("Starting background Git remote ahead monitor worker...")
    threading.Thread(target=git_remote_monitor_worker, daemon=True).start()
    log("Starting background Git local activity monitor worker...")
    threading.Thread(target=git_local_activity_monitor_worker, daemon=True).start()
    log("Telegram bot polling daemon started.")
    offset = 0
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"

    while True:
        try:
            params = {"timeout": 30, "offset": offset}
            resp = requests.get(url, params=params, timeout=35)
            if resp.status_code == 200:
                data = resp.json()
                for update in data.get("result", []):
                    update_id = update.get("update_id", 0)
                    offset = max(offset, update_id + 1)
                    if "message" in update:
                        threading.Thread(target=process_message, args=(update["message"],), daemon=True).start()
                    elif "callback_query" in update:
                        threading.Thread(target=process_callback_query, args=(update["callback_query"],), daemon=True).start()
                    elif "inline_query" in update:
                        threading.Thread(target=process_inline_query, args=(update["inline_query"],), daemon=True).start()
            else:
                log(f"Polling HTTP {resp.status_code}: {resp.text}")
                time.sleep(3)
        except requests.exceptions.RequestException as e:
            time.sleep(4)
        except Exception as e:
            log(f"Unexpected error in poll loop: {e}")
            time.sleep(3)


if __name__ == "__main__":
    poll_loop()

