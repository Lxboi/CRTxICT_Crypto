"""
execution/position_sizing.py

Calcule la taille de position à partir d'un pourcentage de risque du
capital disponible — jamais une taille fixe, pour que le risque réel
pris reste cohérent quelle que soit la taille du compte.
"""

from __future__ import annotations


def compute_position_size(
    account_balance: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
) -> float:
    """
    Calcule la quantité à acheter/vendre pour risquer exactement
    `risk_pct` % du capital si le SL est touché.

    Args:
        account_balance: Solde du compte (devise de cotation, ex: USDT).
        risk_pct: Pourcentage du capital à risquer sur ce trade (ex: 1.0
            pour 1%) — JAMAIS le % de la position elle-même, uniquement
            le risque réel (distance jusqu'au SL).
        entry_price: Prix d'entrée prévu.
        stop_loss: Niveau du stop-loss.

    Returns:
        Quantité de l'actif à acheter/vendre. 0.0 si le calcul n'est
        pas possible (SL égal au prix d'entrée, paramètres invalides).

    Raises:
        ValueError: Si account_balance ou risk_pct est négatif ou nul,
            ou si entry_price est négatif ou nul — des valeurs
            invalides ici ne doivent jamais produire silencieusement
            une taille de position incorrecte.
    """
    if account_balance <= 0:
        raise ValueError(f"account_balance doit être positif, reçu : {account_balance}")
    if risk_pct <= 0:
        raise ValueError(f"risk_pct doit être positif, reçu : {risk_pct}")
    if entry_price <= 0:
        raise ValueError(f"entry_price doit être positif, reçu : {entry_price}")

    risk_amount = account_balance * (risk_pct / 100)
    price_distance = abs(entry_price - stop_loss)

    if price_distance == 0:
        return 0.0

    return risk_amount / price_distance


def compute_required_leverage(
    quantity: float, entry_price: float, account_balance: float, max_leverage: float,
    reserved_balance_pct: float = 0.0,
) -> float:
    """
    Calcule le levier MINIMUM nécessaire pour pouvoir réellement ouvrir
    une position de `quantity` unités avec le capital disponible comme
    marge — le levier ne change PAS le risque réel pris (toujours
    déterminé par compute_position_size, indépendamment du levier),
    il détermine seulement la marge nécessaire pour pouvoir ouvrir
    cette taille de position.

    Args:
        quantity: Quantité à ouvrir (voir compute_position_size()).
        entry_price: Prix d'entrée prévu.
        account_balance: Solde du compte (marge disponible TOTALE,
            réserve non exclue -- voir reserved_balance_pct).
        max_leverage: Plafond de sécurité (voir
            config/alerts.yaml::execution.max_leverage) — jamais
            dépassé, même si la taille de position le demanderait.
        reserved_balance_pct: % du solde à ne JAMAIS utiliser comme
            marge (voir config/alerts.yaml::execution.reserved_balance_pct)
            — la base de calcul du levier utilise
            `account_balance * (1 - reserved_balance_pct/100)`, jamais
            le solde total. Sans cette réserve, une position peut par
            construction consommer la QUASI-TOTALITÉ du solde comme
            marge (le levier est calculé pour occuper tout le solde
            disponible), ne laissant plus rien pour d'autres trades
            simultanés sur d'autres actifs suivis.

    Returns:
        Le levier nécessaire, arrondi au niveau supérieur pour être sûr
        de couvrir la marge requise, plafonné à `max_leverage`.

    Raises:
        ValueError: Si le levier nécessaire dépasse max_leverage, OU
            si reserved_balance_pct est hors de [0, 100) -- une
            combinaison risque%/distance de SL/réserve demandée n'est
            pas atteignable dans la limite de levier fixée ; mieux vaut
            échouer bruyamment que d'ouvrir une position avec un levier
            au-delà du plafond de sécurité choisi, ou d'entamer la
            réserve censée rester intouchée.
    """
    if not (0 <= reserved_balance_pct < 100):
        raise ValueError(f"reserved_balance_pct doit être dans [0, 100), reçu : {reserved_balance_pct}")

    usable_balance = account_balance * (1 - reserved_balance_pct / 100)

    notional = quantity * entry_price
    if notional <= 0 or usable_balance <= 0:
        return 1.0

    required = notional / usable_balance
    required_leverage = max(1.0, required)

    if required_leverage > max_leverage:
        raise ValueError(
            f"Levier nécessaire ({required_leverage:.1f}x) dépasse le plafond de sécurité "
            f"configuré ({max_leverage:.1f}x) -- réduisez risk_pct_per_trade, augmentez "
            f"max_leverage en connaissance de cause, ou réduisez reserved_balance_pct "
            f"(actuellement {reserved_balance_pct:.0f}%)."
        )

    return required_leverage


def split_quantity_by_tp_levels(total_quantity: float, n_levels: int, precision: int = 8) -> list[float]:
    """
    Divise `total_quantity` en `n_levels` parts pour placer un ordre
    take-profit réel par niveau (voir ExchangeClient.place_market_order::
    take_profit_levels) — parts égales, sauf la DERNIÈRE qui absorbe le
    reste de l'arrondi, pour que la somme corresponde exactement à
    `total_quantity` (jamais de "poussière" oubliée par accumulation
    d'arrondis successifs).

    Args:
        total_quantity: Quantité totale de la position à répartir.
        n_levels: Nombre de niveaux de TP disponibles pour ce setup.
        precision: Nombre de décimales autorisées par l'exchange pour
            cet actif (voir *_client.py::_get_*_specs) — sans cet
            arrondi, la somme des parts pourrait légèrement dépasser
            ou être inférieure à `total_quantity`.

    Returns:
        Liste de `n_levels` quantités, dans le même ordre que les
        niveaux de TP fournis (le plus proche en premier).

    Raises:
        ValueError: Si n_levels <= 0.
    """
    if n_levels <= 0:
        raise ValueError(f"n_levels doit être positif, reçu : {n_levels}")

    equal_share = round(total_quantity / n_levels, precision)
    shares = [equal_share] * (n_levels - 1)
    shares.append(round(total_quantity - sum(shares), precision))
    return shares
