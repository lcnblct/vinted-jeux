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

import yaml
import requests
from dotenv import load_dotenv

# Charge .env si présent
load_dotenv()

CONFIG_PATH = Path(__file__).parent / "config.yaml"
DB_DEFAULT = Path(__file__).parent / "seen.db"

# Cache pays vendeur pour filtre FR
_user_country_cache: dict = {}
_french_scraper = None  # lazy init

# ── Helpers notifs ──────────────────────────────────────────────

def notify_telegram(token: str, chat_id: str, text: str, photo_url: str = None):
    """Supporte 1 ou plusieurs chat_id séparés par virgule (dev + destinataire)."""
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
    con.commit()
    return con

def is_seen(con, item_id: str) -> bool:
    cur = con.execute("SELECT 1 FROM seen WHERE id=?", (str(item_id),))
    return cur.fetchone() is not None

def mark_seen(con, item_id: str, title: str, price: str, url: str):
    con.execute("INSERT OR IGNORE INTO seen (id, title, price, url) VALUES (?,?,?,?)",
                (str(item_id), title, price, url))
    con.commit()

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
    """
    global _french_scraper, _user_country_cache
    uid = get_user_id(item)
    if not uid:
        return None
    if uid in _user_country_cache:
        return _user_country_cache[uid]
    try:
        if _french_scraper is None:
            from vinted_scraper import VintedScraper
            _french_scraper = VintedScraper("https://www.vinted.fr")
        data = _french_scraper.curl(f"/api/v2/users/{uid}")
        # VintedJsonModel -> json_data
        jd = getattr(data, "json_data", data) if hasattr(data, "json_data") else data
        if isinstance(jd, dict):
            user = jd.get("user", {})
            cc = user.get("country_code") or user.get("country_iso_code") or user.get("iso_country_code")
            if cc:
                _user_country_cache[uid] = cc
                if verbose:
                    print(f"[fr] vendeur {uid} -> {cc}")
                # petite pause anti rate-limit
                time.sleep(0.12)
                return cc
    except Exception as e:
        if verbose:
            print(f"[fr] erreur pays vendeur {uid}: {e}")
    # cache négatif temporaire pour éviter re-requête
    _user_country_cache[uid] = None
    time.sleep(0.08)
    return None

def filter_french_items(items, verbose=False):
    """Garde uniquement les annonces de vendeurs FR (annonce en français)."""
    out = []
    for it in items:
        cc = get_user_country(it, verbose=verbose)
        if cc is None:
            # si on ne peut pas déterminer, on garde par défaut (évite de perdre des annonces)
            if verbose:
                print(f"[fr] pays inconnu, on garde: {get_item_title(it)[:60]}")
            out.append(it)
        elif cc.upper() == "FR":
            out.append(it)
        else:
            if verbose:
                print(f"[fr] exclu non-FR ({cc}): {get_item_title(it)[:60]}")
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

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat = os.getenv("TELEGRAM_CHAT_ID", "")  # peut être "id1,id2" (dev,destinataire)
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    whatsapp_phone = os.getenv("WHATSAPP_PHONE", "")
    whatsapp_apikey = os.getenv("WHATSAPP_APIKEY", "")
    ntfy_topic = os.getenv("NTFY_TOPIC", "")

    has_notifier = bool(telegram_token and telegram_chat) or bool(discord_webhook) or bool(whatsapp_phone and whatsapp_apikey) or bool(ntfy_topic)
    verbose = args.verbose

    all_new = []

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

        # filtres prix / mots-clés
        items = apply_filters(items, filters, q, verbose=verbose)
        # filtre annonce en français (vendeur FR) si activé
        only_french = filters.get("only_french") or settings.get("only_french") or q.get("only_french")
        if only_french:
            before = len(items)
            items = filter_french_items(items, verbose=verbose)
            if verbose and len(items) != before:
                print(f"[fr] {before} → {len(items)} après filtre FR")

        if args.limit and not args.once_no_notify:
            # mode debug : affiche sans filtrer seen, sans notifier
            pass
        else:
            # filtre anti-doublons
            filtered = []
            for it in items:
                iid = get_item_id(it)
                if iid and is_seen(con, iid):
                    if verbose:
                        print(f"[seen] déjà vu {iid}: {get_item_title(it)[:60]}")
                    continue
                filtered.append(it)
            items = filtered

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
            if not args.once:
                should_notify = True  # en loop, on notifie toujours

            if should_notify:
                msg_md = f"🎲 *{title}*\n💰 {price}\n🔗 {link}\n📦 _{name}_"
                msg_plain = f"{title} — {price}\n{link}"
                # WhatsApp : texte court (pas de markdown, pas d'image)
                msg_wa = f"🎲 {title}\n💰 {price}\n🔗 {link}"

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

    # Boucle infinie
    print("\n▶️ Surveillance en cours... Ctrl+C pour arrêter\n")
    try:
        while True:
            check_once(cfg, con, argparse.Namespace(once=False, limit=None, verbose=args.verbose, force_notify=False, once_no_notify=False))
            print(f"\n⏳ Prochain check dans {poll_interval}s — {datetime.now().strftime('%H:%M:%S')}")
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n⏹️ Arrêté.")
    finally:
        con.close()

if __name__ == "__main__":
    main()
