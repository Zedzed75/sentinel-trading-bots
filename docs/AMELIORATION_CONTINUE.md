# Amelioration continue des strategies

Comment la flotte Sentinel mesure ses strategies, decide des corrections
et les valide avant deploiement. Complement de [STRATEGIE.md](STRATEGIE.md)
(le pourquoi des strategies) et [ARCHITECTURE.md](ARCHITECTURE.md) (le code).

## 1. Les instruments de mesure

| Instrument | Ce qu'il mesure | Frequence |
|---|---|---|
| `sentinel_trade_analytics.py` (bot 5) | Trades reels fermes : win rate, profit factor, expectancy, PnL net, max DD - par strategie, symbole, fenetre 7j/30j/total (`logs/analytics.html`, `logs/trades.csv`) | 15 min |
| `sentinel_telegram.py` (bot 6) | `/pnl` et rapport quotidien : la meme verite, sur mobile | continu |
| `research/backtest_sentinel.py` | Rejoue les regles exactes des bots sur l'historique broker, en R-multiples ; mode `--grid` avec validation par moities | a la demande |

Le principe : **le journal reel (bot 5) est le juge de paix**, le backtest
est l'outil d'instruction. Un changement de strategie n'est jamais decide
sur le backtest seul, ni sur moins de 30 trades reels.

## 2. La boucle de correction

1. **Mesurer** : lire `analytics.html` (ou `/pnl`) par strategie ET par
   symbole. Une strategie peut etre saine sur un actif et perdante sur un
   autre (cas reel : trend or vs trend forex, section 5).
2. **Formuler une hypothese unique** : "le breakout EURUSD perd parce que
   ..." - un seul parametre ou une seule regle a la fois.
3. **Instruire au backtest** : `python research/backtest_sentinel.py
   <strategie> <symbole> --grid`. Verdict robuste = les deux moities de
   l'echantillon sont coherentes. Une variante gagnante sur une moitie
   seulement est un artefact.
4. **Valider en demo** : deployer via PR + CI (branche protegee), puis
   laisser le bot 5 accumuler au moins 30 trades reels avant conclusion.
5. **Documenter** : STRATEGIE.md mis a jour avec le pourquoi, ce fichier
   avec la mesure qui a motive le changement.

## 3. Garde-fous statistiques

- **Taille d'echantillon** : aucune conclusion sous 30 trades ; en dessous
  de 100 trades, seule une degradation grossiere (PF < 0.8) est actionnable.
- **Sur-ajustement** : la recherche de Bailey & Lopez de Prado montre qu'un
  excellent backtest s'obtient facilement en testant quelques variantes -
  et que les strategies sur-ajustees sous-performent systematiquement
  ensuite ([Deflated Sharpe Ratio, SSRN](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551),
  [Statistical Overfitting, Bailey](https://sdm.lbl.gov/oapapers/ssrn-id2507040-bailey.pdf)).
  D'ou : grilles volontairement petites, validation par moities
  (walk-forward simplifie), et jamais de parametre choisi parce qu'il est
  "le meilleur de la grille" - on cherche des plateaux robustes, pas des pics.
- **Un changement a la fois**, sinon l'attribution est impossible.
- **Les parametres canoniques restent la reference** (55/20, 2 sigma...) :
  on ne s'en ecarte que si TOUTES les variantes d'une grille disent la meme
  chose (voir trend or : toutes positives ; trend euro : toutes negatives -
  c'est l'actif qu'on questionne, pas le parametre).

## 4. Seuils d'action (journal reel, par strategie x symbole)

| Constat (>= 30 trades) | Action |
|---|---|
| PF >= 1.2 et les deux moities du backtest coherentes | rien : laisser tourner |
| 1.0 <= PF < 1.2 | observer, reevaluer a +30 trades |
| 0.8 <= PF < 1.0 sur 30j ET total | reduire le risque de moitie sur ce couple |
| PF < 0.8, ou backtest negatif sur les deux moities | suspendre le couple strategie/symbole, instruire au backtest |
| Verrou coupe-circuit declenche | analyse humaine obligatoire avant relance (STRATEGIE.md section 3) |

Une suspension n'est pas une suppression : le couple reste dans le code,
desactive par configuration, et peut etre reevalue.

## 5. Recherche du 2026-07-15 : resultats et recommandations

Backtests sur donnees broker (H4 : 2 ans, M30 : 18 mois), regles de
production, R-multiples, filtre VIX non rejoue. `n` = nombre de trades.

### Trend (Donchian 55/20, stop 2xATR)

| Symbole | Total | Moitie 1 | Moitie 2 | Verdict |
|---|---|---|---|---|
| XAUUSD | +25.3R (n=73, PF 1.5) | +2.3R | +23.0R | **sain** - et TOUTE la grille 40-70/15-25 est positive sur les deux moities |
| EURUSD | -27.7R (n=80, PF 0.51) | -14.1R | -13.6R | **structurel** - les 9 variantes de la grille perdent sur les deux moities |
| GBPUSD | -18.0R (n=74, PF 0.62) | -5.5R | -12.5R | negatif bilateral |
| US500 | +0.4R (n=75, PF 1.01) | +6.7R | -6.3R | neutre, a observer |
| SpotCrude | -21.2R (n=78, PF 0.62) | -22.0R | +0.8R | negatif, moitie 2 neutre |

Lecture : l'edge trend de la flotte est aujourd'hui concentre sur l'or.
La litterature ([Moskowitz, Ooi & Pedersen 2012](https://www.sciencedirect.com/science/article/pii/S0304405X11002613),
[Hurst, Ooi & Pedersen - A Century of Evidence](https://fairmodel.econ.yale.edu/ec439/hurst.pdf))
etablit le time-series momentum sur des portefeuilles tres diversifies et
des decennies ; sur 2 ans de H4, un regime sans tendance produit exactement
ce qu'on observe sur les paires forex : des petites pertes repetees
([Quantpedia](https://quantpedia.com/strategies/time-series-momentum-effect),
[Alpha Architect](https://alphaarchitect.com/time-series-momentum-aka-trend-following-the-historical-evidence/)).
Deux ans de backtest ne refutent pas 137 ans de litterature - mais on n'est
pas oblige de payer pour le verifier en risque plein.

**Recommandation trend** : risque divise par deux sur EURUSD, GBPUSD et
XTIUSD (ou suspension), plein risque conserve sur XAUUSD et US500.
Reevaluation quand le journal reel atteint 30 trades par symbole.
*Appliquee le 2026-07-15* (`risk_mult` dans `TREND_PORTFOLIO`).

### Breakout (plage asiatique, SL 1.5xATR, TP 2R, partiel a 1R)

| Symbole | Total | Moitie 1 | Moitie 2 | Verdict |
|---|---|---|---|---|
| XAUUSD | +16.0R (n=552, PF 1.06) | +18.5R | -2.5R | edge mince et en erosion |
| EURUSD | -52.0R (n=649, PF 0.85) | -28.5R | -23.5R | **structurel** |
| GBPUSD | -10.5R (n=655, PF 0.97) | -7.0R | -3.5R | negatif bilateral |

La grille horaire (18 variantes fenetres x SL) sur l'or ne depasse jamais
PF 1.11 et ne departage pas 08-16h de 13-18h (ecarts dans le bruit).
C'est coherent avec la litterature recente : l'edge des opening range
breakouts s'erode avec sa popularite
([QuantifiedStrategies](https://www.quantifiedstrategies.com/opening-range-breakout-strategy/),
[etude de falsification systematique, arXiv](https://arxiv.org/pdf/2605.04004)),
et les faux breakouts en sont la faiblesse principale.

**Recommandation breakout** : suspendre EURUSD et GBPUSD (PF < 1 sur les
deux moities, ~650 trades chacun : l'echantillon est large). Conserver
XAUUSD sous surveillance : si le journal reel confirme PF < 1 sur 30j,
appliquer la reduction de moitie. Ne PAS retoucher la fenetre horaire
sur la seule foi du backtest (bruit).
*Appliquee le 2026-07-15* (drapeau `breakout` dans `CONFIG_PORTFOLIO` ;
la reversion continue sur les trois actifs).

**Suivi XAUUSD breakout (2026-07-20)** : journal reel depuis le
2026-07-16 - 5 trades, -99.10 net, PF < 1 - coherent avec l'edge "mince
et en erosion" identifie ci-dessus. Echantillon toujours sous le seuil
de 30 trades (section 3), mais le declencheur pre-defini le 2026-07-15
("si le journal reel confirme PF < 1 sur 30j, appliquer la reduction de
moitie") est atteint en anticipe. Risque divise par deux sur XAUUSD
breakout uniquement (`breakout_risk_mult` dans `CONFIG_PORTFOLIO`,
reversion et les autres strategies non affectees). Reevaluation a 30
trades reels sur ce couple.

### Stat-arb Brent/WTI (cointegration, |z|>=2, ADF)

Moteur bi-serie ajoute le 2026-07-15 (`backtest_sentinel.py statarb`,
paire alignee par merge, beta OLS et z-score glissants, ADF aux candidats
d'entree). Resultats sur 3 ans de M15 broker (65 206 bougies communes) :

| Paire | Total | Moitie 1 | Moitie 2 | Verdict |
|---|---|---|---|---|
| Brent/WTI | +40.3R (n=336, PF 1.26) | +12.9R (PF 1.16) | +27.3R (PF 1.38) | **sain** - positif sur les deux moities, et TOUTE la grille (entry_z 1.5-2.5 x max_bars 32-64, 9 variantes) est positive sur les deux moities (PF total 1.16-1.38) : plateau robuste, pas un pic |

Limites du rejeu : SL durs par jambe et purge de jambe orpheline non
simules, stop temporel en bougies alignees (le bot compte en heure
horloge). 1R = ecart entree->stop du spread ((stop_z - entry_z) x sigma).

**Recommandation stat-arb** : rien a changer, laisser tourner ; le
Half-Kelly continue d'ajuster la voilure sur les stats realisees.

### Reversion M5 : non backtestee

Le broker ne fournit pas assez d'historique M5 ; jugement par le journal
reel uniquement (seuils section 4).

## 6. Roadmap recherche

1. ~~Moteur de backtest stat-arb (paire alignee, ADF glissant) dans
   `research/`.~~ *Fait le 2026-07-15* (section 5, verdict sain).
2. Ventilation par heure d'ouverture dans `analytics.html` pour instruire
   les fenetres avec des trades reels plutot qu'au backtest.
3. Deflated Sharpe Ratio sur le journal reel des que n >= 100 trades.
4. Filtre d'evenements macro (NFP, FOMC, CPI) pour le bot 1, backteste
   avant deploiement. *Premier etage fait le 2026-07-16* : le bot 7
   (`sentinel_macro_analyst.py`) publie une meteo macro quotidienne
   (calendrier + debat LLM contradictoire) dans `macro_weather.json` -
   INFORMATIVE uniquement ; le filtre de trading reste a backtester
   avant tout branchement sur le sizing. *Etape 1 lancee le 2026-07-16* :
   archive quotidienne `macro_history.json` (meteo, confiance, focus,
   actif principal) a croiser avec le journal reel du bot 5 apres 30
   jours ; branchement eventuel via `risk_scale.json` (multiplicateur
   leger, jamais binaire) seulement si la valeur predictive est prouvee.
5. ~~Purge automatique des couples suspendus reevalues chaque trimestre.~~
   *Fait le 2026-07-15* : le rapport quotidien Telegram rappelle chaque
   couple suspendu/reduit, les trades reels accumules depuis la decision
   et l'echeance de reevaluation trimestrielle (`SUSPENSIONS` dans
   `sentinel_telegram.py`, a tenir a jour a chaque decision). La
   reevaluation elle-meme reste une analyse humaine.
