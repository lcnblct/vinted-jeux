# Vinted Jeux — Monitor Jeux de Société VF 🇫🇷

Surveillance automatique des annonces **Vinted.fr** pour les jeux de société en **version française**, à partir d'une **watchlist définie** (`config.yaml`). Alerte dès qu'une nouveauté passe **sous ton prix seuil**.

> Vinted n'envoie **pas** de push quand une nouvelle annonce correspond à ta recherche. Ce projet le fait pour toi, en respectant les limites API.

---

## 📋 Watchlist actuelle

Définie dans `config.yaml:4` — prix mini neuf trouvé → `price_max = mini -7€` → alerte seulement si `price <= price_max` + vendeur `FR` + catégorie `Jeux de société` `4881`. La liste est aussi poussée dans la **description du bot** `@alertes_jeux_vinted_bot` (`scripts/update_bot_description.py:1`, sync auto à chaque `push` sur `config.yaml`).

| # | Jeu | Prix mini neuf trouvé | Seuil alerte (-7€) | Mots-clés |
|---|-----|----------------------|---------------------|-----------|
| 1 | **Calico** | 26.90€ | **19.9€** | `calico` -sylvania |
| 2 | **Cascadia** | 29.96€ | **22.96€** | `cascadia` -Brooks |
| 3 | **Cascadia Rolling Hills** | 27.85€ | **20.85€** | `cascadia rolling hills` |
| 4 | **Cascadia Rolling Rivers** | 22.50€ | **15.5€** | `cascadia rolling rivers` |
| 5 | **Frosted Blooms** | 22.08€ | **15.08€** | `frosted blooms` |
| 6 | **Koï** | 39€ | **32€** | `koi` -bassin |
| 7 | **L'Île Des Chats** | 45€ | **38€** | `ile des chats` |
| 8 | **Next Station London** | 12.73€ | **5.73€** | `next station london` |
| 9 | **Next Station Paris** | 12.73€ | **5.73€** | `next station paris` |
| 10 | **Patchwork 10e Anniv** | 19.30€ | **12.3€** | `patchwork` -revues |
| 11 | **Rebirth** | 34.90€ | **27.9€** | `rebirth` |
| 12 | **Take It Easy!** | 22.50€ | **15.5€** | `take easy` -vêtements |
| 13 | **Windmill Valley** | 48.50€ | **41.5€** | `windmill valley` |

> Modifier la watchlist = éditer `config.yaml` (ajouter un bloc ` - name: ... url: ... price_max: ...`), commit + push → GitHub Actions recharge.

---

## ⚙️ Comment ça marche

`monitor.py:225` `fetch_items()` via `vinted_scraper` sur `https://www.vinted.fr/catalog?catalog_ids=4881` (catégorie **Jeux de société**) → `monitor.py:263` `apply_filters()` (prix + `must_contain`/`must_not_contain`) → `monitor.py:237` `filter_french_items()` ( `GET /api/v2/users/{id}` → garde `country_code==FR`) → `llm_filter.py:1` **Filtre vision LLM** `qwen/qwen3.7-flash` via OpenRouter (titre + description + 2 photos → détecte faux positifs : accessoire 3D, upgrade, insert, vêtement, jeu vidéo homonyme, mauvais variant Cascadia/Rolling) → `seen.db:147` anti-doublons → `notify_telegram` `monitor.py:33` / `notify_whatsapp` `monitor.py:66` / `ntfy` `monitor.py:88` / Discord.

Poll GitHub Actions `vinted-monitor.yml:5` `cron: "0 5-21 * * *"` (toutes les heures 7h-23h Paris, ~510min/mois) + `concurrency` + `timeout 5min`. LLM ~$0.00004/appel, 0-5/run, fail-open si pas de clé.

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
OPENROUTER_API_KEY=sk-or-v1-...  # optionnel, IA future (https://openrouter.ai/keys)
```

Obtenir `CHAT_ID` : `python scripts/setup_telegram.py` (guide @BotFather → `/newbot` → envoyer `hello` au bot).

---

## ➕ Ajouter un jeu à la watchlist

Dans `config.yaml:4` (toutes les recherches sont déjà restreintes à `catalog_ids=4881` = Jeux de société) :

```yaml
  - name: "Azul"
    url: "https://www.vinted.fr/catalog?search_text=azul&order=newest_first&catalog_ids=4881"
    price_max: 18          # alerte si <=18€ (prix boutique -7€)
    must_contain: ["azul"] # tous ces mots doivent être dans le titre
    must_not_contain: ["extension"] # optionnel
```

Astuce : sur Vinted, filtre par catégorie **Jeux de société**, copie l'URL complète (doit contenir `catalog_ids=4881`).

```bash
python monitor.py --once --limit 5 --verbose  # dry-run sans notif (LLM désactivé en dry-run)
python monitor.py --once --verbose            # 1 vrai scan + notif + marque vu (avec LLM Qwen 3.7 Flash)
python monitor.py --once --verbose --no-llm   # sans filtre LLM (debug)
python monitor.py --once --limit 1 --force-notify --verbose  # force 1 notif + LLM
python monitor.py                             # boucle locale 60s
# Test LLM seul
python llm_filter.py --game "Cascadia" --title "Lot de 25 Pommes..." --price "5 EUR" --image "https://..." --verbose
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
| **OpenRouter** | `OPENROUTER_API_KEY` | **LLM vision anti faux positifs** `qwen/qwen3.7-flash` (`llm_filter.py:1`) — analyse titre+description+photos, fail-open si pas de clé |

Watchlist rappel Telegram : `monitor.py:437` `get_watchlist_text()` → juste `• [Nom](MyLudo) ≤prix€` (1 ligne/jeu).

**Filtre LLM** `config.yaml:120` `settings.llm_filter.enabled=true` + `model: qwen/qwen3.7-flash` + `confidence_threshold: 0.6` + `max_images: 2`. Désactiver : `--no-llm` ou `enabled: false` ou vide `OPENROUTER_API_KEY`. Marque les faux positifs comme `seen` pour ne pas re-payer.

---

## ☁️ Déploiement permanent gratuit (hors Mac)

**GitHub Actions (recommandé, sans serveur)**
```bash
gh repo create vinted-jeux --private --source=. --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set OPENROUTER_API_KEY  # optionnel, IA future
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
- `429 LLM` → OpenRouter rate-limit (shared pool), retry 1.2s, sinon fail-open → laisse passer l'annonce
- `Aucune nouvelle annonce` → `--verbose` pour voir `exclu prix` / `[fr] exclu non-FR` / `[llm] ✂️ exclu faux positif` / `[llm] ✅ vrai jeu`
- Doublons → `seen.db` → `rm seen.db` pour reset
- Patchwork/Cascadia chaussures → ajuster `must_not_contain` dans `config.yaml` (LLM filtre déjà 90% des vêtements/accessoires)
- Coût LLM → ~$0.00004/appel, ~$0.003/jour (5/jour) ; désactiver avec `--no-llm`

---

## 📁 Structure

```
vinted-jeux/
├── config.yaml          # ← watchlist (catalog_ids=4881 + prix -10€ + FR) + settings.llm_filter
├── monitor.py           # fetch + filtres + FR + LLM vision + notifs
├── llm_filter.py        # ← Qwen 3.7 Flash via OpenRouter (titre+desc+photos → is_true_game)
├── scripts/setup_telegram.py
├── .github/workflows/vinted-monitor.yml # cron horaire + cache + commit seen.db
├── requirements.txt     # vinted_scraper, requests, pyyaml, python-dotenv
└── seen.db              # SQLite anti-doublons
```

## ⚠️ Note légale

API Vinted non-officielle, usage parcimonieux (20min). Respecte les CGU Vinted.
