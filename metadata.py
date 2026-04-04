#!/usr/bin/env python3
"""Camera metadata extraction — telemetry_parser + ffprobe fallback."""

import json, os, subprocess
from pathlib import Path

FFPROBE = "/opt/homebrew/bin/ffprobe"
EXTENSIONS = {".braw", ".mov"}


def _extract_telemetry(filepath: str) -> dict | None:
    """Essaie telemetry_parser (Blackmagic, GoPro, DJI, etc.)."""
    try:
        from telemetry_parser import Parser
        p = Parser(filepath)
        info = {"source": "telemetry_parser"}
        if p.camera:
            info["camera"] = str(p.camera)
        if p.model:
            info["model"] = str(p.model)
        # telemetry() retourne des données brutes — on extrait ce qu'on peut
        try:
            t = p.telemetry(human_readable=True)
            if isinstance(t, dict):
                for key in ("codec", "resolution", "fps", "duration", "lens"):
                    if key in t:
                        info[key] = t[key]
        except Exception:
            pass
        return info if len(info) > 1 else None
    except Exception:
        return None


def _extract_ffprobe(filepath: str) -> dict | None:
    """Fallback via ffprobe JSON."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", filepath],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        info = {"source": "ffprobe"}

        # Chercher le stream vidéo
        video = None
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                video = s
                break

        if video:
            info["codec"] = video.get("codec_name", "—")
            w = video.get("width")
            h = video.get("height")
            if w and h:
                info["resolution"] = f"{w}x{h}"
            # FPS depuis r_frame_rate ou avg_frame_rate
            for key in ("r_frame_rate", "avg_frame_rate"):
                raw = video.get(key, "")
                if "/" in raw:
                    num, den = raw.split("/")
                    try:
                        fps = round(int(num) / int(den), 3)
                        if fps > 0:
                            info["fps"] = fps
                            break
                    except (ValueError, ZeroDivisionError):
                        pass
            info["codec_long"] = video.get("codec_long_name", "")

        fmt = data.get("format", {})
        dur = fmt.get("duration")
        if dur:
            try:
                secs = float(dur)
                m, s = divmod(int(secs), 60)
                h, m = divmod(m, 60)
                info["duration"] = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                info["duration_s"] = round(secs, 2)
            except ValueError:
                pass

        # Camera model from format tags
        tags = fmt.get("tags", {})
        for tag_key in ("com.apple.quicktime.model", "make", "model", "encoder"):
            val = tags.get(tag_key)
            if val:
                info.setdefault("camera", val)
                break

        # Lens info from tags
        for tag_key in ("com.apple.quicktime.lens-model", "lens"):
            val = tags.get(tag_key)
            if val:
                info["lens"] = val
                break

        return info
    except Exception:
        return None


def extract_metadata(folder: str) -> list[dict]:
    """Extrait les métadonnées de tous les .braw/.mov dans le dossier."""
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return []

    results = []
    for f in sorted(folder_path.iterdir()):
        if f.suffix.lower() not in EXTENSIONS:
            continue
        clip = {
            "filename": f.name,
            "size_mb": round(f.stat().st_size / 1e6, 1),
        }
        # Essai telemetry_parser d'abord
        meta = _extract_telemetry(str(f))
        if not meta:
            meta = _extract_ffprobe(str(f))
        if meta:
            clip.update(meta)
        results.append(clip)

    return results


if __name__ == "__main__":
    import sys
    folder = sys.argv[1] if len(sys.argv) > 1 else "."
    clips = extract_metadata(folder)
    print(json.dumps(clips, indent=2, ensure_ascii=False))
