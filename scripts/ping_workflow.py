#!/usr/bin/env python3
"""Pinger externe — déclenche le workflow GitHub quand le scheduler cron saute des runs.

Pourquoi : le cron GitHub (`schedule`) est best-effort. Sur ce repo on observe
~5 runs/jour au lieu des 32 attendus (ex: 10:33 → 14:05 → 17:50 le 03/09).
Un cron externe (cron-job.org, gratuit) qui appelle l'API GitHub toutes les
~25 min garantit les runs.

Usage local (test) :
    export GH_PINGER_TOKEN="github_pat_..."   # PAT avec Actions: Read+Write (ou classic: repo + workflow)
    python scripts/ping_workflow.py
    python scripts/ping_workflow.py --method repository_dispatch

Setup cron-job.org (recommandé, gratuit) :
    1. GitHub → Settings → Developer settings → Personal access tokens →
       Fine-grained token : repo = vinted-jeux uniquement,
       permissions Repository → Actions : Read and write, Contents : Read.
       Copie le token (ne l'expire jamais ou 1 an + rappel).
    2. cron-job.org → Create cronjob :
       - URL (workflow_dispatch) :
         https://api.github.com/repos/lcnblct/vinted-jeux/actions/workflows/vinted-monitor.yml/dispatches
        - Schedule : custom `*/15 * * * *` (toutes les 15min, 24/7)
       - Request method : POST
       - Headers : Accept: application/vnd.github+json
                   Authorization: Bearer TON_PAT
                   X-GitHub-Api-Version: 2022-11-28
       - Body (JSON) : {"ref":"main"}
       Alternative (repository_dispatch) :
         URL : https://api.github.com/repos/lcnblct/vinted-jeux/dispatches
         Body : {"event_type":"ping"}
    3. Vérifie : onglet Actions → le run apparaît avec badge
       "workflow_dispatch" (ou "repository_dispatch").

Équivalent curl :
    curl -X POST \\
      -H "Accept: application/vnd.github+json" \\
      -H "Authorization: Bearer $GH_PINGER_TOKEN" \\
      -H "X-GitHub-Api-Version: 2022-11-28" \\
      https://api.github.com/repos/lcnblct/vinted-jeux/actions/workflows/vinted-monitor.yml/dispatches \\
      -d '{"ref":"main"}'
"""
import argparse
import json
import os
import sys
import urllib.request

REPO = os.getenv("GITHUB_REPO", "lcnblct/vinted-jeux")
WORKFLOW_FILE = os.getenv("GITHUB_WORKFLOW", "vinted-monitor.yml")
TOKEN = os.getenv("GH_PINGER_TOKEN") or os.getenv("GITHUB_TOKEN", "")


def _post(url: str, payload: dict) -> tuple[int, str]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode()[:300]
    except Exception as e:
        # urllib lève HTTPError avec .code/.read() pour les 4xx/5xx
        code = getattr(getattr(e, "fp", None), "status", None) or getattr(e, "code", "?")
        try:
            body = e.read().decode()[:300]  # type: ignore
        except Exception:
            body = str(e)[:300]
        return int(code) if str(code).isdigit() else 0, body


def main() -> int:
    ap = argparse.ArgumentParser(description="Ping externe du workflow Vinted")
    ap.add_argument(
        "--method",
        choices=["workflow_dispatch", "repository_dispatch"],
        default="workflow_dispatch",
        help="workflow_dispatch (défaut) ou repository_dispatch (event ping)",
    )
    args = ap.parse_args()

    if not TOKEN:
        print("GH_PINGER_TOKEN manquant (export GH_PINGER_TOKEN=github_pat_...)")
        return 1

    if args.method == "workflow_dispatch":
        url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
        status, body = _post(url, {"ref": "main"})
    else:
        url = f"https://api.github.com/repos/{REPO}/dispatches"
        status, body = _post(url, {"event_type": "ping"})

    if status in (201, 204):
        print(f"OK ping envoyé ({args.method}) → vérifie l'onglet Actions.")
        return 0
    print(f"Échec {status}: {body}")
    print("Vérifie : token valide ? scope Actions:write ? repo/workflow corrects ?")
    return 1


if __name__ == "__main__":
    sys.exit(main())
