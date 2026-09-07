# WAISoft-Reports

<p align="center">
  <b>Production-Grade Git Automation, Server Process Orchestration, CI/CD Engine & Telegram AI Control Center</b>
  <br>
  <a href="README.ar.md">[AR] اقرأ هذا الدليل باللغة العربية</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat&logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/Telegram-Bot%20API-24A1DE.svg?style=flat&logo=telegram&logoColor=white" alt="Telegram API" />
  <img src="https://img.shields.io/badge/AI-Groq%20Llama%203.3-F55036.svg?style=flat" alt="Groq AI" />
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat" alt="MIT License" />
  <img src="https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-lightgrey.svg?style=flat" alt="macOS & Linux" />
</p>

---

## Overview

**WAISoft-Reports** is an all-in-one DevOps and engineering companion designed to eliminate repetitive Git tasks, monitor local and remote servers, run build and CI/CD pipelines, and provide an intelligent **Telegram AI Operations Center** directly on your phone.

Whether you manage multiple development repositories, run background Next.js / Node.js / Python servers, or need real-time push alerts and voice query diagnostics via Telegram, WAISoft-Reports handles it securely with **zero external telemetry and 100% local execution**.

---

## Key Features

### 1. Telegram Operations & Automated Diagnostics
- **14 Strict Slash Commands (No Underscores)**: Clean, user-friendly commands such as `/status`, `/diagnose`, `/managerreport`, `/servers`, `/server`, `/restart`, `/build`, `/sync`, `/repos`, and `/help`.
- **Groq AI Integration**: Automated log diagnostics, root-cause analysis, and natural-language voice query understanding.
- **Interactive Inline Keyboards**: One-tap server control, target build triggering, and diagnostic drills.
- **Exact Timestamps**: Every command and status report outputs precise `HH:MM:SS` execution timestamps.
- **Hardware & Disk Monitors**: Real-time tracking of CPU, RAM, and Disk space across macOS and Linux mount points.

### 2. Automated Git Multi-Repo Synchronization
- Scans base directories dynamically for Git repositories.
- Detects uncommitted changes, stages files intelligently, and crafts contextual AI commit messages.
- Pulls with `--rebase` and pushes to remote with detailed execution logs.

### 3. Server Process Orchestrator (`servers.json`)
- Manages local development servers (Next.js, Vite, Flask, FastAPI, Django, Express, etc.).
- Active server tracking (`active_servers.json`) with PID management, port health checks, and automatic restart on crash.
- Dynamic project discovery: Matches aliases, directories, and target port numbers.

### 4. CI/CD Pipeline Engine (`targets.json`)
- Define local or remote build pipelines (e.g., Flutter release, Docker build, PyPI publish, npm test).
- Pre-flight checks, dependency verification, and build artifact logging.
- Instant Telegram notifications on pipeline success or failure with execution logs.

### 5. Zero-Dependency Web Setup Console (`setup_wizard.py`)
- Standalone single-file setup application running on standard Python libraries (`http.server`).
- Modern, responsive Tailwind CSS UI accessible at `http://localhost:8585`.
- **Live Connection Testers**:
  - Test Telegram Bot Token & Chat ID (`getMe` API check)
  - Test Groq API Key (`models.list` API check)
  - Test GitHub Token (`/user` API check)
- Edit and save `.env`, `servers.json`, and `targets.json` directly from your browser.
- Dedicated configuration onboarding: no process management or control overhead in the web layer.

---

## Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/your-username/WAISoft-Reports.git
cd WAISoft-Reports
```

### 2. Run Setup (One-Step Installer)
Run the installer script (automatically verifies dependencies and launches the setup wizard):
```bash
./install.sh
```
> **For VPS / Headless servers**: Run with `--cli` for an interactive terminal onboarding:
> ```bash
> ./install.sh --cli
> ```

Open **`http://localhost:8585`** in your browser:
1. **Telegram Bot Token**: Paste your token from `@BotFather` and click **Auto-Detect ID** (or send any message to your bot on Telegram).
2. **Projects Directory**: Click **Browse Folder...** to choose your repository root (supports multiple paths separated by comma).
3. **Local Servers**: Click **Auto-Discover Servers** to automatically detect your local development servers (Next.js, Vite, FastAPI, etc.).
4. (Optional) Enter your **Groq API Key** for AI diagnostic reports and **GitHub Token** for CI/CD.
5. Click **Save Settings**.

---

## Service Execution & Management

### Direct Execution
Install dependencies:
```bash
pip install -r requirements.txt
```

Run the Telegram Bot Daemon:
```bash
./scripts/run_bot.sh
```

Run Git Auto Sync manually:
```bash
./scripts/run_sync.sh
```

---

### Running as a Background Daemon (Auto-Start on Boot)

#### On macOS (`launchd`)
We provide a ready-to-use launchd service:
```bash
./scripts/install_launchd.sh
```
- Logs: `server_logs/bot_stdout.log` and `server_logs/bot_stderr.log`
- Stop service: `launchctl stop com.waisoft.bot`
- Unload service: `launchctl unload ~/Library/LaunchAgents/com.waisoft.bot.plist`

#### On Linux (`systemd`)
For systemd-based Linux systems:
```bash
./scripts/install_systemd.sh
```
- Status: `systemctl --user status waisoft-bot.service`
- Restart: `systemctl --user restart waisoft-bot.service`
- Logs: `journalctl --user -u waisoft-bot.service -f`

---

## Telegram Bot Commands Reference

All commands follow strict syntax guidelines **without underscores**:

| Command | Description | Example |
| :--- | :--- | :--- |
| `/status` | Full system health report (CPU, RAM, Disks, Active Servers) | `/status` |
| `/managerreport` | Comprehensive manager summary (Git activity, servers, alerts) | `/managerreport` |
| `/diagnose [target]` | AI-powered diagnosis of server logs or system errors | `/diagnose backend` |
| `/servers` | List all configured servers with live status & interactive keyboard | `/servers` |
| `/server <name>` | Detailed status and port check for a specific server | `/server api` |
| `/startserver <name>` | Start a configured server process | `/startserver web` |
| `/stopserver <name>` | Stop a running server process | `/stopserver web` |
| `/restart <name>` | Restart a running or failed server process | `/restart web` |
| `/build <target>` | Trigger a configured CI/CD build target | `/build mobile` |
| `/sync` | Trigger an immediate Git auto-sync across all repositories | `/sync` |
| `/repos` | List all discovered repositories and their current Git status | `/repos` |
| `/report` | Quick daily report of Git commits and server uptime | `/report` |
| `/clearcache` | Clear temporary diagnostic caches and error logs | `/clearcache` |
| `/help` | Display interactive command manual and usage tips | `/help` |

> **Voice Commands**: Send a voice note to the bot! If Groq AI is enabled, the bot will transcribe the voice query and execute the corresponding command or answer intelligently.

---

## Configuration Details

### 1. Environment Variables (`.env`)
Copy `.env.example` to `.env` or use the Web Wizard:

```ini
# Telegram Configuration
TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ"
TELEGRAM_CHAT_ID="123456789"
TELEGRAM_ADMIN_IDS="123456789"

# AI Integration (Groq)
GROQ_API_KEY="gsk_xxxxxxxxxxxxxxxxxxxxxx"
GROQ_MODEL="llama-3.3-70b-versatile"

# GitHub Integration
GITHUB_TOKEN="ghp_xxxxxxxxxxxxxxxxxxxxxx"

# Git Auto Sync Settings
SYNC_BASE_DIR="/path/to/your/projects"
SYNC_IGNORED_NAMES=".git,node_modules,dist,build,.venv"
SYNC_PUSH="true"
SYNC_INTERVAL_MINUTES="30"
```

### 2. Server Configuration (`servers.json`)
Define servers that the bot can monitor and orchestrate:
```json
{
  "web": {
    "name": "Frontend Web App",
    "cwd": "/path/to/projects/frontend",
    "cmd": "npm run dev",
    "port": 3000,
    "aliases": ["web", "frontend", "ui"]
  },
  "api": {
    "name": "Backend API",
    "cwd": "/path/to/projects/backend",
    "cmd": "python3 main.py",
    "port": 8000,
    "aliases": ["api", "backend", "server"]
  }
}
```

### 3. CI/CD Build Targets (`targets.json`)
Define automated build targets:
```json
{
  "mobile-release": {
    "name": "Flutter Android Release",
    "cwd": "/path/to/projects/mobile",
    "cmd": "flutter build apk --release",
    "notify_on": "always"
  },
  "docker-deploy": {
    "name": "Docker Production Deploy",
    "cwd": "/path/to/projects/api",
    "cmd": "docker-compose up -d --build",
    "notify_on": "failure"
  }
}
```

---

## Security & Privacy Architecture

- **Zero Hardcoded Secrets**: All tokens, keys, and IDs are isolated in `.env`, which is strictly excluded in `.gitignore`.
- **Sandboxed Local Execution**: The bot runs entirely on your local machine or server. No intermediary proxy or telemetry server is contacted.
- **Admin ID Whitelisting**: Set `TELEGRAM_ADMIN_IDS` to restrict bot commands to authorized Telegram users only.
- **Safe Process Spawning**: Subprocess execution validates commands and uses process isolation to prevent shell injection.

---

## Contributing

Contributions, issues, and feature requests are welcome!
Feel free to check [issues page](https://github.com/your-username/WAISoft-Reports/issues).

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
