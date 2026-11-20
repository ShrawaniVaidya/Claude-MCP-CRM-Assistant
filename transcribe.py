#!/usr/bin/env python3

import os, sys, argparse, tempfile, urllib.request, time, re
import mysql.connector
from dotenv import load_dotenv

load_dotenv()
# ── CONFIG ────────────────────────────────────────────────────────────────────

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "3306")),
    "database": os.getenv("DB_NAME", "leads_db"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD"),
}

# Model size: tiny / base / small / medium / large-v2
# medium = best for Hindi/Urdu/Telugu/English mix (Hyderabad calls)
WHISPER_MODEL = "medium"

MIN_DURATION_SEC = 30   # skip calls shorter than this

# Languages likely in Hyderabad sales calls
# Whisper will auto-detect but we hint it to these candidates
EXPECTED_LANGUAGES = ["hi", "ur", "te", "en"]

# ── Garbage Detection ─────────────────────────────────────────────────────────
def is_garbage_transcript(text: str) -> bool:
    """
    Returns True if the transcript looks like transcription noise, not real speech.
    Catches patterns like: "h h h h h", repeated single chars, only punctuation, etc.
    """
    if not text or len(text.strip()) < 10:
        return True

    words = text.strip().split()

    # Too few words
    if len(words) < 3:
        return True

    # Check for repetitive single-character words (e.g. "h h h h h h")
    single_char_words = [w for w in words if len(re.sub(r'[^\w]', '', w)) <= 1]
    if len(single_char_words) / len(words) > 0.5:
        return True

    # Check if majority of characters are non-Latin AND non-Devanagari AND non-Telugu
    # (catches random Arabic/Chinese hallucinations on silent audio)
    latin_devanagari_telugu = re.findall(
        r'[a-zA-Z\u0900-\u097F\u0C00-\u0C7F]', text
    )
    total_alpha = re.findall(r'\w', text)
    if total_alpha and len(latin_devanagari_telugu) / len(total_alpha) < 0.3:
        return True

    # Check for excessive repetition (same word repeated many times)
    if len(words) > 5:
        unique_ratio = len(set(w.lower() for w in words)) / len(words)
        if unique_ratio < 0.3:
            return True

    return False

# ── DB Helpers ────────────────────────────────────────────────────────────────
def get_conn():
    return mysql.connector.connect(**DB_CONFIG)

def fetch_pending(lead_id=None, limit=None):
    conn = get_conn()
    cur  = conn.cursor(dictionary=True)
    sql  = """
        SELECT id, lead_id, agent_name, recording_url, call_duration_sec
        FROM call_recordings
        WHERE transcript_status = 'pending'
          AND recording_url IS NOT NULL
          AND call_duration_sec >= %s
    """
    params = [MIN_DURATION_SEC]
    if lead_id:
        sql += " AND lead_id = %s"
        params.append(lead_id)
    sql += " ORDER BY start_time DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def save_transcript(call_id, transcript, status="done", detected_lang=None):
    conn = get_conn()
    cur  = conn.cursor()
    # Save detected language in transcript as a prefix comment if available
    final_text = transcript
    if transcript and detected_lang:
        final_text = f"[lang:{detected_lang}] {transcript}"
    cur.execute("""
        UPDATE call_recordings
        SET transcript = %s, transcript_status = %s
        WHERE id = %s
    """, (final_text, status, call_id))
    conn.commit()
    cur.close()
    conn.close()

# ── Download ──────────────────────────────────────────────────────────────────
def download_audio(url: str, dest_path: str) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            with open(dest_path, "wb") as f:
                f.write(resp.read())
        return os.path.getsize(dest_path) > 0
    except Exception as e:
        print(f"    ⚠️  Download failed: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--lead_id", type=int)
    parser.add_argument("--limit",   type=int)
    args = parser.parse_args()

    print("=" * 55)
    print("  Objection Analysis — Step 2: Transcribe Calls")
    print("=" * 55)

    # Load faster-whisper
    print(f"\n🤖 Loading Whisper model '{WHISPER_MODEL}'...")
    if WHISPER_MODEL == "medium":
        print("   (First run downloads ~1.5GB model — please wait)")
    else:
        print("   (First run downloads model — please wait)")

    try:
        from faster_whisper import WhisperModel
        model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        print(f"✅ Whisper ready")
    except ImportError:
        print("❌ Run: pip install faster-whisper")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Model load failed: {e}")
        sys.exit(1)

    # Fetch pending
    print(f"\n🔍 Fetching calls to transcribe...")
    pending = fetch_pending(lead_id=args.lead_id, limit=args.limit)
    print(f"   Found {len(pending)} calls to transcribe")

    if not pending:
        print("\n✅ Nothing to transcribe — all calls already done!")
        sys.exit(0)

    done = 0
    failed = 0
    garbage = 0
    print(f"\n🎙️  Starting transcription...\n")

    for i, call in enumerate(pending, 1):
        call_id = call["id"]
        lead_id = call["lead_id"]
        agent   = call["agent_name"] or "Unknown"
        url     = call["recording_url"]
        dur     = call["call_duration_sec"]

        print(f"[{i}/{len(pending)}] Call #{call_id} | Lead #{lead_id} | Agent: {agent} | {dur}s")

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Download
            print(f"    ⬇️  Downloading...", end=" ", flush=True)
            if not download_audio(url, tmp_path):
                print("FAILED")
                save_transcript(call_id, None, "failed")
                failed += 1
                continue
            print("OK")

            # Transcribe
            print(f"    🎙️  Transcribing...", end=" ", flush=True)
            start = time.time()

            segments, info = model.transcribe(
                tmp_path,
                language=None,       # Auto-detect per call (Hindi/Urdu/Telugu/English)
                beam_size=5,
                vad_filter=True,     # Skip silent/noise parts automatically
                vad_parameters=dict(
                    min_silence_duration_ms=500,
                    speech_pad_ms=200,
                ),
            )
            transcript = " ".join(seg.text.strip() for seg in segments).strip()
            elapsed = round(time.time() - start, 1)
            detected_lang = info.language
            lang_conf = round(info.language_probability * 100)
            print(f"OK ({elapsed}s) | 🌐 {detected_lang} ({lang_conf}% confidence)")

            # Check for empty transcript
            if not transcript:
                print(f"    ⚠️  Empty transcript — marking failed")
                save_transcript(call_id, None, "failed")
                failed += 1
                continue

            # Check for garbage (noise hallucinations like "h h h h h")
            if is_garbage_transcript(transcript):
                print(f"    🗑️  Garbage transcript detected — marking failed")
                print(f"    Preview: \"{transcript[:60]}\"")
                save_transcript(call_id, None, "failed")
                garbage += 1
                failed += 1
                continue

            # Warn if unexpected language detected (possible hallucination)
            if detected_lang not in EXPECTED_LANGUAGES:
                print(f"    ⚠️  Unexpected language '{detected_lang}' — saving but verify manually")

            save_transcript(call_id, transcript, "done", detected_lang)
            preview = transcript[:80] + ("..." if len(transcript) > 80 else "")
            print(f"    ✅ \"{preview}\"")
            done += 1

        except Exception as e:
            print(f"\n    ❌ Error: {e}")
            save_transcript(call_id, None, "failed")
            failed += 1
        finally:
            try: os.unlink(tmp_path)
            except: pass

        print()

    print("=" * 55)
    print(f"✅ Transcription complete!")
    print(f"   Successful : {done}")
    print(f"   Failed     : {failed - garbage}")
    if garbage:
        print(f"   Garbage    : {garbage}  ← noise/silent calls")
    print(f"\n▶️  Restart Claude Desktop and ask:")
    print(f'   "What are the most common objections from leads?"')
    print(f'   "Which agent handles objections best?"')
    print("=" * 55)

if __name__ == "__main__":
    main()