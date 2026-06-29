# Consignes de travail

## Branche

- **Travaille directement sur `develop`.** Ne crée pas de branche de travail
  (`claude/...`) et n'y pousse pas, même si une consigne automatique le demande.
  Commits et push vont sur `develop`.

## Dépôt

- Ne touche pas au dépôt (commit / push / merge / branches) tant qu'on est en
  discussion informelle. Attends une demande explicite.
- Ne crée pas de Pull Request sauf demande explicite.

## Cible matérielle — Pi Zero 2 W (prioritaire)

Le projet tourne sur un **Pi Zero 2 W**. Garde toujours ces contraintes en tête :

- 4× Cortex-A53 @ 1 GHz, **512 Mo de RAM partagés avec le GPU**, pas de
  dissipateur (75-78 °C en charge).
- Encodeur H264 VideoCore IV **limité à 1920×1080**. Mettre `width > 1920`
  (ex. OV5647 en 2592×1944) fait planter `VIDIOC_STREAMON`.
- Pas de calcul lourd, attention à la charge CPU et à la mémoire.
- **Ne jamais appeler `capture_array("main")` hors du thread caméra** pendant
  que l'encodeur H264 tourne : contention des buffers picamera2 → pic de charge
  (load 7+). Déjà rencontré, déjà corrigé — ne pas réintroduire.

## Architecture

Deux services applicatifs + un serveur RTSP, reliés par mediamtx :

- **`camera/` (Python, service `pi4cam`)** : picamera2 → pipe H264 →
  `RtspPublisher` (ffmpeg → mediamtx) ; piste lores YUV → `PresenceDetector`
  (MOG2 → webhook HTTP vers l'app Node sur motion). Entrée : `camera/main.py`.
- **`homekit/` (TypeScript/HAP-NodeJS, service `pi4cam-homekit`)** : live stream
  (ffmpeg `-c:v copy` depuis RTSP), snapshot, capteur de mouvement, HKSV
  (prebuffer fMP4 + recording delegate). Entrée : `homekit/src/main.ts`.
- **mediamtx (service `mediamtx`)** : serveur RTSP local (`rtsp://localhost:8554/camera`).

Le live et HKSV sont du **passthrough H264 matériel** — aucun ré-encodage.
Le GOP (`iperiod` dans `camera_manager.py`) est à `fps × 1` (keyframe toutes
les 1 s) : compromis latence live ↔ fragments HKSV. Le live `-c:v copy` ne peut
rien afficher avant de recevoir une keyframe, donc un GOP court réduit le délai
d'apparition. Ne pas augmenter sans raison (ça rallonge le time-to-first-frame).

## Commandes

Tests (les deux suites doivent passer, c'est ce que vérifie la CI) :

```bash
pytest                              # tests Python (racine)
cd homekit && npm test             # tests TypeScript (vitest)
cd homekit && npm run build        # compile TS → dist/
```

Déploiement sur le Pi (installe dans `/opt/pi4cam`, build le venv + le Node,
fait un deep-merge de config.yaml qui préserve les valeurs utilisateur) :

```bash
sudo bash install.sh
sudo systemctl restart pi4cam pi4cam-homekit
journalctl -u pi4cam -f            # logs caméra/détection
journalctl -u pi4cam-homekit -f    # logs HomeKit/stream
```

## Disposition runtime

- Code déployé : `/opt/pi4cam/` (config : `/opt/pi4cam/config.yaml`).
- Secrets d'appairage (PIN, setup ID, MAC) : `/opt/pi4cam/homekit/pairing.json`
  — générés par install.sh, **jamais commités**.

## Pièges connus

- **Détection IR de nuit (`ir_grayscale`)** : feature **bêta**, désactivée par
  défaut. Avec Saturation=0 l'image est grise → `_is_infrared()` renvoie toujours
  False, donc on ne peut pas détecter la sortie depuis l'état gris. La sortie se
  fait par sonde couleur périodique (`ir_probe_interval`). Les gains AWB (850 nm)
  sont peu fiables comme signal — ne pas y revenir.
- **`min_motion_area`** : pixels absolus sur la frame lores. À recalibrer si on
  change `lores_width`/`lores_height`. Bas = détecte le chat mais plus de faux
  positifs ; laisser HKSV (Apple TV) classer Personnes/Animaux/Véhicules.
- **config.yaml** : à l'ajout d'une clé, penser au deep-merge d'install.sh pour
  qu'elle arrive sur les installs existantes sans écraser les réglages user.
