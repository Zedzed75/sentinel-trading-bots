# Architecture du code

Documentation technique du depot `sentinel-trading-bots`. Pour le raisonnement
d'investissement, voir [STRATEGIE.md](STRATEGIE.md).

## 1. Vue d'ensemble

La flotte se compose de **4 processus Python independants**, chacun dans un
fichier unique (< 600 lignes), connectes au meme terminal MetaTrader 5
(Pepperstone) :

```
bots/sentinel_bot.py               bot 1 : intraday multi-actifs (M5/M30)
bots/sentinel_signals.py           fonctions pures du bot 1 (indicateurs,
                                   signaux, fenetres) - pas un processus
bots/sentinel_alpha_compound.py    bot 2 : stat-arb Brent/WTI (M15)
bots/sentinel_trend.py             bot 3 : trend-following (H4)
bots/sentinel_risk_orchestrator.py bot 4 : superviseur de risque (ne trade pas)
```

Les tests sont dans `tests/` (un fichier par bot) et la documentation dans
`docs/`.

Il n'y a **aucun import entre les bots** : chaque bot est autonome et
peut etre lance, arrete ou mis a jour sans toucher aux autres. Seule
exception au fichier unique : le bot 1 delegue ses fonctions pures a
`sentinel_signals.py` (module sans acces MT5 ni reseau, respect de la
limite de 600 lignes par fichier). La coordination passe par deux
mecanismes decouples :

- **Les magic numbers MT5** : chaque strategie signe ses ordres, ce qui
  permet a chaque bot de ne gerer que ses propres positions, et a
  l'orchestrateur d'identifier la flotte sans toucher aux trades manuels
  ou aux autres EA.

| Magic | Proprietaire |
|---|---|
| 1001 / 1002 | bot 1 - breakout / reversion XAUUSD |
| 2001 / 2002 | bot 1 - breakout / reversion EURUSD |
| 3001 / 3002 | bot 1 - breakout / reversion GBPUSD |
| 4001 | bot 2 - spread Brent/WTI (les deux jambes) |
| 5001..5005 | bot 3 - un magic par actif |

- **Des fichiers JSON partages** (crees dans `bots/`, exclus de git) :
  - `risk_scale.json` : ecrit par l'orchestrateur, lu par les bots 1-3
    (`read_risk_scale()`), facteur [0,1] applique au sizing. Fichier
    absent = 1.0, donc les bots fonctionnent sans orchestrateur.
  - `sentinel_state.json`, `alpha_state.json`, `trend_state.json`,
    `orchestrator_state.json` : etat persistant propre a chaque bot
    (references de balance, historique Kelly, pics d'equite, verrous).
    Ils permettent aux coupe-circuits de **survivre a un redemarrage**.

## 2. Patron commun a tous les bots

Chaque bot suit la meme structure interne :

1. **Constantes de configuration** en tete de fichier (seuils, risques,
   portefeuille avec symboles de repli broker).
2. **Fonctions pures** (indicateurs, signaux, sizing) : pas d'acces MT5,
   testables directement. C'est la que vit toute la logique de decision.
3. **Acces MT5** : fonctions/methodes minces autour de `mt5.*` avec gestion
   de `mt5.last_error()`.
4. **`run_cycle()`** : un passage de boucle, injectable en test (`now`
   parametrable). Ordre systematique : coupe-circuit -> gestion des
   positions ouvertes -> recherche de signaux.
5. **`main()`** : `while True` + `time.sleep(1)`, reconnexion MT5 en cas de
   `ConnectionError`, exceptions loggees sans tuer le processus.

Conventions transverses :

- **Nouvelle bougie cloturee uniquement** : les signaux sont evalues une
  seule fois par bougie (suivi `last_bars`), jamais sur la bougie en cours.
- **Resolution de symboles avec replis** : le nom canonique (`XAUUSD`) est
  teste, puis les variantes broker (`XAUUSD.p`, `GOLD`, `SpotBrent`...).
  Un actif absent est retire avec un WARNING sans bloquer le reste.
- **Logs a precision dynamique** : `fp(symbol, value)` affiche 5 decimales
  pour les paires forex, 2 pour or/energie/indices.
- **Aucun ordre au marche sans stop loss** (bots 1 et 3 ont aussi un TP ou
  une sortie par canal ; le bot 2 sort par logique de spread mais chaque
  jambe porte un SL dur).

## 3. Detail par bot

### 3.1 `sentinel_bot.py` - intraday multi-actifs

- `CONFIG_PORTFOLIO` : XAUUSD, EURUSD, GBPUSD avec magics et flag
  `vix_filter` par actif.
- Indicateurs (dans `sentinel_signals.py`, comme tous les signaux et
  fenetres horaires) : `rsi` (Wilder), `bollinger`, `atr` (Wilder),
  `is_flat_range` (ecart-type Bollinger plat **et** moyenne mobile plate -
  le second critere evite de prendre une tendance reguliere pour un range).
- Signaux : `breakout_signal` (cloture M30 hors plage asiatique 22h-08h UTC
  calculee par `asian_range`) et `reversion_signal` (M5 : cloture hors
  bande + RSI < 20 ou > 80, puis retour dans la bande).
- `apply_macro_filter(signal, vix, vix_filter)` : si l'actif a le flag,
  VIX > 25 (ou introuvable) bloque les SELL. `MacroFilter` recupere le VIX
  via yfinance une fois par jour (cache).
- Sizing : `compute_lot` risque 1.5% du solde sur `1.5 x ATR(14) M30`,
  TP a 2R, multiplie par `read_risk_scale()`.
- Gestion active : a 1R de profit, cloture de 50% + SL au break-even
  (`manage_positions`, filtre strict symbole + magic).
- `DayGuard` : balance de reference a 00:00 UTC, verrou journalier a -4%
  d'equite, `close_everything()` global.
- `FORCE_TRADING_HOURS` : bypass temporaire de la fenetre 13h-18h UTC pour
  les tests en direct (laisser `False` en production).

### 3.2 `sentinel_alpha_compound.py` - stat-arb + Kelly

Structure en classes :

- `AlphaState` : etat persistant (PnL realises, pic d'equite, verrou,
  position ouverte avec heure d'entree).
- `CointegrationEngine` : `hedge_ratio` (OLS), `analyze` (spread
  `A - beta*B`, test ADF `statsmodels`, Z-score fenetre 96),
  `entry_signal` (|z| >= 2 **et** p-ADF < 0.05), `exit_reason`
  (convergence |z| <= 0.5, `z_stop` |z| >= 4, `time_stop` 48 bougies M15).
- `KellySizer` : `kelly_fraction(W, R) = W - (1-W)/R`, Half-Kelly
  (divise par 2), plafond 5%, risque plancher 1% tant que moins de
  10 trades d'historique. `lots_for_spread` applique la fraction a
  l'**equite** courante (compounding) x `read_risk_scale()`, jambe B
  dimensionnee par le beta.
- `DrawdownGuard` : verrou **permanent** a -15% du pic d'equite.
- `PairTrader` : ouverture atomique des deux jambes (rollback si une jambe
  echoue), SL dur a 4 sigma par jambe, purge de jambe orpheline si un SL a
  saute d'un cote, alignement des deux series par merge sur `time`.

### 3.3 `sentinel_trend.py` - suivi de tendance

- `TREND_PORTFOLIO` : 5 actifs multi-classes, un magic chacun.
- `donchian(df, n)` : canal des n bougies **precedant** la bougie de signal
  (la cassure ne se compte pas elle-meme).
- `entry_signal` : cassure du canal 55 -> BUY/SELL. `exit_signal` : cloture
  au-dela du canal 20 oppose a la position.
- `open_trend_trade` : SL dur a 2xATR(14) H4, **pas de TP** (sortie par
  canal), 1% de l'equite x `read_risk_scale()`.
- `PeakGuard` : verrou permanent a -15% du pic, ne ferme que les magics
  5001-5005.

### 3.4 `sentinel_risk_orchestrator.py` - superviseur

- `EquityMonitor` : un snapshot d'equite par jour UTC (historique persiste,
  90 jours max), `realized_vol()` = ecart-type des rendements quotidiens
  annualise (fenetre 20 jours, minimum 6 echantillons sinon `None`).
- `vol_scale(realisee)` = `min(1, max(0.25, 0.10 / realisee))` ecrit dans
  `risk_scale.json` a chaque cycle (Moreira & Muir 2017).
- `direction_concentration` : alerte WARNING si >= 4 positions Sentinel
  dans le meme sens.
- `check_drawdown` : verrou **global permanent** a -10% du pic d'equite ->
  `kill_fleet()` ferme uniquement les positions dont le magic appartient a
  `SENTINEL_MAGICS` et continue de purger a chaque cycle tant que le verrou
  est actif (si un bot rouvre, la position est refermee).

## 4. Tests (TDD)

Une suite par module, executable **sans terminal MT5** : le module `MetaTrader5` (et
`yfinance` pour le bot 1) est remplace par un `MagicMock` injecte dans
`sys.modules` avant l'import du bot. `statsmodels` est reel dans les tests
du bot 2 (series synthetiques cointegrees et marches aleatoires
independantes, graines fixees).

```
python -m unittest discover -s tests -v
```

Points systematiquement couverts : formules de sizing (valeurs exactes),
seuils des coupe-circuits (declenchement a la borne, pas avant), isolation
symbole/magic (une position etrangere ne doit jamais etre geree ni fermee),
persistance des verrous apres redemarrage, un signal par bougie cloturee,
et preuve du compounding (lots proportionnels a l'equite).

La CI GitHub Actions (`.github/workflows/tests.yml`, runner
`windows-latest` car le package MetaTrader5 est Windows-only) execute
toutes les suites a chaque push.

## 5. Operations

- Prerequis : terminal MT5 Pepperstone installe (chemin code dans chaque
  `main()`), option "Algo Trading" activee, `pip install -r requirements.txt`.
- **Une seule instance de chaque bot** (deux instances = course sur les
  signaux et sur les fichiers d'etat).
- **Supervision automatique** : la tache planifiee Windows
  `SentinelWatchdog` (declencheur : ouverture de session) execute
  `ops/watchdog.ps1`, qui lance la flotte (orchestrateur en premier),
  relance tout bot mort en <= 30 s, capture stdout/stderr de chaque bot
  dans `logs/<bot>.log` (rotation a 10 Mo) et se protege contre les
  doubles instances. Lancement manuel :
  `Start-ScheduledTask -TaskName SentinelWatchdog`.
- Deverrouillage manuel apres coupe-circuit permanent : supprimer (ou
  editer `"locked": false` dans) le fichier d'etat concerne, apres analyse
  de la cause.
- Les logs vont sur la console (`logging`, format horodate).
