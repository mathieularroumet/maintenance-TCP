# Plateforme de signalement maintenance — club de sport

Les adhérents scannent un QR code affiché dans chaque espace (court, salle, douches…), choisissent le type de problème et le décrivent. La date, l'heure et le lieu sont enregistrés automatiquement. L'équipe de maintenance traite les signalements dans un back office protégé par mot de passe.

## Fonctionnalités

**Côté adhérent (formulaire ouvert par le QR code)**
- Lieu reconnu automatiquement grâce au QR code
- 4 types : Nettoyage, Remplacement d'équipement, Défaut dangereux, Autre
- Description libre + contact facultatif
- Liste des problèmes déjà signalés sur cet espace (évite les doublons)
- Numéro de suivi après envoi ; protection anti-spam (champ piège + limite d'envois)

**Côté équipe (back office `/admin`)**
- Tableau de bord : nouveaux, défauts dangereux ouverts, corrections planifiées, résolus sur 30 jours
- Défauts dangereux remontés en tête de liste, badge « En retard » si la date planifiée est dépassée
- Actions : **Problème vu**, **Correction planifiée** (avec date), **Pas de solution**, **Problème résolu** (+ Rouvrir), chacune avec un commentaire facultatif
- Historique complet de chaque signalement
- Filtres (statut, type, espace, recherche) et export CSV (compatible Excel)
- Gestion des espaces (ajout, renommage, désactivation) et page d'impression des QR codes (A4, 4 par page) ou téléchargement PNG

## Lancer en local (test)

Prérequis : Python 3.9 ou plus. Aucune bibliothèque à installer.

```bash
ADMIN_PASSWORD=monmotdepasse CLUB_NAME="Tennis Club de Paris" python3 server.py
```

Puis ouvrir http://localhost:8000/admin. Six espaces d'exemple sont créés au premier lancement ; renommez-les ou supprimez-les depuis l'onglet **Espaces**.

## Configuration

| Variable | Rôle | Défaut |
|---|---|---|
| `ADMIN_PASSWORD` | Mot de passe du back office | généré et affiché au 1er lancement |
| `CLUB_NAME` | Nom affiché sur les pages et les QR codes | `Mon club de sport` |
| `BASE_URL` | **Adresse publique du site** (utilisée dans les QR codes) | déduite de la requête |
| `PORT` / `HOST` | Port et interface d'écoute | `8000` / `0.0.0.0` |
| `DATA_DIR` | Dossier de la base SQLite | `./data` |
| `TRUST_PROXY` | `1` si le site est derrière un proxy HTTPS (nginx, Caddy, hébergeur) | `0` |
| `TIMEZONE` | Fuseau horaire des dates | `Europe/Paris` |

⚠️ Définissez `BASE_URL` (ex. `https://maintenance.monclub.fr`) **avant d'imprimer les QR codes**. Les QR codes ne changent jamais ensuite, même si vous renommez un espace.

## Mettre en ligne

Le site doit être accessible depuis internet en HTTPS pour que les téléphones des adhérents puissent l'ouvrir.

**Option 1 — Docker (VPS, NAS Synology, etc.)**
```bash
docker build -t club-maintenance .
docker run -d --name maintenance -p 8000:8000 \
  -e ADMIN_PASSWORD='un-mot-de-passe-solide' \
  -e CLUB_NAME='Tennis Club de Paris' \
  -e BASE_URL='https://maintenance.monclub.fr' \
  -e TRUST_PROXY=1 \
  -v maintenance-data:/data \
  --restart unless-stopped club-maintenance
```
Placez devant un proxy HTTPS (Caddy est le plus simple : `maintenance.monclub.fr { reverse_proxy localhost:8000 }`).

**Option 2 — Hébergeur clé en main** (Render, Railway, Fly.io, Scalingo, Clever Cloud…) : déployez ce dossier avec le Dockerfile, renseignez les variables ci-dessus et **attachez un volume persistant monté sur `/data`** (sinon les signalements sont perdus à chaque redéploiement).

## Sauvegarde

Toutes les données sont dans `data/maintenance.db` (un seul fichier SQLite). Copiez-le régulièrement. L'export CSV du back office permet aussi un archivage simple.

## Notes

- La page des QR codes charge un petit générateur depuis cdnjs.cloudflare.com : il faut une connexion internet sur l'ordinateur qui imprime.
- Changer `ADMIN_PASSWORD` déconnecte toutes les sessions ouvertes.
