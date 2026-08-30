# Vinted Jeux — Monitor Next Station Paris

Surveille automatiquement les nouvelles annonces Vinted pour **Next Station Paris** (et autres jeux de société) et t'alerte instantanément.

> Vinted ne propose PAS de notification native quand une nouvelle annonce correspond à ta recherche. Ce projet comble ce manque.

## 🎯 3 options (du plus simple au plus puissant)

### Option 0 — Sans code (2 minutes)

1. **Recherche sauvegardée Vinted** : Va sur https://www.vinted.fr/catalog?search_text=next%20station%20paris → clique sur le 🔖 marque-page. Tu verras un badge `+N` mais **pas de push**.
2. **Solution recommandée sans coder** :
   - **Vintify** (extension Chrome + app mobile) : surveille 24h/24 et push instantanée — gratuit pour 1 alerte → https://vintify.io
   - **Telvin Bot** (Telegram) : `/addurl` + colle l'URL Vinted → notif Telegram en 750ms → https://www.telvin-bot.com
   - **Vintrack** (Discord + Dashboard self-hosted) : ultra-rapide 1.5s, Docker → https://github.com/JakobAIOdev/Vintrack-Vinted-Monitor

→ Parfait si tu ne veux rien héberger.

### Option 1 — Ce projet (recommandé, gratuit & privé)

Script Python qui tourne sur ton Mac, poll Vinted toutes les 60s et t'envoie :
- Notification **Telegram** OU **Discord** OU **desktop macOS** + log console
- Base SQLite anti-doublons (`seen.db`)
- Tourne en `daemon` ou via `launchd` / `cron`

### Option 2 — Docker complet

Utilise `cchrkk/vinted-notifier` ou `Fuyucch1/Vinted-Notifications` (UI web + RSS + Telegram) si tu veux une interface graphique.

---

## 🖥️ UI Web (nouveau)

Dashboard live avec grille d'annonces, filtres prix/tri, scan en 1 clic.

```bash
cd /Volumes/SD/Projets/vinted-jeux
./start_ui.sh
# → http://localhost:8000
```

Fonctionnalités `web.py:32` :
- **Live Vinted** : fetch direct toutes les 60s (configurable) `templates/index.html:78`
- **Grille** : image, prix, titre, lien Vinted, tri prix/date
- **Scan** : bouton "Scanner maintenant" → `POST /api/scan` → notifie Telegram/Discord + marque vu
- **Config** : ⚙️ → édite URL, interval, prix max/min → `POST /api/config`
- **Tabs** : Live / Historique (seen.db) / Nouveautés
- **API** : `/api/items?live=true` `/api/status` `/api/seen`

Arrêt : `Ctrl+C` ou `kill $(cat /tmp/vinted-web.pid)`

## 🚀 Installation Option 1 (CLI, sans UI)

```bash
cd /Volumes/SD/Projets/vinted-jeux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# édite .env pour mettre ton TELEGRAM_BOT_TOKEN / DISCORD_WEBHOOK_URL
```

## ⚙️ Configuration

Édite `config.yaml` ou `.env` :

```yaml
query:
  url: "https://www.vinted.fr/catalog?search_text=next%20station%20paris&order=newest_first"
  name: "Next Station Paris"

poll_interval: 60  # secondes
price_max: 20      # filtre optionnel
database: "seen.db"
```

`.env` :
```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Trouve ton `CHAT_ID` : envoie un message à ton bot puis `curl https://api.telegram.org/bot<TOKEN>/getUpdates`

## ▶️ Lancer

```bash
# Test ponctuel (affiche les 5 dernières annonces)
python monitor.py --once --limit 5

# Surveillance continue
python monitor.py

# Avec debug
python monitor.py --verbose
```

Arrêt : `Ctrl+C`

## 🔔 Notifications supportées

| Canal | Config |
|-------|--------|
| Telegram | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| Discord | `DISCORD_WEBHOOK_URL` |
| macOS | automatique via `osascript` si aucun webhook configuré |
| Console | toujours actif |

## ⏰ Lancer au démarrage (macOS)

```bash
# Crée un service launchd qui relance toutes les 60s
./install_launchd.sh
launchctl load ~/Library/LaunchAgents/com.vinted.jeux.plist
```

Ou avec cron :
```bash
crontab -e
# ajoute:
*/2 * * * * /Volumes/SD/Projets/vinted-jeux/.venv/bin/python /Volumes/SD/Projets/vinted-jeux/monitor.py --once >> /tmp/vinted.log 2>&1
```

## 🔍 URL Vinted générée pour Next Station Paris

```
https://www.vinted.fr/catalog?search_text=next%20station%20paris&order=newest_first
```

Variantes utiles :
- Avec prix max 15€ : `&price_to=15&currency=EUR`
- Jeux & divertissement (catalog 2310) : `&catalog[]=2310`
- Très bon état uniquement : `&status_ids[]=2`

Astuce : fais ta recherche manuellement sur Vinted avec tous les filtres, puis copie-colle l'URL dans `config.yaml`.

## 🛠️ Dépannage

- **403 / Cloudflare** : le scraper gère les cookies automatiquement. Si bloqué, attends 5min ou ajoute un délai plus grand (`poll_interval: 120`)
- **Rien ne s'affiche** : teste `--once --limit 10` et vérifie l'URL dans un navigateur
- **Doublons** : supprimés via `seen.db` — efface le fichier pour repartir à zéro

## 📁 Structure

```
vinted-jeux/
├── monitor.py          # script principal
├── config.yaml         # requêtes + intervalles
├── requirements.txt
├── .env.example
├── seen.db             # (créé auto) SQLite anti-doublons
└── install_launchd.sh  # auto-start macOS
```

## ⚠️ Note légale

Utilise l'API publique non-officielle de Vinted avec parcimonie (60s min). Ne scrape pas massivement (risque 429 / ban IP). Respecte les CGU Vinted.
