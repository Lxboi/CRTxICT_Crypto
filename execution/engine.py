"""
execution/engine.py

Orchestre l'exécution automatique d'un signal d'entrée trouvé par le
robot — vérifie SYSTÉMATIQUEMENT le kill-switch avant toute action, et
ne fait jamais aucune supposition silencieuse.
"""

from __future__ import annotations

from loguru import logger

from execution import kill_switch
from execution.exchange_client import ExchangeClient, OrderResult
from execution.position_sizing import compute_position_size, compute_required_leverage


def try_execute_entry(
    client: ExchangeClient,
    symbol: str,
    bias: str,
    entry_price: float,
    stop_loss: float,
    risk_pct: float,
    max_leverage: float,
    user_id: str = kill_switch.OWNER_USER_ID,
    take_profit_levels: list[float] | None = None,
    reserved_balance_pct: float = 0.0,
) -> OrderResult | None:
    """
    Tente d'exécuter un ordre pour le signal fourni, POUR UN UTILISATEUR
    DONNÉ — mais UNIQUEMENT si le kill-switch de CET utilisateur précis
    est activé (chaque utilisateur a son propre interrupteur, voir
    execution/kill_switch.py — désactiver un compte n'affecte jamais
    les autres). Vérifié à CHAQUE appel (jamais mis en cache), pour
    qu'une désactivation via manage_execution.py --disable prenne
    effet immédiatement, y compris en plein milieu d'un cycle.

    Le levier utilisé est calculé dynamiquement (voir
    position_sizing.py::compute_required_leverage) pour permettre
    d'ouvrir la taille de position correspondant exactement à
    `risk_pct` % de risque — jamais un levier fixe. Si le levier requis
    dépasse `max_leverage` (plafond de sécurité configuré), l'ordre est
    refusé plutôt que d'ouvrir une position avec un levier non désiré.

    Returns:
        None si le kill-switch est désactivé pour cet utilisateur
        (aucune tentative d'ordre). Un OrderResult sinon — succès ou
        échec, jamais d'exception laissée remonter (toute erreur
        inattendue est capturée et renvoyée comme un échec, pour ne
        jamais interrompre le reste du cycle du robot, ni les autres
        utilisateurs).
    """
    if not kill_switch.is_enabled(user_id):
        logger.debug(
            f"[{symbol}] Exécution automatique désactivée pour '{user_id}' — "
            "signal noté mais aucun ordre passé."
        )
        return None

    try:
        account_balance = client.get_account_balance()
    except Exception as e:
        logger.exception(f"[{symbol}] Impossible de récupérer le solde du compte — ordre annulé par sécurité.")
        # Message d'erreur RÉEL conservé (pas un texte générique) :
        # nécessaire pour que execution/api_health.py::is_auth_failure()
        # puisse détecter une panne d'authentification (clé révoquée)
        # et déclencher la notification correspondante.
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message=f"Solde du compte inaccessible : {e}")

    try:
        quantity = compute_position_size(account_balance, risk_pct, entry_price, stop_loss)
    except ValueError as e:
        logger.error(f"[{symbol}] Taille de position invalide : {e}")
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message=str(e))

    if quantity <= 0:
        logger.error(f"[{symbol}] Taille de position calculée nulle ou négative — ordre annulé.")
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message="Taille de position nulle")

    try:
        leverage = compute_required_leverage(
            quantity, entry_price, account_balance, max_leverage, reserved_balance_pct
        )
    except ValueError as e:
        logger.error(f"[{symbol}] {e}")
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message=str(e))

    side = "long" if bias == "bullish" else "short"

    try:
        result = client.place_market_order(
            user_id, symbol, side, quantity, stop_loss, leverage, take_profit_levels
        )
    except Exception as e:
        logger.exception(f"[{symbol}] Erreur inattendue lors du passage d'ordre — capturée, pas de crash.")
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message=f"Exception inattendue : {e}")

    if result.success:
        logger.warning(
            f"[{symbol}] Ordre {side} exécuté : {result.filled_quantity} @ {result.filled_price} "
            f"(levier {leverage:.1f}x)"
        )
    else:
        logger.error(f"[{symbol}] Échec de l'ordre : {result.error_message}")

    return result


def try_move_stop_to_breakeven(
    client: ExchangeClient, symbol: str, entry_price: float, user_id: str = kill_switch.OWNER_USER_ID
) -> OrderResult | None:
    """
    Déplace RÉELLEMENT le stop-loss d'une position ouverte vers le prix
    d'entrée, dès que TP1 est atteint, POUR UN UTILISATEUR DONNÉ — la
    contrepartie exécutée de la consigne notifiée par main.py::
    _check_tp_sl_hits (qui, elle, se contente d'informer l'utilisateur).
    Avec l'exécution automatique activée pour ce compte, cette fonction
    doit être appelée en plus de la notification, au même moment précis
    (transition TP1 nouvellement atteint), pour que le SL soit
    réellement déplacé sur l'exchange et pas seulement suggéré.

    Comme try_execute_entry() : vérifie le kill-switch de CET
    utilisateur à chaque appel, ne lève jamais d'exception, renvoie
    None si l'exécution automatique est désactivée pour lui (le SL
    reste alors à sa charge, via la notification classique).
    """
    if not kill_switch.is_enabled(user_id):
        logger.debug(
            f"[{symbol}] Exécution automatique désactivée pour '{user_id}' — SL non déplacé automatiquement."
        )
        return None

    try:
        result = client.move_stop_loss(user_id, symbol, entry_price)
    except Exception as e:
        logger.exception(
            f"[{symbol}] Erreur inattendue lors du déplacement du SL au Breakeven — "
            "position potentiellement SANS stop-loss actif, vérification manuelle requise."
        )
        return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                            error_message=f"Exception inattendue lors du déplacement du SL : {e}")

    if result.success:
        logger.warning(f"[{symbol}] SL déplacé au prix d'entrée (Breakeven) : {entry_price}")
    else:
        logger.error(
            f"[{symbol}] Échec du déplacement du SL au Breakeven : {result.error_message} — "
            "vérification manuelle requise, la position peut être sans protection."
        )

    return result


def try_execute_entry_for_all_users(
    symbol: str,
    bias: str,
    entry_price: float,
    stop_loss: float,
    risk_pct: float,
    exchange_leverage_config: dict[str, float],
    owner_client: ExchangeClient | None,
    owner_exchange: str | None,
    user_risk_overrides: dict[str, float] | None = None,
    take_profit_levels: list[float] | None = None,
    reserved_balance_pct: float = 0.0,
) -> dict[str, OrderResult | None]:
    """
    Tente l'exécution pour VOUS (owner_client, si fourni) -- voir
    _for_each_user().

    Args:
        exchange_leverage_config: Dict {nom_exchange: max_leverage},
            lu depuis config/alerts.yaml::execution.exchanges.
        owner_client: Votre propre ExchangeClient déjà construit (ou
            None si vous ne voulez pas exécuter sur ce signal).
        owner_exchange: Le nom de VOTRE exchange (voir
            config/alerts.yaml::execution.exchange) — pour résoudre
            votre propre plafond de levier depuis exchange_leverage_config.
        user_risk_overrides: Conservé pour compatibilité de signature --
            plus aucune source ne le renseigne depuis le retrait du
            multi-utilisateur (voir _for_each_user, un seul "utilisateur"
            désormais : vous).

    Returns:
        Dict {user_id: OrderResult | None} -- un seul élément (VOTRE
        user_id, execution.kill_switch.OWNER_USER_ID), None si le
        kill-switch était désactivé.
    """
    def _action(client: ExchangeClient, user_id: str, exchange: str, user_risk_pct: float) -> OrderResult | None:
        effective_risk_pct = (user_risk_overrides or {}).get(user_id, user_risk_pct)
        max_leverage = exchange_leverage_config.get(exchange)
        if max_leverage is None:
            logger.error(
                f"[{symbol}] Aucun plafond de levier configuré pour l'exchange '{exchange}' "
                f"(voir config/alerts.yaml::execution.exchanges) -- ordre refusé pour '{user_id}'."
            )
            return OrderResult(
                success=False, order_id=None, filled_price=None, filled_quantity=None,
                error_message=f"Plafond de levier non configuré pour l'exchange '{exchange}'",
            )
        return try_execute_entry(
            client, symbol, bias, entry_price, stop_loss, effective_risk_pct, max_leverage, user_id=user_id,
            take_profit_levels=take_profit_levels, reserved_balance_pct=reserved_balance_pct,
        )

    return _for_each_user(owner_client, owner_exchange, risk_pct, _action)


def check_and_execute_missing_position(
    symbol: str,
    bias: str,
    entry_price: float,
    stop_loss: float,
    risk_pct: float,
    exchange_leverage_config: dict[str, float],
    owner_client: ExchangeClient | None,
    owner_exchange: str | None,
    user_risk_overrides: dict[str, float] | None = None,
    take_profit_levels: list[float] | None = None,
    reserved_balance_pct: float = 0.0,
) -> dict[str, OrderResult | None]:
    """
    Exécution de RATTRAPAGE, à appeler à CHAQUE cycle pour tout setup
    déjà notifié (encore ouvert) — voir main.py::_check_missing_execution.

    dispatch_alert() ne tente l'exécution QU'À LA TOUTE PREMIÈRE
    détection d'un setup (voir main.py::dispatch_alert, `new_setups`) :
    si le kill-switch était désactivé, ou qu'une erreur exchange
    ponctuelle a fait échouer cette unique tentative, AUCUNE position
    réelle n'est jamais ouverte, alors même que le setup reste
    "entry_recommended" et suivi normalement pour son SL/TP -- cas réel
    (BTC, août 2026) : plusieurs setups notifiés sans jamais avoir été
    exécutés faute d'exécution activée à ce moment précis.

    Si AUCUNE position n'existe actuellement sur ce symbole pour vous,
    tente une exécution avec les paramètres DÉJÀ enregistrés pour ce
    setup (prix d'entrée/stop du moment de la détection) — mêmes
    garde-fous que pour une exécution normale (`try_execute_entry` :
    R:R minimum, réserve de solde, plafond de levier). Si une position
    existe déjà (peu importe laquelle), ne fait RIEN -- ne tente jamais
    d'ouvrir une deuxième position sur le même symbole.
    """
    def _action(client: ExchangeClient, user_id: str, exchange: str, user_risk_pct: float) -> OrderResult | None:
        if not kill_switch.is_enabled(user_id):
            return None

        try:
            positions = client.get_open_positions()
        except Exception:
            logger.exception(f"[{symbol}] Impossible de vérifier les positions pour '{user_id}' (rattrapage).")
            return None
        if any(p.symbol == symbol for p in positions):
            return None  # position déjà ouverte -- rien à rattraper pour cet utilisateur

        effective_risk_pct = (user_risk_overrides or {}).get(user_id, user_risk_pct)
        max_leverage = exchange_leverage_config.get(exchange)
        if max_leverage is None:
            return None

        result = try_execute_entry(
            client, symbol, bias, entry_price, stop_loss, effective_risk_pct, max_leverage, user_id=user_id,
            take_profit_levels=take_profit_levels, reserved_balance_pct=reserved_balance_pct,
        )
        if result is not None and result.success:
            logger.warning(f"[{symbol}] Exécution de RATTRAPAGE réussie pour '{user_id}'.")
        return result

    return _for_each_user(owner_client, owner_exchange, risk_pct, _action)


def _for_each_user(
    owner_client: ExchangeClient | None, owner_exchange: str | None, owner_risk_pct: float, per_user_action
):
    """
    Exécute `per_user_action` pour VOUS (owner_client), si fourni.

    Simplification architecture (août 2026) : cette fonction bouclait
    auparavant sur vous PUIS sur chaque utilisateur tiers actif (voir
    accounts/user_account.py::list_active_accounts, supprimé avec le
    reste du multi-utilisateur/facturation). Conservée sous ce nom et
    avec cette signature (plutôt qu'inlinée dans chacun des 5
    appelants) pour garder UN SEUL point du code responsable de la
    détection de panne d'authentification (ci-dessous) — comportement
    partagé par try_execute_entry_for_all_users(),
    check_and_execute_missing_position(),
    try_move_stop_to_breakeven_for_all_users(),
    try_close_position_for_all_users() et
    check_and_repair_position_protection().

    Détecte un échec d'AUTHENTIFICATION (clé API révoquée/permissions
    retirées — voir execution/api_health.py::is_auth_failure) et
    notifie automatiquement (une seule fois par panne, jusqu'à
    résolution) — voir execution/admin_notifications.py::
    notify_api_key_broken / notify_api_key_restored.

    `per_user_action(client, user_id, exchange, risk_pct_per_trade) -> OrderResult | None`
    """
    from execution.admin_notifications import notify_api_key_broken, notify_api_key_restored
    from execution.api_health import clear_broken_flag, flag_broken, is_auth_failure, is_flagged_broken

    def _check_health(user_id: str, exchange: str, result: Any) -> None:
        if result is None or not isinstance(result, OrderResult):
            # Actions dont le résultat n'est pas un OrderResult (ex:
            # check_and_repair_position_protection, qui renvoie un dict
            # personnalisé) -- pas concernées par la détection de panne
            # d'authentification, on ne fait rien de plus ici.
            return

        if not result.success and is_auth_failure(result.error_message):
            if not is_flagged_broken(user_id):
                flag_broken(user_id)
                notify_api_key_broken(user_id, exchange, result.error_message or "erreur inconnue")
        elif result.success and is_flagged_broken(user_id):
            clear_broken_flag(user_id)
            notify_api_key_restored(user_id, exchange)

    results: dict[str, Any] = {}

    if owner_client is not None:
        exchange = owner_exchange or "inconnu"
        result = per_user_action(owner_client, kill_switch.OWNER_USER_ID, exchange, owner_risk_pct)
        _check_health(kill_switch.OWNER_USER_ID, exchange, result)
        results[kill_switch.OWNER_USER_ID] = result

    return results


def try_move_stop_to_breakeven_for_all_users(
    symbol: str, entry_price: float, owner_client: ExchangeClient | None, owner_exchange: str | None
) -> dict[str, OrderResult | None]:
    """Équivalent multi-utilisateurs de try_move_stop_to_breakeven() — voir _for_each_user()."""
    return _for_each_user(
        owner_client, owner_exchange, 0.0,  # risk_pct non utilisé pour un déplacement de SL
        lambda client, user_id, exchange, risk_pct: try_move_stop_to_breakeven(
            client, symbol, entry_price, user_id=user_id
        ),
    )


def try_close_position_for_all_users(
    symbol: str, owner_client: ExchangeClient | None, owner_exchange: str | None, fraction: float = 1.0
) -> dict[str, OrderResult | None]:
    """
    Ferme une FRACTION de la position sur l'exchange (1.0 = clôture
    intégrale, comportement historique inchangé) pour VOUS.

    Le registre d'ordre stop (execution/stop_order_registry.py) n'est
    nettoyé QUE lors d'une clôture INTÉGRALE (fraction >= 1.0) — une
    clôture partielle laisse une position restante qui a toujours
    besoin d'un stop-loss actif, redimensionné séparément (voir
    main.py::_check_tp_sl_hits, qui rappelle try_move_stop_to_breakeven_for_all_users
    juste après une clôture partielle pour ajuster la taille du stop
    à ce qui reste ouvert).

    Un utilisateur dont le kill-switch est désactivé n'est jamais
    fermé automatiquement ici (voir try_execute_entry pour la même
    logique) — sa position, si elle existe, reste sous son contrôle
    manuel.
    """
    def _close_and_log(client: ExchangeClient, user_id: str, exchange: str, risk_pct: float) -> OrderResult | None:
        if not kill_switch.is_enabled(user_id):
            logger.debug(f"[{symbol}] Exécution automatique désactivée pour '{user_id}' — clôture non tentée.")
            return None

        try:
            positions = client.get_open_positions()
            position = next((p for p in positions if p.symbol == symbol), None)
            realized_pnl = (position.unrealized_pnl * fraction) if position else 0.0
        except Exception:
            logger.exception(f"[{symbol}] Impossible de lire la position pour '{user_id}' avant clôture.")
            realized_pnl = 0.0

        try:
            result = client.close_position(user_id, symbol, fraction)
        except Exception as e:
            logger.exception(f"[{symbol}] Erreur inattendue lors de la clôture pour '{user_id}'.")
            return OrderResult(success=False, order_id=None, filled_price=None, filled_quantity=None,
                                error_message=f"Exception inattendue : {e}")

        if result.success:
            if fraction >= 1.0:
                from execution.stop_order_registry import clear_stop_order_id

                clear_stop_order_id(user_id, symbol)  # l'ordre stop lié n'a plus lieu d'être suivi
                logger.warning(
                    f"[{symbol}] Position clôturée pour '{user_id}' — PnL réalisé : {realized_pnl:+.2f}"
                )
            else:
                logger.warning(
                    f"[{symbol}] Position partiellement clôturée ({fraction:.0%}) pour '{user_id}' "
                    f"— PnL réalisé (partiel) : {realized_pnl:+.2f}"
                )
        else:
            logger.error(f"[{symbol}] Échec de la clôture pour '{user_id}' : {result.error_message}")

        return result

    return _for_each_user(owner_client, owner_exchange, 0.0, _close_and_log)


def check_and_repair_position_protection(
    symbol: str,
    alert: dict,
    owner_client: ExchangeClient | None,
    owner_exchange: str | None,
) -> dict[str, dict[str, bool]]:
    """
    Vérifie, à CHAQUE cycle, que les positions RÉELLEMENT ouvertes sur
    l'exchange (pas seulement suivies en base) ont bien un stop-loss ET
    des take-profits actifs — et RÉPARE automatiquement si ce n'est pas
    le cas (ex: le robot a été arrêté juste après l'entrée, avant que
    le placement des TP n'ait pu se terminer, ou un TP a échoué à la
    création sans jamais être rattrapé).

    Contrairement à try_close_position_for_all_users (qui agit APRÈS
    un franchissement de prix détecté par le robot), cette vérification
    ne dépend d'AUCUN événement déclencheur : elle s'exécute à chaque
    cycle, tant que le robot tourne — importante, car un utilisateur
    peut choisir de ne pas laisser le robot actif en permanence, et
    cette vérification comble le trou dès que le robot revient en ligne.

    Args:
        alert: Dict de l'alerte ouverte correspondante (voir
            database/db.py::get_open_alerts) — fournit stop_loss,
            tp_50/tp_100/tp_htf_dol, highest_tp_hit, entry_price.

    Returns:
        Dict {user_id: {"sl_repaired": bool, "tp_repaired": bool}} —
        pour savoir qui a été réparé et sur quoi, à des fins de
        notification.
    """
    def _action(client: ExchangeClient, user_id: str, exchange: str, risk_pct: float) -> dict[str, bool] | None:
        if not kill_switch.is_enabled(user_id):
            return None

        try:
            positions = client.get_open_positions()
        except Exception:
            logger.exception(f"[{symbol}] Impossible de vérifier les positions pour '{user_id}'.")
            return None

        position = next((p for p in positions if p.symbol == symbol), None)
        if position is None:
            return None  # rien d'ouvert pour cet utilisateur -- rien à vérifier

        repaired = {"sl_repaired": False, "tp_repaired": False}
        highest_tp_hit = alert.get("highest_tp_hit", 0) or 0

        from execution.stop_order_registry import get_stop_order_id
        from execution.tp_order_registry import get_tp_order_ids

        if get_stop_order_id(user_id, symbol) is None:
            entry_price = alert.get("entry_price")
            effective_sl = (
                entry_price if highest_tp_hit >= 1 and entry_price is not None else alert.get("stop_loss")
            )
            if effective_sl is not None:
                result = client.move_stop_loss(user_id, symbol, effective_sl)
                if result.success:
                    logger.warning(f"[{symbol}] SL manquant RÉPARÉ pour '{user_id}' (placé à {effective_sl}).")
                    repaired["sl_repaired"] = True
                else:
                    logger.error(f"[{symbol}] Échec de réparation du SL pour '{user_id}' : {result.error_message}")

        if not get_tp_order_ids(user_id, symbol):
            all_levels = [
                (1, alert.get("tp_50")), (2, alert.get("tp_100")), (3, alert.get("tp_htf_dol")),
            ]
            remaining_levels = [
                price for level_num, price in all_levels if price is not None and level_num > highest_tp_hit
            ]
            if remaining_levels:
                result = client.ensure_take_profit_orders(
                    user_id, symbol, position.side, position.quantity, remaining_levels
                )
                if result.success:
                    logger.warning(
                        f"[{symbol}] TP manquant(s) RÉPARÉ(S) pour '{user_id}' "
                        f"({len(remaining_levels)} niveau(x))."
                    )
                    repaired["tp_repaired"] = True
                else:
                    logger.error(f"[{symbol}] Échec de réparation des TP pour '{user_id}' : {result.error_message}")

        return repaired if (repaired["sl_repaired"] or repaired["tp_repaired"]) else None

    results = _for_each_user(owner_client, owner_exchange, 0.0, _action)
    return {user_id: r for user_id, r in results.items() if r is not None}