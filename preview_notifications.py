"""
preview_notifications.py

Script de prévisualisation : affiche les messages que le robot envoie
au cours de la vie d'un trade (création du setup avec R:R/biais
BTC/SMT, TP1 touché avec consigne de déplacement du SL, clôture en
Breakeven, clôture sur SL) — pour voir le rendu exact sans attendre
qu'un vrai setup se déclenche sur le marché.

Utilise ETH (pas BTC) comme actif principal : la ligne "Biais BTC
(référence)" ne s'affiche JAMAIS pour BTC lui-même (voir
messaging/formatter.py), donc un actif autre que BTC est nécessaire
pour la démontrer.

IMPORTANT : ce fichier n'a JAMAIS de préfixe "test_" et n'est jamais
importé automatiquement — il ne se lance que manuellement, via :

    python preview_notifications.py            # aperçu console uniquement (aucun envoi Telegram)
    python preview_notifications.py --send      # envoie réellement chaque message sur Telegram

Toute la logique est protégée par `if __name__ == "__main__":`, donc
même si ce fichier était importé par erreur (ex: par un outil qui scanne
tous les .py du projet), rien ne s'exécuterait automatiquement.

Utilise une base de données TEMPORAIRE (jamais database/alerts.db) et
appelle les VRAIES fonctions de main.py (dispatch_alert,
_check_tp_sl_hits) — donc les messages affichés sont garantis
identiques à ceux que le robot enverrait réellement, pas une
reconstruction approximative qui pourrait diverger avec le temps.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).parent / ".env")

TEST_BANNER = "🧪🧪🧪 CECI EST UN TEST — PAS UN VRAI SIGNAL 🧪🧪🧪"

# Biais BTC simulé pour ce cycle — normalement calculé par run_once()
# en traitant BTC en premier (voir main.py), ici fourni "à la main"
# puisqu'on ne fait tourner qu'ETH dans cet aperçu.
FAKE_BTC_BIAS_CONTEXT = {"bias": "bullish", "source_timeframe": "weekly"}

# Divergence SMT simulée (ETH vs BTC — la paire fixe configurée pour
# BTC/ETH, voir config/alerts.yaml::correlated_pairs) — purement
# informatif, voir rules/narrative_rule.py::_get_smt_context.
FAKE_SMT_CONTEXT = {
    "available": True,
    "secondary_symbol": "BTC",
    "divergence": {
        "type": "bullish_smt",
        "detail": "Nouveau plus bas confirmé, mais pas sur BTC -> divergence haussière potentielle (SMT)",
    },
}


def run_preview(actually_send: bool) -> None:
    import database.db as db
    import main
    from rules.decision_engine import DecisionReport, TimeframeSetup

    # Base de données temporaire, séparée de database/alerts.db — ne
    # touche jamais à votre base réelle.
    tmp_db_path = Path(tempfile.gettempdir()) / "preview_notifications.db"
    try:
        if tmp_db_path.exists():
            tmp_db_path.unlink()
    except PermissionError:
        # Windows verrouille parfois encore le fichier d'un lancement
        # précédent juste après la fermeture de la connexion SQLite --
        # pas grave, on réutilise simplement le fichier existant.
        pass
    db.DB_PATH = tmp_db_path
    db.init_db()

    captured_messages: list[str] = []

    def fake_send(message: str) -> bool:
        wrapped = f"{TEST_BANNER}\n\n{message}\n\n{TEST_BANNER}"
        captured_messages.append(wrapped)
        if actually_send:
            from notifications.telegram_sender import send_telegram_message

            return send_telegram_message(wrapped)
        return True

    setup = TimeframeSetup(
        timeframe="weekly",
        setup_name="ETH Weekly CRT (04/05)",
        htf_crt={"bias": "bullish", "dol_price": 3600.0, "purge_side": "low"},
        crt_targets={"tp_50": 3500.0, "tp_100": 3600.0, "tp_htf_dol": None, "stop_loss": 3350.0},
        entry_recommended=True,
    )
    report = DecisionReport(
        reasoning_log=["Test de prévisualisation"],
        effective_bias="bullish",
        confidence="high",
        entry_recommended=True,
        timeframe_setups=[setup],
        smt_context=FAKE_SMT_CONTEXT,
    )

    with patch("main.send_telegram_message", side_effect=fake_send):
        # --- 1. Création du setup (entrée à 3450, avant tout TP) ---
        # -> montre R:R, biais BTC (référence), et SMT en une seule fois
        entry_price = 3450.0
        decision_id = db.save_decision(report, entry_price, symbol="ETH")
        main.dispatch_alert(report, decision_id, entry_price, "ETH", FAKE_BTC_BIAS_CONTEXT)

        # --- 2. TP1 touché (3500) : notification + consigne de déplacement du SL ---
        main._check_tp_sl_hits("ETH", 3510.0)

        # --- 3. Le prix revient au prix d'entrée (3450) : clôture en Breakeven ---
        main._check_tp_sl_hits("ETH", 3450.0)

    # --- 4. Scénario SÉPARÉ : SL touché directement, sans TP1 préalable ---
    # (dol_price volontairement différent du 1er setup, pour éviter que la
    # déduplication ne le traite à tort comme le même setup — voir
    # database/db.py::_build_dedup_key, basée entre autres sur dol_price)
    setup_sl = TimeframeSetup(
        timeframe="weekly",
        setup_name="ETH Weekly CRT (04/05) [scénario SL]",
        htf_crt={"bias": "bullish", "dol_price": 3650.0, "purge_side": "low"},
        crt_targets={"tp_50": 3500.0, "tp_100": 3650.0, "tp_htf_dol": None, "stop_loss": 3350.0},
        entry_recommended=True,
    )
    report_sl = DecisionReport(
        reasoning_log=["Test de prévisualisation"],
        effective_bias="bullish",
        confidence="high",
        entry_recommended=True,
        timeframe_setups=[setup_sl],
    )
    with patch("main.send_telegram_message", side_effect=fake_send):
        decision_id_sl = db.save_decision(report_sl, 3450.0, symbol="ETH")
        main.dispatch_alert(report_sl, decision_id_sl, 3450.0, "ETH", FAKE_BTC_BIAS_CONTEXT)
        main._check_tp_sl_hits("ETH", 3340.0)  # SL touché directement

    labels = [
        "1. CRÉATION DU SETUP (R:R, Biais BTC référence, SMT)",
        "2. TP1 TOUCHÉ (+ consigne de déplacement du SL au PE)",
        "3. CLÔTURE EN BREAKEVEN (retour au prix d'entrée)",
        "4. CRÉATION D'UN 2E SETUP (scénario SL)",
        "5. CLÔTURE SUR SL (sans TP1 préalable)",
    ]
    for label, message in zip(labels, captured_messages):
        print("=" * 70)
        print(label)
        print("=" * 70)
        print(message)
        print()

    try:
        tmp_db_path.unlink(missing_ok=True)
    except PermissionError:
        print(
            "\nℹ️  Fichier temporaire encore verrouillé par Windows -- sera nettoyé "
            "automatiquement au prochain lancement, aucun impact sur ce test."
        )

    if actually_send:
        print("✅ Chaque message a été réellement envoyé sur Telegram (--send activé).")
    else:
        print(
            "ℹ️  Aperçu console uniquement, aucun message envoyé sur Telegram "
            "(relancez avec --send pour un envoi réel)."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Aperçu des notifications du robot.")
    parser.add_argument(
        "--send",
        action="store_true",
        help="Envoie réellement chaque message sur Telegram (par défaut : aperçu console uniquement).",
    )
    args = parser.parse_args()
    run_preview(actually_send=args.send)