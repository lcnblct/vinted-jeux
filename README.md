# Vinted Jeux — Monitor Jeux de Société VF 🇫🇷

Surveillance automatique des annonces **Vinted.fr** pour les jeux de société en **version française**, à partir d'une **watchlist définie** (`config.yaml`). Alerte dès qu'une nouveauté passe **sous ton prix seuil**.

> Vinted n'envoie **pas** de push quand une nouvelle annonce correspond à ta recherche. Ce projet le fait pour toi, en respectant les limites API.

---

## 📋 Watchlist actuelle

Définie dans `config.yaml:4` — prix mini neuf trouvé → `price_max = mini -10€` → alerte seulement si `price <= price_max` + vendeur `FR` + catégorie `Jeux de société` `4881`. La liste est aussi poussée dans la **description du bot** `@alertes_jeux_vinted_bot` (`scripts/update_bot_description.py:1`, sync auto à chaque `push` sur `config.yaml`).

| # | Jeu | Prix mini neuf trouvé | Seuil alerte (-10€) | Mots-clés |
|---|-----|----------------------|---------------------|-----------|
| 1 | **Windmill Valley** | 48.50€ | **38.5€** | `windmill valley` |
| 2 | **Take It Easy!** | 22.50€ | **12.5€** | `take easy` -vêtements |
| 3 | **Rebirth** | 34.90€ | **24.9€** | `rebirth` |
| 4 | **Patchwork 10e Anniv** | 19.30€ | **9.3€** | `patchwork` -revues |
| 5 | **Next Station Paris** | 12.73€ | **2.73€** | `next station paris` |
| 6 | **Next Station London** | 12.73€ | **2.73€** | `next station london` |
| 7 | **L'Île Des Chats** | 45€ | **35€** | `ile des chats` |
| 8 | **Koï** | 39€ | **29€** | `koi` -bassin |
| 9 | **Frosted Blooms** | 22.08€ | **12.08€** | `frosted blooms` |
| 10 | **Cortex Challenge** | 13.90€ | **3.9€** | `cortex` |
| 11 | **Cascadia Rolling Rivers** | 22.50€ | **12.5€** | `cascadia rolling rivers` |
| 12 | **Cascadia Rolling Hills** | 27.85€ | **17.85€** | `cascadia rolling hills` |
| 13 | **Cascadia** | 29.96€ | **19.96€** | `cascadia` -Brooks |
| 14 | **Calico** | 26.90€ | **16.9€** | `calico` -sylvania |

> Modifier la watchlist = éditer `config.yaml` (ajouter un bloc ` - name: ... url: ... price_max: ...`), commit + push → GitHub Actions recharge.

---

## ⚙️ Comment ça marche

`monitor.py:225` `fetch_items()` via `vinted_scraper` sur `https://www.vinted.fr/catalog?catalog_ids=4881` (catégorie **Jeux de société**) → `monitor.py:263` `apply_filters()` (prix + `must_contain`/`must_not_contain`) → `monitor.py:237` `filter_french_items()` ( `GET /api/v2/users/{id}` → garde `country_code==FR`) → `seen.db:147` anti-doublons → `notify_telegram` `monitor.py:33` / `notify_whatsapp` `monitor.py:66` / `ntfy` `monitor.py:88` / Discord.

Poll GitHub Actions `vinted-monitor.yml:5` `cron: "0 5-21 * * *"` (toutes les heures 7h-23h Paris, ~510min/mois) + `concurrency` + `timeout 5min`.

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
gh workflow run "Vinted Jeux — Watchlist FR (-10€)"
```
- Privé = 2000min/mois → `0 5-21` (horaire) ≈510min OK. Public = illimité.
- `seen.db` versionné `vinted-monitor.yml:49` (pull --rebase) → anti-doublons persistant.

**Local Mac (optionnel)**
```bash
./install_launchd.sh
launchctl load ~/Library/LaunchAgents/com.vinted.jeux.plist
```

## 🛠️ Dépannage

- `403 / 429` → normal, backoff `0.12s` + cache `_user_country_cache` `monitor.py:29` ; si bloqué attends 5min ou passe `poll_interval: 120`
- `Aucune nouvelle annonce` → `--verbose` pour voir `exclu prix` / `[fr] exclu non-FR`
- Doublons → `seen.db` → `rm seen.db` pour reset
- Patchwork/Cascadia chaussures → ajuster `must_not_contain` dans `config.yaml`

---

## 📁 Structure

```
vinted-jeux/
├── config.yaml          # ← watchlist (catalog_ids=4881 + prix -10€ + FR)
├── monitor.py           # fetch + filtres + notifs Telegram
├── scripts/setup_telegram.py
├── .github/workflows/vinted-monitor.yml # cron horaire + cache + commit seen.db
├── requirements.txt     # vinted_scraper, pyyaml...
└── seen.db              # SQLite anti-doublons
```

## ⚠️ Note légale

API Vinted non-officielle, usage parcimonieux (20min). Respecte les CGU Vinted.
