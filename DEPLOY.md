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

## 3. Variables (dashboard Railway → ton service → Variables → Raw Editor)
Un bloc par compte ; chaque personne a son URL `/u/<NAME>/?token=<TOKEN>` et ses propres options :
```
USER1_NAME=ao
USER1_EMAIL=…
USER1_PASSWORD=…
USER1_TOKEN=…        # openssl rand -hex 24, un différent par personne
USER2_NAME=pote1
USER2_EMAIL=…
USER2_PASSWORD=…
USER2_TOKEN=…
SPORTEASY_TEAM_ID=2050454
SPORTEASY_TEAM_HOST=sitebad.sporteasy.net
TZ=Europe/Paris
```
Ajouter un compte = ajouter un bloc USER3_* et redéployer. Chacun règle ses délais dans son UI.
Lien perso à envoyer à chacun : `https://<domaine>/i/<USERn_INVITE>` (sans `USERn_INVITE`, ce sont les
8 premiers caractères du token). Le lien pose le cookie et redirige vers sa page ; le token n'est jamais
visible dans l'URL. **Ce lien est la clé du compte** : choisis un code impossible à deviner
(`prenom-` + 6 caractères au hasard, ex. `openssl rand -hex 3`), pas `prenom-quelquechose`.
Changer un code ne casse pas l'app déjà installée sur un téléphone (elle reste reconnue par son cookie).
Optionnelles (mêmes valeurs que .env.example) : `MAX_PER_WEEK`, `NOTIFY_WEBHOOK`, `TICK_SECONDS`…
**Notifications sur le téléphone (ntfy, gratuit)** : chacun installe l'app ntfy (App Store / Play Store),
s'abonne à un topic imprévisible (ex. `ezbad-prenom-xxxxxx`, avec de vrais caractères au hasard : quiconque
connaît le nom peut lire les notifs), puis le saisit dans l'app (ⓘ → Mon compte) ou tu mets `USERn_NTFY=…` dans les
variables. Bouton "Tester la notif" dans l'UI. Le bot envoie ✅ réservé / ⏳ liste d'attente / ✅ place libérée /
🗑 créneau annulé / ❌ erreur / ↩ libéré — une seule notif par événement.
**Par email (Resend, sans app)** : crée une clé sur resend.com, mets `RESEND_API_KEY=re_…` dans les variables.
Chacun reçoit sur son adresse Sporteasy (`USERn_EMAIL`), ou sur `USERn_EMAIL_NOTIF` si défini.
Sans domaine vérifié chez Resend, seul l'email du compte Resend peut recevoir ; pour envoyer aux potes,
vérifie un domaine (DNS) et mets `RESEND_FROM="EZ bad <bot@tondomaine.fr>"`.
(Alternative : `NOTIFY_WEBHOOK` Slack/Discord, commun à tous les comptes.)


## Auto-inscription (recommandé)
Plus besoin de mettre les identifiants des potes dans Railway :
1. **Une fois** : ajoute `SECRET_KEY=<résultat de openssl rand -hex 32>` dans les variables Railway.
   C'est la clé qui chiffre les mots de passe Sporteasy stockés sur le volume. **Ne la change jamais**
   (sinon les comptes inscrits devront se reconnecter). Sans elle, une clé est générée sur le volume (moins sûr).
2. Dans l'app (compte admin, `ADMIN_USERS=ao` par défaut) : bouton **＋** → lien d'invitation à usage unique
   (7 jours, `INVITE_DAYS`). Le pote l'ouvre, entre son email/mot de passe Sporteasy, le bot teste la connexion
   en direct et crée son compte. N'importe quel club Sporteasy (il colle l'adresse de son club).
3. Chacun gère son compte dans ⓘ → Mon compte : canal ntfy, nouveau mot de passe, suppression.
Les comptes `USERn_*` des variables continuent de fonctionner tels quels.

## 4. Volume (pour ne pas perdre tes options à chaque redéploiement)
Dashboard → service → Settings → Volumes → **Add volume**, mount path : `/data`.

## 5. URL publique
Dashboard → service → Settings → Networking → **Generate domain** (port 8765).
Sur l'iPhone, ouvre une fois `https://<ton-domaine>.up.railway.app/u/ao/?token=<USER1_TOKEN>`
(ton pote : `/u/pote1/?token=<USER2_TOKEN>`)
puis Safari → Partager → **Sur l'écran d'accueil**. Le token est gardé en cookie un an.

## Mises à jour
```bash
cd ~/Developer/sporteasy-bot && git stash && git pull --rebase && railway up --detach
```
Le bot est coupé 1 à 2 minutes pendant le redémarrage : évite de déployer autour d'une heure d'ouverture
(19h30–20h15 en général).

## Vérifier
`https://<ton-domaine>/healthz` répond `{"ok":true,"version":"…"}` : c'est la version en ligne (elle est aussi
affichée en bas de l'app). Dashboard → Deployments → Logs : tu dois voir `EZ bad <version>` puis `watcher démarré`.

## Tests (sans compte Sporteasy)
```bash
pip install httpx                      # une fois
python tests/test_engine.py            # moteur + API avec un Sporteasy simulé
python tests/demo.py                   # l'app en local sur des données fictives
```
