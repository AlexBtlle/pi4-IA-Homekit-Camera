# pi4-IA-Homekit-Camera

**🇬🇧 [English version](README.md)**

Transformez un Raspberry Pi et un module caméra en **caméra HomeKit Secure Video native** comme une caméra qui sort de sa boîte.

```
Installer →  scanner le QR code  →  c'est fini.
```

Pas de Homebridge, pas de plugin, pas de compte cloud, pas d'interface d'administration à surveiller. La caméra s'appaire directement avec l'app Maison, diffuse en direct, détecte le mouvement et enregistre dans iCloud des clips HKSV qui commencent *avant* le mouvement.

## Fonctionnalités

- **Flux en direct** — le H264 matériel du Pi est transmis tel quel à HomeKit (SRTP, zéro ré-encodage). 1080p30 fluide, CPU quasiment au repos. Les contrôleurs IPv6 sont pris en charge (*bêta* — implémenté selon la spec, pas encore validé sur un réseau IPv6-preferred ; retours bienvenus).
- **HomeKit Secure Video** — enregistrements déclenchés par le mouvement, stockés dans iCloud, lisibles directement dans l'historique de l'app Maison. Un prébuffer glissant de 4 secondes fait démarrer chaque clip avant l'événement.
- **Classification intelligente** — la détection Personnes / Animaux / Véhicules est faite par votre concentrateur Apple (Apple TV / HomePod), exactement comme les caméras HKSV du commerce. Le Pi se contente de signaler le mouvement, de façon fiable et économe.
- **Notifications riches** — alertes de mouvement avec snapshot sur l'iPhone.
- **Tableau de bord** — une page web intégrée (`http://<pi>.local:8080`) affiche le QR code d'appairage et un état en temps réel : statut global, température & throttling, charge CPU, RAM/swap, uptime, statut par service, fraîcheur du snapshot, état HKSV et dernier mouvement.
- **Léger** — ~210 Mo de RAM avec un flux actif, faible charge CPU, trois petits services systemd.
- **Privé** — tout tourne sur votre Pi. Le flux RTSP est restreint à localhost (jamais exposé sur le réseau) ; le seul cloud impliqué est votre propre iCloud (pour les enregistrements HKSV, chiffrés de bout en bout par Apple).

## Prérequis

| | |
|---|---|
| **Carte** | Raspberry Pi 4 (toutes tailles de RAM), Pi Zero 2 W, Pi 3. |
| **Caméra** | Tout module CSI supporté par `libcamera` (Camera Module 2/3, HQ, NoIR…) |
| **OS** | Raspberry Pi OS **64 bits** |
| **Côté Apple** | iPhone + un concentrateur (Apple TV 4K ou HomePod) |
| **Pour les enregistrements** | Abonnement iCloud+ (quel que soit le palier — les clips HKSV ne comptent pas dans votre stockage) |

> **Pi Zero 2 W** : entièrement supporté, HKSV compris. Mesuré sur une vraie unité :
> ~194 Mo de RAM au repos, ~212 Mo avec un flux en direct actif (sur 512 Mo). Le swap
> zram est bel et bien utilisé (~180 Mo, davantage quand l'enregistrement HKSV est
> armé) — c'est de la RAM compressée, pas la carte SD, et c'est absorbé sans réglage.
> Un **dissipateur thermique** est fortement recommandé : le SoC chauffe sous charge continue.
> Même avec un dissipateur pleine carte, comptez ~75–80 °C et du throttling ponctuel en
> boîtier fermé — ajoutez des trous de ventilation ou un petit ventilateur 5 V pour
> rester confortablement en dessous.

## Flasher la carte SD

En partant d'une carte vierge, utilisez **[Raspberry Pi Imager](https://www.raspberrypi.com/software/)** :

1. **Choisir l'OS** → *Raspberry Pi OS (other)* → **Raspberry Pi OS Lite (64 bits)**. Lite suffit — la caméra tourne sans écran ; le 64 bits est obligatoire sur le Zero 2 W.
2. **Choisir le stockage** → votre carte SD.
3. Cliquez sur la **roue crantée** (⚙ / `Ctrl+Maj+X`) pour ouvrir les réglages avancés, afin que le Pi démarre directement sur votre réseau, sans écran ni clavier :
   - **Nom d'hôte** (ex. `cam-pi-zero`) — la caméra sera accessible sur `http://<nom-hote>.local`
   - **Activer SSH** (mot de passe ou clé publique)
   - **Wi-Fi** SSID + mot de passe (et votre pays)
   - **Nom d'utilisateur / mot de passe**, locale et clavier
4. **Écrire** l'image, insérez la carte et allumez le Pi.
5. Connectez-vous en SSH, puis suivez l'**Installation** ci-dessous :
   ```bash
   ssh <utilisateur>@<nom-hote>.local
   ```

## Installation

```bash
git clone https://github.com/AlexBtlle/pi4-IA-Homekit-Camera.git
cd pi4-IA-Homekit-Camera
sudo bash install.sh
```

L'installateur s'occupe de tout : paquets système, Node.js 22, mediamtx, le pipeline caméra Python, l'app HomeKit et trois services systemd. **Rien n'est compilé sur votre Pi** — les binaires lourds (mediamtx, le ffmpeg allégé qui fait démarrer le direct en ~0,2 s) sont téléchargés précompilés depuis les releases, avec vérification du checksum. À la fin, il affiche votre **PIN d'appairage**, et le service HomeKit logge un **QR code** :

```bash
journalctl -u pi4cam-homekit -b --no-pager | head -40
```

### Appairer avec l'app Maison

1. Ouvrez `http://<nom-du-pi>.local:8080` dans Safari sur votre iPhone ou Mac
   — la page affiche le QR code et le PIN de votre caméra, ainsi qu'un tableau
   de bord en temps réel (services, température, mémoire, mouvement)
2. Ouvrez **Maison** → **+** → **Ajouter un accessoire** → scannez le QR code
   (ou *Plus d'options…* et saisissez le PIN)
3. L'avertissement « non certifié » est normal pour tout accessoire DIY — touchez *Ajouter quand même*

### Activer HomeKit Secure Video

1. Appui long sur la tuile de la caméra → réglages (engrenage)
2. **Options d'enregistrement** → sélectionnez **Diffuser et autoriser l'enregistrement**
3. Choisissez quand enregistrer (ex. *En cas de mouvement*) et quelle activité (Personnes, Animaux, Véhicules…)

C'est tout. Passez devant la caméra : un clip apparaît dans l'historique de Maison, démarrant ~4 secondes avant votre entrée dans le champ.

## Mise à jour

Pour mettre à jour une installation existante vers la dernière version :

```bash
cd pi4-IA-Homekit-Camera
git pull
sudo bash install.sh
```

L'installateur reconstruit ce qui a changé et redémarre lui-même les trois services. La mise à jour est sûre par conception :

- **Vos réglages sont préservés** — `/opt/pi4cam/config.yaml` n'est jamais écrasé ; seules les clés introduites par la nouvelle version sont ajoutées (les défauts annotés sont écrits dans `/opt/pi4cam/config.yaml.dist` pour comparaison).
- **Pas de ré-appairage** — les secrets d'appairage survivent aux mises à jour : la caméra garde son identité dans l'app Maison et son historique HKSV.

Après la mise à jour, ouvrez `http://<pi>.local:8080` et vérifiez que tout est au vert.

## Comment ça marche

```
┌─ pi4cam.service (Python) ──────────────────────────────────┐
│ picamera2                                                   │
│  ├─ main 1920×1080, H264 matériel (keyframe toutes les 1 s) │
│  │    └→ ffmpeg -c copy → RTSP → mediamtx                   │
│  ├─ main YUV420 → snapshot JPEG → /dev/shm toutes les 2 s   │
│  └─ lores 320×240 → détection de mouvement OpenCV MOG2      │
│       └→ POST localhost:8989/motion                         │
│  (watchdog : redémarre sur timeout du frontend libcamera)   │
└─────────────────────────────────────────────────────────────┘
┌─ mediamtx.service ─────────────────────────────────────────┐
│ Diffusion RTSP (127.0.0.1:8554) — 1 producteur, N conso.    │
└─────────────────────────────────────────────────────────────┘
┌─ pi4cam-homekit.service (Node, HAP-NodeJS) ────────────────┐
│ Accessoire caméra HomeKit autonome :                        │
│  • Direct  : RTSP → passthrough SRTP (-c:v copy)            │
│  • Snapshot: sert le dernier JPEG de tmpfs (instantané)     │
│  • Mouvement: MotionSensor + endpoint HTTP local :8989      │
│  • HKSV    : prébuffer MP4 fragmenté continu (ring 6 s)     │
│              → le delegate envoie init + fragments live     │
│                au concentrateur dès qu'il y a du mouvement  │
│  • Web     : QR + tableau de bord sur :8080                 │
└─────────────────────────────────────────────────────────────┘
```

La vidéo est encodée **une seule fois**, en matériel, sur la caméra. Tout ce qui suit (direct, enregistrements, snapshots) réutilise ce même flux H264 sans ré-encodage — c'est ce qui rend l'ensemble fluide et léger.

### Les limites assumées du passthrough

Le zéro ré-encodage est le choix qui rend le projet viable sur Pi Zero 2 W — et il implique d'ignorer délibérément une partie de ce que HomeKit négocie. Ce sont des tolérances connues, partagées par tout l'écosystème DIY (Scrypted, homebridge-camera-ffmpeg…), documentées ici par transparence :

- **Résolution fixe** — HomeKit choisit une résolution dans la liste annoncée (souvent 640×360 en grille ou à distance) mais reçoit toujours le flux natif (1080p par défaut). iOS redimensionne côté client.
- **Débit** *(le seul paramètre négocié qui EST honoré)* — les sessions live pilotent l'encodeur matériel vers ce qu'elles négocient (~2 Mbps en distant/cellulaire), et il remonte au plafond configuré quand elles se ferment. Sur un uplink modeste, envisager aussi un `camera.bitrate` de 3-4 Mbps.
- **Profil H264** — le flux est toujours en High profile quel que soit le profil négocié ; les décodeurs Apple le lisent sans broncher.
- **Configuration d'enregistrement HKSV** — le profil/bitrate/iFrameInterval sélectionnés par le concentrateur ne sont pas appliqués (même raison passthrough) ; les clips iCloud pèsent ce que la caméra encode.
- **Audio live fantôme** — un bloc audio AAC-ELD est déclaré parce que HomeKit en exige un dans la négociation, mais aucun paquet audio n'est jamais émis (le module caméra n'a pas de micro). L'icône haut-parleur de l'app Maison ne produit rien.
- **Snapshots** — toujours servis en 1280×720 quelle que soit la taille demandée ; iOS redimensionne.

## Configuration

Tout tient dans un seul fichier : [`config.yaml`](config.yaml). Sur un système installé, modifiez directement `/opt/pi4cam/config.yaml` puis redémarrez les services (`sudo systemctl restart pi4cam pi4cam-homekit`). Relancer `install.sh` n'écrase jamais vos valeurs — il n'ajoute que les clés introduites par les nouvelles versions (une référence annotée est conservée dans `/opt/pi4cam/config.yaml.dist`).

| Clé | Défaut | Description |
|---|---|---|
| `camera.source` | csi | `csi` (module caméra Pi) ou `usb` (webcam UVC, *bêta* — voir [TROUBLESHOOTING](TROUBLESHOOTING.md#usb-webcam-beta)) |
| `camera.device` | /dev/video0 | Périphérique V4L2 (`source: usb` uniquement) |
| `camera.usb_format` | mjpeg | Sortie de la webcam : `mjpeg` / `yuyv` / `h264` (`source: usb` uniquement) |
| `camera.width` × `height` | 1920×1080 | Résolution capture / direct / enregistrement |
| `camera.fps` | 30 | Cadence d'images |
| `camera.bitrate` | 8000000 | Débit H264 (bit/s) — ~8 Mbps pour un 1080p30 net ; descendre à ~4 Mbps pour économiser la bande passante |
| `camera.day_min_bitrate` | 500000 | Débit plancher (bit/s) que le gouverneur live peut demander de jour. C'est le minimum pratique de l'encodeur ; le remonter coûte de la bande passante/du stockage sans gain prouvé. Toujours plafonné par `camera.bitrate`. |
| `camera.night_min_bitrate` | 3000000 | Même plancher une fois le mode nuit IR actif. L'étirement auto-levels amplifie le bruit sur toute l'image, qui se pixellise sous ~3 Mbps (testé sur le terrain, 4G comprise). Toujours plafonné par `camera.bitrate`. |
| `camera.rotation` | 0 | 0 / 180 uniquement — l'ISP du Pi ne sait pas pivoter à 90°/270° (valeur ignorée avec un avertissement) |
| `camera.full_fov` | true | Utilise toute la surface du capteur pour exploiter l'angle complet de l'objectif. La plupart des capteurs (IMX219, OV5647…) recadrent au centre en mode 1080p natif, ce qui rétrécit le champ ; cette option force un mode pleine vue (binned) puis redimensionne à la résolution de sortie. Mettre `false` pour le recadrage natif, plus net mais plus serré. |
| `camera.sharpness` | 1.0 | Accentuation ISP (0.0–16.0). Essayer 1.5–2.0 pour compenser la mollesse de l'objectif. |
| `camera.contrast` | 1.0 | Contraste ISP (0.0–32.0). |
| `camera.saturation` | 1.0 | Saturation couleur ISP (0.0–32.0). Essayer 1.2–1.5 pour des couleurs plus riches. |
| `camera.day_ev` | 0.0 | **(bêta)** Éclaircissement **automatique** des scènes **couleur** sombres, en EV/stops (+1 = ×2 la cible AE). Activé **uniquement quand la scène est sombre** (le capteur sature son gain analogique), avec hystérésis ; de jour le déclencheur reste inactif et l'image n'est pas touchée — aucune surexposition d'une pièce ensoleillée. Là où il agit, le capteur est déjà saturé : il éclaircit via le gain numérique de l'ISP (plus clair mais plus bruité). La nuit/IR a son propre chemin (`ir_grayscale`). Plage utile 0.0–1.5 ; 0.0 = off. |
| `camera.ir_grayscale` | false | **(bêta)** Bascule le flux **et** la miniature en niveaux de gris en vision nocturne IR, supprimant la dominante rose du 850 nm. L'IR est détecté sur les statistiques chroma du flux de détection (avec hystérésis), et l'effet neutralise les plans couleur du frame avant encodage — les transitions jour/nuit sont mesurées sur de vraies données couleur. |
| `camera.snapshot_path` | /dev/shm/pi4cam-snapshot.jpg | Emplacement d'écriture de la miniature JPEG — un chemin tmpfs (RAM), pour éviter l'usure de la carte SD due aux réécritures 24/7. |
| `homekit.camera_name` | Pi Camera | Nom affiché dans l'app Maison |
| `homekit.motion_timeout` | 10 | Durée (s) d'activation du capteur de mouvement |
| `detection.min_motion_area` | 1500 | Sensibilité du mouvement, en pixels absolus sur la frame basse résolution (plus petit = plus sensible). 1500 est calibré pour les humains à 320×240 ; réduire à ~300–600 pour détecter aussi les chats/chiens. À recalibrer si vous changez `lores_width`/`lores_height`. |
| `detection.cooldown` | 30 | Délai (s) entre deux *épisodes* de mouvement. Un mouvement continu garde le capteur — et le clip HKSV — actif pendant toute sa durée. |

Les secrets d'appairage (PIN, setup ID, MAC de l'accessoire) sont générés une seule fois par l'installateur dans `/opt/pi4cam/homekit/pairing.json` et survivent aux réinstallations — mettre à jour le code ne nécessite jamais de ré-appairage.

## Dépannage

Pour un guide complet symptôme par symptôme (thermique/throttling, mémoire &
swap, latence du stream, réglage de la détection, sauvegarde d'appairage…),
voir **[TROUBLESHOOTING.md](TROUBLESHOOTING.md)** (en anglais).

```bash
journalctl -u pi4cam -f            # pipeline caméra + détection de mouvement
journalctl -u pi4cam-homekit -f    # app HomeKit (QR d'appairage, streams, HKSV)
journalctl -u mediamtx -f          # serveur RTSP
```

- **Caméra introuvable à l'appairage** — l'iPhone et le Pi doivent être sur le même réseau ; vérifiez qu'`avahi-daemon` tourne (mDNS).
- **« Options d'enregistrement » absent dans Maison** — les capacités de l'accessoire sont mises en cache au moment de l'appairage. Supprimez la caméra de l'app Maison et ré-appairez-la.
- **Snapshot figé** — le pipeline Python rafraîchit `/dev/shm/pi4cam-snapshot.jpg` toutes les 2 s. S'il cesse de se mettre à jour, consultez `journalctl -u pi4cam` (le watchdog redémarre automatiquement le service en cas de timeout libcamera).
- **Vérifier l'état des services** — ouvrez `http://<pi>.local:8080` : le tableau de bord indique le statut de chaque service, la température & le throttling, la mémoire/swap et le nombre de détections.
- **Vérifier le flux brut** — le flux RTSP est restreint à localhost pour des raisons de confidentialité ; sondez-le donc *depuis le Pi lui-même* : `ffprobe rtsp://127.0.0.1:8554/camera` doit afficher `h264, 1920x1080`.

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
