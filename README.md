# Vinted Jeux — Monitor Jeux de Société VF 🇫🇷

Surveillance automatique des annonces **Vinted.fr** pour les jeux de société en **version française**, à partir d'une **watchlist définie** (`config.yaml`). Alerte dès qu'une nouveauté passe **sous ton prix seuil**.

> Vinted n'envoie **pas** de push quand une nouvelle annonce correspond à ta recherche. Ce projet le fait pour toi, en respectant les limites API.

---

## 📋 Watchlist actuelle

Définie dans `config.yaml` — prix mini neuf trouvé → `price_max = mini -7€` arrondi à l'euro supérieur → alerte seulement si `price <= price_max` + vendeur `FR` + catégorie `Jeux de société` `4881`. La liste est aussi poussée dans la **description du bot** `@alertes_jeux_vinted_bot` (`scripts/update_bot_description.py`, sync auto à chaque `push` sur `config.yaml`).

| # | Jeu | Prix mini neuf trouvé | Seuil alerte (-7€) | Mots-clés |
|---|-----|----------------------|---------------------|-----------|
| 1 | **Akropolis** | 24.80€ | **18€** | `akropolis` -extensions |
| 2 | **Aqua** | 27.92€ | **21€** | `aqua` -aqualin/-aquatica |
| 3 | **Calico** | 26.90€ | **20€** | `calico` -sylvania |
| 4 | **Cascadia** | 29.96€ | **23€** | `cascadia` -Brooks |
| 5 | **Cascadia Rolling Hills** | 27.85€ | **21€** | `cascadia rolling hills` |
| 6 | **Cascadia Rolling Rivers** | 22.50€ | **16€** | `cascadia rolling rivers` |
| 7 | **Frosted Blooms** | 22.08€ | **16€** | `frosted blooms` |
| 8 | **Koï** | 39€ | **32€** | `koi` -bassin |
| 9 | **L'Île Des Chats** | 45€ | **38€** | `ile des chats` |
| 10 | **Next Station London** | 12.73€ | **6€** | `next station london` |
| 11 | **Next Station Paris** | 12.73€ | **6€** | `next station paris` |
| 12 | **Patchwork 10e Anniv** | 19.30€ | **13€** | `patchwork` -revues |
| 13 | **Rebirth** | 34.90€ | **28€** | `rebirth` |
| 14 | **Take It Easy!** | 22.50€ | **16€** | `take easy` -vêtements |
| 15 | **Windmill Valley** | 48.50€ | **42€** | `windmill valley` |

> Modifier la watchlist = éditer `config.yaml` (ajouter un bloc ` - name: ... url: ... price_max: ...`), commit + push → GitHub Actions recharge.

---

## ⚙️ Comment ça marche

`fetch_items()` via `vinted_scraper` (1 recherche par jeu, catégorie **Jeux de société** `4881`, tri nouveautés) → `apply_filters()` (prix + `must_contain` minimal, insensible aux accents, `must_not_contain` toujours vide — politique anti faux négatifs, la précision est le job du LLM) → anti-doublons `seen.db` + filtre fraîcheur `max_age_days: 3` (timestamp photo, proxy date création — l'API search n'a pas de champ date), AVANT les appels API → `filter_french_items()` (`GET /api/v2/users/{id}` → garde `country_code==FR`, exclusion si inconnu, cache SQLite `user_country` persistant ; inconnu retesté tant que frais) → **Filtre vision LLM** `qwen/qwen3.7-flash` via OpenRouter (`llm_filter.py` : titre + description + 2 photos + boîte réf MyLudo → détecte faux positifs : accessoire 3D, upgrade, insert, vêtement, jeu vidéo homonyme, mauvais variant Cascadia/Rolling, extensions) → `notify_telegram` / `notify_whatsapp` / `notify_ntfy` / `notify_discord` (`monitor.py`).

Déclenché par **cron-job.org** toutes les 30min (`workflow_dispatch`, voir `scripts/ping_workflow.py`) + à chaque `push` sur `config.yaml` — le `schedule` natif GitHub est désactivé (best-effort, sautait des runs). `concurrency` + `timeout 5min`. LLM ~$0.00004/appel, fail-open si pas de clé. Watchlist **1×/jour max** : envoyée seulement au **premier run du jour avec ≥1 vraie nouveauté** (`meta.last_watchlist_date` → `seen.db`, persistant), pas si aucun nouveau.

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
OPENROUTER_API_KEY=sk-or-v1-...  # optionnel, filtre LLM vision (https://openrouter.ai/keys)
```

Obtenir `CHAT_ID` : `python scripts/setup_telegram.py` (guide @BotFather → `/newbot` → envoyer `hello` au bot).

---

## ➕ Ajouter un jeu à la watchlist (procédure anti faux négatifs)

Principe : **recall maximal, précision = job du LLM**. On ne doit jamais exclure à tort ici ; les faux positifs sont éliminés par Qwen (vision + réf MyLudo).

Dans `config.yaml` (recherches restreintes à `catalog_ids=4881` = Jeux de société) :

```yaml
  - name: "Azul"
    url: "https://www.vinted.fr/catalog?search_text=azul&order=newest_first&catalog_ids=4881"
    price_max: 18          # ceil(prix mini neuf - 7€) → seuil rond
    must_contain: ["azul"] # 1-3 tokens distinctifs MINIMAUX, minuscules sans accents
    # must_not_contain: TOUJOURS vide — même pour extensions/variants/homonymes,
    # le LLM sait les reconnaître (ex. Athena/Panthéon, Aqualin, C'koi, Rolling)
```

1. **Prix** : mini neuf trouvé − 7€, arrondi à l'euro **supérieur**.
2. **`must_contain`** : 1 token distinctif suffit (`azul`, `koi`, `patchwork`) ; 2-3 si ambigu (`next station paris`, `cascadia rolling hills`). Écrire sans accents (le matching normalise de toute façon).
3. **`must_not_contain`** : vide par défaut. Seule exception : spin-off au **sous-titre stable** qui n'apparaît jamais sur la boîte du jeu de base et que le LLM confond (ex. `explore`, `draw` pour L'Île des Chats — cf. incident 04/09/2026). Jamais de vocabulaire de jeu générique (extension, variant, homonyme) : c'est le job du LLM.
4. **Variants** : si le jeu est un variant d'un jeu existant (ex. Rolling), placer sa requête **AVANT** la requête générique pour un bon libellé d'alerte.
5. **Réf MyLudo** : ajouter la fiche exacte dans `MYLUDO_EXACT` (`monitor.py`) + l'image boîte dans `MYLUDO_REF_IMAGES` (`llm_filter.py`, via `https://www.myludo.fr/?_escaped_fragment_=/game/<slug>` → `og:image`) + le cas d'homonymie dans le prompt `_build_prompt` si nouveau.
6. **README** : ajouter la ligne au tableau watchlist.
7. Commit + push → run auto (trigger `push` sur `config.yaml`), vérifier l'onglet Actions.

Limites connues (non bloquantes) : fenêtre fraîcheur 3j (panne >3j = annonces ratées), bump sans nouvelles photos invisible, vendeur FR illisible côté API exclu puis retesté tant que frais, `per_page: 20` (volume >20 nouveautés/30min improbable sur ces niches).

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
| **Telegram** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | `🎲 titre` + `💰 prix` + lien Vinted + fiche MyLudo + photo |
| **WhatsApp** | `WHATSAPP_PHONE` + `WHATSAPP_APIKEY` (CallMeBot) | texte court |
| **ntfy.sh** | `NTFY_TOPIC` | push `https://ntfy.sh/<topic>` |
| **Discord** | `DISCORD_WEBHOOK_URL` | embed |
| **macOS** | rien (fallback local) | `osascript` |
| **OpenRouter** | `OPENROUTER_API_KEY` | **LLM vision anti faux positifs** `qwen/qwen3.7-flash` (`llm_filter.py`) — analyse titre+description+photos, fail-open si pas de clé |

Watchlist rappel Telegram : `get_watchlist_text()` (`monitor.py`) → juste `• [Nom](MyLudo) ≤prix€` (1 ligne/jeu), **envoyée 1×/jour max** au premier run avec nouveauté (`seen.db`, table `meta`, clé `last_watchlist_date`, fuseau `Europe/Paris`).

**Filtre LLM** (`config.yaml`, `settings.llm_filter` : `enabled=true` + `model: qwen/qwen3.7-flash` + `confidence_threshold: 0.6` + `max_images: 2`). Désactiver : `--no-llm` ou `enabled: false` ou vide `OPENROUTER_API_KEY`. Marque les faux positifs comme `seen` pour ne pas re-payer.

---

## ☁️ Déploiement

**GitHub Actions (recommandé, sans serveur)**
```bash
gh repo create vinted-jeux --private --source=. --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
gh secret set OPENROUTER_API_KEY  # optionnel, filtre LLM vision
gh workflow run "Vinted Jeux — Watchlist FR"
```
- Privé = 2000min/mois → toutes les 30min ~7h07-22h37 ≈960min OK. Public = illimité.
- `seen.db` versionné par le workflow (pull --rebase) → anti-doublons + `meta.last_watchlist_date` persistant.

## 🛠️ Dépannage

- `403 / 429` → normal, backoff + cache `_user_country_cache` ; si bloqué attends 5min ou passe `poll_interval: 120`
- `429 LLM` → OpenRouter rate-limit (shared pool), retry 1.2s, sinon fail-open → laisse passer l'annonce
- `Aucune nouvelle annonce` → `--verbose` pour voir `exclu prix` / `[fr] exclu non-FR` / `[llm] ✂️ exclu faux positif` / `[llm] ✅ vrai jeu`
- Doublons → `seen.db` → `rm seen.db` pour reset
- Patchwork/Cascadia chaussures → ajuster `must_not_contain` dans `config.yaml` (LLM filtre déjà 90% des vêtements/accessoires)
- Coût LLM → ~$0.00004/appel, ~$0.003/jour (5/jour) ; désactiver avec `--no-llm`

---

## 📁 Structure

```
vinted-jeux/
├── config.yaml          # ← watchlist (catalog_ids=4881 + prix -7€ + FR) + settings.llm_filter
├── monitor.py           # fetch + filtres + FR + LLM vision + notifs
├── llm_filter.py        # ← Qwen 3.7 Flash via OpenRouter (titre+desc+photos → is_true_game)
├── scripts/setup_telegram.py       # helper obtention chat_id
├── scripts/update_bot_description.py # sync watchlist → description du bot
├── .github/workflows/vinted-monitor.yml # cron 30min + commit seen.db + historique
├── requirements.txt     # vinted_scraper, requests, pyyaml, python-dotenv
├── seen.db              # SQLite anti-doublons (versionné)
└── telegram_history.log # audit des envois (versionné)
```

## ⚠️ Note légale

API Vinted non-officielle, usage parcimonieux (1 scan / 30min). Respecte les CGU Vinted.
