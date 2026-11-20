#!/usr/bin/env python3
"""
daily_email.py — Standalone Daily Summary Email Script
Run this manually or set it up in Windows Task Scheduler to run every morning.

Usage:
    python daily_email.py

Setup in Windows Task Scheduler:
    1. Open Task Scheduler (search it in Start menu)
    2. Click "Create Basic Task"
    3. Name: Lead Daily Email
    4. Trigger: Daily, set your time (e.g. 9:00 AM)
    5. Action: Start a program
    6. Program: python
    7. Arguments: C:\\full\\path\\to\\daily_email.py
    8. Click Finish
"""

import os
import smtplib, sys
from datetime import datetime, date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv

load_dotenv()

try:
    import mysql.connector
except ImportError:
    raise SystemExit("Run: pip install mysql-connector-python")


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
# ══════════════════════════════════════════════════════════════════════════════

def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def ensure_tables():
    conn = get_conn()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id              INT AUTO_INCREMENT PRIMARY KEY,
            lead_id         BIGINT NOT NULL,
            agent_name      VARCHAR(200),
            scheduled_date  DATE NOT NULL,
            scheduled_time  VARCHAR(10) DEFAULT '09:00',
            reason          TEXT,
            status          VARCHAR(30) DEFAULT 'pending',
            created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            lead_id     BIGINT NOT NULL,
            agent_name  VARCHAR(200),
            note_text   TEXT NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    cur.close()
    conn.close()

def query(sql, params=None):
    conn = get_conn()
    cur  = conn.cursor(dictionary=True)
    cur.execute(sql, params or [])
    rows = cur.fetchall()
    cur.close(); conn.close()
    return [dict(r) for r in rows]

def fmt_sec(sec):
    if not sec: return "0s"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:   return f"{h}h {m}m {s}s"
    elif m: return f"{m}m {s}s"
    return f"{s}s"

def score(row):
    s = 0
    cc = int(row.get("completed_calls") or 0)
    ad = float(row.get("avg_duration_sec") or 0)
    ds = int(row.get("days_since_last_call") or 999)
    tc = int(row.get("total_calls") or 0)
    if cc>=3: s+=35
    elif cc>=2: s+=25
    elif cc>=1: s+=15
    if ad>=60: s+=25
    elif ad>=15: s+=15
    elif ad>=1: s+=8
    if ds<=15: s+=25
    elif ds<=17: s+=18
    elif ds<=20: s+=10
    else: s+=3
    if tc>=5: s+=15
    elif tc>=3: s+=10
    elif tc>=2: s+=5
    else: s+=2
    return min(s, 100)

def build_report():
    today      = date.today().strftime("%Y-%m-%d")
    today_full = datetime.now().strftime("%A, %d %B %Y")

    # Overall stats
    stats = query("""
        SELECT COUNT(*) AS tc, COUNT(DISTINCT lead_id) AS ul,
               SUM(status='Completed') AS comp,
               SUM(LOWER(status) LIKE '%no response%') AS nr,
               SUM(duration_sec) AS total_dur
        FROM calls
    """)[0]

    # Lead summaries
    leads_raw = query("""
        SELECT lead_id,
               COUNT(*) AS total_calls,
               SUM(status='Completed') AS completed_calls,
               SUM(LOWER(status) LIKE '%no response%') AS no_response_calls,
               AVG(duration_sec) AS avg_duration_sec,
               SUM(duration_sec) AS total_duration_sec,
               DATEDIFF(NOW(), MAX(start_time)) AS days_since_last_call,
               MAX(agent_name) AS agent_name,
               MAX(to_number)  AS phone
        FROM calls GROUP BY lead_id
    """)
    for l in leads_raw:
        l["score"]   = score(l)
        l["quality"] = "HOT" if l["score"]>=80 else "WARM" if l["score"]>=50 else "COLD"

    hot   = sorted([l for l in leads_raw if l["quality"]=="HOT"],  key=lambda x: x["score"], reverse=True)
    warm  = sorted([l for l in leads_raw if l["quality"]=="WARM"], key=lambda x: x["score"], reverse=True)
    cold  = [l for l in leads_raw if l["quality"]=="COLD"]
    stale = sorted([l for l in leads_raw if int(l.get("days_since_last_call") or 0)>=10],
                   key=lambda x: x["days_since_last_call"], reverse=True)[:10]

    # Today's callbacks
    today_cbs = query(
    "SELECT f.id, f.lead_id, f.scheduled_time, f.agent_name, f.reason, "
    "MAX(c.to_number) AS to_number "
    "FROM follow_ups f LEFT JOIN calls c ON c.lead_id = f.lead_id "
    "WHERE f.scheduled_date = %s AND f.status = 'pending' "
    "GROUP BY f.id, f.lead_id, f.scheduled_time, f.agent_name, f.reason "
    "ORDER BY f.scheduled_time",
    [today]
)

    # Agent performance
    agents = query("""
        SELECT agent_name, COUNT(*) AS tc,
               SUM(status='Completed') AS comp,
               COUNT(DISTINCT lead_id) AS leads,
               SUM(duration_sec) AS total_dur
        FROM calls GROUP BY agent_name ORDER BY comp DESC
    """)

    # ── Build email body ──────────────────────────────────────────────────────
    tc = int(stats["tc"] or 0)
    lines = [
        f"LEAD MANAGEMENT DAILY SUMMARY",
        f"{today_full}",
        "=" * 55,
        "",
        "📊 DATABASE OVERVIEW",
        f"  Total call records  : {tc}",
        f"  Unique leads        : {stats['ul']}",
        f"  Completed calls     : {stats['comp']}",
        f"  No response         : {stats['nr']}",
        f"  Connect rate        : {round(int(stats['comp'] or 0)/tc*100,1) if tc else 0}%",
        f"  Total talk time     : {fmt_sec(stats['total_dur'])}",
        "",
        "🎯 LEAD QUALITY BREAKDOWN",
        f"  🔥 HOT  (score ≥80) : {len(hot)}",
        f"  🌡️  WARM (score 50-79): {len(warm)}",
        f"  ❄️  COLD (score <50)  : {len(cold)}",
        "",
    ]

    # Today's callbacks section
    if today_cbs:
        lines += [f"📅 TODAY'S CALLBACKS — {len(today_cbs)} scheduled", "─"*46]
        for c in today_cbs:
            lines.append(
                f"  ⏰ {c['scheduled_time']}  "
                f"Lead #{c['lead_id']}  📞 {c['to_number'] or 'unknown'}  "
                f"🧑 {c['agent_name'] or 'unassigned'}"
                + (f"  —  {c['reason']}" if c['reason'] else "")
            )
        lines.append("")
    else:
        lines += [f"📅 No callbacks scheduled for today.", ""]

    # HOT leads
    if hot:
        lines += [f"🔥 HOT LEADS — CALL IMMEDIATELY ({len(hot)} leads)", "─"*46]
        for l in hot[:15]:
            lines.append(
                f"  #{l['lead_id']}  Score:{l['score']}  "
                f"📞 {l['phone']}  🧑 {l['agent_name']}  "
                f"Last called: {l['days_since_last_call']}d ago  "
                f"Completed: {l['completed_calls']}/{l['total_calls']}"
            )
        lines.append("")

    # Stale leads
    if stale:
        lines += [f"⏰ STALE LEADS — NOT CONTACTED IN 10+ DAYS ({len(stale)} shown)", "─"*46]
        for l in stale:
            lines.append(
                f"  #{l['lead_id']}  {l['days_since_last_call']}d ago  "
                f"📞 {l['phone']}  🧑 {l['agent_name']}  Score:{l['score']}"
            )
        lines.append("")

    # Agent performance
    if agents:
        lines += ["👥 AGENT PERFORMANCE", "─"*46]
        for a in agents:
            tc_a = int(a['tc'] or 0)
            cr   = round(int(a['comp'] or 0)/tc_a*100,1) if tc_a else 0
            lines.append(
                f"  🧑 {a['agent_name']}\n"
                f"     Leads: {a['leads']}  |  Calls: {tc_a}  |  "
                f"Completed: {a['comp']} ({cr}%)  |  "
                f"Talk time: {fmt_sec(a['total_dur'])}"
            )
        lines.append("")

    lines += ["─"*55, "Sent automatically by Lead Management System"]
    return "\n".join(str(l) for l in lines), today_full

def send_email():
    print("Building report...")
    body, today_full = build_report()

    print("Connecting to Gmail...")
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

        print(f"✅ Email sent to {EMAIL_CONFIG['recipient_email']}")
        print(f"   Subject: Lead Summary — {today_full}")
    except smtplib.SMTPAuthenticationError:
        print("❌ Gmail authentication failed.")
        print("   Make sure you are using an App Password, not your regular Gmail password.")
        print("   Go to: Google Account → Security → 2-Step Verification → App Passwords")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Failed to send email: {e}")
        sys.exit(1)

if __name__ == "__main__":
   if not all([
    DB_CONFIG["password"],
    EMAIL_CONFIG["sender_email"],
    EMAIL_CONFIG["sender_password"],
    EMAIL_CONFIG["recipient_email"],
]):
    print("Missing required configuration in .env")
    sys.exit(1)
    ensure_tables()
    send_email()