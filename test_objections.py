import os
import json
import urllib.request
import urllib.error
import mysql.connector

from dotenv import load_dotenv

load_dotenv(override=True)

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is missing from .env")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("GEMINI_API_KEY is missing from .env")

if not DB_CONFIG["password"]:
    raise RuntimeError("DB_PASSWORD is missing from .env")

MODEL = "gemini-2.5-flash"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={GEMINI_API_KEY}"

# Step 1: fetch 3 transcripts from DB
conn = mysql.connector.connect(**DB_CONFIG)
cur = conn.cursor(dictionary=True)
cur.execute('SELECT id, agent_name, transcript FROM call_recordings WHERE transcript_status = "done" LIMIT 3')
rows = cur.fetchall()
print(f"Fetched {len(rows)} transcripts from DB")
for r in rows:
    print(f"  ID: {r['id']} | agent: {r['agent_name']} | transcript: {str(r['transcript'])[:80]}")

# Step 2: send to Gemini
transcript_text = ""
for i, r in enumerate(rows, 1):
    transcript_text += f"Call {i}: {str(r['transcript'] or '')[:300]}\n\n"

prompt = f"""Analyse these sales call transcripts and list the objections you find. Return as JSON with key "objection_categories" as a list.

{transcript_text}"""

payload = json.dumps({
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {"maxOutputTokens": 500, "temperature": 0.2}
}).encode("utf-8")

req = urllib.request.Request(URL, data=payload, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    print("\n✅ Gemini response:\n", text[:500])
except urllib.error.HTTPError as e:
    print("❌ Gemini error:", e.code, e.read().decode()[:300])
except Exception as e:
    print("❌ Error:", e)