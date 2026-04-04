"""LUT Preview — parse .cube LUT files and expose via API."""

import os, re
from pathlib import Path

# Locations to scan for .cube files
LUT_SEARCH_PATHS = [
    "/Volumes/NavTGV1",
    os.path.expanduser("~/Library/ColorSync/Profiles"),
    "/tmp/backup_luts",
]


def list_luts() -> list[dict]:
    """Scan known locations for .cube LUT files. Returns list of {name, path, size_kb}."""
    results = []
    seen = set()
    for base in LUT_SEARCH_PATHS:
        base_path = Path(base)
        if not base_path.exists():
            continue
        for cube in sorted(base_path.rglob("*.cube")):
            real = str(cube.resolve())
            if real in seen:
                continue
            seen.add(real)
            try:
                results.append({
                    "name": cube.stem,
                    "path": str(cube),
                    "size_kb": round(cube.stat().st_size / 1024, 1),
                })
            except OSError:
                continue
    return results


def parse_lut(filepath: str) -> dict:
    """Parse a .cube LUT file and return metadata + data as JSON-safe dict.

    Supports 1D and 3D LUT formats.
    Returns: {title, type, size, domain_min, domain_max, data}
    - data is a flat list of [r,g,b] triplets (floats)
    """
    p = Path(filepath)
    if not p.exists() or p.suffix.lower() != ".cube":
        return {"error": f"Not a valid .cube file: {filepath}"}

    title = p.stem
    lut_type = None  # "1D" or "3D"
    size = 0
    domain_min = [0.0, 0.0, 0.0]
    domain_max = [1.0, 1.0, 1.0]
    data = []

    try:
        with open(p, "r", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue

                upper = line.upper()

                if upper.startswith("TITLE"):
                    # TITLE "Some Name"
                    m = re.match(r'TITLE\s+"?(.+?)"?\s*$', line, re.IGNORECASE)
                    if m:
                        title = m.group(1)

                elif upper.startswith("LUT_1D_SIZE"):
                    lut_type = "1D"
                    size = int(line.split()[-1])

                elif upper.startswith("LUT_3D_SIZE"):
                    lut_type = "3D"
                    size = int(line.split()[-1])

                elif upper.startswith("DOMAIN_MIN"):
                    parts = line.split()
                    if len(parts) >= 4:
                        domain_min = [float(parts[1]), float(parts[2]), float(parts[3])]

                elif upper.startswith("DOMAIN_MAX"):
                    parts = line.split()
                    if len(parts) >= 4:
                        domain_max = [float(parts[1]), float(parts[2]), float(parts[3])]

                else:
                    # Data line — three floats
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
                            data.append([r, g, b])
                        except ValueError:
                            continue

    except Exception as e:
        return {"error": str(e)}

    # Validate
    if not lut_type:
        # Infer from data count
        n = len(data)
        # Check if n is a perfect cube
        cube_root = round(n ** (1.0 / 3.0))
        if cube_root ** 3 == n and cube_root > 1:
            lut_type = "3D"
            size = cube_root
        elif n > 0:
            lut_type = "1D"
            size = n

    return {
        "title": title,
        "type": lut_type or "unknown",
        "size": size,
        "domain_min": domain_min,
        "domain_max": domain_max,
        "data": data,
        "entries": len(data),
    }
