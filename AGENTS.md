# AGENTS.md — règles permanentes vinted-jeux

## Ordre permanent : toujours push en prod
- Après chaque modif (seuils, watchlist, code, docs) : commit + `git pull --rebase` + `git push` vers `main`, **sans demander**.
- Le push sur `config.yaml` déclenche le workflow GitHub (scan + sync description bot) — c'est voulu.
- Ne jamais laisser de changements locaux non pushés.

## Modif watchlist / seuils
1. `config.yaml` (`price_max`) + tableau watchlist du `README.md` (même valeur).
2. Sync description bot : `python3 scripts/update_bot_description.py` (doit répondre `True True`).
3. Commit + pull --rebase + push (voir ci-dessus).

## Conflit `seen.db` / `telegram_history.log` au push (CI écrit en continu)
- Ne jamais écraser : fusionner par union.
- `seen.db` : `INSERT OR IGNORE` des lignes manquantes (tables `seen`, `user_country` ; `meta` = garder la date max) via sqlite3.
- `telegram_history.log` : union des lignes (garder le header `#`), triées.
- Puis commit + push.

## Ne pas commiter
- `.env` (secrets), `__pycache__/`, `.DS_Store`, fichiers `??` non demandés (ex. brouillons dans `scripts/`).

## Vérifications
- Dry-run sans notif : `python monitor.py --once --limit 5 --verbose`
- Vrai scan + notif : `python monitor.py --once --verbose`
