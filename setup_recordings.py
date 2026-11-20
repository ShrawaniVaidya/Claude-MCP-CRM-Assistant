#!/usr/bin/env python3

import csv
import os
import sys
import mysql.connector
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

# ── CONFIG — same as mcp_server.py ────────────────────────────────────────────

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is missing from .env")


# Path to your CSV file — update if needed
CSV_PATH = "call_recordings.csv"

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def clean(val):
    """Convert NULL strings and empty to Python None."""
    if val is None: return None
    if str(val).strip().upper() in ("NULL", ""): return None
    return str(val).strip()

def parse_dt(val):
    """Parse datetime string to MySQL-compatible format."""
    if not clean(val): return None
    try:
        return datetime.strptime(val.strip(), "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M:%S")
    except:
        return None

# ── Create Table ──────────────────────────────────────────────────────────────
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS call_recordings (
    id                  BIGINT PRIMARY KEY,
    call_type           VARCHAR(50),
    from_number         VARCHAR(30),
    to_number           VARCHAR(30),
    status              VARCHAR(50),
    status_reason       VARCHAR(255),
    start_time          DATETIME,
    end_time            DATETIME,
    unique_call_id      VARCHAR(255),
    country             VARCHAR(50),
    call_duration       VARCHAR(20),
    duration_sec   INT DEFAULT 0,
    ring_duration       VARCHAR(20),
    hangup_by           VARCHAR(50),
    recording_url       TEXT,
    is_active           TINYINT(1) DEFAULT 1,
    created_by          BIGINT,
    created_on          DATETIME,
    last_modified_by    BIGINT,
    last_modified_time  DATETIME,
    client_group_id     BIGINT,
    lead_id             BIGINT,
    remarks             TEXT,
    agent_mobile_number VARCHAR(30),
    agent_name          VARCHAR(100),
    user_id             BIGINT,

    -- Added for objection analysis pipeline
    transcript          LONGTEXT DEFAULT NULL,
    transcript_status   ENUM('pending','done','failed','too_short') DEFAULT 'pending',
    objection_analysis  JSON DEFAULT NULL,
    analysed_at         DATETIME DEFAULT NULL,

    INDEX idx_lead_id    (lead_id),
    INDEX idx_agent_name (agent_name),
    INDEX idx_start_time (start_time),
    INDEX idx_transcript_status (transcript_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

def duration_to_sec(dur: str) -> int:
    """Convert HH:MM:SS to seconds."""
    try:
        parts = str(dur).strip().split(":")
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except:
        return 0

# ── Load CSV ──────────────────────────────────────────────────────────────────
def load_csv():
    if not os.path.exists(CSV_PATH):
        print(f"❌ CSV not found: {CSV_PATH}")
        print(f"   Make sure the file is in the same folder as this script.")
        sys.exit(1)

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"📂 CSV loaded — {len(rows)} rows")
    return rows

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("=" * 55)
    print("  Objection Analysis — Step 1: Setup Recordings Table")
    print("=" * 55)

    # Connect
    print(f"\n🔌 Connecting to MySQL '{DB_CONFIG['database']}'...")
    try:
        conn = get_conn()
        cur  = conn.cursor()
        print("✅ Connected")
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Create table
    print("\n📋 Creating call_recordings table...")
    cur.execute(CREATE_TABLE)
    conn.commit()
    print("✅ Table ready")

    # Load CSV
    print("\n📂 Loading CSV...")
    rows = load_csv()

    # Insert rows
    print(f"\n⬆️  Inserting {len(rows)} rows...")
    INSERT = """
        INSERT IGNORE INTO call_recordings (
            id, call_type, from_number, to_number, status, status_reason,
            start_time, end_time, unique_call_id, country,
            call_duration, call_duration_sec, ring_duration, hangup_by,
            recording_url, is_active, created_by, created_on,
            last_modified_by, last_modified_time, client_group_id,
            lead_id, remarks, agent_mobile_number, agent_name, user_id,
            transcript_status
        ) VALUES (
            %s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,%s,
            %s,%s,%s,
            %s,%s,%s,%s,%s,
            %s
        )
    """

    inserted = 0
    skipped  = 0
    for r in rows:
        dur_sec = duration_to_sec(r.get("call_duration",""))
        # Mark very short calls as too_short — not worth transcribing
        t_status = "too_short" if dur_sec < 30 else "pending"

        try:
            cur.execute(INSERT, (
                clean(r.get("id")),
                clean(r.get("call_type")),
                clean(r.get("from_number")),
                clean(r.get("to_number")),
                clean(r.get("status")),
                clean(r.get("status_reason")),
                parse_dt(r.get("start_time")),
                parse_dt(r.get("end_time")),
                clean(r.get("unique_call_id")),
                clean(r.get("country")),
                clean(r.get("call_duration")),
                dur_sec,
                clean(r.get("ring_duration")),
                clean(r.get("hangup_by")),
                clean(r.get("recording_url")),
                1 if str(r.get("is_active","1")) == "1" else 0,
                clean(r.get("created_by")),
                parse_dt(r.get("created_on")),
                clean(r.get("last_modified_by")),
                parse_dt(r.get("last_modified_time")),
                clean(r.get("client_group_id")),
                clean(r.get("lead_id")),
                clean(r.get("remarks")),
                clean(r.get("agent_mobile_number")),
                clean(r.get("agent_name")),
                clean(r.get("user_id")),
                t_status,
            ))
            inserted += 1
        except Exception as e:
            print(f"  ⚠️  Row {r.get('id')} skipped: {e}")
            skipped += 1

    conn.commit()
    cur.close()
    conn.close()

    print(f"\n✅ Done!")
    print(f"   Inserted : {inserted} rows")
    print(f"   Skipped  : {skipped} rows (duplicates or errors)")

    # Summary
    print(f"\n📊 What's next:")
    print(f"   • Calls ready to transcribe (>30s) : ~196")
    print(f"   • Calls too short (<30s)           : ~309  (skipped automatically)")
    print(f"\n▶️  Run next: python transcribe.py")
    print("=" * 55)

if __name__ == "__main__":
    main()
