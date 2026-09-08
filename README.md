# Vinted Jeux — Monitor Jeux de Société VF 🇫🇷

Surveillance automatique des annonces **Vinted.fr** pour les jeux de société en **version française**, à partir d'une **watchlist définie** (`config.yaml`). Alerte dès qu'une nouveauté passe **sous ton prix seuil**.

> Vinted n'envoie **pas** de push quand une nouvelle annonce correspond à ta recherche. Ce projet le fait pour toi, en respectant les limites API.

---

## 📋 Watchlist actuelle

Définie dans `config.yaml` — seuils **manuels** par jeu (`price_max`) → alerte seulement si `price <= price_max` + vendeur `FR` + catégorie `Jeux de société` `4881`. La liste est aussi poussée dans la **description du bot** `@alertes_jeux_vinted_bot` (`scripts/update_bot_description.py`, sync auto à chaque `push` sur `config.yaml`).

| # | Jeu | Prix mini neuf trouvé | Seuil alerte | Mots-clés |
|---|-----|----------------------|---------------------|-----------|
| 1 | **Akropolis** | 24.80€ | **12€** | `akropolis` -extensions |
| 2 | **Aqua** | 27.92€ | **22€** | `aqua` -aqualin/-aquatica |
| 3 | **Cascadia** | 29.96€ | **22€** | `cascadia` -Brooks |
| 4 | **Cascadia Rolling Hills** | 27.85€ | **18€** | `cascadia rolling hills` |
| 5 | **Cascadia Rolling Rivers** | 22.50€ | **16€** | `cascadia rolling rivers` |
| 6 | **Frosted Blooms** | 22.08€ | **16€** | `frosted blooms` |
| 7 | **Koï** | 39€ | **27€** | `koi` -bassin |
| 8 | **L'Île Des Chats** | 45€ | **29€** | `ile des chats` |
| 9 | **Next Station London** | 12.73€ | **9€** | `next station london` |
| 10 | **Next Station Paris** | 12.73€ | **9€** | `next station paris` |
| 11 | **Patchwork 10e Anniv** | 19.30€ | **15€** | `patchwork` -doodle/-express/-folklore/-halloween/-winter/-automa |
| 12 | **Rebirth** | 34.90€ | **23€** | `rebirth` |
| 13 | **Take It Easy!** | 22.50€ | **16€** | `take easy` -vêtements |
| 14 | **Windmill Valley** | 48.50€ | **36€** | `windmill valley` |

> Modifier la watchlist = éditer `config.yaml` (ajouter un bloc ` - name: ... url: ... price_max: ...`), commit + push → GitHub Actions recharge.

---

## ⚙️ Comment ça marche

`fetch_items()` via `vinted_scraper` (1 recherche par jeu, catégorie **Jeux de société** `4881`, tri nouveautés) → `apply_filters()` (prix + `must_contain` minimal, insensible aux accents, exceptions `must_not_contain` uniquement pour les variantes stables connues) → anti-doublons `seen.db` + filtre fraîcheur `max_age_days: 3` (timestamp photo, proxy date création — l'API search n'a pas de champ date), AVANT les appels API → `filter_french_items()` (`GET /api/v2/users/{id}` → garde `country_code==FR`, exclusion si inconnu, cache SQLite `user_country` persistant ; inconnu retesté tant que frais) → **Filtre vision LLM** `qwen/qwen3.7-flash` via OpenRouter (`llm_filter.py` : titre + description + 2 photos + boîte de référence versionnée pour Patchwork 10e Anniversaire → détecte faux positifs : accessoire 3D, upgrade, insert, vêtement, jeu vidéo homonyme, mauvais variant Cascadia/Rolling, extensions) → `notify_telegram` / `notify_whatsapp` / `notify_ntfy` / `notify_discord` (`monitor.py`, prix affiché avec total frais acheteur inclus via `total_item_price` API, fallback calcul 0.70€ + 5%).

Déclenché par **cron-job.org** toutes les 15min 24/7 (`*/15 * * * *`, `workflow_dispatch`, voir `scripts/ping_workflow.py`) + à chaque `push` sur `config.yaml` — le `schedule` natif GitHub est désactivé (best-effort, sautait des runs). `concurrency` + budget de scan de 180s (reprise au prochain passage) + limite du job de 10min pour laisser finir les appels et sauvegarder. LLM ~$0.00004/appel, fail-open si pas de clé. Watchlist **1×/jour max** : envoyée seulement au **premier run du jour avec ≥1 vraie nouveauté** (`meta.last_watchlist_date` → `seen.db`, persistant), pas si aucun nouveau.

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
    price_max: 18          # seuil manuel en €
    must_contain: ["azul"] # 1-3 tokens distinctifs MINIMAUX, minuscules sans accents
    # must_not_contain: TOUJOURS vide — même pour extensions/variants/homonymes,
    # le LLM sait les reconnaître (ex. Athena/Panthéon, Aqualin, C'koi, Rolling)
```

1. **Prix** : seuil manuel fixé à la main (baisser si trop de bruit, monter si rien ne passe).
2. **`must_contain`** : 1 token distinctif suffit (`azul`, `koi`, `patchwork`) ; 2-3 si ambigu (`next station paris`, `cascadia rolling hills`). Écrire sans accents (le matching normalise de toute façon).
3. **`must_not_contain`** : vide par défaut. Exception pour un sous-titre stable qui désigne toujours un autre jeu et que le LLM a déjà confondu (ex. `explore`, `draw` pour L'Île des Chats, ou `doodle` pour Patchwork 10e Anniversaire). Ne pas y mettre de vocabulaire générique.
4. **Variants** : si le jeu est un variant d'un jeu existant (ex. Rolling), placer sa requête **AVANT** la requête générique pour un bon libellé d'alerte.
5. **Réf visuelle + fiche LLM** : ajouter la fiche exacte dans `MYLUDO_EXACT` (`monitor.py`), l'image boîte dans `MYLUDO_REF_IMAGES` (`llm_filter.py`, via MyLudo ou un fichier versionné dans `references/`) et une entrée `GAME_PROFILES` (cible exacte + liste à-rejeter : extensions, spin-offs, homonymes — le prompt est construit **par jeu**, pas généraliste).
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
| **Telegram** | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | `🎲 titre` + `💰 prix (≈X€ frais inclus)` + lien Vinted + fiche MyLudo + photo |
| **WhatsApp** | `WHATSAPP_PHONE` + `WHATSAPP_APIKEY` (CallMeBot) | texte court |
| **ntfy.sh** | `NTFY_TOPIC` | push `https://ntfy.sh/<topic>` |
| **Discord** | `DISCORD_WEBHOOK_URL` | embed |
| **macOS** | rien (fallback local) | `osascript` |
| **OpenRouter** | `OPENROUTER_API_KEY` | **LLM vision anti faux positifs** `qwen/qwen3.7-flash` (`llm_filter.py`) — analyse titre+description+photos, fail-open si pas de clé |

Watchlist rappel Telegram : `get_watchlist_text()` (`monitor.py`) → juste `• [Nom](MyLudo) ≤prix€` (1 ligne/jeu), **envoyée 1×/jour max** au premier run avec nouveauté (`seen.db`, table `meta`, clé `last_watchlist_date`, fuseau `Europe/Paris`).

**Filtre LLM** (`config.yaml`, `settings.llm_filter` : `enabled=true` + `model: qwen/qwen3.7-flash` + `confidence_threshold: 0.6` + `max_images: 2`). Désactiver : `--no-llm` ou `enabled: false` ou vide `OPENROUTER_API_KEY`. Mémorise les rejets dans `llm_rejections`, par annonce, recherche et version du filtre. `seen` est réservé aux livraisons terminées ; un rejet pour un variant ne bloque pas le jeu principal.

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
- Repo public = minutes Actions illimitées → `*/15 * * * *` 24/7 sans souci de quota.
- `seen.db` versionné par le workflow (fusion par union + pull --rebase + push avec réessais) → anti-doublons, file de livraison, rejets LLM et état de santé persistants. Une sauvegarde GitHub Actions permet une récupération si le push échoue.

## Fiabilité et contrôles

- **Livraison persistante** : chaque alerte et rappel quotidien passe par `delivery_outbox`, avec une ligne par destinataire. Un échec reste en attente et est retenté au scan suivant, même si l'annonce a disparu des résultats. Un destinataire déjà servi n'est pas renotifié. Les secrets restent dans l'environnement ; les destinataires Telegram sont représentés par leur empreinte. Un destinataire retiré de la configuration laisse sa livraison en attente jusqu'à intervention.
- **État visible** : une recherche échouée, un budget de temps dépassé ou une livraison encore en attente produit un code de sortie non nul. Le workflow sauvegarde l'état même après l'échec du scan. Le bilan affiche les recherches réussies/échouées et le nombre d'envois en attente.
- **Reprise** : `settings.scan_budget_seconds` vaut 180 par défaut. Le budget est vérifié entre les opérations ; un appel réseau déjà lancé peut finir après cette limite. La recherche interrompue est mémorisée dans `meta.scan_resume` et reprise au prochain passage. Les annonces déjà livrées ou rejetées pour cette recherche sont dédupliquées. La limite d'une page par recherche reste de 20 annonces par défaut : ce mécanisme ne garantit pas de retrouver une annonce sortie de cette page avant son traitement.
- **Connexion Vinted** : une session Vinted est réutilisée pour toute la watchlist afin d'éviter de redemander un cookie à chaque jeu. Une erreur réseau ou anti-bot transitoire est retentée jusqu'à trois fois, avec une courte pause ; une erreur persistante reste visible dans le bilan du scan.
- **Contrôle de santé** : après un scan complet sans échec de recherche ni livraison en attente, le bot enregistre `meta.last_successful_scan_at`. Le workflow `Vinted Jeux — Watchdog` le vérifie toutes les 30 minutes et échoue s'il date de plus de 45 minutes. Les notifications dépendent de tes réglages GitHub Actions ; ce contrôle n'envoie pas de Telegram et dépend lui aussi de GitHub. Une panne générale de GitHub nécessite un contrôle extérieur indépendant. Le premier contrôle peut échouer tant qu'aucun scan de la nouvelle version n'a terminé.
- **Données vérifiées** : prix EUR lisibles, finis et positifs ou nuls ; annonces explicitement vendues exclues ; YAML validé avant le scan. Un prix inconnu est écarté sans marquage définitif. Le modèle LLM du YAML est effectivement utilisé. Une réponse LLM mal formée reste incertaine et ne provoque pas de rejet définitif.
- **Tests automatiques** : versions des dépendances directes fixées, tests hors réseau sur les modifications de code et avant chaque scan en production.

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py --once --limit 5 --verbose  # aucune notification, SQLite en mémoire
python3 scripts/check_health.py --db seen.db --max-age-minutes 45
```

Limites : les anciens enregistrements de `seen` sont conservés, car ils ne permettent pas de distinguer avec certitude les anciennes alertes des anciens rejets. Un doublon reste possible si le service accepte un message mais que sa réponse réseau est perdue avant confirmation locale. Le journal texte est un audit secondaire ; SQLite conserve l'état des livraisons.

---

## 🛠️ Dépannage

- `403 / 429` → normal, backoff + cache `_user_country_cache` ; si bloqué attends 5min ou passe `poll_interval: 120`
- `429 LLM` → OpenRouter rate-limit (shared pool), retry 1.2s, sinon fail-open → laisse passer l'annonce
- `Aucune nouvelle annonce` → `--verbose` pour voir `exclu prix` / `[fr] exclu non-FR` / `[llm] ✂️ exclu faux positif` / `[llm] ✅ vrai jeu`
- Doublons → examiner `delivery_outbox` et l’historique avant toute action ; supprimer `seen.db` efface aussi les envois en attente et les décisions mémorisées
- Patchwork/Cascadia chaussures → ajuster `must_not_contain` dans `config.yaml` (LLM filtre déjà 90% des vêtements/accessoires)
- Coût LLM → ~$0.00004/appel, ~$0.003/jour (5/jour) ; désactiver avec `--no-llm`

---

## 📁 Structure

```
vinted-jeux/
├── config.yaml          # ← watchlist (catalog_ids=4881 + seuils manuels + FR) + settings.llm_filter
├── monitor.py           # fetch + filtres + FR + LLM vision + notifs
├── llm_filter.py        # ← Qwen 3.7 Flash via OpenRouter (titre+desc+photos → is_true_game)
├── references/           # ← boîtes de référence versionnées pour les comparaisons visuelles
├── scripts/setup_telegram.py       # helper obtention chat_id
├── scripts/update_bot_description.py # sync watchlist → description du bot
├── .github/workflows/vinted-monitor.yml # cron 30min + commit seen.db + historique
├── requirements.txt     # vinted_scraper, requests, pyyaml, python-dotenv
├── seen.db              # SQLite anti-doublons (versionné)
└── telegram_history.log # audit des envois (versionné)
```

## ⚠️ Note légale

API Vinted non-officielle, usage parcimonieux (1 scan / 30min). Respecte les CGU Vinted.
