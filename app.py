"""
app.py  —  FastAPI backend for the Lead Management Dashboard
─────────────────────────────────────────────────────────────
Connects DIRECTLY to the same MySQL database as mcp_server.py.
Same leads_db, same tables: calls, notes, follow_ups, lead_meta, agent_targets.

Run:
    pip install fastapi uvicorn mysql-connector-python anthropic
    python app.py

Opens at: http://localhost:8000
Dashboard: http://localhost:8000
"""

import os, math, json
from datetime import datetime, date
from calendar import monthrange
from contextlib import asynccontextmanager
from typing import Optional, List

import mysql.connector
from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
else:
    raise RuntimeError("GEMINI_API_KEY is missing from .env")

# MySQL Configuration
DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}
# ══════════════════════════════════════════════════════════════════════════════

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def query(sql: str, params=None) -> list[dict]:
    conn = get_conn()
    cur  = conn.cursor(dictionary=True)
    cur.execute(sql, params or [])
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows

def scalar(sql: str, params=None):
    rows = query(sql, params)
    if not rows: return None
    return list(rows[0].values())[0]

def execute(sql: str, params=None) -> int:
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute(sql, params or [])
    conn.commit()
    lid = cur.lastrowid
    cur.close(); conn.close()
    return lid

def ensure_tables():
    """Create all required tables if not exists."""
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            lead_id    BIGINT NOT NULL,
            agent_name VARCHAR(200),
            note_text  TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_notes_lead (lead_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            lead_id        BIGINT NOT NULL,
            agent_name     VARCHAR(200),
            scheduled_date DATE NOT NULL,
            scheduled_time VARCHAR(10) DEFAULT '09:00',
            reason         TEXT,
            status         VARCHAR(30) DEFAULT 'pending',
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_fu_lead   (lead_id),
            INDEX idx_fu_date   (scheduled_date),
            INDEX idx_fu_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS lead_meta (
            lead_id     BIGINT PRIMARY KEY,
            lead_status VARCHAR(30) DEFAULT 'new',
            tags        TEXT,
            updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_lm_status (lead_status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
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
    cur.close(); conn.close()

# ── Scoring (identical to mcp_server.py) ─────────────────────────────────────

_dataset_age_cache = None
def _dataset_age_days() -> int:
    """Days between the most recent call in DB and today. Normalises recency for imported datasets."""
    global _dataset_age_cache
    if _dataset_age_cache is None:
        try:
            latest = scalar("SELECT MAX(start_time) FROM calls")
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
    """Compute a 0-100 score. Identical logic to mcp_server.py.

    Scoring (100 pts total):
      connect_rate    40 — (completed + answered) / total calls
      call_volume     20 — raw call count, capped at 20
      avg_duration    15 — average call duration in seconds
      recency         15 — days since last call, relative to dataset's newest call
      completed_bonus 10 — absolute completed call count, capped at 10
      no_resp_penalty -10 max — proportion of no-response calls
    """
    score, breakdown = 0, []

    def add(param, earned, max_pts):
        nonlocal score
        score += earned
        breakdown.append({"param": param, "earned": earned, "max": max_pts,
                          "pct": round(earned / max_pts * 100) if max_pts else 0})

    tc   = int(lead.get("total_calls", 0) or 0)
    comp = int(lead.get("completed_calls", 0) or 0)
    ans  = int(lead.get("answered_calls", 0) or 0)
    nr   = int(lead.get("no_response_calls", 0) or 0)
    avg  = float(lead.get("avg_duration_sec", 0) or 0)
    days = int(lead.get("days_since_last_call", 999) or 999)

    # 1. Connect rate — 40 pts
    connect_rate = (comp + ans) / tc if tc else 0
    add("connect_rate",     min(round(connect_rate * 40), 40), 40)

    # 2. Call volume — 20 pts
    add("call_volume",      min(tc, 20), 20)

    # 3. Avg duration — 15 pts
    add("avg_duration",     15 if avg >= 120 else 10 if avg >= 60 else 5 if avg >= 20 else 0, 15)

    # 4. Recency relative to dataset — 15 pts
    relative_days = max(0, days - _dataset_age_days())
    add("recency",          15 if relative_days <= 3 else 10 if relative_days <= 7 else 5 if relative_days <= 14 else 0, 15)

    # 5. Completed count bonus — 10 pts
    add("completed_bonus",  min(comp, 10), 10)

    # 6. No-response penalty — up to -10
    nr_rate = nr / tc if tc else 0
    add("no_resp_penalty",  round(max(-10, -nr_rate * 10)), 0)

    score = max(0, min(score, 100))
    if   score >= 80: q, emoji = "HOT",  "🔥"
    elif score >= 50: q, emoji = "WARM", "🌡️"
    else:             q, emoji = "COLD", "❄️"
    lead.update({"score": score, "quality": q, "emoji": emoji, "breakdown": breakdown})
    return lead

def calculate_churn_score(lead: dict):
    score = 0

    # Inactivity
    days = lead.get("days_since_last_call", 0)

    if days >= 30:
        score += 40
    elif days >= 20:
        score += 30
    elif days >= 15:
        score += 20

    # No response behavior
    no_resp = lead.get("no_response_calls", 0)
    score += min(no_resp * 3, 25)

    # Lack of completed calls
    completed = lead.get("completed_calls", 0)

    if completed == 0:
        score += 20
    elif completed <= 2:
        score += 10

    # Short conversations
    avg_duration = lead.get("avg_duration_sec", 0)

    if avg_duration < 20:
        score += 15
    elif avg_duration < 60:
        score += 8

    score = min(score, 100)

    if score >= 70:
        risk = "HIGH"
    elif score >= 40:
        risk = "MEDIUM"
    else:
        risk = "LOW"

    return score, risk

def _all_lead_summaries() -> list[dict]:
    rows = query("""
        SELECT
            c.lead_id,
            MAX(c.to_number)                                   AS phone,
            MAX(c.agent_name)                                  AS agent_name,
            COUNT(*)                                           AS total_calls,
            SUM(c.status = 'Completed')                        AS completed_calls,
            SUM(c.status = 'ANSWERED')                         AS answered_calls,
            SUM(c.status = 'No response')                      AS no_response_calls,
            SUM(c.status = 'CANCEL')                           AS cancelled_calls,
            SUM(c.duration_sec)  AS total_duration_sec,
            AVG(NULLIF(c.duration_sec, 0)) AS avg_duration_sec,
            MIN(c.start_time)                                  AS first_call_date,
            MAX(c.start_time)                                  AS last_call_date,
            DATEDIFF(NOW(), MAX(c.start_time))                 AS days_since_last_call,
            COALESCE(lm.lead_status, 'new')                    AS lead_status,
            COALESCE(lm.tags, '')                              AS tags_raw
        FROM calls c
        LEFT JOIN lead_meta lm ON lm.lead_id = c.lead_id
        GROUP BY c.lead_id
    """)
    leads = []
    for r in rows:
        lead = {
            "lead_id":              int(r["lead_id"] or 0),
            "phone":                str(r["phone"] or ""),
            "agent_name":           str(r["agent_name"] or ""),
            "total_calls":          int(r["total_calls"] or 0),
            "completed_calls":      int(r["completed_calls"] or 0),
            "answered_calls":       int(r["answered_calls"] or 0),
            "no_response_calls":    int(r["no_response_calls"] or 0),
            "cancelled_calls":      int(r["cancelled_calls"] or 0),
            "total_duration_sec":   int(r["total_duration_sec"] or 0),
            "avg_duration_sec":     round(float(r["avg_duration_sec"] or 0), 1),
            "first_call_date":      str(r["first_call_date"] or "")[:10],
            "last_call_date":       str(r["last_call_date"] or "")[:10],
            "days_since_last_call": int(r["days_since_last_call"] or 0),
            "lead_status":          r["lead_status"],
            "tags":                 [t.strip() for t in (r["tags_raw"] or "").split(",") if t.strip()],
        }
        
        lead = score_lead(lead)

        churn_score, churn_risk = calculate_churn_score(lead)

        lead["churn_score"] = churn_score
        lead["churn_risk"] = churn_risk

        leads.append(lead)

    return leads

# ── FastAPI app ───────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_tables()
    print("✅ Tables verified. Dashboard ready at http://localhost:8000")
    yield

app = FastAPI(lifespan=lifespan, title="Lead Management Dashboard API")

# ── Serve dashboard HTML ──────────────────────────────────────────────────────

DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "index.html")

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    if not os.path.exists(DASHBOARD_PATH):
        return HTMLResponse("<h1>index.html not found — put it in the same folder as app.py</h1>", status_code=404)
    with open(DASHBOARD_PATH, encoding="utf-8") as f:
        return HTMLResponse(f.read())

# ── /api/health ───────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    try:
        records = scalar("SELECT COUNT(*) FROM calls")
        return {"status": "ok", "records": records}
    except Exception as e:
        raise HTTPException(status_code=503, detail=str(e))

# ── /api/stats ────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    r = query("""
        SELECT COUNT(*) AS tc, COUNT(DISTINCT lead_id) AS ul,
               SUM(status='Completed') AS comp,
               SUM(status='No response') AS nr,
               SUM(duration_sec) AS total_dur
        FROM calls
    """)[0]
    tc   = int(r["tc"] or 0)
    comp = int(r["comp"] or 0)

    leads     = _all_lead_summaries()
    hot       = sum(1 for l in leads if l["quality"] == "HOT")
    warm      = sum(1 for l in leads if l["quality"] == "WARM")
    cold      = sum(1 for l in leads if l["quality"] == "COLD")
    converted = sum(1 for l in leads if l["lead_status"] == "converted")

    today_cbs = scalar("SELECT COUNT(*) FROM follow_ups WHERE scheduled_date=CURDATE() AND status='pending'") or 0
    notes_cnt = scalar("SELECT COUNT(*) FROM notes") or 0
    total_min = round(int(r["total_dur"] or 0) / 60)

    return {
        "total_records":    tc,
        "unique_leads":     int(r["ul"] or 0),
        "completed":        comp,
        "no_response":      int(r["nr"] or 0),
        "connect_rate":     round(comp / tc * 100, 1) if tc else 0,
        "hot_leads":        hot,
        "warm_leads":       warm,
        "cold_leads":       cold,
        "converted":        converted,
        "total_talk_min":   total_min,
        "total_notes":      notes_cnt,
        "todays_callbacks": today_cbs,
    }

# ── /api/leads ────────────────────────────────────────────────────────────────

@app.get("/api/leads")
async def get_leads(
    page:       int = Query(1, ge=1),
    per_page:   int = Query(50, ge=1, le=500),
    sort_by:    str = Query("score"),
    order:      str = Query("desc"),
    search:     str = Query(""),
    quality:    str = Query(""),
    agent:      str = Query(""),
    lead_status: str = Query(""),
):
    leads = _all_lead_summaries()

    # filters
    if search:
        s = search.lower()
        leads = [l for l in leads if s in str(l["lead_id"]) or s in l["phone"].lower() or s in l["agent_name"].lower()]
    if quality:
        leads = [l for l in leads if l["quality"] == quality.upper()]
    if agent:
        leads = [l for l in leads if agent.lower() in l["agent_name"].lower()]
    if lead_status:
        leads = [l for l in leads if l["lead_status"] == lead_status]

    # sort
    valid_sorts = {"score", "total_calls", "completed_calls", "days_since_last_call", "avg_duration_sec"}
    sf = sort_by if sort_by in valid_sorts else "score"
    leads.sort(key=lambda l: l.get(sf, 0), reverse=(order == "desc"))

    total = len(leads)
    total_pages = max(1, math.ceil(total / per_page))
    start = (page - 1) * per_page
    page_leads = leads[start: start + per_page]

    return {"leads": page_leads, "total": total, "page": page, "total_pages": total_pages, "per_page": per_page}

# ── /api/lead/{id} ────────────────────────────────────────────────────────────

@app.get("/api/lead/{lead_id}")
async def get_lead(lead_id: int):
    leads = _all_lead_summaries()
    lead = next((l for l in leads if l["lead_id"] == lead_id), None)
    if not lead:
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")

    calls = query("""
        SELECT id, status, call_duration, duration_sec, start_time, end_time,
               agent_name, to_number, from_number, call_type, hangup_by, remarks
        FROM calls WHERE lead_id=%s ORDER BY start_time ASC
    """, [lead_id])
    for c in calls:
        c["start_time"] = str(c["start_time"] or "")
        c["end_time"]   = str(c["end_time"] or "")

    notes = query("""
        SELECT id, agent_name, note_text, created_at
        FROM notes WHERE lead_id=%s ORDER BY created_at DESC
    """, [lead_id])
    for n in notes:
        n["created_at"] = str(n["created_at"] or "")

    callbacks = query("""
        SELECT id, scheduled_date, scheduled_time, reason, status
        FROM follow_ups WHERE lead_id=%s ORDER BY scheduled_date, scheduled_time
    """, [lead_id])
    for cb in callbacks:
        cb["scheduled_date"] = str(cb["scheduled_date"] or "")

    lead["calls"]     = calls
    lead["notes"]     = notes
    lead["callbacks"] = callbacks
    return lead

# ── /api/lead/{id}/meta  (PATCH) ──────────────────────────────────────────────

class LeadMetaPatch(BaseModel):
    lead_status: Optional[str] = None
    tags: Optional[List[str]] = None

@app.patch("/api/lead/{lead_id}/meta")
async def patch_lead_meta(lead_id: int, body: LeadMetaPatch):
    if not scalar("SELECT COUNT(*) FROM calls WHERE lead_id=%s", [lead_id]):
        raise HTTPException(status_code=404, detail=f"Lead {lead_id} not found")

    updates, params = [], []
    if body.lead_status is not None:
        valid = {"new", "in_progress", "converted", "lost"}
        if body.lead_status not in valid:
            raise HTTPException(status_code=400, detail=f"status must be one of {valid}")
        updates.append("lead_status = %s"); params.append(body.lead_status)
    if body.tags is not None:
        tag_str = ", ".join(t.strip().lower() for t in body.tags if t.strip())
        updates.append("tags = %s"); params.append(tag_str)

    if not updates:
        raise HTTPException(status_code=400, detail="Nothing to update")

    # Ensure row exists, then update
    execute("INSERT INTO lead_meta (lead_id) VALUES (%s) ON DUPLICATE KEY UPDATE lead_id=lead_id", [lead_id])
    params.append(lead_id)
    execute(f"UPDATE lead_meta SET {', '.join(updates)} WHERE lead_id = %s", params)

    return {"ok": True, "lead_id": lead_id}

# ── /api/agents ───────────────────────────────────────────────────────────────

@app.get("/api/agents")
async def get_agents():
    month = datetime.now().strftime("%Y-%m")
    rows = query("""
        SELECT
            c.agent_name                                       AS name,
            COUNT(*)                                           AS total_calls,
            COUNT(DISTINCT c.lead_id)                          AS unique_leads,
            SUM(c.status = 'Completed')                        AS completed,
            SUM(c.status = 'No response')                      AS no_response,
            SUM(c.status = 'ANSWERED')                         AS answered,
            SUM(c.duration_sec)   AS total_dur,
            AVG(NULLIF(c.duration_sec, 0)) AS avg_dur
        FROM calls c
        GROUP BY c.agent_name
        ORDER BY completed DESC
    """)

    # targets for this month
    targets = query("SELECT agent_name, target_calls, target_completed FROM agent_targets WHERE month=%s", [month])
    tmap = {t["agent_name"]: t for t in targets}

    # lead quality per agent (from all lead summaries)
    leads = _all_lead_summaries()

    agents = []
    for r in rows:
        name     = r["name"]
        tc       = int(r["total_calls"] or 0)
        comp     = int(r["completed"] or 0)
        cr       = round(comp / tc * 100, 1) if tc else 0
        ag_leads = [l for l in leads if l["agent_name"] == name]
        t        = tmap.get(name, {})

        tc_tgt  = int(t.get("target_calls", 0) or 0)
        tcc_tgt = int(t.get("target_completed", 0) or 0)

        agents.append({
            "name":           name,
            "total_calls":    tc,
            "unique_leads":   int(r["unique_leads"] or 0),
            "completed":      comp,
            "no_response":    int(r["no_response"] or 0),
            "answered":       int(r["answered"] or 0),
            "connect_rate":   cr,
            "total_dur":      int(r["total_dur"] or 0),
            "avg_dur":        round(float(r["avg_dur"] or 0), 1),
            "hot_leads":      sum(1 for l in ag_leads if l["quality"] == "HOT"),
            "warm_leads":     sum(1 for l in ag_leads if l["quality"] == "WARM"),
            "cold_leads":     sum(1 for l in ag_leads if l["quality"] == "COLD"),
            "converted":      sum(1 for l in ag_leads if l["lead_status"] == "converted"),
            "avg_score":      round(sum(l["score"] for l in ag_leads) / len(ag_leads), 1) if ag_leads else 0,
            "target_calls":     tc_tgt or None,
            "target_completed": tcc_tgt or None,
            "calls_pct":  round(tc   / tc_tgt  * 100) if tc_tgt  else None,
            "comp_pct":   round(comp / tcc_tgt * 100) if tcc_tgt else None,
        })
    return {"agents": agents}

# ── /api/notes ────────────────────────────────────────────────────────────────

class NoteCreate(BaseModel):
    lead_id:    int
    note_text:  str
    agent_name: Optional[str] = None

@app.post("/api/notes")
async def create_note(body: NoteCreate):
    if not scalar("SELECT COUNT(*) FROM calls WHERE lead_id=%s", [body.lead_id]):
        raise HTTPException(status_code=404, detail=f"Lead {body.lead_id} not found")
    nid = execute(
        "INSERT INTO notes (lead_id, agent_name, note_text) VALUES (%s,%s,%s)",
        [body.lead_id, body.agent_name, body.note_text.strip()]
    )
    return {"ok": True, "id": nid}

@app.delete("/api/notes/{note_id}")
async def delete_note(note_id: int):
    if not scalar("SELECT COUNT(*) FROM notes WHERE id=%s", [note_id]):
        raise HTTPException(status_code=404, detail="Note not found")
    execute("DELETE FROM notes WHERE id=%s", [note_id])
    return {"ok": True}

# ── /api/callbacks ────────────────────────────────────────────────────────────

class CallbackCreate(BaseModel):
    lead_id:        int
    scheduled_date: str
    scheduled_time: Optional[str] = "09:00"
    reason:         Optional[str] = None
    agent_name:     Optional[str] = None

@app.post("/api/callbacks")
async def create_callback(body: CallbackCreate):
    if not scalar("SELECT COUNT(*) FROM calls WHERE lead_id=%s", [body.lead_id]):
        raise HTTPException(status_code=404, detail=f"Lead {body.lead_id} not found")
    cid = execute(
        "INSERT INTO follow_ups (lead_id, agent_name, scheduled_date, scheduled_time, reason) VALUES (%s,%s,%s,%s,%s)",
        [body.lead_id, body.agent_name, body.scheduled_date, body.scheduled_time or "09:00", body.reason]
    )
    return {"ok": True, "id": cid}

@app.patch("/api/callbacks/{cb_id}/done")
async def complete_callback(cb_id: int):
    if not scalar("SELECT COUNT(*) FROM follow_ups WHERE id=%s", [cb_id]):
        raise HTTPException(status_code=404, detail="Callback not found")
    execute("UPDATE follow_ups SET status='done' WHERE id=%s", [cb_id])
    return {"ok": True}

# ── /api/targets ──────────────────────────────────────────────────────────────

class TargetCreate(BaseModel):
    agent_name:       str
    month:            str
    target_calls:     int = 0
    target_completed: int = 0

@app.get("/api/targets")
async def get_targets(month: str = Query(...)):
    rows = query("SELECT agent_name, target_calls, target_completed FROM agent_targets WHERE month=%s", [month])
    return {"targets": rows, "month": month}

@app.post("/api/targets")
async def set_target(body: TargetCreate):
    execute("""
        INSERT INTO agent_targets (agent_name, month, target_calls, target_completed)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            target_calls     = VALUES(target_calls),
            target_completed = VALUES(target_completed)
    """, [body.agent_name, body.month, body.target_calls, body.target_completed])
    return {"ok": True}

@app.get("/api/high-risk-leads")
async def get_high_risk_leads():
    leads = _all_lead_summaries()

    risky = [
        l for l in leads
        if l["churn_risk"] == "HIGH"
    ]

    risky.sort(
        key=lambda x: x["churn_score"],
        reverse=True
    )

    return risky[:20]

def create_churn_followups(time_str="09:00"):
    leads = _all_lead_summaries()

    high_risk = [
        l for l in leads
        if l["churn_risk"] == "HIGH"
    ]

    created = 0
    skipped = 0

    for lead in high_risk:

        #existing = scalar(
        #    """
        #    SELECT COUNT(*)
        #    FROM follow_ups
        #    WHERE lead_id = %s
         #   AND status = 'pending'
          #  """,
           # [lead["lead_id"]]
        #)

        #if existing:
         #   print(f"Skipping lead {lead['lead_id']} - existing pending followup found")
          #  skipped += 1
           # continue

        execute(
            """
            INSERT INTO follow_ups
            (

                lead_id,
                agent_name,
                scheduled_date,
                scheduled_time,
                reason
            )
            VALUES (%s,%s,DATE_ADD(CURDATE(), INTERVAL 1 DAY),%s,%s)
            """,
            [
                lead["lead_id"],
                lead["agent_name"],
                time_str,
                f"Auto-created by Churn Agent. Churn Score: {lead['churn_score']}"
            ]
        )

        created += 1

    return {
        "count": created,
        "skipped": skipped,
        "leads": len(high_risk)
    }
# ── /api/ai-analyze ───────────────────────────────────────────────────────────

class AiRequest(BaseModel):
    question: str

@app.post("/api/ai-analyze")
async def ai_analyze(body: AiRequest):
    # Give Claude a snapshot of the database to answer from
    try:
        question = body.question.lower()

        if "create followup" in question or "create followups" in question:
           
           followup_time = "09:00"

           if "3 pm" in question:
             followup_time = "15:00"
           elif "4 pm" in question:
             followup_time = "16:00"
           elif "5 pm" in question:
             followup_time = "17:00"

           result = create_churn_followups(followup_time)

           return {
            "answer": (
            f"✅ Churn Agent Action Completed\n\n"
            f"Created {result['count']} followups\n"
            f"Skipped {result['skipped']} existing followups\n"
            f"Scheduled Time: {followup_time}"
            )
}


        stats_r = query("""
            SELECT COUNT(*) AS tc, COUNT(DISTINCT lead_id) AS ul,
                   COUNT(DISTINCT agent_name) AS agents,
                   SUM(status='Completed') AS comp,
                   SUM(LOWER(status) LIKE '%no response%') AS nr
            FROM calls
        """)[0]
        leads   = _all_lead_summaries()
        hot     = [l for l in leads if l["quality"] == "HOT"]
        warm    = [l for l in leads if l["quality"] == "WARM"]
        cold    = [l for l in leads if l["quality"] == "COLD"]
        converted_leads = [l for l in leads if l["lead_status"] == "converted"]
        month   = datetime.now().strftime("%Y-%m")
        targets = query("SELECT * FROM agent_targets WHERE month=%s", [month])

        agents_perf = query("""
            SELECT agent_name, COUNT(*) AS tc, SUM(status='Completed') AS comp,
                   COUNT(DISTINCT lead_id) AS leads
            FROM calls GROUP BY agent_name ORDER BY comp DESC
        """)

        high_risk = [
           l for l in leads
           if l["churn_risk"] == "HIGH"
        ]

        high_risk = sorted(
           high_risk,
           key=lambda x: x["churn_score"],
           reverse=True
        ) 
        
        context = f"""You are an AI analyst for a lead management CRM. Here is the current database snapshot:

OVERALL STATS (as of {datetime.now().strftime('%Y-%m-%d %H:%M')}):
- Total call records: {stats_r['tc']}
- Unique leads: {stats_r['ul']}
- Agents: {stats_r['agents']}
- Completed calls: {stats_r['comp']}
- No response: {stats_r['nr']}
- Connect rate: {round(int(stats_r['comp'] or 0)/int(stats_r['tc'] or 1)*100,1)}%

LEAD QUALITY:
- 🔥 HOT (score ≥80): {len(hot)} leads
- 🌡️ WARM (50-79): {len(warm)} leads
- ❄️ COLD (<50): {len(cold)} leads

CRM STATUS:
- Converted: {len(converted_leads)} leads
- In progress: {sum(1 for l in leads if l['lead_status']=='in_progress')}
- Lost: {sum(1 for l in leads if l['lead_status']=='lost')}
- New: {sum(1 for l in leads if l['lead_status']=='new')}

TOP HOT LEADS (top 10):
{chr(10).join(f"  Lead #{l['lead_id']} | Score:{l['score']} | {l['phone']} | Agent:{l['agent_name']} | Status:{l['lead_status']} | Tags:{','.join(l['tags']) or 'none'}" for l in sorted(hot, key=lambda x: x['score'], reverse=True)[:10])}

AGENT PERFORMANCE:
{chr(10).join(f"  {a['agent_name']}: {a['tc']} calls, {a['comp']} completed, {a['leads']} leads" for a in agents_perf)}

MONTHLY TARGETS ({month}):
{chr(10).join(f"  {t['agent_name']}: target_calls={t['target_calls']}, target_completed={t['target_completed']}" for t in targets) or '  No targets set this month'}

STALE LEADS (15+ days no contact, top 10):
{chr(10).join(f"  Lead #{l['lead_id']} | {l['days_since_last_call']}d ago | {l['phone']} | Agent:{l['agent_name']}" for l in sorted(leads, key=lambda x: x['days_since_last_call'], reverse=True)[:10] if l['days_since_last_call'] >= 15)}

TOP CHURN RISK LEADS:
{chr(10).join(
    f"  Lead #{l['lead_id']} | Churn:{l['churn_score']} | Risk:{l['churn_risk']} | Days:{l['days_since_last_call']} | NoResp:{l['no_response_calls']} | Agent:{l['agent_name']}"
    for l in high_risk[:10]
)}

CHURN SIGNALS:

For each lead consider:
- days_since_last_call
- completed_calls
- answered_calls
- no_response_calls
- avg_duration_sec
- score

A lead with:
- high inactivity
- low completed calls
- high no response calls
- low connect rate
is more likely to churn.
Answer the user's question based on this data.

For churn prediction:

A lead is considered at churn risk when:
- Days since last call > 15
- High no-response rate
- Very few completed calls
- Low connect rate
- Long inactivity

When asked about churn:
1. Identify risky leads.
2. Explain why they are risky.
3. Rank them from highest to lowest risk.
4. Recommend actions.

Be concise, specific, and actionable.
Use lead IDs, scores, and metrics whenever possible."""
        model = genai.GenerativeModel("gemini-2.5-flash")

        response = model.generate_content(
            f"""
{context}

User Question:
{body.question}
"""
        )

        return {"answer": response.text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)