# Sentinel Trading Bots

Flotte de bots de trading algorithmique MetaTrader 5 (Pepperstone),
independants, concus en TDD. Compte demo recommande.

## Structure

```
bots/     les 6 bots (sans imports croises) + sentinel_signals.py
          (fonctions pures du bot 1)
tests/    les 8 suites de tests (147 tests, MT5 mocke)
docs/     ARCHITECTURE.md (le code), STRATEGIE.md (l'investissement),
          AMELIORATION_CONTINUE.md (mesure et correction des strategies)
research/ backtest_sentinel.py (rejoue les regles des bots sur
          l'historique broker, grilles anti sur-ajustement)
```

## Bots

| Fichier | Strategie | Risque | Coupe-circuit |
|---|---|---|---|
| `sentinel_bot.py` | Breakout M30 (plage asiatique) sur XAUUSD uniquement (suspendu sur EURUSD/GBPUSD le 2026-07-15, cf. AMELIORATION_CONTINUE.md) + Mean Reversion M5 (Bollinger/RSI) sur les trois actifs ; filtre VIX asymetrique (or uniquement) | 1.5% du solde/trade, SL=1.5xATR(14) M30, TP 1:2, partiel 50% + break-even a 1R | -4% d'equite/jour (reference 00:00 UTC), verrou jusqu'au lendemain |
| `sentinel_alpha_compound.py` | Stat-arb : cointegration Brent/WTI (test ADF), entree a \|z\|>=2, sortie convergence/stop temporel 48xM15/stop 4 sigma | Half-Kelly dynamique sur l'equite (plafond 5%, plancher 1% avant 10 trades) | -15% du pic d'equite historique, verrou permanent |
| `sentinel_trend.py` | Suivi de tendance (time-series momentum) : cassure Donchian 55 H4, sortie canal 20 oppose, sur XAUUSD, EURUSD, GBPUSD, US500, XTIUSD | 1% de l'equite/trade (0.5% sur EURUSD/GBPUSD/XTIUSD depuis le 2026-07-15, cf. AMELIORATION_CONTINUE.md), SL dur 2xATR(14), pas de TP | -15% du pic d'equite historique, verrou permanent |
| `sentinel_risk_orchestrator.py` | Ne trade pas : vol targeting 10% annualise (ecrit `risk_scale.json`, applique par tous les bots), alerte de concentration directionnelle | reduit les tailles quand la vol du compte monte (plancher 0.25) | -10% GLOBAL du pic d'equite : ferme toute la flotte (magics Sentinel uniquement), verrou permanent |
| `sentinel_trade_analytics.py` | Ne trade pas : reconstitue les trades fermes depuis l'historique MT5 (magics Sentinel) et publie `logs/trades.csv` + `logs/analytics.html` (win rate, profit factor, expectancy, max DD par strategie/symbole sur 7j/30j/total) | aucun (lecture seule) | aucun |
| `sentinel_telegram.py` | Ne trade pas : notifications Telegram (ouvertures, clotures avec PnL, coupe-circuits, rapport quotidien 18h UTC) et commandes `/status` (equite, positions, verrous, processus) et `/pnl` (gains/pertes jour/7j/30j/total par strategie) | aucun (lecture seule) | aucun |

## Utilisation

```
pip install -r requirements.txt
python bots/sentinel_risk_orchestrator.py   # bot 4 d'abord (pose risk_scale.json)
python bots/sentinel_bot.py                 # bot 1 (multi-actifs intraday)
python bots/sentinel_alpha_compound.py      # bot 2 (spread Brent/WTI)
python bots/sentinel_trend.py               # bot 3 (trend-following H4)
python bots/sentinel_trade_analytics.py     # bot 5 (analyse des trades)
python bots/sentinel_telegram.py            # bot 6 (notifications mobile)
```

### Telegram (bot 6)

1. Sur Telegram, parler a `@BotFather` : `/newbot`, choisir un nom, copier
   le token.
2. Copier `bots/telegram_config.example.json` vers
   `bots/telegram_config.json` et y coller le token (fichier gitignore,
   jamais commite).
3. Envoyer `/start` au bot cree : le chat est enregistre automatiquement,
   les notifications et commandes (`/status`, `/pnl`) sont actives.

Le watchdog envoie aussi une alerte Telegram a chaque relance d'un bot.

Prerequis : terminal MT5 Pepperstone installe (chemin dans `main()`),
"Algo Trading" active. Une seule instance de chaque bot a la fois.

Les fichiers `*_state.json` (references de balance, historique Kelly,
verrous) sont crees au premier cycle et ne se versionnent pas ; toutes
les ecritures d'etat sont atomiques (temporaire + rename).

Chaque bot ecrit `logs/<bot>.hb` apres chaque cycle reussi : le watchdog
relance un processus vivant mais gele (heartbeat trop vieux), avec alerte
Telegram.

Fenetres horaires (UTC reel, bougies serveur converties automatiquement) :
breakout 08:00-16:00 et reversion 13:00-18:00 (bot 1), nouvelles entrees
07:00-20:00 (bot 2), pas d'ouverture 21:00-23:00 (bot 3, rollover). Les
sorties et coupe-circuits ne sont jamais bloques par une fenetre.
`FORCE_TRADING_HOURS` (sentinel_bot.py) : `True` = bypass des fenetres du
bot 1 pour les tests en direct ; laisser a `False` en production.

## Tests

147 tests, MT5 et yfinance mockes (executables sans terminal) :

```
python -m unittest discover -s tests -v
```

La CI (GitHub Actions, `windows-latest`) les execute a chaque push.

## Regles de contribution (protection de branche)

La branche `master` est protegee :

1. **Aucun push direct sur `master`** : tout changement passe par une
   branche puis une pull request.
2. **Une validation (review approuvee) est requise** pour merger une PR ;
   une nouvelle serie de commits invalide les approbations precedentes.
3. **La CI doit etre verte** (job `test`, la suite complete) avant le merge,
   et la branche doit etre a jour avec `master`.
4. **Force-push et suppression de `master` interdits.**

Ces regles s'appliquent a **tout le monde, administrateurs compris**
(enforce_admins actif). L'auteur d'une PR ne peut pas approuver sa
propre PR.

**Validation de secours (absence d'autre dev)** : un administrateur peut
apposer le label `validation-solo` sur la PR ; le workflow
`validation-solo.yml` fait alors approuver la PR par le bot
`github-actions`. La pose du label est l'acte de validation (tracee dans
la PR) ; la CI reste obligatoire. En dernier recours absolu,
l'administrateur peut suspendre temporairement la protection dans
Settings > Branches, puis la reactiver immediatement apres.

Workflow type :

```
git checkout -b feature/ma-modif
# ... commits ...
git push -u origin feature/ma-modif
gh pr create            # puis review + CI verte -> merge
```
