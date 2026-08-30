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

# Description longue <512 car (Telegram limite)
header = f"Surveillance Vinted.fr — Jeux de société VF ({len(queries)} jeux, catégorie 4881, vendeur FR)\nSeuil = prix mini neuf -10€, alerte horaire 7h30-22h Paris:\n\n"
body = ", ".join(lines)
desc = header + body
if len(desc) > 512:
    # coupe si besoin
    desc = desc[:509] + "..."

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
