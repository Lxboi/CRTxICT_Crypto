"""
manage_execution.py

Script de contrôle manuel de l'automatisation des prises de position.
C'EST LE SEUL MOYEN d'activer l'exécution automatique — jamais une
conséquence indirecte d'une autre commande.

Mono-compte (VOUS uniquement) depuis la simplification architecture
(août 2026, retrait du multi-utilisateur) — agit toujours sur VOTRE
propre compte (kill_switch.OWNER_USER_ID).

Usage :
    python manage_execution.py --status
    python manage_execution.py --enable    # active pour vous
    python manage_execution.py --disable   # désactive pour vous

IMPORTANT : ce fichier n'a JAMAIS de préfixe "test_" et toute sa logique
est protégée par `if __name__ == "__main__":`.
"""

from __future__ import annotations

import argparse
import sys

from execution import kill_switch


def print_status() -> None:
    state = kill_switch.get_state(kill_switch.OWNER_USER_ID)
    if state["enabled"]:
        print("🟢 Exécution automatique : ACTIVÉE")
    else:
        print("🔴 Exécution automatique : DÉSACTIVÉE")
    print(f"   Dernier changement : {state.get('changed_at') or 'jamais'}")
    print(f"   Raison : {state.get('reason')}")


def do_enable() -> None:
    print("⚠️  ATTENTION : vous êtes sur le point d'activer l'exécution automatique.")
    print("   Le robot va commencer à passer de VRAIS ordres, avec de VRAIS fonds,")
    print("   sans confirmation manuelle supplémentaire à chaque signal.")
    print()
    confirm = input('Pour confirmer, tapez exactement "ACTIVER" : ')
    if confirm.strip() != "ACTIVER":
        print("Annulé — l'exécution automatique reste désactivée.")
        sys.exit(0)

    kill_switch.enable(kill_switch.OWNER_USER_ID)
    print("\n🟢 Exécution automatique ACTIVÉE.")
    print("   Pour désactiver à tout moment : python manage_execution.py --disable")


def do_disable() -> None:
    kill_switch.disable(kill_switch.OWNER_USER_ID)
    print("🔴 Exécution automatique DÉSACTIVÉE immédiatement.")
    print("   Les setups continueront d'être détectés et notifiés normalement,")
    print("   mais aucun ordre ne sera plus passé automatiquement.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Contrôle de l'exécution automatique des positions.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true", help="Affiche l'état actuel.")
    group.add_argument("--enable", action="store_true", help="Active l'exécution automatique (confirmation requise).")
    group.add_argument("--disable", action="store_true", help="Désactive l'exécution automatique immédiatement.")
    args = parser.parse_args()

    if args.status:
        print_status()
    elif args.enable:
        do_enable()
    elif args.disable:
        do_disable()
