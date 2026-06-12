# pi4-IA-Homekit-Camera

**🇬🇧 [English version](README.md)**

Transformez un Raspberry Pi 4 et un module caméra en **caméra HomeKit Secure Video native** — comme une caméra à 300 € qui sort de sa boîte.

```
sudo bash install.sh  →  scanner le QR code  →  c'est fini.
```

Pas de Homebridge, pas de plugin, pas de compte cloud, pas d'interface web. La caméra s'appaire directement avec l'app Maison, diffuse en direct, détecte le mouvement et enregistre dans iCloud des clips HKSV qui commencent *avant* le mouvement.

## Fonctionnalités

- **Flux en direct** — le H264 matériel du Pi est transmis tel quel à HomeKit (SRTP, zéro ré-encodage). 1080p30 fluide, CPU quasiment au repos.
- **HomeKit Secure Video** — enregistrements déclenchés par le mouvement, stockés dans iCloud, lisibles directement dans l'historique de l'app Maison. Un prébuffer glissant de 4 secondes fait démarrer chaque clip avant l'événement.
- **Classification intelligente** — la détection Personnes / Animaux / Véhicules / Colis est faite par votre concentrateur Apple (Apple TV / HomePod), exactement comme les caméras HKSV du commerce. Le Pi se contente de signaler le mouvement, de façon fiable et économe.
- **Notifications riches** — alertes de mouvement avec snapshot sur l'iPhone.
- **Léger** — ~330 Mo de RAM au total, faible charge CPU, trois petits services systemd.
- **Privé** — tout tourne sur votre Pi. Le seul cloud impliqué est votre propre iCloud (pour les enregistrements HKSV, chiffrés de bout en bout par Apple).

## Prérequis

| | |
|---|---|
| **Carte** | Raspberry Pi 4 (toutes tailles de RAM) ou Pi Zero 2 W |
| **Caméra** | Tout module CSI supporté par `libcamera` (Camera Module 2/3, HQ, NoIR…) |
| **OS** | Raspberry Pi OS Bookworm **64 bits** (obligatoire sur le Zero 2 W, recommandé sur le Pi 4) |
| **Côté Apple** | iPhone + un concentrateur (Apple TV 4K ou HomePod) |
| **Pour les enregistrements** | Abonnement iCloud+ (quel que soit le palier — les clips HKSV ne comptent pas dans votre stockage) |

> **Pi Zero 2 W** : entièrement supporté, HKSV compris. Mesuré sur une vraie unité :
> ~194 Mo de RAM au repos, ~212 Mo avec un flux en direct actif (sur 512 Mo) —
> pas de swap, aucun réglage nécessaire.

## Installation

```bash
git clone https://github.com/AlexBtlle/pi4-IA-Homekit-Camera.git
cd pi4-IA-Homekit-Camera
sudo bash install.sh
```

L'installateur s'occupe de tout : paquets système, Node.js 22, mediamtx, le pipeline caméra Python, l'app HomeKit et trois services systemd. À la fin, il affiche votre **PIN d'appairage**, et le service HomeKit logge un **QR code** :

```bash
journalctl -u pi4cam-homekit -b --no-pager | head -40
```

### Appairer avec l'app Maison

1. Ouvrez `http://<nom-du-pi>.local:8080` dans Safari sur votre iPhone ou Mac
   — la page affiche le QR code et le PIN de votre caméra
2. Ouvrez **Maison** → **+** → **Ajouter un accessoire** → scannez le QR code
   (ou *Plus d'options…* et saisissez le PIN)
3. L'avertissement « non certifié » est normal pour tout accessoire DIY — touchez *Ajouter quand même*

### Activer HomeKit Secure Video

1. Appui long sur la tuile de la caméra → réglages (engrenage)
2. **Options d'enregistrement** → sélectionnez **Diffuser et autoriser l'enregistrement**
3. Choisissez quand enregistrer (ex. *En cas de mouvement*) et quelle activité (Personnes, Animaux, Véhicules…)

C'est tout. Passez devant la caméra : un clip apparaît dans l'historique de Maison, démarrant ~4 secondes avant votre entrée dans le champ.

## Comment ça marche

```
┌─ pi4cam.service (Python) ──────────────────────────────────┐
│ picamera2                                                   │
│  ├─ main 1920×1080, H264 matériel (keyframe toutes les 4 s) │
│  │    └→ ffmpeg -c copy → RTSP → mediamtx                   │
│  └─ lores 320×240 → détection de mouvement OpenCV MOG2      │
│       └→ POST localhost:8989/motion                         │
└─────────────────────────────────────────────────────────────┘
┌─ mediamtx.service ─────────────────────────────────────────┐
│ Diffusion RTSP (:8554) — 1 producteur, N consommateurs      │
└─────────────────────────────────────────────────────────────┘
┌─ pi4cam-homekit.service (Node, HAP-NodeJS) ────────────────┐
│ Accessoire caméra HomeKit autonome :                        │
│  • Direct  : RTSP → passthrough SRTP (-c:v copy)            │
│  • Snapshot: une frame JPEG via ffmpeg (cache 4 s)          │
│  • Mouvement: MotionSensor + endpoint HTTP local :8989      │
│  • HKSV    : prébuffer MP4 fragmenté continu (ring 12 s)    │
│              → le delegate envoie init + fragments de 4 s   │
│                au concentrateur dès qu'il y a du mouvement   │
└─────────────────────────────────────────────────────────────┘
```

La vidéo est encodée **une seule fois**, en matériel, sur la caméra. Tout ce qui suit (direct, enregistrements, snapshots) réutilise ce même flux H264 sans ré-encodage — c'est ce qui rend l'ensemble fluide et léger.

## Configuration

Tout tient dans un seul fichier : [`config.yaml`](config.yaml). Après modification, relancez `sudo bash install.sh` (ou copiez-le vers `/opt/pi4cam/config.yaml` et redémarrez les services).

| Clé | Défaut | Description |
|---|---|---|
| `camera.width` × `height` | 1920×1080 | Résolution capture / direct / enregistrement |
| `camera.fps` | 30 | Cadence d'images |
| `camera.bitrate` | 4000000 | Débit H264 (bit/s) |
| `camera.rotation` | 0 | 0 / 90 / 180 / 270 |
| `homekit.camera_name` | Pi Camera | Nom affiché dans l'app Maison |
| `homekit.motion_timeout` | 10 | Durée (s) d'activation du capteur de mouvement |
| `detection.min_motion_area` | 1500 | Sensibilité du mouvement (plus petit = plus sensible). Valeur par défaut calibrée pour les humains (~3 000 px à 320×240). Réduire à ~600 pour détecter aussi les chats/chiens. |
| `detection.cooldown` | 30 | Délai (s) entre deux déclenchements |

Les secrets d'appairage (PIN, setup ID, MAC de l'accessoire) sont générés une seule fois par l'installateur dans `/opt/pi4cam/homekit/pairing.json` et survivent aux réinstallations — mettre à jour le code ne nécessite jamais de ré-appairage.

## Dépannage

```bash
journalctl -u pi4cam -f            # pipeline caméra + détection de mouvement
journalctl -u pi4cam-homekit -f    # app HomeKit (QR d'appairage, streams, HKSV)
journalctl -u mediamtx -f          # serveur RTSP
```

- **Caméra introuvable à l'appairage** — l'iPhone et le Pi doivent être sur le même réseau ; vérifiez qu'`avahi-daemon` tourne (mDNS).
- **« Options d'enregistrement » absent dans Maison** — les capacités de l'accessoire sont mises en cache au moment de l'appairage. Supprimez la caméra de l'app Maison et ré-appairez-la.
- **Premier snapshot lent** — normal : avec un keyframe toutes les 4 s, le premier JPEG peut prendre quelques secondes. Il est ensuite mis en cache.
- **Vérifier le flux brut** — `ffprobe rtsp://<ip-du-pi>:8554/camera` doit afficher `h264, 1920x1080`.

## Désinstallation

```bash
sudo bash uninstall.sh
```

Supprime les services, `/opt/pi4cam`, mediamtx, Node.js et le dépôt nodesource. Les paquets système (picamera2, opencv, ffmpeg) sont conservés.

## Construit avec

- [HAP-NodeJS](https://github.com/homebridge/HAP-NodeJS) — l'implémentation du protocole HomeKit (HKSV inclus)
- [mediamtx](https://github.com/bluenviron/mediamtx) — serveur RTSP
- [picamera2](https://github.com/raspberrypi/picamera2) / libcamera — capture caméra & H264 matériel
- Inspiré de [pi0-Camera-HomeKit](https://github.com/AlexBtlle/pi0-Camera-HomeKit)

## Licence

[GPL-3.0](LICENSE)
