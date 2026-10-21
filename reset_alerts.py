"""
reset_alerts.py

Réinitialise le suivi des alertes/positions du bot, pour pouvoir
recevoir de nouvelles notifications sur des setups déjà notifiés
(utile en test, ou après un arrêt prolongé du bot).

Usage :
    python reset_alerts.py            # vide uniquement la table alerts
                                       # (garde l'historique des décisions/snapshots)
    python reset_alerts.py --full     # supprime toute la base (repart de zéro)
"""

from __future__ import annotations

import argparse
import sys

import database.db as db


def reset_alerts_only() -> None:
    """
    Vide la table `alerts` uniquement — tous les setups déjà notifiés
    (dedup_key) redeviennent éligibles à une nouvelle notification au
    prochain cycle. L'historique des décisions et des instantanés de
    marché est conservé intact.
    """
    db.init_db()  # s'assure que la table existe avant de la vider (base neuve ou ancienne)
    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        conn.execute("DELETE FROM alerts")
    print(f"OK : {count} alerte(s) supprimée(s). Tous les setups pourront être notifiés à nouveau.")


def reset_full() -> None:
    """
    Supprime le fichier de base de données entier (décisions, snapshots,
    alertes), PUIS recrée immédiatement le schéma vide -- ne compte
    JAMAIS sur un futur redémarrage du bot pour ça (bug réel corrigé,
    août 2026 : lancé via `docker compose exec` pendant que le
    conteneur tournait déjà, le process du bot en cours d'exécution
    n'appelle db.init_db() qu'une seule fois, à SON PROPRE démarrage --
    sans ce correctif, le fichier restait sans aucune table jusqu'à un
    éventuel redémarrage manuel du conteneur, provoquant des erreurs
    "no such table" à chaque écriture entre-temps).
    """
    if db.DB_PATH.exists():
        db.DB_PATH.unlink()
        print(f"Base de données supprimée ({db.DB_PATH}).")
    else:
        print("Aucune base de données trouvée, rien à supprimer.")

    db.init_db()
    print("OK : schéma recréé immédiatement -- rien à redémarrer, le bot peut continuer de tourner normalement.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Réinitialise le suivi des alertes du bot.")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Supprime TOUTE la base (décisions + snapshots + alertes), pas seulement les alertes.",
    )
    args = parser.parse_args()

    if args.full:
        confirm = input(
            "⚠️  Ceci va supprimer TOUT l'historique (décisions, snapshots, alertes). "
            "Confirmer ? [oui/N] "
        )
        if confirm.strip().lower() != "oui":
            print("Annulé.")
            sys.exit(0)
        reset_full()
    else:
        reset_alerts_only()