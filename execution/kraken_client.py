"""
execution/kraken_client.py

Implémentation concrète de ExchangeClient pour KRAKEN FUTURES
(futures.kraken.com/derivatives/api/v3) — PAS Kraken Pro Spot/Marge, un
produit et une API ENTIÈREMENT SÉPARÉS (base URL, clés API, schéma
d'authentification différents). Remplace l'ancienne implémentation
Spot/Marge de ce fichier (juillet 2026 — le robot passe entièrement en
contrats à terme, levier fixe toujours disponible plutôt que variable
par paire comme en marge Spot).

⚠️ AVERTISSEMENT CRITIQUE — LU AVANT TOUT USAGE RÉEL :
  - Écrit à partir de la documentation publique Kraken Futures, jamais
    exécuté contre l'API réelle. DOIT être testé avec des montants
    minuscules avant tout usage sérieux.
  - Contrairement à Kraken Pro Spot/Marge (qui n'a AUCUN testnet),
    Kraken Futures propose un vrai environnement démo séparé :
    https://demo-futures.kraken.com — ses propres clés API, à générer
    séparément dans la section Futures de votre compte. TESTEZ D'ABORD
    LÀ avant tout compte réel.
  - Symboles au format "PF_XBTUSD" (perpétuels multi-collatéral,
    réglés en USD) — pas "XBTUSD" comme en Spot.
  - Le levier est réglé PAR SYMBOLE via un endpoint dédié
    (leveragepreferences), pas par ordre comme en Spot Kraken —
    passage en marge isolée dès qu'un levier max est précisé.

Authentification (différente du Spot) : SHA-256(postData + Nonce +
endpointPath), puis HMAC-SHA512 avec la clé secrète décodée en
base64, puis encodage base64 du résultat — voir
https://docs.kraken.com/api/docs/guides/futures-rest/
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Literal

import requests
from loguru import logger

from execution.exchange_client import ExchangeClient, OrderResult, Position
from execution.stop_order_registry import get_stop_order_id, save_stop_order_id
from execution.tp_order_registry import save_tp_order_ids

KRAKEN_FUTURES_BASE_URL = "https://futures.kraken.com"
API_PREFIX = "/derivatives/api/v3"


class KrakenClient(ExchangeClient):
    """
    Client Kraken FUTURES. Le stop-loss est placé comme un ordre
    indépendant (orderType="stp"), annulable/remplaçable pour le
    Breakeven — identifiant persisté via execution/stop_order_registry.py
    (jamais en mémoire, survit à un redémarrage du processus).
    """

    def __init__(self, api_key: str, api_secret: str) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._contract_specs_cache: dict[str, tuple[int, float]] = {}

    def _sign(self, endpoint_path: str, nonce: str, post_data: str) -> str:
        """SHA-256(postData + Nonce + endpointPath), puis HMAC-SHA512, puis base64 -- voir docstring du module."""
        sha256_digest = hashlib.sha256((post_data + nonce + endpoint_path).encode()).digest()
        secret_decoded = base64.b64decode(self._api_secret)
        signature = hmac.new(secret_decoded, sha256_digest, hashlib.sha512)
        return base64.b64encode(signature.digest()).decode()

    def _private_request(
        self, method: str, endpoint: str, params: dict | None = None, send_as_query_params: bool = False
    ) -> dict:
        """
        Appelle un endpoint privé Kraken Futures (signé). Lève
        requests.HTTPError ou ValueError en cas d'échec réseau/API —
        capturé par execution/engine.py, jamais laissé remonter jusqu'à
        faire planter le cycle du robot.

        Args:
            send_as_query_params: La quasi-totalité des endpoints privés
                Kraken Futures (/sendorder, /cancelorder...) attendent
                leurs paramètres dans le CORPS de la requête, form-encodé
                -- comportement par défaut (False). MAIS certains
                endpoints, notamment /leveragepreferences (PUT), les
                exigent en PARAMÈTRES DE REQUÊTE dans l'URL elle-même
                (confirmé par la spec OpenAPI officielle Kraken : `symbol`
                et `maxLeverage` déclarés `in: query`, jamais dans le
                corps) -- sans ce paramètre à True pour cet endpoint
                précis, Kraken répond 404 (bug réel corrigé, août 2026).
                La signature (Authent) reste calculée EXACTEMENT de la
                même façon dans les deux cas (voir post_data ci-dessous)
                -- seul le TRANSPORT effectif de la requête change.
        """
        params = dict(params or {})
        # IMPORTANT : le endpointPath utilisé pour la SIGNATURE ne
        # comprend PAS le préfixe "/derivatives" (voir la doc Kraken :
        # exemple donné "/api/v3/orderbook", pas "/derivatives/api/v3/
        # orderbook") -- alors que l'URL de la REQUÊTE, elle, l'inclut
        # bien. Deux chemins différents pour deux usages différents.
        signing_path = f"/api/v3{endpoint}"
        request_path = f"{API_PREFIX}{endpoint}"
        nonce = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(params)

        headers = {
            "APIKey": self._api_key,
            "Nonce": nonce,
            "Authent": self._sign(signing_path, nonce, post_data),
        }

        url = f"{KRAKEN_FUTURES_BASE_URL}{request_path}"
        if method == "GET" or send_as_query_params:
            response = requests.request(method, url, headers=headers, params=params, timeout=10)
        else:
            response = requests.request(method, url, headers=headers, data=params, timeout=10)
        response.raise_for_status()
        result = response.json()

        if result.get("result") != "success":
            raise ValueError(f"Erreur API Kraken Futures sur {endpoint} : {result}")

        return result

    def _get_contract_specs(self, futures_symbol: str) -> tuple[int, float]:
        """
        Renvoie (précision de taille, tickSize du prix) pour ce
        contrat — endpoint public GET /instruments, mis en cache par
        instance de client (le mapping ne change pas en cours de
        route) pour éviter un appel réseau supplémentaire à chaque ordre.

        Sans cet arrondi, Kraken Futures répond 'invalidSize' pour une
        taille avec trop de décimales, et une erreur équivalente pour
        un prix (stop-loss) qui ne respecte pas le tickSize du contrat.
        """
        if futures_symbol not in self._contract_specs_cache:
            response = requests.get(
                f"{KRAKEN_FUTURES_BASE_URL}{API_PREFIX}/instruments", timeout=10
            )
            response.raise_for_status()
            data = response.json()
            precision, tick_size = 8, 0.01  # repli prudent si le symbole est introuvable (rarissime)
            for instrument in data.get("instruments", []):
                if instrument.get("symbol") == futures_symbol:
                    precision = int(instrument.get("contractValueTradePrecision", 8))
                    tick_size = float(instrument.get("tickSize", 0.01))
                    break
            self._contract_specs_cache[futures_symbol] = (precision, tick_size)

        return self._contract_specs_cache[futures_symbol]

    @staticmethod
    def _round_to_tick(price: float, tick_size: float) -> float:
        """Arrondit un prix au tickSize le plus proche (obligatoire côté Kraken Futures, sinon ordre refusé)."""
        if tick_size <= 0:
            return price
        return round(round(price / tick_size) * tick_size, 10)

    def place_market_order(
        self, user_id: str, symbol: str, side: Literal["long", "short"], quantity: float,
        stop_loss: float, leverage: float, take_profit_levels: list[float] | None = None,
    ) -> OrderResult:
        futures_symbol = f"PF_{symbol}USD"
        order_side = "buy" if side == "long" else "sell"
        opposite_side = "sell" if side == "long" else "buy"

        precision, tick_size = self._get_contract_specs(futures_symbol)
        quantity = round(quantity, precision)
        if precision == 0:
            quantity = int(quantity)
        stop_loss = self._round_to_tick(stop_loss, tick_size)

        try:
            # Levier réglé AVANT l'ordre -- passage en marge isolée sur
            # ce symbole avec le plafond demandé (voir docstring du module).
            # send_as_query_params=True : cet endpoint précis exige ses
            # paramètres dans l'URL, pas dans le corps (voir docstring
            # de _private_request -- bug réel corrigé, 404 sinon).
            self._private_request(
                "PUT", "/leveragepreferences", {"symbol": futures_symbol, "maxLeverage": leverage},
                send_as_query_params=True,
            )

            entry_result = self._private_request(
                "POST", "/sendorder",
                {"orderType": "mkt", "symbol": futures_symbol, "side": order_side, "size": quantity},
            )
            send_status = entry_result.get("sendStatus", {})
            if send_status.get("status") != "placed":
                raise ValueError(f"Ordre non placé : {send_status}")
            entry_order_id = send_status["order_id"]

            # Stop-loss en ordre INDÉPENDANT (orderType="stp"), pour
            # rester librement annulable/remplaçable ensuite (Breakeven).
            stop_result = self._private_request(
                "POST", "/sendorder",
                {
                    "orderType": "stp", "symbol": futures_symbol, "side": opposite_side,
                    "size": quantity, "stopPrice": stop_loss, "reduceOnly": "true",
                },
            )
            stop_status = stop_result.get("sendStatus", {})
            if stop_status.get("status") != "placed":
                raise ValueError(f"Ordre stop non placé : {stop_status}")
            save_stop_order_id(user_id, symbol, stop_status["order_id"])

            # Ordres take-profit RÉELS (reduce-only) -- sécurité : ils
            # s'exécutent sur l'exchange même si le robot s'arrête,
            # sans dépendre de sa boucle de surveillance du prix.
            if take_profit_levels:
                self._place_tp_orders(user_id, symbol, futures_symbol, opposite_side, quantity, take_profit_levels)

            return OrderResult(success=True, order_id=entry_order_id, filled_price=None, filled_quantity=quantity)
        except (requests.HTTPError, ValueError, KeyError) as e:
            logger.error(f"[Kraken Futures] Échec place_market_order({symbol}) : {e}")
            return OrderResult(
                success=False, order_id=None, filled_price=None, filled_quantity=None,
                error_message=str(e),
            )

    def _place_tp_orders(
        self, user_id: str, symbol: str, futures_symbol: str, opposite_side: str,
        quantity: float, take_profit_levels: list[float],
    ) -> None:
        """Logique de placement des TP partagée entre place_market_order() et ensure_take_profit_orders()."""
        from execution.position_sizing import split_quantity_by_tp_levels

        precision, tick_size = self._get_contract_specs(futures_symbol)
        tp_quantities = split_quantity_by_tp_levels(quantity, len(take_profit_levels), precision)
        tp_order_ids = []
        for tp_price, tp_quantity in zip(take_profit_levels, tp_quantities):
            tp_price_rounded = self._round_to_tick(tp_price, tick_size)
            tp_qty_rounded = round(tp_quantity, precision)
            if precision == 0:
                tp_qty_rounded = int(tp_qty_rounded)
            try:
                tp_result = self._private_request(
                    "POST", "/sendorder",
                    {
                        "orderType": "lmt", "symbol": futures_symbol, "side": opposite_side,
                        "size": tp_qty_rounded, "limitPrice": tp_price_rounded, "reduceOnly": "true",
                    },
                )
                tp_status = tp_result.get("sendStatus", {})
                if tp_status.get("status") == "placed":
                    tp_order_ids.append(tp_status["order_id"])
                else:
                    logger.error(
                        f"[Kraken Futures] Échec de placement d'un ordre TP à {tp_price} "
                        f"pour {symbol} : {tp_status} -- les autres TP/SL restent actifs."
                    )
            except (requests.HTTPError, ValueError, KeyError) as e:
                logger.error(f"[Kraken Futures] Erreur lors du placement d'un ordre TP à {tp_price} : {e}")
        if tp_order_ids:
            save_tp_order_ids(user_id, symbol, tp_order_ids)

    def ensure_take_profit_orders(
        self, user_id: str, symbol: str, side: Literal["long", "short"],
        quantity: float, take_profit_levels: list[float],
    ) -> OrderResult:
        futures_symbol = f"PF_{symbol}USD"
        opposite_side = "sell" if side == "long" else "buy"
        try:
            self._place_tp_orders(user_id, symbol, futures_symbol, opposite_side, quantity, take_profit_levels)
            return OrderResult(success=True, order_id=None, filled_price=None, filled_quantity=quantity)
        except Exception as e:
            logger.error(f"[Kraken Futures] Échec ensure_take_profit_orders({symbol}) : {e}")
            return OrderResult(
                success=False, order_id=None, filled_price=None, filled_quantity=None,
                error_message=str(e),
            )

    def move_stop_loss(self, user_id: str, symbol: str, new_stop_loss: float) -> OrderResult:
        # Si aucun stop n'est enregistré, ce n'est PAS un échec : ça
        # signifie simplement qu'aucun n'existe encore à annuler --
        # utilisé aussi bien pour un déplacement normal (Breakeven) que
        # pour RÉPARER une position dont le stop manquerait (voir
        # main.py::_check_position_protection) -- dans ce cas, on place
        # directement un nouveau stop, sans tenter d'en annuler un.
        old_order_id = get_stop_order_id(user_id, symbol)

        futures_symbol = f"PF_{symbol}USD"
        _, tick_size = self._get_contract_specs(futures_symbol)
        new_stop_loss = self._round_to_tick(new_stop_loss, tick_size)

        try:
            if old_order_id is not None:
                self._private_request("POST", "/cancelorder", {"order_id": old_order_id})

            positions = self.get_open_positions()
            position = next((p for p in positions if p.symbol == symbol), None)
            if position is None:
                return OrderResult(
                    success=False, order_id=None, filled_price=None, filled_quantity=None,
                    error_message=f"Aucune position ouverte trouvée pour {symbol} après annulation du stop.",
                )

            opposite_side = "sell" if position.side == "long" else "buy"
            new_stop_result = self._private_request(
                "POST", "/sendorder",
                {
                    "orderType": "stp", "symbol": futures_symbol, "side": opposite_side,
                    "size": position.quantity, "stopPrice": new_stop_loss, "reduceOnly": "true",
                },
            )
            new_status = new_stop_result.get("sendStatus", {})
            if new_status.get("status") != "placed":
                raise ValueError(f"Nouvel ordre stop non placé : {new_status}")
            save_stop_order_id(user_id, symbol, new_status["order_id"])

            return OrderResult(
                success=True, order_id=new_status["order_id"],
                filled_price=new_stop_loss, filled_quantity=None,
            )
        except (requests.HTTPError, ValueError, KeyError) as e:
            logger.error(
                f"[Kraken Futures] Échec move_stop_loss({symbol}) : {e} -- POSITION "
                "POTENTIELLEMENT SANS STOP-LOSS ACTIF, vérification manuelle requise immédiatement."
            )
            return OrderResult(
                success=False, order_id=None, filled_price=None, filled_quantity=None,
                error_message=str(e),
            )

    def close_position(self, user_id: str, symbol: str, fraction: float = 1.0) -> OrderResult:
        futures_symbol = f"PF_{symbol}USD"
        try:
            positions = self.get_open_positions()
            position = next((p for p in positions if p.symbol == symbol), None)
            if position is None:
                return OrderResult(
                    success=False, order_id=None, filled_price=None, filled_quantity=None,
                    error_message=f"Aucune position ouverte pour {symbol}.",
                )

            precision, _ = self._get_contract_specs(futures_symbol)
            quantity_to_close = round(position.quantity * fraction, precision)
            if precision == 0:
                quantity_to_close = int(quantity_to_close)

            opposite_side = "sell" if position.side == "long" else "buy"
            result = self._private_request(
                "POST", "/sendorder",
                {
                    "orderType": "mkt", "symbol": futures_symbol, "side": opposite_side,
                    "size": quantity_to_close, "reduceOnly": "true",
                },
            )
            status = result.get("sendStatus", {})
            if status.get("status") != "placed":
                raise ValueError(f"Ordre de clôture non placé : {status}")

            if fraction >= 1.0:
                self._cancel_remaining_tp_orders(user_id, symbol)

            return OrderResult(success=True, order_id=status["order_id"], filled_price=None, filled_quantity=0)
        except (requests.HTTPError, ValueError, KeyError) as e:
            logger.error(f"[Kraken Futures] Échec close_position({symbol}) : {e}")
            return OrderResult(
                success=False, order_id=None, filled_price=None, filled_quantity=None,
                error_message=str(e),
            )

    def _cancel_remaining_tp_orders(self, user_id: str, symbol: str) -> None:
        """Annule tout ordre take-profit encore en attente pour ce symbole -- voir avertissement dans close_position()."""
        from execution.tp_order_registry import clear_tp_order_ids, get_tp_order_ids

        for tp_order_id in get_tp_order_ids(user_id, symbol):
            try:
                self._private_request("POST", "/cancelorder", {"order_id": tp_order_id})
            except (requests.HTTPError, ValueError) as e:
                logger.warning(
                    f"[Kraken Futures] Échec d'annulation de l'ordre TP {tp_order_id} pour "
                    f"{symbol} (déjà exécuté ou expiré probablement) : {e}"
                )
        clear_tp_order_ids(user_id, symbol)

    def get_open_positions(self) -> list[Position]:
        result = self._private_request("GET", "/openpositions")
        positions = []
        for pos in result.get("openPositions", []):
            symbol = pos["symbol"].replace("PF_", "").replace("USD", "")
            positions.append(
                Position(
                    symbol=symbol,
                    side="long" if pos["side"] == "long" else "short",
                    entry_price=float(pos.get("price", 0.0)),
                    quantity=float(pos.get("size", 0.0)),
                    unrealized_pnl=float(pos.get("unrealizedPnl", 0.0)),
                    leverage=float(pos.get("leverage", 1)),
                )
            )
        return positions

    def get_account_balance(self) -> float:
        result = self._private_request("GET", "/accounts")
        # Compte multi-collatéral "flex" -- solde disponible en USD.
        flex_account = result.get("accounts", {}).get("flex", {})
        return float(flex_account.get("availableMargin", 0.0))
