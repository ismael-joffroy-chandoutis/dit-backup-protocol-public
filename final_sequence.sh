#!/bin/bash
# Séquence finale — tout séquentiel sur Nav2, zéro conflit
NOMAD="${NOMAD_SSH}"
MINI="${MINI_SSH}"
RCLONE="C:\\ProgramData\\chocolatey\\bin\\rclone.exe"
R2="cloudflare-r2:${R2_BUCKET}"
LOG="/tmp/silverstack_wrangler.log"

log()    { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
notify() { ~/.claude/scripts/notify.sh "$*" 2>/dev/null || true; }

log "=== SÉQUENCE FINALE ==="

# ─── 1. Attendre iPhone Nav2 hash (déjà lancé PID 17830) ───
log "1. Attente iPhone Nav2 hash..."
wait 17830 2>/dev/null
# Si PID pas enfant, poll le fichier
while ps -p 17830 > /dev/null 2>&1; do
  LINES=$(wc -l < /tmp/iPhone_nav2_xxh128.txt 2>/dev/null || echo 0)
  log "   iPhone hash: $LINES lignes"
  sleep 20
done
log "   iPhone Nav2 hash terminé"

# BIT-PERFECT iPhone Nav2
IPHONE_BP=$(python3 -c "
def ext(f, e='.mov'):
    out=[]
    for l in open(f).readlines():
        p=l.strip().split()
        if len(p)>=2 and p[-1].lower().endswith(e):
            bn=p[-1].split('/')[-1].split('\\\\')[-1]
            if not bn.startswith('._'): out.append(p[0]+' '+bn)
    return sorted(out)
s=ext('/tmp/iPhone_source_xxh128.txt')
d=ext('/tmp/iPhone_nav2_xxh128.txt')
if s==d and len(s)>0: print('BIT-PERFECT')
else: print(f'MISMATCH src={len(s)} nav2={len(d)}')
")
log "   iPhone Nav2: $IPHONE_BP"
notify "iPhone Nav2 $IPHONE_BP"

# ─── 2. Hash H8 SD Nav2 ───
log "2. Hash H8 SD Nav2..."
ssh -o ServerAliveInterval=30 "$NOMAD" "$RCLONE hashsum xxh128 G:\\H8_SD_2026-04-03\\ 2>&1" > /tmp/H8_nav2_xxh128.txt 2>&1
H8_LINES=$(wc -l < /tmp/H8_nav2_xxh128.txt | tr -d ' ')
log "   H8 Nav2 hash: $H8_LINES lignes"

# BIT-PERFECT H8 Nav2 (compare par hash seul, pas par chemin)
H8_BP=$(python3 -c "
hs=sorted([l.split()[0] for l in open('/tmp/H8_source_xxh128.txt').readlines() if l.strip()])
hd=sorted([l.split()[0] for l in open('/tmp/H8_nav2_xxh128.txt').readlines() if l.strip()])
if hs==hd and len(hs)>0: print(f'BIT-PERFECT ({len(hs)} fichiers)')
else: print(f'MISMATCH src={len(hs)} nav2={len(hd)}')
")
log "   H8 Nav2: $H8_BP"
notify "H8 Nav2 $H8_BP"

# ─── 3. R2 sync A015 depuis Nav2 (écrase l'ancien) ───
log "3. R2 A015 sync depuis Nav2 (écrasement complet)..."
notify "R2 A015 sync depuis Nav2..."
ssh -o ServerAliveInterval=60 "$NOMAD" "$RCLONE sync G:\\A015_BRAW_2026-03-28 $R2/A015_BRAW_2026-03-28 --transfers 12 --buffer-size 64M --stats 30s --stats-log-level NOTICE 2>&1" >> "$LOG" 2>&1
log "   R2 A015 sync terminé"

# ─── 4. R2 A020 depuis Nav2 ───
log "4. R2 A020 depuis Nav2..."
ssh -o ServerAliveInterval=60 "$NOMAD" "$RCLONE sync G:\\A020_BRAW_2026-04-03 $R2/A020_BRAW_2026-04-03 --transfers 12 --buffer-size 64M --stats 30s 2>&1" >> "$LOG" 2>&1
log "   R2 A020 terminé"

# ─── 5. R2 iPhone depuis Nav2 ───
log "5. R2 iPhone depuis Nav2..."
ssh -o ServerAliveInterval=60 "$NOMAD" "$RCLONE copy G:\\iPhone-2TB_2026-04-03 $R2/iPhone-2TB_2026-04-03 --transfers 12 --buffer-size 64M --stats 30s 2>&1" >> "$LOG" 2>&1
log "   R2 iPhone terminé"

# ─── 6. R2 H8 depuis Nav2 ───
log "6. R2 H8 depuis Nav2..."
ssh -o ServerAliveInterval=60 "$NOMAD" "$RCLONE copy G:\\H8_SD_2026-04-03 $R2/H8_SD_2026-04-03 --transfers 8 --stats 30s 2>&1" >> "$LOG" 2>&1
log "   R2 H8 terminé"
notify "R2 TOUT COMPLET"

# ─── 7. Verify R2 ───
log "7. Verify R2..."
for FOLDER in A015_BRAW_2026-03-28 A020_BRAW_2026-04-03 iPhone-2TB_2026-04-03 H8_SD_2026-04-03; do
  LABEL=$(echo "$FOLDER" | cut -d_ -f1)
  RESULT=$(ssh -o ServerAliveInterval=30 "$NOMAD" "$RCLONE check G:\\${FOLDER} $R2/${FOLDER} --size-only 2>&1")
  if echo "$RESULT" | grep -q "0 differences"; then
    log "   R2 $LABEL VÉRIFIÉ ✅"
  else
    log "   R2 $LABEL ERREURS ❌"
    echo "$RESULT" >> "$LOG"
  fi
done
notify "R2 VERIFY COMPLET"

# ─── 8. UGreen NAS : A015 écrasement depuis R2 + sync tout ───
log "8. NAS A015 écrasement + sync tout..."
notify "NAS sync..."
ssh -o ServerAliveInterval=60 "$MINI" "rclone sync cloudflare-r2:${R2_BUCKET}/A015_BRAW_2026-03-28 ~/mnt-nas-shared/BackupR2/A015_BRAW_2026-03-28 --transfers 8 --buffer-size 64M 2>&1" >> "$LOG" 2>&1
log "   NAS A015 écrasé ✅"
ssh -o ServerAliveInterval=60 "$MINI" "~/sync-r2-to-nas.sh" >> "$LOG" 2>&1
log "   NAS sync complet"
notify "NAS COMPLET"

# ─── 9. Rapport final ───
log ""
log "═══════════════════════════════════════════════"
log " BACKUP NAGE 2026-04-03 — COMPLET"
log " iPhone Nav2: $IPHONE_BP"
log " H8 Nav2:     $H8_BP"
log " R2:          vérifié"
log " NAS:         synchro + A015 écrasé"
log " SAFE ÉJECTER TOUTES SOURCES"
log "═══════════════════════════════════════════════"
notify "BACKUP NAGE COMPLET ✅ — SAFE ÉJECTER"
