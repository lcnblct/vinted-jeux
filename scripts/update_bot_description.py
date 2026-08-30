#!/usr/bin/env python3
"""Met à jour la description du bot Telegram depuis config.yaml — à lancer après chaque modif watchlist."""
import os, sys, yaml, requests
from pathlib import Path

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or open(".env").read().split("TELEGRAM_BOT_TOKEN=")[1].split()[0] if Path(".env").exists() else ""
if not TOKEN or ":" not in TOKEN:
    print("TELEGRAM_BOT_TOKEN manquant"); sys.exit(1)

cfg = yaml.safe_load(open("config.yaml"))
queries = cfg.get("queries", [])
# Construit description courte et longue
short = f"Alertes Vinted {len(queries)} jeux VF (-10€, FR, 7h30-22h)"

lines = []
for q in queries:
    lines.append(f"{q['name']} ≤{q['price_max']}€")

# Telegram n'affiche que la 1ère ligne en aperçu → on met la watchlist dès la 1ère ligne
# Short (120c max) = résumé, Desc (512c max) = liste complète sur 1-2 lignes max
body_one_line = ", ".join(lines)
# Version compacte pour être visible d'un coup
desc = f"{len(queries)} jeux VF (-10€, FR, 7h30-22h) : {body_one_line}"
if len(desc) > 512:
    desc = desc[:509] + "..."
# Short doit tenir en 120c, on y met aussi le début de la liste si possible
short = f"{len(queries)} jeux: " + ", ".join([f"{q['name'].split()[0]}≤{q['price_max']}€" for q in queries[:4]]) + "..."
if len(short) > 120:
    short = short[:117] + "..."

print(f"Short ({len(short)}): {short}")
print(f"Desc ({len(desc)}):\n{desc}\n")

# Appels API
for endpoint, payload in [
    ("setMyShortDescription", {"short_description": short}),
    ("setMyDescription", {"description": desc}),
]:
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/{endpoint}", data=payload, timeout=15)
    print(endpoint, r.json().get("ok"), r.json().get("result"), r.json().get("description","")[:120])

# Optionnel : setMyName déjà "Alertes jeux Vinted"
