#!/usr/bin/env python3
"""
transcribe_only.py

Requirements:
  pip install groq
"""

import os, sys, argparse, tempfile, urllib.request, time, csv, re
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise RuntimeError("GROQ_API_KEY is missing from .env")
INPUT_CSV         = "call_recordings.csv"   # your source data
OUTPUT_CSV        = "transcripts.csv"       # results saved here
MIN_DURATION_SEC  = 30                      # skip calls shorter than this

# ── Duration helper ───────────────────────────────────────────────────────────
def duration_to_sec(d):
    if not d:
        return 0
    try:
        parts = d.strip().split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except:
        return 0
    return 0

# ── Garbage detection ─────────────────────────────────────────────────────────
def is_garbage(text: str) -> bool:
    if not text or len(text.strip()) < 10:
        return True
    words = text.strip().split()
    if len(words) < 3:
        return True
    single_char = [w for w in words if len(re.sub(r'[^\w]', '', w)) <= 1]
    if len(single_char) / len(words) > 0.5:
        return True
    if len(words) > 5:
        if len(set(w.lower() for w in words)) / len(words) < 0.3:
            return True
    return False

# ── Download audio ────────────────────────────────────────────────────────────
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

# ── Load already-processed call IDs from output CSV ──────────────────────────
def load_done_ids(output_csv: str) -> set:
    done = set()
    if not Path(output_csv).exists():
        return done
    with open(output_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            done.add(row["call_id"])
    return done

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Max calls to process")
    args = parser.parse_args()

    print("=" * 55)
    print("  Objection Analysis — Transcribe Calls (Groq)")
    print("=" * 55)

    # Load Groq
    try:
        from groq import Groq
        client = Groq(api_key=GROQ_API_KEY)
        print("✅ Groq client ready")
    except ImportError:
        print("❌ Run: pip install groq")
        sys.exit(1)

    # Read input CSV
    if not Path(INPUT_CSV).exists():
        print(f"❌ {INPUT_CSV} not found — place it in the same folder as this script")
        sys.exit(1)

    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        all_rows = list(csv.DictReader(f))

    # Filter: ANSWERED + has URL + duration >= 30s
    pending = [
        r for r in all_rows
        if r["status"].strip().upper() == "ANSWERED"
        and r["recording_url"].strip()
        and duration_to_sec(r["call_duration"]) >= MIN_DURATION_SEC
    ]

    # Skip already processed
    done_ids = load_done_ids(OUTPUT_CSV)
    pending = [r for r in pending if str(r["id"]) not in done_ids]

    if args.limit:
        pending = pending[:args.limit]

    print(f"\n📋 Calls to transcribe : {len(pending)}")
    print(f"   Already done        : {len(done_ids)}")
    if not pending:
        print("\n✅ Nothing left to transcribe!")
        sys.exit(0)

    # Prepare output CSV (append mode)
    output_exists = Path(OUTPUT_CSV).exists()
    out_file = open(OUTPUT_CSV, "a", newline="", encoding="utf-8")
    fieldnames = ["call_id", "lead_id", "agent_name", "call_duration",
                  "detected_language", "transcript", "status", "transcribed_at"]
    writer = csv.DictWriter(out_file, fieldnames=fieldnames)
    if not output_exists:
        writer.writeheader()

    done = failed = garbage = 0
    print(f"\n🎙️  Starting transcription...\n")

    for i, row in enumerate(pending, 1):
        call_id   = row["id"]
        lead_id   = row["lead_id"]
        agent     = row["agent_name"] or "Unknown"
        url       = row["recording_url"].strip()
        dur       = duration_to_sec(row["call_duration"])

        print(f"[{i}/{len(pending)}] Call #{call_id} | Lead #{lead_id} | Agent: {agent} | {dur}s")

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp_path = tmp.name

        result_row = {
            "call_id": call_id,
            "lead_id": lead_id,
            "agent_name": agent,
            "call_duration": dur,
            "detected_language": "",
            "transcript": "",
            "status": "failed",
            "transcribed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

        try:
            # Download
            print(f"    ⬇️  Downloading...", end=" ", flush=True)
            if not download_audio(url, tmp_path):
                print("FAILED")
                writer.writerow(result_row)
                out_file.flush()
                failed += 1
                continue
            print("OK")

            # Transcribe via Groq
            print(f"    🎙️  Transcribing...", end=" ", flush=True)
            start = time.time()

            with open(tmp_path, "rb") as audio_file:
                response = client.audio.transcriptions.create(
                    file=audio_file,
                    model="whisper-large-v3",
                    response_format="verbose_json",  # includes detected language
                )

            transcript     = response.text.strip()
            detected_lang  = getattr(response, "language", "unknown")
            elapsed        = round(time.time() - start, 1)
            print(f"OK ({elapsed}s) | 🌐 {detected_lang}")

            # Empty check
            if not transcript:
                print(f"    ⚠️  Empty transcript")
                writer.writerow(result_row)
                out_file.flush()
                failed += 1
                continue

            # Garbage check
            if is_garbage(transcript):
                print(f"    🗑️  Garbage: \"{transcript[:60]}\"")
                result_row["status"] = "garbage"
                result_row["transcript"] = transcript
                writer.writerow(result_row)
                out_file.flush()
                garbage += 1
                failed += 1
                continue

            # Success
            result_row["transcript"]        = transcript
            result_row["detected_language"] = detected_lang
            result_row["status"]            = "done"
            writer.writerow(result_row)
            out_file.flush()

            preview = transcript[:80] + ("..." if len(transcript) > 80 else "")
            print(f"    ✅ \"{preview}\"")
            done += 1

        except Exception as e:
            print(f"\n    ❌ Error: {e}")
            writer.writerow(result_row)
            out_file.flush()
            failed += 1
        finally:
            try:
                os.unlink(tmp_path)
            except:
                pass

        print()

    out_file.close()

    print("=" * 55)
    print(f"✅ Done! Results saved to: {OUTPUT_CSV}")
    print(f"   Successful : {done}")
    print(f"   Failed     : {failed - garbage}")
    if garbage:
        print(f"   Garbage    : {garbage}  ← noise/silent calls")
    print(f"\n▶️  Review {OUTPUT_CSV}, then run:")
    print(f"   python upload_to_db.py")
    print("=" * 55)

if __name__ == "__main__":
    main()
