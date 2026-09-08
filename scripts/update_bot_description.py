#!/usr/bin/env python3
"""Synchronise la description du bot Telegram avec ``config.yaml``."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable

import requests
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
ENV_PATH = PROJECT_ROOT / ".env"
TELEGRAM_API = "https://api.telegram.org"


def _load_token() -> str:
    # Le chemin explicite rend le script indépendant du répertoire courant.
    load_dotenv(ENV_PATH)
    return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _build_descriptions(queries: Iterable[dict[str, Any]]) -> tuple[str, str]:
    queries = list(queries)
    lines = [f"{q['name']} ≤{q['price_max']}€" for q in queries]
    desc = f"{len(queries)} jeux VF (FR) : {', '.join(lines)}"
    if len(desc) > 512:
        desc = desc[:509] + "..."
    short = f"{len(queries)} jeux: " + ", ".join(
        f"{q['name'].split()[0]}≤{q['price_max']}€" for q in queries[:4]
    ) + "..."
    if len(short) > 120:
        short = short[:117] + "..."
    return short, desc


def main() -> int:
    token = _load_token()
    if not token or ":" not in token:
        print("TELEGRAM_BOT_TOKEN manquant ou invalide", file=sys.stderr)
        return 1
    if not CONFIG_PATH.exists():
        print(f"config.yaml introuvable: {CONFIG_PATH}", file=sys.stderr)
        return 1
    try:
        with CONFIG_PATH.open(encoding="utf-8") as stream:
            cfg = yaml.safe_load(stream) or {}
        queries = cfg.get("queries", [])
        if not isinstance(queries, list):
            raise ValueError("queries doit être une liste")
        short, desc = _build_descriptions(queries)
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        print(f"configuration invalide: {exc}", file=sys.stderr)
        return 1

    print(f"Short ({len(short)}): {short}")
    print(f"Desc ({len(desc)}):\n{desc}\n")
    for endpoint, payload in (
        ("setMyShortDescription", {"short_description": short}),
        ("setMyDescription", {"description": desc}),
    ):
        try:
            response = requests.post(
                f"{TELEGRAM_API}/bot{token}/{endpoint}", data=payload, timeout=15
            )
            response.raise_for_status()
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"{endpoint}: échec HTTP/JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(result, dict) or result.get("ok") is not True:
            print(f"{endpoint}: API Telegram en échec: {result}", file=sys.stderr)
            return 1
        print(endpoint, True, result.get("result"), result.get("description", "")[:120])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
