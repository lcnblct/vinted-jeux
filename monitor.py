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
    "Crystalla": "https://www.myludo.fr/#!/game/crystalla-83958",
    "IQ Planètes": "https://www.myludo.fr/#!/game/iq-planetes-91242",
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
    "Calico": "https://www.myludo.fr/#!/game/calico-42460",
}

# ── Helpers notifs ──────────────────────────────────────────────

def notify_telegram(token: str, chat_id: str, text: str, photo_url: str = None):
    """Supporte 1 ou plusieurs chat_id séparés par virgule."""
    if not token or not chat_id:
        return False
    ids = [c.strip() for c in str(chat_id).split(",") if c.strip()]
    ok_any = False
    for cid in ids:
        try:
            if photo_url:
                url = f"https://api.telegram.org/bot{token}/sendPhoto"
                data = {"chat_id": cid, "caption": text, "photo": photo_url, "parse_mode": "Markdown"}
            else:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = {"chat_id": cid, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False}
            r = requests.post(url, data=data, timeout=15)
            if r.status_code != 200:
                print(f"[telegram] Erreur {r.status_code} pour {cid}: {r.text[:300]}")
            else:
                ok_any = True
        except Exception as e:
            print(f"[telegram] Exception pour {cid}: {e}")
    return ok_any

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
        if r.status_code == 200 and "Message queued" in r.text or "sent" in r.text.lower():
            print(f"[whatsapp] ✅ envoyé à {phone}")
            return True
        # même si le texte de réponse varie, 200 = souvent OK
        if r.status_code == 200:
            print(f"[whatsapp] réponse: {r.text[:200]}")
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
        safe_title = title.replace('"', "'")[:80]
        safe_msg = message.replace('"', "'")[:200]
        script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "Ping"'
        os.system(f"osascript -e '{script}' 2>/dev/null")
        print(f"[macos] 🔔 {title} — {message}")
        if url:
            print(f"       🔗 {url}")
    except Exception as e:
        print(f"[macos] {e}")

def format_price(item) -> str:
    try:
        price = item.price if hasattr(item, 'price') else item.get('price', '?')
        currency = item.currency if hasattr(item, 'currency') else item.get('currency', 'EUR')
        # certains wrappers renvoient price en string "12.00"
        return f"{price} {currency}" if price else "prix ?"
    except:
        return "?"

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
    con.commit()
    return con

def is_seen(con, item_id: str) -> bool:
    cur = con.execute("SELECT 1 FROM seen WHERE id=?", (str(item_id),))
    return cur.fetchone() is not None

def mark_seen(con, item_id: str, title: str, price: str, url: str):
    con.execute("INSERT OR IGNORE INTO seen (id, title, price, url) VALUES (?,?,?,?)",
                (str(item_id), title, price, url))
    con.commit()

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
    if hasattr(item, "id"):
        return str(item.id)
    if isinstance(item, dict) and "id" in item:
        return str(item["id"])
    return None

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

def get_user_country(item, verbose=False) -> str:
    """Retourne country_code (FR, IT, DE...) du vendeur, avec cache.
    Utilise Vinted API /api/v2/users/{id}. Retourne None si indisponible.
    Gère 429 avec retry.
    """
    global _french_scraper, _user_country_cache
    uid = get_user_id(item)
    if not uid:
        return None
    if uid in _user_country_cache:
        return _user_country_cache[uid]
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
    _user_country_cache[uid] = None
    time.sleep(0.1)
    return None

def filter_french_items(items, verbose=False):
    """Garde uniquement les annonces de vendeurs FR (annonce en français).
    Si pays inconnu (429 persistant), on exclut par défaut pour éviter les faux positifs IT/EN."""
    out = []
    for it in items:
        cc = get_user_country(it, verbose=verbose)
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

def fetch_items(query_url: str, per_page: int = 20, verbose: bool = False):
    """
    Utilise vinted_scraper (synchrone, gère cookies Cloudflare)
    Fallback: requête directe si lib absente
    """
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

    scraper = VintedScraper(base_url, params=params) if False else None
    # API vinted_scraper: VintedScraper("https://www.vinted.fr") puis .search(params)
    # On instancie avec base_url uniquement
    try:
        scraper = VintedScraper(base_url)
        # search prend un dict de params
        items = scraper.search(params)
        if verbose:
            print(f"[fetch] {len(items)} items reçus via vinted_scraper")
        return items[:per_page]
    except Exception as e:
        if verbose:
            print(f"[fetch] Erreur vinted_scraper: {e}")
            import traceback; traceback.print_exc()
        # fallback: essaie avec VintedScraper(base_url, cookie="auto") déjà géré
        raise

def apply_filters(items, filters: dict, query_cfg: dict, verbose=False):
    """Filtres locaux optionnels (prix, mots-clés)"""
    out = []
    price_max = query_cfg.get("price_max") or filters.get("price_max_global")
    price_min = query_cfg.get("price_min")
    # supporte must_contain au niveau query OU global
    must_contain = query_cfg.get("must_contain") or filters.get("must_contain") or []
    must_not_contain = query_cfg.get("must_not_contain") or filters.get("must_not_contain") or []

    for it in items:
        title = get_item_title(it).lower()
        # must_contain
        if must_contain and not all(kw.lower() in title for kw in must_contain):
            if verbose: print(f"[filter] exclu (must_contain): {title}")
            continue
        if must_not_contain and any(kw.lower() in title for kw in must_not_contain):
            if verbose: print(f"[filter] exclu (must_not_contain): {title}")
            continue
        # prix
        try:
            raw_price = it.price if hasattr(it, 'price') else (it.get('price') if isinstance(it, dict) else None)
            if raw_price is not None:
                p = float(str(raw_price).replace(",", ".").replace("€","").strip())
                if price_max is not None and p > float(price_max):
                    if verbose: print(f"[filter] exclu prix {p} > {price_max}: {title}")
                    continue
                if price_min is not None and p < float(price_min):
                    if verbose: print(f"[filter] exclu prix {p} < {price_min}: {title}")
                    continue
        except:
            pass
        out.append(it)
    return out

# ── Main ────────────────────────────────────────────────────────

def load_config():
    if not CONFIG_PATH.exists():
        print(f"[!] {CONFIG_PATH} introuvable, utilisation config par défaut")
        return {
            "queries": [{"name": "Next Station Paris", "url": "https://www.vinted.fr/catalog?search_text=next%20station%20paris&order=newest_first"}],
            "settings": {"poll_interval": 60, "per_page": 20, "database": "seen.db"},
            "filters": {}
        }
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg

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

    all_new = []
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

    for q in queries:
        name = q.get("name", "Recherche Vinted")
        url = q.get("url", "")
        if not url:
            continue
        print(f"\n{'='*60}\n🔍 [{name}] {url}\n{'='*60}")
        try:
            items = fetch_items(url, per_page=per_page, verbose=verbose)
        except Exception as e:
            print(f"[ERR] fetch failed pour {name}: {e}")
            continue

        # filtres prix / mots-clés (local, gratuit)
        items = apply_filters(items, filters, q, verbose=verbose)
        # anti-doublons TÔT (local, avant les appels API FR coûteux) — sauf dry-run
        if args.limit and not args.once_no_notify:
            # mode debug : affiche sans filtrer seen, sans notifier
            pass
        else:
            before = len(items)
            kept = []
            for it in items:
                iid = get_item_id(it)
                if iid and is_seen(con, iid):
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
        only_french = filters.get("only_french") or settings.get("only_french") or q.get("only_french")
        if only_french:
            before = len(items)
            items = filter_french_items(items, verbose=verbose)
            if verbose and len(items) != before:
                print(f"[fr] {before} → {len(items)} après filtre FR")

        if not items:
            print("  → Aucune nouvelle annonce.")
            continue

        # limite d'affichage en mode --limit
        display_items = items[:args.limit] if args.limit else items

        for it in display_items:
            title = get_item_title(it)
            price = format_price(it)
            link = get_item_url(it)
            img = get_item_image(it)
            iid = get_item_id(it) or link

            print(f"  🎲 {title}\n     💰 {price}\n     🔗 {link}")
            if img and verbose:
                print(f"     🖼️ {img}")

            # Notifications seulement si --once sans --limit debug ou en mode daemon
            should_notify = not args.limit or not args.once  # si --limit alone on est en debug, on ne notifie pas
            # mais si --once sans --limit, on notifie
            # Si args.once et args.limit, on considère que c'est un test dry-run -> pas de notif sauf --force-notify
            if args.once and args.limit and not args.force_notify:
                should_notify = False
            if args.force_notify:
                should_notify = True
            if not args.once:
                should_notify = True  # en loop, on notifie toujours

            # ── Filtre LLM vision (Qwen 3.7 Flash) + réf MyLudo ───────────────
            if should_notify and llm_enabled:
                try:
                    desc = enrich_item_description(it, verbose=verbose)
                    photos = get_item_photos(it)
                    # Référence visuelle MyLudo (boîte officielle) pour comparaison A vs B.
                    # llm_filter résout l'image via game_name seul (dict statique 17 jeux),
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
                    )
                    if not is_true:
                        if conf >= llm_threshold:
                            print(f"  [llm] ✂️ exclu faux positif ({conf:.2f}): {reason}")
                            if verbose and photos:
                                print(f"       🖼️ {photos[0][:90]}...")
                            # marque comme vu pour ne pas re-payer le LLM au prochain run
                            if iid:
                                mark_seen(con, iid, title, price, link)
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
                # Envoi rappel watchlist avant la première alerte — 1×/jour max (premier run avec nouveauté)
                if should_send_watchlist_today and not watchlist_sent_this_run and telegram_token and telegram_chat:
                    try:
                        txt = get_watchlist_text()
                        if notify_telegram(telegram_token, telegram_chat, txt):
                            watchlist_sent_this_run = True
                            should_send_watchlist_today = False  # évite 2e envoi dans même run si plusieurs jeux
                            set_meta(con, "last_watchlist_date", paris_today)
                            append_history(f"WATCHLIST | - | Watchlist {len(queries)} jeux | - | - | {paris_today}", verbose=verbose)
                            if verbose:
                                print(f"[watchlist] ✅ envoyée et marquée {paris_today}")
                        else:
                            if verbose:
                                print(f"[watchlist] ❌ échec envoi Telegram (pas de marquage)")
                        time.sleep(0.4)
                    except Exception as e:
                        if verbose:
                            print(f"[watchlist] err {e}")
                myludo = MYLUDO_EXACT.get(name, get_myludo_url(name))
                msg_md = f"🎲 *{title}*\n💰 {price}\n🔗 Lien Vinted: {link}\n📖 MyLudo: [{name}]({myludo})\n📦 _{name}_"
                msg_plain = f"{title} — {price}\nLien Vinted: {link}\nMyLudo: {myludo}"
                # WhatsApp : texte court (pas de markdown, pas d'image)
                msg_wa = f"🎲 {title}\n💰 {price}\nLien Vinted: {link}\nMyLudo: {myludo}"

                sent = False
                if telegram_token and telegram_chat:
                    sent = notify_telegram(telegram_token, telegram_chat, msg_md, photo_url=img) or sent
                if discord_webhook:
                    sent = notify_discord(discord_webhook, msg_plain, title=title, url=link, image=img) or sent
                if whatsapp_phone and whatsapp_apikey:
                    sent = notify_whatsapp(whatsapp_phone, whatsapp_apikey, msg_wa) or sent
                if ntfy_topic:
                    sent = notify_ntfy(ntfy_topic, msg_plain, title=f"{name} — {price}") or sent
                if not has_notifier:
                    notify_macos(f"{name} — {price}", title, url=link)

                # Marque comme vu même si notif échouée (pour éviter spam), sauf si Telegram/Discord échoue et on veut retry
                # On marque toujours
                if iid:
                    mark_seen(con, iid, title, price, link)
                # Historique factuel de ce qui a été envoyé (anti-doublon audit) — seulement si au moins un notifier a répondu ok
                if sent:
                    append_history(f"ALERT | {iid} | {title[:80]} | {price} | {link} | {name}", verbose=verbose)
                elif has_notifier and verbose:
                    print(f"[history] ⚠️ notif échouée pour {iid}, pas de log history")
                all_new.append(it)
            else:
                # dry-run, ne marque pas comme vu
                pass

        # En dry-run, on ne marque rien. En vrai run, déjà marqué ci-dessus
        if args.once and args.limit and not args.force_notify:
            print(f"\n[ dry-run ] {len(display_items)} annonces affichées (non marquées comme vues, pas de notif)")

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

    cfg = load_config()
    settings = cfg.get("settings", {})
    db_path = Path(__file__).parent / settings.get("database", "seen.db")
    poll_interval = settings.get("poll_interval", 60)

    con = init_db(db_path)
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
        check_once(cfg, con, args)
        con.close()
        return

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
            check_once(cfg, con, argparse.Namespace(once=False, limit=None, verbose=args.verbose, force_notify=False, once_no_notify=False, no_llm=args.no_llm))
            print(f"\n⏳ Prochain check dans {poll_interval}s — {datetime.now().strftime('%H:%M:%S')}")
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n⏹️ Arrêté.")
    finally:
        con.close()

if __name__ == "__main__":
    main()
