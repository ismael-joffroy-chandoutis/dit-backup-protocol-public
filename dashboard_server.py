#!/usr/bin/env python3
"""Backup Dashboard Server — lit les logs rsync/rclone et sert un JSON de statut."""

import json, re, os, glob, shutil, tempfile, time, subprocess
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from datetime import datetime

# Cache SSH calls — (result, timestamp)
_ssh_cache: dict = {}

XP_FILE = "/tmp/backup_dashboard/xp.json"

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

        # Speed + ETA via birthtime du manifest
        speed_str, eta_str = "—", "—"
        if status == "active" and done > 0:
            try:
                import stat as _stat
                st = p.stat()
                birth = getattr(st, "st_birthtime", st.st_mtime)
                elapsed = datetime.now().timestamp() - birth
                if elapsed > 5:
                    rate = done / elapsed          # fichiers/sec
                    remaining = max(total - done, 0)
                    eta_sec = int(remaining / rate) if rate > 0 else 0
                    h2, m2, s2 = eta_sec // 3600, (eta_sec % 3600) // 60, eta_sec % 60
                    eta_str = f"{h2}:{m2:02d}:{s2:02d}" if h2 else f"{m2}:{s2:02d}"
                    # Vitesse en fichiers/min
                    fpm = rate * 60
                    speed_str = f"{fpm:.1f} f/min"
            except Exception:
                pass

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

    if iphone_nav2_finished or iphone_nav2_done >= iphone_total_mov:
        iphone_status, iphone_pct = "done", 100
        iphone_speed = "—"
    elif iphone_nav2_done > 0:
        iphone_status = "active"
        iphone_pct = min(int(iphone_nav2_done / iphone_total_mov * 100), 99)
        iphone_speed = "local"
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
    hash_iphone_nav1 = get_hash_progress("/tmp/iPhone_nav1_xxh128.txt", 80)
    hash_a015_nav2   = get_hash_progress("/tmp/A015_nav2_xxh128.txt", 33, running_fn=rclone_hashsum_running)
    hash_a020_nav2   = get_hash_progress("/tmp/A020_nav2_xxh128.txt", 25, running_fn=rclone_hashsum_running)
    hash_iphone_nav2 = get_hash_progress("/tmp/iPhone_nav2_xxh128.txt", 80, running_fn=rclone_hashsum_running)

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
        for p in ["PHASE 8", "PHASE 7", "PHASE 6", "PHASE 5", "PHASE 4", "PHASE 3", "PHASE 2", "PHASE 1"]:
            if p in wlog:
                wrangler_phase = p.lower().replace(" ", "_")
                break
        if "COMPLET" in wlog and "BACKUP NAGE" in wlog:
            wrangler_phase = "complete"

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
                "eta": "—",
                "files": f"{iphone_nav2_done}/{iphone_total_mov}",
                "bitperfect": bitperfect_iphone_nav2,
            },
            {
                "label": "R2 Cloud",
                "dest": "☁️",
                "machine": "Nomad",
                "status": r2_status,
                "percent": r2_pct,
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
        ],
        "wrangler": wrangler_phase,
        "disks": disks,
    }


class Handler(SimpleHTTPRequestHandler):
    """Handler HTTP avec protection contre les crashs."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/tmp/backup_dashboard", **kwargs)

    def do_GET(self):
        if self.path == "/api/status":
            try:
                data = get_status()
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", len(err))
                self.end_headers()
                self.wfile.write(err)
        else:
            super().do_GET()

    def log_message(self, *args):
        pass  # silencieux


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer
    port = 4242
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Dashboard: http://localhost:{port}")
    print(f"Réseau:    http://$(hostname -I | awk '{{print $1}}'):{port}")
    server.serve_forever()
