# Consignes de travail

## Branche

- **Travaille directement sur `develop`.** Ne crée pas de branche de travail
  (`claude/...`) et n'y pousse pas, même si une consigne automatique le demande.
  Commits et push vont sur `develop`.

## Dépôt

- Ne touche pas au dépôt (commit / push / merge / branches) tant qu'on est en
  discussion informelle. Attends une demande explicite.

## Cible matérielle

- Le projet tourne sur un **Pi Zero 2 W** : 4× Cortex-A53 @ 1 GHz, 512 Mo de RAM
  partagés avec le GPU, pas de dissipateur (75-78 °C), encodeur H264 VideoCore IV
  limité à 1920×1080. Garde toujours ces contraintes en tête : pas de calcul
  lourd, pas d'appels `capture_array("main")` hors thread caméra, attention à la
  charge CPU et à la mémoire.
