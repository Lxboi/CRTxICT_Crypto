# Trading_alert_bot — État du projet (mise à jour 17 août 2026)

## ⚠️ Simplification architecture (17 août 2026)

Changement majeur, décision explicite de l'utilisateur : le robot est
recentré **crypto uniquement** (Binance pour la collecte/analyse),
exécution sur **SON SEUL compte Kraken** (IBKR/OKX/Binance-exécution
retirés), et le **multi-utilisateur/facturation tierce a été retiré**
(plus d'`accounts/`, `add_user_account.py`, `manage_user_account.py`,
`generate_fee_report.py`). Stratégie, kill-switch et filtres
d'exécution (R:R minimum, garde-fous de levier) **inchangés**. Détail
complet (fichiers modifiés/supprimés/déplacés, décisions prises) dans
`CLAUDE.md` (section en tête de fichier) et le doc projet
`claude/architecture-simplification-plan.md` (côté claude.ai).

Tout le contenu ci-dessous décrit l'état APRÈS ce refactor.

## Pipeline global

```
Binance (crypto)
    → Collecte → Modèles
    → Moteur de règles (cascade DOL, CRT multi-timeframe, Narrative, Purge, Entrées)
    → DecisionEngine → Base de données → Telegram → Exécution automatique (Kraken)
```

**Aucune IA dans le pipeline** — testée puis entièrement retirée
(`ai/analyzer.py`, `ai/prompts.py` supprimés ; le dossier `ai/`
lui-même a depuis été renommé en `messaging/`, il ne contenait plus
que le formatage). Le message final est construit de façon 100%
déterministe par `messaging/formatter.py`.

## Actifs suivis

BTC, ETH, ADA, TRX, BNB, DOT, SUI — tous via Binance FUTURES (voir
`config/alerts.yaml::orchestration.market`). Seule source active
depuis la simplification du 17/08 : `collectors/polygon.py` (Forex/
CFD/indices) reste disponible dans le repo pour un usage futur, mais
aucun actif n'y est configuré.

## Ce qui est fait

### `collectors/`
| Fichier | État | Contenu |
|---|---|---|
| `binance.py` | ✅ | Klines multi-timeframe (monthly/weekly/daily/intraday 1h/m15 15min), ticker 24h. Aucune clé requise. **Spot ET Futures** (`market="spot"/"futures"`) — contrôlé par `config/alerts.yaml::orchestration.market`, l'analyse se fait sur les mêmes prix que l'exécution. |
| `polygon.py` | ✅ disponible, dormant | Forex/Indices, multi-timeframe. Conservé pour un usage futur — aucun actif configuré depuis le 17/08. |
| `macro.py` | ✅ | SoSoValue (calendrier économique) |
| `ibkr.py` | ❌ supprimé (17/08) | Retiré avec le reste d'IBKR (exécution + collecte) |
| `coingecko.py`, `news.py`, `reddit.py`, `etf.py` | ❌ supprimés | Jamais utilisés en production ou devenus redondants |

### `models/`
| Fichier | État |
|---|---|
| `market_data.py` | ✅ Candle, CandleSeries, MarketData, `format_price_adaptive()` (décimales selon l'ordre de grandeur du prix) |
| `macro_data.py` | ✅ MacroEvent |
| `sentiment_data.py` | ❌ supprimé (dépendait de `news.py`, supprimé) |

### `rules/` — moteur de règles complet
Tous les fichiers sont fonctionnels et testés (voir `tests/`).
`purge_rule.py::resolve_asset_category()` retourne systématiquement
`"crypto"` depuis le 17/08 (ne dépend plus de l'ex-`execution/
ibkr_contracts.py`) — voir `CLAUDE.md` pour l'historique complet des
corrections majeures (CRT, Narrative, Decision Engine, SMT).

### `messaging/` (renommé depuis `ai/` — ne contient plus aucune IA)
| Fichier | État |
|---|---|
| `formatter.py` | ✅ Message Telegram complet : DOL long terme (avec justification de la timeframe), Narrative (jours observés + provisoire si incomplet), calendrier économique toujours visible, **SMT** (si divergence détectée), KL HTF classées par puissance, Setup + entrée précise + **R:R** intégrés, **Biais BTC (référence)** pour tout actif autre que BTC |

### `database/`
`db.py` : SQLite, tables décisions/alertes/snapshots. Déduplication par
setup (nom stable), suivi progressif des TP (`highest_tp_hit`, initialisé
correctement si déjà dépassé à la création), suivi des entrées notifiées
(`entry_notified`) pour éviter les répétitions. **Mécanique Breakeven** :
`entry_price` stocké à la création, SL "effectif" déplacé au prix
d'entrée dès TP1 touché — notification dédiée, distincte d'un SL
classique. Aucune colonne `user_id` — indexé purement par symbole,
totalement inaffecté par le retrait du multi-utilisateur.

### `notifications/`
`telegram_sender.py` : envoi Markdown (mode legacy — pas de soulignement
disponible, gras utilisé à la place).

### `config/`
- `alerts.yaml` : 7 actifs crypto, seuils de calibrage par règle (voir
  commentaires inline pour la justification de chaque valeur).
  `correlated_pairs` : BTC<->ETH fixé en dur (protégé de la recherche
  dynamique) ; tous les autres actifs sont appariés automatiquement par
  `rules/correlation.py::find_correlated_pairs`. `execution.exchanges`
  réduit à `kraken` uniquement depuis le 17/08. Section `saas:` (frais
  de performance) supprimée avec le multi-utilisateur.
- `settings.py` : variables d'environnement — Telegram, Kraken,
  Polygon (dormant), SoSoValue. `binance_api_key`/`binance_api_secret`
  et tous les champs `ibkr_*` retirés le 17/08 (ne servaient qu'à
  l'exécution Binance/IBKR, toutes deux supprimées).

### `execution/`
| Fichier | État |
|---|---|
| `kill_switch.py` | ✅ Fail-safe strict (jamais activé par défaut, même si fichier corrompu). Indexé par user_id (toujours `"_owner"` désormais) — inchangé par le retrait du multi-utilisateur. |
| `exchange_client.py` | ✅ Interface abstraite (`ExchangeClient`) — une seule implémentation concrète désormais (Kraken) |
| `kraken_client.py` | ✅ Kraken **FUTURES** (pas Spot/Marge, API et clés totalement séparées) — écrit à partir de la doc publique, **jamais exécuté contre l'API réelle** (pas d'accès réseau côté Claude). Testnet dédié disponible (`demo-futures.kraken.com`) |
| `client_factory.py` | ✅ Simplifié (17/08) : Kraken uniquement |
| `position_sizing.py` | ✅ Taille de position sur % de risque du capital ; levier calculé dynamiquement, refus propre si au-delà du plafond configuré |
| `stop_order_registry.py` / `tp_order_registry.py` | ✅ Identifiants d'ordres PERSISTÉS par utilisateur/symbole — survivent à un redémarrage du processus |
| `engine.py` | ✅ Simplifié (17/08) : `_for_each_user()` agit uniquement sur VOUS (owner_client) — la boucle sur des comptes tiers a été retirée. `try_execute_entry_for_all_users`/`try_move_stop_to_breakeven_for_all_users`/`try_close_position_for_all_users`/`check_and_execute_missing_position`/`check_and_repair_position_protection` conservés (signatures inchangées) |
| `api_health.py` | ✅ Déplacé depuis `accounts/api_health.py` (17/08), contenu inchangé — détecte une panne d'authentification, notifie une seule fois par panne |
| `admin_notifications.py` | ✅ Déplacé et allégé depuis `accounts/admin_notifications.py` (17/08) — notifications de gestion de comptes tiers/facturation retirées ; conserve clé API cassée/rétablie, protection de position réparée, échecs d'exécution/Breakeven/clôture |

Câblé dans `main.py` (`dispatch_alert`/`_check_tp_sl_hits`) — désactivé
par défaut (kill-switch), le trading automatique ne démarre que via
`manage_execution.py --enable`.

### `accounts/` — supprimé (17 août 2026)

Package entier retiré avec le multi-utilisateur/facturation
(`user_account.py`, `fee_calculator.py`, `trade_ledger.py`) —
`api_health.py`/`admin_notifications.py` déplacés dans `execution/`
(voir ci-dessus, seules pièces encore utiles pour un usage mono-compte).

## Scripts utilitaires

| Script | Rôle |
|---|---|
| `reset_alerts.py` | Réinitialise les alertes en base (`--full` pour tout supprimer) |
| `reset_logs.py` | Nettoie les logs anciens (`--days N`, défaut 7 ; `--full` pour tout supprimer) |
| `preview_notifications.py` | Aperçu des messages de TRADING (création, TP1+consigne Breakeven, clôture Breakeven, clôture SL) |
| `preview_admin_notifications.py` | Aperçu des messages D'ADMINISTRATION — simplifié (17/08) au seul scénario clé API cassée/rétablie |
| `manage_execution.py` | Active/désactive le kill-switch pour VOUS (`--enable/--disable/--status`) — simplifié (17/08), plus de `--user`/`--list` |
| `check_kraken_connection.py` | Vérifie les identifiants Kraken sans jamais passer d'ordre, diagnostic contextuel selon le type d'erreur |

Supprimés le 17/08 : `manage_user_account.py`, `add_user_account.py`,
`generate_fee_report.py`, `check_ibkr_connection.py`.

## Projet séparé : backtest

`backtest/` + `run_backtest.py`/`run_backtest_intraday.py` +
`monte_carlo.py` : réutilise `rules/` et `models/` tels quels, sans
notification ni persistance ni exécution réelle — indépendant de la
simplification architecture du 17/08 (aucun changement requis).

**Backtest réaliste (m15, vraies règles d'entrée live, coûts réels) +
Monte Carlo — les 5 phases sont terminées (16/08/2026)**, voir
`CLAUDE.md` pour le détail complet. `run_backtest_intraday.py` (Phase
0-2, m15, VRAI `DecisionEngine.decide()` bougie par bougie) enchaîne
automatiquement le backtest RÉALISTE (Phase 3 — frais/slippage, skip
"dernier objectif déjà atteint", filtre R:R minimum, position
sizing/levier réels contre un capital simulé évoluant trade après
trade), qui exporte un CSV consommé par `monte_carlo.py` (Phase 4 —
ré-échantillonnage bootstrap des `net_r_multiple`, bandes de
percentile 5/25/50/75/95, distribution du drawdown max, probabilité de
ruine). Testé en réel sur BTC (1 an, Kraken) : R cumulé brut 6,63R mais
dépendant presque entièrement de 2 trades outliers (sans eux, l'année
aurait été négative de -8,06R). Ré-entrée TSQ précise (Weekly
post-TP1) et blocage de coordination HTF restent volontairement HORS
PÉRIMÈTRE de cette v1. 189 tests pour ce périmètre backtest, sanity
checks revert effectués à chaque phase.

## Ce qui reste à faire

- [ ] **Redéploiement Pi de la version simplifiée** — le Pi a été
  entièrement nettoyé (conteneurs/images/cache Docker supprimés,
  dossier de l'ancien déploiement effacé, 17/08) en attendant ce
  redéploiement. Utiliser les commandes robocopy + scp +
  `docker compose up -d --build` habituelles une fois la version
  simplifiée validée localement.
- [ ] Tester `execution/kraken_client.py` avec de petites quantités
  réelles après ce refactor (aucun changement de logique d'exécution
  elle-même, mais à valider comme tout changement touchant ce chemin).
- [ ] Ancien historique IBKR (round-trip d'ordres, migration `ibapi`,
  etc.) devenu sans objet — IBKR entièrement retiré du projet le 17/08.
  Conservé dans `CLAUDE.md` à titre d'historique des décisions passées.
