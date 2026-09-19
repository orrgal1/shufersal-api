"""
Continuously listens for incoming feedback from the remote agent
via the HTTP API (tailing feedback.jsonl) and optional email queries.
"""
import os
import time
import json
import subprocess
from pathlib import Path

FEEDBACK_FILE = Path("feedback.jsonl")
CHECK_INTERVAL_SECONDS = 5
REMOTE_AGENT_EMAIL = os.getenv("REMOTE_AGENT_EMAIL", "")

def check_gmail_messages(seen_ids: set):
    if not REMOTE_AGENT_EMAIL:
        return
    cmd = [
        "gapi", "gmail", "search",
        f"from:{REMOTE_AGENT_EMAIL} newer_than:1d",
        "--max", "5"
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout.strip():
            messages = json.loads(res.stdout)
            if isinstance(messages, list):
                for msg in messages:
                    msg_id = msg.get("id")
                    if msg_id and msg_id not in seen_ids:
                        seen_ids.add(msg_id)
                        fetch_cmd = ["gapi", "gmail", "get", msg_id]
                        m_res = subprocess.run(fetch_cmd, capture_output=True, text=True, timeout=10)
                        body = ""
                        if m_res.returncode == 0:
                            try:
                                m_data = json.loads(m_res.stdout)
                                body = m_data.get("body", m_data.get("snippet", ""))
                            except Exception:
                                body = msg.get("snippet", "")
                        print(f"\n📨 [NEW GMAIL FEEDBACK from {msg.get('from')}]")
                        print(f"Subject: {msg.get('subject')}")
                        print(f"Message: {body}\n", flush=True)
    except Exception:
        pass

def main():
    print("Feedback listener started. Monitoring feedback.jsonl...", flush=True)
    last_pos = FEEDBACK_FILE.stat().st_size if FEEDBACK_FILE.exists() else 0
    seen_gmail_ids = set()

    if REMOTE_AGENT_EMAIL:
        try:
            cmd = ["gapi", "gmail", "search", f"from:{REMOTE_AGENT_EMAIL} newer_than:1d", "--max", "5"]
            init_res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if init_res.returncode == 0 and init_res.stdout.strip():
                msgs = json.loads(init_res.stdout)
                if isinstance(msgs, list):
                    for m in msgs:
                        seen_gmail_ids.add(m.get("id"))
        except Exception:
            pass

    while True:
        if FEEDBACK_FILE.exists():
            curr_size = FEEDBACK_FILE.stat().st_size
            if curr_size > last_pos:
                with FEEDBACK_FILE.open("r", encoding="utf-8") as f:
                    f.seek(last_pos)
                    new_lines = f.readlines()
                    last_pos = f.tell()
                for line in new_lines:
                    if line.strip():
                        try:
                            entry = json.loads(line)
                            print(f"\n📢 [API FEEDBACK RECEIVED] Category: [{entry.get('category')}]")
                            print(f"Message: {entry.get('message')}")
                            if entry.get("details"):
                                print(f"Details: {json.dumps(entry['details'], ensure_ascii=False)}")
                            print(f"Timestamp: {entry.get('timestamp')}\n", flush=True)
                        except Exception:
                            print(f"\n📢 [RAW FEEDBACK LINE]: {line.strip()}\n", flush=True)

        if REMOTE_AGENT_EMAIL:
            check_gmail_messages(seen_gmail_ids)

        time.sleep(CHECK_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
