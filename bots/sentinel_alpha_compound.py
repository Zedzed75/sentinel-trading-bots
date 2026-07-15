"""SENTINEL ALPHA COMPOUND - Bot MT5 independant : stat-arb + Kelly.

Strategie : trading de cointegration (spread Brent/WTI) valide par test ADF
(statsmodels). Entree quand le Z-score du spread depasse +/-2 ecarts-types,
pari sur le retour a la moyenne. Sortie : convergence (|z| < 0.5), stop
temporel (N bougies sans convergence) ou stop d'ecartement (|z| > 4).

Compounding : taille de mise via Critere de Kelly (K = W - (1-W)/R) sur les
statistiques realisees de la strategie, contrainte Half-Kelly (Thorp/MacLean),
recalculee sur l'EQUITE courante du compte a chaque ouverture.

Securite : coupe-circuit sur drawdown maximal historique (pic d'equite),
verrouillage permanent en cas de bris de regime (perte de cointegration).
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import MetaTrader5 as mt5
from statsmodels.tsa.stattools import adfuller

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Paire cointegree : Brent (A) vs WTI (B), replis selon nomenclature broker
LEG_A = {"name": "XBRUSD", "fallback": ["XBRUSD.p", "SpotBrent", "UKOIL"]}
LEG_B = {"name": "XTIUSD", "fallback": ["XTIUSD.p", "SpotCrude", "USOIL"]}
MAGIC_ALPHA = 4001

TIMEFRAME = None              # fixe dans main() : mt5.TIMEFRAME_M15
TF_MINUTES = 15
LOOKBACK = 240                # bougies pour beta (OLS) et test ADF
ZSCORE_WINDOW = 96            # fenetre du Z-score du spread

ADF_PVALUE_MAX = 0.05         # cointegration exigee (H0 rejetee)
ENTRY_Z = 2.0                 # entree si |z| >= 2
EXIT_Z = 0.5                  # convergence atteinte
STOP_Z = 4.0                  # ecartement anormal : coupure immediate
MAX_BARS_IN_TRADE = 48        # stop temporel : N bougies sans convergence

# Nouvelles entrees uniquement quand Brent ET WTI sont liquides (sessions
# Londres/NY) : la nuit et pendant le rollover (~21h-22h UTC) les spreads
# s'elargissent et polluent le z-score. Les sorties (convergence, stops)
# restent permises 24h/24 - on ne retient jamais une protection.
ENTRY_HOUR_START = 7
ENTRY_HOUR_END = 20

MIN_TRADES_FOR_KELLY = 10     # avant : risque par defaut
DEFAULT_RISK = 0.01           # 1% tant que l'historique est insuffisant
KELLY_DIVISOR = 2.0           # Half-Kelly
MAX_RISK = 0.05               # plafond absolu de fraction risquee
MAX_HISTO_DD = 0.15           # verrou si equite < 85% du pic historique

DEVIATION = 20
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "alpha_state.json")
RISK_SCALE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "risk_scale.json")

log = logging.getLogger("alpha")

CCY = {"EUR", "GBP", "USD", "JPY", "CHF", "AUD", "NZD", "CAD"}


def price_fmt(symbol: str) -> str:
    """Forex (deux devises ISO) : 5 decimales ; matieres premieres/indices : 2."""
    s = symbol.upper()
    return "%.5f" if s[:3] in CCY and s[3:6] in CCY else "%.2f"


def fp(symbol: str, value: float | None) -> str:
    """Prix formate pour les logs selon la precision de l'actif."""
    return "n/a" if value is None else price_fmt(symbol) % value


def entries_allowed(now: datetime) -> bool:
    """Nouveau spread uniquement dans [ENTRY_HOUR_START, ENTRY_HOUR_END) UTC."""
    return ENTRY_HOUR_START <= now.hour < ENTRY_HOUR_END


def read_risk_scale(path: str | None = None) -> float:
    """Facteur [0,1] ecrit par l'orchestrateur de risque ; 1.0 par defaut."""
    try:
        with open(path or RISK_SCALE_FILE, encoding="utf-8") as fh:
            return min(1.0, max(0.0, float(json.load(fh)["scale"])))
    except (OSError, ValueError, KeyError):
        return 1.0


def kelly_fraction(win_rate: float, rr: float) -> float:
    """Critere de Kelly K = W - (1-W)/R, borne a 0 si esperance negative."""
    if rr <= 0:
        return 0.0
    return max(0.0, win_rate - (1 - win_rate) / rr)


# ----------------------------------------------------------------------------
# Etat persistant (historique de trades, pic d'equite, position ouverte)
# ----------------------------------------------------------------------------
class AlphaState:
    def __init__(self, path: str = STATE_FILE):
        self.path = path
        self.trades: list[float] = []      # PnL realises de la strategie
        self.peak_equity: float = 0.0
        self.locked = False
        self.open: dict | None = None      # {direction, entry_time, beta, sigma}
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                st = json.load(fh)
            self.trades = st.get("trades", [])
            self.peak_equity = st.get("peak_equity", 0.0)
            self.locked = st.get("locked", False)
            self.open = st.get("open")
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as fh:
                json.dump({"trades": self.trades,
                           "peak_equity": self.peak_equity,
                           "locked": self.locked, "open": self.open}, fh)
        except OSError as exc:
            log.warning("Echec sauvegarde etat : %s", exc)


# ----------------------------------------------------------------------------
# Moteur de cointegration (statistiques pures, testable)
# ----------------------------------------------------------------------------
class CointegrationEngine:
    """Beta de couverture (OLS), test ADF sur le spread, Z-score."""

    @staticmethod
    def hedge_ratio(a: pd.Series, b: pd.Series) -> float:
        beta, _ = np.polyfit(b.values, a.values, 1)
        return float(beta)

    def analyze(self, a: pd.Series, b: pd.Series) -> dict | None:
        """Statistiques du spread ; None si series trop courtes."""
        if len(a) < ZSCORE_WINDOW + 2 or len(a) != len(b):
            return None
        beta = self.hedge_ratio(a, b)
        spread = a - beta * b
        pvalue = float(adfuller(spread.values, autolag="AIC")[1])
        mu = float(spread.rolling(ZSCORE_WINDOW).mean().iloc[-1])
        sd = float(spread.rolling(ZSCORE_WINDOW).std().iloc[-1])
        if sd <= 0:
            return None
        return {"beta": beta, "sigma": sd, "pvalue": pvalue,
                "coint": pvalue < ADF_PVALUE_MAX,
                "z": (float(spread.iloc[-1]) - mu) / sd}

    @staticmethod
    def entry_signal(analysis: dict | None) -> str | None:
        """BUY_SPREAD (achat A / vente B) si z <= -2, SELL_SPREAD si z >= 2.

        Aucune entree sans cointegration validee par l'ADF (p < 0.05).
        """
        if not analysis or not analysis["coint"]:
            return None
        if analysis["z"] <= -ENTRY_Z:
            return "BUY_SPREAD"
        if analysis["z"] >= ENTRY_Z:
            return "SELL_SPREAD"
        return None

    @staticmethod
    def exit_reason(z: float, bars_held: int) -> str | None:
        """Motif de sortie, ou None si la position reste ouverte."""
        if abs(z) <= EXIT_Z:
            return "convergence"
        if abs(z) >= STOP_Z:
            return "z_stop"
        if bars_held >= MAX_BARS_IN_TRADE:
            return "time_stop"
        return None


# ----------------------------------------------------------------------------
# Moteur de compounding : Kelly dynamique sur l'equite
# ----------------------------------------------------------------------------
class KellySizer:
    """Fraction de mise Half-Kelly issue de l'historique realise."""

    def __init__(self, state: AlphaState):
        self.state = state

    @property
    def win_rate(self) -> float:
        t = self.state.trades
        return sum(1 for p in t if p > 0) / len(t) if t else 0.0

    @property
    def rr_ratio(self) -> float:
        wins = [p for p in self.state.trades if p > 0]
        losses = [-p for p in self.state.trades if p < 0]
        if not wins or not losses:
            return 0.0
        return (sum(wins) / len(wins)) / (sum(losses) / len(losses))

    def risk_fraction(self) -> float:
        """Half-Kelly plafonne ; risque par defaut si historique insuffisant."""
        if len(self.state.trades) < MIN_TRADES_FOR_KELLY:
            return DEFAULT_RISK
        k = kelly_fraction(self.win_rate, self.rr_ratio) / KELLY_DIVISOR
        return min(k, MAX_RISK) if k > 0 else DEFAULT_RISK

    def record(self, pnl: float):
        self.state.trades.append(round(float(pnl), 2))
        self.state.save()
        log.info("Trade enregistre PnL=%.2f | W=%.2f R=%.2f -> fraction=%.4f",
                 pnl, self.win_rate, self.rr_ratio, self.risk_fraction())

    @staticmethod
    def lot_for(risk_amount: float, sl_distance: float, sym) -> float:
        """Volume MT5 risquant risk_amount sur sl_distance (normalise)."""
        if sl_distance <= 0 or sym.trade_tick_size <= 0:
            return 0.0
        loss_per_lot = (sl_distance / sym.trade_tick_size) * sym.trade_tick_value
        if loss_per_lot <= 0:
            return 0.0
        lots = np.floor((risk_amount / loss_per_lot) / sym.volume_step)
        lots = round(lots * sym.volume_step, 8)
        if lots < sym.volume_min:
            return 0.0
        return float(min(lots, sym.volume_max))

    def lots_for_spread(self, equity: float, analysis: dict,
                        sym_a, sym_b) -> tuple[float, float]:
        """(lot_a, lot_b) : risque Half-Kelly de l'EQUITE, partage par jambe.

        SL de reference : ecartement du spread jusqu'a STOP_Z sigma. La jambe
        B est dimensionnee par le beta de couverture (neutralite du spread).
        """
        risk_leg = equity * self.risk_fraction() * read_risk_scale() / 2
        sl_a = STOP_Z * analysis["sigma"]
        lot_a = self.lot_for(risk_leg, sl_a, sym_a)
        beta = abs(analysis["beta"]) or 1.0
        lot_b = self.lot_for(risk_leg, sl_a / beta, sym_b)
        # ajuste la jambe B au ratio de couverture
        lot_b = min(lot_b, round(np.floor(lot_a * beta / sym_b.volume_step)
                                 * sym_b.volume_step, 8))
        return lot_a, float(max(lot_b, 0.0))


# ----------------------------------------------------------------------------
# Coupe-circuit : drawdown maximal historique
# ----------------------------------------------------------------------------
class DrawdownGuard:
    """Verrouille le bot si l'equite perd MAX_HISTO_DD depuis son pic."""

    def __init__(self, state: AlphaState):
        self.state = state

    def check(self, equity: float) -> bool:
        """True si le bot doit etre (ou rester) verrouille."""
        if self.state.locked:
            return True
        if equity > self.state.peak_equity:
            self.state.peak_equity = equity
            self.state.save()
        elif (self.state.peak_equity > 0
              and equity <= self.state.peak_equity * (1 - MAX_HISTO_DD)):
            self.state.locked = True
            self.state.save()
            log.critical("DRAWDOWN MAX : equite %.2f <= -%.0f%% du pic %.2f. "
                         "Bot verrouille (bris de regime suspecte) - "
                         "intervention manuelle requise.", equity,
                         MAX_HISTO_DD * 100, self.state.peak_equity)
            return True
        return False


# ----------------------------------------------------------------------------
# Acces MT5 et orchestration
# ----------------------------------------------------------------------------
class PairTrader:
    """Execution du spread : resolution symboles, ordres, cycle de vie."""

    def __init__(self, state: AlphaState):
        self.state = state
        self.engine = CointegrationEngine()
        self.sizer = KellySizer(state)
        self.sym_a: str | None = None
        self.sym_b: str | None = None
        self.last_bar = None

    # --- infrastructure -----------------------------------------------------
    @staticmethod
    def _resolve(cfg: dict) -> str | None:
        return next((s for s in [cfg["name"]] + cfg["fallback"]
                     if mt5.symbol_select(s, True)
                     and mt5.symbol_info(s) is not None), None)

    def resolve_pair(self) -> bool:
        self.sym_a, self.sym_b = self._resolve(LEG_A), self._resolve(LEG_B)
        if not self.sym_a or not self.sym_b:
            log.error("Paire indisponible (A=%s, B=%s) : %s",
                      self.sym_a, self.sym_b, mt5.last_error())
            return False
        log.info("Paire cointegration : %s / %s", self.sym_a, self.sym_b)
        return True

    @staticmethod
    def closes(symbol: str, timeframe: int, count: int) -> pd.DataFrame | None:
        rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
        if rates is None or len(rates) == 0:
            log.warning("Pas de donnees %s : %s", symbol, mt5.last_error())
            return None
        df = pd.DataFrame(rates)[["time", "close"]]
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        return df

    @staticmethod
    def _send(request: dict):
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("Ordre refuse : %s / %s", getattr(result, "retcode",
                      None), mt5.last_error())
            return None
        return result

    def _market_order(self, symbol: str, direction: str, lot: float,
                      sl: float | None = None) -> bool:
        tick = mt5.symbol_info_tick(symbol)
        sym = mt5.symbol_info(symbol)
        if tick is None or sym is None:
            return False
        buy = direction == "BUY"
        price = tick.ask if buy else tick.bid
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
               "volume": lot,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
               "price": price, "deviation": DEVIATION, "magic": MAGIC_ALPHA,
               "comment": "alpha_spread", "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_IOC}
        if sl is not None:
            req["sl"] = round(sl, sym.digits)
        if self._send(req):
            log.info("%s %s lot=%.2f @ %s SL=%s", direction, symbol, lot,
                     fp(symbol, price), fp(symbol, req.get("sl")))
            return True
        return False

    def _positions(self) -> list:
        out = []
        for s in (self.sym_a, self.sym_b):
            out += [p for p in (mt5.positions_get(symbol=s) or [])
                    if p.magic == MAGIC_ALPHA and p.symbol == s]
        return out

    # --- cycle de vie du spread ----------------------------------------------
    def open_spread(self, direction: str, analysis: dict, equity: float,
                    now: datetime) -> bool:
        sym_a, sym_b = mt5.symbol_info(self.sym_a), mt5.symbol_info(self.sym_b)
        tick_a = mt5.symbol_info_tick(self.sym_a)
        tick_b = mt5.symbol_info_tick(self.sym_b)
        if None in (sym_a, sym_b, tick_a, tick_b):
            return False
        lot_a, lot_b = self.sizer.lots_for_spread(equity, analysis,
                                                  sym_a, sym_b)
        if lot_a <= 0 or lot_b <= 0:
            log.warning("Lots nuls (equite %.2f), spread ignore.", equity)
            return False
        sl_a = STOP_Z * analysis["sigma"]
        sl_b = sl_a / (abs(analysis["beta"]) or 1.0)
        if direction == "BUY_SPREAD":     # achat A / vente B
            legs = [(self.sym_a, "BUY", lot_a, tick_a.ask - sl_a),
                    (self.sym_b, "SELL", lot_b, tick_b.bid + sl_b)]
        else:                             # vente A / achat B
            legs = [(self.sym_a, "SELL", lot_a, tick_a.bid + sl_a),
                    (self.sym_b, "BUY", lot_b, tick_b.ask - sl_b)]
        if not all(self._market_order(*leg) for leg in legs):
            self.close_spread("echec jambe (rollback)")
            return False
        self.state.open = {"direction": direction,
                           "entry_time": now.isoformat(),
                           "beta": analysis["beta"],
                           "sigma": analysis["sigma"]}
        self.state.save()
        log.info("SPREAD %s ouvert (z=%.2f, beta=%.3f, fraction=%.4f)",
                 direction, analysis["z"], analysis["beta"],
                 self.sizer.risk_fraction())
        return True

    def close_spread(self, reason: str):
        """Ferme les deux jambes, enregistre le PnL realise du spread."""
        positions = self._positions()
        pnl = sum(p.profit for p in positions)
        for pos in positions:
            tick = mt5.symbol_info_tick(pos.symbol)
            if tick is None:
                continue
            buy = pos.type == mt5.POSITION_TYPE_SELL
            self._send({"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
                        "volume": pos.volume,
                        "type": mt5.ORDER_TYPE_BUY if buy
                        else mt5.ORDER_TYPE_SELL,
                        "position": pos.ticket,
                        "price": tick.ask if buy else tick.bid,
                        "deviation": DEVIATION, "magic": MAGIC_ALPHA,
                        "comment": f"alpha_close_{reason}"[:31],
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": mt5.ORDER_FILLING_IOC})
        if positions:
            self.sizer.record(pnl)
            log.info("SPREAD ferme (%s) PnL=%.2f", reason, pnl)
        if self.state.open:
            self.state.open = None
            self.state.save()

    def bars_held(self, now: datetime) -> int:
        if not self.state.open:
            return 0
        entry = datetime.fromisoformat(self.state.open["entry_time"])
        return int((now - entry).total_seconds() // (TF_MINUTES * 60))

    def manage(self, analysis: dict | None, equity: float, now: datetime):
        """Sorties si position ouverte, sinon recherche d'entree."""
        if self.state.open:
            positions = self._positions()
            if len(positions) < 2:        # jambe orpheline (SL touche) : purge
                self.close_spread("jambe_orpheline")
                return
            if analysis:
                reason = self.engine.exit_reason(analysis["z"],
                                                 self.bars_held(now))
                if reason:
                    self.close_spread(reason)
            return
        signal = self.engine.entry_signal(analysis)
        if signal and not entries_allowed(now):
            log.info("Signal %s ignore hors fenetre %02d-%02dh UTC "
                     "(liquidite/rollover) ; reevalue a la prochaine bougie.",
                     signal, ENTRY_HOUR_START, ENTRY_HOUR_END)
            return
        if signal:
            log.info("Signal %s (z=%.2f, p-ADF=%.4f)", signal,
                     analysis["z"], analysis["pvalue"])
            self.open_spread(signal, analysis, equity, now)


def run_cycle(trader: PairTrader, guard: DrawdownGuard, timeframe: int,
              now: datetime | None = None):
    """Un passage : coupe-circuit, alignement des series, gestion du spread."""
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO : {mt5.last_error()}")
    if guard.check(acc.equity):
        trader.close_spread("drawdown_max")
        return

    df_a = trader.closes(trader.sym_a, timeframe, LOOKBACK + 2)
    df_b = trader.closes(trader.sym_b, timeframe, LOOKBACK + 2)
    if df_a is None or df_b is None:
        return
    merged = df_a.merge(df_b, on="time", suffixes=("_a", "_b")).iloc[:-1]
    if len(merged) < ZSCORE_WINDOW + 2:
        return
    bar_time = merged["time"].iloc[-1]
    if trader.last_bar == bar_time:       # pas de nouvelle bougie cloturee
        return
    trader.last_bar = bar_time
    analysis = trader.engine.analyze(merged["close_a"], merged["close_b"])
    trader.manage(analysis, acc.equity, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Demarrage SENTINEL ALPHA COMPOUND (stat-arb + Half-Kelly)")
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
        return 1
    state = AlphaState()
    trader = PairTrader(state)
    guard = DrawdownGuard(state)
    if not trader.resolve_pair():
        mt5.shutdown()
        return 1
    timeframe = mt5.TIMEFRAME_M15
    while True:
        try:
            run_cycle(trader, guard, timeframe)
        except ConnectionError as exc:
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
