#!/usr/bin/env python3
"""
LLM Vision Filter — anti faux positifs Vinted
Utilise OpenRouter + Qwen 3.7 Flash (text+image+video->text) pour vérifier
si une annonce correspond bien au VRAI jeu de société complet, et pas à
un accessoire, upgrade, insert, pièce 3D, promo, règle seule, vêtement,
jeu vidéo homonyme, etc.

Env:
  OPENROUTER_API_KEY=sk-or-v1-...
  OPENROUTER_MODEL=qwen/qwen3.7-flash (default)
  OPENROUTER_BASE_URL=https://openrouter.ai/api/v1 (default)

Usage:
  from llm_filter import is_true_positive
  ok, reason, conf, raw = is_true_positive(
      game_name="Cascadia",
      title="Lot de 25 Pommes de Pin 3D pour Cascadia",
      description="...",
      price="5.0 EUR",
      image_urls=["https://images1.vinted.net/..."],
      verbose=True
  )

Coût: qwen3.7-flash ~$0.00004 / appel vision (800 prompt + 120 completion)
Fail-open: si clé absente ou erreur API → retourne True (on notifie) pour ne pas rater un vrai bon plan.
"""
import base64
import json
import os
import time
from typing import List, Tuple, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "qwen/qwen3.7-flash").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()

# Cache simple par (game_name, title, price) pour éviter de repayer 2x même annonce dans le même run
_cache: dict = {}

# ── Images de référence MyLudo (boîtes officielles) ──────────────────
# Récupérées via https://www.myludo.fr/?_escaped_fragment_=/game/<slug>
# (og:image). Vérifiées le 2026-09-03 : toutes en 200 + téléchargeables.
# Note: "Patchwork 10e Anniversaire" pointe vers la fiche Patchwork de base
# (même gamme visuelle — le prompt tolère l'édition anniversaire).
MYLUDO_REF_IMAGES: dict = {
    "Windmill Valley": "https://www.myludo.fr/img/jeux/1735060102/jpg/cx/75718.jpg",
    "Take It Easy!": "https://www.myludo.fr/img/jeux/1764762820/jpg/cu/72302.jpg",
    "Rebirth": "https://www.myludo.fr/img/jeux/1783930291/jpg/di/86622.jpg",
    "Patchwork 10e Anniversaire": "https://www.myludo.fr/img/jeux/1758872438/300/au/20059.png",
    "Next Station Paris": "https://www.myludo.fr/img/jeux/1754809375/jpg/cw/74727.jpg",
    "Next Station London": "https://www.myludo.fr/img/jeux/1780144603/jpg/cd/55261.jpg",
    "L'Ile Des Chats": "https://www.myludo.fr/img/jeux/1768734484/300/bm/38772.png",
    "Koi": "https://www.myludo.fr/img/jeux/1785574691/jpg/dq/94495.jpg",
    "Frosted Blooms": "https://www.myludo.fr/img/jeux/1774641516/jpg/dn/91724.jpg",
    "Cascadia Rolling Rivers": "https://www.myludo.fr/img/jeux/1733808141/jpg/cv/73113.jpg",
    "Cascadia Rolling Hills": "https://www.myludo.fr/img/jeux/1733808179/jpg/cv/73114.jpg",
    "Cascadia": "https://www.myludo.fr/img/jeux/1776255418/jpg/bz/51951.jpg",
    "Calico": "https://www.myludo.fr/img/jeux/1786356544/jpg/bq/42460.jpg",
}

# Cache b64 des images de référence (1 entrée / jeu / run — évite de retélécharger)
_ref_b64_cache: dict = {}


def _resolve_reference_url(game_name: str, myludo_url: str = None, reference_image_url: str = None) -> Optional[str]:
    """Retourne l'URL de l'image de référence MyLudo pour ce jeu, ou None.

    Priorité: param explicite > dict statique (game_name) > fetch dynamique
    via myludo_url (og:image de ?_escaped_fragment_=...).
    """
    if reference_image_url and reference_image_url.startswith("http"):
        return reference_image_url
    if game_name and game_name in MYLUDO_REF_IMAGES:
        return MYLUDO_REF_IMAGES[game_name]
    # Fallback dynamique: extrait le slug depuis l'URL MyLudo et fetch og:image
    if myludo_url and "/game/" in myludo_url:
        try:
            import re as _re
            m = _re.search(r"/game/([a-z0-9\-]+)", myludo_url)
            if m:
                return _fetch_myludo_og_image(m.group(1))
        except Exception:
            return None
    return None


def _fetch_myludo_og_image(slug: str, timeout: int = 10) -> Optional[str]:
    """Fetch og:image MyLudo via le rendu SEO ?_escaped_fragment_. Retourne URL ou None."""
    try:
        import re as _re
        url = f"https://www.myludo.fr/?_escaped_fragment_=/game/{slug}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        if r.status_code != 200 or not r.text:
            return None
        m = _re.search(r'<meta property="og:image" content="([^"]+)"', r.text)
        if m and m.group(1).startswith("http"):
            return m.group(1)
    except Exception:
        pass
    return None


def _fetch_ref_b64(url: str, verbose: bool = False) -> Optional[Tuple[str, str]]:
    """Télécharge l'image de référence avec cache mémoire. Retourne (b64, mime) ou None."""
    if not url:
        return None
    if url in _ref_b64_cache:
        return _ref_b64_cache[url]
    # Referer MyLudo requis parfois (hotlink protection légère)
    res = None
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.myludo.fr/"})
        if r.status_code == 200 and r.content and len(r.content) >= 400:
            ctype = r.headers.get("content-type", "image/jpeg")
            if "webp" in ctype:
                mime = "image/webp"
            elif "png" in ctype:
                mime = "image/png"
            else:
                mime = "image/jpeg"
            b64 = base64.b64encode(r.content).decode("utf-8")
            if len(b64) <= 5_000_000:
                res = (b64, mime)
    except Exception as e:
        if verbose:
            print(f"[llm] ref fetch err {e}")
        res = None
    if res is None:
        # fallback sans Referer via helper générique
        res = _fetch_image_b64(url, verbose=verbose)
    if res is not None:
        _ref_b64_cache[url] = res
    elif verbose:
        print(f"[llm] référence illisible {url[:80]} → comparaison texte+annonce seule")
    return res


def _fetch_image_b64(url: str, timeout: int = 8, verbose: bool = False) -> Optional[Tuple[str, str]]:
    """Télécharge image et retourne (b64, mime). Retourne None si échec."""
    if not url or not url.startswith("http"):
        return None
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200 or not r.content or len(r.content) < 400:
            if verbose:
                print(f"[llm] image fetch {r.status_code} {url[:80]}")
            return None
        ctype = r.headers.get("content-type", "image/jpeg")
        # normalise mime
        if "webp" in ctype:
            mime = "image/webp"
        elif "png" in ctype:
            mime = "image/png"
        else:
            mime = "image/jpeg"
        b64 = base64.b64encode(r.content).decode("utf-8")
        # OpenRouter limite taille; garde < 5MB b64 (≈ 3.75MB image)
        if len(b64) > 5_000_000:
            if verbose:
                print(f"[llm] image trop grosse {len(b64)}")
            return None
        return b64, mime
    except Exception as e:
        if verbose:
            print(f"[llm] image fetch err {e}")
        return None


def _build_prompt(game_name: str, title: str, description: str, price: str, has_reference: bool = False) -> str:
    desc_snippet = (description.strip()[:600] + "…") if description and len(description) > 600 else (description.strip() if description else "(pas de description)")
    ref_block = """
IMAGES JOINTES:
- IMAGE A = boîte OFFICIELLE de référence du jeu recherché (source MyLudo, fiable).
- IMAGES B (1 à N) = photos de l'annonce Vinted à vérifier.

CONSIGNE VISUELLE PRIORITAIRE — compare A vs B:
- Mêmes illustration/titre/couleurs/charte graphique sur la boîte ? → bon signe.
- B montre un jeu VISUELLEMENT DIFFÉRENT de A (autre jeu, autre gamme, cartes traditionnelles vs boîte moderne, etc.) → FAUX (is_true_game=false), MÊME SI le titre contient le mot recherché.
  Ex: on cherche "Koi" (boîte moderne carpes koï) mais l'annonce "Jeu hanafuda koi koi" montre des cartes hanafuda japonaises → FAUX.
  Ex: on cherche "Cascadia" mais B montre "Cascadia Rolling Hills/Rivers" (titre différent sur la boîte) → FAUX.
- Tolère: angle/lumière/cellophane/boîte ouverte ou d'occasion, reflets, photo amateur — tant que c'est reconnaissablement la MÊME boîte/charte que A.
- Tolère: édition anniversaire / réédition même gamme (ex: Patchwork 10e Anniversaire vs Patchwork de base, même charte) → VRAI si visuel même famille.
- IMPORTANT photo catalogue/stock: si B est identique ou quasi-identique à A (visuel catalogue, image boutique), c'est une PREUVE que c'est le même jeu → VRAI (ne pénalise JAMAIS une photo stock/catalogue; ne suspecte aucune fraude sur ce seul motif). Seuls les autres critères (langue, accessoire, mauvais variant…) peuvent alors rendre FAUX.
- Si A absente/illisible: décide sur texte + B uniquement. Si B absente: décide sur titre/description, baisse confidence.
""" if has_reference else """
IMAGES JOINTES (si présentes): photos de l'annonce Vinted. Pas d'image de référence disponible — décide sur texte + photos.
"""
    return f"""Tu es un expert jeux de société, rigoureux anti faux positifs.

On recherche le jeu "{game_name}" (vrai jeu de société BOÎTE COMPLÈTE, VF jouable, même d'occasion mais complet).

Annonce Vinted à vérifier:
- Jeu recherché: {game_name}
- Titre: {title}
- Description: {desc_snippet}
- Prix: {price}
{ref_block}
 13 jeux distincts — ne confonds pas: Cascadia (base) ≠ Cascadia Rolling Hills ≠ Cascadia Rolling Rivers | Next Station Paris ≠ London | Windmill Valley, Take It Easy!, Rebirth, Patchwork, L'Ile Des Chats, Koi, Frosted Blooms, Calico.
 Attention homonymes: "Koi" (jeu de société moderne) ≠ "hanafuda koi-koi" (cartes traditionnelles japonaises) ≠ "C'koi" (jeu d'ambiance) ≠ carpe koï (manche à air, déco) → tout ça = FAUX pour "Koi".

Faux positif = PAS le jeu complet VF. Exclus si:
- version NON française: titre/description/photo montre clairement version étrangère (DE/EN/IT/NL/ES) comme "im herzen der natur", "DEU", "DE", "ENG", "gioco da tavolo", "NL", "sigillato", "italian", "deutsch", "english edition", boîte avec texte allemand/anglais/italien dominant → FAUX (on veut UNIQUEMENT VF francophone)
- accessoire/upgrade: lot jetons/pommes pin/tulipes/meeples, insert, porte-cartes, sleeves, tapis, pièce 3D, jeton promo (même si "{game_name}" dans titre et boîte visible en fond)
- pièce détachée, règle seule, boîte vide
- vêtement/chaussure même mot (Cascadia Brooks, Patchwork tissu)
- autre jeu homonyme ou jeu vidéo même mot (pour "Rebirth": Final Fantasy/Jurassic/Suicide/Black Rose Rebirth = FAUX; seul "Rebirth" board game Knizia est VRAI)
- mauvais variant: on cherche "{game_name}" mais annonce est un autre jeu de la liste → FAUX
- lot multi-jeux où "{game_name}" n'est qu'un détail
- photo montre autre produit
- COMPARAISON VISUELLE: B visuellement différent de la référence A → FAUX (prioritaire sur le texte)

Vrai = photo montre BOÎTE de "{game_name}" en VERSION FRANÇAISE (VF, Français, texte FR sur boîte/règles, description indique "VF" ou vendeur FR + boîte FR) ET titre/description confirment vente du jeu jouable complet (neuf scellé ou occasion complet). Si doute sur langue mais vendeur FR et boîte neutre, laisse passer.
RÈGLE LANGUE ÉQUITABLE (anti faux négatif): les visuels de boîtes sont souvent identiques dans toutes les langues (titre/illustration inchangés, seules les règles/le dos changent) et la référence MyLudo est parfois l'édition originale EN. Ne conclus JAMAIS à une version étrangère sur le seul texte (anglais) visible sur la boîte de RÉFÉRENCE A ou sur une photo catalogue/stock. Pour la langue, exige un indice CONCRET d'édition étrangère dans l'ANNONCE elle-même: titre/description en langue étrangère ("im herzen der natur", "ed. Inglese", "sigillato", description IT/DE…), drapeau/mention DE/EN/IT/NL sur les photos de B, ou vendeur non-FR. Une boîte B visuellement identique à A + mention explicite "VF"/"français" dans le titre ou la description = VRAI (même si le texte imprimé sur la boîte est en anglais).

Réponds STRICTEMENT JSON:
{{"is_true_game": true/false, "reason": "1 phrase", "confidence": 0.0-1.0}}
"""


def is_true_positive(
    game_name: str,
    title: str,
    description: str = "",
    price: str = "",
    image_urls: List[str] = None,
    verbose: bool = False,
    max_images: int = 2,
    reference_image_url: str = None,
    myludo_url: str = None,
) -> Tuple[bool, str, float, dict]:
    """
    Retourne (is_true, reason, confidence, raw_json).
    Fail-open si pas de clé ou erreur.

    Comparaison visuelle: IMAGE A (boîte officielle MyLudo résolue via
    game_name/myludo_url/reference_image_url) vs IMAGES B (annonce Vinted).
    Si la référence est indisponible, retombe sur texte + photos annonce.
    """
    if not game_name or not title:
        return True, "param missing, fail-open", 0.0, {}

    # v2: la clé inclut la réf (l'ancien cache sans réf est invalidé)
    ref_url = _resolve_reference_url(game_name, myludo_url, reference_image_url)
    cache_key = f"v2|{game_name}|{title}|{price}|{','.join((image_urls or [])[:1])}|{ref_url or ''}"
    if cache_key in _cache:
        return _cache[cache_key]

    # Fail-open si pas de clé — lecture dynamique (env peut être set après import)
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip() or OPENROUTER_API_KEY
    model = os.getenv("OPENROUTER_MODEL", "").strip() or OPENROUTER_MODEL
    base_url = os.getenv("OPENROUTER_BASE_URL", "").strip() or OPENROUTER_BASE_URL
    if not api_key:
        if verbose:
            print("[llm] pas de OPENROUTER_API_KEY → fail-open True")
        return True, "no api key", 1.0, {}
    if verbose:
        # debug masqué: longueur + prefix
        print(f"[llm] key {api_key[:8]}... len={len(api_key)} model={model}")
    # Résout la référence AVANT de builder le prompt (le prompt dépend de has_reference)
    ref_b64 = _fetch_ref_b64(ref_url, verbose=verbose) if ref_url else None
    has_ref = ref_b64 is not None
    prompt = _build_prompt(game_name, title, description or "", price or "", has_reference=has_ref)

    # Prépare images: IMAGE A (référence) d'abord, avec labels texte intercalés
    # pour que le modèle sache laquelle est laquelle.
    content_parts = [{"type": "text", "text": prompt}]
    if has_ref:
        b64, mime = ref_b64
        content_parts.append({"type": "text", "text": f"IMAGE A — boîte officielle de référence du jeu « {game_name} » (source MyLudo) :"})
        content_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"}
        })
        content_parts.append({"type": "text", "text": "IMAGES B — photos de l'annonce Vinted à vérifier (compare chaque B à A) :"})
    fetched = 0
    for url in (image_urls or [])[:max_images]:
        res = _fetch_image_b64(url, verbose=verbose)
        if res:
            b64, mime = res
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}
            })
            fetched += 1
        time.sleep(0.05)

    # Si aucune image annonce n'a pu être fetch, on reste en text-only + réf (le modèle reste efficace, mais moins)
    if fetched == 0 and verbose:
        print(f"[llm] aucune image annonce fetch pour {title[:50]} → {'ref-only + texte' if has_ref else 'text-only'}")
    elif verbose:
        print(f"[llm] ref={'OK' if has_ref else 'absente'} + {fetched} image(s) annonce pour {title[:50]}")

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": 400,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        # Désactive le reasoning imbriqué pour avoir le JSON direct dans content (sinon il part dans reasoning_details)
        "reasoning": {"enabled": False},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/lcnblct/vinted-jeux",
        "X-Title": "vinted-jeux llm_filter",
    }
    url = f"{base_url.rstrip('/')}/chat/completions"

    for attempt in range(2):
        try:
            t0 = time.time()
            r = requests.post(url, headers=headers, json=payload, timeout=45)
            dt = time.time() - t0
            if r.status_code != 200:
                if verbose:
                    print(f"[llm] {r.status_code} {r.text[:400]} (dt {dt:.1f}s)")
                # 429 / 5xx → retry
                if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                    time.sleep(1.2)
                    continue
                # fail-open
                return True, f"api {r.status_code}", 0.0, {"error": r.text[:500]}
            j = r.json()
            msg = j.get("choices", [{}])[0].get("message", {})
            content = msg.get("content") or ""
            # parfois le JSON est dans reasoning si enabled, mais on a disabled → sinon check reasoning
            if not content and "reasoning" in msg:
                content = msg.get("reasoning", "")
            # parfois content est déjà dict
            if isinstance(content, dict):
                parsed = content
            else:
                # content peut contenir ```json ``` → extrait
                c = content.strip()
                if c.startswith("```"):
                    c = c.split("```")[1]
                    if c.startswith("json"):
                        c = c[4:]
                    c = c.strip()
                try:
                    parsed = json.loads(c) if c else {}
                except:
                    # fallback: tente de trouver JSON dans le texte
                    import re
                    m = re.search(r"\{.*\}", c, re.DOTALL)
                    parsed = json.loads(m.group(0)) if m else {}

            is_true = parsed.get("is_true_game")
            # compat: certains modèles renvoient is_match / is_true
            if is_true is None:
                is_true = parsed.get("is_match", True)
            if isinstance(is_true, str):
                is_true = is_true.lower() in ("true", "oui", "yes")
            is_true = bool(is_true)
            reason = str(parsed.get("reason", ""))[:240]
            conf = float(parsed.get("confidence", 0.85)) if parsed.get("confidence") is not None else 0.85
            conf = max(0.0, min(1.0, conf))

            if verbose:
                print(f"[llm] {game_name} | {title[:55]} → {is_true} ({conf:.2f}) {reason} dt={dt:.1f}s")

            # log coût si présent
            usage = j.get("usage", {})
            if verbose and usage:
                print(f"[llm] usage {usage.get('prompt_tokens')}/{usage.get('completion_tokens')} cost {usage.get('cost')}")

            _cache[cache_key] = (is_true, reason, conf, parsed)
            return is_true, reason, conf, parsed

        except Exception as e:
            if verbose:
                print(f"[llm] exception {e}")
            if attempt == 0:
                time.sleep(0.6)
                continue
            return True, f"exception {e}", 0.0, {}

    return True, "retry exhausted", 0.0, {}


# Petit helper CLI pour test manuel
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--game", required=True, help="Nom du jeu recherché")
    p.add_argument("--title", required=True)
    p.add_argument("--desc", default="")
    p.add_argument("--price", default="")
    p.add_argument("--image", action="append", default=[])
    p.add_argument("--ref", default=None, help="URL image de référence (défaut: auto via MyLudo)")
    p.add_argument("--myludo", default=None, help="URL fiche MyLudo (fallback dynamique)")
    p.add_argument("--no-ref", action="store_true", help="Désactive la référence (test ablation)")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    _ref = None if args.no_ref else args.ref
    # --no-ref force l'absence de réf même si dict statique : on passe un game inconnu ? non —
    # on gère en vidant temporairement le dict pour ce game
    if args.no_ref and args.game in MYLUDO_REF_IMAGES:
        MYLUDO_REF_IMAGES.pop(args.game, None)
    ok, reason, conf, raw = is_true_positive(
        args.game, args.title, args.desc, args.price, args.image,
        verbose=True, reference_image_url=_ref, myludo_url=args.myludo,
    )
    print(json.dumps({"is_true_game": ok, "reason": reason, "confidence": conf, "raw": raw}, ensure_ascii=False, indent=2))
