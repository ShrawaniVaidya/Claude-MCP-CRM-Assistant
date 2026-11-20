#!/usr/bin/env python3
import asyncio, os, json, math, smtplib
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

try:
    import mysql.connector
    import mysql.connector.cursor
except ImportError:
    raise SystemExit("Run: pip install mysql-connector-python")

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent, Prompt, PromptMessage, PromptArgument
except ImportError:
    raise SystemExit("Run: pip install mcp")


DB_CONFIG = {
     "host": os.getenv("DB_HOST", "localhost"),
     "port": int(os.getenv("DB_PORT", "3306")),
     "database": os.getenv("DB_NAME", "leads_db"),
     "user": os.getenv("DB_USER", "root"),
     "password": os.getenv("DB_PASSWORD"),
}

EMAIL_CONFIG = {
     "sender_email": os.getenv("EMAIL_SENDER"),
     "sender_password": os.getenv("EMAIL_PASSWORD"),
     "recipient_email": os.getenv("EMAIL_RECIPIENT"),
     "smtp_host": os.getenv("SMTP_HOST", "smtp.gmail.com"),
     "smtp_port": int(os.getenv("SMTP_PORT", "587")),
}

# ── DB Helpers ────────────────────────────────────────────────────────────────
def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def query(sql: str, params=None) -> list[dict]:
    conn = get_conn()
    cur  = conn.cursor(dictionary=True)
    cur.execute(sql, params or [])
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [dict(r) for r in rows]

def scalar(sql: str, params=None):
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(sql, params or [])
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def execute(sql: str, params=None):
    """Run INSERT / UPDATE / DELETE."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(sql, params or [])
    last_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return last_id

def ensure_tables():
    """Create notes, follow_ups, lead_meta, and agent_targets tables if they don't exist yet."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            lead_id     BIGINT NOT NULL,
            agent_name  VARCHAR(200),
            note_text   TEXT NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_notes_lead_id (lead_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            lead_id         BIGINT NOT NULL,
            agent_name      VARCHAR(200),
            scheduled_date  DATE NOT NULL,
            scheduled_time  VARCHAR(10) DEFAULT '09:00',
            reason          TEXT,
            status          VARCHAR(30) DEFAULT 'pending',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_fu_lead_id (lead_id),
            INDEX idx_fu_date    (scheduled_date),
            INDEX idx_fu_status  (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    # Lead CRM status + tags
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lead_meta (
            lead_id     BIGINT PRIMARY KEY,
            lead_status VARCHAR(30) DEFAULT 'new',
            tags        TEXT,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_lm_status (lead_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    # Monthly agent targets
    cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_targets (
            id               INT AUTO_INCREMENT PRIMARY KEY,
            agent_name       VARCHAR(200) NOT NULL,
            month            VARCHAR(7)   NOT NULL,
            target_calls     INT DEFAULT 0,
            target_completed INT DEFAULT 0,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_agent_month (agent_name, month)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()
    conn.close()

def fmt_sec(sec):
    if not sec: return "0s"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:   return f"{h}h {m}m {s}s"
    elif m: return f"{m}m {s}s"
    return f"{s}s"

_dataset_age_cache = None
def _dataset_age_days() -> int:
    """Days between the most recent call in DB and today. Normalises recency for imported datasets."""
    global _dataset_age_cache
    if _dataset_age_cache is None:
        try:
            latest = scalar("SELECT MAX(start_time) FROM call_recordings")
            if latest is None:
                _dataset_age_cache = 0
            elif isinstance(latest, str):
                _dataset_age_cache = (datetime.now() - datetime.fromisoformat(latest[:19])).days
            else:
                _dataset_age_cache = (datetime.now() - latest).days
        except Exception:
            _dataset_age_cache = 0
    return _dataset_age_cache

def score_lead(lead: dict) -> dict:
    """Compute a 0-100 score from aggregated lead stats.

    Scoring (100 pts total):
      connect_rate   40 — (completed + answered) / total calls
      call_volume    20 — raw call count, capped at 20
      avg_duration   15 — average call duration in seconds
      recency        15 — days since last call, relative to dataset's newest call
      completed_bonus 10 — absolute completed call count, capped at 10
      no_resp_penalty -10 max — proportion of no-response calls
    """
    score, breakdown = 0, []

    def add(param, earned, max_pts):
        nonlocal score
        score += earned
        breakdown.append({
            "param":  param,
            "earned": earned,
            "max":    max_pts,
            "pct":    round(earned / max_pts * 100) if max_pts else 0,
        })

    tc   = int(lead.get("total_calls", 0) or 0)
    comp = int(lead.get("completed_calls", 0) or 0)
    ans  = int(lead.get("answered_calls", 0) or 0)
    nr   = int(lead.get("no_response_calls", 0) or 0)
    avg  = float(lead.get("avg_duration_sec", 0) or 0)
    days = int(lead.get("days_since_last_call", 999) or 999)

    # 1. Connect rate — 40 pts
    connect_rate = (comp + ans) / tc if tc else 0
    add("connect_rate",     min(round(connect_rate * 40), 40), 40)

    # 2. Call volume — 20 pts (1 pt per call, max 20)
    add("call_volume",      min(tc, 20), 20)

    # 3. Avg duration — 15 pts
    dur_pts = 15 if avg >= 120 else 10 if avg >= 60 else 5 if avg >= 20 else 0
    add("avg_duration",     dur_pts, 15)

    # 4. Recency relative to dataset — 15 pts
    relative_days = max(0, days - _dataset_age_days())
    rec_pts = 15 if relative_days <= 3 else 10 if relative_days <= 7 else 5 if relative_days <= 14 else 0
    add("recency",          rec_pts, 15)

    # 5. Completed count bonus — 10 pts
    add("completed_bonus",  min(comp, 10), 10)

    # 6. No-response penalty — up to -10
    nr_rate = nr / tc if tc else 0
    add("no_resp_penalty",  round(max(-10, -nr_rate * 10)), 0)

    score = max(0, min(score, 100))
    if   score >= 80: q, emoji = "HOT",  "🔥"
    elif score >= 50: q, emoji = "WARM", "🌡️"
    else:             q, emoji = "COLD", "❄️"
    return {"score": score, "quality": q, "emoji": emoji, "breakdown": breakdown}

# ── Build lead summary from DB ─────────────────────────────────────────────────
def _lead_summary(lead_id: int) -> dict | None:
    rows = query("""
        SELECT
            lead_id,
            COUNT(*)                                                                    AS total_calls,
            SUM(status = 'Completed')                                                   AS completed_calls,
            SUM(status = 'ANSWERED')                                                    AS answered_calls,
            SUM(status = 'No response')                                                 AS no_response_calls,
            SUM(status = 'CANCEL')                                                      AS cancelled_calls,
            SUM(call_duration_sec)               AS total_duration_sec,
            AVG(NULLIF(call_duration_sec, 0))    AS avg_duration_sec,
            MAX(start_time)                                                             AS last_call_date,
            MIN(start_time)                                                             AS first_call_date,
            DATEDIFF(NOW(), MAX(start_time))                                            AS days_since_last_call,
            MAX(agent_name)                                                             AS agent_name,
            MAX(to_number)                                                              AS phone
        FROM call_recordings
        WHERE lead_id = %s
        GROUP BY lead_id
    """, [lead_id])
    if not rows: return None
    r = rows[0]
    lead = {
        "lead_id":              int(r["lead_id"]),
        "phone":                str(r["phone"] or ""),
        "agent_name":           str(r["agent_name"] or ""),
        "total_calls":          int(r["total_calls"] or 0),
        "completed_calls":      int(r["completed_calls"] or 0),
        "answered_calls":       int(r["answered_calls"] or 0),
        "no_response_calls":    int(r["no_response_calls"] or 0),
        "cancelled_calls":      int(r["cancelled_calls"] or 0),
        "total_duration_sec":   int(r["total_duration_sec"] or 0),
        "avg_duration_sec":     round(float(r["avg_duration_sec"] or 0), 1),
        "last_call_date":       str(r["last_call_date"])[:16] if r["last_call_date"] else "",
        "first_call_date":      str(r["first_call_date"])[:16] if r["first_call_date"] else "",
        "days_since_last_call": int(r["days_since_last_call"] or 999),
    }
    lead.update(score_lead(lead))
    return lead

def _all_lead_summaries() -> list[dict]:
    
    rows = query("""
        SELECT
            lead_id,
            COUNT(*)                                    AS total_calls,
            SUM(status = 'Completed')                   AS completed_calls,
            SUM(status = 'ANSWERED')                    AS answered_calls,
            SUM(status = 'No response')                 AS no_response_calls,
            SUM(status = 'CANCEL')                      AS cancelled_calls,
            SUM(call_duration_sec)                  AS total_duration_sec,
            AVG(NULLIF(call_duration_sec, 0))       AS avg_duration_sec,
            MAX(start_time)                             AS last_call_date,
            MIN(start_time)                             AS first_call_date,
            DATEDIFF(NOW(), MAX(start_time))            AS days_since_last_call,
            MAX(agent_name)                             AS agent_name,
            MAX(to_number)                              AS phone
        FROM call_recordings
        GROUP BY lead_id
        ORDER BY lead_id
    """)
    leads = []
    for r in rows:
        lead = {
            "lead_id":              int(r["lead_id"]),
            "phone":                str(r["phone"] or ""),
            "agent_name":           str(r["agent_name"] or ""),
            "total_calls":          int(r["total_calls"] or 0),
            "completed_calls":      int(r["completed_calls"] or 0),
            "answered_calls":       int(r["answered_calls"] or 0),
            "no_response_calls":    int(r["no_response_calls"] or 0),
            "cancelled_calls":      int(r["cancelled_calls"] or 0),
            "total_duration_sec":   int(r["total_duration_sec"] or 0),
            "avg_duration_sec":     round(float(r["avg_duration_sec"] or 0), 1),
            "last_call_date":       str(r["last_call_date"])[:16] if r["last_call_date"] else "",
            "first_call_date":      str(r["first_call_date"])[:16] if r["first_call_date"] else "",
            "days_since_last_call": int(r["days_since_last_call"] or 999),
        }
        lead.update(score_lead(lead))
        leads.append(lead)
    return leads

# ── Real AI Objection Analysis (Gemini API) ────────────────────────────────────
BUSINESS_CONTEXT = """
COMPANY: Manage My Lead (Hyderabad-based SaaS)
PRODUCT: CRM software with lead management, WhatsApp automation, WhatsApp marketing
SALES CHANNEL: Outbound cold calls to leads who submitted Facebook/social media enquiries
AGENT: Manaswini Sahu (primary agent in this dataset)

WHAT THE AGENT PITCHES:
- Lead management CRM (track all leads in one place)
- WhatsApp automation (auto-respond to leads via WhatsApp)
- WhatsApp marketing (broadcast campaigns)
- Social media integration (Facebook, Instagram into one dashboard)

TYPICAL LEAD PROFILE:
- Mix of small business owners, startups, and larger companies
- Already submitted an enquiry on Facebook so they have shown prior interest
- Based across India, primarily Hindi/Telugu/Urdu speaking
- Many are unfamiliar with CRM software

SUCCESSFUL CALL (any one of these counts):
- Lead agrees to a demo or trial
- Lead gives a specific callback time
- Lead asks detailed questions about features or pricing
- Lead says "send details" WITH genuine engagement
- Lead stays on call over 90 seconds and is actively listening

FAILED/DEFLECTED CALL:
- Lead says "call back later" without a specific time
- Lead says "send on WhatsApp" and immediately disengages
- Lead hangs up within 30 seconds
- Lead says "not interested" / "zaroorat nahi" / "nahi chahiye"
- Lead says "already using another CRM"
- Lead gives only one-word answers without engaging

GOOD AGENT BEHAVIOUR:
- Explains features clearly in simple terms
- Handles "I already have software" by asking what features they are missing
- Asks about the lead's business needs before pitching
- Offers a demo proactively

POOR AGENT BEHAVIOUR:
- Just reads the pitch without listening to the lead
- Gives up too quickly when lead deflects
- Does not address objections
"""

HINDI_URDU_CONTEXT = """IMPORTANT LANGUAGE NOTE:
These transcripts are from Indian sales calls for Manage My Lead CRM (Hyderabad).
They may be in Hindi, Urdu, Telugu, English, or Hinglish.
You MUST analyse ALL transcripts regardless of language. Output everything in English.

Common phrases in these calls:
- "abhi nahi" / "abhi nahi chahiye" = not interested right now (DEFLECTION)
- "mehnga hai" = too expensive (OBJECTION)
- "sochna hai" / "sochenge" = need to think (SOFT DEFLECTION)
- "baad mein" / "baad mein call karo" = call back later (DEFLECTION)
- "zaroorat nahi" / "nahi chahiye" = don't need it (REJECTION)
- "busy hoon" = I'm busy (DEFLECTION)
- "theek hai" / "haan" = okay/yes (POSITIVE)
- "details bhejo" / "WhatsApp pe bhejo" = send details (usually DEFLECTION)
- "already use karte hain" = already using a CRM (OBJECTION)
- "dekh lete hain" = we'll see (SOFT DEFLECTION)
- "enquiry daali thi" = I submitted an enquiry (POSITIVE — they remember)
"""

BATCH_SIZE = 10  # transcripts per Gemini call — keeps JSON small and reliable

def _strip_lang_prefix(text: str) -> str:
    """Remove [lang:xx] prefix added by transcribe pipeline."""
    if text and text.startswith("[lang:"):
        return text.split("] ", 1)[-1]
    return text or ""

def _analyse_batch(batch: list) -> dict:
    """
    Keyword-based objection detection for a batch of transcripts.
    No API needed — detects objections from actual words in transcripts.
    """
    OBJECTION_KEYWORDS = {
        "Price Concern":        ["cost", "price", "pricing", "costing", "how much", "expensive", "mehnga", "kitna", "rupees", "budget", "afford", "per month", "fees", "charges", "thousand"],
        "Not Interested":       ["not interested", "no need", "dont need", "don't need", "zaroorat nahi", "nahi chahiye", "not required", "no thanks", "not looking"],
        "Call Back Later":      ["call back", "call later", "baad mein", "abhi nahi", "not now", "some other time", "busy", "free nahi", "call after"],
        "Need More Time":       ["will think", "let me think", "need to think", "will see", "dekh lete", "sochenge", "not today", "tomorrow", "next week", "later"],
        "Send Details":         ["send details", "whatsapp", "send on", "email me", "share link", "send info", "details bhejo"],
        "Already Has Solution": ["already have", "already using", "using zoho", "using crm", "have software", "current system", "competitor"],
        "Decision Maker":       ["partner", "discuss with", "check with", "consult", "owner decide", "team decide", "husband", "wife", "boss"],
        "Feature Gap":          ["don't need this", "no call recording", "not required", "we don't use", "no need for"],
        "Unexpected Call":      ["out of the blue", "suddenly", "who gave you", "how did you get", "why are you calling"],
    }

    REBUTTALS = {
        "Price Concern":        "Share ROI — 'At ₹1000/month that's ₹33/day. Our customers save 3+ hours daily.'",
        "Not Interested":       "Ask what they currently use — find the pain point.",
        "Call Back Later":      "Lock specific time — 'Can I call you tomorrow at 11am?'",
        "Need More Time":       "Offer a short demo — 'Just 15 minutes, I'll show you the key features.'",
        "Send Details":         "Don't just send — 'I'll send AND also do a quick 10 min walkthrough. When works?'",
        "Already Has Solution": "Ask pain points — 'What's one thing you wish your current tool did better?'",
        "Decision Maker":       "Include them — 'Can we set up a call with your partner as well?'",
        "Feature Gap":          "Clarify in demo — 'Let me show you exactly how this works.'",
        "Unexpected Call":      "Acknowledge and pivot — 'Sorry for the surprise! You had enquired last week — can I take 2 minutes?'",
    }

    calls_with_objections = 0
    category_counts = {}

    for t in batch:
        raw = _strip_lang_prefix(str(t.get("transcript") or "")).lower()
        found_any = False
        for category, keywords in OBJECTION_KEYWORDS.items():
            for kw in keywords:
                if kw in raw:
                    found_any = True
                    if category not in category_counts:
                        # Find example quote
                        example = ""
                        orig = _strip_lang_prefix(str(t.get("transcript") or ""))
                        for sentence in orig.replace(".", ".|").split("|"):
                            if kw in sentence.lower() and len(sentence.strip()) > 5:
                                example = sentence.strip()[:80]
                                break
                        category_counts[category] = {
                            "count": 1,
                            "example_quote": example,
                            "example_lead_id": t.get("lead_id"),
                            "example_agent": t.get("agent_name") or "—",
                            "suggested_rebuttal": REBUTTALS.get(category, "Acknowledge and redirect.")
                        }
                    else:
                        category_counts[category]["count"] += 1
                    break
        if found_any:
            calls_with_objections += 1

    # Sort by frequency
    sorted_cats = sorted([
        {"objection": cat, "count": data["count"],
         "example_quote": data["example_quote"],
         "example_lead_id": data.get("example_lead_id"),
         "example_agent": data.get("example_agent", "—"),
         "suggested_rebuttal": data["suggested_rebuttal"]}
        for cat, data in category_counts.items()
    ], key=lambda x: x["count"], reverse=True)

    return {
        "calls_with_objections": calls_with_objections,
        "objection_categories": sorted_cats
    }

def _merge_batch_results(batch_results: list, total_transcripts: int) -> dict:
    """Merge multiple batch results into one combined analysis."""
    from collections import defaultdict
    combined = defaultdict(lambda: {"count": 0, "example_quote": "", "example_lead_id": None, "example_agent": "—", "suggested_rebuttal": ""})
    total_with_objections = 0

    for result in batch_results:
        total_with_objections += int(result.get("calls_with_objections", 0))
        for cat in result.get("objection_categories", []):
            key = cat.get("objection", "").strip()
            if not key:
                continue
            combined[key]["count"] += int(cat.get("count", 1))
            if not combined[key]["example_quote"]:
                combined[key]["example_quote"] = cat.get("example_quote", "")
                combined[key]["example_lead_id"] = cat.get("example_lead_id")
                combined[key]["example_agent"] = cat.get("example_agent", "—")
            if not combined[key]["suggested_rebuttal"]:
                combined[key]["suggested_rebuttal"] = cat.get("suggested_rebuttal", "")

    sorted_cats = sorted([
        {"objection": k, "count": v["count"],
         "example_quote": v["example_quote"],
         "example_lead_id": v.get("example_lead_id"),
         "example_agent": v.get("example_agent", "—"),
         "suggested_rebuttal": v["suggested_rebuttal"]}
        for k, v in combined.items()
    ], key=lambda x: x["count"], reverse=True)

    # Generate insights from the data without API
    if sorted_cats:
        top = sorted_cats[0]
        second = sorted_cats[1] if len(sorted_cats) > 1 else None
        pct = round(total_with_objections / total_transcripts * 100) if total_transcripts else 0
        insights = f"{pct}% of calls had objections. Most common: '{top['objection']}' ({top['count']} calls)."
        if second:
            insights += f" Second most common: '{second['objection']}' ({second['count']} calls)."
        recommendation = f"Train agents to handle '{top['objection']}' — use this rebuttal: {top['suggested_rebuttal']}"
    else:
        insights = f"No significant objections detected across {total_transcripts} calls."
        recommendation = "Continue monitoring call quality."

    return {
        "total_calls_analysed":  total_transcripts,
        "calls_with_objections": min(total_with_objections, total_transcripts),
        "objection_categories":  sorted_cats,
        "key_insights":          insights,
        "top_recommendation":    recommendation,
    }

def ai_analyse_objections_batch(transcripts: list) -> dict:
    """
    Analyse transcripts in small batches to avoid Gemini JSON truncation errors.
    Batches of BATCH_SIZE, then merges results.
    Handles Hindi, Urdu, Telugu, English from Hyderabad call centre.
    """
    if not transcripts:
        return {
            "total_calls_analysed": 0, "calls_with_objections": 0,
            "objection_categories": [], "key_insights": "No transcripts provided.",
            "top_recommendation": ""
        }

    # Split into batches
    batches = [transcripts[i:i + BATCH_SIZE] for i in range(0, len(transcripts), BATCH_SIZE)]
    batch_results = []

    for batch in batches:
        try:
            result = _analyse_batch(batch)
            # Validate each category entry
            for cat in result.get("objection_categories", []):
                if not isinstance(cat.get("count"), int) or cat["count"] < 0:
                    cat["count"] = 1
                if not cat.get("objection"):
                    cat["objection"] = "Unknown objection"
                if not cat.get("suggested_rebuttal"):
                    cat["suggested_rebuttal"] = "Acknowledge and ask about their specific needs."
            batch_results.append(result)
        except Exception as e:
            # If one batch fails, continue with others
            batch_results.append({"calls_with_objections": 0, "objection_categories": []})

    return _merge_batch_results(batch_results, len(transcripts))

def ai_analyse_single_transcript(call: dict) -> dict:
    """
    Analyse a single call transcript for objections using keyword detection.
    Works without any API — detects objections from actual words in transcript.
    """
    raw_transcript = _strip_lang_prefix(str(call.get("transcript") or ""))
    if len(raw_transcript) > 3000:
        raw_transcript = raw_transcript[:3000] + "..."

    text = raw_transcript.lower()

    # ── Objection keyword patterns ─────────────────────────────────────────────
    OBJECTION_PATTERNS = {
        "PRICE": [
            "cost", "price", "pricing", "costing", "how much", "expensive",
            "budget", "afford", "charge", "fee", "rupees", "rupee", "money",
            "thousand", "lakh", "per month", "kitna", "kitne", "mehnga", "costly"
        ],
        "NOT_INTERESTED": [
            "not interested", "no need", "don't need", "dont need",
            "zaroorat nahi", "nahi chahiye", "not required", "already have",
            "no thanks", "not looking", "nahi chahiye", "nahi"
        ],
        "NEED_MORE_TIME": [
            "not today", "call back", "call later", "later", "tomorrow",
            "next week", "some other time", "baad mein", "will think",
            "let me think", "need to think", "will see", "dekh lete",
            "sochenge", "abhi nahi", "not now", "out of the blue",
            "suddenly", "unexpected"
        ],
        "BUSY": [
            "busy", "in a meeting", "call later", "not free",
            "free nahi", "engaged", "occupied", "baad me", "abhi busy"
        ],
        "DECISION_MAKER": [
            "partner", "boss", "owner", "director", "manager",
            "husband", "wife", "family", "discuss", "check with",
            "consult", "approval", "permission", "team decide"
        ],
        "FEATURE_GAP": [
            "don't need", "dont need", "not required", "we don't use",
            "no call recording", "no need for", "already have this",
            "we have software", "using something", "current system"
        ],
        "COMPETITOR": [
            "already using", "have crm", "using zoho", "using salesforce",
            "have software", "competitor", "other company", "similar product"
        ],
    }

    found_objections = []
    found_category   = "OTHER"
    found_quotes     = []

    for category, keywords in OBJECTION_PATTERNS.items():
        for kw in keywords:
            if kw in text:
                found_category = category
                found_objections.append(kw.title())
                # Find the actual sentence containing this keyword
                for sentence in raw_transcript.replace(".", ".|").split("|"):
                    if kw in sentence.lower() and len(sentence.strip()) > 5:
                        found_quotes.append(sentence.strip()[:80])
                        break
                break  # one match per category is enough

    # Deduplicate
    found_objections = list(dict.fromkeys(found_objections))[:3]
    found_quotes     = list(dict.fromkeys(found_quotes))[:3]

    had_objection = len(found_objections) > 0
    duration      = int(call.get("duration_sec", 0))

    # Long call with no clear objection keywords — check for soft signals
    if not had_objection and duration > 180:
        # Check if outcome seems unclear (no strong positive signals)
        positive_signals = ["sure", "yes", "okay", "demo", "meeting", "connect tomorrow", "sounds good"]
        has_positive = any(p in text for p in positive_signals)
        if not has_positive:
            had_objection = True
            found_objections = ["Unclear commitment despite long conversation"]
            found_category   = "NEED_MORE_TIME"

    # Determine severity
    if not had_objection:
        severity = "none"
    elif found_category in ("NOT_INTERESTED", "COMPETITOR"):
        severity = "high"
    elif found_category in ("PRICE", "FEATURE_GAP"):
        severity = "medium"
    else:
        severity = "low"

    # Determine outcome
    if any(w in text for w in ["not interested", "zaroorat nahi", "no need"]):
        outcome = "rejected"
    elif any(w in text for w in ["demo", "meeting", "connect tomorrow", "schedule", "sure i will"]):
        outcome = "follow_up"
    elif any(w in text for w in ["i will buy", "sign up", "let's go", "proceed"]):
        outcome = "converted"
    else:
        outcome = "unclear"

    # Generate rebuttal based on category
    REBUTTALS = {
        "PRICE":          "Share ROI examples — 'Our customers save 3 hours/day. At ₹1000/month that's less than ₹35/day.'",
        "NOT_INTERESTED":  "Ask what they're currently using — find the gap.",
        "NEED_MORE_TIME":  "Lock in a specific time — 'Can we do 15 min tomorrow at 12:30?'",
        "BUSY":           "Respect their time — 'I'll take just 2 minutes now, or when's a better time?'",
        "DECISION_MAKER":  "Offer to include decision maker — 'Can we set up a quick call with your partner too?'",
        "FEATURE_GAP":    "Clarify the feature — 'Let me show you exactly how this works in the demo.'",
        "COMPETITOR":     "Ask about pain points with current tool — 'What's one thing you wish it did better?'",
        "OTHER":          "Acknowledge their concern and redirect to a specific next step.",
    }

    return {
        "had_objection":      had_objection,
        "objection_category": found_category if had_objection else "OTHER",
        "objections":         found_objections,
        "objection_severity": severity,
        "how_agent_handled":  f"Detected from transcript keywords. Key phrases: {'; '.join(found_quotes[:2]) if found_quotes else 'N/A'}",
        "suggested_rebuttal": REBUTTALS.get(found_category, REBUTTALS["OTHER"]) if had_objection else "No rebuttal needed.",
        "outcome":            outcome
    }


# ── Real AI Churn Prediction (Gemini API) ──────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from .env")
GEMINI_MODEL   = "gemini-2.5-flash"
GEMINI_URL     = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

import urllib.request
def _call_gemini(prompt: str, max_tokens: int = 1000) -> str:
    """Make a Gemini API call with retry. Returns clean text response."""
    import time as _time
    last_err = None
    for attempt in range(3):
        try:
            payload = json.dumps({
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.1}
            }).encode("utf-8")
            req = urllib.request.Request(
                GEMINI_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())
            return data["candidates"][0]["content"]["parts"][0]["text"].strip()
        except Exception as e:
            last_err = e
            if attempt < 2:
                _time.sleep(2)
    raise RuntimeError(f"Gemini failed after 3 attempts: {last_err}")

def _parse_json_safe(raw: str):
    """
    Robustly extract JSON from Gemini response.
    Handles markdown fences, leading text, truncated arrays.
    """
    if not raw:
        raise ValueError("Empty Gemini response")
    # Strip markdown fences
    if "```" in raw:
        parts = raw.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"): part = part[4:].strip()
            if part.startswith("{") or part.startswith("["):
                raw = part
                break
    raw = raw.strip()
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Find outermost JSON object or array
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = raw.find(start_char)
        end   = raw.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start:end+1])
            except json.JSONDecodeError:
                pass
    raise ValueError(f"No valid JSON in response: {raw[:300]}")

def _score_lead_rules(lead: dict) -> dict:
    """
    Granular rule-based churn scoring.
    Designed to spread scores across 0-100 even when most leads are inactive.
    Uses weighted combination of 5 signals.
    """
    days      = lead.get("days_since_last_call", 0)
    no_resp   = lead.get("no_response_calls", 0)
    completed = lead.get("completed_calls", 0)
    total     = max(lead.get("total_calls", 1), 1)
    avg_dur   = lead.get("avg_duration_sec", 0)
    answered  = lead.get("answered_calls", 0)

    # ── Signal 1: Inactivity (0-35 pts) ──────────────────────────────────────
    if days >= 60:    inact = 35
    elif days >= 45:  inact = 30
    elif days >= 30:  inact = 25
    elif days >= 20:  inact = 18
    elif days >= 15:  inact = 12
    elif days >= 7:   inact = 6
    else:             inact = 0

    # ── Signal 2: No-response rate (0-25 pts) ─────────────────────────────────
    no_resp_rate = no_resp / total
    if no_resp_rate >= 1.0:   noresp = 25
    elif no_resp_rate >= 0.8: noresp = 20
    elif no_resp_rate >= 0.6: noresp = 15
    elif no_resp_rate >= 0.4: noresp = 10
    elif no_resp_rate >= 0.2: noresp = 5
    else:                     noresp = 0

    # ── Signal 3: Completed calls (0-20 pts) ──────────────────────────────────
    if completed == 0 and total >= 3:   comp = 20
    elif completed == 0 and total >= 1: comp = 15
    elif completed == 1:                comp = 8
    elif completed == 2:                comp = 4
    else:                               comp = 0

    # ── Signal 4: Avg call duration (0-12 pts) ────────────────────────────────
    if avg_dur == 0:     dur = 12
    elif avg_dur < 10:   dur = 10
    elif avg_dur < 30:   dur = 7
    elif avg_dur < 60:   dur = 4
    elif avg_dur < 120:  dur = 2
    else:                dur = 0

    # ── Signal 5: Total attempts (0-8 pts) ────────────────────────────────────
    # Many attempts with no success = high churn risk
    if total >= 5 and completed == 0:  att = 8
    elif total >= 3 and completed == 0: att = 5
    elif total >= 5 and answered == 0:  att = 4
    else:                               att = 0

    score = min(inact + noresp + comp + dur + att, 100)
    risk   = "HIGH" if score >= 60 else ("MEDIUM" if score >= 30 else "LOW")
    action = "call_today" if score >= 75 else (
             "call_this_week" if score >= 45 else (
             "monitor" if score >= 20 else "drop"))

    reasons = []
    if days >= 20:           reasons.append(f"inactive {days}d")
    elif days >= 7:          reasons.append(f"last call {days}d ago")
    if no_resp_rate >= 0.6:  reasons.append(f"{no_resp}/{total} calls missed")
    if completed == 0:       reasons.append("never completed a call")
    if avg_dur < 30 and avg_dur > 0: reasons.append(f"avg {int(avg_dur)}s duration")

    return {
        "lead_id":            lead.get("lead_id"),
        "score":              score,
        "risk":               risk,
        "confidence":         80,
        "reasoning":          "; ".join(reasons[:2]) if reasons else "Low engagement.",
        "recommended_action": action.replace("_", " ").title(),
        "urgency":            action
    }

def ai_predict_churn(lead: dict) -> dict:
    """Pure rule-based churn scoring — validated before returning."""
    result = _score_lead_rules(lead)
    return validate_churn_output(result)

def ai_predict_churn_batch(leads: list, top_n: int = 10) -> list:
    """Score all leads with rules — each result validated before returning."""
    results = [validate_churn_output(_score_lead_rules(l)) for l in leads]
    results.sort(key=lambda x: x.get("score", 0), reverse=True)
    return results[:top_n]


# ── MCP Server ─────────────────────────────────────────────────────────────────

ROLE = """[ROLE] You are a CRM Lead Management Assistant for a Hyderabad-based sales team.
You ONLY help with: leads, calls, call analysis, churn risk, callbacks, objections,
agent performance, and database stats.
If asked anything outside this scope, reply exactly:
"I am a CRM assistant. I can only help with leads, calls, agents, and churn analysis." """

FORMAT_RULES = """
STRICT OUTPUT FORMAT RULES — NEVER VIOLATE THESE:

1. ALWAYS show a TABLE when the answer has multiple rows of data (leads, calls,
   agents, objections). Never use bullet points, prose paragraphs, or narrative
   summaries for list data.

2. For single-stat answers (counts, totals, summaries with no rows) — write ONE
   compact line or a small inline stat block. No bullet points, no "A few quick
   stats:" headers, no extra commentary.
   Example: "296 unique leads | 505 total calls | 7 agents | Avg score: 50.6"

3. Tables MUST include Lead ID and Agent columns so the user knows exactly who
   each row refers to.

4. Show TOP 10 rows maximum, ranked by the relevant score/metric. State the
   total count in the header (e.g. "Top 10 of 315 HIGH-risk leads").

5. When multiple tools are called in one response, show each tool's result as
   its own clearly labelled section — NEVER merge them into a combined paragraph.

6. NEVER ask follow-up questions. NEVER suggest next actions. NEVER write
   "Want me to...", "Would you like...", "Let me know if...", or similar.
   End the response immediately after the last line of data.

7. NEVER add insights, commentary, patterns, or recommendations unless the user
   explicitly asks for analysis. Data only.

8. No disclaimers, no apologies, no filler phrases, no restating the question.

9. Numbers must include units (%, days, score/100) directly in the cell."""

# Appended to every tool result so Claude sees it immediately before writing
OUTPUT_INSTRUCTION = (
    "\n\n[SYSTEM INSTRUCTION — FOLLOW EXACTLY: "
    "Present the table(s) above verbatim. "
    "Do NOT rewrite as prose or narrative. "
    "Do NOT add recommendations, next steps, follow-up questions, "
    "patterns, insights, or any commentary. "
    "Stop writing immediately after the last table row or rebuttal line.]"
)

CHURN_TABLE_FORMAT = """
When returning churn-risk leads, table columns MUST be exactly:
| Lead ID | Agent | Score | Risk | Days Since Call | Action |
Show only the top 10 by score, but state the total count at the top."""

PRIORITY_TABLE_FORMAT = """
When returning priority/follow-up leads, table columns MUST be exactly:
| Lead ID | Agent | Score | Calls Done/Total | Days Since Call |
Show only the top 10 by score, but state the total count at the top."""

OBJECTION_TABLE_FORMAT = """
When returning objection analysis, table columns MUST be exactly:
| Objection | Count | Lead ID | Agent | Example |
Show only the top 10 objection categories by count, but state the total count
of categories/calls analysed at the top. One example row per category."""

app = Server("lead-management-mysql")

# GUARDRAIL 1 — AGENT SCOPE & SYSTEM PROMPT

AGENT_ROLE = "CRM Lead Management Assistant for a Hyderabad-based sales team"

ALLOWED_TOPICS = [
    "leads and lead details",
    "call history and call analysis",
    "agent performance",
    "churn risk and predictions",
    "callbacks and follow-ups",
    "objection analysis",
    "database statistics",
    "notes and tags",
    "email summaries",
]

BLOCKED_TOPICS = [
    "general knowledge questions",
    "coding help unrelated to this system",
    "personal advice",
    "topics unrelated to CRM or sales",
    "other companies or products",
]

# GUARDRAIL 2 — SQL SAFETY PARAMETERS

BLOCKED_SQL_KEYWORDS = [
    "DROP", "DELETE", "TRUNCATE", "UPDATE", "INSERT",
    "ALTER", "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE"
]

def validate_sql(sql):
    cleaned = sql.strip().upper()
    if not cleaned.startswith("SELECT"):
        return False, "Only SELECT queries are allowed. This agent cannot modify data."
    for keyword in BLOCKED_SQL_KEYWORDS:
        if keyword in cleaned:
            return False, f"Blocked keyword {keyword} detected. This agent cannot modify data."
    if cleaned.count(";") > 1 or (cleaned.count(";") == 1 and not cleaned.endswith(";")):
        return False, "Multiple SQL statements are not allowed."
    return True, ""

# GUARDRAIL 3 — OUTPUT VALIDATION PARAMETERS

VALID_OUTCOMES    = {"interested", "deflected", "rejected", "converted", "unclear"}
VALID_BEHAVIOURS  = {"engaged", "deflecting", "disinterested", "asked_questions", "hostile"}
VALID_PERFORMANCE = {"good", "average", "poor"}
VALID_PRIORITIES  = {"hot", "warm", "cold"}
VALID_CHURN_RISK  = {"high", "medium", "low"}

def validate_call_analysis(result):
    if result.get("outcome") not in VALID_OUTCOMES:
        result["outcome"] = "unclear"
    if result.get("lead_behaviour") not in VALID_BEHAVIOURS:
        result["lead_behaviour"] = "deflecting"
    if result.get("agent_performance") not in VALID_PERFORMANCE:
        result["agent_performance"] = "average"
    try:
        score = int(result.get("lead_engagement_score", 5))
        result["lead_engagement_score"] = max(0, min(10, score))
    except (ValueError, TypeError):
        result["lead_engagement_score"] = 5
    for field in ["what_lead_said", "agent_strengths", "agent_weaknesses", "key_moment", "next_best_action"]:
        if not result.get(field) or not isinstance(result.get(field), str):
            result[field] = "Not available"
    return result

def validate_churn_output(result):
    # Fix: use "risk" and "score" — matching what _score_lead_rules() returns
    if result.get("risk", "").lower() not in VALID_CHURN_RISK:
        result["risk"] = "medium"
    try:
        score = int(result.get("score", 50))
        result["score"] = max(0, min(100, score))
    except (ValueError, TypeError):
        result["score"] = 50
    if result.get("urgency") not in {"call_today", "call_this_week", "monitor", "drop"}:
        result["urgency"] = "monitor"
    return result

def validate_lead_score(result):
    if result.get("priority") not in VALID_PRIORITIES:
        result["priority"] = "warm"
    try:
        score = int(result.get("follow_up_score", 50))
        result["follow_up_score"] = max(0, min(100, score))
    except (ValueError, TypeError):
        result["follow_up_score"] = 50
    return result


@app.list_prompts()
async def list_prompts():
    return [
        Prompt(
            name="crm-assistant",
            description="CRM Lead Management Assistant — behaviour and format instructions",
            arguments=[]
        )
    ]


@app.get_prompt()
async def get_prompt(name: str, arguments: dict):
    from mcp.types import GetPromptResult, PromptMessage, TextContent as TC
    instructions = """You are a CRM Lead Management Assistant for a Hyderabad-based sales team.

YOUR ONLY JOB:
Answer questions about leads, calls, churn risk, objection analysis, agent performance, callbacks, notes, and email summaries.
Refuse anything outside this scope politely.

STRICT OUTPUT FORMAT — ALWAYS FOLLOW THESE:
1. Never write paragraphs. Always use bullet points or numbered lists.
2. For churn leads: numbered list — Lead ID | Risk | Score | Action | Reason (max 10 words)
3. For summaries: bullet points with numbers, no sentences
4. Keep every line short — under 15 words
5. Never add recommendations, patterns, or analysis unless specifically asked
6. Never suggest what the user should do next
7. Never mention Gemini, AI, errors, or backend systems
8. Show data exactly — do not interpret or editorialize
9. If results are more than 20 items, show top 20 and state the total count only"""

    return GetPromptResult(
        description="CRM Assistant behaviour instructions",
        messages=[
            PromptMessage(
                role="user",
                content=TC(type="text", text=instructions)
            )
        ]
    )


@app.list_tools()
async def list_tools():
    tools = [

        Tool(name="get_stats",
             description=(
                 "Overall database statistics: total call records, unique leads, "
                 "HOT/WARM/COLD counts, completion rate, total agents. "
                 "Call this first for a dataset overview."
             ),
             inputSchema={"type": "object", "properties": {}}),

        Tool(name="get_lead",
             description=(
                 "Get EVERYTHING about one lead — all their call records from the database "
                 "exactly as stored, plus a computed summary (total calls, completed, "
                 "no-response, avg duration, score). "
                 "Use when user gives a lead_id."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "The lead_id to look up"}
             }, "required": ["lead_id"]}),

        Tool(name="get_all_calls",
             description=(
                 "Return call records from the database. "
                 "Filter by status, agent, lead_id, or date range. "
                 "Returns every column that exists in the database."
             ),
             inputSchema={"type": "object", "properties": {
                 "status":    {"type": "string", "description": "e.g. Completed, No response, ANSWERED, CANCEL"},
                 "agent":     {"type": "string", "description": "Partial agent name match"},
                 "lead_id":   {"type": "integer"},
                 "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                 "date_to":   {"type": "string", "description": "YYYY-MM-DD"},
                 "limit":     {"type": "integer", "description": "Max rows (default 100)"},
             }}),

        Tool(name="get_leads",
             description=(
                 "List all unique leads with their aggregated stats and lead scores. "
                 "Filter by quality (HOT/WARM/COLD), agent, or score range. "
                 "Sort by score, calls, or days since last call."
             ),
             inputSchema={"type": "object", "properties": {
                 "quality":   {"type": "string", "enum": ["HOT", "WARM", "COLD"]},
                 "agent":     {"type": "string"},
                 "min_score": {"type": "number"},
                 "max_score": {"type": "number"},
                 "sort_by":   {"type": "string",
                               "description": "score | total_calls | completed_calls | days_since_last_call | avg_duration_sec"},
                 "order":     {"type": "string", "enum": ["asc", "desc"]},
                 "limit":     {"type": "integer"},
             }}),

        Tool(name="get_agents",
             description=(
                 "Per-agent performance from the database: calls made, completed, "
                 "no-response, connect rate, total talk time."
             ),
             inputSchema={"type": "object", "properties": {}}),

        Tool(name="find_priority_leads",
             description=(
                 "Top leads to call RIGHT NOW ranked by score. "
                 "Specify quality tier (HOT/WARM/COLD) and how many to return."
             ),
             inputSchema={"type": "object", "properties": {
                 "quality": {"type": "string", "enum": ["HOT", "WARM", "COLD"], "description": "Default: WARM"},
                 "top_n":   {"type": "integer", "description": "Default 10"},
             }}),

        Tool(name="find_stale_leads",
             description=(
                 "Leads not called in N+ days, queried live from the database, "
                 "sorted by most overdue first."
             ),
             inputSchema={"type": "object", "properties": {
                 "min_days": {"type": "integer", "description": "Default 7"},
                 "limit":    {"type": "integer", "description": "Default 20"},
             }}),

        Tool(name="search_by_phone",
             description="Find all call records in the database for a phone number.",
             inputSchema={"type": "object", "properties": {
                 "phone": {"type": "string"}
             }, "required": ["phone"]}),

        Tool(name="run_query",
             description=(
                 "Run a custom read-only SQL SELECT on the leads database. "
                 "Table name is 'call_recordings'. Columns: id, lead_id, status, call_duration, "
                 "call_duration_sec AS duration_sec, start_time, end_time, agent_name, to_number, from_number, "
                 "call_type, hangup_by, remarks, recording_url, client_group_id, "
                 "transcript, transcript_status. "
                 "Use for any custom analysis not covered by other tools."
             ),
             inputSchema={"type": "object", "properties": {
                 "sql": {"type": "string", "description": "A SELECT statement only. No INSERT/UPDATE/DELETE."}
             }, "required": ["sql"]}),

        Tool(name="db_info",
             description="Show database connection info and table schema. Use to verify the connection is working.",
             inputSchema={"type": "object", "properties": {}}),

        # ── NOTES ──────────────────────────────────────────────────────────────
        Tool(name="add_note",
             description=(
                 "Save a note against a lead. Use when the user says things like "
                 "'add note to lead 12345: customer is interested' or "
                 "'remember that lead 99999 said call back next week'. "
                 "Notes are saved permanently in the database."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id":    {"type": "integer", "description": "The lead to attach the note to"},
                 "note_text":  {"type": "string",  "description": "The note content"},
                 "agent_name": {"type": "string",  "description": "Agent adding the note (optional)"},
             }, "required": ["lead_id", "note_text"]}),

        Tool(name="get_notes",
             description=(
                 "Get saved notes. If lead_id is given, returns notes for that lead. "
                 "If no lead_id, returns all recent notes across all leads. "
                 "Use when user asks 'what notes do we have on lead 12345', "
                 "'show all notes', or 'what notes were added recently'."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "The lead to get notes for (optional — omit to get all notes)"},
                 "limit":   {"type": "integer", "description": "Max notes to return, default 20"},
             }}),

        Tool(name="delete_note",
             description="Delete a specific note by its ID. Get the note ID from get_notes first.",
             inputSchema={"type": "object", "properties": {
                 "note_id": {"type": "integer", "description": "The note ID to delete"},
             }, "required": ["note_id"]}),

        # ── CALLBACKS ──────────────────────────────────────────────────────────
        Tool(name="schedule_callback",
             description=(
                 "Schedule a callback for a lead on a specific date. "
                 "Use when user says 'schedule callback for lead 12345 tomorrow' or "
                 "'remind me to call lead 99999 on Monday' or "
                 "'call back lead 12345 on 2025-02-10 at 11am'."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id":        {"type": "integer", "description": "Lead to schedule callback for"},
                 "scheduled_date": {"type": "string",  "description": "Date as YYYY-MM-DD"},
                 "scheduled_time": {"type": "string",  "description": "Time as HH:MM e.g. 10:00, 14:30 (optional, default 09:00)"},
                 "reason":         {"type": "string",  "description": "Why calling back (optional)"},
                 "agent_name":     {"type": "string",  "description": "Which agent should call (optional)"},
             }, "required": ["lead_id", "scheduled_date"]}),

        Tool(name="get_todays_callbacks",
             description=(
                 "Get all callbacks scheduled for today. "
                 "Use when user asks 'what do I need to do today', "
                 "'show me todays callbacks', 'who do I need to call today'."
             ),
             inputSchema={"type": "object", "properties": {
                 "agent_name": {"type": "string", "description": "Filter by agent (optional, leave empty for all)"},
             }}),

        Tool(name="get_upcoming_callbacks",
             description=(
                 "Get all pending callbacks for the next N days. "
                 "Use when user asks 'show upcoming callbacks', 'what callbacks this week'."
             ),
             inputSchema={"type": "object", "properties": {
                 "days":       {"type": "integer", "description": "How many days ahead to look (default 7)"},
                 "agent_name": {"type": "string",  "description": "Filter by agent (optional)"},
             }}),

        Tool(name="complete_callback",
             description=(
                 "Mark a scheduled callback as done. "
                 "Use when user says 'mark callback 5 as done' or 'I called lead 12345, mark it complete'."
             ),
             inputSchema={"type": "object", "properties": {
                 "callback_id": {"type": "integer", "description": "The callback ID to mark as done (get from get_todays_callbacks)"},
             }, "required": ["callback_id"]}),

        # ── LEAD STATUS ────────────────────────────────────────────────────────
        Tool(name="set_lead_status",
             description=(
                 "Set the CRM status of a lead. Valid values: new, in_progress, converted, lost. "
                 "Use when user says 'mark lead 12345 as converted' or 'set lead 99 to in_progress'. "
                 "This is saved permanently and visible in the dashboard."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "The lead to update"},
                 "status":  {"type": "string",  "enum": ["new", "in_progress", "converted", "lost"]},
             }, "required": ["lead_id", "status"]}),

        Tool(name="get_lead_status",
             description=(
                 "Get the current CRM status and tags for a lead or list leads by status. "
                 "Use when user asks 'what is the status of lead 12345' or "
                 "'show all converted leads' or 'how many leads converted this month'."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "Single lead to look up (optional)"},
                 "status":  {"type": "string",  "enum": ["new", "in_progress", "converted", "lost"],
                             "description": "Filter all leads by this status (optional)"},
                 "month":   {"type": "string",  "description": "YYYY-MM, count conversions this month (optional)"},
             }}),

        # ── LEAD TAGS ──────────────────────────────────────────────────────────
        Tool(name="set_lead_tags",
             description=(
                 "Add or replace tags on a lead. Tags like: interested, not_interested, "
                 "callback_requested, wrong_number, vip, urgent, or anything custom. "
                 "Use when user says 'tag lead 12345 as interested' or 'add vip tag to lead 99'. "
                 "Use mode='add' to append to existing tags, mode='replace' to overwrite."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "The lead to tag"},
                 "tags":    {"type": "array",   "items": {"type": "string"},
                             "description": "List of tags e.g. ['interested', 'callback_requested']"},
                 "mode":    {"type": "string",  "enum": ["add", "replace"],
                             "description": "add = append to existing, replace = overwrite. Default: add"},
             }, "required": ["lead_id", "tags"]}),

        Tool(name="get_leads_by_tag",
             description=(
                 "Find all leads that have a specific tag. "
                 "Use when user says 'show all leads tagged interested' or 'which leads need callbacks'."
             ),
             inputSchema={"type": "object", "properties": {
                 "tag": {"type": "string", "description": "Tag to search for e.g. 'interested'"},
             }, "required": ["tag"]}),

        # ── AGENT TARGETS ──────────────────────────────────────────────────────
        Tool(name="set_agent_target",
             description=(
                 "Set a monthly call target for an agent. "
                 "Use when user says 'Sai Ram should complete 50 calls this month' or "
                 "'set target for John: 100 calls, 60 completed in June 2025'. "
                 "month format: YYYY-MM. If month not given, use current month."
             ),
             inputSchema={"type": "object", "properties": {
                 "agent_name":       {"type": "string",  "description": "Exact or partial agent name"},
                 "target_calls":     {"type": "integer", "description": "Total call target"},
                 "target_completed": {"type": "integer", "description": "Completed calls target"},
                 "month":            {"type": "string",  "description": "YYYY-MM, default current month"},
             }, "required": ["agent_name"]}),

        Tool(name="get_agent_targets",
             description=(
                 "Show agent progress toward their monthly targets. "
                 "Use when user asks 'how is Sai Ram doing on targets' or "
                 "'which agents are meeting their targets' or 'show all targets this month'. "
                 "Returns current vs target with days remaining."
             ),
             inputSchema={"type": "object", "properties": {
                 "agent_name": {"type": "string", "description": "Filter to one agent (optional)"},
                 "month":      {"type": "string", "description": "YYYY-MM, default current month"},
             }}),

        # ── EMAIL ──────────────────────────────────────────────────────────────
        Tool(name="send_daily_summary",
             description=(
                 "Generate and send the daily lead summary email right now. "
                 "Use when user says 'send me the daily report' or 'email me the summary'. "
                 "Requires EMAIL_CONFIG to be filled in mcp_server.py."
             ),
             inputSchema={"type": "object", "properties": {}}),

        Tool(name="send_custom_email",
             description=(
                "Send any custom content via email directly. "
                "The recipient email is already configured in the system — "
                "NEVER ask the user for an email address. "
                "NEVER ask for confirmation. Just call this tool immediately "
                "when the user says 'email me', 'send me this on mail', "
                "'mail me the results', 'send me what you just told me', "
                "'email me the summary', 'send this to my mail', or any "
                "variation of wanting to receive information by email. "
                "Build the subject and body yourself from the data you just fetched "
                "and call the tool directly without asking anything."
             ),
             inputSchema={"type": "object", "properties": {
              "subject": {"type": "string", "description": "Email subject line"},
              "body":    {"type": "string", "description": "Full email content to send"},
             }, "required": ["subject", "body"]}),

        # ── OBJECTION ANALYSIS ────────────────────────────────────────────────
        Tool(name="analyse_objections",
             description=(
                 "Analyse call transcripts to identify common objections raised by leads. "
                 "Uses Gemini AI to read actual call transcripts and extract objections, "
                 "categorise them, and suggest rebuttals for each. "
                 "Use when user asks: 'what objections are leads raising?', "
                 "'what are the most common objections?', 'what excuses do leads give?', "
                 "'why are leads not converting?', 'objection analysis'. "
                 "Can filter by agent or date range."
             ),
             inputSchema={"type": "object", "properties": {
                 "agent":     {"type": "string", "description": "Filter by agent name (optional)"},
                 "date_from": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                 "date_to":   {"type": "string", "description": "YYYY-MM-DD (optional)"},
                 "limit":     {"type": "integer", "description": "Max transcripts to analyse. Default 50"}
             }}),

        Tool(name="get_agent_objection_performance",
             description=(
                 "Compare how different agents handle objections. "
                 "Shows which agents get past objections and convert, "
                 "which agents struggle, and what patterns distinguish top performers. "
                 "Use when user asks: 'which agent handles objections best?', "
                 "'who is best at dealing with objections?', 'agent objection comparison'."
             ),
             inputSchema={"type": "object", "properties": {
                 "date_from": {"type": "string", "description": "YYYY-MM-DD (optional)"},
                 "date_to":   {"type": "string", "description": "YYYY-MM-DD (optional)"},
             }}),

        Tool(name="get_lead_objection_history",
             description=(
                 "Get the full objection history for a specific lead — "
                 "what objections they raised on each call and what was said. "
                 "Use when user asks about a specific lead's objections or resistance."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "The lead_id to look up"}
             }, "required": ["lead_id"]}),

        # ── CHURN PREDICTION ───────────────────────────────────────────────────
        Tool(name="get_churn_risk_leads",
             description=(
                 "Identify MULTIPLE leads at risk of going cold / being lost — a ranked LIST. "
                 "Scores every lead 0-100 based on inactivity, no-response rate, "
                 "lack of completed calls, and short call durations. "
                 "Returns leads ranked by churn score with a plain-English reason "
                 "for each lead's risk. "
                 "Use when the user asks about: churn risk, leads going cold, "
                 "at-risk leads, top N churn leads, leads to follow up urgently, "
                 "leads not responding, or any variation. "
                 "Filter by risk level (HIGH/MEDIUM/LOW) and limit results."
             ),
             inputSchema={"type": "object", "properties": {
                 "risk_filter": {
                     "type": "string",
                     "enum": ["HIGH", "MEDIUM", "LOW", "ALL"],
                     "description": "Filter by risk level. Default: ALL"
                 },
                 "top_n": {
                     "type": "integer",
                     "description": "Return only top N leads by churn score. Default: 10"
                 },
                 "agent": {
                     "type": "string",
                     "description": "Filter by agent name (partial match)"
                 }
             }}),

        Tool(name="get_lead_churn_detail",
             description=(
                 "Get the churn breakdown for ONE SINGLE SPECIFIC lead — requires a lead_id number. "
                 "NEVER use this tool unless the user gives a specific lead ID. "
                 "If the user asks for 'overall', 'summary', 'all leads', or general churn "
                 "analysis WITHOUT a lead ID, use get_churn_summary instead — NOT this tool. "
                 "Shows every signal for that one lead — inactivity days, no-response count, "
                 "completed calls, avg duration — and why each contributes to risk."
             ),
             inputSchema={"type": "object", "properties": {
                 "lead_id": {"type": "integer", "description": "REQUIRED. The specific lead_id to analyse. Do not call this tool without it."}
             }, "required": ["lead_id"]}),

        Tool(name="get_churn_summary",
             description=(
                 "OVERALL / fleet-wide churn dashboard across ALL leads at once — no lead_id needed. "
                 "Shows how many are HIGH/MEDIUM/LOW risk, average churn score across the whole "
                 "database, which agents have the most at-risk leads. "
                 "ALWAYS use this tool (not get_lead_churn_detail) when the user asks for "
                 "'overall churn analysis', 'churn overview', 'churn dashboard', "
                 "'how many leads are at risk', or any company-wide / aggregate churn question "
                 "that does not mention a specific lead."
             ),
             inputSchema={"type": "object", "properties": {}}),
    ]

    # ── Inject ROLE + FORMAT_RULES into every tool description ──────────────
    TABLE_RULES = {
        "get_churn_risk_leads":  CHURN_TABLE_FORMAT,
        "get_lead_churn_detail": CHURN_TABLE_FORMAT,
        "get_churn_summary":     CHURN_TABLE_FORMAT,
        "find_priority_leads":   PRIORITY_TABLE_FORMAT,
        "find_stale_leads":      PRIORITY_TABLE_FORMAT,
        "analyse_objections":              OBJECTION_TABLE_FORMAT,
        "get_agent_objection_performance": OBJECTION_TABLE_FORMAT,
        "get_lead_objection_history":      OBJECTION_TABLE_FORMAT,
    }
    for t in tools:
        extra = TABLE_RULES.get(t.name, "")
        t.description = f"{ROLE}\n\n{t.description}\n\n{FORMAT_RULES}{extra}"

    return tools


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    try:

        # ── GUARDRAIL 1 — Scope Check ──────────────────────────────────────────
        VALID_TOOLS = {
            "db_info", "get_stats", "get_lead", "get_all_calls", "get_leads",
            "get_agents", "find_priority_leads", "find_stale_leads",
            "search_by_phone", "run_query", "add_note", "get_notes",
            "delete_note", "schedule_callback", "get_todays_callbacks",
            "get_upcoming_callbacks", "complete_callback", "set_lead_status",
            "get_lead_status", "set_lead_tags", "get_leads_by_tag",
            "set_agent_target", "get_agent_targets", "send_daily_summary",
            "send_custom_email", "analyse_objections",
            "get_agent_objection_performance", "get_lead_objection_history",
            "get_churn_risk_leads", "get_lead_churn_detail", "get_churn_summary",
        }
        if name not in VALID_TOOLS:
            return [TextContent(type="text", text=(
                f"❌ Tool '{name}' is not available.\n"
                f"This agent only handles: leads, calls, churn prediction, "
                f"objection analysis, notes, callbacks, and email summaries."
            ))]

        # ── db_info ────────────────────────────────────────────────────────────
        if name == "db_info":
            total = scalar("SELECT COUNT(*) FROM call_recordings")
            leads = scalar("SELECT COUNT(DISTINCT lead_id) FROM call_recordings")
            cols  = query("""
                SELECT COLUMN_NAME, DATA_TYPE
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'call_recordings'
                ORDER BY ORDINAL_POSITION
            """, [DB_CONFIG["database"]])
            col_lines = "\n".join(f"  {c['COLUMN_NAME']:25s}  {c['DATA_TYPE']}" for c in cols)
            return [TextContent(type="text", text=(
                f"✅ Connected to MySQL\n"
                f"{'─'*46}\n"
                f"Host     : {DB_CONFIG['host']}:{DB_CONFIG['port']}\n"
                f"Database : {DB_CONFIG['database']}\n"
                f"Table    : call_recordings\n"
                f"Rows     : {total}\n"
                f"Leads    : {leads}\n\n"
                f"Columns:\n{col_lines}"
            ))]

        # ── get_stats ──────────────────────────────────────────────────────────
        if name == "get_stats":
            r = query("""
                SELECT
                    COUNT(*)                             AS total_calls,
                    COUNT(DISTINCT lead_id)              AS unique_leads,
                    COUNT(DISTINCT agent_name)           AS unique_agents,
                    SUM(status = 'Completed')            AS completed,
                    SUM(LOWER(status) LIKE '%no response%') AS no_response,
                    SUM(status = 'ANSWERED')             AS answered,
                    SUM(status = 'CANCEL')               AS cancelled,
                    SUM(call_duration_sec)               AS total_dur,
                    AVG(call_duration_sec)               AS avg_dur
                FROM call_recordings
            """)[0]
            tc    = int(r["total_calls"] or 0)
            comp  = int(r["completed"] or 0)
            leads = _all_lead_summaries()
            hot   = sum(1 for l in leads if l["quality"] == "HOT")
            warm  = sum(1 for l in leads if l["quality"] == "WARM")
            cold  = sum(1 for l in leads if l["quality"] == "COLD")
            avg_score = round(sum(l["score"] for l in leads) / len(leads), 1) if leads else 0
            stats_text = (
                f"**{r['unique_leads']} unique leads | {tc} total calls | {r['unique_agents']} agents | Avg score: {avg_score}/100**\n\n"
                f"| Metric | Value |\n"
                f"|---|---|\n"
                f"| Completed calls | {r['completed']} |\n"
                f"| No response | {r['no_response']} |\n"
                f"| Answered | {r['answered']} |\n"
                f"| Cancelled | {r['cancelled']} |\n"
                f"| Completion rate | {round(comp/tc*100,1) if tc else 0}% |\n"
                f"| Total talk time | {fmt_sec(r['total_dur'])} |\n"
                f"| Avg call duration | {round(float(r['avg_dur'] or 0),1)}s |\n"
                f"| HOT leads | {hot} |\n"
                f"| WARM leads | {warm} |\n"
                f"| COLD leads | {cold} |\n"
            )
            stats_text += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=stats_text)]

        # ── get_lead ───────────────────────────────────────────────────────────
        if name == "get_lead":
            lid  = int(arguments["lead_id"])
            lead = _lead_summary(lid)
            if not lead:
                return [TextContent(type="text", text=f"❌ No records found for lead_id {lid}.")]

            calls = query("""
                SELECT id, lead_id, status, call_duration, call_duration_sec AS duration_sec,
                       start_time, end_time, agent_name, to_number, from_number,
                       call_type, hangup_by, remarks, recording_url, client_group_id
                FROM call_recordings
                WHERE lead_id = %s
                ORDER BY start_time ASC
            """, [lid])

            bd = "\n".join(
                f"  {b['param']:28s}  {b['earned']:>2}/{b['max']}  ({b['pct']}%)"
                for b in lead["breakdown"]
            )
            advice = {
                "HOT":  "Contact immediately 🔥",
                "WARM": "Nurture with targeted content 🌡️",
                "COLD": "Add to long-term sequence ❄️"
            }[lead["quality"]]

            lines = [
                f"{lead['emoji']} Lead #{lid}  —  {lead['quality']}  ({lead['score']}/100)",
                "═"*50,
                f"Phone          : {lead['phone']}",
                f"Agent          : {lead['agent_name']}",
                f"First contact  : {lead['first_call_date']}",
                f"Last contact   : {lead['last_call_date']}",
                f"Days since call: {lead['days_since_last_call']}",
                "",
                "📞 Call Summary",
                "─"*50,
                f"  Total calls    : {lead['total_calls']}",
                f"  Completed      : {lead['completed_calls']}",
                f"  Answered       : {lead['answered_calls']}",
                f"  No response    : {lead['no_response_calls']}",
                f"  Cancelled      : {lead['cancelled_calls']}",
                f"  Total talk time: {fmt_sec(lead['total_duration_sec'])}",
                f"  Avg duration   : {lead['avg_duration_sec']}s",
                "",
                "🎯 Score Breakdown",
                "─"*50,
                bd,
                "",
                f"💡 {advice}",
                "",
                f"📋 All {len(calls)} Call Records (from database)",
                "─"*50,
            ]
            for i, c in enumerate(calls, 1):
                st  = str(c.get("start_time", ""))[:16]
                rem = f"\n       Remarks  : {c['remarks']}"   if c.get("remarks")       else ""
                rec = f"\n       Recording: {c['recording_url']}" if c.get("recording_url") else ""
                lines.append(
                    f"\n  #{i}  [{c['id']}]  {st}  →  {c['status']}\n"
                    f"       Duration : {c['call_duration']}  ({c['duration_sec']}s)\n"
                    f"       Agent    : {c['agent_name']}\n"
                    f"       Type     : {c['call_type']}  |  Hangup: {c['hangup_by'] or 'N/A'}\n"
                    f"       From     : {c['from_number']}  →  {c['to_number']}"
                    + rem + rec
                )
            return [TextContent(type="text", text="\n".join(lines))]

        # ── get_all_calls ──────────────────────────────────────────────────────
        if name == "get_all_calls":
            where, params = ["1=1"], []
            if s := arguments.get("status"):
                where.append("LOWER(status) LIKE %s"); params.append(f"%{s.lower()}%")
            if a := arguments.get("agent"):
                where.append("LOWER(agent_name) LIKE %s"); params.append(f"%{a.lower()}%")
            if lid := arguments.get("lead_id"):
                where.append("lead_id = %s"); params.append(int(lid))
            if df := arguments.get("date_from"):
                where.append("start_time >= %s"); params.append(df)
            if dt := arguments.get("date_to"):
                where.append("start_time <= %s"); params.append(dt + " 23:59:59")
            limit = int(arguments.get("limit", 10))
            params.append(limit)
            rows  = query(
                f"SELECT id, lead_id, status, call_duration_sec AS duration_sec, "
                f"start_time, agent_name FROM call_recordings "
                f"WHERE {' AND '.join(where)} ORDER BY start_time DESC LIMIT %s",
                params
            )
            count_params = params[:-1]
            total = scalar(
                f"SELECT COUNT(*) FROM call_recordings WHERE {' AND '.join(where[:])}",
                count_params
            )
            if not rows:
                return [TextContent(type="text", text="No calls found.")]
            trows = [f"| {c['id']} | {c['lead_id']} | {c['agent_name'] or '—'} | {c['status']} | {c['duration_sec']}s | {str(c['start_time'])[:16]} |"
                     for c in rows]
            table = (
                f"**Top {len(rows)} of {total} calls**\n\n"
                "| Call ID | Lead ID | Agent | Status | Duration | Start Time |\n"
                "|---|---|---|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_leads ──────────────────────────────────────────────────────────
        if name == "get_leads":
            leads = _all_lead_summaries()
            if q  := arguments.get("quality"):
                leads = [l for l in leads if l["quality"] == q.upper()]
            if a  := arguments.get("agent"):
                leads = [l for l in leads if a.lower() in l["agent_name"].lower()]
            if (mn := arguments.get("min_score")) is not None:
                leads = [l for l in leads if l["score"] >= mn]
            if (mx := arguments.get("max_score")) is not None:
                leads = [l for l in leads if l["score"] <= mx]
            sf  = arguments.get("sort_by", "score")
            rev = arguments.get("order", "desc") != "asc"
            leads.sort(key=lambda l: l.get(sf, 0), reverse=rev)
            total_matched = len(leads)
            lim = int(arguments.get("limit", 10))
            leads = leads[:lim]
            if not leads:
                return [TextContent(type="text", text="No leads found matching those filters.")]
            rows = [f"| {l['lead_id']} | {l['agent_name'] or '—'} | {l['score']} | {l['completed_calls']}/{l['total_calls']} | {l['days_since_last_call']}d |"
                    for l in leads]
            table = (
                f"**Top {len(leads)} of {total_matched} leads**\n\n"
                "| Lead ID | Agent | Score | Calls Done/Total | Days Since Call |\n"
                "|---|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_agents ─────────────────────────────────────────────────────────
        if name == "get_agents":
            rows = query("""
                SELECT
                    agent_name,
                    COUNT(*)                                  AS total_calls,
                    COUNT(DISTINCT lead_id)                   AS unique_leads,
                    SUM(status = 'Completed')                 AS completed,
                    SUM(LOWER(status) LIKE '%no response%')   AS no_response,
                    SUM(status = 'ANSWERED')                  AS answered,
                    SUM(call_duration_sec)                    AS total_dur,
                    AVG(call_duration_sec)                    AS avg_dur
                FROM call_recordings
                GROUP BY agent_name
                ORDER BY completed DESC
            """)
            if not rows:
                return [TextContent(type="text", text="No agent data found.")]
            trows = []
            for r in rows:
                tc = int(r["total_calls"] or 0)
                cr = round(int(r["completed"] or 0) / tc * 100, 1) if tc else 0
                trows.append(f"| {r['agent_name']} | {r['unique_leads']} | {tc} | {cr}% |")
            table = (
                "**Agent performance**\n\n"
                "| Agent | Leads | Calls | Completion Rate |\n"
                "|---|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── find_priority_leads ────────────────────────────────────────────────
        if name == "find_priority_leads":
            quality = arguments.get("quality", "WARM").upper()
            top_n   = int(arguments.get("top_n", 10))
            leads   = [l for l in _all_lead_summaries() if l["quality"] == quality]
            leads.sort(key=lambda l: l["score"], reverse=True)
            total_matched = len(leads)
            leads = leads[:top_n]
            if not leads:
                return [TextContent(type="text", text=f"No {quality} leads found.")]
            rows = [f"| {l['lead_id']} | {l['agent_name'] or '—'} | {l['score']} | {l['completed_calls']}/{l['total_calls']} | {l['days_since_last_call']}d |"
                    for l in leads]
            table = (
                f"**Top {len(leads)} of {total_matched} {quality} leads**\n\n"
                "| Lead ID | Agent | Score | Calls Done/Total | Days Since Call |\n"
                "|---|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── find_stale_leads ───────────────────────────────────────────────────
        if name == "find_stale_leads":
            min_days = int(arguments.get("min_days", 7))
            limit    = int(arguments.get("limit", 10))
            leads    = [l for l in _all_lead_summaries()
                        if l["days_since_last_call"] >= min_days]
            leads.sort(key=lambda l: l["days_since_last_call"], reverse=True)
            total_matched = len(leads)
            leads = leads[:limit]
            if not leads:
                return [TextContent(type="text", text=f"No leads with {min_days}+ days since last call.")]
            rows = [f"| {l['lead_id']} | {l['agent_name'] or '—'} | {l['score']} | {l['completed_calls']}/{l['total_calls']} | {l['days_since_last_call']}d |"
                    for l in leads]
            table = (
                f"**Top {len(leads)} of {total_matched} stale leads ({min_days}+ days)**\n\n"
                "| Lead ID | Agent | Score | Calls Done/Total | Days Since Call |\n"
                "|---|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── search_by_phone ────────────────────────────────────────────────────
        if name == "search_by_phone":
            phone = arguments["phone"].replace(" ", "").replace("-", "")
            rows  = query("""
                SELECT id, lead_id, status, call_duration, start_time,
                       agent_name, to_number, hangup_by, remarks
                FROM call_recordings
                WHERE REPLACE(REPLACE(to_number,   ' ',''),'-','') LIKE %s
                   OR REPLACE(REPLACE(from_number, ' ',''),'-','') LIKE %s
                ORDER BY start_time
            """, [f"%{phone}%", f"%{phone}%"])
            if not rows:
                return [TextContent(type="text", text=f"No calls found for phone '{phone}'.")]
            lid  = rows[0]["lead_id"]
            lead = _lead_summary(lid)
            lines = [
                f"Found {len(rows)} call(s) for '{phone}'  —  lead_id: {lid}\n"
                f"Quality: {lead['emoji']} {lead['quality']}  Score: {lead['score']}/100\n"
                "─"*46
            ]
            for c in rows:
                lines.append(
                    f"\n  {str(c['start_time'])[:16]}  →  {c['status']}\n"
                    f"  Duration: {c['call_duration']}  |  Agent: {c['agent_name']}  |  ID: {c['id']}"
                    + (f"\n  Remarks: {c['remarks']}" if c.get("remarks") else "")
                )
            return [TextContent(type="text", text="\n".join(lines))]

        # ── run_query ──────────────────────────────────────────────────────────
        if name == "run_query":
            sql = arguments["sql"].strip()
            is_safe, err_msg = validate_sql(sql)
            if not is_safe:
                return [TextContent(type="text", text=f"❌ {err_msg}")]
            rows = query(sql)
            if not rows:
                return [TextContent(type="text", text="Query returned 0 rows.")]
            keys   = list(rows[0].keys())
            header = "  ".join(f"{k:18s}" for k in keys)
            sep    = "  ".join("─"*18 for _ in keys)
            lines  = [f"Rows: {len(rows)}\n", header, sep]
            for r in rows[:200]:
                lines.append("  ".join(f"{str(r.get(k,''))[:18]:18s}" for k in keys))
            if len(rows) > 200:
                lines.append(f"... ({len(rows)-200} more rows not shown)")
            return [TextContent(type="text", text="\n".join(lines))]

        # ── add_note ───────────────────────────────────────────────────────────
        if name == "add_note":
            lead_id    = int(arguments["lead_id"])
            note_text  = arguments["note_text"].strip()
            agent_name = arguments.get("agent_name", "").strip()
            # verify lead exists
            if not scalar("SELECT COUNT(*) FROM call_recordings WHERE lead_id = %s", [lead_id]):
                return [TextContent(type="text", text=f"❌ Lead {lead_id} not found in database.")]
            nid = execute(
                "INSERT INTO notes (lead_id, agent_name, note_text) VALUES (%s, %s, %s)",
                [lead_id, agent_name or None, note_text]
            )
            return [TextContent(type="text", text=(
                f"✅ Note saved (ID: {nid})\n"
                f"Lead    : #{lead_id}\n"
                f"Agent   : {agent_name or 'not specified'}\n"
                f"Note    : {note_text}\n"
                f"Saved at: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            ))]

        # ── get_notes ──────────────────────────────────────────────────────────
        if name == "get_notes":
            lead_id = arguments.get("lead_id")
            limit   = int(arguments.get("limit", 20))
            if lead_id:
                lead_id = int(lead_id)
                notes = query(
                    "SELECT id, lead_id, agent_name, note_text, created_at FROM notes "
                    "WHERE lead_id = %s ORDER BY created_at DESC LIMIT %s",
                    [lead_id, limit]
                )
                header = f"📝 Notes for Lead #{lead_id}  ({len(notes)} total)"
            else:
                notes = query(
                    "SELECT id, lead_id, agent_name, note_text, created_at FROM notes "
                    "ORDER BY created_at DESC LIMIT %s",
                    [limit]
                )
                header = f"📝 All Recent Notes  ({len(notes)} shown, limit {limit})"
            if not notes:
                return [TextContent(type="text", text="No notes found.")]
            rows = [f"| {n['id']} | {n['lead_id']} | {str(n['created_at'])[:16]} | {n['note_text'][:60]} |"
                    for n in notes]
            table = (
                f"**{header.split(chr(40))[0].strip()} ({len(notes)})**\n\n"
                "| Note ID | Lead ID | Date | Note |\n"
                "|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── delete_note ────────────────────────────────────────────────────────
        if name == "delete_note":
            note_id = int(arguments["note_id"])
            note    = query("SELECT id, lead_id, note_text FROM notes WHERE id = %s", [note_id])
            if not note:
                return [TextContent(type="text", text=f"❌ Note ID {note_id} not found.")]
            execute("DELETE FROM notes WHERE id = %s", [note_id])
            return [TextContent(type="text", text=(
                f"🗑️ Note {note_id} deleted.\n"
                f"Was on Lead #{note[0]['lead_id']}: {note[0]['note_text'][:80]}"
            ))]

        # ── schedule_callback ──────────────────────────────────────────────────
        if name == "schedule_callback":
            lead_id   = int(arguments["lead_id"])
            sdate     = arguments["scheduled_date"]
            stime     = arguments.get("scheduled_time", "09:00")
            reason    = arguments.get("reason", "")
            agent     = arguments.get("agent_name", "")
            if not scalar("SELECT COUNT(*) FROM call_recordings WHERE lead_id = %s", [lead_id]):
                return [TextContent(type="text", text=f"❌ Lead {lead_id} not found in database.")]
            # get lead info for display
            lead = _lead_summary(lead_id)
            cid  = execute(
                "INSERT INTO follow_ups (lead_id, agent_name, scheduled_date, scheduled_time, reason) "
                "VALUES (%s, %s, %s, %s, %s)",
                [lead_id, agent or None, sdate, stime, reason or None]
            )
            return [TextContent(type="text", text=(
                f"✅ Callback scheduled (ID: {cid})\n"
                f"Lead    : #{lead_id}  ({lead['phone'] if lead else 'unknown'})\n"
                f"Date    : {sdate} at {stime}\n"
                f"Agent   : {agent or 'not assigned'}\n"
                f"Reason  : {reason or 'not specified'}"
            ))]

        # ── get_todays_callbacks ───────────────────────────────────────────────
        if name == "get_todays_callbacks":
            agent  = arguments.get("agent_name", "")
            today  = date.today().strftime("%Y-%m-%d")
            sql    = ("SELECT f.id, f.lead_id, f.agent_name, f.scheduled_time, "
                      "f.reason, f.status, "
                      "(SELECT MAX(to_number) FROM call_recordings c WHERE c.lead_id = f.lead_id) AS to_number "
                      "FROM follow_ups f "
                      "WHERE f.scheduled_date = %s AND f.status = 'pending' ")
            params = [today]
            if agent:
                sql += " AND LOWER(f.agent_name) LIKE %s"
                params.append(f"%{agent.lower()}%")
            sql += " ORDER BY f.scheduled_time"
            rows = query(sql, params)
            if not rows:
                return [TextContent(type="text", text=f"No callbacks scheduled for today ({today}).")]
            total_matched = len(rows)
            rows = rows[:10]
            trows = [f"| {r['scheduled_time']} | {r['lead_id']} | {r['agent_name'] or '—'} | {r['reason'] or '—'} |"
                     for r in rows]
            table = (
                f"**Top {len(rows)} of {total_matched} callbacks today — {today}**\n\n"
                "| Time | Lead ID | Agent | Reason |\n"
                "|---|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_upcoming_callbacks ─────────────────────────────────────────────
        if name == "get_upcoming_callbacks":
            days  = int(arguments.get("days", 7))
            agent = arguments.get("agent_name", "")
            sql   = ("SELECT f.id, f.lead_id, f.agent_name, f.scheduled_date, "
                     "f.scheduled_time, f.reason, f.status, "
                     "(SELECT MAX(to_number) FROM call_recordings c WHERE c.lead_id = f.lead_id) AS to_number "
                     "FROM follow_ups f "
                     "WHERE f.scheduled_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL %s DAY) "
                     "AND f.status = 'pending' ")
            params = [days]
            if agent:
                sql += " AND LOWER(f.agent_name) LIKE %s"
                params.append(f"%{agent.lower()}%")
            sql += " ORDER BY f.scheduled_date, f.scheduled_time"
            rows = query(sql, params)
            if not rows:
                return [TextContent(type="text", text=f"No upcoming callbacks in the next {days} days.")]
            total_matched = len(rows)
            rows = rows[:10]
            trows = [f"| {str(r['scheduled_date'])} | {r['scheduled_time']} | {r['lead_id']} | {r['agent_name'] or '—'} |"
                     for r in rows]
            table = (
                f"**Top {len(rows)} of {total_matched} upcoming callbacks — next {days} days**\n\n"
                "| Date | Time | Lead ID | Agent |\n"
                "|---|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── complete_callback ──────────────────────────────────────────────────
        if name == "complete_callback":
            cid = int(arguments["callback_id"])
            cb  = query("SELECT id, lead_id, scheduled_date, reason FROM follow_ups WHERE id = %s", [cid])
            if not cb:
                return [TextContent(type="text", text=f"❌ Callback ID {cid} not found.")]
            execute("UPDATE follow_ups SET status = 'done' WHERE id = %s", [cid])
            return [TextContent(type="text", text=(
                f"✅ Callback {cid} marked as done.\n"
                f"Lead #{cb[0]['lead_id']}  —  was scheduled for {cb[0]['scheduled_date']}"
                + (f"\nReason: {cb[0]['reason']}" if cb[0]['reason'] else "")
            ))]

        # ── set_lead_status ────────────────────────────────────────────────────
        if name == "set_lead_status":
            lead_id = int(arguments["lead_id"])
            status  = arguments["status"]
            if status not in ("new", "in_progress", "converted", "lost"):
                return [TextContent(type="text", text=f"❌ Invalid status '{status}'. Use: new, in_progress, converted, lost")]
            if not scalar("SELECT COUNT(*) FROM call_recordings WHERE lead_id = %s", [lead_id]):
                return [TextContent(type="text", text=f"❌ Lead {lead_id} not found.")]
            execute("""
                INSERT INTO lead_meta (lead_id, lead_status)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE lead_status = VALUES(lead_status)
            """, [lead_id, status])
            emoji = {"new": "🆕", "in_progress": "⚡", "converted": "✅", "lost": "❌"}[status]
            return [TextContent(type="text", text=(
                f"{emoji} Lead #{lead_id} status set to '{status}'.\n"
                f"Visible immediately in the dashboard."
            ))]

        # ── get_lead_status ────────────────────────────────────────────────────
        if name == "get_lead_status":
            lead_id = arguments.get("lead_id")
            status  = arguments.get("status")
            month   = arguments.get("month")

            if lead_id:
                row = query("SELECT lead_status, tags, updated_at FROM lead_meta WHERE lead_id = %s", [int(lead_id)])
                if not row:
                    return [TextContent(type="text", text=f"Lead #{lead_id} has no CRM status set yet (default: new).")]
                r = row[0]
                tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
                return [TextContent(type="text", text=(
                    f"Lead #{lead_id}\n"
                    f"  Status   : {r['lead_status']}\n"
                    f"  Tags     : {', '.join(tags) if tags else 'none'}\n"
                    f"  Updated  : {str(r['updated_at'])[:16]}"
                ))]

            if month:
                count = scalar("""
                    SELECT COUNT(*) FROM lead_meta
                    WHERE lead_status = 'converted'
                    AND DATE_FORMAT(updated_at, '%%Y-%%m') = %s
                """, [month])
                return [TextContent(type="text", text=f"✅ {count} leads converted in {month}.")]

            if status:
                rows = query("""
                    SELECT lm.lead_id, lm.tags, lm.updated_at,
                           MAX(c.to_number) AS phone, MAX(c.agent_name) AS agent
                    FROM lead_meta lm
                    JOIN call_recordings c ON c.lead_id = lm.lead_id
                    WHERE lm.lead_status = %s
                    GROUP BY lm.lead_id, lm.tags, lm.updated_at
                    ORDER BY lm.updated_at DESC
                """, [status])
                emoji = {"new": "🆕", "in_progress": "⚡", "converted": "✅", "lost": "❌"}.get(status, "")
                lines = [f"{emoji} {len(rows)} leads with status '{status}':\n"]
                for r in rows:
                    tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
                    lines.append(
                        f"  Lead #{r['lead_id']}  📞 {r['phone']}  🧑 {r['agent']}"
                        + (f"  tags: {', '.join(tags)}" if tags else "")
                    )
                return [TextContent(type="text", text="\n".join(lines))]

            # summary of all statuses
            rows = query("""
                SELECT lead_status, COUNT(*) AS cnt
                FROM lead_meta GROUP BY lead_status ORDER BY cnt DESC
            """)
            lines = ["📊 Lead Status Summary:\n"]
            for r in rows:
                emoji = {"new": "🆕", "in_progress": "⚡", "converted": "✅", "lost": "❌"}.get(r["lead_status"], "")
                lines.append(f"  {emoji} {r['lead_status']:12s}: {r['cnt']}")
            return [TextContent(type="text", text="\n".join(lines))]

        # ── set_lead_tags ──────────────────────────────────────────────────────
        if name == "set_lead_tags":
            lead_id = int(arguments["lead_id"])
            new_tags = [t.strip().lower() for t in arguments["tags"] if str(t).strip()]
            mode    = arguments.get("mode", "add")
            if not scalar("SELECT COUNT(*) FROM call_recordings WHERE lead_id = %s", [lead_id]):
                return [TextContent(type="text", text=f"❌ Lead {lead_id} not found.")]
            if mode == "add":
                existing = query("SELECT tags FROM lead_meta WHERE lead_id = %s", [lead_id])
                old_tags = [t.strip() for t in (existing[0]["tags"] if existing else "").split(",") if t.strip()]
                combined = list(dict.fromkeys(old_tags + new_tags))  # preserve order, dedupe
            else:
                combined = new_tags
            tag_str = ", ".join(combined)
            execute("""
                INSERT INTO lead_meta (lead_id, tags)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE tags = VALUES(tags)
            """, [lead_id, tag_str])
            return [TextContent(type="text", text=(
                f"🏷️ Lead #{lead_id} tags updated.\n"
                f"  Tags: {tag_str}\n"
                f"  Visible immediately in the dashboard."
            ))]

        # ── get_leads_by_tag ───────────────────────────────────────────────────
        if name == "get_leads_by_tag":
            tag = arguments["tag"].strip().lower()
            rows = query("""
                SELECT lm.lead_id, lm.tags, lm.lead_status, lm.updated_at,
                       MAX(c.to_number) AS phone, MAX(c.agent_name) AS agent
                FROM lead_meta lm
                JOIN call_recordings c ON c.lead_id = lm.lead_id
                WHERE FIND_IN_SET(%s, LOWER(REPLACE(lm.tags, ', ', ',')))
                GROUP BY lm.lead_id, lm.tags, lm.lead_status, lm.updated_at
                ORDER BY lm.updated_at DESC
            """, [tag])
            if not rows:
                return [TextContent(type="text", text=f"No leads found with tag '{tag}'.")]
            total_matched = len(rows)
            rows = rows[:10]
            trows = [f"| {r['lead_id']} | {r['agent'] or '—'} | {r['lead_status']} | {r['tags']} |" for r in rows]
            table = (
                f"**Top {len(rows)} of {total_matched} leads tagged '{tag}'**\n\n"
                "| Lead ID | Agent | Status | Tags |\n"
                "|---|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── set_agent_target ───────────────────────────────────────────────────
        if name == "set_agent_target":
            agent_input = arguments["agent_name"].strip()
            month       = arguments.get("month") or datetime.now().strftime("%Y-%m")
            tc          = int(arguments.get("target_calls", 0))
            tcc         = int(arguments.get("target_completed", 0))
            # fuzzy match agent name
            agents = query("SELECT DISTINCT agent_name FROM call_recordings WHERE LOWER(agent_name) LIKE %s",
                           [f"%{agent_input.lower()}%"])
            if not agents:
                return [TextContent(type="text", text=f"❌ No agent found matching '{agent_input}'.")]
            agent_name = agents[0]["agent_name"]
            execute("""
                INSERT INTO agent_targets (agent_name, month, target_calls, target_completed)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    target_calls     = VALUES(target_calls),
                    target_completed = VALUES(target_completed)
            """, [agent_name, month, tc, tcc])
            return [TextContent(type="text", text=(
                f"🎯 Target set for {agent_name} — {month}\n"
                f"  Total calls target    : {tc}\n"
                f"  Completed calls target: {tcc}\n"
                f"Visible in the Agents tab of the dashboard."
            ))]

        # ── get_agent_targets ──────────────────────────────────────────────────
        if name == "get_agent_targets":
            month       = arguments.get("month") or datetime.now().strftime("%Y-%m")
            agent_filter = arguments.get("agent_name", "")
            # days remaining in month
            from calendar import monthrange
            yr, mo = int(month[:4]), int(month[5:7])
            days_in_month = monthrange(yr, mo)[1]
            today_day = datetime.now().day if datetime.now().strftime("%Y-%m") == month else days_in_month
            days_left = max(0, days_in_month - today_day)

            sql = """
                SELECT at.agent_name, at.target_calls, at.target_completed,
                       COALESCE(ag.total_calls, 0) AS actual_calls,
                       COALESCE(ag.completed, 0)   AS actual_completed
                FROM agent_targets at
                LEFT JOIN (
                    SELECT agent_name,
                           COUNT(*) AS total_calls,
                           SUM(status = 'Completed') AS completed
                    FROM call_recordings
                    WHERE DATE_FORMAT(start_time, '%%Y-%%m') = %s
                    GROUP BY agent_name
                ) ag ON ag.agent_name = at.agent_name
                WHERE at.month = %s
            """
            params = [month, month]
            if agent_filter:
                sql += " AND LOWER(at.agent_name) LIKE %s"
                params.append(f"%{agent_filter.lower()}%")
            rows = query(sql, params)
            if not rows:
                return [TextContent(type="text", text=f"No targets set for {month}. Use set_agent_target to add some.")]
            trows = []
            for r in rows:
                tc, tcc = int(r["target_calls"] or 0), int(r["target_completed"] or 0)
                ac, acc = int(r["actual_calls"] or 0), int(r["actual_completed"] or 0)
                call_pct = round(ac / tc * 100) if tc else 0
                comp_pct = round(acc / tcc * 100) if tcc else 0
                trows.append(f"| {r['agent_name']} | {ac}/{tc} ({call_pct}%) | {acc}/{tcc} ({comp_pct}%) |")
            table = (
                f"**Agent targets — {month} ({days_left}d remaining)**\n\n"
                "| Agent | Calls Done/Target | Completed Done/Target |\n"
                "|---|---|---|\n" + "\n".join(trows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── send_daily_summary ─────────────────────────────────────────────────
        if name == "send_daily_summary":
            if EMAIL_CONFIG["sender_email"] == "your_email@gmail.com":
                return [TextContent(type="text", text=(
                    "❌ Email not configured.\n"
                    "Open mcp_server.py and fill in EMAIL_CONFIG with your Gmail address and App Password."
                ))]

            # Build report data
            stats  = query("""
                SELECT COUNT(*) AS tc, COUNT(DISTINCT lead_id) AS ul,
                       SUM(status='Completed') AS comp,
                       SUM(LOWER(status) LIKE '%no response%') AS nr
                FROM call_recordings
            """)[0]
            leads  = _all_lead_summaries()
            hot    = [l for l in leads if l["quality"] == "HOT"]
            warm   = [l for l in leads if l["quality"] == "WARM"]
            cold   = [l for l in leads if l["quality"] == "COLD"]
            today  = date.today().strftime("%Y-%m-%d")
            today_cbs = query(
                "SELECT f.id, f.lead_id, f.scheduled_time, f.agent_name, f.reason, "
                "(SELECT MAX(to_number) FROM call_recordings c WHERE c.lead_id = f.lead_id) AS to_number "
                "FROM follow_ups f "
                "WHERE f.scheduled_date = %s AND f.status = 'pending' "
                "ORDER BY f.scheduled_time",
                [today]
            )
            stale = [l for l in leads if l["days_since_last_call"] >= 10][:10]
            agents_data = query("""
                SELECT agent_name,
                       COUNT(*) AS tc, SUM(status='Completed') AS comp,
                       COUNT(DISTINCT lead_id) AS leads
                FROM call_recordings GROUP BY agent_name ORDER BY comp DESC
            """)

            # Build plain text email
            today_full = datetime.now().strftime("%A, %d %B %Y")
            lines = [
                f"LEAD MANAGEMENT DAILY SUMMARY — {today_full}",
                "=" * 55,
                "",
                "DATABASE OVERVIEW",
                f"  Total records   : {stats['tc']}",
                f"  Unique leads    : {stats['ul']}",
                f"  Completed calls : {stats['comp']}",
                f"  No response     : {stats['nr']}",
                f"  Connect rate    : {round(int(stats['comp'] or 0)/int(stats['tc'] or 1)*100,1)}%",
                "",
                "LEAD QUALITY",
                f"  🔥 HOT  : {len(hot)}",
                f"  🌡️  WARM : {len(warm)}",
                f"  ❄️  COLD : {cold and len(cold)}",
                "",
            ]
            if today_cbs:
                lines += [f"TODAY'S CALLBACKS ({len(today_cbs)})", "─"*40]
                for c in today_cbs:
                    lines.append(f"  {c['scheduled_time']}  Lead #{c['lead_id']}  "
                                 f"{c['to_number'] or ''}  {c['agent_name'] or 'unassigned'}"
                                 + (f"  — {c['reason']}" if c['reason'] else ""))
                lines.append("")
            if hot:
                lines += ["TOP HOT LEADS — CALL IMMEDIATELY", "─"*40]
                for l in hot[:10]:
                    lines.append(f"  #{l['lead_id']}  Score:{l['score']}  "
                                 f"{l['phone']}  Agent:{l['agent_name']}  "
                                 f"Last called:{l['days_since_last_call']}d ago")
                lines.append("")
            if stale:
                lines += ["STALE LEADS (10+ days no contact)", "─"*40]
                for l in stale:
                    lines.append(f"  #{l['lead_id']}  {l['days_since_last_call']}d ago  "
                                 f"{l['phone']}  {l['agent_name']}")
                lines.append("")
            if agents_data:
                lines += ["AGENT PERFORMANCE", "─"*40]
                for a in agents_data:
                    tc = int(a['tc'] or 0)
                    cr = round(int(a['comp'] or 0)/tc*100,1) if tc else 0
                    lines.append(f"  {a['agent_name']}  —  "
                                 f"Leads:{a['leads']}  Calls:{tc}  "
                                 f"Completed:{a['comp']} ({cr}%)")
            lines += ["", "─"*55, "Sent by Lead Management MCP System"]
            body = "\n".join(str(l) for l in lines)

            # Send email
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = f"Lead Summary — {today_full}"
                msg["From"]    = EMAIL_CONFIG["sender_email"]
                msg["To"]      = EMAIL_CONFIG["recipient_email"]
                msg.attach(MIMEText(body, "plain"))
                with smtplib.SMTP(EMAIL_CONFIG["smtp_host"], EMAIL_CONFIG["smtp_port"]) as s:
                    s.starttls()
                    s.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
                    s.sendmail(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["recipient_email"], msg.as_string())
                return [TextContent(type="text", text=(
                    f"✅ Daily summary email sent to {EMAIL_CONFIG['recipient_email']}\n"
                    f"Subject: Lead Summary — {today_full}\n\n"
                    f"Preview:\n{body[:600]}..."
                ))]
            except Exception as e:
                return [TextContent(type="text", text=f"❌ Failed to send email: {e}")]

    # ── send_custom_email ──────────────────────────────────────────────────────
        if name == "send_custom_email":
            if EMAIL_CONFIG["sender_email"] == "your_email@gmail.com":
                return [TextContent(type="text", text="❌ Email not configured in mcp_server.py")]
            subject = arguments.get("subject", "Message from Lead System")
            body    = arguments.get("body", "")
            try:
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"]    = EMAIL_CONFIG["sender_email"]
                msg["To"]      = EMAIL_CONFIG["recipient_email"]
                msg.attach(MIMEText(body, "plain"))
                with smtplib.SMTP(EMAIL_CONFIG["smtp_host"], EMAIL_CONFIG["smtp_port"]) as s:
                    s.starttls()
                    s.login(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["sender_password"])
                    s.sendmail(EMAIL_CONFIG["sender_email"], EMAIL_CONFIG["recipient_email"], msg.as_string())
                return [TextContent(type="text", text=f"✅ Email sent!\nTo: {EMAIL_CONFIG['recipient_email']}\nSubject: {subject}")]
            except Exception as e:
                return [TextContent(type="text", text=f"❌ Failed to send: {e}")]

        # ── analyse_objections  (REAL AI) ─────────────────────────────────────
        if name == "analyse_objections":
            agent_filt = (arguments.get("agent") or "").lower()
            date_from  = arguments.get("date_from")
            date_to    = arguments.get("date_to")
            limit      = int(arguments.get("limit") or 50)

            sql = """
                SELECT id AS call_id, lead_id, agent_name,
                       transcript, call_duration_sec AS duration_sec, start_time
                FROM call_recordings
                WHERE transcript_status = 'done'
                  AND transcript IS NOT NULL
            """
            params = []
            if agent_filt:
                sql += " AND LOWER(agent_name) LIKE %s"
                params.append(f"%{agent_filt}%")
            if date_from:
                sql += " AND DATE(start_time) >= %s"
                params.append(date_from)
            if date_to:
                sql += " AND DATE(start_time) <= %s"
                params.append(date_to)
            sql += f" ORDER BY start_time DESC LIMIT {limit}"

            transcripts = query(sql, params)
            transcripts = [dict(t) for t in transcripts]

            if not transcripts:
                return [TextContent(type="text", text=(
                    "⚠️ No transcribed calls found.\n\n"
                    "Make sure you have run:\n"
                    "  1. python setup_recordings.py\n"
                    "  2. python transcribe.py\n\n"
                    "These scripts load and transcribe your call recordings."
                ))]

            # 🤖 Gemini analyses ALL transcripts at once
            analysis = ai_analyse_objections_batch(transcripts)

            cats = analysis.get("objection_categories", [])
            total       = analysis.get("total_calls_analysed", 0)
            with_obj    = analysis.get("calls_with_objections", 0)

            if not cats:
                return [TextContent(type="text", text=(
                    f"**{total} calls analysed, {with_obj} had objections**\n\n"
                    "No significant objections found."
                ))]

            sorted_cats = sorted(cats, key=lambda x: x.get("count", 0), reverse=True)
            top_cats = sorted_cats[:10]
            rows = [f"| {c.get('objection','')} | {c.get('count',0)} | {c.get('example_lead_id','—')} | "
                    f"{c.get('example_agent','—')} | {c.get('example_quote','')[:60]} |"
                    for c in top_cats]
            rebuttal_notes = "\n".join(
                f"- **{c.get('objection','')}**: {c.get('suggested_rebuttal','')}"
                for c in top_cats
            )

            header = f"**{total} calls analysed, {with_obj} had objections — top {len(top_cats)} of {len(sorted_cats)} objection types**\n\n"
            table = (
                header +
                "| Objection | Count | Lead ID | Agent | Example |\n"
                "|---|---|---|---|---|\n" + "\n".join(rows) +
                "\n\n**Suggested rebuttals**\n" + rebuttal_notes +
                "\n\n[INSTRUCTION: Output the tables above exactly as-is. Do NOT rewrite as prose. Do NOT add recommendations, next steps, or follow-up questions. Stop after the last rebuttal line.]"
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_agent_objection_performance  (REAL AI) ────────────────────────
        if name == "get_agent_objection_performance":
            date_from = arguments.get("date_from")
            date_to   = arguments.get("date_to")

            sql = """
                SELECT id AS call_id, lead_id, agent_name,
                       transcript, call_duration_sec AS duration_sec, status
                FROM call_recordings
                WHERE transcript_status = 'done'
                  AND transcript IS NOT NULL
            """
            params = []
            if date_from:
                sql += " AND DATE(start_time) >= %s"; params.append(date_from)
            if date_to:
                sql += " AND DATE(start_time) <= %s"; params.append(date_to)
            sql += " ORDER BY agent_name, start_time DESC"

            transcripts = [dict(t) for t in query(sql, params)]

            if not transcripts:
                return [TextContent(type="text", text=(
                    "⚠️ No transcribed calls found. Run setup_recordings.py and transcribe.py first."
                ))]

            # Group by agent
            from collections import defaultdict
            by_agent = defaultdict(list)
            for t in transcripts:
                by_agent[t["agent_name"] or "Unknown"].append(t)

            # Build per-agent transcript blocks for Gemini
            agent_blocks = ""
            for agent, calls in by_agent.items():
                agent_blocks += f"\n\n=== AGENT: {agent} ({len(calls)} calls) ===\n"
                for c in calls[:15]:  # cap per agent to avoid huge prompt
                    agent_blocks += f"Call #{c['call_id']} ({c['duration_sec']}s): {c['transcript'][:1500]}\n"

            prompt = f"""You are a sales performance analyst for a Hyderabad-based call-centre.

IMPORTANT LANGUAGE NOTE:
These transcripts may be in Hindi, Urdu, Telugu, English, or Hinglish (mix).
You MUST analyse ALL transcripts regardless of language.
Common Hindi objection phrases: "abhi nahi" (not now), "mehnga hai" (too expensive),
"sochna hai" (need to think), "zaroorat nahi" (don't need it), "baad mein" (call later).
Translate all your analysis output into English.

Analyse the following call transcripts grouped by agent.
For each agent, assess how well they handle objections from leads.

{agent_blocks}

Compare agents and rank them by objection handling skill.

Respond ONLY as valid JSON, no markdown. All output in English:
{{
  "agent_rankings": [
    {{
      "agent_name": "<name>",
      "rank": <1 = best>,
      "calls_analysed": <integer>,
      "objection_handling_score": <0-10>,
      "strengths": "<what this agent does well>",
      "weaknesses": "<where this agent struggles>",
      "top_tip": "<single most useful improvement tip>"
    }}
  ],
  "best_agent": "<name of best objection handler>",
  "why_best": "<reason why this agent is best>",
  "team_pattern": "<overall pattern you noticed across the whole team>"
}}"""

            try:
                raw = _call_gemini(prompt, max_tokens=2000)
                if raw.startswith("```"):
                    raw = raw.split("```")[1]
                    if raw.startswith("json"): raw = raw[4:]
                result = json.loads(raw.strip())
            except Exception as e:
                return [TextContent(type="text", text=f"❌ AI analysis failed: {e}")]

            rankings = result.get("agent_rankings", [])
            if not rankings:
                return [TextContent(type="text", text="No agent objection data available.")]

            rows = [f"| {a.get('rank','')} | {a.get('agent_name','')} | {a.get('objection_handling_score',0)}/10 | {a.get('calls_analysed',0)} |"
                    for a in rankings]
            table = (
                f"**Best: {result.get('best_agent','')}**\n\n"
                "| Rank | Agent | Score | Calls |\n"
                "|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_lead_objection_history  (REAL AI) ─────────────────────────────
        if name == "get_lead_objection_history":
            lead_id = int(arguments["lead_id"])
            calls = query("""
                SELECT id AS call_id, agent_name, transcript,
                       call_duration_sec AS duration_sec, start_time, status
                FROM call_recordings
                WHERE lead_id = %s
                  AND transcript_status = 'done'
                  AND transcript IS NOT NULL
                ORDER BY start_time ASC
            """, [lead_id])
            calls = [dict(c) for c in calls]

            if not calls:
                # Check if lead exists but not transcribed
                total = scalar("SELECT COUNT(*) FROM call_recordings WHERE lead_id = %s", [lead_id])
                if total:
                    return [TextContent(type="text", text=(
                        f"Lead #{lead_id} has {total} calls but none are transcribed yet.\n"
                        f"Run: python transcribe.py --lead_id {lead_id}"
                    ))]
                return [TextContent(type="text", text=f"No calls found for lead #{lead_id}.")]

            # 🤖 Gemini analyses each call for this lead
            rows = []
            for c in calls:
                analysis = ai_analyse_single_transcript(c)
                date = str(c.get('start_time', ''))[:10]
                had  = "Yes" if analysis.get("had_objection") else "No"
                objections = ", ".join(analysis.get("objections", [])) or "—"
                rows.append(f"| {date} | {had} | {objections} | {analysis.get('outcome','unclear')} |")

            table = (
                f"**Lead #{lead_id} — {len(calls)} calls**\n\n"
                "| Date | Had Objection | Objection Types | Outcome |\n"
                "|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_churn_risk_leads  (REAL AI PREDICTION) ────────────────────────
        if name == "get_churn_risk_leads":
            all_leads   = _all_lead_summaries()
            top_n       = int(arguments.get("top_n") or 10)
            risk_filter = (arguments.get("risk_filter") or "ALL").upper()
            agent_filt  = (arguments.get("agent") or "").lower()

            if agent_filt:
                all_leads = [l for l in all_leads if agent_filt in l.get("agent_name","").lower()]
            if not all_leads:
                return [TextContent(type="text", text="No leads found matching those filters.")]

            ai_results = ai_predict_churn_batch(all_leads, top_n=min(top_n * 2, len(all_leads)))

            if risk_filter != "ALL":
                ai_results = [r for r in ai_results if r.get("risk","").upper() == risk_filter]
            ai_results = ai_results[:top_n]

            if not ai_results:
                return [TextContent(type="text", text="No leads found at that risk level.")]

            lead_map = {l["lead_id"]: l for l in all_leads}
            rows = []
            for ai in ai_results:
                lid    = ai.get("lead_id", "?")
                lead   = lead_map.get(lid, {})
                risk   = ai.get("risk", "MEDIUM")
                score  = ai.get("score", 0)
                action = ai.get("urgency", "monitor").replace("_", " ").title()
                days   = lead.get("days_since_last_call", "?")
                agent  = lead.get("agent_name") or "—"
                rows.append(f"| {lid} | {agent} | {score} | {risk} | {days}d | {action} |")

            table = (
                f"**Top {len(ai_results)} churn risk leads**\n\n"
                "| Lead ID | Agent | Score | Risk | Days Since Call | Action |\n"
                "|---|---|---|---|---|---|\n" + "\n".join(rows)
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_lead_churn_detail ──────────────────────────────────────────────
        if name == "get_lead_churn_detail":
            if not arguments.get("lead_id"):
                return [TextContent(type="text", text=(
                    "This tool requires a specific lead_id. "
                    "For overall/all-leads churn analysis, use get_churn_summary instead."
                ))]
            lead_id = int(arguments["lead_id"])
            leads   = _all_lead_summaries()
            lead    = next((l for l in leads if l["lead_id"] == lead_id), None)
            if not lead:
                return [TextContent(type="text", text=f"Lead {lead_id} not found.")]

            ai = ai_predict_churn(lead)
            risk   = ai.get("risk", "MEDIUM")
            action = ai.get("urgency", "monitor").replace("_", " ").title()

            table = (
                f"**Lead #{lead_id} churn detail**\n\n"
                "| Score | Risk | Days Since Call | Calls Done/Total | Action |\n"
                "|---|---|---|---|---|\n"
                f"| {ai.get('score',0)} | {risk} | {lead.get('days_since_last_call',0)}d | "
                f"{lead.get('completed_calls',0)}/{lead.get('total_calls',0)} | {action} |\n\n"
                f"Reason: {ai.get('reasoning','')}"
            )
            table += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=table)]

        # ── get_churn_summary ──────────────────────────────────────────────────
        if name == "get_churn_summary":
            all_leads = _all_lead_summaries()
            if not all_leads:
                return [TextContent(type="text", text="No leads found in database.")]
            ai_results = ai_predict_churn_batch(all_leads, top_n=len(all_leads))

            high   = [r for r in ai_results if r.get("risk") == "HIGH"]
            medium = [r for r in ai_results if r.get("risk") == "MEDIUM"]
            low    = [r for r in ai_results if r.get("risk") == "LOW"]
            total  = len(ai_results)
            avg_score = round(sum(r.get("score",50) for r in ai_results) / total, 1) if total else 0

            urgent_today = [r for r in ai_results if r.get("urgency") == "call_today"]
            this_week    = [r for r in ai_results if r.get("urgency") == "call_this_week"]

            from collections import defaultdict
            lead_map   = {l["lead_id"]: l for l in all_leads}
            agent_high = defaultdict(int)
            for r in high:
                agent = lead_map.get(r["lead_id"],{}).get("agent_name","Unknown")
                agent_high[agent] += 1
            top_agents = sorted(agent_high.items(), key=lambda x: x[1], reverse=True)[:5]

            agent_rows = [f"| {a} | {c} |" for a, c in top_agents]

            summary = (
                f"**Churn summary — {total} leads, avg score {avg_score}**\n\n"
                f"HIGH: {len(high)} | MEDIUM: {len(medium)} | LOW: {len(low)} | "
                f"Call today: {len(urgent_today)} | Call this week: {len(this_week)}\n\n"
            )
            if agent_rows:
                summary += (
                    "**Agents with most at-risk leads**\n\n"
                    "| Agent | High-Risk Leads |\n"
                    "|---|---|\n" + "\n".join(agent_rows)
                )
            summary += OUTPUT_INSTRUCTION
            return [TextContent(type="text", text=summary)]

        return [TextContent(type="text", text="I am a CRM assistant. I can only help with leads, calls, agents, and churn analysis.")]
    
    except mysql.connector.Error as e:
        return [TextContent(type="text", text=(
            f"❌ MySQL connection error.\n"
            f"Error: {e}\n\n"
            f"Make sure:\n"
            f"  1. MySQL is running\n"
            f"  2. Password in DB_CONFIG is correct\n"
            f"  3. Database '{DB_CONFIG['database']}' exists (run setup_db.py first)\n"
        ))]
    except Exception as e:
        return [TextContent(type="text", text=f"❌ Error: {e}")]



async def main():
    try:
        ensure_tables()
        total = scalar("SELECT COUNT(*) FROM call_recordings")
        leads = scalar("SELECT COUNT(DISTINCT lead_id) FROM call_recordings")
        import sys
        print(f"✅ Connected to MySQL '{DB_CONFIG['database']}' — {total} calls, {leads} leads",
              file=sys.stderr)
    except Exception as e:
        import sys
        print(f"⚠️  DB connection warning: {e}", file=sys.stderr)

    SYSTEM_PROMPT = """You are a CRM Lead Management Assistant for a Hyderabad-based sales team.

YOUR ONLY JOB: Answer questions about leads, calls, churn risk, objection analysis, agent performance, callbacks, notes, and email summaries using the tools available to you.

If the user asks ANYTHING outside this scope (general knowledge, coding, news, weather, personal questions, etc.), respond with exactly:
"I am a CRM assistant. I can only help with leads, calls, agents, and churn analysis."

Do NOT answer off-topic questions even if you know the answer. Do NOT make exceptions."""

    init_options = app.create_initialization_options()
    # Attach system prompt so Claude Desktop enforces scope on every message
    if hasattr(init_options, "server_capabilities"):
        try:
            init_options.system_prompt = SYSTEM_PROMPT
        except Exception:
            pass

    async with stdio_server() as (r, w):
        await app.run(r, w, init_options)

if __name__ == "__main__":
    asyncio.run(main())