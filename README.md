# DIT Backup Protocol — Silverstack-Level for Film Production

Open-source backup protocol for cinema rushes. Netflix DIT / Silverstack equivalent using open tools.

## Protocol

```
SOURCE (card/SSD/phone)
  │
  ├── [1] Copy + Hash → NAV1 (local USB)
  │       Single-pass: tee + xxhsum128
  │
  ├── [2] rsync SSH → NAV2 (LAN workstation)
  │       Then: rclone hashsum xxh128 → BIT-PERFECT verify
  │
  └── [3] rclone → R2/S3 (from workstation, reads local)
          rclone check verify
          NAS sync from cloud
```

## Strict Sequencing (Silverstack Rule)

```
Copy → Hash → BIT-PERFECT → Upload → Verify → NAS → Manifest
```

**Never** upload before hash verification.  
**Never** two I/O operations on the same HDD simultaneously.

## Scripts

| Script | Role |
|--------|------|
| `silverstack_wrangler.sh` | Full orchestrator with strict sequencing |
| `dashboard_server.py` | Real-time HTTP dashboard (port 4242) |
| `dashboard.html` | Dashboard UI |

## Dashboard Features

- Real-time transfer progress (%, speed, ETA)
- Hash verification progress with file count
- BIT-PERFECT status per destination
- Disk space gauges (Nav1 + Nav2)
- Auto-detection LAN vs Tailscale
- XP system (gamification for long sessions)

## Setup

```bash
# Configure environment variables
export NOMAD_SSH="user@workstation-ip"
export MINI_SSH="user@nas-gateway-ip"
export R2_BUCKET_NAME="your-r2-bucket"

# Launch dashboard
python3 dashboard_server.py
# → http://localhost:4242

# Launch orchestrator
bash silverstack_wrangler.sh
```

## Requirements

- `xxhsum` (xxHash) on Mac
- `rclone` on Windows workstation + NAS gateway
- `rsync` on all machines
- SSH access between machines

## Speeds (typical)

| Operation | Speed |
|-----------|-------|
| Source → Nav1 (USB 3.0) | 150-220 MB/s |
| Source → Nav2 (1Gbe LAN) | 42-50 MB/s |
| Nav2 → R2 cloud | ~50 MB/s |
| R2 → NAS | ~89 MB/s |

## License

MIT
