"""
tests/test_historical_loader.py

Tests pour backtest/historical_loader.py::fetch_historical_multi_timeframe()
-- en particulier le paramètre include_m15 (Phase 1 du backtest réaliste,
août 2026) : False par défaut (comportement Daily/CRT historique
inchangé), True pour le futur simulateur intraday (Phase 2), qui
réutilise fetch_paginated_klines() déjà générique sur l'intervalle,
sans code de pagination supplémentaire.

Aucun appel réseau réel : requests.get est remplacé par un faux qui
enregistre les paramètres reçus et renvoie un petit lot de klines
(moins de 1000 -> une seule page, la boucle de pagination s'arrête
immédiatement).
"""

from __future__ import annotations

from backtest.historical_loader import fetch_historical_multi_timeframe


def _fake_kline(open_time_ms: int) -> list:
    """Kline brute Binance minimale valide (voir collectors/binance.py::_convert_kline_to_simple)."""
    return [open_time_ms, "100.0", "101.0", "99.0", "100.5", "10.0", open_time_ms + 1, "0", 0, "0", "0", "0"]


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _install_fake_get(monkeypatch, calls: list[dict]):
    def fake_get(url, params=None, timeout=None):
        calls.append(dict(params))
        # Une seule bougie -> moins de MAX_KLINES_PER_REQUEST (1000) ->
        # fetch_paginated_klines() considère que c'est la dernière page
        # et s'arrête (pas de boucle infinie, pas de sleep entre pages).
        return _FakeResponse([_fake_kline(params["startTime"])])

    monkeypatch.setattr("backtest.historical_loader.requests.get", fake_get)


def test_include_m15_false_by_default_does_not_fetch_m15(monkeypatch):
    calls: list[dict] = []
    _install_fake_get(monkeypatch, calls)

    result = fetch_historical_multi_timeframe("BTC", years_of_history=1, margin_years=0)

    assert "m15" not in result
    assert set(result.keys()) == {"monthly", "weekly", "daily"}
    intervals_requested = {c["interval"] for c in calls}
    assert "15m" not in intervals_requested


def test_include_m15_true_fetches_m15_on_same_window(monkeypatch):
    calls: list[dict] = []
    _install_fake_get(monkeypatch, calls)

    result = fetch_historical_multi_timeframe(
        "BTC", years_of_history=1, margin_years=0, include_m15=True
    )

    assert "m15" in result
    assert set(result.keys()) == {"monthly", "weekly", "daily", "m15"}
    assert len(result["m15"]) == 1  # une bougie renvoyée par le faux endpoint

    m15_calls = [c for c in calls if c["interval"] == "15m"]
    assert len(m15_calls) == 1
    other_calls = [c for c in calls if c["interval"] != "15m"]
    # m15 interrogé sur la MÊME fenêtre temporelle que les autres timeframes.
    assert m15_calls[0]["startTime"] == other_calls[0]["startTime"]
    assert m15_calls[0]["endTime"] == other_calls[0]["endTime"]
    assert m15_calls[0]["symbol"] == "BTCUSDT"


def test_include_m15_true_uses_futures_market_when_requested(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, params=None, timeout=None):
        calls.append({"url": url, **params})
        return _FakeResponse([_fake_kline(params["startTime"])])

    monkeypatch.setattr("backtest.historical_loader.requests.get", fake_get)

    fetch_historical_multi_timeframe(
        "BTC", years_of_history=1, margin_years=0, include_m15=True, market="futures"
    )

    m15_calls = [c for c in calls if c["interval"] == "15m"]
    assert len(m15_calls) == 1
    assert "fapi.binance.com" in m15_calls[0]["url"]


class TestFetchPaginatedIntradayPolygon:
    """
    Tests pour backtest/historical_loader.py::fetch_paginated_intraday_polygon()
    (Phase 1, août 2026) -- Polygon renvoie tout un historique en UN
    SEUL appel (contrairement à Binance), mais plafonné à 50 000 lignes
    -- au-delà de quelques dizaines de jours de m15, plusieurs années
    d'historique dépassent ce plafond. Cette fonction découpe la
    période en blocs. Aucun appel réseau réel : collectors.polygon.
    get_aggregates est remplacé par un faux qui enregistre les dates
    reçues.

    ⚠️ Ne teste QUE la logique de découpage -- pas la profondeur
    d'historique RÉELLEMENT disponible sur le plan Polygon gratuit
    (inconnue à ce jour, voir avertissement dans historical_loader.py).
    """

    def test_splits_long_range_into_multiple_chunks(self, monkeypatch):
        from datetime import date

        import backtest.historical_loader as loader

        monkeypatch.setattr(loader, "_POLYGON_TARGET_ROWS_PER_CHUNK", 96 * 3)  # 3 jours/bloc à 15 min

        requested_ranges: list[tuple[str, str]] = []

        def fake_get_aggregates(ticker, multiplier, timespan, from_date, to_date, limit):
            requested_ranges.append((from_date, to_date))
            return [{"t": 0, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]

        monkeypatch.setattr("collectors.polygon.get_aggregates", fake_get_aggregates)

        result = loader.fetch_paginated_intraday_polygon(
            "C:EURUSD", date(2026, 1, 1), date(2026, 1, 10), multiplier=15
        )

        # 10 jours / 3 jours-par-bloc -> 4 blocs (3+3+3+1), aucun trou ni chevauchement.
        assert requested_ranges == [
            ("2026-01-01", "2026-01-03"),
            ("2026-01-04", "2026-01-06"),
            ("2026-01-07", "2026-01-09"),
            ("2026-01-10", "2026-01-10"),
        ]
        assert len(result) == 4  # une bougie par bloc dans ce faux

    def test_single_chunk_when_range_fits(self, monkeypatch):
        from datetime import date

        import backtest.historical_loader as loader

        requested_ranges: list[tuple[str, str]] = []

        def fake_get_aggregates(ticker, multiplier, timespan, from_date, to_date, limit):
            requested_ranges.append((from_date, to_date))
            return [
                {"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
                {"t": 2000, "o": 1, "h": 2, "l": 0.5, "c": 1.5},
            ]

        monkeypatch.setattr("collectors.polygon.get_aggregates", fake_get_aggregates)

        result = loader.fetch_paginated_intraday_polygon(
            "C:EURUSD", date(2026, 1, 1), date(2026, 1, 2), multiplier=15
        )

        assert len(requested_ranges) == 1
        assert result == [[1000, 1, 2, 0.5, 1.5], [2000, 1, 2, 0.5, 1.5]]


def test_fetch_historical_multi_timeframe_polygon_include_m15(monkeypatch):
    import backtest.historical_loader as loader

    monkeypatch.setattr(
        loader,
        "fetch_paginated_intraday_polygon",
        lambda ticker, start_date, end_date, multiplier=15: [[123, 1, 2, 0.5, 1.5]],
    )

    def fake_get_aggregates(ticker, multiplier, timespan, from_date, to_date, limit):
        return [{"t": 0, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]

    monkeypatch.setattr("collectors.polygon.get_aggregates", fake_get_aggregates)

    result = loader.fetch_historical_multi_timeframe_polygon(
        "C:EURUSD", years_of_history=1, margin_years=0, include_m15=True
    )

    assert "m15" in result
    assert result["m15"] == [[123, 1, 2, 0.5, 1.5]]
    assert set(result.keys()) == {"monthly", "weekly", "daily", "m15"}


def test_fetch_historical_multi_timeframe_polygon_include_m15_false_by_default(monkeypatch):
    import backtest.historical_loader as loader

    def fake_get_aggregates(ticker, multiplier, timespan, from_date, to_date, limit):
        return [{"t": 0, "o": 1, "h": 2, "l": 0.5, "c": 1.5}]

    monkeypatch.setattr("collectors.polygon.get_aggregates", fake_get_aggregates)

    result = loader.fetch_historical_multi_timeframe_polygon(
        "C:EURUSD", years_of_history=1, margin_years=0
    )

    assert "m15" not in result
