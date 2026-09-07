from __future__ import annotations

import requests


class TelegramNotifier:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        groq_api_key: str | None = None,
        groq_model: str = "qwen/qwen3.8-27b",
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.groq_api_key = groq_api_key
        self.groq_model = groq_model or "qwen/qwen3.8-27b"

    def generate_ai_report(self, sync_summary: str) -> str:
        if not self.groq_api_key:
            return sync_summary

        prompt = (
            "أنت محرك تقارير إدارية وتشغيلية لمستودعات HorusOps. المطلوب إعداد تقرير إنجاز تنفيذي منظم ومباشر.\n"
            "قواعد صارمة:\n"
            "1. ممنوع تماماً استخدام أي إيموجيز في كامل النص.\n"
            "2. لا تستخدم مصطلحات برمجية جافة أو معقدة (تجنب تماماً كلمات مثل git, commit, branch, diff, hash, staged).\n"
            "3. اكتب بأسلوب إداري موجز وواضح يركز على ما تم إنجازه فعلياً.\n"
            "4. نسق التقرير:\n"
            "   --- تقرير إنجاز الأعمال ---\n"
            "   • ملخص تنفيذي موجز لسير العمل.\n"
            "   • تفاصيل الإنجازات لكل مشروع.\n"
            "   • الحالة والجاهزية.\n\n"
            f"بيانات المزامنة:\n{sync_summary}"
        )

        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }
        models_to_try = [self.groq_model, "openai/gpt-oss-120b"]

        for model in models_to_try:
            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": 0.6,
                    },
                    headers=headers,
                    timeout=45,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content and content.strip():
                        return content.strip()
            except Exception as exc:
                print(f"Groq API error ({model}): {exc}", file=sys.stderr)

        return sync_summary

    def send(self, text: str) -> None:
        import sys

        report = self.generate_ai_report(text)
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

        # Telegram message length limit is 4096 characters
        chunks = [report[i : i + 3900] for i in range(0, len(report), 3900)] or [report]
        for chunk in chunks:
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "Markdown",
                    },
                    timeout=30,
                )
                if not resp.ok:
                    # Retry without Markdown if syntax formatting failed
                    resp = requests.post(
                        url,
                        json={
                            "chat_id": self.chat_id,
                            "text": chunk,
                        },
                        timeout=30,
                    )
                if not resp.ok:
                    print(f"Telegram notifier error: {resp.text}", file=sys.stderr)
            except Exception as exc:
                print(f"Failed to send Telegram message: {exc}", file=sys.stderr)
