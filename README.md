# Vinted Jeux — Monitor Jeux de Société VF 🇫🇷

Surveillance automatique des annonces **Vinted.fr** pour les jeux de société en **version française**, à partir d'une **watchlist définie** (`config.yaml`). Alerte dès qu'une nouveauté passe **sous ton prix seuil**.

> Vinted n'envoie **pas** de push quand une nouvelle annonce correspond à ta recherche. Ce projet le fait pour toi, en respectant les limites API.

---

## 📋 Watchlist actuelle

Définie dans `config.yaml:4` — prix boutique → `price_max = prix -10€` → alerte seulement si `price <= price_max` + vendeur `FR`.

| # | Jeu | Prix boutique | Seuil alerte | Mots-clés |
|---|-----|---------------|--------------|-----------|
| 1 | **Windmill Valley** | 53€ | **43€** | `windmill valley` |
| 2 | **Take It Easy!** | 24.95€ | **14.95€** | `take easy` -vêtements |
| 3 | **Rebirth** | 40€ | **30€** | `rebirth` |
| 4 | **Patchwork Édition 10e Anniversaire** | 21€ | **11€** | `patchwork` -revues |
| 5 | **Next Station Paris** | 14.90€ | **4.90€** | `next station paris` |
| 6 | **Next Station London** | 14.90€ | **4.90€** | `next station london` |
| 7 | **L'Île Des Chats** | 49€ | **39€** | `ile des chats` |
| 8 | **Koï** | 39.90€ | **29.90€** | `koi` -bassin |
| 9 | **Frosted Blooms** | 28€ | **18€** | `frosted blooms` |
| 10 | **Cortex Challenge** | 15€ | **5€** | `cortex` |
| 11 | **Cascadia - Rolling Rivers** | 32.50€ | **22.50€** | `cascadia rolling rivers` |
| 12 | **Cascadia - Rolling Hills** | 32.50€ | **22.50€** | `cascadia rolling hills` |
| 13 | **Cascadia** | 35€ | **25€** | `cascadia` -rolling -Brooks |
| 14 | **Calico** | 29.99€ | **19.99€** | `calico` -sylvania |

> Modifier la watchlist = éditer `config.yaml` (ajouter un bloc ` - name: ... url: ... price_max: ...`), commit + push → GitHub Actions recharge.

---

## ⚙️ Comment ça marche

`monitor.py:225` `fetch_items()` via `vinted_scraper` sur `https://www.vinted.fr/catalog?catalog_ids=4881` (catégorie **Jeux de société**) → `monitor.py:263` `apply_filters()` (prix + `must_contain`/`must_not_contain`) → `monitor.py:237` `filter_french_items()` ( `GET /api/v2/users/{id}` → garde `country_code==FR`) → `seen.db:147` anti-doublons → `notify_telegram` `monitor.py:33` / `notify_whatsapp` `monitor.py:66` / `ntfy` `monitor.py:88` / Discord.

Poll GitHub Actions `vinted-monitor.yml:5` `cron: "*/15 * * * *"` + `concurrency` + `timeout 5min` (~1min/run → voir free tier).

---

## 🚀 Installation

```bash
cd vinted-jeux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # y mettre ton token Telegram
```

`.env` :
```
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
# alternative: WHATSAPP_PHONE=+336... WHATSAPP_APIKEY=... / NTFY_TOPIC=... / DISCORD_WEBHOOK_URL=...
```

Obtenir `CHAT_ID` : `python scripts/setup_telegram.py` (guide @BotFather → `/newbot` → envoyer `hello` au bot).

---

## ➕ Ajouter un jeu à la watchlist

Dans `config.yaml:4` (toutes les recherches sont déjà restreintes à `catalog_ids=4881` = Jeux de société) :

```yaml
  - name: "Azul"
    url: "https://www.vinted.fr/catalog?search_text=azul&order=newest_first&catalog_ids=4881"
    price_max: 18          # alerte si <=18€ (prix boutique -10€)
    must_contain: ["azul"] # tous ces mots doivent être dans le titre
    must_not_contain: ["extension"] # optionnel
```

Astuce : sur Vinted, filtre par catégorie **Jeux de société**, copie l'URL complète (doit contenir `catalog_ids=4881`).

```bash
python monitor.py --once --limit 5 --verbose  # dry-run sans notif
python monitor.py --once --verbose            # 1 vrai scan + notif + marque vu
python monitor.py                             # boucle locale 60s
```

---

## 🔔 Notifications

| Canal | Config | Message |
|-------|--------|---------|
| **Telegram** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | `🎲 *title* 💰 10€ 🔗 url` + photo |
| **WhatsApp** | `WHATSAPP_PHONE` + `WHATSAPP_APIKEY` (CallMeBot) | texte court |
| **ntfy.sh** | `NTFY_TOPIC` | push `https://ntfy.sh/<topic>` |
| **Discord** | `DISCORD_WEBHOOK_URL` | embed |
| **macOS** | rien (fallback local) | `osascript` |

---

## ☁️ Déploiement permanent gratuit (hors Mac)

**GitHub Actions (recommandé, sans serveur)**
```bash
gh repo create vinted-jeux --private --source=. --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh workflow run "Vinted Monitor — Next Station Paris"
```
- Privé = 2000min/mois → `*/20` ≈1400min OK, `*/30` ≈960min. Public = illimité.
- `seen.db` versionné `vinted-monitor.yml:49` (pull --rebase) → anti-doublons persistant.

**Local Mac**
```bash
./install_launchd.sh          # monitor 60s
./install_launchd.sh web      # UI http://localhost:8000 `web.py:1`
```

**Docker / Render / Koyeb**
```bash
docker compose up --build # → http://localhost:8000
# Render: import repo → Docker → Free → healthCheck /api/status `render.yaml:1`
```

---

## 🖥️ UI Web (optionnelle)

```bash
./start_ui.sh # → http://localhost:8000
```
Grille live `templates/index.html:78`, tri prix/date, `POST /api/scan`, `POST /api/config`, tabs Live/Historique.

---

## 🛠️ Dépannage

- `403 / 429` → normal, backoff `0.12s` + cache `_user_country_cache` `monitor.py:29` ; si bloqué attends 5min ou passe `poll_interval: 120`
- `Aucune nouvelle annonce` → `--verbose` pour voir `exclu prix` / `[fr] exclu non-FR`
- Doublons → `seen.db` → `rm seen.db` pour reset
- Patchwork/Cascadia chaussures → ajuster `must_not_contain` dans `config.yaml`

---

## 📁 Structure

```
vinted-jeux/
├── config.yaml          # ← ta watchlist
├── monitor.py           # fetch + filtres + notifs
├── web.py               # UI FastAPI
├── scripts/setup_telegram.py
├── .github/workflows/vinted-monitor.yml # cron 20min + cache + commit seen.db
├── requirements.txt     # vinted_scraper, fastapi, pyyaml...
├── seen.db              # SQLite auto
└── templates/index.html # dashboard
```

## ⚠️ Note légale

API Vinted non-officielle, usage parcimonieux (20min). Respecte les CGU Vinted.
