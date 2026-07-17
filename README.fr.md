[English](README.md) · **Français**

# DIT Backup Protocol — Niveau Silverstack pour la production cinéma

Protocole de sauvegarde open-source pour les rushes cinéma. Équivalent DIT Netflix / Silverstack avec des outils ouverts.

> Ceci est la version publique et open-source d'un pipeline de production privé, utilisé sur de vrais tournages. Les chemins, noms de machines et identifiants sont généralisés en variables d'environnement pour l'usage public : la version privée reste un dépôt séparé, spécifique à la production.

## Protocole

```
SOURCE (carte/SSD/téléphone)
  │
  ├── [1] Copie + Hash → NAV1 (USB local)
  │       Passe unique : tee + xxhsum128
  │
  ├── [2] rsync SSH → NAV2 (poste réseau local)
  │       Puis : rclone hashsum xxh128 → vérification BIT-PERFECT
  │
  └── [3] rclone → R2/S3 (depuis le poste, lecture locale)
          rclone check verify
          synchronisation NAS depuis le cloud
```

## Séquencement strict (règle Silverstack)

```
Copie → Hash → BIT-PERFECT → Upload → Vérification → NAS → Manifeste
```

**Jamais** d'upload avant vérification du hash.
**Jamais** deux opérations d'entrée/sortie sur le même disque dur simultanément.

## Scripts

| Script | Rôle |
|--------|------|
| `silverstack_wrangler.sh` | Orchestrateur complet avec séquencement strict |
| `dashboard_server.py` | Dashboard HTTP temps réel (port 4242) |
| `dashboard.html` | Interface du dashboard |

## Fonctionnalités du dashboard

- Progression du transfert en temps réel (%, vitesse, ETA)
- Progression de la vérification des hashs avec compte de fichiers
- Statut BIT-PERFECT par destination
- Jauges d'espace disque (Nav1 + Nav2)
- Détection automatique LAN vs Tailscale
- Système XP (gamification pour les longues sessions)

## Mise en place

```bash
# Configurer les variables d'environnement
export NOMAD_SSH="user@workstation-ip"
export MINI_SSH="user@nas-gateway-ip"
export R2_BUCKET_NAME="your-r2-bucket"

# Lancer le dashboard
python3 dashboard_server.py
# → http://localhost:4242

# Lancer l'orchestrateur
bash silverstack_wrangler.sh
```

## Prérequis

- `xxhsum` (xxHash) sur Mac
- `rclone` sur le poste Windows + la passerelle NAS
- `rsync` sur toutes les machines
- Accès SSH entre les machines

## Vitesses (typiques)

| Opération | Vitesse |
|-----------|---------|
| Source → Nav1 (USB 3.0) | 150-220 Mo/s |
| Source → Nav2 (LAN 1 Gbe) | 42-50 Mo/s |
| Nav2 → cloud R2 | ~50 Mo/s |
| R2 → NAS | ~89 Mo/s |

## Licence

MIT

Par [Ismaël Joffroy Chandoutis](https://ismaeljoffroychandoutis.com).
