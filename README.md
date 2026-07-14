# Sentinel Trading Bots

Flotte de bots de trading algorithmique MetaTrader 5 (Pepperstone),
independants, concus en TDD. Compte demo recommande.

## Structure

```
bots/     les 4 bots (fichiers autonomes, sans imports croises)
tests/    les 4 suites de tests (81 tests, MT5 mocke)
docs/     ARCHITECTURE.md (le code) et STRATEGIE.md (l'investissement)
```

## Bots

| Fichier | Strategie | Risque | Coupe-circuit |
|---|---|---|---|
| `sentinel_bot.py` | Breakout M30 (plage asiatique) + Mean Reversion M5 (Bollinger/RSI) sur XAUUSD, EURUSD, GBPUSD ; filtre VIX asymetrique (or uniquement) | 1.5% du solde/trade, SL=1.5xATR(14) M30, TP 1:2, partiel 50% + break-even a 1R | -4% d'equite/jour (reference 00:00 UTC), verrou jusqu'au lendemain |
| `sentinel_alpha_compound.py` | Stat-arb : cointegration Brent/WTI (test ADF), entree a \|z\|>=2, sortie convergence/stop temporel 48xM15/stop 4 sigma | Half-Kelly dynamique sur l'equite (plafond 5%, plancher 1% avant 10 trades) | -15% du pic d'equite historique, verrou permanent |
| `sentinel_trend.py` | Suivi de tendance (time-series momentum) : cassure Donchian 55 H4, sortie canal 20 oppose, sur XAUUSD, EURUSD, GBPUSD, US500, XTIUSD | 1% de l'equite/trade, SL dur 2xATR(14), pas de TP | -15% du pic d'equite historique, verrou permanent |
| `sentinel_risk_orchestrator.py` | Ne trade pas : vol targeting 10% annualise (ecrit `risk_scale.json`, applique par tous les bots), alerte de concentration directionnelle | reduit les tailles quand la vol du compte monte (plancher 0.25) | -10% GLOBAL du pic d'equite : ferme toute la flotte (magics Sentinel uniquement), verrou permanent |

## Utilisation

```
pip install -r requirements.txt
python bots/sentinel_risk_orchestrator.py   # bot 4 d'abord (pose risk_scale.json)
python bots/sentinel_bot.py                 # bot 1 (multi-actifs intraday)
python bots/sentinel_alpha_compound.py      # bot 2 (spread Brent/WTI)
python bots/sentinel_trend.py               # bot 3 (trend-following H4)
```

Prerequis : terminal MT5 Pepperstone installe (chemin dans `main()`),
"Algo Trading" active. Une seule instance de chaque bot a la fois.

Les fichiers `*_state.json` (references de balance, historique Kelly,
verrous) sont crees au premier cycle et ne se versionnent pas.

`FORCE_TRADING_HOURS` (sentinel_bot.py) : `True` = bypass des horaires
13:00-18:00 UTC pour les tests en direct ; laisser a `False` en production.

## Tests

81 tests, MT5 et yfinance mockes (executables sans terminal) :

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
3. **La CI doit etre verte** (job `test`, les 81 tests) avant le merge,
   et la branche doit etre a jour avec `master`.
4. **Force-push et suppression de `master` interdits.**

Ces regles s'appliquent a **tout le monde, administrateurs compris**
(enforce_admins actif). Consequence pour un depot mono-mainteneur :
l'auteur d'une PR ne pouvant pas approuver sa propre PR, le merge exige
un second compte (collaborateur/reviewer). En cas d'urgence absolue,
l'administrateur peut suspendre temporairement la regle dans
Settings > Branches, puis la reactiver immediatement apres.

Workflow type :

```
git checkout -b feature/ma-modif
# ... commits ...
git push -u origin feature/ma-modif
gh pr create            # puis review + CI verte -> merge
```
