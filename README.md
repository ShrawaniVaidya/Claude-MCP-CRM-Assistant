# Claude MCP CRM Assistant

An AI-powered lead management system for sales teams. It combines an **MCP server** (so Claude can query and manage your leads in plain English), a **web dashboard**, and a **call-transcription pipeline** that analyses sales calls for customer objections and churn risk.

Everything runs on a single local MySQL database (`leads_db`).

---

## Features

- **Chat with your CRM through Claude.** The MCP server exposes about 30 tools for looking up leads, agents, call history, stats and more.
- **Web dashboard.** A FastAPI backend and single-page UI with stats, lead lists, agent performance, notes, callbacks and targets.
- **Lead management.** Notes, follow-up callbacks, lead status, tags and per-agent targets.
- **Priority and stale lead detection.** Find leads that need attention first.
- **Call transcription.** Turn call recordings into text with Groq Whisper (cloud) or faster-whisper (local).
- **Objection analysis.** Gemini analyses transcripts to find why leads hesitate and how each agent handles objections.
- **Churn-risk scoring.** Surface high-risk leads with per-lead detail.
- **Email reports.** Send a daily summary or a custom email through Gmail.

---

## Project structure

| File | Purpose |
|---|---|
| `mcp_server.py` | MCP server with all the CRM tools Claude can call |
| `app.py` | FastAPI backend and API for the dashboard |
| `index.html` | Dashboard frontend |
| `setup_db.py` | One-time setup: creates `leads_db` and loads your calls CSV |
| `setup_recordings.py` | Creates the `call_recordings` table from your recordings CSV |
| `transcribe_only.py` | Step 1: transcribe recordings with Groq Whisper into `transcripts.csv` |
| `upload_to_db.py` | Step 2: upload reviewed transcripts from `transcripts.csv` to MySQL |
| `transcribe.py` | Alternative: transcribe locally with faster-whisper and write to MySQL |
| `daily_email.py` | Standalone daily summary email (schedulable) |
| `check_db.py`, `test_*.py` | Small helper and test scripts |
| `.env.example` | Template for your secrets |

---

## Requirements

- Python 3.10 or newer
- MySQL Server, running locally
- A Gemini API key (objection analysis and AI features)
- A Groq API key (only if you use `transcribe_only.py`)
- A Gmail App Password (only if you want email reports)
- [Claude Desktop](https://claude.ai/download) (to use the MCP server)
- ffmpeg (only if you use local transcription with `transcribe.py`)

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ShrawaniVaidya/Claude-MCP-CRM-Assistant.git
cd Claude-MCP-CRM-Assistant

python -m venv venv
# Windows:  venv\Scripts\activate
# Mac/Linux: source venv/bin/activate

pip install -r requirements.txt
pip install pandas
```

> `pandas` is used by `setup_db.py`. For local transcription, also run `pip install faster-whisper`.

### 2. Configure your secrets

Copy the template and fill in your own values:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

| Variable | Description |
|---|---|
| `DB_PASSWORD` | Your MySQL root password |
| `EMAIL_SENDER` | Gmail address that sends reports |
| `EMAIL_APP_PASSWORD` | Gmail **App Password** (not your normal password) |
| `EMAIL_RECIPIENT` | Address that receives reports (can be the same) |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GROQ_API_KEY` | Groq API key |

To create a Gmail App Password: Google Account, then Security, then 2-Step Verification, then App Passwords.

> **Never commit `.env`.** It is already listed in `.gitignore`.

### 3. Create the database

Put your calls CSV in the project folder as `calls_data.csv`, then run:

```bash
python setup_db.py --password YOUR_MYSQL_PASSWORD
```

This creates `leads_db` and the `calls` table. The notes, follow-ups, lead-status and targets tables are created automatically the first time the app or MCP server starts.

### 4. Run the dashboard

```bash
python app.py
```

Open **http://localhost:8000**.

### 5. Connect the MCP server to Claude Desktop

Add this to your Claude Desktop config file (`claude_desktop_config.json`), using the real path to your project:

```json
{
  "mcpServers": {
    "crm-assistant": {
      "command": "python",
      "args": ["C:/full/path/to/Claude-MCP-CRM-Assistant/mcp_server.py"]
    }
  }
}
```

Restart Claude Desktop, then try prompts like:

- "Show me today's stats."
- "Which leads have gone stale?"
- "Schedule a callback for lead 15439472 tomorrow at 11am."
- "Which leads are at high churn risk?"
- "Email me the daily summary."

---

## Call objection analysis pipeline (optional)

1. **Load recordings.** Put `call_recordings.csv` in the project folder, then run `python setup_recordings.py`.
2. **Transcribe.** Choose one option:
   - **Cloud (Groq):** `python transcribe_only.py` saves results to `transcripts.csv`. Review the file, then run `python upload_to_db.py`. Use `--limit 5` to test and `--dry-run` to preview an upload.
   - **Local (faster-whisper):** `python transcribe.py` writes transcripts straight to MySQL. Use `--limit 10` to test or `--lead_id <id>` for one lead.
3. **Analyse.** Ask Claude to use the `analyse_objections`, `get_agent_objection_performance`, `get_lead_objection_history` and churn-risk tools.

> **Privacy:** `transcripts.csv` holds real call transcripts and agent names. Remove or anonymise it before sharing the repo publicly.

---

## Daily email report

Run it manually:

```bash
python daily_email.py
```

To schedule it on Windows, use Task Scheduler with a daily trigger, program `python`, and the full path to `daily_email.py` as the argument. On Mac or Linux, use `cron`.

---

## MCP tools overview

| Area | Tools |
|---|---|
| **Leads and calls** | `get_stats`, `get_lead`, `get_all_calls`, `get_leads`, `search_by_phone`, `find_priority_leads`, `find_stale_leads` |
| **Agents** | `get_agents`, `set_agent_target`, `get_agent_targets` |
| **Notes** | `add_note`, `get_notes`, `delete_note` |
| **Callbacks** | `schedule_callback`, `get_todays_callbacks`, `get_upcoming_callbacks`, `complete_callback` |
| **Status and tags** | `set_lead_status`, `get_lead_status`, `set_lead_tags`, `get_leads_by_tag` |
| **Email** | `send_daily_summary`, `send_custom_email` |
| **Objections** | `analyse_objections`, `get_agent_objection_performance`, `get_lead_objection_history` |
| **Churn risk** | `get_churn_risk_leads`, `get_lead_churn_detail`, `get_churn_summary` |
| **Database** | `run_query`, `db_info` |

---

## Dashboard API

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/health` | Health check |
| GET | `/api/stats` | Overall statistics |
| GET | `/api/leads` | List leads |
| GET | `/api/lead/{lead_id}` | Lead detail |
| GET | `/api/agents` | Agent performance |
| POST / DELETE | `/api/notes`, `/api/notes/{id}` | Add or delete notes |
| POST | `/api/callbacks` | Schedule a follow-up |
| GET / POST | `/api/targets` | Agent targets |
| GET | `/api/high-risk-leads` | High churn-risk leads |
| POST | `/api/ai-analyze` | AI analysis of a lead |

---

## Tech stack

Python, FastAPI, Uvicorn, MySQL, Model Context Protocol (MCP), Google Gemini (`gemini-2.5-flash`), Groq Whisper, faster-whisper, SMTP (Gmail).

---

## Security notes

- Keep all credentials in `.env`. Never hardcode them in source files.
- `run_query` lets Claude run SQL against your database, so only connect this server to data you are comfortable exposing to it.
- If a secret is ever committed by mistake, rotate it immediately. Deleting it from the latest commit does not remove it from Git history.

---

## Author

**Shrawani Vaidya**
GitHub: [@ShrawaniVaidya](https://github.com/ShrawaniVaidya)
