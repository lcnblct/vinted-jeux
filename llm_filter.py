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


def _build_prompt(game_name: str, title: str, description: str, price: str) -> str:
    desc_snippet = (description.strip()[:600] + "…") if description and len(description) > 600 else (description.strip() if description else "(pas de description)")
    return f"""Tu es un expert jeux de société, rigoureux anti faux positifs.

On recherche le jeu "{game_name}" (vrai jeu de société BOÎTE COMPLÈTE, VF jouable, même d'occasion mais complet).

Annonce Vinted à vérifier:
- Jeu recherché: {game_name}
- Titre: {title}
- Description: {desc_snippet}
- Prix: {price}

14 jeux distincts — ne confonds pas: Cascadia (base) ≠ Cascadia Rolling Hills ≠ Cascadia Rolling Rivers | Next Station Paris ≠ London | Windmill Valley, Take It Easy!, Rebirth, Patchwork, L'Ile Des Chats, Koi, Frosted Blooms, Cortex Challenge, Calico.

Faux positif = PAS le jeu complet. Exclus si:
- accessoire/upgrade: lot jetons/pommes pin/tulipes/meeples, insert, porte-cartes, sleeves, tapis, pièce 3D, jeton promo (même si "{game_name}" dans titre et boîte visible en fond)
- pièce détachée, règle seule, boîte vide
- vêtement/chaussure même mot (Cascadia Brooks, Patchwork tissu)
- autre jeu homonyme ou jeu vidéo même mot (pour "Rebirth": Final Fantasy/Jurassic/Suicide/Black Rose Rebirth = FAUX; seul "Rebirth" board game Knizia est VRAI)
- mauvais variant: on cherche "{game_name}" mais annonce est un autre jeu de la liste → FAUX
- lot multi-jeux où "{game_name}" n'est qu'un détail
- photo montre autre produit

Vrai = photo montre BOÎTE de "{game_name}" et titre/description confirment vente du jeu jouable complet (neuf scellé "sigillato" ou occasion complet). Langue boîte FR/EN/DE/IT OK (vendeur déjà FR).

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
) -> Tuple[bool, str, float, dict]:
    """
    Retourne (is_true, reason, confidence, raw_json).
    Fail-open si pas de clé ou erreur.
    """
    if not game_name or not title:
        return True, "param missing, fail-open", 0.0, {}

    cache_key = f"{game_name}|{title}|{price}|{','.join((image_urls or [])[:1])}"
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
    prompt = _build_prompt(game_name, title, description or "", price or "")

    # Prépare images
    content_parts = [{"type": "text", "text": prompt}]
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

    # Si aucune image n'a pu être fetch, on reste en text-only (le modèle reste efficace, mais moins)
    if fetched == 0 and verbose:
        print(f"[llm] aucune image fetch pour {title[:50]} → text-only")

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
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()
    ok, reason, conf, raw = is_true_positive(args.game, args.title, args.desc, args.price, args.image, verbose=True)
    print(json.dumps({"is_true_game": ok, "reason": reason, "confidence": conf, "raw": raw}, ensure_ascii=False, indent=2))
