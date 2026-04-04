#!/usr/bin/env python3
"""Generate a professional HTML backup report styled for print (A4, monochrome-friendly)."""

import json, os, re
from datetime import datetime
from pathlib import Path


def _read_wrangler_timeline():
    """Extract timestamped events from the wrangler log."""
    log = Path("/tmp/silverstack_wrangler.log")
    if not log.exists():
        return []
    events = []
    ts_re = re.compile(r'^\[(\d{2}:\d{2}:\d{2})\]\s*(.*)')
    try:
        for line in log.read_text(errors="replace").splitlines():
            m = ts_re.match(line.strip())
            if m and m.group(2).strip():
                events.append({"time": m.group(1), "event": m.group(2).strip()})
    except Exception:
        pass
    return events


def _read_hash_manifest(path):
    """Count entries and extract first/last hash from a manifest file."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        lines = [l for l in p.read_text(errors="replace").splitlines() if l.strip()]
        if not lines:
            return {"count": 0}
        return {
            "count": len(lines),
            "first": lines[0][:32] + "..." if len(lines[0]) > 32 else lines[0],
            "last": lines[-1][:32] + "..." if len(lines[-1]) > 32 else lines[-1],
        }
    except Exception:
        return None


def _braw_hashes_sorted(manifest_path, ext=".braw"):
    """Extract sorted hash+basename pairs for a given extension."""
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


def _check_bitperfect(src_path, dst_path, ext=".braw"):
    """Compare two hash manifests and return match info."""
    src = _braw_hashes_sorted(src_path, ext)
    dst = _braw_hashes_sorted(dst_path, ext)
    if src is None or dst is None:
        return {"status": "unavailable", "matched": 0, "total": 0}
    matched = len(set(src) & set(dst))
    return {
        "status": "BIT-PERFECT" if src == dst and len(src) > 0 else "MISMATCH",
        "matched": matched,
        "total_src": len(src),
        "total_dst": len(dst),
    }


def generate_report_html():
    """Generate the full HTML report string."""
    now = datetime.now()

    # Load XP data
    xp_data = {"total_xp": 0, "total_gb": 0, "achievements": []}
    xp_file = Path("/tmp/backup_dashboard/xp.json")
    if xp_file.exists():
        try:
            xp_data = json.loads(xp_file.read_text())
        except Exception:
            pass

    # Collect sources info
    sources = []
    source_dirs = {
        "A020 BRAW": "/Volumes/NavTGV1/A020_BRAW_2026-04-03",
        "A015 BRAW": "/Volumes/NavTGV1/A015_BRAW_2026-03-28",
        "H8 SD": "/Volumes/NavTGV1/H8_SD",
    }
    for label, path in source_dirs.items():
        p = Path(path)
        count = 0
        if p.exists():
            count = len(list(p.iterdir()))
        sources.append({"label": label, "path": path, "files": count})

    # Hash verification results
    verifications = [
        ("A020 BRAW -> Nav1", "/tmp/A020_source_xxh128.txt", "/tmp/A020_nav1_xxh128.txt", ".braw"),
        ("A020 BRAW -> Nav2", "/tmp/A020_source_xxh128.txt", "/tmp/A020_nav2_xxh128.txt", ".braw"),
        ("A015 BRAW -> Nav1", "/tmp/A015_source_xxh128.txt", "/tmp/A015_nav1_xxh128.txt", ".braw"),
        ("A015 BRAW -> Nav2", "/tmp/A015_source_xxh128.txt", "/tmp/A015_nav2_xxh128.txt", ".braw"),
        ("iPhone 2TB -> Nav1", "/tmp/iPhone_source_xxh128.txt", "/tmp/iPhone_nav1_xxh128.txt", ".mov"),
        ("iPhone 2TB -> Nav2", "/tmp/iPhone_source_xxh128.txt", "/tmp/iPhone_nav2_xxh128.txt", ".mov"),
        ("H8 SD -> Nav1", "/tmp/H8_source_xxh128.txt", "/tmp/H8_nav1_xxh128.txt", None),
        ("H8 SD -> Nav2", "/tmp/H8_source_xxh128.txt", "/tmp/H8_nav2_xxh128.txt", None),
    ]
    verify_results = []
    for label, src, dst, ext in verifications:
        if ext:
            result = _check_bitperfect(src, dst, ext)
        else:
            # Generic hash comparison (all lines)
            src_m = _read_hash_manifest(src)
            dst_m = _read_hash_manifest(dst)
            if src_m and dst_m and src_m["count"] > 0 and dst_m["count"] > 0:
                try:
                    sh = sorted([l.split()[0] for l in Path(src).read_text().splitlines() if l.strip()])
                    dh = sorted([l.split()[0] for l in Path(dst).read_text().splitlines() if l.strip()])
                    result = {
                        "status": "BIT-PERFECT" if sh == dh and len(sh) > 0 else "MISMATCH",
                        "matched": len(set(sh) & set(dh)),
                        "total_src": len(sh),
                        "total_dst": len(dh),
                    }
                except Exception:
                    result = {"status": "unavailable", "matched": 0, "total_src": 0, "total_dst": 0}
            else:
                result = {"status": "unavailable", "matched": 0, "total_src": 0, "total_dst": 0}
        verify_results.append({"label": label, **result})

    # Hash manifests summary
    manifests = [
        ("A020 source", "/tmp/A020_source_xxh128.txt"),
        ("A015 source", "/tmp/A015_source_xxh128.txt"),
        ("iPhone source", "/tmp/iPhone_source_xxh128.txt"),
        ("H8 source", "/tmp/H8_source_xxh128.txt"),
        ("A020 Nav1", "/tmp/A020_nav1_xxh128.txt"),
        ("A015 Nav1", "/tmp/A015_nav1_xxh128.txt"),
        ("iPhone Nav1", "/tmp/iPhone_nav1_xxh128.txt"),
        ("H8 Nav1", "/tmp/H8_nav1_xxh128.txt"),
        ("A015 Nav2", "/tmp/A015_nav2_xxh128.txt"),
        ("A020 Nav2", "/tmp/A020_nav2_xxh128.txt"),
        ("iPhone Nav2", "/tmp/iPhone_nav2_xxh128.txt"),
        ("H8 Nav2", "/tmp/H8_nav2_xxh128.txt"),
    ]
    manifest_info = []
    for label, path in manifests:
        info = _read_hash_manifest(path)
        manifest_info.append({"label": label, "path": path, "info": info})

    # Disk usage (simple stat)
    import shutil
    disk_info = {}
    for label, path in [("Nav1", "/Volumes/NavTGV1")]:
        try:
            u = shutil.disk_usage(path)
            disk_info[label] = {
                "total_gb": round(u.total / 1e9, 1),
                "used_gb": round(u.used / 1e9, 1),
                "free_gb": round(u.free / 1e9, 1),
                "percent": int(u.used / u.total * 100),
            }
        except Exception:
            disk_info[label] = None

    # Timeline from wrangler
    timeline = _read_wrangler_timeline()

    # Build HTML
    bitperfect_count = sum(1 for v in verify_results if v["status"] == "BIT-PERFECT")
    total_verifications = len(verify_results)

    # Achievement display
    ach_labels = {
        "BIT-PERFECT": "Bit-Perfect Verified",
        "NAV1_COMPLETE": "Nav1 Complete",
        "DOUBLE_BACKUP": "Double Backup (Nav1 + Nav2)",
        "TRIPLE_LOCK": "Triple Lock (Nav1 + Nav2 + R2)",
    }

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<title>Backup Report — {now.strftime('%Y-%m-%d')}</title>
<style>
  @page {{ size: A4; margin: 20mm; }}
  @media print {{
    body {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
    .no-print {{ display: none !important; }}
  }}
  :root {{
    --bg: #ffffff;
    --surface: #f8f9fa;
    --border: #dee2e6;
    --accent: #5a3d8a;
    --green: #198754;
    --red: #dc3545;
    --text: #212529;
    --muted: #6c757d;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Courier New', monospace;
    color: var(--text);
    background: var(--bg);
    max-width: 210mm;
    margin: 0 auto;
    padding: 20px;
    font-size: 11px;
    line-height: 1.5;
  }}
  h1 {{
    font-size: 18px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    border-bottom: 2px solid var(--text);
    padding-bottom: 8px;
    margin-bottom: 4px;
  }}
  h2 {{
    font-size: 13px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--accent);
    margin: 18px 0 8px 0;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--border);
  }}
  .meta {{ color: var(--muted); font-size: 10px; margin-bottom: 16px; }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 12px;
    font-size: 10.5px;
  }}
  th, td {{
    text-align: left;
    padding: 5px 8px;
    border-bottom: 1px solid var(--border);
  }}
  th {{
    background: var(--surface);
    font-weight: bold;
    text-transform: uppercase;
    font-size: 9.5px;
    letter-spacing: 0.08em;
    color: var(--muted);
  }}
  .ok {{ color: var(--green); font-weight: bold; }}
  .fail {{ color: var(--red); font-weight: bold; }}
  .na {{ color: var(--muted); }}
  .badge {{
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 9px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }}
  .badge-ok {{ background: #d1e7dd; color: var(--green); border: 1px solid var(--green); }}
  .badge-fail {{ background: #f8d7da; color: var(--red); border: 1px solid var(--red); }}
  .badge-na {{ background: #e9ecef; color: var(--muted); border: 1px solid var(--border); }}
  .summary-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 10px;
    margin-bottom: 16px;
  }}
  .summary-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 10px 12px;
    text-align: center;
  }}
  .summary-card .value {{
    font-size: 20px;
    font-weight: bold;
    color: var(--accent);
  }}
  .summary-card .label {{
    font-size: 9px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-top: 2px;
  }}
  .timeline-event {{
    display: flex;
    gap: 12px;
    padding: 3px 0;
    border-bottom: 1px dotted var(--border);
    font-size: 10px;
  }}
  .timeline-event:last-child {{ border-bottom: none; }}
  .timeline-time {{
    color: var(--accent);
    font-weight: bold;
    min-width: 55px;
    flex-shrink: 0;
  }}
  .timeline-text {{ color: var(--text); }}
  .footer {{
    margin-top: 24px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
    font-size: 9px;
    color: var(--muted);
    text-align: center;
  }}
  .ach-list {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
  .ach-badge {{
    background: var(--surface);
    border: 1px solid var(--accent);
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 9.5px;
    color: var(--accent);
    font-weight: bold;
    text-transform: uppercase;
  }}
  .btn-print {{
    display: inline-block;
    background: var(--accent);
    color: white;
    border: none;
    padding: 8px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-family: inherit;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    margin-bottom: 16px;
  }}
  .btn-print:hover {{ opacity: 0.85; }}
</style>
</head>
<body>

<button class="btn-print no-print" onclick="window.print()">Imprimer / PDF</button>

<h1>Backup Session Report</h1>
<div class="meta">
  Session: BACKUP NAGE — {now.strftime('%d/%m/%Y')}<br>
  Generated: {now.strftime('%Y-%m-%d %H:%M:%S')}<br>
  XP: {xp_data['total_xp']} | GB backed: {xp_data['total_gb']}
</div>

<div class="summary-grid">
  <div class="summary-card">
    <div class="value">{bitperfect_count}/{total_verifications}</div>
    <div class="label">Bit-Perfect Verified</div>
  </div>
  <div class="summary-card">
    <div class="value">{xp_data['total_gb']} GB</div>
    <div class="label">Data Backed Up</div>
  </div>
  <div class="summary-card">
    <div class="value">{xp_data['total_xp']}</div>
    <div class="label">Total XP</div>
  </div>
</div>
"""

    # Achievements
    if xp_data.get("achievements"):
        html += '<h2>Achievements</h2>\n<div class="ach-list">\n'
        for a in xp_data["achievements"]:
            html += f'  <span class="ach-badge">{ach_labels.get(a, a)}</span>\n'
        html += '</div>\n'

    # Sources
    html += '<h2>Sources</h2>\n<table>\n<tr><th>Source</th><th>Path</th><th>Files</th></tr>\n'
    for s in sources:
        html += f'<tr><td>{s["label"]}</td><td>{s["path"]}</td><td>{s["files"]}</td></tr>\n'
    html += '</table>\n'

    # Hash Verification Results
    html += '<h2>Hash Verification (xxh128)</h2>\n<table>\n<tr><th>Transfer</th><th>Status</th><th>Matched</th><th>Source</th><th>Dest</th></tr>\n'
    for v in verify_results:
        status_class = "ok" if v["status"] == "BIT-PERFECT" else ("fail" if v["status"] == "MISMATCH" else "na")
        badge_class = "badge-ok" if v["status"] == "BIT-PERFECT" else ("badge-fail" if v["status"] == "MISMATCH" else "badge-na")
        src_count = v.get("total_src", 0)
        dst_count = v.get("total_dst", 0)
        html += f'<tr><td>{v["label"]}</td><td><span class="badge {badge_class}">{v["status"]}</span></td>'
        html += f'<td class="{status_class}">{v["matched"]}</td><td>{src_count}</td><td>{dst_count}</td></tr>\n'
    html += '</table>\n'

    # Hash Manifests
    html += '<h2>Hash Manifests</h2>\n<table>\n<tr><th>Manifest</th><th>Entries</th><th>File</th></tr>\n'
    for m in manifest_info:
        count = m["info"]["count"] if m["info"] else "—"
        exists = "yes" if m["info"] else "not found"
        html += f'<tr><td>{m["label"]}</td><td>{count}</td><td style="font-size:9px">{m["path"]} ({exists})</td></tr>\n'
    html += '</table>\n'

    # Disk Space
    html += '<h2>Disk Space</h2>\n<table>\n<tr><th>Volume</th><th>Used</th><th>Free</th><th>Total</th><th>%</th></tr>\n'
    for label, info in disk_info.items():
        if info:
            html += f'<tr><td>{label}</td><td>{info["used_gb"]} GB</td><td>{info["free_gb"]} GB</td>'
            html += f'<td>{info["total_gb"]} GB</td><td>{info["percent"]}%</td></tr>\n'
        else:
            html += f'<tr><td>{label}</td><td colspan="4" class="na">Not mounted</td></tr>\n'
    html += '</table>\n'

    # Timeline
    if timeline:
        html += '<h2>Wrangler Timeline</h2>\n<div style="max-height:400px;overflow-y:auto">\n'
        for ev in timeline:
            html += f'<div class="timeline-event"><span class="timeline-time">{ev["time"]}</span><span class="timeline-text">{ev["event"]}</span></div>\n'
        html += '</div>\n'

    html += f"""
<div class="footer">
  Backup Dashboard Report — generated {now.strftime('%Y-%m-%d %H:%M:%S')} — xxh128 hash verification
</div>

</body>
</html>"""

    return html


if __name__ == "__main__":
    print(generate_report_html())
