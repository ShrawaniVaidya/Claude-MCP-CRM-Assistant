#!/usr/bin/env python3
"""
Requirements:
  pip install mysql-connector-python
"""

import csv
import sys
import argparse
import os
import mysql.connector

from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# ── CONFIG ────────────────────────────────────────────────────────────────────

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is missing from .env")


INPUT_CSV = "transcripts.csv"   # output from transcribe_only.py

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def get_already_uploaded(conn) -> set:
    """Return set of call IDs already marked done in DB."""
    cur = conn.cursor()
    cur.execute("SELECT id FROM call_recordings WHERE transcript_status = 'done'")
    ids = {str(row[0]) for row in cur.fetchall()}
    cur.close()
    return ids

def upload_transcript(conn, call_id, transcript, detected_lang):
    cur = conn.cursor()
    final_text = f"[lang:{detected_lang}] {transcript}" if detected_lang else transcript
    cur.execute("""
        UPDATE call_recordings
        SET transcript = %s, transcript_status = 'done'
        WHERE id = %s
    """, (final_text, call_id))
    conn.commit()
    cur.close()

def mark_failed(conn, call_id):
    cur = conn.cursor()
    cur.execute("""
        UPDATE call_recordings
        SET transcript_status = 'failed'
        WHERE id = %s
    """, (call_id,))
    conn.commit()
    cur.close()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be uploaded without writing to DB")
    args = parser.parse_args()

    print("=" * 55)
    print("  Objection Analysis — Upload Transcripts to DB")
    print("=" * 55)

    if not Path(INPUT_CSV).exists():
        print(f"❌ {INPUT_CSV} not found — run transcribe_only.py first")
        sys.exit(1)

    # Load CSV
    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    done_rows    = [r for r in rows if r["status"] == "done"]
    failed_rows  = [r for r in rows if r["status"] == "failed"]
    garbage_rows = [r for r in rows if r["status"] == "garbage"]

    print(f"\n📋 Transcripts CSV summary:")
    print(f"   Ready to upload : {len(done_rows)}")
    print(f"   Failed          : {len(failed_rows)}")
    print(f"   Garbage         : {len(garbage_rows)}")

    if not done_rows:
        print("\n⚠️  No successful transcripts to upload.")
        sys.exit(0)

    if args.dry_run:
        print(f"\n🔍 DRY RUN — nothing will be written to DB")
        print(f"\nSample of what would be uploaded:")
        for row in done_rows[:5]:
            preview = row["transcript"][:70] + "..."
            print(f"  Call #{row['call_id']} | {row['detected_language']} | \"{preview}\"")
        print(f"\n  ... and {max(0, len(done_rows)-5)} more")
        print(f"\nRun without --dry-run to actually upload.")
        sys.exit(0)

    # Connect to DB
    print(f"\n🔌 Connecting to MySQL...", end=" ", flush=True)
    try:
        conn = get_conn()
        print("OK")
    except Exception as e:
        print(f"\n❌ DB connection failed: {e}")
        sys.exit(1)

    already_uploaded = get_already_uploaded(conn)
    skipped = uploaded = failed_marks = 0

    print(f"\n⬆️  Uploading transcripts...\n")

    for row in done_rows:
        call_id  = row["call_id"]
        lang     = row["detected_language"]
        agent    = row["agent_name"]
        transcript = row["transcript"]

        if call_id in already_uploaded:
            print(f"   Call #{call_id} — already in DB, skipping")
            skipped += 1
            continue

        try:
            upload_transcript(conn, call_id, transcript, lang)
            preview = transcript[:60] + ("..." if len(transcript) > 60 else "")
            print(f"   ✅ Call #{call_id} | {agent} | [{lang}] \"{preview}\"")
            uploaded += 1
        except Exception as e:
            print(f"   ❌ Call #{call_id} failed: {e}")

    # Also mark failed/garbage calls in DB
    print(f"\n🗑️  Marking failed/garbage calls in DB...")
    for row in failed_rows + garbage_rows:
        call_id = row["call_id"]
        if call_id not in already_uploaded:
            try:
                mark_failed(conn, call_id)
                failed_marks += 1
            except:
                pass

    conn.close()

    print()
    print("=" * 55)
    print(f"✅ Upload complete!")
    print(f"   Uploaded  : {uploaded}")
    print(f"   Skipped   : {skipped}  ← already in DB")
    print(f"   Marked failed : {failed_marks}")
    print(f"\n▶️  Restart Claude Desktop and ask:")
    print(f'   "What are the most common objections from leads?"')
    print(f'   "Which agent handles objections best?"')
    print("=" * 55)

if __name__ == "__main__":
    main()
