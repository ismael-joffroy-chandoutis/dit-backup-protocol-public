#!/bin/bash
# BACKUP MONITOR — Surveillance autonome du wrangler
# Envoie alertes Discord + Telegram, pose des questions si besoin
# Écrit dans /tmp/backup_alerts.json pour que Claude puisse agir

LOG="/tmp/silverstack_wrangler.log"
ALERTS="/tmp/backup_alerts.json"
LAST_LINE_FILE="/tmp/backup_monitor_lastline"
DISCORD_QUEUE="/tmp/discord_queue.txt"

log() { echo "[$(date '+%H:%M:%S')] MONITOR: $*" >> /tmp/backup_monitor.log; }
notify() { ~/.claude/scripts/notify.sh "$*" 2>/dev/null || true; }

# Init
touch "$LAST_LINE_FILE"
LAST_LINES=$(wc -l < "$LOG" 2>/dev/null || echo 0)
echo "$LAST_LINES" > "$LAST_LINE_FILE"

log "Monitor démarré — surveillance $LOG"

while true; do
  [ ! -f "$LOG" ] && { sleep 10; continue; }

  CURRENT_LINES=$(wc -l < "$LOG")
  PREV_LINES=$(cat "$LAST_LINE_FILE")

  if [ "$CURRENT_LINES" -gt "$PREV_LINES" ]; then
    # Nouvelles lignes
    NEW=$(tail -n +"$((PREV_LINES+1))" "$LOG" | head -n "$((CURRENT_LINES-PREV_LINES))")
    echo "$CURRENT_LINES" > "$LAST_LINE_FILE"

    # Check pour erreurs critiques
    if echo "$NEW" | grep -qi "FATAL\|ERREUR.*❌\|MISMATCH\|TIMEOUT"; then
      ERROR_MSG=$(echo "$NEW" | grep -i "FATAL\|ERREUR\|MISMATCH\|TIMEOUT" | tail -1)
      log "ALERTE: $ERROR_MSG"
      notify "⚠️ BACKUP ALERTE: $ERROR_MSG"
      # Queue pour Discord
      echo "ALERT|$(date '+%H:%M:%S')|$ERROR_MSG" >> "$DISCORD_QUEUE"
    fi

    # Check pour phases complétées
    if echo "$NEW" | grep -q "BIT-PERFECT"; then
      BP_MSG=$(echo "$NEW" | grep "BIT-PERFECT" | tail -1)
      log "BIT-PERFECT: $BP_MSG"
      echo "INFO|$(date '+%H:%M:%S')|$BP_MSG" >> "$DISCORD_QUEUE"
    fi

    if echo "$NEW" | grep -q "PHASE [0-9]"; then
      PHASE_MSG=$(echo "$NEW" | grep "PHASE" | tail -1)
      log "PHASE: $PHASE_MSG"
      echo "PHASE|$(date '+%H:%M:%S')|$PHASE_MSG" >> "$DISCORD_QUEUE"
    fi

    if echo "$NEW" | grep -q "COMPLET.*SAFE\|SAFE.*ÉJECTER"; then
      log "BACKUP COMPLET!"
      echo "DONE|$(date '+%H:%M:%S')|BACKUP COMPLET — 3-2-1 vérifié — SAFE ÉJECTER" >> "$DISCORD_QUEUE"
    fi

    if echo "$NEW" | grep -qi "R2.*terminé\|R2.*COMPLET"; then
      R2_MSG=$(echo "$NEW" | grep -i "R2" | tail -1)
      echo "INFO|$(date '+%H:%M:%S')|$R2_MSG" >> "$DISCORD_QUEUE"
    fi

    if echo "$NEW" | grep -qi "NAS.*terminé\|NAS.*COMPLET"; then
      NAS_MSG=$(echo "$NEW" | grep -i "NAS" | tail -1)
      echo "INFO|$(date '+%H:%M:%S')|$NAS_MSG" >> "$DISCORD_QUEUE"
    fi
  fi

  # Check si wrangler est mort
  if ! ps -p $(pgrep -f "silverstack" | head -1) > /dev/null 2>&1; then
    if ! pgrep -f "silverstack_resume\|silverstack_wrangler" > /dev/null 2>&1; then
      LAST_LOG=$(tail -1 "$LOG")
      if ! echo "$LAST_LOG" | grep -q "SAFE ÉJECTER"; then
        log "WRANGLER MORT AVANT FIN!"
        notify "⚠️ WRANGLER MORT — dernier log: $LAST_LOG"
        echo "ALERT|$(date '+%H:%M:%S')|WRANGLER MORT avant fin! Dernier: $LAST_LOG" >> "$DISCORD_QUEUE"
      fi
    fi
  fi

  sleep 15
done
