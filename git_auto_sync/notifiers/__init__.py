from __future__ import annotations

from git_auto_sync.display import display_path
from git_auto_sync.models import RunSummary
from git_auto_sync.notifiers.lark import LarkNotifier
from git_auto_sync.notifiers.log import LogNotifier
from git_auto_sync.notifiers.telegram import TelegramNotifier

_STATUS_LABEL = {
    "skipped": "[SKIP] No changes",
    "committed": "[OK] Committed (not pushed)",
    "committed_pushed": "[OK] Committed and pushed",
    "failed": "[FAIL] Failed",
}


def format_summary(summary: RunSummary) -> str:
    lines = ["git-auto-sync sync result:"]
    for r in summary.results:
        label = _STATUS_LABEL.get(r.status, r.status)
        line = f"{label}  {display_path(r.path)}"
        if r.message:
            line += f"\n  {r.message.splitlines()[0]}"
        if r.error:
            line += f"\n  Reason: {r.error}"
        if r.ignored_paths:
            line += f"\n  Ignored: {', '.join(r.ignored_paths)}"
        if r.blocked_paths:
            line += f"\n  Blocked: {', '.join(r.blocked_paths)}"
        lines.append(line)
    return "\n".join(lines)


def build_notifiers(config: dict) -> list:
    notifiers = []
    for name, conf in config.items():
        if not conf.get("enabled"):
            continue
        if name == "log":
            notifiers.append(LogNotifier(conf["path"]))
        elif name == "telegram":
            notifiers.append(
                TelegramNotifier(
                    conf["bot_token"],
                    conf["chat_id"],
                    groq_api_key=conf.get("groq_api_key"),
                    groq_model=conf.get("groq_model", "qwen/qwen3.8-27b"),
                )
            )
        elif name == "lark":
            notifiers.append(LarkNotifier(conf["webhook"]))
    return notifiers
