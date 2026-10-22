# Déployer sur Railway (tourne 24/7 sans le Mac)

~5 $/mois. Tes identifiants Sporteasy vont dans les variables Railway, nulle part ailleurs.

## 1. CLI (une fois)
```bash
brew install railway        # si pas déjà installé
railway login               # ouvre le navigateur
```

## 2. Créer le projet et pousser le code
```bash
cd ~/Developer/sporteasy-bot
railway init                # nom : sporteasy-bot
railway up                  # build de l'image (~3 min la première fois)
```

## 3. Variables (dashboard Railway → ton service → Variables)
Obligatoires :
```
SPORTEASY_EMAIL      = ton email
SPORTEASY_PASSWORD   = ton mot de passe
ACCESS_TOKEN         = résultat de :  openssl rand -hex 24
```
Optionnelles (mêmes valeurs que .env.example) : `MAX_PER_WEEK`, `NOTIFY_WEBHOOK`, `TICK_SECONDS`…
Sur Railway il n'y a pas de notif macOS : mets un `NOTIFY_WEBHOOK` (Slack incoming webhook, ou Discord
webhook avec `/slack` à la fin de l'URL) pour recevoir "✅ Réservé" sur ton téléphone.

## 4. Volume (pour ne pas perdre tes options à chaque redéploiement)
Dashboard → service → Settings → Volumes → **Add volume**, mount path : `/data`.

## 5. URL publique
Dashboard → service → Settings → Networking → **Generate domain** (port 8765).
Sur l'iPhone, ouvre une fois `https://<ton-domaine>.up.railway.app/?token=<ACCESS_TOKEN>`
puis Safari → Partager → **Sur l'écran d'accueil**. Le token est gardé en cookie un an.

## Mises à jour
```bash
cd ~/Developer/sporteasy-bot && railway up
```

## Vérifier
Dashboard → Deployments → Logs : tu dois voir `login OK` puis `watcher démarré`.
