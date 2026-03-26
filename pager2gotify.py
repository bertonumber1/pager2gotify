#!/usr/bin/env python3
"""pager2gotify: Read multimon-ng decoded pager lines from stdin and forward clean pages to Gotify.

Config via environment variables:
  GOTIFY_URL   e.g. http://127.0.0.1:8088
  GOTIFY_TOKEN Gotify application token

Optional filters:
  ALLOW_BAUD=512
  ALLOW_SUFFIX=450
  BLOCK_PHRASES=TEST MESSAGE,+++TIME=
"""
from __future__ import annotations
import os
import re
import sys
import time
import urllib.request
from typing import Dict

GOTIFY_URL = os.environ.get("GOTIFY_URL", "http://127.0.0.1:8088").rstrip("/")
GOTIFY_TOKEN = os.environ.get("GOTIFY_TOKEN", "CHANGE_ME")
DEDUP_SECONDS = int(os.environ.get("DEDUP_SECONDS", "60"))
ALLOW_BAUD = os.environ.get("ALLOW_BAUD", "512")
ALLOW_SUFFIX = os.environ.get("ALLOW_SUFFIX", "450")
BLOCK_PHRASES = [
    p.strip().upper()
    for p in os.environ.get("BLOCK_PHRASES", "TEST MESSAGE,+++TIME=").split(",")
    if p.strip()
]

seen: Dict[str, float] = {}

POCSAG_RE = re.compile(
    r"POCSAG(?P<speed>\d+):\s+Address:\s+(?P<addr>\d+)\s+Function:\s+(?P<func>\d+)\s+(?P<kind>Alpha|Numeric):\s+(?P<msg>.+)$"
)
FLEX_STRUCTURED_RE = re.compile(
    r'^(?P<prefix>\d{2}:\d{2}\|[^|]+\|[^|]+\|)(?P<addr>\d+)\|(?P<kind>[A-Z]+)\|(?P<msg>.+)$'
)


def cleanup_seen() -> None:
    now = time.time()
    for k, v in list(seen.items()):
        if now - v > DEDUP_SECONDS:
            del seen[k]


def send_gotify(title: str, message: str, priority: int = 5) -> None:
    if GOTIFY_TOKEN == "CHANGE_ME":
        raise RuntimeError("Set GOTIFY_TOKEN environment variable")
    url = f"{GOTIFY_URL}/message?token={GOTIFY_TOKEN}"
    boundary = "----pager2gotifyboundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="title"\r\n\r\n{title}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="message"\r\n\r\n{message}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="priority"\r\n\r\n{priority}\r\n'
        f"--{boundary}--\r\n"
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()


def has_control_junk(text: str) -> bool:
    return any(ord(c) < 32 and c not in "\t\r\n" for c in text)


def looks_like_garbage(text: str) -> bool:
    if not text.strip():
        return True
    text = text.strip()
    if len(text) < 4:
        return True
    if has_control_junk(text):
        return True
    if text.count('*') > 8:
        return True
    if re.search(r'[\\<>{}\[\]^`~|]{5,}', text):
        return True
    letters = sum(1 for c in text if c.isalpha())
    digits = sum(1 for c in text if c.isdigit())
    spaces = sum(1 for c in text if c.isspace())
    printable = sum(1 for c in text if c.isprintable())
    if printable == 0:
        return True
    non_alnum = len(text) - letters - digits - spaces
    if digits >= letters * 3 and letters < 4:
        return True
    if letters == 0 and digits > 0:
        return True
    if non_alnum > letters + digits:
        return True
    return False


def clean_message(msg: str) -> str:
    msg = msg.replace("\r", " ").replace("\n", " ").strip()
    msg = re.sub(r'\s+', ' ', msg)
    msg = msg.replace(" ,", ",").replace(" .", ".")
    return msg


def should_drop(speed: str, addr: str, msg: str) -> bool:
    upper = msg.upper().strip()
    if speed != ALLOW_BAUD:
        return True
    if not addr.endswith(ALLOW_SUFFIX):
        return True
    if any(phrase in upper for phrase in BLOCK_PHRASES):
        return True
    if looks_like_garbage(msg):
        return True
    return False


def process_line(line: str):
    line = line.strip()
    if not line:
        return None
    if line.startswith("FLEX"):
        return None
    m = POCSAG_RE.match(line)
    if not m:
        return None
    addr = m.group("addr")
    speed = m.group("speed")
    func = m.group("func")
    kind = m.group("kind")
    msg = clean_message(m.group("msg"))
    if should_drop(speed, addr, msg):
        return {"action": "drop", "mode": "POCSAG", "baud": speed, "capcode": addr, "message": msg}
    key = f"POCSAG|{speed}|{addr}|{msg}"
    cleanup_seen()
    if key in seen:
        return {"action": "dedup", "mode": "POCSAG", "baud": speed, "capcode": addr, "message": msg}
    seen[key] = time.time()
    title = f"SFRS {addr}"
    body = f"Mode: POCSAG\nBaud: {speed}\nCapcode: {addr}\nFunction: {func}\nType: {kind}\n\n{msg}"
    return {"action": "send", "title": title, "body": body, "mode": "POCSAG", "baud": speed, "capcode": addr, "message": msg}


def main() -> int:
    for raw in sys.stdin:
        try:
            result = process_line(raw)
            if not result:
                continue
            if result["action"] == "send":
                print(f"SEND {result['title']}: {result['message']}", flush=True)
                send_gotify(result["title"], result["body"])
            elif result["action"] == "drop":
                print(f"DROP {result['mode']} {result['baud']} {result['capcode']}: {result['message']}", flush=True)
        except Exception as e:
            print(f"ERR {e}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
