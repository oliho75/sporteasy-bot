# SitéBad booking bot

Réserve automatiquement tes créneaux Sporteasy dès qu'ils ouvrent (J-2, même heure).
Tourne sur ton Mac. Tes identifiants ne quittent jamais la machine.

## 1. Install (une fois, ~3 min)
```bash
cd sporteasy-bot
chmod +x run.sh
./run.sh install        # venv + dépendances + Chrome headless
open -e .env            # remplis SPORTEASY_EMAIL / SPORTEASY_PASSWORD (+ ACCESS_TOKEN si iPhone)
./run.sh                # → http://localhost:8765
```

## 2. Utilisation
- Calendrier des 6 prochaines semaines avec les vrais créneaux du club et le nombre d'inscrits.
- **1 clic** sur un créneau = priorité · **2 clics** = fallback · **3 clics** = retiré.
  Un fallback n'est réservé que si aucun prioritaire du même jour n'est pris, et il est libéré
  automatiquement si un prioritaire du même jour est obtenu ensuite.
- Le bot poll toutes les 30 s ; 2 min avant l'ouverture il passe en mode rapide (1,5 s) et réserve
  à la seconde où ça ouvre. Ouverture manuelle en retard par un admin → attrapée au tick suivant.
- Notif macOS (+ webhook optionnel) à chaque résa / échec. Journal dans l'UI et `bot.log`.
- Garde-fou : `MAX_PER_WEEK` résas par semaine (modifiable dans l'UI).

## 3. Tourner en permanence (launchd)
```bash
sed "s#__DIR__#$(pwd)#g" com.ao.sporteasy-bot.plist > ~/Library/LaunchAgents/com.ao.sporteasy-bot.plist
launchctl load ~/Library/LaunchAgents/com.ao.sporteasy-bot.plist
```
Le Mac doit être **réveillé** à l'heure d'ouverture (samedi 19h30 pour le Rigal du lundi, etc.) :
Réglages → Batterie → Options → « Empêcher la suspension automatique quand l'écran est éteint »,
ou lance `caffeinate -s` dans un terminal. Un Mac fermé = pas de résa, point.

## 4. Depuis l'iPhone (Tailscale, gratuit)
Le serveur reste sur localhost ; Tailscale le publie en HTTPS sur ton réseau privé, nulle part ailleurs.
1. Installe Tailscale sur le Mac (`brew install --cask tailscale`) et sur l'iPhone (App Store), même compte.
2. Dans `.env`, mets `ACCESS_TOKEN=` une longue chaîne aléatoire (`openssl rand -hex 24`), relance le bot.
3. Sur le Mac : `tailscale serve --bg 8765` → il affiche une URL `https://<ton-mac>.<tailnet>.ts.net`.
4. Sur l'iPhone, ouvre `https://<ton-mac>.<tailnet>.ts.net/?token=<ACCESS_TOKEN>` une fois
   (le token est posé en cookie), puis « Ajouter à l'écran d'accueil » dans Safari. L'UI est responsive.

## 5. Valider le format de réservation (première fois)
Le bot envoie `PUT …/events/<id>/profiles/<moi>/` avec `{"attendance_status": "present"}`
(format standard de l'API Sporteasy, non vérifié en conditions réelles). Deux façons de valider :
- Sur un créneau **déjà ouvert** où tu n'es pas inscrit : `curl -X POST localhost:8765/api/book-now/<event_id>`
  (ajoute `?token=…` si ACCESS_TOKEN est défini), puis vérifie dans l'app Sporteasy. Désinscris-toi en cliquant
  le créneau réservé dans l'UI.
- Ou laisse faire la première ouverture réelle et regarde le journal.
Si ça renvoie une erreur 400, ouvre Chrome → DevTools → Network, clique « Présent » sur un créneau, et
envoie-moi le corps du PUT : je corrige en 2 lignes dans `sporteasy.py` (`set_attendance`).
