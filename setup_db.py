#!/usr/bin/env python3

import argparse
import os
import sys
import pandas as pd
import mysql.connector

from dotenv import load_dotenv

load_dotenv(override=True)


parser = argparse.ArgumentParser()

parser.add_argument("--host", default=os.getenv("DB_HOST", "localhost"))
parser.add_argument("--port", default=int(os.getenv("DB_PORT", "3306")), type=int)
parser.add_argument("--user", default=os.getenv("DB_USER", "root"))
parser.add_argument("--password", default=os.getenv("DB_PASSWORD"))
parser.add_argument("--dbname", default=os.getenv("DB_NAME", "leads_db"))
parser.add_argument(
    "--csv",
    default=os.path.join(os.path.dirname(__file__), "calls_data.csv")
)

args = parser.parse_args()

if not args.password:
    parser.error("MySQL password missing. Configure DB_PASSWORD in .env.")


print(f"\n{'='*55}")
print("  Lead Management — MySQL Database Setup")
print(f"{'='*55}")
print(f"  Host     : {args.host}:{args.port}")
print(f"  User     : {args.user}")
print(f"  Database : {args.dbname}")
print(f"  CSV      : {args.csv}")
print(f"{'='*55}\n")

# Step 1 — Create database
print("Step 1/3  Creating database...")
try:
    conn = mysql.connector.connect(
        host=args.host, port=args.port,
        user=args.user, password=args.password
    )
    cur = conn.cursor()
    cur.execute(f"SHOW DATABASES LIKE '{args.dbname}'")
    if cur.fetchone():
        print(f"  ✓ Database '{args.dbname}' already exists.")
    else:
        cur.execute(
            f"CREATE DATABASE {args.dbname} "
            f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        print(f"  ✓ Database '{args.dbname}' created.")
    cur.close()
    conn.close()
except mysql.connector.Error as e:
    print(f"\n  ✗ Could not connect to MySQL: {e}")
    print("\n  Make sure MySQL is running and your password is correct.")
    print("  Try: python setup_db.py --password YOUR_PASSWORD\n")
    sys.exit(1)

# Step 2 — Create table
print("Step 2/3  Creating table...")
try:
    conn = mysql.connector.connect(
        host=args.host, port=args.port,
        user=args.user, password=args.password,
        database=args.dbname
    )
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id               BIGINT PRIMARY KEY,
            lead_id          BIGINT,
            status           VARCHAR(100),
            call_duration    VARCHAR(20),
            duration_sec     INT DEFAULT 0,
            start_time       DATETIME,
            end_time         DATETIME,
            agent_name       VARCHAR(200),
            to_number        VARCHAR(50),
            from_number      VARCHAR(50),
            call_type        VARCHAR(50),
            hangup_by        VARCHAR(100),
            remarks          TEXT,
            recording_url    TEXT,
            client_group_id  BIGINT,
            created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_lead_id    (lead_id),
            INDEX idx_status     (status),
            INDEX idx_agent      (agent_name),
            INDEX idx_start_time (start_time),
            INDEX idx_to_number  (to_number)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)
    conn.commit()
    print("  ✓ Table 'calls' ready with indexes.")
except Exception as e:
    print(f"  ✗ Error creating table: {e}")
    sys.exit(1)

# Step 3 — Load CSV
print("Step 3/3  Loading CSV...")
if not os.path.exists(args.csv):
    print(f"  ✗ CSV not found: {args.csv}")
    print("  Put calls_data.csv in the same folder, or pass --csv /full/path")
    sys.exit(1)

def dur_to_sec(d):
    try:
        p = str(d).strip().split(":")
        return int(p[0])*3600 + int(p[1])*60 + int(p[2])
    except:
        return 0

def safe(v):
    if pd.isna(v): return None
    return v

try:
    df = pd.read_csv(args.csv)
    df.columns = df.columns.str.strip()
    df["duration_sec"] = df["call_duration"].apply(dur_to_sec)
    df["start_time"]   = pd.to_datetime(df["start_time"], errors="coerce")
    df["end_time"]     = pd.to_datetime(df["end_time"],   errors="coerce")

    cur.execute("SELECT COUNT(*) FROM calls")
    existing = cur.fetchone()[0]
    if existing > 0:
        print(f"  ! Table already has {existing} rows.")
        ans = input("  Reload? This will DELETE all existing rows. (y/N): ").strip().lower()
        if ans == "y":
            cur.execute("DELETE FROM calls")
            conn.commit()
            print("  ✓ Cleared existing rows.")
        else:
            print("  Skipped. Database unchanged.")
            cur.close(); conn.close()
            sys.exit(0)

    inserted, skipped = 0, 0
    for _, row in df.iterrows():
        try:
            cur.execute("""
                INSERT IGNORE INTO calls (
                    id, lead_id, status, call_duration, duration_sec,
                    start_time, end_time, agent_name, to_number, from_number,
                    call_type, hangup_by, remarks, recording_url, client_group_id
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (
                safe(row.get("id")),
                safe(row.get("lead_id")),
                safe(row.get("status")),
                safe(row.get("call_duration")),
                safe(row.get("duration_sec")),
                safe(row.get("start_time")),
                safe(row.get("end_time")),
                safe(row.get("agent_name")),
                safe(row.get("to_number")),
                safe(row.get("from_number")),
                safe(row.get("call_type")),
                safe(row.get("hangup_by")),
                safe(row.get("remarks")),
                safe(row.get("recording_url")),
                safe(row.get("client_group_id")),
            ))
            inserted += 1
        except Exception:
            skipped += 1

    conn.commit()
    print(f"  ✓ Inserted {inserted} rows. Skipped {skipped}.")
    cur.close()
    conn.close()

except Exception as e:
    print(f"  ✗ Error loading CSV: {e}")
    sys.exit(1)

print(f"\n{'='*55}")
print("  ✅  Setup complete!")
print(f"{'='*55}")
print("""
  Your data is now in MySQL.

  Next:
  1. Verify your .env configuration.
  2. Configure Claude Desktop to use mcp_server.py.
  3. Restart Claude Desktop.
""")
