# Strategie d'investissement

Raisonnement d'investissement de la flotte Sentinel. Pour le fonctionnement
technique, voir [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Philosophie : la priorite est le capital, pas le trade

Le portefeuille ne cherche pas LA strategie gagnante mais la **combinaison
de styles decorreles**, chacun etaye par la recherche academique, avec une
gestion du risque a trois etages (trade, strategie, compte). L'objectif :
une croissance geometrique du capital dont le moteur est la survie -
un drawdown de 50% exige +100% pour revenir a l'equilibre, d'ou la
hierarchie stricte de coupe-circuits.

Cette flotte est concue comme le **satellite** d'un patrimoine dont le
coeur devrait rester un investissement passif indiciel (ETF World en DCA,
hors broker CFD) : les bots cherchent l'alpha, le coeur assure le beta.

## 2. Les quatre briques et leur justification

### Bot 1 - Intraday multi-actifs (retour a la moyenne + breakout)

Deux effets intrajournaliers exploites sur XAUUSD/EURUSD/GBPUSD, chacun
dans sa propre fenetre horaire (UTC). Depuis le 2026-07-15, le breakout
est suspendu sur EURUSD et GBPUSD (backtest structurellement negatif,
~650 trades par paire - voir AMELIORATION_CONTINUE.md section 5) ; la
reversion reste active sur les trois actifs.

- **Breakout de la plage asiatique** (M30, entrees 08h-16h) : la session
  asiatique comprime la volatilite ; la cassure de sa plage a l'ouverture
  occidentale a une esperance directionnelle documentee (litterature sur
  les "opening range breakouts"). La fenetre s'ouvre des la fin de la
  plage (08h, cassures fraiches a Londres) et se ferme avec le
  recouvrement Londres/NY (16h) : au-dela, la cassure est tardive et la
  liquidite decroit.
- **Mean reversion Bollinger/RSI** (M5, entrees 13h-18h) : en phase de
  range confirmee (ecart-type ET moyenne mobile plats), les excursions a
  2 ecarts-types avec RSI extreme tendent a revenir dans la bande ; le
  recouvrement puis l'apres-midi new-yorkais offrent la liquidite sans
  l'elan directionnel des ouvertures.
- **Filtre macro VIX asymetrique** : VIX > 25 = stress de marche. L'or
  monte en crise (valeur refuge) : ses ventes sont bloquees. Les paires
  forex, elles, s'effondrent face au dollar en crise : leurs shorts
  restent autorises.

Profil : beaucoup de petits gains, asymetrie negative. C'est le bot le
plus actif, donc le plus surveille (fenetre horaire, verrou journalier).

### Bot 2 - Arbitrage statistique Brent/WTI

Deux petroles physiquement lies ne peuvent pas diverger durablement :
leur ecart (spread) est **cointegre**. Le bot le verifie en continu avec
le test augmente de Dickey-Fuller (Engle & Granger 1987, prix Nobel 2003)
et ne trade que si p < 0.05. Entree quand le spread s'ecarte de 2 ecarts-
types (Z-score), pari sur le retour a la moyenne, neutre a la direction
du petrole.

Trois sorties : convergence (gain), stop temporel de 48 bougies M15 (un
spread qui ne revient pas n'a plus de raison d'etre tenu), stop a 4 sigma
(la relation se casse). Les nouvelles entrees sont limitees a 07h-20h UTC :
la nuit et pendant le rollover quotidien, les spreads des deux CFD
s'elargissent et un z-score "extreme" peut n'etre qu'un artefact de
cotation. Les sorties, elles, restent permises 24h/24. Le verrou a -15% du pic protege contre le risque
majeur de la strategie : un **bris de cointegration** durable (changement
de regime, ex. revolution logistique du petrole americain).

Sizing par **Critere de Kelly** (Kelly 1956) : `K = W - (1-W)/R` calcule
sur les statistiques realisees du bot, divise par 2 (**Half-Kelly**,
Thorp & MacLean : ~75% de la croissance pour ~50% de la variance),
plafonne a 5%, et applique a l'equite courante - c'est le moteur de
compounding : les mises grossissent avec le compte, et se reduisent
d'elles-memes apres les pertes.

### Bot 3 - Suivi de tendance (time-series momentum)

L'anomalie la plus robuste de la finance quantitative : Moskowitz, Ooi &
Pedersen (JFE 2012) sur 58 actifs, Hurst, Ooi & Pedersen (AQR 2017) sur
**137 ans** de donnees. Implementation type Turtle System 2 : cassure du
canal Donchian 55 bougies H4, stop initial 2xATR, sortie sur le canal 20
oppose - on coupe vite les faux departs, on laisse courir les tendances.

Pas de fenetre de session : le momentum H4 est insensible a l'heure et un
filtre horaire serait du sur-ajustement (principe n. 4, section 5). Seule
exception operationnelle : aucune ouverture entre 21h et 23h UTC
(rollover, spreads elargis) - une cassure detectee dans cette plage est
reprise des la sortie du blackout, les sorties restent libres.

Depuis le 2026-07-15, le risque par trade est reduit de moitie (0.5%)
sur EURUSD, GBPUSD et XTIUSD : le backtest 2 ans y est negatif sur
toutes les variantes de parametres (AMELIORATION_CONTINUE.md section 5).
La litterature etablissant le momentum sur longue periode, c'est une
reduction reversible, pas une suppression - reevaluation a 30 trades
reels par actif.

Profil : asymetrie **positive** (beaucoup de petites pertes, gains rares
et larges), exactement le miroir des bots 1-2. Surtout, le trend-following
produit du "crisis alpha" : ses meilleures annees historiques (2008, 2020,
2022) sont celles ou le retour a la moyenne souffre. C'est la brique de
diversification par regime de marche.

### Bot 4 - Orchestrateur de risque (ne trade pas)

La diversification ne protege que si le risque **agrege** est pilote :

- **Volatility targeting** (Moreira & Muir, Journal of Finance 2017) :
  quand la volatilite realisee du compte depasse la cible de 10%
  annualisee, toutes les tailles de position de la flotte sont reduites
  proportionnellement (facteur cible/realisee, plancher 0.25). Reduire
  l'exposition quand la volatilite monte ameliore le ratio de Sharpe et
  ecrete les drawdowns.
- **Alerte de concentration** : en crise, les strategies se correlent ;
  4 positions dans le meme sens = un seul pari deguise, signale.
- **Verrou global a -10% du pic d'equite** : au-dessus des verrous
  individuels, il ferme toute la flotte et la maintient fermee.
  L'hypothese : si l'ensemble des strategies perd 10%, le probleme n'est
  pas une strategie mais le regime de marche ou un defaut systemique -
  on arrete tout et un humain analyse.

## 3. La pyramide de gestion du risque

| Etage | Mecanisme | Seuil |
|---|---|---|
| Trade | SL obligatoire dimensionne par l'ATR (volatilite courante) | 1.5% du solde (bot 1), Half-Kelly <= 5% (bot 2), 1% de l'equite (bot 3) |
| Journee | Verrou quotidien du bot 1 | -4% d'equite vs balance de 00:00 UTC |
| Strategie | Verrous permanents bots 2 et 3 | -15% du pic d'equite historique |
| Compte | Vol targeting permanent | vol realisee > 10% annualisee -> reduction des tailles |
| Compte | Verrou global de l'orchestrateur | -10% du pic d'equite historique |

Les etages s'emboitent : le vol targeting reduit les tailles bien avant
que les verrous ne se declenchent ; le verrou global (-10%) tombe avant
les verrous de strategie (-15%) si la perte est collective.

Deux principes transverses :

- **Compounding prudent** : les tailles suivent l'equite a la hausse comme
  a la baisse ; jamais de martingale, jamais de moyenne a la baisse.
- **Verrous persistants** : tous les coupe-circuits survivent a un
  redemarrage de process (etat sur disque). Un verrou permanent exige une
  intervention humaine deliberee pour etre leve.

## 4. Comportement attendu (et a ne pas mal interpreter)

- Le bot 2 et le bot 3 peuvent rester **plusieurs jours sans trader**.
  C'est le comportement correct : leurs seuils (2 sigma, canal 55) sont
  rarement atteints par construction. L'inactivite n'est pas une panne.
- Le bot 3 perdra **souvent de petits montants** : son esperance vient de
  quelques grandes tendances par an. Le juger sur un mois n'a pas de sens.
- Le bot 1 est le plus regulier mais plafonne : gains cibles a 2R,
  partiels a 1R.
- Les 10 premiers trades du bot 2 partent au risque plancher de 1% :
  le Kelly a besoin d'un historique reel avant d'etre estime.

## 5. Limites connues et discipline de deploiement

1. **Aucun backtest long n'a ete realise.** Les strategies sont etayees
   par la litterature, pas encore par leur historique propre. La recherche
   de Lopez de Prado (deflated Sharpe ratio) montre que la majorite des
   strategies echouent hors echantillon : la **validation en compte demo
   prolongee est LE test**, et rien ne doit passer en reel avant.
2. Les couts reels (spread variable, swaps overnight pour les bots 2-3,
   slippage sur annonces) ne sont mesurables qu'en conditions reelles.
3. Le filtre VIX depend de yfinance (source externe) : en cas d'echec,
   le bot 1 choisit la prudence (SELL bloques sur l'or).
4. Les parametres (55/20, 2 sigma, 10% de vol cible...) sont des valeurs
   canoniques de la litterature, volontairement non optimisees sur nos
   donnees - c'est une protection contre le sur-ajustement, pas une
   negligence.
5. Risque operationnel : une seule instance par bot, un seul compte,
   surveillance des processus et des verrous requise.

## 6. Feuille de route

- Validation demo multi-semaines, puis revue des statistiques realisees
  (win rate, R realise, drawdowns par bot) avant toute decision de reel.
- Notifications Telegram (ouvertures/clotures, verrous, rapport
  quotidien) : **fait** (bot 6, `sentinel_telegram.py`, + alertes de
  relance du watchdog).
- Backtests historiques longs par strategie pour calibrer les attentes.
- Evitement d'evenements macro (NFP, FOMC, CPI) pour le bot 1.
