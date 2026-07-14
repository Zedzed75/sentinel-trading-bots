# Sentinel Trading Bots

Deux bots de trading algorithmique MetaTrader 5 (Pepperstone), independants,
concus en TDD. Compte demo recommande.

## Bots

| Fichier | Strategie | Risque | Coupe-circuit |
|---|---|---|---|
| `sentinel_bot.py` | Breakout M30 (plage asiatique) + Mean Reversion M5 (Bollinger/RSI) sur XAUUSD, EURUSD, GBPUSD ; filtre VIX asymetrique (or uniquement) | 1.5% du solde/trade, SL=1.5xATR(14) M30, TP 1:2, partiel 50% + break-even a 1R | -4% d'equite/jour (reference 00:00 UTC), verrou jusqu'au lendemain |
| `sentinel_alpha_compound.py` | Stat-arb : cointegration Brent/WTI (test ADF), entree a \|z\|>=2, sortie convergence/stop temporel 48xM15/stop 4 sigma | Half-Kelly dynamique sur l'equite (plafond 5%, plancher 1% avant 10 trades) | -15% du pic d'equite historique, verrou permanent |

## Utilisation

```
pip install -r requirements.txt
python sentinel_bot.py              # bot 1 (multi-actifs)
python sentinel_alpha_compound.py   # bot 2 (spread Brent/WTI)
```

Prerequis : terminal MT5 Pepperstone installe (chemin dans `main()`),
"Algo Trading" active. Une seule instance de chaque bot a la fois.

Les fichiers `*_state.json` (references de balance, historique Kelly,
verrous) sont crees au premier cycle et ne se versionnent pas.

`FORCE_TRADING_HOURS` (sentinel_bot.py) : `True` = bypass des horaires
13:00-18:00 UTC pour les tests en direct ; laisser a `False` en production.

## Tests

57 tests, MT5 et yfinance mockes (executables sans terminal) :

```
python -m unittest test_sentinel_bot test_sentinel_alpha_compound -v
```

La CI (GitHub Actions, `windows-latest`) les execute a chaque push.
