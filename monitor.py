#!/usr/bin/env python3
"""
Vinted Jeux Monitor — Next Station Paris
Poll Vinted toutes les N secondes et notifie Telegram / Discord / macOS
Usage:
  python monitor.py              # boucle infinie
  python monitor.py --once       # un seul check + notifie les nouveautés
  python monitor.py --once --limit 5 --verbose  # debug sans notifier, affiche 5 annonces
"""
import argparse
import os
import re
import sqlite3
import sys
import time
import json
import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, quote
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None  # py <3.9 fallback -> UTC

import yaml
import requests
from dotenv import load_dotenv

# Charge .env si présent
load_dotenv()

# LLM vision filter (Qwen 3.7 Flash via OpenRouter) — optionnel, fail-open si pas de clé
try:
    import llm_filter  # type: ignore
except ImportError:
    llm_filter = None

CONFIG_PATH = Path(__file__).parent / "config.yaml"
DB_DEFAULT = Path(__file__).parent / "seen.db"
SENT_HISTORY = Path(__file__).parent / "telegram_history.log"

# Cache pays vendeur pour filtre FR
_user_country_cache: dict = {}
_french_scraper = None  # lazy init
_scraper_cache: dict = {}  # one Vinted session per host and process

# Creating a VintedScraper fetches a session cookie.  Doing that once for every
# watchlist entry can trigger a transient 406/429 from Vinted.  Reusing the
# session avoids the burst, while the bounded retry handles an expired session.
# Historique 08/09/2026 : 2 runs rouges (12/14 en 500 + ReadTimeout pendant
# ~10 min, outage Vinted côté serveur, pas un rate-limit de notre pacing).
# Le retry court (1s/2s) + nouvelle session à chaque tentative a amplifié le
# burst (45 cookies/run). On passe en backoff exponentiel, on réutilise la
# même session pour les premiers essais et on espace les recherches.
FETCH_RETRIES = 5
FETCH_BACKOFF_BASE = 2.0  # délai = base * 2**attempt, plafonné à 20s
FETCH_BACKOFF_MAX = 20.0
FETCH_PACING_SECONDS = 1.5  # pause entre 2 recherches pour éviter le burst 429

def append_history(entry: str, verbose: bool = False):
    """Persiste l'historique de ce qui a été réellement envoyé sur Telegram (audit + debug doublons).
    Format ligne: ISO Paris | TYPE | id | title | price | url
    Stocké dans telegram_history.log versionné dans le repo."""
    try:
        ts = datetime.now(ZoneInfo("Europe/Paris")).isoformat() if ZoneInfo else datetime.now().isoformat()
    except Exception:
        ts = datetime.now().isoformat()
    line = f"{ts} | {entry}"
    try:
        # Crée le header si fichier inexistant (premier run)
        if not SENT_HISTORY.exists():
            SENT_HISTORY.write_text("# telegram_history.log — audit des envois réels (Paris ISO | TYPE | id | title | price | url | jeu)\n", encoding="utf-8")
        with open(SENT_HISTORY, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if verbose:
            print(f"[history] {line[:200]}")
    except Exception as e:
        if verbose:
            print(f"[history] err {e}")

# MyLudo : liens cliquables pour vérifier le jeu — fallback search si fiche exacte non trouvée
def get_myludo_url(game_name: str) -> str:
    # MyLudo n'a pas d'API publique stable, on utilise la recherche qui est cliquable et redirige vers la fiche
    # Ex: https://www.myludo.fr/#!/search?q=Windmill%20Valley
    return f"https://www.myludo.fr/#!/search?q={quote(game_name)}"

# Fiches directes MyLudo — trouvées via sitemap (direct, pas recherche)
MYLUDO_EXACT = {
    "Akropolis": "https://www.myludo.fr/#!/game/akropolis-55664",
    "Aqua": "https://www.myludo.fr/#!/game/aqua-73746",
    "Windmill Valley": "https://www.myludo.fr/#!/game/windmill-valley-75718",
    "Take It Easy!": "https://www.myludo.fr/#!/game/take-it-easy-72302",
    "Rebirth": "https://www.myludo.fr/#!/game/rebirth-86622",
    "Patchwork 10e Anniversaire": "https://www.myludo.fr/#!/game/patchwork-20059",
    "Next Station Paris": "https://www.myludo.fr/#!/game/next-station-paris-74727",
    "Next Station London": "https://www.myludo.fr/#!/game/next-station-london-55261",
    "L'Ile Des Chats": "https://www.myludo.fr/#!/game/l-ile-des-chats-38772",
    "Koi": "https://www.myludo.fr/#!/game/koi-94495",
    "Frosted Blooms": "https://www.myludo.fr/#!/game/frosted-blooms-91724",
    "Cascadia Rolling Rivers": "https://www.myludo.fr/#!/game/cascadia-rolling-rivers-73113",
    "Cascadia Rolling Hills": "https://www.myludo.fr/#!/game/cascadia-rolling-hills-73114",
    "Cascadia": "https://www.myludo.fr/#!/game/cascadia-51951",
}

# ── Helpers notifs ──────────────────────────────────────────────

def notify_telegram(token: str, chat_id: str, text: str, photo_url: str = None):
    """Supporte 1 ou plusieurs chat_id séparés par virgule."""
    if not token or not chat_id:
        return False
    ids = [c.strip() for c in str(chat_id).split(",") if c.strip()]
    ok_any = False
    for cid in ids:
        if _notify_telegram_one(token, cid, text, photo_url=photo_url):
            ok_any = True
    return ok_any


def _telegram_plain(text: str) -> str:
    """Markdown Telegram est fragile avec les titres Vinted: garder un repli lisible."""
    # Without parse_mode Telegram accepts the original Unicode/punctuation and
    # links remain clickable. Escaping by deleting URL punctuation would damage
    # the useful part of the alert.
    return str(text)


def _notify_telegram_one(token: str, cid: str, text: str, photo_url: str = None) -> bool:
    """Send to one recipient, retrying once as plain text when Markdown/photo fails."""
    if not token or not cid:
        return False
    attempts = []
    if photo_url:
        attempts.append((f"https://api.telegram.org/bot{token}/sendPhoto",
                         {"chat_id": cid, "caption": text, "photo": photo_url, "parse_mode": "Markdown"}))
    else:
        attempts.append((f"https://api.telegram.org/bot{token}/sendMessage",
                         {"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}))
    # A malformed title or an unavailable image should not lose the alert.
    attempts.append((f"https://api.telegram.org/bot{token}/sendMessage",
                     {"chat_id": cid, "text": _telegram_plain(text), "disable_web_page_preview": False}))
    for index, (url, data) in enumerate(attempts):
        try:
            r = requests.post(url, data=data, timeout=15)
            if r.status_code == 200:
                try:
                    body = r.json()
                except (ValueError, AttributeError):
                    body = {}
                if body.get("ok") is True:
                    return True
                print(f"[telegram] Réponse invalide pour {cid}")
                return False
            if r.status_code != 400:
                print(f"[telegram] Erreur {r.status_code} pour {cid}: {r.text[:300]}")
                return False
            if index == len(attempts) - 1:
                print(f"[telegram] Erreur {r.status_code} pour {cid}: {r.text[:300]}")
        except Exception as e:
            print(f"[telegram] Exception: {type(e).__name__}")
            return False
    return False

def notify_discord(webhook: str, text: str, title: str = None, url: str = None, image: str = None):
    if not webhook:
        return False
    try:
        embed = {
            "title": title or "Nouvelle annonce Vinted",
            "description": text[:4000],
            "url": url,
            "color": 0x09B1BA,  # couleur Vinted
            "timestamp": datetime.utcnow().isoformat(),
        }
        if image:
            embed["thumbnail"] = {"url": image}
        payload = {"embeds": [embed], "content": f"🎲 **{title}**" if title else None}
        r = requests.post(webhook, json=payload, timeout=15)
        if r.status_code not in (200, 204):
            print(f"[discord] Erreur {r.status_code}: {r.text[:300]}")
            return False
        return True
    except Exception as e:
        print(f"[discord] Exception: {e}")
        return False

def notify_whatsapp(phone: str, apikey: str, text: str):
    """WhatsApp gratuit via CallMeBot (https://www.callmebot.com).
    1. Ajoute +34 644 10 55 84 à tes contacts WhatsApp
    2. Envoie "I allow callmebot to send me messages" au numéro pour obtenir l'apikey
    3. Définis WHATSAPP_PHONE (+33...) et WHATSAPP_APIKEY dans .env / secrets GH
    """
    if not phone or not apikey:
        return False
    try:
        # CallMeBot attend un GET avec text urlencodé
        url = f"https://api.callmebot.com/whatsapp.php?phone={quote(phone)}&text={quote(text)}&apikey={apikey}"
        r = requests.get(url, timeout=15)
        body = r.text.lower()
        if r.status_code == 200 and ("message queued" in body or "message sent" in body) and "error" not in body:
            print("[whatsapp] ✅ accepté")
            return True
        print(f"[whatsapp] Erreur {r.status_code}: {r.text[:300]}")
        return False
    except Exception as e:
        print(f"[whatsapp] Exception: {e}")
        return False

def notify_ntfy(topic: str, text: str, title: str = None):
    """Fallback gratuit ultra-simple : ntfy.sh (push sans app, ou app ntfy).
    topic = ton topic secret ex: vinted-next-paris-xyz123
    Env: NTFY_TOPIC
    Reçoit sur https://ntfy.sh/<topic> ou app mobile ntfy
    """
    if not topic:
        return False
    try:
        url = f"https://ntfy.sh/{topic.strip('/')}"
        headers = {"Title": title or "Vinted — Next Station Paris"}
        r = requests.post(url, data=text.encode("utf-8"), headers=headers, timeout=10)
        if r.status_code in (200, 204):
            print(f"[ntfy] ✅ {topic}")
            return True
        print(f"[ntfy] Erreur {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        print(f"[ntfy] {e}")
        return False

def notify_macos(title: str, message: str, url: str = None):
    """Notification macOS native via osascript + son"""
    try:
        safe_title = str(title).replace('"', "'")[:80]
        safe_msg = str(message).replace('"', "'")[:200]
        script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "Ping"'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False
        print(f"[macos] 🔔 {title} — {message}")
        if url:
            print(f"       🔗 {url}")
        return True
    except Exception as e:
        print(f"[macos] {e}")
        return False

def format_price(item) -> str:
    try:
        price = item.price if hasattr(item, 'price') else item.get('price', '?')
        currency = item.currency if hasattr(item, 'currency') else item.get('currency', 'EUR')
        # certains wrappers renvoient price en string "12.00"
        return f"{price} {currency}" if price else "prix ?"
    except:
        return "?"

def _parse_price_float(item) -> float | None:
    """Normalize a finite, non-negative EUR sale price; unknown values defer."""
    import math
    try:
        raw = item.get("price") if isinstance(item, dict) else getattr(item, "price", None)
        currency = item.get("currency", "EUR") if isinstance(item, dict) else getattr(item, "currency", "EUR")
        if isinstance(raw, dict):
            currency = raw.get("currency_code", raw.get("currency", currency))
            raw = raw.get("amount")
        if currency and str(currency).upper() != "EUR":
            return None
        if raw is None or isinstance(raw, bool):
            return None
        value = float(str(raw).replace(",", ".").replace("€", "").strip())
        return value if math.isfinite(value) and value >= 0 else None
    except (TypeError, ValueError):
        return None

# Frais acheteur Vinted (protection acheteur) : 0.70€ + 5% du prix — vérifié 09/2026
# (l'API search renvoie exactement ça via total_item_price).
BUYER_FEE_FIXED = 0.70
BUYER_FEE_RATE = 0.05

def get_item_total_price(item, price_value: float | None = None) -> tuple[float | None, bool]:
    """Prix frais Vinted inclus. Retourne (total, from_api).

    1) total_item_price de l'API search (vinted_scraper) si présent ;
    2) sinon calcul manuel 0.70€ + 5% (formule Vinted).
    """
    # 1) API directe (attribut, dict, ou json_data brut)
    candidates = []
    try:
        if hasattr(item, "total_item_price"):
            candidates.append(getattr(item, "total_item_price"))
    except:
        pass
    if isinstance(item, dict):
        candidates.append(item.get("total_item_price"))
        jd = item.get("json_data") or {}
        if isinstance(jd, dict):
            candidates.append(jd.get("total_item_price"))
    else:
        jd = getattr(item, "json_data", None)
        if isinstance(jd, dict):
            candidates.append(jd.get("total_item_price"))
    for v in candidates:
        try:
            if v is not None and str(v).strip() != "":
                return round(float(v), 2), True
        except:
            continue
    # 2) fallback manuel
    if price_value is None:
        price_value = _parse_price_float(item)
    if price_value is not None:
        return round(price_value * (1 + BUYER_FEE_RATE) + BUYER_FEE_FIXED, 2), False
    return None, False

def format_price_with_fees(item) -> str:
    """'20.0 EUR (≈21.70€ frais inclus)' — total via API si dispo, sinon calculé."""
    base = format_price(item)
    total, _from_api = get_item_total_price(item)
    if total is None:
        return base
    return f"{base} (≈{total:.2f}€ frais inclus)"

def get_item_url(item) -> str:
    # vinted_scraper fournit .url ou .path
    for attr in ("url", "path", "item_url"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            if v:
                if v.startswith("http"):
                    return v
                return f"https://www.vinted.fr{v}"
    if isinstance(item, dict):
        for k in ("url", "path", "item_url"):
            if k in item and item[k]:
                v = item[k]
                return v if v.startswith("http") else f"https://www.vinted.fr{v}"
        # fallback via id
        if "id" in item:
            return f"https://www.vinted.fr/items/{item['id']}"
    # via id attribute
    if hasattr(item, 'id'):
        return f"https://www.vinted.fr/items/{item.id}"
    return "https://www.vinted.fr/catalog?search_text=next%20station%20paris"

def get_item_image(item) -> str:
    for attr in ("photo", "image", "thumbnail"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            if isinstance(v, dict) and "url" in v:
                return v["url"]
            if isinstance(v, str) and v.startswith("http"):
                return v
            if hasattr(v, "url"):
                return v.url
    if isinstance(item, dict):
        if "photo" in item and isinstance(item["photo"], dict):
            return item["photo"].get("url") or item["photo"].get("full_size_url") or ""
        if "image" in item:
            return item["image"]
    return None

def get_item_title(item) -> str:
    for attr in ("title", "name"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            if v: return str(v)
    if isinstance(item, dict):
        return item.get("title") or item.get("name") or "Annonce Vinted"
    return "Annonce Vinted"

def get_item_description(item) -> str:
    """Retourne description si déjà présente sur l'item (search n'en a pas)."""
    for attr in ("description", "desc"):
        if hasattr(item, attr):
            v = getattr(item, attr)
            if v and isinstance(v, str) and v.strip():
                return v.strip()
    if isinstance(item, dict):
        for k in ("description", "desc"):
            if k in item and item[k]:
                return str(item[k]).strip()
    return ""

def get_item_photos(item) -> list:
    """Retourne liste d'URLs photos (search → VintedImage)."""
    urls = []
    # VintedItem.photos est la source principale (liste VintedImage)
    if hasattr(item, "photos") and item.photos:
        for p in item.photos:
            u = None
            if hasattr(p, "url") and p.url:
                u = p.url
            elif isinstance(p, dict) and p.get("url"):
                u = p["url"]
            elif hasattr(p, "full_size_url") and p.full_size_url:
                u = p.full_size_url
            if u and u.startswith("http"):
                urls.append(u)
        if urls:
            return urls
    # fallback single image
    single = get_item_image(item)
    if single:
        return [single]
    if isinstance(item, dict):
        if "photos" in item and isinstance(item["photos"], list):
            for p in item["photos"]:
                if isinstance(p, dict) and p.get("url"):
                    urls.append(p["url"])
                elif isinstance(p, str) and p.startswith("http"):
                    urls.append(p)
    return urls

def enrich_item_description(item, verbose=False) -> str:
    """Tente de récupérer og:description depuis la page Vinted (via VintedScraper.item)."""
    existing = get_item_description(item)
    if existing and len(existing) > 30:
        return existing
    iid = get_item_id(item)
    if not iid:
        return existing
    try:
        from vinted_scraper import VintedScraper
        from vinted_scraper.models import OgField
        global _french_scraper
        if _french_scraper is None:
            _french_scraper = VintedScraper("https://www.vinted.fr")
        # On réutilise le scraper FR (gère cookies), mais on fetch description seule
        data = _french_scraper.item(str(iid), [OgField.DESCRIPTION, OgField.TITLE])
        if hasattr(data, "description") and data.description:
            if verbose:
                print(f"[desc] {iid} → {data.description[:80]}...")
            return data.description.strip()
        if isinstance(data, dict) and data.get("description"):
            return str(data["description"]).strip()
    except Exception as e:
        if verbose:
            print(f"[desc] enrich {iid} err: {e}")
    return existing

# ── DB ──────────────────────────────────────────────────────────

def init_db(db_path: Path):
    con = sqlite3.connect(str(db_path))
    con.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            title TEXT,
            price TEXT,
            url TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # meta pour persister l'état journalier (ex: last_watchlist_date)
    con.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # cache pays vendeur persistant (le pays ne change quasiment jamais —
    # évite de re-payer 1 appel API / vendeur à chaque run)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_country (
            user_id TEXT PRIMARY KEY,
            country TEXT,
            checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Persistent delivery queue. A row is one destination, so a partial send
    # is retried without duplicating destinations that already succeeded.
    con.execute("""
        CREATE TABLE IF NOT EXISTS delivery_outbox (
            item_id TEXT NOT NULL,
            destination TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (item_id, destination)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS llm_rejections (
            item_id TEXT NOT NULL,
            query_key TEXT NOT NULL,
            filter_version TEXT NOT NULL,
            reason TEXT,
            confidence REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (item_id, query_key, filter_version)
        )
    """)
    con.commit()
    return con


def _query_key(query_cfg: dict) -> str:
    """Stable scope for an LLM decision; a rejection belongs to one search."""
    raw = str(query_cfg.get("url") or query_cfg.get("name") or "").strip().lower()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _llm_filter_version(query_cfg: dict, llm_cfg: dict) -> str:
    prompt_version = getattr(llm_filter, "PROMPT_VERSION", "unknown")
    model = llm_cfg.get("model") or os.getenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
    profiles = getattr(llm_filter, "GAME_PROFILES", {})
    profiles_raw = json.dumps(profiles, sort_keys=True, default=str, ensure_ascii=False)
    profiles_hash = hashlib.sha256(profiles_raw.encode("utf-8")).hexdigest()
    relevant = {"query": query_cfg, "llm": llm_cfg, "prompt_version": prompt_version,
                "model": model, "profiles_hash": profiles_hash}
    raw = json.dumps(relevant, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_llm_rejected(con, item_id: str, query_cfg: dict, filter_version: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM llm_rejections WHERE item_id=? AND query_key=? AND filter_version=?",
        (str(item_id), _query_key(query_cfg), filter_version),
    ).fetchone()
    return row is not None


def mark_llm_rejected(con, item_id: str, query_cfg: dict, filter_version: str,
                      reason: str = "", confidence: float | None = None):
    con.execute(
        "INSERT OR REPLACE INTO llm_rejections(item_id,query_key,filter_version,reason,confidence) VALUES (?,?,?,?,?)",
        (str(item_id), _query_key(query_cfg), filter_version, reason or "", confidence),
    )
    con.commit()

def is_seen(con, item_id: str) -> bool:
    cur = con.execute("SELECT 1 FROM seen WHERE id=?", (str(item_id),))
    return cur.fetchone() is not None

def mark_seen(con, item_id: str, title: str, price: str, url: str):
    con.execute("INSERT OR IGNORE INTO seen (id, title, price, url) VALUES (?,?,?,?)",
                (str(item_id), title, price, url))
    con.commit()


def _outbox_enqueue(con, item_id: str, deliveries: dict[str, dict]):
    for destination, payload in deliveries.items():
        con.execute(
            "INSERT OR IGNORE INTO delivery_outbox(item_id,destination,payload,status) VALUES (?,?,?,'pending')",
            (str(item_id), destination, json.dumps(payload, ensure_ascii=False)),
        )
    con.commit()


def _configured_deliveries(item_id: str, title: str, price: str, link: str, img: str,
                          name: str, msg_md: str, msg_plain: str, msg_wa: str,
                          telegram_token: str, telegram_chat: str, discord_webhook: str,
                          whatsapp_phone: str, whatsapp_apikey: str, ntfy_topic: str) -> dict[str, dict]:
    deliveries = {}
    if telegram_token and telegram_chat:
        for cid in [c.strip() for c in str(telegram_chat).split(",") if c.strip()]:
            digest = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:24]
            deliveries[f"telegram:{digest}"] = {"kind": "telegram", "target_digest": digest,
                                               "text": msg_md, "photo_url": img or ""}
    if discord_webhook:
        deliveries["discord"] = {"kind": "discord", "text": msg_plain,
                                  "title": title, "url": link, "image": img or ""}
    if whatsapp_phone and whatsapp_apikey:
        deliveries["whatsapp"] = {"kind": "whatsapp", "text": msg_wa}
    if ntfy_topic:
        deliveries["ntfy"] = {"kind": "ntfy", "text": msg_plain, "title": f"{name} — {price}"}
    for payload in deliveries.values():
        payload["item_name"] = name
    return deliveries


def _send_outbox_payload(payload: dict) -> bool:
    kind = payload.get("kind")
    if kind == "telegram":
        digest = payload.get("target_digest", "")
        targets = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
        target = next((cid for cid in targets if hashlib.sha256(cid.encode("utf-8")).hexdigest()[:24] == digest), None)
        return _notify_telegram_one(os.getenv("TELEGRAM_BOT_TOKEN", ""), target or "",
                                    payload.get("text", ""), payload.get("photo_url") or None)
    if kind == "discord":
        return notify_discord(os.getenv("DISCORD_WEBHOOK_URL", ""), payload.get("text", ""),
                              title=payload.get("title"), url=payload.get("url"), image=payload.get("image") or None)
    if kind == "whatsapp":
        return notify_whatsapp(os.getenv("WHATSAPP_PHONE", ""), os.getenv("WHATSAPP_APIKEY", ""), payload.get("text", ""))
    if kind == "ntfy":
        return notify_ntfy(os.getenv("NTFY_TOPIC", ""), payload.get("text", ""), title=payload.get("title"))
    return False


def process_outbox(con, verbose=False, item_ids=None, attempted=None, deadline=None) -> set[str]:
    """Retry pending destinations and return item ids fully delivered."""
    attempted = attempted if attempted is not None else set()
    if item_ids:
        placeholders = ",".join("?" for _ in item_ids)
        rows = con.execute(f"SELECT item_id,destination,payload,attempts FROM delivery_outbox WHERE status!='sent' AND item_id IN ({placeholders})", tuple(item_ids)).fetchall()
    else:
        rows = con.execute("SELECT item_id,destination,payload,attempts FROM delivery_outbox WHERE status!='sent'").fetchall()
    completed = set()
    for item_id, destination, raw, attempts in rows:
        if deadline is not None and time.monotonic() >= deadline:
            break
        key = (str(item_id), str(destination))
        if key in attempted:
            continue
        attempted.add(key)
        try:
            ok = _send_outbox_payload(json.loads(raw))
            if ok:
                con.execute("UPDATE delivery_outbox SET status='sent',updated_at=CURRENT_TIMESTAMP WHERE item_id=? AND destination=?",
                            (item_id, destination))
                con.commit()
            else:
                con.execute("UPDATE delivery_outbox SET attempts=attempts+1,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE item_id=? AND destination=?",
                            ("send_failed", item_id, destination))
                con.commit()
                if verbose:
                    print(f"[outbox] échec {item_id}/{destination}, nouvelle tentative au prochain scan")
        except Exception as exc:
            con.execute("UPDATE delivery_outbox SET attempts=attempts+1,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE item_id=? AND destination=?",
                        (type(exc).__name__, item_id, destination))
            con.commit()
            if verbose:
                print(f"[outbox] erreur {item_id}/{destination}: {exc}")
    item_rows = con.execute("SELECT DISTINCT item_id FROM delivery_outbox" + (f" WHERE item_id IN ({','.join('?' for _ in item_ids)})" if item_ids else ""), tuple(item_ids or ())).fetchall()
    for (item_id,) in item_rows:
        pending = con.execute("SELECT 1 FROM delivery_outbox WHERE item_id=? AND status!='sent' LIMIT 1", (item_id,)).fetchone()
        if not pending:
            if is_seen(con, item_id):
                continue
            completed.add(str(item_id))
            # The payload carries metadata so an item can be finalized even
            # when it no longer appears in a later Vinted search.
            row = con.execute("SELECT payload FROM delivery_outbox WHERE item_id=? LIMIT 1", (item_id,)).fetchone()
            if row:
                try:
                    meta = json.loads(row[0])
                    if not is_seen(con, item_id):
                        if meta.get("event_type") == "WATCHLIST":
                            watchlist_date = meta.get("watchlist_date", "")
                            if watchlist_date:
                                set_meta(con, "last_watchlist_date", watchlist_date)
                        mark_seen(con, item_id, meta.get("item_title", ""), meta.get("item_price", ""), meta.get("item_url", ""))
                        event = meta.get("event_type", "ALERT")
                        append_history(f"{event} | {item_id} | {meta.get('item_title', '')[:80]} | {meta.get('item_price', '')} | {meta.get('item_url', '')} | {meta.get('item_name', '')}", verbose=False)
                except Exception as exc:
                    raise RuntimeError(f"finalisation outbox impossible: {type(exc).__name__}") from exc
    return completed

def get_meta(con, key: str) -> str | None:
    cur = con.execute("SELECT value FROM meta WHERE key=?", (key,))
    row = cur.fetchone()
    return row[0] if row else None

def set_meta(con, key: str, value: str):
    con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))
    con.commit()

def _paris_today_iso() -> str:
    """Date du jour à Paris (Europe/Paris) en ISO YYYY-MM-DD."""
    try:
        if ZoneInfo is not None:
            return datetime.now(ZoneInfo("Europe/Paris")).date().isoformat()
    except Exception:
        pass
    # fallback UTC (décalage ~2h, acceptable)
    return datetime.now().date().isoformat()

def get_item_id(item) -> str:
    value = item.get("id") if isinstance(item, dict) else getattr(item, "id", None)
    return str(value) if value is not None and str(value).strip() else None

def get_user_id(item) -> str:
    try:
        if hasattr(item, "user") and item.user and hasattr(item.user, "id"):
            return str(item.user.id)
        if isinstance(item, dict) and "user" in item and item["user"]:
            return str(item["user"].get("id", ""))
        # fallback via json_data
        if hasattr(item, "json_data") and isinstance(item.json_data, dict):
            u = item.json_data.get("user", {})
            if u and "id" in u:
                return str(u["id"])
    except:
        pass
    return None

def get_user_country(item, con=None, verbose=False) -> str:
    """Retourne country_code (FR, IT, DE...) du vendeur, avec cache mémoire + SQLite.
    Utilise Vinted API /api/v2/users/{id}. Retourne None si indisponible.
    Gère 429 avec retry. Seuls les pays résolus sont persistés (le pays d'un
    vendeur ne change quasiment jamais) ; les inconnus sont retestés au run
    suivant tant que l'annonce est fraîche (anti faux négatif sur 429 transient).
    """
    global _french_scraper, _user_country_cache
    uid = get_user_id(item)
    if not uid:
        return None
    if uid in _user_country_cache:
        return _user_country_cache[uid]
    if con is not None:
        try:
            cur = con.execute("SELECT country FROM user_country WHERE user_id=?", (str(uid),))
            row = cur.fetchone()
            if row:
                _user_country_cache[uid] = row[0]
                return row[0]
        except Exception:
            pass  # table absente (vieux seen.db) → créée au prochain init_db
    for attempt in range(2):
        try:
            if _french_scraper is None:
                from vinted_scraper import VintedScraper
                _french_scraper = VintedScraper("https://www.vinted.fr")
            data = _french_scraper.curl(f"/api/v2/users/{uid}")
            jd = getattr(data, "json_data", data) if hasattr(data, "json_data") else data
            if isinstance(jd, dict):
                user = jd.get("user", {})
                cc = user.get("country_code") or user.get("country_iso_code") or user.get("iso_country_code")
                if cc:
                    _user_country_cache[uid] = cc
                    if con is not None:
                        try:
                            con.execute("INSERT OR REPLACE INTO user_country (user_id, country) VALUES (?,?)",
                                        (str(uid), cc))
                            con.commit()
                        except Exception:
                            pass
                    if verbose:
                        print(f"[fr] vendeur {uid} -> {cc}")
                    time.sleep(0.25)
                    return cc
        except Exception as e:
            msg = str(e)
            is_429 = "429" in msg or "429" in str(getattr(e, 'args', ''))
            if is_429 and attempt == 0:
                if verbose:
                    print(f"[fr] 429 pour {uid}, pause 1.2s et retry")
                time.sleep(1.2)
                continue
            if verbose:
                print(f"[fr] erreur pays vendeur {uid}: {e}")
            break
        time.sleep(0.15)
    # Do not cache transient failures: 429/network errors should be retried on
    # the next scan instead of permanently excluding this seller in-process.
    time.sleep(0.1)
    return None

def filter_french_items(items, con=None, verbose=False, deadline=None):
    """Garde uniquement les annonces de vendeurs FR (annonce en français).
    Si pays inconnu (429 persistant), on exclut par défaut pour éviter les faux positifs IT/EN.
    (L'annonce reste non-marquée et sera retestée aux runs suivants tant qu'elle est fraîche.)"""
    out = []
    for it in items:
        if deadline is not None and time.monotonic() >= deadline:
            raise ScanBudgetExceeded("budget atteint pendant la vérification des vendeurs")
        cc = get_user_country(it, con=con, verbose=verbose)
        if cc is None:
            if verbose:
                print(f"[fr] pays inconnu, exclu (sécurité FR): {get_item_title(it)[:60]}")
            continue
        elif cc.upper() == "FR":
            out.append(it)
        else:
            if verbose:
                print(f"[fr] exclu non-FR ({cc}): {get_item_title(it)[:60]}")
    return out

def get_item_timestamp(item) -> int | None:
    """Timestamp Unix de mise en ligne (proxy de la date de création).

    L'API search ne renvoie AUCUN champ date — on utilise
    photos[0].high_resolution.timestamp (présent à 100%, corrélé à
    l'ordre newest_first, vérifié 09/2026). Retourne None si absent.
    """
    try:
        jd = getattr(item, "json_data", None)
        if isinstance(jd, dict):
            for p in (jd.get("photos") or []):
                hr = (p or {}).get("high_resolution") or {}
                if hr.get("timestamp"):
                    return int(hr["timestamp"])
            ph = jd.get("photo") or {}
            hr = (ph or {}).get("high_resolution") or {}
            if hr.get("timestamp"):
                return int(hr["timestamp"])
        if hasattr(item, "photos") and item.photos:
            for p in item.photos:
                hr = getattr(p, "high_resolution", None)
                ts = getattr(hr, "timestamp", None) if hr else None
                if ts:
                    return int(ts)
    except Exception:
        pass
    return None

def filter_recent_items(items, max_age_hours: float | None, verbose=False):
    """Exclut les annonces plus vieilles que max_age_hours (via timestamp photo).
    None/<=0 = désactivé. Sans timestamp → conservé (fail-open)."""
    if not max_age_hours or max_age_hours <= 0:
        return items
    now = time.time()
    out = []
    for it in items:
        ts = get_item_timestamp(it)
        if ts is None:
            out.append(it)
            continue
        age_h = (now - ts) / 3600
        if age_h > max_age_hours:
            if verbose:
                print(f"[date] exclu ancien ({age_h/24:.1f}j): {get_item_title(it)[:60]}")
            continue
        out.append(it)
    return out

# ── Vinted fetch ────────────────────────────────────────────────

def _fetch_backoff(attempt: int) -> float:
    """Backoff exponentiel plafonné : 2s, 4s, 8s, 16s, 20s…"""
    try:
        base = float(FETCH_BACKOFF_BASE)
    except (TypeError, ValueError):
        base = 2.0
    return min(float(FETCH_BACKOFF_MAX), base * (2 ** attempt))


def _should_refresh_session(exc: Exception) -> bool:
    """Une session expirée/bloquée mérite un nouveau cookie, pas un 500/timeout isolé."""
    msg = str(exc).lower()
    return any(k in msg for k in ("406", "401", "403", "429", "session", "cookie", "auth", "token", "forbidden", "unauthorized"))


def fetch_items(query_url: str, per_page: int = 20, verbose: bool = False):
    """
    Utilise vinted_scraper (synchrone, gère cookies Cloudflare)
    Fallback: requête directe si lib absente
    """
    global _french_scraper
    try:
        from vinted_scraper import VintedScraper
    except ImportError:
        print("[!] vinted_scraper non installé — pip install -r requirements.txt")
        sys.exit(1)

    # Extrait les params de l'URL pour les passer à scraper.search()
    parsed = urlparse(query_url)
    qs = parse_qs(parsed.query)
    # parse_qs donne des listes
    params = {k: v[0] if len(v) == 1 else v for k, v in qs.items()}
    # scraper attend base_url séparé
    base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else "https://www.vinted.fr"
    if verbose:
        print(f"[fetch] base_url={base_url} params={params}")

    params["per_page"] = per_page
    last_error = None
    scraper = _scraper_cache.get(base_url)
    for attempt in range(FETCH_RETRIES):
        try:
            if scraper is None:
                scraper = VintedScraper(base_url)
                _scraper_cache[base_url] = scraper
                # User-country and description calls can reuse the same session.
                if base_url == "https://www.vinted.fr" and _french_scraper is None:
                    _french_scraper = scraper
            items = scraper.search(params)
            if verbose:
                print(f"[fetch] {len(items)} items reçus via vinted_scraper")
            return items[:per_page]
        except Exception as exc:
            last_error = exc
            if verbose:
                print(f"[fetch] tentative {attempt + 1}/{FETCH_RETRIES} échouée: {exc}")
            # Ne recrée la session que si elle semble en cause (ou après 2
            # échecs avec la même session) : pendant un outage 500/timeout,
            # garder la session évite un burst de cookies qui aggrave le blocage.
            if _should_refresh_session(exc) or attempt >= 1:
                if _french_scraper is scraper:
                    _french_scraper = None
                _scraper_cache.pop(base_url, None)
                scraper = None
            if attempt < FETCH_RETRIES - 1:
                time.sleep(_fetch_backoff(attempt))
    if verbose and last_error:
        import traceback
        traceback.print_exception(last_error)
    raise last_error

def _norm(s: str) -> str:
    """Minuscules + sans accents (anti faux négatifs : 'île' matche 'ile')."""
    import unicodedata
    s = (s or "").lower()
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))

def apply_filters(items, filters: dict, query_cfg: dict, verbose=False):
    """Filtres locaux optionnels (prix, mots-clés, insensibles aux accents).

    Politique anti faux négatifs : must_contain minimal (1-3 tokens distinctifs),
    must_not_contain reste vide par défaut. Les seules exceptions sont les
    sous-titres stables qui désignent toujours un autre jeu (par exemple
    Patchwork Doodle pour la recherche Patchwork 10e Anniversaire).
    """
    out = []
    price_max = query_cfg.get("price_max", filters.get("price_max_global"))
    price_min = query_cfg.get("price_min")
    # supporte must_contain au niveau query OU global
    must_contain = query_cfg.get("must_contain") or filters.get("must_contain") or []
    must_not_contain = query_cfg.get("must_not_contain") or filters.get("must_not_contain") or []

    for it in items:
        title = _norm(get_item_title(it))
        # must_contain
        if must_contain and not all(_norm(kw) in title for kw in must_contain):
            if verbose: print(f"[filter] exclu (must_contain): {get_item_title(it)[:60]}")
            continue
        if must_not_contain and any(_norm(kw) in title for kw in must_not_contain):
            if verbose: print(f"[filter] exclu (must_not_contain): {get_item_title(it)[:60]}")
            continue
        # Sold and unknown-price listings are deferred, never marked seen here.
        data = it if isinstance(it, dict) else getattr(it, "json_data", {})
        data = data if isinstance(data, dict) else {}
        sold = data.get("is_sold", getattr(it, "is_sold", False))
        if filters.get("exclude_sold", True) and sold in (True, 1, "true", "True"):
            if verbose: print(f"[filter] exclu vendu: {title}")
            continue
        p = _parse_price_float(it)
        if p is None:
            if verbose: print(f"[filter] prix inconnu, à revérifier: {title}")
            continue
        if price_max is not None and p > float(price_max):
            if verbose: print(f"[filter] exclu prix {p} > {price_max}: {title}")
            continue
        if price_min is not None and p < float(price_min):
            if verbose: print(f"[filter] exclu prix {p} < {price_min}: {title}")
            continue
        out.append(it)
    return out

# ── Main ────────────────────────────────────────────────────────

class ScanFetchError(RuntimeError):
    """A scan did not complete successfully; persisted work can be retried."""


class ScanBudgetExceeded(ScanFetchError):
    """Graceful time limit, with query cursor persisted for the next run."""


def validate_config(cfg):
    """Fail before scanning when a configuration is malformed."""
    import math
    if not isinstance(cfg, dict) or not isinstance(cfg.get("queries"), list) or not cfg["queries"]:
        raise ValueError("config: queries doit être une liste non vide")
    settings = cfg.get("settings", {})
    filters = cfg.get("filters", {})
    if not isinstance(settings, dict) or not isinstance(filters, dict):
        raise ValueError("config: settings et filters doivent être des objets")
    def number(value, name, minimum=0):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
            raise ValueError(f"config: {name} invalide")
    for key in ("poll_interval", "per_page", "scan_budget_seconds"):
        if key in settings:
            number(settings[key], key, 1)
    if "per_page" in settings and not isinstance(settings["per_page"], int):
        raise ValueError("config: per_page doit être entier")
    if "max_age_days" in settings:
        number(settings["max_age_days"], "max_age_days")
    for section in (settings, filters):
        for key in ("only_french", "exclude_sold"):
            if key in section and not isinstance(section[key], bool):
                raise ValueError(f"config: {key} doit être un booléen")
    if "price_max_global" in filters:
        number(filters["price_max_global"], "price_max_global")
    for key in ("must_contain", "must_not_contain"):
        values = filters.get(key, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError(f"config: filters.{key} doit être une liste de mots")
    llm = settings.get("llm_filter", {})
    if not isinstance(llm, dict):
        raise ValueError("config: llm_filter doit être un objet")
    if "enabled" in llm and not isinstance(llm["enabled"], bool):
        raise ValueError("config: llm_filter.enabled doit être un booléen")
    if "model" in llm and (not isinstance(llm["model"], str) or not llm["model"].strip()):
        raise ValueError("config: modèle LLM invalide")
    threshold = llm.get("confidence_threshold", 0.6)
    number(threshold, "confidence_threshold")
    if threshold > 1:
        raise ValueError("config: confidence_threshold doit être entre 0 et 1")
    images = llm.get("max_images", 2)
    if isinstance(images, bool) or not isinstance(images, int) or not 1 <= images <= 3:
        raise ValueError("config: max_images doit être entre 1 et 3")
    names = set()
    for q in cfg["queries"]:
        if not isinstance(q, dict) or not isinstance(q.get("name"), str) or not q["name"].strip():
            raise ValueError("config: chaque recherche doit avoir un nom")
        if q["name"] in names:
            raise ValueError(f"config: nom en double: {q['name']}")
        names.add(q["name"])
        if not isinstance(q.get("url"), str):
            raise ValueError("config: URL manquante ou invalide")
        if "only_french" in q and not isinstance(q["only_french"], bool):
            raise ValueError("config: only_french doit être un booléen")
        url = urlparse(q["url"])
        if url.scheme != "https" or url.hostname not in ("www.vinted.fr", "vinted.fr"):
            raise ValueError(f"config: URL Vinted invalide pour {q['name']}")
        for key in ("price_max", "price_min", "max_age_days"):
            if key in q:
                number(q[key], key)
        if q.get("price_min", 0) > q.get("price_max", float("inf")):
            raise ValueError("config: price_min dépasse price_max")
        for key in ("must_contain", "must_not_contain"):
            value = q.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ValueError(f"config: {key} doit être une liste de mots")
    return cfg


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return validate_config(yaml.safe_load(f))

def check_once(cfg, con, args):
    settings = cfg.get("settings", {})
    filters = cfg.get("filters", {})
    per_page = settings.get("per_page", 20)
    queries = cfg.get("queries", [])
    # LLM vision config (fail-open si pas de clé)
    llm_cfg = settings.get("llm_filter", {}) or {}
    llm_enabled = bool(llm_cfg.get("enabled", True)) and not getattr(args, "no_llm", False)
    # si pas de clé OpenRouter, désactive silencieusement (fail-open)
    if llm_enabled and llm_filter is not None and not os.getenv("OPENROUTER_API_KEY"):
        llm_enabled = False
        if args.verbose:
            print("[llm] OPENROUTER_API_KEY manquante → filtre vision désactivé (fail-open)")
    if llm_enabled and llm_filter is None:
        llm_enabled = False
        if args.verbose:
            print("[llm] module llm_filter non importable → désactivé")
    llm_threshold = float(llm_cfg.get("confidence_threshold", 0.6))
    llm_max_images = int(llm_cfg.get("max_images", 2))

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")  # peut être "id1,id2" (plusieurs destinataires)
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    whatsapp_phone = os.getenv("WHATSAPP_PHONE", "")
    whatsapp_apikey = os.getenv("WHATSAPP_APIKEY", "")
    ntfy_topic = os.getenv("NTFY_TOPIC", "")

    has_notifier = bool(telegram_token and telegram_chat) or bool(discord_webhook) or bool(whatsapp_phone and whatsapp_apikey) or bool(ntfy_topic)
    verbose = args.verbose

    # One explicit dry-run flag governs every send and all queue retries.
    dry_run = bool(getattr(args, "once_no_notify", False) or
                   (args.limit is not None and not args.force_notify))
    deadline = time.monotonic() + float(settings.get("scan_budget_seconds", 420))
    attempted_deliveries = set()
    if not dry_run:
        process_outbox(con, verbose=verbose, attempted=attempted_deliveries, deadline=deadline)
    cursor = 0
    if not dry_run:
        saved = get_meta(con, "scan_resume") or ""
        try:
            cursor = int(saved.rsplit("|", 1)[-1]) % len(queries)
        except (ValueError, ZeroDivisionError):
            pass
    indexed_queries = list(enumerate(queries))
    indexed_queries = indexed_queries[cursor:] + indexed_queries[:cursor]

    all_new = []
    fetch_attempted = 0
    fetch_failed = 0
    watchlist_sent_this_run = False
    # Watchlist 1×/jour max : seulement au premier run du jour qui a au moins 1 vraie nouvelle annonce
    last_watchlist_date = get_meta(con, "last_watchlist_date")
    paris_today = _paris_today_iso()
    should_send_watchlist_today = (last_watchlist_date != paris_today)
    if verbose:
        print(f"[watchlist] last_sent={last_watchlist_date} today={paris_today} should_send_today={should_send_watchlist_today}")

    def get_watchlist_text():
        # Watchlist simplifiée : juste noms (lien MyLudo) + prix — triée alphabétiquement (config l'est déjà)
        lines = []
        for qq in queries:
            name = qq.get('name')
            myludo = MYLUDO_EXACT.get(name, get_myludo_url(name))
            lines.append(f"• [{name}]({myludo}) ≤{qq.get('price_max')}€")
        header = f"📋 *Watchlist — {len(queries)} jeux*\n"
        body = "\n".join(lines)
        return header + body

    for query_index, q in indexed_queries:
        if not dry_run:
            # Prefix makes merge-by-max select the most recent cursor update.
            set_meta(con, "scan_resume", f"{datetime.now(ZoneInfo('UTC')).isoformat()}|{query_index}")
        if time.monotonic() >= deadline:
            raise ScanBudgetExceeded("budget atteint; reprise à cette recherche au prochain scan")
        # Pacing anti-burst : espace les recherches pour ne pas déclencher
        # le 429/500 anti-bot de Vinted (15 requêtes rafale = suspect).
        if fetch_attempted > 0:
            try:
                time.sleep(float(FETCH_PACING_SECONDS))
            except (TypeError, ValueError):
                time.sleep(1.5)
        name = q.get("name", "Recherche Vinted")
        url = q.get("url", "")
        if not url:
            continue
        print(f"\n{'='*60}\n🔍 [{name}] {url}\n{'='*60}")
        fetch_attempted += 1
        try:
            items = fetch_items(url, per_page=per_page, verbose=verbose)
        except Exception as e:
            fetch_failed += 1
            print(f"[ERR] fetch failed pour {name}: {e}")
            continue

        # filtres prix / mots-clés (local, gratuit)
        items = apply_filters(items, filters, q, verbose=verbose)
        # anti-doublons TÔT (local, avant les appels API FR coûteux) — sauf dry-run
        if dry_run:
            # mode debug : affiche sans filtrer seen, sans notifier
            pass
        else:
            before = len(items)
            kept = []
            for it in items:
                iid = get_item_id(it)
                if iid and (is_seen(con, iid) or con.execute(
                    "SELECT 1 FROM delivery_outbox WHERE item_id=? LIMIT 1", (iid,)
                ).fetchone()):
                    if verbose:
                        print(f"[seen] déjà vu {iid}: {get_item_title(it)[:60]}")
                    continue
                kept.append(it)
            items = kept
            if verbose and len(items) != before:
                print(f"[seen] {before} → {len(items)} après anti-doublons")
        # filtre fraîcheur (local, via timestamp photo — proxy date création)
        max_age_days = q.get("max_age_days", settings.get("max_age_days", 3))
        if max_age_days:
            before = len(items)
            items = filter_recent_items(items, float(max_age_days) * 24, verbose=verbose)
            if verbose and len(items) != before:
                print(f"[date] {before} → {len(items)} après filtre fraîcheur (<{max_age_days}j)")
        # filtre annonce en français (vendeur FR, 1 appel API / vendeur) si activé
        only_french = q.get("only_french", filters.get("only_french", settings.get("only_french", False)))
        if only_french:
            before = len(items)
            items = filter_french_items(items, con=con, verbose=verbose, deadline=deadline)
            if verbose and len(items) != before:
                print(f"[fr] {before} → {len(items)} après filtre FR")

        if not items:
            print("  → Aucune nouvelle annonce.")
            continue

        # limite d'affichage en mode --limit
        display_items = items[:args.limit] if args.limit else items

        for it in display_items:
            if time.monotonic() >= deadline:
                raise ScanBudgetExceeded("budget atteint; annonces restantes revérifiées au prochain scan")
            title = get_item_title(it)
            price = format_price(it)
            link = get_item_url(it)
            img = get_item_image(it)
            iid = get_item_id(it) or link

            print(f"  🎲 {title}\n     💰 {format_price_with_fees(it)}\n     🔗 {link}")
            if img and verbose:
                print(f"     🖼️ {img}")

            should_notify = not dry_run

            # ── Filtre LLM vision (Qwen 3.7 Flash) + réf MyLudo ───────────────
            if should_notify and llm_enabled:
                try:
                    if is_llm_rejected(con, iid, q, _llm_filter_version(q, llm_cfg)):
                        if verbose:
                            print(f"  [llm] rejet déjà mémorisé pour cette recherche: {iid}")
                        continue
                    desc = enrich_item_description(it, verbose=verbose)
                    photos = get_item_photos(it)
                    # Référence visuelle MyLudo (boîte officielle) pour comparaison A vs B.
                    # llm_filter résout l'image via game_name seul (dict statique 15 jeux),
                    # myludo_url sert de fallback dynamique si nouveau jeu ajouté au config.
                    myludo_ref = MYLUDO_EXACT.get(name, get_myludo_url(name))
                    is_true, reason, conf, raw = llm_filter.is_true_positive(
                        game_name=name,
                        title=title,
                        description=desc,
                        price=price,
                        image_urls=photos,
                        verbose=verbose,
                        max_images=llm_max_images,
                        myludo_url=myludo_ref,
                        model=llm_cfg.get("model"),
                    )
                    if not is_true:
                        if conf >= llm_threshold:
                            print(f"  [llm] ✂️ exclu faux positif ({conf:.2f}): {reason}")
                            if verbose and photos:
                                print(f"       🖼️ {photos[0][:90]}...")
                            # Rejection is scoped to this query and filter version.
                            # It must not hide the same item from another query.
                            if iid:
                                mark_llm_rejected(con, iid, q, _llm_filter_version(q, llm_cfg), reason, conf)
                                # trace même les exclusions pour audit (faux positifs non envoyés)
                                # append_history(f"FILTERED | {iid} | {title[:60]} | {price} | {name} | {reason[:80]}", verbose=verbose)
                            time.sleep(0.35)  # évite burst 429 shared pool
                            continue  # skip notification
                        else:
                            print(f"  [llm] ⚠️ incertain ({conf:.2f}): {reason} → laisse passer")
                    else:
                        if verbose:
                            print(f"  [llm] ✅ vrai jeu ({conf:.2f}): {reason}")
                    time.sleep(0.35)  # throttle LLM
                except Exception as e:
                    print(f"  [llm] erreur filtre ({e}) → fail-open, laisse passer")
                    if verbose:
                        import traceback; traceback.print_exc()

            if should_notify:
                if time.monotonic() >= deadline:
                    raise ScanBudgetExceeded("budget atteint avant envoi; reprise au prochain scan")
                # Daily reminder uses the same per-recipient retry queue.
                if should_send_watchlist_today and not watchlist_sent_this_run and telegram_token and telegram_chat:
                    day_id = f"watchlist:{paris_today}"
                    reminder = _configured_deliveries(day_id, "Watchlist", "", "", "", "",
                        get_watchlist_text(), "", "", telegram_token, telegram_chat, "", "", "", "")
                    for delivery in reminder.values():
                        delivery.update({"event_type": "WATCHLIST", "watchlist_date": paris_today,
                                         "item_title": "Watchlist", "item_price": "", "item_url": "", "item_name": ""})
                    _outbox_enqueue(con, day_id, reminder)
                    process_outbox(con, verbose=verbose, item_ids=[day_id], attempted=attempted_deliveries, deadline=deadline)
                    watchlist_sent_this_run = True
                myludo = MYLUDO_EXACT.get(name, get_myludo_url(name))
                price_fees = format_price_with_fees(it)
                msg_md = f"🎲 *{title}*\n💰 {price_fees}\n🔗 Lien Vinted: {link}\n📖 MyLudo: [{name}]({myludo})\n📦 _{name}_"
                msg_plain = f"{title} — {price_fees}\nLien Vinted: {link}\nMyLudo: {myludo}"
                # WhatsApp : texte court (pas de markdown, pas d'image)
                msg_wa = f"🎲 {title}\n💰 {price_fees}\nLien Vinted: {link}\nMyLudo: {myludo}"

                sent = False
                deliveries = _configured_deliveries(iid, title, price, link, img, name, msg_md, msg_plain, msg_wa,
                                                    telegram_token, telegram_chat, discord_webhook,
                                                    whatsapp_phone, whatsapp_apikey, ntfy_topic)
                if deliveries:
                    for delivery in deliveries.values():
                        delivery.update({"item_title": title, "item_price": price, "item_url": link, "item_name": name})
                    _outbox_enqueue(con, iid, deliveries)
                    completed = process_outbox(con, verbose=verbose, item_ids=[str(iid)], attempted=attempted_deliveries, deadline=deadline)
                    sent = str(iid) in completed
                else:
                    if os.getenv("GITHUB_ACTIONS"):
                        raise ScanFetchError("aucun canal de notification configuré en CI")
                    sent = notify_macos(f"{name} — {price}", title, url=link)
                    if sent and iid:
                        mark_seen(con, iid, title, price, link)
                        append_history(f"ALERT | {iid} | {title[:80]} | {price} | {link} | {name}", verbose=verbose)
                if not sent and verbose:
                    print(f"[outbox] {iid}: livraison incomplète, conservée pour réessai")
                all_new.append(it)
            else:
                # dry-run, ne marque pas comme vu
                pass

        # En dry-run, on ne marque rien. En vrai run, déjà marqué ci-dessus
        if args.once and args.limit and not args.force_notify:
            print(f"\n[ dry-run ] {len(display_items)} annonces affichées (non marquées comme vues, pas de notif)")

    pending = con.execute("SELECT COUNT(*) FROM delivery_outbox WHERE status!='sent'").fetchone()[0]
    succeeded = fetch_attempted - fetch_failed
    print(f"[bilan] recherches={fetch_attempted} réussies={succeeded} "
          f"échouées={fetch_failed} nouveautés={len(all_new)} envois_en_attente={pending}")
    if not dry_run:
        set_meta(con, "scan_resume", f"{datetime.now(ZoneInfo('UTC')).isoformat()}|0")
    # Politique anti-runs-rouges (outage Vinted 08/09/2026 : 12/14 en 500) :
    # un échec partiel reste un scan utile (alertes des recherches réussies
    # déjà envoyées). Seul un échec TOTAL est fatal ; le partiel sera
    # rattrapé au prochain run (toutes les 5 min). Le watchdog (45 min sans
    # succès) reste le vrai signal de panne prolongée.
    if not fetch_attempted:
        raise ScanFetchError("aucune recherche tentée")
    if succeeded <= 0:
        raise ScanFetchError(f"recherches échouées: {fetch_failed}/{fetch_attempted}")
    if fetch_failed:
        print(f"[warn] {fetch_failed}/{fetch_attempted} recherches échouées (transient Vinted probable) — "
              f"run vert, rattrapage au prochain scan")
    if pending and not dry_run:
        raise ScanFetchError(f"{pending} livraison(s) toujours en attente")
    if not dry_run:
        set_meta(con, "last_successful_scan_at", datetime.now(ZoneInfo("UTC")).isoformat())
    return all_new

def main():
    parser = argparse.ArgumentParser(description="Monitor Vinted Next Station Paris")
    parser.add_argument("--once", action="store_true", help="Un seul check puis exit")
    parser.add_argument("--limit", type=int, default=None, help="Limite d'annonces à afficher (mode debug, pas de notif ni de marquage)")
    parser.add_argument("--verbose", action="store_true", help="Logs détaillés")
    parser.add_argument("--force-notify", action="store_true", help="Force l'envoi de notifs même en mode --limit")
    parser.add_argument("--no-llm", action="store_true", help="Désactive le filtre LLM vision (Qwen) — debug / économie")
    parser.add_argument("--once-no-notify", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit doit être strictement positif")
    # Debug switches always run once and can never enter the notifying daemon.
    args.once = args.once or args.limit is not None or args.once_no_notify

    cfg = load_config()
    settings = cfg.get("settings", {})
    db_path = Path(__file__).parent / settings.get("database", "seen.db")
    poll_interval = settings.get("poll_interval", 60)

    dry_run = args.once_no_notify or (args.limit is not None and not args.force_notify)
    con = init_db(":memory:" if dry_run else db_path)
    print(f"📦 DB: {db_path} | interval: {poll_interval}s")
    print(f"🔧 Config: {CONFIG_PATH}")

    # Vérifie notifiers
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    whatsapp_phone = os.getenv("WHATSAPP_PHONE", "")
    whatsapp_apikey = os.getenv("WHATSAPP_APIKEY", "")
    ntfy_topic = os.getenv("NTFY_TOPIC", "")
    if telegram_token and telegram_chat:
        n = len([c for c in str(telegram_chat).split(",") if c.strip()])
        print(f"🔔 Notif: Telegram activé ({n} destinataire(s))")
    if discord_webhook:
        print("🔔 Notif: Discord activé")
    if whatsapp_phone and whatsapp_apikey:
        print(f"🔔 Notif: WhatsApp activé ({whatsapp_phone})")
    if ntfy_topic:
        print(f"🔔 Notif: ntfy.sh/{ntfy_topic} activé")
    if not any([telegram_token and telegram_chat, discord_webhook, whatsapp_phone and whatsapp_apikey, ntfy_topic]):
        print("🔔 Notif: macOS locale (aucun webhook configuré) — configure .env")

    if args.once:
        try:
            check_once(cfg, con, args)
        except ScanBudgetExceeded as exc:
            # Le curseur scan_resume est déjà persisté : le prochain run
            # reprend où celui-ci s'est arrêté. Ce n'est pas un échec.
            print(f"[warn] budget dépassé, reprise au prochain scan: {exc}")
            con.close()
            return 0
        except ScanFetchError as exc:
            print(f"[ERR] scan inutilisable: {exc}", file=sys.stderr)
            con.close()
            return 2
        con.close()
        return 0

    # Vérifie LLM
    if os.getenv("OPENROUTER_API_KEY"):
        llm_model = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash")
        print(f"🤖 LLM vision: {llm_model} (Qwen Flash) activé — filtre anti faux positifs")
    else:
        print("🤖 LLM vision: désactivé (pas de OPENROUTER_API_KEY) — fail-open")

    # Boucle infinie
    print("\n▶️ Surveillance en cours... Ctrl+C pour arrêter\n")
    try:
        while True:
            try:
                check_once(cfg, con, argparse.Namespace(once=False, limit=None, verbose=args.verbose, force_notify=False, once_no_notify=False, no_llm=args.no_llm))
            except ScanBudgetExceeded as exc:
                print(f"[warn] budget dépassé, reprise au prochain passage: {exc}")
            except ScanFetchError as exc:
                print(f"[ERR] scan inutilisable: {exc}", file=sys.stderr)
            print(f"\n⏳ Prochain check dans {poll_interval}s — {datetime.now().strftime('%H:%M:%S')}")
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n⏹️ Arrêté.")
    finally:
        con.close()

if __name__ == "__main__":
    sys.exit(main())
