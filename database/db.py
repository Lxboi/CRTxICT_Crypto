"""
database/db.py

Couche de persistance SQLite du projet. Trois usages :

  1. Historique complet des décisions (`decisions`) — chaque cycle du
     bot, qu'une alerte soit déclenchée ou non. Nécessaire pour évaluer
     plus tard la qualité des règles (beaucoup de leurs seuils sont des
     heuristiques de départ, pas encore calibrées sur données réelles).
  2. Déduplication des alertes (`alerts`) — évite de renotifier le même
     setup CRT à chaque cycle tant qu'il reste valide (le bot tourne
     toutes les `poll_interval_minutes`, un setup peut rester actif sur
     plusieurs cycles consécutifs).
  3. Snapshots bruts (`market_snapshots`, `raw_ohlc_snapshots`) — pour
     pouvoir rejouer/backtester exactement ce que le bot a vu à un
     instant donné, indépendamment de la logique de décision.

Utilise sqlite3 (bibliothèque standard, aucune dépendance
supplémentaire) plutôt qu'un ORM, pour rester simple à ce stade.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from loguru import logger

from models.market_data import MarketData
from rules.decision_engine import DecisionReport, TimeframeSetup

DB_PATH = Path(__file__).parent / "alerts.db"

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'BTC',
    price_usd REAL NOT NULL,
    price_change_24h_percent REAL,
    volume_24h_usd REAL,
    market_cap_usd REAL
);

CREATE TABLE IF NOT EXISTS raw_ohlc_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'BTC',
    scope TEXT NOT NULL,
    days_param INTEGER NOT NULL,
    raw_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'BTC',
    price_at_decision REAL NOT NULL,
    effective_bias TEXT,
    confidence TEXT NOT NULL,
    monthly_bias TEXT,
    weekly_bias TEXT,
    monthly_weekly_aligned INTEGER NOT NULL,
    narrative_validated_by_liquidation INTEGER NOT NULL,
    entry_recommended INTEGER NOT NULL,
    report_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL DEFAULT 'BTC',
    htf_timeframe TEXT NOT NULL,
    ltf_timeframe TEXT NOT NULL,
    bias TEXT NOT NULL,
    dol_price REAL,
    tp_50 REAL,
    tp_100 REAL,
    tp_htf_dol REAL,
    stop_loss REAL,
    confidence TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    sent_to_telegram INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    closed_at TEXT,
    closed_reason TEXT,
    setup_name TEXT NOT NULL DEFAULT '',
    highest_tp_hit INTEGER NOT NULL DEFAULT 0,
    entry_notified INTEGER NOT NULL DEFAULT 0,
    entry_source TEXT,
    entry_price REAL,
    awaiting_tsq_entry INTEGER NOT NULL DEFAULT 0,
    breakeven_pending INTEGER NOT NULL DEFAULT 0
);
"""

INDEX_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup_key ON alerts(dedup_key);
CREATE INDEX IF NOT EXISTS idx_alerts_created_at ON alerts(created_at);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol);
"""

# Colonnes attendues par table — utilisé par _migrate_missing_columns()
# pour ajouter automatiquement les colonnes manquantes sur une base
# créée avant leur introduction (ex: `symbol`, ajoutée lors du passage
# au multi-paires). CREATE TABLE IF NOT EXISTS ne modifie jamais une
# table déjà existante, d'où le besoin de cette migration légère.
EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "market_snapshots": [("symbol", "TEXT NOT NULL DEFAULT 'BTC'")],
    "raw_ohlc_snapshots": [("symbol", "TEXT NOT NULL DEFAULT 'BTC'")],
    "decisions": [("symbol", "TEXT NOT NULL DEFAULT 'BTC'")],
    "alerts": [
        ("symbol", "TEXT NOT NULL DEFAULT 'BTC'"),
        ("stop_loss", "REAL"),
        ("status", "TEXT NOT NULL DEFAULT 'open'"),
        ("closed_at", "TEXT"),
        ("closed_reason", "TEXT"),
        ("setup_name", "TEXT NOT NULL DEFAULT ''"),
        ("highest_tp_hit", "INTEGER NOT NULL DEFAULT 0"),
        ("entry_notified", "INTEGER NOT NULL DEFAULT 0"),
        ("entry_source", "TEXT"),
        ("entry_price", "REAL"),
        ("awaiting_tsq_entry", "INTEGER NOT NULL DEFAULT 0"),
        ("breakeven_pending", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    Fournit une connexion SQLite avec commit/rollback automatique.
    Chaque appel ouvre et referme sa propre connexion : sqlite3 gère mal
    le partage de connexions entre threads, et le volume d'écritures ici
    (quelques lignes toutes les 15 min) ne justifie pas un pool.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_missing_columns(conn: sqlite3.Connection) -> None:
    """
    Ajoute les colonnes manquantes sur des tables déjà existantes (ex:
    une base créée avant l'introduction de `symbol`). Sans cette étape,
    une base ancienne casse dès qu'on référence une colonne qui
    n'existait pas au moment de sa création — c'est exactement ce qui
    se produisait avant ce correctif.
    """
    for table, expected_columns in EXPECTED_COLUMNS.items():
        existing_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column_name, column_def in expected_columns:
            if column_name not in existing_columns:
                logger.warning(
                    f"Migration : ajout de la colonne '{column_name}' à la table "
                    f"'{table}' (base créée avant son introduction)"
                )
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column_name} {column_def}")


def init_db() -> None:
    """
    Crée les tables si elles n'existent pas encore, migre les colonnes
    manquantes sur une base existante plus ancienne, puis crée les
    index. Idempotent — sans danger à appeler à chaque démarrage.
    """
    with get_connection() as conn:
        conn.executescript(TABLE_SCHEMA)
        _migrate_missing_columns(conn)
        conn.executescript(INDEX_SCHEMA)
    logger.info(f"Base de données initialisée ({DB_PATH})")


def save_market_snapshot(market_data: MarketData) -> None:
    """Enregistre l'instantané de marché du cycle courant (symbole inclus, pour le multi-paires)."""
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO market_snapshots
               (created_at, symbol, price_usd, price_change_24h_percent, volume_24h_usd, market_cap_usd)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                market_data.symbol,
                market_data.price_usd,
                market_data.price_change_percentage_24h,
                market_data.volume_24h_usd,
                market_data.market_cap_usd,
            ),
        )


def save_raw_ohlc_snapshot(
    symbol: str, scope: str, days_param: int, raw_ohlc: list[list[float]]
) -> None:
    """
    Enregistre la réponse brute du collecteur telle quelle (avant tout
    traitement), pour pouvoir reconstruire exactement les mêmes
    CandleSeries plus tard lors d'un backtest.

    Args:
        symbol: Symbole de l'actif (ex: "BTC", "ETH") — nécessaire
            depuis le passage au multi-paires, pour ne pas mélanger les
            snapshots de différents actifs.
        scope: "htf" ou "ltf", selon l'appel d'origine dans main.py.
        days_param: profondeur demandée au collecteur (nombre de
            bougies pour Binance, jours pour l'ancien CoinGecko).
        raw_ohlc: la liste brute [[timestamp_ms, o, h, l, c], ...].
    """
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO raw_ohlc_snapshots (created_at, symbol, scope, days_param, raw_json)
               VALUES (?, ?, ?, ?, ?)""",
            (_now_iso(), symbol, scope, days_param, json.dumps(raw_ohlc)),
        )


def save_decision(report: DecisionReport, price_at_decision: float, symbol: str = "BTC") -> int:
    """
    Sauvegarde le DecisionReport complet (sérialisé en JSON), y compris
    quand aucune alerte n'est déclenchée — c'est ce journal complet qui
    permettra plus tard d'évaluer la qualité des règles sur des données
    réelles.

    Args:
        report: Le DecisionReport à sauvegarder.
        price_at_decision: Prix de l'actif au moment de la décision.
        symbol: Symbole de l'actif analysé (ex: "BTC", "ETH") — le
            DecisionReport ne le porte pas lui-même (il est agnostique
            de l'actif), donc à fournir explicitement depuis
            l'appelant (main.py, qui connaît la paire en cours).

    Returns:
        L'id de la ligne insérée, à réutiliser pour lier une éventuelle
        alerte qui en découle (save_alert_if_new).
    """
    report_json = json.dumps(asdict(report), default=str)

    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO decisions
               (created_at, symbol, price_at_decision, effective_bias, confidence, monthly_bias,
                weekly_bias, monthly_weekly_aligned, narrative_validated_by_liquidation,
                entry_recommended, report_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _now_iso(),
                symbol,
                price_at_decision,
                report.effective_bias,
                report.confidence,
                report.monthly_bias,
                report.weekly_bias,
                int(report.monthly_weekly_aligned),
                int(report.narrative_validated_by_liquidation),
                int(report.entry_recommended),
                report_json,
            ),
        )
        return cursor.lastrowid


def _build_dedup_key(setup: TimeframeSetup, symbol: str) -> str:
    """
    Identifie un setup CRT de façon stable à travers plusieurs cycles :
    même symbole, même timeframe, même biais, même ancre (le prix du
    DOL et le côté de la purge suffisent à distinguer deux CRT
    différents, sans avoir besoin de l'horodatage exact de l'ancre).
    Le symbole est inclus depuis le passage au multi-actifs : sans lui,
    un setup BTC et un setup ETH ayant coïncidemment les mêmes valeurs
    seraient confondus.
    """
    crt = setup.htf_crt or {}
    return "|".join(
        [
            symbol,
            setup.timeframe,
            str(crt.get("bias")),
            str(crt.get("dol_price")),
            str(crt.get("purge_side")),
        ]
    )


def save_alert_if_new(
    decision_id: int,
    setup: TimeframeSetup,
    confidence: str,
    symbol: str = "BTC",
    current_price: float | None = None,
) -> tuple[str, int] | None:
    """
    Enregistre une alerte pour un TimeframeSetup, seulement si un setup
    identique (même symbole) n'a pas déjà été alerté lors d'un cycle
    précédent.

    Args:
        decision_id: id retourné par save_decision() pour ce cycle.
        setup: le TimeframeSetup dont `entry_recommended` est True.
        confidence: le niveau de confiance global du DecisionReport.
        symbol: Symbole de l'actif analysé (ex: "BTC", "ETH").
        current_price: Si fourni, initialise highest_tp_hit selon les
            niveaux DÉJÀ dépassés par le prix actuel au moment de la
            création — un CRT peut rester parfaitement valide (sa cible
            finale, TP2/DOL, n'est jamais vérifiée que sur le prix
            actuel dépassant TP1 avant même la première notification,
            ex: actif de faible valeur/volatil où TP1 (50%) est franchi
            rapidement). Sans ce paramètre, l'alerte partirait toujours
            de highest_tp_hit=0, laissant croire à tort que TP1 est
            "à venir" alors qu'il est déjà consommé.

    Returns:
        Un tuple (dedup_key, initial_highest_tp_hit) si une nouvelle
        alerte a été insérée (à notifier, et à réutiliser avec
        mark_alert_sent() une fois l'envoi confirmé) — l'appelant DOIT
        exploiter initial_highest_tp_hit pour notifier explicitement
        chaque TP déjà dépassé dès la création (voir main.py::
        dispatch_alert), pas seulement le mentionner en passant dans le
        message initial : ne pas notifier explicitement ces
        franchissements laisserait l'utilisateur sans le savoir tant
        qu'aucun NOUVEAU seuil n'est franchi ensuite. Renvoie None si
        l'alerte existait déjà (déduplication) ou si `setup` ne
        recommande pas d'entrée.
    """
    if not setup.entry_recommended or not setup.htf_crt:
        return None

    dedup_key = _build_dedup_key(setup, symbol)
    targets = setup.crt_targets or {}
    bias = setup.htf_crt["bias"]

    initial_highest_tp_hit = 0
    if current_price is not None:
        levels = [
            (1, targets.get("tp_50")),
            (2, targets.get("tp_100")),
            (3, targets.get("tp_htf_dol")),
        ]
        for level_num, tp_price in levels:
            if tp_price is None:
                continue
            already_hit = (bias == "bullish" and current_price >= tp_price) or (
                bias == "bearish" and current_price <= tp_price
            )
            if already_hit:
                initial_highest_tp_hit = level_num

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM alerts WHERE dedup_key = ?", (dedup_key,)
        ).fetchone()
        if existing is not None:
            return None

        conn.execute(
            """INSERT INTO alerts
               (decision_id, created_at, symbol, htf_timeframe, ltf_timeframe, setup_name, bias,
                dol_price, tp_50, tp_100, tp_htf_dol, stop_loss, confidence, dedup_key,
                sent_to_telegram, highest_tp_hit, entry_price)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            (
                decision_id,
                _now_iso(),
                symbol,
                setup.timeframe,
                "",  # vestige : conservé pour compatibilité avec le schéma existant
                     # (colonne NOT NULL sans défaut) ; remplacé par setup_name, non utilisé
                setup.setup_name,
                bias,
                setup.htf_crt["dol_price"],
                targets.get("tp_50"),
                targets.get("tp_100"),
                targets.get("tp_htf_dol"),
                targets.get("stop_loss"),
                confidence,
                dedup_key,
                initial_highest_tp_hit,
                current_price,
            ),
        )

    return dedup_key, initial_highest_tp_hit


def mark_alert_sent(dedup_key: str) -> None:
    """
    À appeler une fois notifications/telegram_sender.py construit, pour
    marquer une alerte comme effectivement envoyée.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET sent_to_telegram = 1 WHERE dedup_key = ?", (dedup_key,)
        )


def get_open_alerts(symbol: str | None = None) -> list[dict[str, Any]]:
    """
    Renvoie les alertes encore "ouvertes" (status='open') — celles dont
    ni le TP ni le SL n'ont encore été touchés, donc à surveiller à
    chaque cycle. Si `symbol` est fourni, filtre sur cet actif.
    """
    with get_connection() as conn:
        if symbol is not None:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status = 'open' AND symbol = ? ORDER BY created_at",
                (symbol,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status = 'open' ORDER BY created_at"
            ).fetchall()
        return [dict(row) for row in rows]


def close_alert(alert_id: int, status: str, reason: str) -> None:
    """
    Clôture une alerte (TP ou SL touché) : passe son status à `status`
    ("closed_tp" ou "closed_sl"), horodate la clôture, et enregistre la
    raison lisible (ex: "TP1 (50%) touché à $92 450.00"). Une fois
    clôturée, l'alerte n'apparaît plus dans get_open_alerts() — la
    "réinitialisation" demandée : un nouveau setup sur le même CRT
    pourra être notifié à nouveau normalement (la détection amont étant
    de toute façon insensible à un CRT déjà atteint, voir le filtre
    anti-obsolescence de rules/crt_rule.py).
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET status = ?, closed_at = ?, closed_reason = ? WHERE id = ?",
            (status, _now_iso(), reason, alert_id),
        )


def close_alert_by_dedup_key(dedup_key: str, status: str, reason: str) -> None:
    """
    Équivalent de close_alert(), mais par dedup_key plutôt que par id —
    utile juste après save_alert_if_new() (voir main.py::dispatch_alert),
    quand on n'a que la dedup_key sous la main, pas l'id auto-incrémenté.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET status = ?, closed_at = ?, closed_reason = ? WHERE dedup_key = ?",
            (status, _now_iso(), reason, dedup_key),
        )


def update_alert_tp_progress(alert_id: int, highest_tp_hit: int) -> None:
    """
    Enregistre qu'un TP intermédiaire a été touché SANS clôturer
    l'alerte — conformément à la méthodologie : "si TP1 touché alors le
    setup reste et le robot doit continuer à prendre en compte ce setup
    avec les prochains objectifs toujours en vue". Seul le TP final
    (voir _get_available_tp_levels) ou le SL clôturent réellement
    l'alerte (close_alert).
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET highest_tp_hit = ? WHERE id = ?",
            (highest_tp_hit, alert_id),
        )


def mark_awaiting_tsq_entry(dedup_key: str) -> None:
    """
    Marque une alerte comme "exécution différée, en attente d'une
    entrée TSQ précise" — cas d'un setup Weekly dont TP1 est déjà
    atteint dès la détection (voir main.py::dispatch_alert) : plutôt
    que d'exécuter au prix courant, le robot attend qu'une vraie
    entrée TSQ (CISD/OTE/Continuation/KOD) confirme, potentiellement
    sur un cycle ultérieur (voir main.py::_check_awaiting_tsq_entries).
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET awaiting_tsq_entry = 1 WHERE dedup_key = ?",
            (dedup_key,),
        )


def clear_awaiting_tsq_entry(dedup_key: str) -> None:
    """À appeler dès qu'une entrée TSQ a été trouvée et exécutée (ou si le setup est autrement clôturé)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET awaiting_tsq_entry = 0 WHERE dedup_key = ?",
            (dedup_key,),
        )


def get_alerts_awaiting_tsq_entry(symbol: str) -> list[dict[str, Any]]:
    """
    Renvoie toutes les alertes OUVERTES de ce symbole en attente d'une
    entrée TSQ — vérifiées à chaque cycle (voir main.py::
    _check_awaiting_tsq_entries) pour savoir si une entrée précise est
    maintenant disponible.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE symbol = ? AND status = 'open' AND awaiting_tsq_entry = 1",
            (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_breakeven_pending(dedup_key: str) -> None:
    """
    Marque une alerte comme "déplacement du SL au Breakeven tenté mais
    échoué" -- bug réel corrigé (13 août 2026, panne Kraken 503 le
    12/08) : ce déplacement n'était tenté qu'UNE SEULE FOIS, au cycle
    exact où TP1 est franchi (voir main.py::_check_tp_sl_hits) ; en cas
    d'échec (ex: panne exchange transitoire), rien ne le retentait
    avant que TP2 soit lui-même atteint -- ce qui peut prendre
    longtemps, ou ne jamais arriver. Ce flag permet à
    main.py::_check_pending_breakeven() de retenter à CHAQUE cycle
    jusqu'à confirmation du déplacement.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET breakeven_pending = 1 WHERE dedup_key = ?",
            (dedup_key,),
        )


def clear_breakeven_pending(dedup_key: str) -> None:
    """À appeler dès que le déplacement du SL au Breakeven a réussi (ou si le setup est autrement clôturé)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET breakeven_pending = 0 WHERE dedup_key = ?",
            (dedup_key,),
        )


def get_alerts_awaiting_breakeven(symbol: str) -> list[dict[str, Any]]:
    """
    Renvoie toutes les alertes OUVERTES de ce symbole dont le
    déplacement du SL au Breakeven a échoué et reste à retenter --
    vérifiées à chaque cycle (voir main.py::_check_pending_breakeven).
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE symbol = ? AND status = 'open' AND breakeven_pending = 1",
            (symbol,),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_entry_notified(alert_id: int, source: str) -> None:
    """
    Marque qu'une entrée précise a été notifiée pour ce setup — évite
    de renotifier en boucle la même entrée à chaque cycle tant qu'elle
    reste d'actualité. Conforme à la consigne : "pas besoin de
    renotifier à chaque fois, le but va être que le robot me notifie la
    première entrée qu'il voit".
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE alerts SET entry_notified = 1, entry_source = ? WHERE id = ?",
            (source, alert_id),
        )


def get_recent_alerts(limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
    """
    Renvoie les dernières alertes enregistrées, les plus récentes
    d'abord. Si `symbol` est fourni, ne renvoie que les alertes de cet
    actif (ex: "BTC" seul, sans les alertes ETH).
    """
    with get_connection() as conn:
        if symbol is not None:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE symbol = ? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]


def get_recent_decisions(limit: int = 20, symbol: str | None = None) -> list[dict[str, Any]]:
    """
    Renvoie les derniers DecisionReport enregistrés (résumé, sans le
    JSON complet). Si `symbol` est fourni, filtre sur cet actif.
    """
    with get_connection() as conn:
        if symbol is not None:
            rows = conn.execute(
                """SELECT id, created_at, symbol, price_at_decision, effective_bias,
                          confidence, entry_recommended
                   FROM decisions WHERE symbol = ? ORDER BY created_at DESC LIMIT ?""",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, created_at, symbol, price_at_decision, effective_bias,
                          confidence, entry_recommended
                   FROM decisions ORDER BY created_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]