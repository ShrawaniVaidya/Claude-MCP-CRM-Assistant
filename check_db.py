
import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is missing from .env")

conn = None

try:
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM call_recordings")
    print("call_recordings rows:", cur.fetchone()[0])

    cur.execute(
        'SELECT COUNT(*) FROM call_recordings WHERE transcript_status = "done"'
    )
    print("transcript_status=done:", cur.fetchone()[0])

    cur.execute("SHOW TABLES")
    print("Tables:", [r[0] for r in cur.fetchall()])

    cur.close()

finally:
    if conn and conn.is_connected():
        conn.close()
