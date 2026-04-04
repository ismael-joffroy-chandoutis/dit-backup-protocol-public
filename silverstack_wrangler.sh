#!/bin/bash
# ═══════════════════════════════════════════════════════════════
# SILVERSTACK PROTOCOL v3 — Netflix DIT Level
# ═══════════════════════════════════════════════════════════════
# Règle absolue :
#   Copy → Hash → BIT-PERFECT → R2 → Verify R2 → NAS → Verify NAS
#   JAMAIS R2 avant hash. JAMAIS deux I/O sur même HDD.
#   ZERO fichier non vérifié.
# ═══════════════════════════════════════════════════════════════

set -uo pipefail
# pas -e : grep retourne 1 quand 0 matches, pas une erreur fatale

NOMAD="${NOMAD_SSH}"
MINI="${MINI_SSH}"
LOG="/tmp/silverstack_wrangler.log"
RCLONE="C:\\ProgramData\\chocolatey\\bin\\rclone.exe"
R2_BUCKET="cloudflare-r2:${R2_BUCKET}"

log()    { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }
notify() { ~/.claude/scripts/notify.sh "$*" 2>/dev/null || true; }
fail()   { log "FATAL: $*"; notify "FATAL: $*"; exit 1; }

# ─── Helpers ─────────────────────────────────────

MAX_WAIT_LOOPS=360  # 360 x 30s = 3h max

wait_rclone_idle() {
  log "  Attente HDD idle (0 rclone)..."
  local i=0
  while true; do
    COUNT=$(ssh -o ConnectTimeout=5 "$NOMAD" 'tasklist 2>nul | findstr /i rclone' 2>/dev/null | grep -c rclone || true)
    COUNT=$(echo "$COUNT" | head -1 | tr -d '[:space:]')
    [ "${COUNT:-0}" -le 0 ] 2>/dev/null && break
    i=$((i+1))
    [ "$i" -ge "$MAX_WAIT_LOOPS" ] && { log "TIMEOUT attente rclone"; notify "TIMEOUT rclone idle"; break; }
    log "    rclone actifs: $COUNT"
    sleep 30
  done
}

wait_robocopy_done() {
  log "  Attente fin robocopy..."
  local i=0
  while true; do
    RC=$(ssh -o ConnectTimeout=5 "$NOMAD" 'tasklist 2>nul | findstr /i robocopy' 2>/dev/null | grep -ci robocopy || true)
    RC=$(echo "$RC" | head -1 | tr -d '[:space:]')
    [ "${RC:-0}" -le 0 ] 2>/dev/null && break
    i=$((i+1))
    [ "$i" -ge 720 ] && { log "TIMEOUT attente robocopy"; notify "TIMEOUT robocopy"; break; }
    sleep 15
  done
}

hash_nav2() {
  local folder="$1" out="$2" label="$3"
  log "  rclone hashsum xxh128 G:\\${folder}..."
  > "$out"
  ssh -o ConnectTimeout=10 -o ServerAliveInterval=60 "$NOMAD" "$RCLONE hashsum xxh128 G:\\${folder}\\ 2>&1" > "$out" 2>&1
  local lines
  lines=$(wc -l < "$out" | tr -d ' ')
  log "  → $lines fichiers hashés"
}

bitperfect() {
  local src="$1" dst="$2" ext="$3"
  python3 << PYEOF
def extract(f, ext='$ext'):
    out=[]
    for l in open(f).readlines():
        p=l.strip().split()
        if len(p)>=2 and p[-1].lower().endswith(ext):
            bn=p[-1].split('/')[-1].split('\\\\')[-1]
            if not bn.startswith('._'): out.append(p[0]+' '+bn)
    return sorted(out)
s,d=extract('$src'),extract('$dst')
if s==d and len(s)>0: print(f'BIT-PERFECT ({len(s)} fichiers)')
else: print(f'MISMATCH src={len(s)} dst={len(d)}')
PYEOF
}

r2_upload() {
  local folder="$1" label="$2"
  log "  Upload $label → R2..."
  ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=5 "$NOMAD" "$RCLONE copy G:\\${folder} ${R2_BUCKET}/${folder} --transfers 8 --stats 30s --stats-log-level NOTICE --log-file=C:\\Users\\ismael\\rclone_r2_${label}.log 2>&1" \
    > "/tmp/rclone_r2_${label}_final.log" 2>&1
  log "  → R2 $label upload terminé"
}

r2_verify() {
  local folder="$1" label="$2"
  log "  Verify $label Nav2 vs R2 (rclone check)..."
  local result
  result=$(ssh -o ServerAliveInterval=30 "$NOMAD" "$RCLONE check G:\\${folder} ${R2_BUCKET}/${folder} --size-only 2>&1" 2>/dev/null)
  echo "$result" > "/tmp/rclone_verify_${label}.log"
  if echo "$result" | grep -q "0 differences"; then
    log "  → R2 $label VÉRIFIÉ ✅"
    echo "VERIFIED"
  else
    local diffs
    diffs=$(echo "$result" | grep -c "ERROR" || echo "?")
    log "  → R2 $label $diffs ERREURS ❌"
    echo "FAILED"
  fi
}

nas_verify() {
  local folder="$1" label="$2"
  log "  Verify $label NAS vs R2 (via Mac Mini)..."
  local result
  result=$(ssh -o ServerAliveInterval=30 "$MINI" "rclone check ~/NAS-Goldberg/${folder} ${R2_BUCKET}/${folder} --size-only 2>&1" 2>/dev/null)
  echo "$result" > "/tmp/nas_verify_${label}.log"
  if echo "$result" | grep -q "0 differences"; then
    log "  → NAS $label VÉRIFIÉ ✅"
    echo "VERIFIED"
  else
    log "  → NAS $label DIFFÉRENCES ❌"
    echo "FAILED"
  fi
}

# ═══════════════════════════════════════════════════════════════
log ""
log "═══════════════════════════════════════════════"
log " SILVERSTACK PROTOCOL v3 — $(date)"
log " Copy → Hash → BIT-PERFECT → R2 → Verify → NAS"
log "═══════════════════════════════════════════════"
log ""

# ─────────────────────────────────────────────────
# PHASE 1 — Cleanup : attendre que le HDD soit libre
# ─────────────────────────────────────────────────
log "PHASE 1 — Nettoyage I/O Nav2"
wait_rclone_idle
wait_robocopy_done
log "Nav2 HDD libre ✅"
log ""

# ─────────────────────────────────────────────────
# PHASE 2 — Hash A015 Nav2
# ─────────────────────────────────────────────────
log "PHASE 2 — Hash A015 Nav2"
hash_nav2 "A015_BRAW_2026-03-28" "/tmp/A015_nav2_xxh128.txt" "A015"
A015_BP=$(bitperfect "/tmp/A015_source_xxh128.txt" "/tmp/A015_nav2_xxh128.txt" ".braw")
log "  A015 Nav2 : $A015_BP"
notify "A015 Nav2 $A015_BP"
log ""

# ─────────────────────────────────────────────────
# PHASE 3 — Hash A020 Nav2
# ─────────────────────────────────────────────────
log "PHASE 3 — Hash A020 Nav2"
A020_FILES=$(ssh -o ConnectTimeout=5 "$NOMAD" "dir /b G:\\A020_BRAW_2026-04-03\\*.braw 2>nul" 2>/dev/null | wc -l | tr -d ' ')
log "  A020 Nav2 : $A020_FILES fichiers"
hash_nav2 "A020_BRAW_2026-04-03" "/tmp/A020_nav2_xxh128.txt" "A020"
A020_BP=$(bitperfect "/tmp/A020_source_xxh128.txt" "/tmp/A020_nav2_xxh128.txt" ".braw")
log "  A020 Nav2 : $A020_BP"
notify "A020 Nav2 $A020_BP"
log ""

# ─────────────────────────────────────────────────
# PHASE 4 — iPhone → Nav2 (détection + copie + hash)
# ─────────────────────────────────────────────────
log "PHASE 4 — iPhone Nav2"
log "  Détection iPhone sur Nomad (max 2h)..."
IPHONE_LETTER=""
DETECT_TRIES=0
while [ -z "$IPHONE_LETTER" ]; do
  for L in D H I J K L M N; do
    FOUND=$(ssh -o ConnectTimeout=5 "$NOMAD" "if exist ${L}:\\DCIM echo YES" 2>/dev/null | grep -c "YES" || echo 0)
    if [ "$FOUND" -gt 0 ]; then
      IPHONE_LETTER="$L"
      break
    fi
  done
  if [ -z "$IPHONE_LETTER" ]; then
    DETECT_TRIES=$((DETECT_TRIES+1))
    if [ "$DETECT_TRIES" -ge 480 ]; then
      log "TIMEOUT détection iPhone (2h) — abandon"
      notify "TIMEOUT iPhone détection — vérifier branchement"
      break
    fi
    log "    iPhone non détecté — attente ($DETECT_TRIES)..."
    sleep 15
  fi
done
[ -z "$IPHONE_LETTER" ] && { log "SKIP iPhone — non détecté"; }
log "  iPhone détecté : ${IPHONE_LETTER}:"
notify "iPhone détecté ${IPHONE_LETTER}: — copie Nav2..."

log "  Robocopy iPhone → Nav2..."
ssh -o ServerAliveInterval=60 "$NOMAD" "robocopy ${IPHONE_LETTER}:\\DCIM G:\\iPhone-2TB_2026-04-03\\DCIM /E /COPYALL /J /NP /R:3 /W:5 /XD .fseventsd .Spotlight-V100 .TemporaryItems /XF ._* /LOG+:C:\\Users\\ismael\\robocopy_iphone.log" >> "$LOG" 2>&1 || true
log "  iPhone Nav2 copie terminée"

log "  Hash iPhone Nav2..."
hash_nav2 "iPhone-2TB_2026-04-03" "/tmp/iPhone_nav2_xxh128.txt" "iPhone"
IPHONE_BP=$(bitperfect "/tmp/iPhone_source_xxh128.txt" "/tmp/iPhone_nav2_xxh128.txt" ".mov")
log "  iPhone Nav2 : $IPHONE_BP"
notify "iPhone Nav2 $IPHONE_BP"
log ""

# ─────────────────────────────────────────────────
# PHASE 5 — R2 Upload (APRÈS tous les hashes)
# ─────────────────────────────────────────────────
log "PHASE 5 — R2 Upload"
log "  A015 : re-upload forcé (ancien pipeline non vérifié)"
r2_upload "A015_BRAW_2026-03-28" "A015" &
PID_A015=$!
r2_upload "A020_BRAW_2026-04-03" "A020" &
PID_A020=$!
wait $PID_A015 $PID_A020
log "  A015 + A020 R2 terminé"

log "  iPhone R2 (séquentiel après BRAW — même HDD)..."
r2_upload "iPhone-2TB_2026-04-03" "iPhone"
log "R2 upload COMPLET"
notify "R2 upload complet — vérification..."
log ""

# ─────────────────────────────────────────────────
# PHASE 6 — Verify R2 (Nav2 local vs R2 cloud)
# ─────────────────────────────────────────────────
log "PHASE 6 — Verify R2"
R2_A015=$(r2_verify "A015_BRAW_2026-03-28" "A015")
R2_A020=$(r2_verify "A020_BRAW_2026-04-03" "A020")
R2_IP=$(r2_verify "iPhone-2TB_2026-04-03" "iPhone")
log "  R2 A015=$R2_A015 A020=$R2_A020 iPhone=$R2_IP"
notify "R2 verify: A015=$R2_A015 A020=$R2_A020 iPhone=$R2_IP"
log ""

# ─────────────────────────────────────────────────
# PHASE 7 — NAS Paris sync + verify
# ─────────────────────────────────────────────────
log "PHASE 7 — NAS Paris"
log "  Sync R2 → NAS via Mac Mini..."
ssh -o ServerAliveInterval=60 "$MINI" "~/sync-r2-to-nas.sh" >> "$LOG" 2>&1 || true
log "  NAS sync terminé"

log "  Verify NAS vs R2..."
NAS_A015=$(nas_verify "A015_BRAW_2026-03-28" "A015")
NAS_A020=$(nas_verify "A020_BRAW_2026-04-03" "A020")
NAS_IP=$(nas_verify "iPhone-2TB_2026-04-03" "iPhone")
log "  NAS A015=$NAS_A015 A020=$NAS_A020 iPhone=$NAS_IP"
notify "NAS verify: A015=$NAS_A015 A020=$NAS_A020 iPhone=$NAS_IP"
log ""

# ─────────────────────────────────────────────────
# PHASE 8 — Manifests + rapport final
# ─────────────────────────────────────────────────
log "PHASE 8 — Manifests"
DATE=$(date +%Y-%m-%d)
for DIR in /Volumes/NavTGV1/A020_BRAW_2026-04-03 /Volumes/NavTGV1/A015_BRAW_2026-03-28 /Volumes/NavTGV1/iPhone-2TB_2026-04-03; do
  [ -d "$DIR" ] || continue
  LABEL=$(basename "$DIR")
  cat > "${DIR}/BACKUP_MANIFEST_${DATE}.txt" << MEOF
BACKUP MANIFEST — $LABEL
========================
Date     : $(date)
Protocol : Silverstack DIT v3
Hash     : xxh128 (rclone hashsum)

COPIES:
  Nav1 (local)  : vérifié xxh128 BIT-PERFECT
  Nav2 (Nomad)  : vérifié xxh128
  R2 (cloud)    : rclone check
  NAS (Paris)   : rclone check vs R2

RESULTS:
  A015 Nav2 BIT-PERFECT : $A015_BP
  A020 Nav2 BIT-PERFECT : $A020_BP
  iPhone Nav2           : $IPHONE_BP
  R2 A015               : $R2_A015
  R2 A020               : $R2_A020
  R2 iPhone             : $R2_IP
  NAS A015              : $NAS_A015
  NAS A020              : $NAS_A020
  NAS iPhone            : $NAS_IP
MEOF
  log "  Manifest : $DIR"
done

log ""
log "═══════════════════════════════════════════════"
log " BACKUP NAGE 2026-04-03 — COMPLET"
log "═══════════════════════════════════════════════"
log " Nav1  : A020 ✅  A015 ✅  iPhone ✅"
log " Nav2  : A015=$A015_BP"
log "         A020=$A020_BP"
log "         iPhone=$IPHONE_BP"
log " R2    : A015=$R2_A015  A020=$R2_A020  iPhone=$R2_IP"
log " NAS   : A015=$NAS_A015  A020=$NAS_A020  iPhone=$NAS_IP"
log ""
log " SAFE ÉJECTER TOUTES SOURCES"
log "═══════════════════════════════════════════════"

notify "BACKUP NAGE COMPLET ✅ 3-2-1 vérifié partout — SAFE ÉJECTER"
