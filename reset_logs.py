"""
reset_logs.py

Réinitialise les logs du bot (dossier logs/), indépendamment de la
rotation automatique déjà en place (voir main.py::main() —
rotation="1 day", retention="30 days", qui supprime déjà les fichiers
de plus de 30 jours tout seule). Ce script sert pour un nettoyage
manuel immédiat : avant un déploiement propre, pour libérer de la
place sans attendre la rotation automatique, ou simplement pour
repartir de logs vides en test.

Usage :
    python reset_logs.py            # supprime les logs de plus de N jours (7 par défaut)
    python reset_logs.py --days 3   # personnalise le seuil
    python reset_logs.py --full     # supprime TOUS les fichiers de logs, sans exception
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"


def reset_old_logs(days: int) -> None:
    """
    Supprime uniquement les fichiers de logs plus vieux que `days`
    jours — les logs récents (utiles pour déboguer un souci en cours)
    sont conservés.
    """
    if not LOG_DIR.exists():
        print(f"Aucun dossier de logs trouvé ({LOG_DIR}), rien à faire.")
        return

    cutoff = time.time() - days * 86400
    removed = 0
    freed_bytes = 0

    for log_file in LOG_DIR.glob("*.log"):
        if log_file.stat().st_mtime < cutoff:
            freed_bytes += log_file.stat().st_size
            log_file.unlink()
            removed += 1

    freed_mb = freed_bytes / 1024 / 1024
    print(
        f"OK : {removed} fichier(s) de logs de plus de {days} jour(s) supprimé(s) "
        f"(~{freed_mb:.1f} Mo libérés)."
    )


def reset_full() -> None:
    """Supprime TOUS les fichiers de logs, sans exception (y compris les plus récents)."""
    if not LOG_DIR.exists():
        print(f"Aucun dossier de logs trouvé ({LOG_DIR}), rien à faire.")
        return

    removed = 0
    freed_bytes = 0
    for log_file in LOG_DIR.glob("*.log"):
        freed_bytes += log_file.stat().st_size
        log_file.unlink()
        removed += 1

    freed_mb = freed_bytes / 1024 / 1024
    print(f"OK : {removed} fichier(s) de logs supprimé(s) (~{freed_mb:.1f} Mo libérés).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Réinitialise les logs du bot.")
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Supprime les logs plus vieux que N jours (défaut : 7). Ignoré avec --full.",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Supprime TOUS les fichiers de logs, y compris les plus récents.",
    )
    args = parser.parse_args()

    if args.full:
        confirm = input(
            "⚠️  Ceci va supprimer TOUS les fichiers de logs, y compris les plus récents "
            "(vous perdrez la trace des derniers cycles). Confirmer ? [oui/N] "
        )
        if confirm.strip().lower() != "oui":
            print("Annulé.")
            sys.exit(0)
        reset_full()
    else:
        reset_old_logs(args.days)