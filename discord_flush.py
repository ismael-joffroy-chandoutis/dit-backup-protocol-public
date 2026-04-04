#!/usr/bin/env python3
"""Discord + Telegram flush daemon.
Lit /tmp/discord_queue.txt, écrit les messages à envoyer dans /tmp/discord_to_send.json.
Claude Code lit ce fichier et envoie via MCP Discord."""

import time, json, os
from pathlib import Path

QUEUE = Path("/tmp/discord_queue.txt")
TO_SEND = Path("/tmp/discord_to_send.json")
PROCESSED = Path("/tmp/discord_processed.txt")

def flush():
    if not QUEUE.exists() or QUEUE.stat().st_size == 0:
        return

    lines = QUEUE.read_text().splitlines()
    if not lines:
        return

    processed = set()
    if PROCESSED.exists():
        processed = set(PROCESSED.read_text().splitlines())

    messages = []
    for line in lines:
        if line in processed or not line.strip():
            continue
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        msg_type, ts, content = parts

        if msg_type == "ALERT":
            messages.append({
                "type": "alert",
                "text": f"⚠️ **ALERTE BACKUP** [{ts}]\n{content}\n\nRépondre **oui** pour continuer ou **non** pour stopper.",
                "needs_response": True
            })
        elif msg_type == "DONE":
            messages.append({
                "type": "done",
                "text": f"✅ **{content}**\n\nTout est vérifié. Sources safe à éjecter.",
                "needs_response": False
            })
        elif msg_type == "PHASE":
            messages.append({
                "type": "phase",
                "text": f"⏳ {content}",
                "needs_response": False
            })
        elif msg_type == "INFO":
            messages.append({
                "type": "info",
                "text": f"ℹ️ {content}",
                "needs_response": False
            })

        processed.add(line)

    if messages:
        TO_SEND.write_text(json.dumps(messages, ensure_ascii=False, indent=2))
        PROCESSED.write_text("\n".join(processed))

while True:
    try:
        flush()
    except Exception as e:
        print(f"Error: {e}")
    time.sleep(10)
