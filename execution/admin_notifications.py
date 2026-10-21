"""
execution/admin_notifications.py

Messages Telegram d'administration pour VOTRE propre compte -- clé API
cassée/rétablie, position réparée, échec d'exécution/Breakeven/clôture
-- distincts des notifications de trading (voir messaging/formatter.py).
Chaque message est précédé d'un bandeau explicite pour ne jamais les
confondre avec un signal de trading, même en parcourant l'historique du
chat plus tard.

Déplacé depuis accounts/admin_notifications.py (simplification
architecture, août 2026) et allégé : les notifications propres à la
gestion de comptes tiers et à la facturation (nouvel utilisateur,
changement de statut de compte tiers, rapport mensuel de frais) ont été
retirées avec accounts/ -- il ne reste que les notifications liées à
VOTRE exécution (clé API, réparation de position, échecs).

Envoyées sur le même bot/chat Telegram configuré (voir .env).
"""

from __future__ import annotations

from notifications.telegram_sender import escape_markdown, send_telegram_message

ADMIN_BANNER = "🔧 *MESSAGE ADMINISTRATEUR* 🔧"


def _send_admin_message(body: str) -> None:
    """Préfixe systématiquement le bandeau administrateur avant l'envoi -- jamais oubliable un par un."""
    send_telegram_message(f"{ADMIN_BANNER}\n\n{body}")


def notify_api_key_broken(user_id: str, exchange: str, error_detail: str) -> None:
    """
    À appeler UNE SEULE FOIS par panne (voir execution/api_health.py) --
    dès qu'une erreur d'authentification est détectée (clé révoquée,
    permissions retirées, secret invalide).
    """
    message = (
        f"⚠️ *Clé API non fonctionnelle*\n"
        f"Exchange : {escape_markdown(exchange)}\n"
        f"Détail : {escape_markdown(error_detail)}\n\n"
        "_L'exécution automatique est bloquée jusqu'à résolution._"
    )
    _send_admin_message(message)


def notify_api_key_restored(user_id: str, exchange: str) -> None:
    """À appeler dès qu'un appel réussit à nouveau après une précédente panne signalée."""
    message = f"✅ *Clé API de nouveau fonctionnelle*\nExchange : {escape_markdown(exchange)}"
    _send_admin_message(message)


def notify_position_protection_repaired(user_id: str, symbol: str, sl_repaired: bool, tp_repaired: bool) -> None:
    """
    À appeler dès que check_and_repair_position_protection() a dû
    replacer un stop-loss et/ou des take-profits manquants sur une
    position déjà ouverte -- pour que vous sachiez qu'une lacune de
    protection a été détectée ET corrigée automatiquement, sans avoir à
    surveiller les logs en continu.
    """
    repaired_parts = []
    if sl_repaired:
        repaired_parts.append("stop-loss")
    if tp_repaired:
        repaired_parts.append("take-profit(s)")
    repaired_label = " et ".join(repaired_parts)

    message = (
        f"🔧 *Protection de position réparée*\n"
        f"Actif : {escape_markdown(symbol)}\n"
        f"Réparé : {repaired_label}\n\n"
        "_Une position ouverte n'avait plus cette protection active sur l'exchange "
        "(ex: robot arrêté juste après l'entrée) -- replacée automatiquement._"
    )
    _send_admin_message(message)


def notify_execution_failed(user_id: str, symbol: str, reason: str) -> None:
    """
    À appeler dès qu'une tentative d'exécution automatique échoue
    (solde insuffisant, levier requis au-delà du plafond configuré,
    erreur exchange...) -- jusqu'ici seulement loggé, jamais notifié :
    si le robot n'est pas surveillé en continu, un trade refusé pouvait
    passer totalement inaperçu.
    """
    message = (
        f"⚠️ *Exécution automatique échouée*\n"
        f"Actif : {escape_markdown(symbol)}\n"
        f"Raison : {escape_markdown(reason)}\n\n"
        "_Le setup reste notifié normalement, mais aucune position n'a été ouverte sur ce trade précis._"
    )
    _send_admin_message(message)


def notify_breakeven_failed(user_id: str, symbol: str, reason: str) -> None:
    """
    À appeler dès qu'une tentative de déplacement du SL au Breakeven
    échoue (ex: panne exchange transitoire).

    ⚠️ Contrairement à notify_close_failed, la position reste
    normalement protégée par son SL INITIAL (jamais annulé tant que le
    nouveau n'est pas confirmé placé, voir kraken_client.py::move_stop_loss)
    -- pas une urgence immédiate, mais un gain déjà acquis (au-delà de
    TP1) qui n'est pas encore sécurisé. Le robot retente automatiquement
    à chaque cycle (voir main.py::_check_pending_breakeven) jusqu'à
    confirmation.
    """
    message = (
        f"⚠️ *Déplacement du SL au Breakeven échoué*\n"
        f"Actif : {escape_markdown(symbol)}\n"
        f"Raison : {escape_markdown(reason)}\n\n"
        "_Le SL INITIAL reste actif (position protégée, pas sans stop-loss), mais le gain "
        "acquis depuis TP1 n'est pas encore sécurisé. Le robot retente automatiquement à "
        "chaque cycle -- vérifiez manuellement si ça persiste._"
    )
    _send_admin_message(message)


def notify_breakeven_repaired(user_id: str, symbol: str) -> None:
    """À appeler dès qu'un déplacement du SL au Breakeven réussit après un précédent échec (voir notify_breakeven_failed)."""
    message = (
        f"✅ *SL déplacé au Breakeven avec succès*\n"
        f"Actif : {escape_markdown(symbol)}\n\n"
        "_Une précédente tentative avait échoué -- résolu automatiquement au cycle suivant._"
    )
    _send_admin_message(message)


def notify_close_failed(user_id: str, symbol: str, reason: str) -> None:
    """
    À appeler dès qu'une tentative de CLÔTURE automatique échoue (SL
    touché, TP touché, biais HTF invalidé...) -- notre base marque
    l'alerte comme fermée AVANT même de tenter la clôture réelle sur
    l'exchange (nécessaire pour éviter de retenter indéfiniment un
    setup déjà traité) -- si cette clôture réelle échoue, RIEN ne la
    retente automatiquement ensuite. Sans cette notification, une
    position pouvait rester ouverte et NON PROTÉGÉE (SL potentiellement
    déjà annulé/consommé) indéfiniment, sans que personne ne s'en aperçoive.
    """
    message = (
        f"🚨 *Clôture automatique échouée -- vérification manuelle requise*\n"
        f"Actif : {escape_markdown(symbol)}\n"
        f"Raison : {escape_markdown(reason)}\n\n"
        "_Une position est très probablement encore OUVERTE sur l'exchange, "
        "sans qu'aucune nouvelle tentative automatique ne soit prévue. "
        "Vérifiez et fermez manuellement si nécessaire._"
    )
    _send_admin_message(message)
