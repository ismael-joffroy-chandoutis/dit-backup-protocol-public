#!/usr/bin/env python3
"""Backup Dashboard Server — lit les logs rsync/rclone et sert un JSON de statut."""

import json, re, os, glob, shutil, tempfile, time, subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse, parse_qs

# Cache SSH calls — (result, timestamp)
_ssh_cache: dict = {}

XP_FILE = "/tmp/backup_dashboard/xp.json"
ANNOTATIONS_FILE = "/tmp/backup_dashboard/annotations.json"
PROXY_DIR = "/tmp/backup_proxies"

LEVELS = [
    (0,     "Trainee DIT"),
    (500,   "Junior DIT"),
    (2000,  "Senior DIT"),
    (5000,  "Data Wrangler"),
    (12000, "Netflix DIT"),
    (25000, "Hollywood Legend"),
]

def get_level(xp):
    level_name = LEVELS[0][1]
    for threshold, name in LEVELS:
        if xp >= threshold:
            level_name = name
    next_threshold = None
    for threshold, _ in LEVELS:
        if xp < threshold:
            next_threshold = threshold
            break
    return level_name, next_threshold

def load_xp():
    if os.path.exists(XP_FILE):
        try:
            with open(XP_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"total_xp": 0, "total_gb": 0, "achievements": []}

def save_xp(data):
    # Écriture atomique — évite la corruption si power loss
    tmp = XP_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, XP_FILE)

def load_annotations():
    if os.path.exists(ANNOTATIONS_FILE):
        try:
            with open(ANNOTATIONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_annotations(data):
    tmp = ANNOTATIONS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ANNOTATIONS_FILE)

def parse_rsync_log(path):
    """Progression rsync globale via to-chk=X/Y (fichiers restants).
    Utilise le per-file % uniquement pour la vitesse et l'ETA."""
    try:
        content = Path(path).read_text(errors="replace")
        lines = content.split("\n")

        # Vérification terminé (résumé final rsync)
        for line in reversed(lines):
            if "total size is" in line:
                return {"percent": 100, "speed": "—", "eta": "0:00:00", "active": False, "done": True}

        # Progression globale : to-chk=remaining/total
        global_pct = None
        for line in reversed(lines):
            m = re.search(r'to-chk=(\d+)/(\d+)', line)
            if m:
                remaining, total = int(m.group(1)), int(m.group(2))
                done_files = total - remaining
                global_pct = int(done_files / total * 100) if total else 0
                break

        # Vitesse + ETA du fichier en cours (per-file)
        speed, eta = "—", "—"
        for line in reversed(lines):
            m = re.search(r'\d+%\s+([\d\.]+\w+B/s)\s+([\d:]+)', line)
            if m:
                speed, eta = m.group(1), m.group(2)
                break

        if global_pct is not None:
            return {"percent": global_pct, "speed": speed, "eta": eta, "active": True}

    except Exception:
        pass
    return None

def parse_rclone_log(path):
    """Extrait les stats rclone (Transferred, speed)."""
    try:
        content = Path(path).read_text(errors="replace")
        for line in reversed(content.split("\n")):
            if "Transferred:" in line and "%" in line:
                m = re.search(r'Transferred:\s+([\d\.]+\s*\w+)\s*/\s*([\d\.]+\s*\w+),\s*(\d+)%,\s*([\d\.]+\s*\w+Bits/s)', line)
                if m:
                    return {
                        "transferred": m.group(1),
                        "total": m.group(2),
                        "percent": int(m.group(3)),
                        "speed": m.group(4),
                        "active": True
                    }
    except Exception:
        pass
    return None

def count_nav1_files(folder):
    try:
        return len(list(Path(folder).glob("*.braw")))
    except Exception:
        return 0

def xxhsum_running():
    """Vérifie si un processus xxhsum est actif."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "args"], capture_output=True, text=True, timeout=5).stdout
        return "xxhsum" in out and "grep" not in out.split("xxhsum")[0][-20:]
    except Exception:
        return False

def rclone_hashsum_running():
    """Vérifie si rclone hashsum tourne (pour A015 Nav2)."""
    try:
        out = subprocess.check_output(["ps", "-A", "-o", "args"], text=True, stderr=subprocess.DEVNULL)
        return "rclone" in out and "hashsum" in out
    except Exception:
        return False

MINI = "user@MINI_IP"
NOMAD_LAN = "192.168.4.43"
NOMAD_TS  = "100.82.222.123"

def _nomad_ip() -> str:
    """Retourne l'IP Nomad accessible (LAN préféré, Tailscale sinon). Cache 60s."""
    cache_key = "__nomad_ip__"
    now = time.time()
    if cache_key in _ssh_cache:
        val, ts = _ssh_cache[cache_key]
        if now - ts < 60:
            return val
    # Test LAN avec timeout court
    r = subprocess.run(
        f"ping -c 1 -W 2 {NOMAD_LAN}",
        shell=True, capture_output=True, timeout=5
    )
    ip = NOMAD_LAN if r.returncode == 0 else NOMAD_TS
    _ssh_cache[cache_key] = (ip, now)
    return ip

def _ssh(cmd: str, ttl: int = 15) -> str:
    """SSH vers Nomad avec cache TTL secondes. Auto-sélectionne LAN vs Tailscale."""
    key = cmd
    now = time.time()
    if key in _ssh_cache:
        val, ts = _ssh_cache[key]
        if now - ts < ttl:
            return val
    try:
        import shlex
        ip = _nomad_ip()
        ssh_cmd = f"ssh -o ConnectTimeout=8 -o StrictHostKeyChecking=no ismael@{ip} {shlex.quote(cmd)} 2>/dev/null"
        result = subprocess.run(ssh_cmd, shell=True, capture_output=True, timeout=20)
        out = result.stdout.decode("cp850", errors="replace")
        _ssh_cache[key] = (out, now)
        return out
    except Exception:
        return ""

def nas_disk_usage():
    """Espace disque NAS UGreen via Mac Mini SSH (cache 60s)."""
    key = "__nas_df__"
    now = time.time()
    if key in _ssh_cache:
        val, ts = _ssh_cache[key]
        if now - ts < 60:
            return val
    try:
        result = subprocess.run(
            f"ssh -o ConnectTimeout=3 -o ServerAliveInterval=2 {MINI} 'df -k ~/mnt-nas-shared/ 2>/dev/null' 2>/dev/null",
            shell=True, capture_output=True, timeout=8
        )
        out = result.stdout.decode("utf-8", errors="replace")
        for line in out.splitlines():
            if "mnt-nas" in line or "192.168" in line:
                parts = line.split()
                if len(parts) >= 4:
                    total_kb = int(parts[1])
                    used_kb  = int(parts[2])
                    free_kb  = int(parts[3])
                    info = {
                        "used_gb":  round(used_kb  / 1e6, 1),
                        "total_gb": round(total_kb / 1e6, 1),
                        "free_gb":  round(free_kb  / 1e6, 1),
                        "percent":  int(used_kb / total_kb * 100) if total_kb else 0,
                    }
                    _ssh_cache[key] = (info, now)
                    return info
    except Exception:
        pass
    return None

def nav2_disk_usage():
    """Espace disque G: sur Nomad via SSH (cache 30s)."""
    out = _ssh("fsutil volume diskfree G:", ttl=30)
    if not out:
        return None
    try:
        # Extract all lines with "octets" and a number
        bytes_re = re.compile(r':\s+([\d\s\xa0]+)\s+\(')
        lines = out.splitlines()
        total = free = None
        for line in lines:
            m = bytes_re.search(line)
            if not m:
                continue
            val = int(re.sub(r'[\s\xa0]', '', m.group(1)))
            # French: "Nombre total d'octets libres" comes before "Nombre total d'octets"
            if ("libres" in line or "free" in line.lower()) and free is None:
                free = val
            elif ("octets" in line or "bytes" in line.lower()) and "libres" not in line and "libre" not in line and total is None:
                total = val
        if total and free:
            used = total - free
            return {
                "used_gb":  round(used  / 1e9, 1),
                "total_gb": round(total / 1e9, 1),
                "free_gb":  round(free  / 1e9, 1),
                "percent":  int(used / total * 100) if total else 0,
            }
    except Exception:
        pass
    return None

def a020_nav2_progress(total_braw=25):
    """Lit C:\\Users\\ismael\\robocopy_a020.log sur Nomad via SSH (cache 10s).
    Retourne dict status/percent/done/speed/eta compatible parse_robocopy_log."""
    # Vérifier d'abord si marqueur de fin dans le pipeline log
    pipeline = Path("/tmp/a020_iphone_pipeline.log")
    if pipeline.exists() and "A020 Nav2 COMPLET" in pipeline.read_text(errors="replace"):
        return {"status": "done", "percent": 100, "done": total_braw, "total": total_braw, "speed": "—", "eta": "—"}

    # Compter les fichiers BRAW dans la destination via dir /b (compte les lignes côté Mac)
    dir_out = _ssh('dir /b G:\\A020_BRAW_2026-04-03\\*.braw 2>nul', ttl=10)
    done = len([l for l in dir_out.splitlines() if l.strip()])
    finished = done >= total_braw

    if finished and done > 0:
        return {"status": "done", "percent": 100, "done": done, "total": total_braw, "speed": "—", "eta": "—"}
    elif done > 0:
        # Calcul ETA: basé sur le compte de fichiers et le timestamp pipeline
        # Vitesse estimée via taux de copie fichiers/sec
        # Utiliser mtime du log comme base de temps
        try:
            pipeline_text = pipeline.read_text(errors="replace") if pipeline.exists() else ""
            ts_re = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]')
            start_ts = None
            for l in pipeline_text.split("\n"):
                m2 = ts_re.match(l)
                if m2 and "A020" in l and "Nav2" in l:
                    start_ts = datetime.strptime(m2.group(1), "%H:%M:%S").replace(
                        year=datetime.now().year, month=datetime.now().month, day=datetime.now().day)
                    break
            if start_ts:
                elapsed = (datetime.now() - start_ts).total_seconds()
                if elapsed > 0 and done > 0:
                    rate = done / elapsed
                    remaining = max(total_braw - done, 0)
                    eta_sec = int(remaining / rate)
                    h, m3, s = eta_sec // 3600, (eta_sec % 3600) // 60, eta_sec % 60
                    eta_str = f"{h}:{m3:02d}:{s:02d}" if h else f"{m3}:{s:02d}"
                    spd = "local"
                    pct = min(int(done / total_braw * 100), 99)
                    return {"status": "active", "percent": pct, "done": done, "total": total_braw,
                            "speed": spd, "eta": eta_str}
        except Exception:
            pass
        pct = min(int(done / total_braw * 100), 99)
        return {"status": "active", "percent": pct, "done": done, "total": total_braw, "speed": "local", "eta": "—"}
    else:
        return {"status": "active", "percent": 0, "done": 0, "total": total_braw, "speed": "—", "eta": "—"}

def get_hash_progress(manifest_path, total_braw, running_fn=None):
    """Progression hashing. running_fn() indique si le process de hash est actif.
    total_braw = nb clips attendus (manifest peut en avoir plus avec sidecars)."""
    if running_fn is None:
        running_fn = xxhsum_running
    try:
        p = Path(manifest_path)
        running = running_fn()

        if not p.exists():
            return {"done": 0, "total": total_braw, "percent": 0,
                    "status": "active" if running else "waiting"}

        lines = [l for l in p.read_text(errors="replace").splitlines() if l.strip()]
        done = len(lines)

        if done >= total_braw and done > 0:
            # Toutes les lignes attendues : terminé (même si un autre xxhsum tourne)
            status = "done"
            total = done
        elif running and done > 0:
            # En cours : process actif + manifest non complet
            status = "active"
            total = total_braw
        elif not running and done > 0:
            # Process terminé + manifest a du contenu = done
            status = "done"
            total = done
        else:
            # Pas de process, manifest vide : en attente
            status = "waiting"
            total = total_braw

        pct = 100 if status == "done" else int(done / total * 100) if total else 0

        # Speed + ETA via cache progressive (taux réel entre 2 mesures)
        speed_str, eta_str = "—", "—"
        if status == "active" and done > 0:
            cache_key_h = f"__hash_rate_{manifest_path}__"
            now_h = time.time()
            if cache_key_h in _ssh_cache:
                prev_done_h, prev_ts_h = _ssh_cache[cache_key_h]
                dt_h = now_h - prev_ts_h
                if dt_h > 10 and done > prev_done_h:
                    rate = (done - prev_done_h) / dt_h
                    remaining = max(total_braw - done, 0)
                    eta_sec = int(remaining / rate) if rate > 0 else 0
                    h2, m2, s2 = eta_sec // 3600, (eta_sec % 3600) // 60, eta_sec % 60
                    eta_str = f"{h2}:{m2:02d}:{s2:02d}" if h2 else f"{m2}:{s2:02d}"
                    speed_str = f"{rate * 60:.1f} f/min"
                    _ssh_cache[cache_key_h] = (done, now_h)
                elif dt_h > 30:
                    _ssh_cache[cache_key_h] = (done, now_h)
            else:
                _ssh_cache[cache_key_h] = (done, now_h)

        return {"done": done, "total": total, "percent": pct, "status": status,
                "speed": speed_str, "eta": eta_str}
    except Exception:
        return {"done": 0, "total": total_braw, "percent": 0, "status": "waiting",
                "speed": "—", "eta": "—"}

def disk_usage(path):
    """Retourne (used_gb, total_gb, free_gb) ou None si non monté."""
    try:
        usage = shutil.disk_usage(path)
        return {
            "used_gb":  round(usage.used  / 1e9, 1),
            "total_gb": round(usage.total / 1e9, 1),
            "free_gb":  round(usage.free  / 1e9, 1),
            "percent":  int(usage.used / usage.total * 100),
        }
    except Exception:
        return None

def parse_robocopy_log(log_path, total_braw, finished_marker):
    """Progression robocopy Nomad : compte les 'Nouveau fichier *.braw' dans le log pipeline.
    finished_marker : chaîne exacte indiquant la fin de cette copie spécifique."""
    try:
        content = Path(log_path).read_text(errors="replace")
        lines = content.split("\n")
        # Fichiers copiés (robocopy log embed dans pipeline log)
        done = sum(1 for l in lines if "Nouveau fichier" in l and ".braw" in l.lower()
                   and "._" not in l)
        # Terminé uniquement si marqueur spécifique présent
        finished = finished_marker in content
        # Actif si pipeline en cours et pas encore fini
        active = bool(content.strip()) and not finished

        if finished:
            return {"status": "done", "percent": 100, "done": max(done, total_braw), "total": total_braw, "speed": "—", "eta": "—"}
        elif active and done > 0:
            pct = min(int(done / total_braw * 100), 99)
            return {"status": "active", "percent": pct, "done": done, "total": total_braw, "speed": "local", "eta": "—"}
        elif active:
            return {"status": "active", "percent": 0, "done": 0, "total": total_braw, "speed": "local", "eta": "—"}
    except Exception:
        pass
    return None

def calc_eta_from_robocopy(log_path, total_braw, finished_marker):
    """Calcule ETA et vitesse à partir des timestamps pipeline + fichiers copiés robocopy."""
    from datetime import timedelta
    try:
        content = Path(log_path).read_text(errors="replace")
        if finished_marker in content:
            return "—", "—"
        lines = content.split("\n")
        ts_re = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]')
        braw_lines = [l for l in lines if "Nouveau fichier" in l and ".braw" in l.lower() and "._" not in l]
        done = len(braw_lines)
        if done == 0:
            return "—", "—"

        # Taille totale copiée (GB) pour vitesse
        size_re = re.compile(r'([\d.]+)\s+[gG]')
        total_gb = sum(float(m.group(1)) for l in braw_lines for m in [size_re.search(l)] if m)

        # Start time = dernière ligne horodatée AVANT le premier "Nouveau fichier"
        first_braw_idx = next(i for i, l in enumerate(lines) if "Nouveau fichier" in l and ".braw" in l.lower() and "._" not in l)
        start_ts = None
        for l in lines[:first_braw_idx]:
            m = ts_re.match(l)
            if m:
                start_ts = datetime.strptime(m.group(1), "%H:%M:%S").replace(
                    year=datetime.now().year, month=datetime.now().month, day=datetime.now().day)

        now = datetime.now().replace(microsecond=0)
        if start_ts and start_ts > now:
            start_ts -= timedelta(days=1)

        if start_ts:
            elapsed = (now - start_ts).total_seconds()
            if elapsed > 0 and done > 0:
                rate = done / elapsed
                remaining = max(total_braw - done, 0)
                eta_sec = int(remaining / rate)
                h, m2, s = eta_sec // 3600, (eta_sec % 3600) // 60, eta_sec % 60
                eta_str = f"{h}:{m2:02d}:{s:02d}" if h else f"{m2}:{s:02d}"
                speed_str = f"{total_gb / elapsed * 1000:.0f} MB/s" if total_gb > 0 else "local"
                return eta_str, speed_str
    except Exception:
        pass
    return "—", "—"

def iphone_nav1_status():
    """Statut copie iPhone → Nav1 depuis rsync log."""
    log = Path("/tmp/rsync_iphone_nav1.log")
    if not log.exists():
        return "waiting", 0, "—", "—"
    try:
        content = log.read_text(errors="replace")
        # Terminé si ligne finale rsync présente
        if "total size is" in content and "speedup is" in content:
            return "done", 100, "—", "—"
        # Actif : lire la progression to-chk=X/Y
        m = re.search(r'to-chk=(\d+)/(\d+)', content)
        if m:
            remaining, total = int(m.group(1)), int(m.group(2))
            done = total - remaining
            pct = int(done / total * 100) if total else 0
            # Vitesse depuis dernière ligne de vitesse
            speeds = re.findall(r'([\d.]+)MB/s', content)
            speed = f"{float(speeds[-1]):.0f} MB/s" if speeds else "—"
            return "active", pct, speed, "—"
    except Exception:
        pass
    return "waiting", 0, "—", "—"

_R2_START = {"A015": "R2 A015 sync depuis Nav2", "A020": "R2 A020 depuis Nav2",
             "iPhone": "R2 iPhone depuis Nav2", "H8": "R2 H8 depuis Nav2"}
_R2_END   = {"A015": "R2 A015 sync terminé", "A020": "R2 A020 terminé",
             "iPhone": "R2 iPhone terminé", "H8": "R2 H8 terminé"}

def _r2_step_status(label):
    """Status R2 par source. Marqueurs EXACTS du script final_sequence.sh."""
    try:
        wlog = Path("/tmp/silverstack_wrangler.log").read_text(errors="replace") if Path("/tmp/silverstack_wrangler.log").exists() else ""
        end = _R2_END.get(label, "")
        start = _R2_START.get(label, "")
        if end and end in wlog:
            return "done"
        if start and start in wlog:
            return "active"
    except Exception:
        pass
    return "waiting"

def _r2_step_pct(label):
    s = _r2_step_status(label)
    return 100 if s == "done" else 50 if s == "active" else 0

def get_status():
    xp_data = load_xp()

    # Nav1 A020
    nav1_a020_files = count_nav1_files("/Volumes/NAV1_VOLUME/A020_BRAW_2026-04-03")
    nav1_a020_total = 25
    nav1_a020_pct = int(nav1_a020_files / nav1_a020_total * 100)

    nav1_rsync = parse_rsync_log("/tmp/rsync_a020_nav1_v3.log")
    if nav1_a020_files >= nav1_a020_total:
        nav1_status = "done"
        nav1_pct = 100
    elif nav1_rsync:
        nav1_status = "active"
        nav1_pct = nav1_rsync.get("percent", nav1_a020_pct)
    else:
        nav1_status = "waiting"
        nav1_pct = nav1_a020_pct

    # iPhone → Nav1 (rsync MacBook, déjà fait)
    iphone_nav1_st, iphone_nav1_pct, iphone_nav1_speed, iphone_nav1_eta = iphone_nav1_status()

    # Nav2 A020 — lit robocopy_a020.log sur Nomad via SSH (progress réel)
    a020_robo = a020_nav2_progress(25)
    a015_robo = parse_robocopy_log("/tmp/a015_nomad_pipeline.log", 33, "A015 Nav2 copiée")

    if a020_robo and a020_robo["status"] == "done":
        nav2_a020_status, nav2_a020_pct = "done", 100
    elif a020_robo and a020_robo["status"] == "active":
        nav2_a020_status = "active"
        nav2_a020_pct = a020_robo["percent"]
    else:
        nav2_a020_status, nav2_a020_pct = "waiting", 0

    # Nav2 A015 — robocopy Nomad + ETA
    a015_eta, a015_speed = calc_eta_from_robocopy("/tmp/a015_nomad_pipeline.log", 33, "A015 Nav2 copiée")
    if a015_robo and a015_robo["status"] == "done":
        nav2_a015_status, nav2_a015_pct = "done", 100
        nav2_a015_files = f"{a015_robo['done']}/33"
        a015_eta, a015_speed = "—", "—"
    elif a015_robo and a015_robo["status"] == "active":
        nav2_a015_status = "active"
        nav2_a015_pct = a015_robo["percent"]
        nav2_a015_files = f"{a015_robo['done']}/33"
    else:
        nav2_a015_status, nav2_a015_pct, nav2_a015_files = "waiting", 0, "0/33"

    # Nav2 iPhone — compte les .MOV sur Nomad via SSH
    iphone_total_mov = 80
    iphone_dir = _ssh('dir /b G:\\iPhone-2TB_2026-04-03\\DCIM\\100APPLE\\*.MOV 2>nul', ttl=10)
    iphone_nav2_done = len([l for l in iphone_dir.splitlines() if l.strip()])
    # Aussi checker le wrangler log
    try:
        wrangler_log_content = Path("/tmp/silverstack_wrangler.log").read_text(errors="replace") if Path("/tmp/silverstack_wrangler.log").exists() else ""
    except (FileNotFoundError, OSError):
        wrangler_log_content = ""
    iphone_nav2_finished = "iPhone Nav2 copie terminée" in wrangler_log_content

    iphone_eta = "—"
    if iphone_nav2_finished or iphone_nav2_done >= iphone_total_mov:
        iphone_status, iphone_pct = "done", 100
        iphone_speed, iphone_eta = "—", "0:00"
    elif iphone_nav2_done > 0:
        iphone_status = "active"
        iphone_pct = min(int(iphone_nav2_done / iphone_total_mov * 100), 99)
        iphone_speed = "local"
        # ETA basé sur le taux de fichiers (cache progressive)
        cache_key = "__iphone_nav2_rate__"
        now_ts = time.time()
        if cache_key in _ssh_cache:
            prev_done, prev_ts = _ssh_cache[cache_key]
            dt = now_ts - prev_ts
            if dt > 20 and iphone_nav2_done > prev_done:
                rate = (iphone_nav2_done - prev_done) / dt
                remaining = iphone_total_mov - iphone_nav2_done
                eta_sec = int(remaining / rate) if rate > 0 else 0
                h, m2, s = eta_sec // 3600, (eta_sec % 3600) // 60, eta_sec % 60
                iphone_eta = f"{h}:{m2:02d}:{s:02d}" if h else f"{m2}:{s:02d}"
                iphone_speed = f"{rate * 60:.1f} f/min"
                _ssh_cache[cache_key] = (iphone_nav2_done, now_ts)
            elif dt > 60:
                _ssh_cache[cache_key] = (iphone_nav2_done, now_ts)
        else:
            _ssh_cache[cache_key] = (iphone_nav2_done, now_ts)
    else:
        iphone_status, iphone_pct, iphone_speed = "waiting", 0, "—"

    # R2 — logs pipeline + anciens logs rclone
    r2_logs = glob.glob("/tmp/rclone_r2_*_final.log")
    r2_parsed = {l: parse_rclone_log(l) for l in r2_logs}
    r2_results = [v for v in r2_parsed.values() if v]
    pipeline_log = Path("/tmp/a020_iphone_pipeline.log")
    pipeline_text = pipeline_log.read_text(errors="replace") if pipeline_log.exists() else ""
    r2_pipeline_done = "R2 COMPLET" in pipeline_text
    r2_pipeline_active = "Lancement R2" in pipeline_text and not r2_pipeline_done
    if r2_pipeline_done:
        r2_status, r2_pct = "done", 100
    elif r2_pipeline_active or r2_results:
        r2_status = "active"
        r2_pct = int(sum(r["percent"] for r in r2_results) / len(r2_results)) if r2_results else 0
    else:
        r2_status, r2_pct = "waiting", 0

    # NAS — fixé : "active" si log récent sans erreur + pas encore "done"
    nas_log = Path("/tmp/nas_sync_final.log")
    nas_done = False
    nas_status = "waiting"
    if nas_log.exists():
        nas_text = nas_log.read_text(errors="replace").lower()
        if "error" not in nas_text and len(nas_text.strip()) > 0:
            nas_done = True
            nas_status = "done"
        else:
            nas_status = "active" if r2_status == "done" else "waiting"

    # BIT-PERFECT
    def extract_sorted(manifest_path, ext):
        """Extrait hash + basename des fichiers avec l'extension donnée (sans ._), trié."""
        try:
            lines = Path(manifest_path).read_text(errors="replace").splitlines()
            entries = []
            for l in lines:
                parts = l.strip().split()
                if len(parts) >= 2 and parts[-1].lower().endswith(ext):
                    fname = parts[-1].split("/")[-1].split("\\")[-1]
                    if not fname.startswith("._"):
                        entries.append(f"{parts[0]} {fname}")
            return sorted(entries)
        except Exception:
            return None

    def braw_only_sorted(manifest_path):
        return extract_sorted(manifest_path, ".braw")

    bitperfect_nav1 = False
    for src_m, dst_m in [("/tmp/A020_source_xxh128.txt", "/tmp/A020_nav1_xxh128.txt"),
                         ("/tmp/a020_src_s.txt",         "/tmp/a020_nav1_s.txt")]:
        if Path(src_m).exists() and Path(dst_m).exists():
            src_braw = braw_only_sorted(src_m)
            dst_braw = braw_only_sorted(dst_m)
            if src_braw is not None and dst_braw is not None:
                bitperfect_nav1 = (src_braw == dst_braw) and len(src_braw) > 0
            break

    bitperfect_a015 = False
    for src_m, dst_m in [("/tmp/A015_source_xxh128.txt", "/tmp/A015_nav2_xxh128.txt"),
                         ("/tmp/a015_src_s.txt",         "/tmp/a015_nav2_s.txt")]:
        if Path(src_m).exists() and Path(dst_m).exists():
            src_braw = braw_only_sorted(src_m)
            dst_braw = braw_only_sorted(dst_m)
            if src_braw is not None and dst_braw is not None:
                bitperfect_a015 = (src_braw == dst_braw) and len(src_braw) > 0
            break

    # XP
    gb_backed = nav1_a020_files * 12
    if xp_data["total_gb"] != gb_backed:
        xp_data["total_xp"] += max(0, (gb_backed - xp_data["total_gb"]) * 10)
        xp_data["total_gb"] = gb_backed

    # Achievements — liste séparée pour éviter mutation concurrente
    achievements = list(xp_data.get("achievements", []))
    if bitperfect_nav1 and "BIT-PERFECT" not in achievements:
        achievements.append("BIT-PERFECT")
        xp_data["total_xp"] += 500
    if nav1_status == "done" and "NAV1_COMPLETE" not in achievements:
        achievements.append("NAV1_COMPLETE")
        xp_data["total_xp"] += 200
    if nav1_status == "done" and nav2_a020_status == "done" and "DOUBLE_BACKUP" not in achievements:
        achievements.append("DOUBLE_BACKUP")
        xp_data["total_xp"] += 1000
    if nav1_status == "done" and nav2_a020_status == "done" and r2_status == "done" and "TRIPLE_LOCK" not in achievements:
        achievements.append("TRIPLE_LOCK")
        xp_data["total_xp"] += 2000

    xp_data["achievements"] = achievements
    save_xp(xp_data)

    level_name, next_level_xp = get_level(xp_data["total_xp"])

    # Hash progress — toutes les destinations
    hash_a020_src    = get_hash_progress("/tmp/A020_source_xxh128.txt", 25)
    hash_a015_src    = get_hash_progress("/tmp/A015_source_xxh128.txt", 33)
    hash_iphone_src  = get_hash_progress("/tmp/iPhone_source_xxh128.txt", 80)
    hash_a020_nav1   = get_hash_progress("/tmp/A020_nav1_xxh128.txt", 25)
    hash_a015_nav1   = get_hash_progress("/tmp/A015_nav1_xxh128.txt", 33)
    # xxhsum avec tous les args n'écrit qu'à la fin — forcer "active" si process tourne
    if hash_a015_nav1["status"] == "waiting" and xxhsum_running():
        hash_a015_nav1["status"] = "active"
    hash_iphone_nav1 = get_hash_progress("/tmp/iPhone_nav1_xxh128.txt", 80)
    hash_a015_nav2   = get_hash_progress("/tmp/A015_nav2_xxh128.txt", 33, running_fn=rclone_hashsum_running)
    hash_a020_nav2   = get_hash_progress("/tmp/A020_nav2_xxh128.txt", 25, running_fn=rclone_hashsum_running)
    # iPhone Nav2 : 622 fichiers total (80 MOV + 542 proxies/metadata)
    hash_iphone_nav2 = get_hash_progress("/tmp/iPhone_nav2_xxh128.txt", 622, running_fn=rclone_hashsum_running)
    hash_h8_src      = get_hash_progress("/tmp/H8_source_xxh128.txt", 77)
    hash_h8_nav1     = get_hash_progress("/tmp/H8_nav1_xxh128.txt", 77)
    hash_h8_nav2     = get_hash_progress("/tmp/H8_nav2_xxh128.txt", 77, running_fn=rclone_hashsum_running)

    # BIT-PERFECT H8 Nav2
    bitperfect_h8_nav2 = False
    if Path("/tmp/H8_source_xxh128.txt").exists() and Path("/tmp/H8_nav2_xxh128.txt").exists():
        try:
            hs2 = sorted([l.split()[0] for l in Path("/tmp/H8_source_xxh128.txt").read_text().splitlines() if l.strip()])
            hd2 = sorted([l.split()[0] for l in Path("/tmp/H8_nav2_xxh128.txt").read_text().splitlines() if l.strip()])
            bitperfect_h8_nav2 = (hs2 == hd2 and len(hs2) > 0)
        except Exception:
            pass

    # BIT-PERFECT H8 Nav1
    bitperfect_h8_nav1 = False
    if Path("/tmp/H8_source_xxh128.txt").exists() and Path("/tmp/H8_nav1_xxh128.txt").exists():
        try:
            hs = sorted([l.split()[0] for l in Path("/tmp/H8_source_xxh128.txt").read_text().splitlines() if l.strip()])
            hd = sorted([l.split()[0] for l in Path("/tmp/H8_nav1_xxh128.txt").read_text().splitlines() if l.strip()])
            bitperfect_h8_nav1 = (hs == hd and len(hs) > 0)
        except Exception:
            pass

    # BIT-PERFECT A015 Nav1
    bitperfect_a015_nav1 = False
    if Path("/tmp/A015_source_xxh128.txt").exists() and Path("/tmp/A015_nav1_xxh128.txt").exists():
        src_b2 = braw_only_sorted("/tmp/A015_source_xxh128.txt")
        dst_b2 = braw_only_sorted("/tmp/A015_nav1_xxh128.txt")
        bitperfect_a015_nav1 = (src_b2 is not None and dst_b2 is not None
                                and src_b2 == dst_b2 and len(src_b2) > 0)

    # BIT-PERFECT A020 Nav2
    bitperfect_a020_nav2 = False
    if Path("/tmp/A020_source_xxh128.txt").exists() and Path("/tmp/A020_nav2_xxh128.txt").exists():
        src_b = braw_only_sorted("/tmp/A020_source_xxh128.txt")
        dst_b = braw_only_sorted("/tmp/A020_nav2_xxh128.txt")
        bitperfect_a020_nav2 = (src_b is not None and dst_b is not None
                                and src_b == dst_b and len(src_b) > 0)

    # BIT-PERFECT iPhone Nav2
    bitperfect_iphone_nav2 = False
    if Path("/tmp/iPhone_source_xxh128.txt").exists() and Path("/tmp/iPhone_nav2_xxh128.txt").exists():
        src_m2 = extract_sorted("/tmp/iPhone_source_xxh128.txt", ".mov")
        dst_m2 = extract_sorted("/tmp/iPhone_nav2_xxh128.txt", ".mov")
        bitperfect_iphone_nav2 = (src_m2 is not None and dst_m2 is not None
                                  and src_m2 == dst_m2 and len(src_m2) > 0)

    # Wrangler phase (lit le log)
    wrangler_phase = "idle"
    wrangler_log = Path("/tmp/silverstack_wrangler.log")
    try:
        wlog = wrangler_log.read_text(errors="replace") if wrangler_log.exists() else ""
    except (FileNotFoundError, OSError):
        wlog = ""
    if wlog:
        # Séquence finale a priorité sur l'ancien wrangler
        if "SÉQUENCE FINALE" in wlog:
            # Dernière ligne significative après "SÉQUENCE FINALE"
            seq_start = wlog.rfind("SÉQUENCE FINALE")
            seq_lines = wlog[seq_start:].splitlines()
            for line in reversed(seq_lines):
                if not line.strip():
                    continue
                if "SAFE ÉJECTER" in line:
                    wrangler_phase = "complete"; break
                elif "NAS" in line and "sync" in line.lower() and "terminé" not in line:
                    wrangler_phase = "nas_sync"; break
                elif "Verify R2" in line:
                    wrangler_phase = "r2_verify"; break
                elif "R2 H8" in line and "terminé" not in line:
                    wrangler_phase = "r2_h8"; break
                elif "R2 iPhone" in line and "terminé" not in line:
                    wrangler_phase = "r2_iphone"; break
                elif "R2 A020" in line and "terminé" not in line:
                    wrangler_phase = "r2_a020"; break
                elif "R2 A015" in line and "terminé" not in line:
                    wrangler_phase = "r2_a015"; break
                elif "Hash H8" in line or "H8 Nav2" in line:
                    wrangler_phase = "hash_h8_nav2"; break
                elif "iPhone hash" in line or "Attente iPhone" in line:
                    wrangler_phase = "hash_iphone_nav2"; break

    # BIT-PERFECT iPhone Nav1 — compare MOV source vs Nav1
    bitperfect_iphone = False
    iphone_src_m  = Path("/tmp/iPhone_source_xxh128.txt")
    iphone_nav1_m = Path("/tmp/iPhone_nav1_xxh128.txt")
    if iphone_src_m.exists() and iphone_nav1_m.exists():
        src_mov  = extract_sorted("/tmp/iPhone_source_xxh128.txt", ".mov")
        nav1_mov = extract_sorted("/tmp/iPhone_nav1_xxh128.txt", ".mov")
        bitperfect_iphone = (src_mov is not None and nav1_mov is not None
                             and src_mov == nav1_mov and len(src_mov) > 0)

    # Espace disque
    disks = {
        "nav1": disk_usage("/Volumes/NAV1_VOLUME"),
        "nav2": nav2_disk_usage(),
        "nas": nas_disk_usage(),
    }

    return {
        "session": "BACKUP NAGE — " + datetime.now().strftime("%d/%m/%Y"),
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "transfers": [
            {
                "label": "A020 BRAW",
                "dest": "Nav1",
                "machine": "MacBook",
                "status": nav1_status,
                "percent": nav1_pct,
                "speed": nav1_rsync.get("speed", "—") if nav1_rsync else "—",
                "eta": nav1_rsync.get("eta", "—") if nav1_rsync else "—",
                "files": f"{nav1_a020_files}/{nav1_a020_total}",
                "bitperfect": bitperfect_nav1,
            },
            {
                "label": "iPhone 2TB",
                "dest": "Nav1",
                "machine": "MacBook",
                "status": iphone_nav1_st,
                "percent": iphone_nav1_pct,
                "speed": iphone_nav1_speed,
                "eta": iphone_nav1_eta,
                "files": "80/80" if iphone_nav1_st == "done" else "—",
                "bitperfect": bitperfect_iphone,
            },
            {
                "label": "A015 BRAW",
                "dest": "Nav1",
                "machine": "MacBook",
                "status": "done" if bitperfect_a015_nav1 or count_nav1_files("/Volumes/NAV1_VOLUME/A015_BRAW_2026-03-28") >= 33 else "waiting",
                "percent": 100 if count_nav1_files("/Volumes/NAV1_VOLUME/A015_BRAW_2026-03-28") >= 33 else 0,
                "speed": "—",
                "eta": "—",
                "files": f"{count_nav1_files('/Volumes/NAV1_VOLUME/A015_BRAW_2026-03-28')}/33",
                "bitperfect": bitperfect_a015_nav1,
            },
            {
                "label": "A015 BRAW",
                "dest": "Nav2",
                "machine": "Nomad",
                "status": nav2_a015_status,
                "percent": nav2_a015_pct,
                "speed": a015_speed,
                "eta": a015_eta,
                "files": nav2_a015_files,
                "bitperfect": bitperfect_a015,
            },
            {
                "label": "A020 BRAW",
                "dest": "Nav2",
                "machine": "Nomad",
                "status": nav2_a020_status,
                "percent": nav2_a020_pct,
                "speed": a020_robo.get("speed", "—") if a020_robo else "—",
                "eta": a020_robo.get("eta", "—") if a020_robo else "—",
                "files": f"{a020_robo['done']}/{a020_robo['total']}" if a020_robo else "—/25",
                "bitperfect": bitperfect_a020_nav2,
            },
            {
                "label": "iPhone 2TB",
                "dest": "Nav2",
                "machine": "Nomad",
                "status": iphone_status,
                "percent": iphone_pct,
                "speed": iphone_speed,
                "eta": iphone_eta,
                "files": f"{iphone_nav2_done}/{iphone_total_mov}",
                "bitperfect": bitperfect_iphone_nav2,
            },
            {
                "label": "H8 SD",
                "dest": "Nav1",
                "machine": "MacBook",
                "status": "done",
                "percent": 100,
                "speed": "—",
                "eta": "—",
                "files": "77/77",
                "bitperfect": bitperfect_h8_nav1,
            },
            {
                "label": "H8 SD",
                "dest": "Nav2",
                "machine": "Nomad",
                "status": "done",
                "percent": 100,
                "speed": "—",
                "eta": "—",
                "files": "77/77",
                "bitperfect": bitperfect_h8_nav2 if 'bitperfect_h8_nav2' in dir() else False,
            },
            {
                "label": "A015 BRAW",
                "dest": "R2 ☁️",
                "machine": "Nomad",
                "status": _r2_step_status("A015"),
                "percent": _r2_step_pct("A015"),
                "speed": "—",
                "eta": "—",
                "files": "—",
                "bitperfect": False,
            },
            {
                "label": "A020 BRAW",
                "dest": "R2 ☁️",
                "machine": "Nomad",
                "status": _r2_step_status("A020"),
                "percent": _r2_step_pct("A020"),
                "speed": "—",
                "eta": "—",
                "files": "—",
                "bitperfect": False,
            },
            {
                "label": "iPhone 2TB",
                "dest": "R2 ☁️",
                "machine": "Nomad",
                "status": _r2_step_status("iPhone"),
                "percent": _r2_step_pct("iPhone"),
                "speed": "—",
                "eta": "—",
                "files": "—",
                "bitperfect": False,
            },
            {
                "label": "H8 SD",
                "dest": "R2 ☁️",
                "machine": "Nomad",
                "status": _r2_step_status("H8"),
                "percent": _r2_step_pct("H8"),
                "speed": "—",
                "eta": "—",
                "files": "—",
                "bitperfect": False,
            },
            {
                "label": "NAS Paris",
                "dest": "🏠",
                "machine": "Mini",
                "status": nas_status,
                "percent": 100 if nas_done else 0,
                "speed": "—",
                "eta": "—",
                "files": "—",
                "bitperfect": False,
            },
        ],
        "xp": {
            "total": xp_data["total_xp"],
            "level": level_name,
            "next_level_xp": next_level_xp,
            "gb_backed": gb_backed,
        },
        "achievements": achievements,
        "hash": {
            "a020_nav1": bitperfect_nav1,
            "a020_nav2": bitperfect_a020_nav2,
            "a015_nav1": bitperfect_a015_nav1,
            "a015_nav2": bitperfect_a015,
            "iphone_nav1": bitperfect_iphone,
            "iphone_nav2": bitperfect_iphone_nav2,
            "h8_nav1": bitperfect_h8_nav1,
            "h8_nav2": bitperfect_h8_nav2,
        },
        "hashing": [
            {"label": "xxhsum A020 source",    "machine": "MacBook", **hash_a020_src},
            {"label": "xxhsum A015 source",    "machine": "MacBook", **hash_a015_src},
            {"label": "xxhsum iPhone source",  "machine": "MacBook", **hash_iphone_src},
            {"label": "xxhsum A020 → Nav1",    "machine": "MacBook", **hash_a020_nav1},
            {"label": "xxhsum A015 → Nav1",    "machine": "MacBook", **hash_a015_nav1},
            {"label": "xxhsum iPhone → Nav1",  "machine": "MacBook", **hash_iphone_nav1},
            {"label": "xxhsum A015 → Nav2",    "machine": "Nomad",   **hash_a015_nav2},
            {"label": "xxhsum A020 → Nav2",    "machine": "Nomad",   **hash_a020_nav2},
            {"label": "xxhsum iPhone → Nav2",  "machine": "Nomad",   **hash_iphone_nav2},
            {"label": "xxhsum H8 SD source",   "machine": "MacBook", **hash_h8_src},
            {"label": "xxhsum H8 SD → Nav1",   "machine": "MacBook", **hash_h8_nav1},
            {"label": "xxhsum H8 SD → Nav2",   "machine": "Nomad",   **hash_h8_nav2},
        ],
        "wrangler": wrangler_phase,
        "disks": disks,
    }


class Handler(SimpleHTTPRequestHandler):
    """Handler HTTP avec protection contre les crashs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/tmp/backup_dashboard", **kwargs)

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _qs(self):
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def do_GET(self):
        path, qs = self._qs()

        if path == "/api/status":
            try:
                data = get_status()
                self._json_response(data)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/metadata":
            folder = qs.get("folder", [None])[0]
            if not folder:
                self._json_response({"error": "?folder= requis"}, 400)
                return
            try:
                from metadata import extract_metadata
                clips = extract_metadata(folder)
                self._json_response({"folder": folder, "clips": clips, "count": len(clips)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/generate-proxies":
            source = qs.get("source", [None])[0]
            dest = qs.get("dest", [PROXY_DIR])[0]
            if not source:
                self._json_response({"error": "?source= requis"}, 400)
                return
            try:
                from generate_proxies import start_batch
                result = start_batch(source, dest)
                self._json_response(result)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/proxy-progress":
            try:
                from generate_proxies import get_progress
                self._json_response(get_progress())
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/proxies":
            try:
                proxy_path = Path(PROXY_DIR)
                proxy_path.mkdir(parents=True, exist_ok=True)
                clips = []
                for f in sorted(proxy_path.iterdir()):
                    if f.suffix.lower() == ".mp4":
                        clips.append({
                            "name": f.name,
                            "path": f"/proxy-files/{f.name}",
                            "size_mb": round(f.stat().st_size / 1e6, 1),
                        })
                self._json_response({"clips": clips, "count": len(clips)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/luts":
            try:
                from lut_preview import list_luts
                self._json_response({"luts": list_luts()})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/lut":
            filepath = qs.get("file", [None])[0]
            if not filepath:
                self._json_response({"error": "?file= requis"}, 400)
                return
            try:
                from lut_preview import parse_lut
                result = parse_lut(filepath)
                if "error" in result:
                    self._json_response(result, 400)
                else:
                    self._json_response(result)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path.startswith("/proxy-files/"):
            # Servir les fichiers proxy directement
            filename = path.replace("/proxy-files/", "")
            filepath = Path(PROXY_DIR) / filename
            if filepath.exists() and filepath.suffix.lower() == ".mp4":
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", filepath.stat().st_size)
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(filepath, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            else:
                self.send_error(404)

        elif path == "/api/report":
            try:
                from generate_report import generate_report_html
                body = generate_report_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        elif path == "/api/annotations":
            try:
                clip_filter = qs.get("clip", [None])[0]
                annotations = load_annotations()
                if clip_filter:
                    annotations = [a for a in annotations if a.get("clip") == clip_filter]
                self._json_response({"annotations": annotations, "count": len(annotations)})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        else:
            super().do_GET()

    def do_POST(self):
        path, qs = self._qs()

        if path == "/api/generate-mhl":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                params = json.loads(body)
                folder = params.get("folder", "")
                if not folder or not os.path.isdir(folder):
                    self._json_response({"error": f"Invalid folder path: {folder}"}, 400)
                    return
                # Run MHL generation in a subprocess to avoid blocking the server
                script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_mhl.py")
                result = subprocess.run(
                    ["python", script, folder],
                    capture_output=True, text=True, timeout=600,
                )
                if result.returncode == 0:
                    # Extract the created file path from output
                    mhl_file = None
                    for line in result.stdout.splitlines():
                        if "Generation saved:" in line or line.startswith("MHL manifest:"):
                            mhl_file = line.split(":", 1)[1].strip()
                    self._json_response({
                        "status": "ok",
                        "mhl_file": mhl_file,
                        "output": result.stdout,
                    })
                else:
                    self._json_response({
                        "status": "error",
                        "error": result.stderr or result.stdout,
                    }, 500)
            except subprocess.TimeoutExpired:
                self._json_response({"error": "MHL generation timed out (10 min limit)"}, 504)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
        elif path == "/api/annotate":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_length) if content_length > 0 else b"{}"
                params = json.loads(body)
                clip = params.get("clip", "").strip()
                note = params.get("note", "").strip()
                rating = params.get("rating", 0)
                tags = params.get("tags", [])
                if not clip:
                    self._json_response({"error": "clip field is required"}, 400)
                    return
                # Validate rating 0-5
                try:
                    rating = max(0, min(5, int(rating)))
                except (ValueError, TypeError):
                    rating = 0
                # Validate tags
                VALID_TAGS = {"selects", "vfx", "audio", "reshoot", "circled"}
                tags = [t for t in tags if t in VALID_TAGS]
                annotation = {
                    "clip": clip,
                    "note": note,
                    "rating": rating,
                    "tags": tags,
                    "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                annotations = load_annotations()
                annotations.append(annotation)
                save_annotations(annotations)
                self._json_response({"status": "ok", "annotation": annotation})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)

        else:
            self._json_response({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight for POST requests."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass  # silencieux


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer
    port = 4242
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard: http://localhost:{port}")
    print(f"Réseau:    http://$(hostname -I | awk '{{print $1}}'):{port}")
    server.serve_forever()
