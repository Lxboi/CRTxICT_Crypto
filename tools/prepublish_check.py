"""
tools/prepublish_check.py

Vérification locale AVANT tout `git push` vers un dépôt public.
Examine uniquement ce que Git s'apprête réellement à publier
(fichiers suivis + fichiers indexés), jamais le reste du disque.

Usage :
    python tools/prepublish_check.py

Code de sortie 0 = rien de suspect, 1 = au moins un problème à corriger.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# Fichiers qui ne doivent JAMAIS être suivis par Git.
FORBIDDEN_PATHS = [
    re.compile(r"(^|/)\.env$"),
    re.compile(r"(^|/)\.env\.(?!example$)[^/]+$"),
    re.compile(r"\.(db|sqlite3?|db-wal|db-shm|db-journal)$"),
    re.compile(r"(^|/)(logs|execution_state|ib_gateway_settings|\.venv|venv)/"),
    re.compile(r"\.(csv|log|pem|key|p12|pfx)$"),
    re.compile(r"(^|/)COMMANDES_RASPBERRY\.txt$"),
]

# Contenus suspects dans les fichiers texte suivis.
SUSPICIOUS_CONTENT = {
    "token Telegram": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "clé privée": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "IP locale": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "variable secrète renseignée": re.compile(
        r"^(TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|KRAKEN_API_KEY|KRAKEN_API_SECRET|"
        r"SOSOVALUE_API_KEY|POLYGON_API_KEY)[ \t]*=[ \t]*[^\s#]+",
        re.MULTILINE,
    ),
}

# Identifiants personnels à traquer (nom d'hôte, utilisateur SSH...),
# passés par variable d'environnement pour ne jamais les écrire dans
# le dépôt lui-même, ex. (PowerShell) :
#   $env:PREPUBLISH_MARKERS="monuser,monpc"; python tools/prepublish_check.py
PERSONAL_MARKERS: list[str] = [
    m.strip() for m in os.getenv("PREPUBLISH_MARKERS", "").split(",") if m.strip()
]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--cached"], capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def main() -> int:
    problems: list[str] = []
    files = tracked_files()

    for path in files:
        for pattern in FORBIDDEN_PATHS:
            if pattern.search(path):
                problems.append(f"[FICHIER INTERDIT] {path}")
                break

        file = Path(path)
        if not file.is_file() or file.stat().st_size > 2_000_000:
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for label, pattern in SUSPICIOUS_CONTENT.items():
            if pattern.search(text):
                problems.append(f"[{label.upper()}] {path}")
        for marker in PERSONAL_MARKERS:
            if marker.lower() in text.lower():
                problems.append(f"[DONNÉE PERSONNELLE '{marker}'] {path}")

    print(f"{len(files)} fichiers suivis analysés.")
    if problems:
        print("\nProblèmes détectés :")
        for p in problems:
            print(f"  - {p}")
        print("\nNe poussez pas avant d'avoir corrigé ces points.")
        return 1

    print("Aucun problème détecté. Prêt à publier.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
