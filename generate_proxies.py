#!/usr/bin/env python3
"""Proxy generation — FFmpeg H.264 720p depuis .braw/.mov."""

import json, os, subprocess, threading, time
from pathlib import Path

FFMPEG = "/opt/homebrew/bin/ffmpeg"
# .braw nécessite le plugin Blackmagic RAW — on teste si ffmpeg sait le lire
_BRAW_SUPPORTED: bool | None = None


def _check_braw_support() -> bool:
    """Vérifie si ffmpeg peut decoder les .braw (Blackmagic RAW plugin)."""
    global _BRAW_SUPPORTED
    if _BRAW_SUPPORTED is not None:
        return _BRAW_SUPPORTED
    try:
        r = subprocess.run(
            [FFMPEG, "-decoders"],
            capture_output=True, text=True, timeout=10
        )
        _BRAW_SUPPORTED = "braw" in r.stdout.lower()
    except Exception:
        _BRAW_SUPPORTED = False
    return _BRAW_SUPPORTED


def _can_transcode(filepath: Path) -> bool:
    """Retourne True si on peut transcoder ce fichier."""
    ext = filepath.suffix.lower()
    if ext == ".mov":
        return True
    if ext == ".braw":
        return _check_braw_support()
    return False


# Tracking de la progression en cours
_progress: dict = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "errors": [],
    "completed": [],
}
_lock = threading.Lock()


def get_progress() -> dict:
    with _lock:
        return dict(_progress)


def generate_proxy(source_path: Path, dest_path: Path) -> tuple[bool, str]:
    """Transcode un fichier en H.264 720p proxy. Retourne (success, message)."""
    out_name = source_path.stem + ".mp4"
    out_file = dest_path / out_name

    if out_file.exists():
        return True, f"Déjà existant: {out_name}"

    try:
        cmd = [
            FFMPEG,
            "-i", str(source_path),
            "-vf", "scale=-2:720",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "28",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-y",
            str(out_file),
        ]
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600
        )
        if result.returncode == 0 and out_file.exists():
            return True, f"OK: {out_name}"
        else:
            # Nettoyage fichier partiel
            out_file.unlink(missing_ok=True)
            err = result.stderr[-200:] if result.stderr else "unknown error"
            return False, f"Erreur {out_name}: {err}"
    except subprocess.TimeoutExpired:
        out_file.unlink(missing_ok=True)
        return False, f"Timeout: {out_name}"
    except Exception as e:
        out_file.unlink(missing_ok=True)
        return False, f"Exception {out_name}: {e}"


def _run_batch(source: str, dest: str):
    """Thread worker : transcode tous les fichiers éligibles."""
    global _progress
    source_path = Path(source)
    dest_path = Path(dest)
    dest_path.mkdir(parents=True, exist_ok=True)

    files = sorted([
        f for f in source_path.iterdir()
        if f.suffix.lower() in {".braw", ".mov"} and _can_transcode(f)
    ])

    with _lock:
        _progress = {
            "running": True,
            "total": len(files),
            "done": 0,
            "current": "",
            "errors": [],
            "completed": [],
        }

    for f in files:
        with _lock:
            _progress["current"] = f.name
        ok, msg = generate_proxy(f, dest_path)
        with _lock:
            _progress["done"] += 1
            if ok:
                _progress["completed"].append(f.stem + ".mp4")
            else:
                _progress["errors"].append(msg)

    with _lock:
        _progress["running"] = False
        _progress["current"] = ""


def start_batch(source: str, dest: str) -> dict:
    """Lance le batch en arrière-plan. Retourne le statut initial."""
    if _progress.get("running"):
        return {"error": "Un batch est déjà en cours", **get_progress()}

    t = threading.Thread(target=_run_batch, args=(source, dest), daemon=True)
    t.start()
    time.sleep(0.2)  # laisser le thread initialiser _progress
    return get_progress()


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: generate_proxies.py <source_folder> <dest_folder>")
        sys.exit(1)
    start_batch(sys.argv[1], sys.argv[2])
    while _progress.get("running"):
        time.sleep(1)
        p = get_progress()
        print(f"  [{p['done']}/{p['total']}] {p['current']}")
    p = get_progress()
    print(f"\nTerminé: {len(p['completed'])} OK, {len(p['errors'])} erreurs")
    for e in p["errors"]:
        print(f"  ! {e}")
