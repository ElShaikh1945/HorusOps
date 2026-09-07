#!/usr/bin/env python3
"""Scheduled sync runner for git-auto-sync.

- Native scheduled runner (00:00 and 09:00 via launchd)
- Mount & Network connectivity guards
- Safe rebase conflict detection & multi-user guard
- Silent if no changes exist
- Sends formal summary notification to Telegram only on sync or conflict
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

from env_loader import load_env

env = load_env()
BOT_TOKEN = env.get("TELEGRAM_BOT_TOKEN", "")
AUTHORIZED_USER_ID = int(env.get("TELEGRAM_USER_ID", "0"))

from sync_engine import (
    BASE_DIR,
    LOG_PATH,
    check_environment,
    discover_repos,
    execute_full_push,
)
import requests


def log(msg: str) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] [ScheduledSync] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify_telegram(text: str) -> None:
    if not BOT_TOKEN or not AUTHORIZED_USER_ID:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": AUTHORIZED_USER_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, json=payload, timeout=25)
    except Exception as exc:
        log(f"Failed to notify telegram: {exc}")


def main() -> int:
    now = datetime.datetime.now()
    is_midnight = (now.hour == 0 or now.hour == 23 or "--midnight" in sys.argv)
    log(f"Running scheduled sync check (is_midnight={is_midnight}, hour={now.hour})...")

    ok, err_msg = check_environment()
    if not ok:
        log(f"Environment check failed: {err_msg}")
        if is_midnight:
            notify_telegram(f"⚠️ *تنبيه الفحص الليلي (12:00 منتصف الليل):*\nتعذر فحص المشاريع: {err_msg}")
        return 1

    result = execute_full_push()
    if not result.get("ok"):
        log(f"Push failed: {result.get('error')}")
        notify_telegram(f"❌ *تنبيه خطأ في المزامنة:*\n{result.get('error')}")
        return 1

    if result.get("has_conflicts"):
        conflict_lines = ["🚨 *تنبيه المزامنة المجدولة: تعارض في الدمج (Merge Conflict)*"]
        for r in result["results"]:
            if r.get("status") == "conflict":
                conflict_lines.append(f"• المشروع: `{r['repo']}`")
                conflict_lines.append(f"  الملفات المتعارضة: {', '.join(r.get('conflicts', []))}")
        conflict_lines.append("\nتم تعليق رفع هذا المشروع لتفادي الكتابة فوق تعديلات الفريق. يرجى حل التعارض يدوياً.")
        notify_telegram("\n".join(conflict_lines))
        return 2

    if result.get("has_errors"):
        error_lines = ["⚠️ *تنبيه: أخطاء أثناء مزامنة بعض المشاريع:*"]
        for r in result["results"]:
            if r.get("status") == "error":
                error_lines.append(f"• المشروع: `{r['repo']}`\n  السبب: {r.get('error')}")
        notify_telegram("\n".join(error_lines))

    pushed_items = [r for r in result["results"] if r.get("status") == "pushed"]
    if not pushed_items:
        log("All repositories are clean and up to date. No push needed.")
        if is_midnight:
            heartbeat_msg = (
                "✅ *تقرير الفحص الليلي (12:00 منتصف الليل)*\n\n"
                f"• تم فحص كافة مستودعات المشاريع في `{BASE_DIR}`.\n"
                "• لا توجد أي تعديلات جديدة معلقة، وجميع المستودعات متزامنة مع GitHub.\n"
                "• حالة الخادم والاتصال: متصل وجاهز للعمل 🟢"
            )
            notify_telegram(heartbeat_msg)
        return 0

    lines = ["✅ *تقرير المزامنة التلقائية المجدولة*\n"]
    for p in pushed_items:
        lines.append(f"📦 *مشروع:* `{p['repo']}`")
        if p.get("commit_url"):
            lines.append(f"• الـ Commit الجديد: [{p['new_commit_hash']}]({p['commit_url']}) - {p['new_commit_msg']}")
        else:
            lines.append(f"• الـ Commit الجديد: `{p['new_commit_hash']}` - {p['new_commit_msg']}")

        if p.get("branch_url"):
            lines.append(f"• الفرع: [{p['branch']}]({p['branch_url']})")
        else:
            lines.append(f"• الفرع: `{p['branch']}`")

        lines.append(f"• الـ Commit السابق: `{p['prev_commit_hash']}` (تاريخ: {p['prev_commit_date']})")
        lines.append("")

    notify_telegram("\n".join(lines).strip())
    log(f"Scheduled sync successfully pushed {len(pushed_items)} repository/ies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


