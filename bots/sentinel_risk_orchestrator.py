"""SENTINEL RISK ORCHESTRATOR - Superviseur de risque du portefeuille de bots.

Ne trade pas : il surveille le compte et coordonne les bots Sentinel.

1. Volatility targeting (Moreira & Muir 2017) : mesure la volatilite
   realisee de l'equite (echantillon quotidien) et ecrit dans
   risk_scale.json un facteur [MIN_SCALE, 1] = cible/realisee que les bots
   appliquent a leur taille de position.
2. Concentration directionnelle : alerte si trop de positions Sentinel
   vont dans le meme sens (les strategies deviennent un seul pari).
3. Coupe-circuit GLOBAL : si l'equite perd GLOBAL_MAX_DD depuis son pic
   historique, fermeture de toutes les positions des magics Sentinel
   (celles d'autres EA/manuelles sont intouchees) et verrou permanent -
   il continue de purger tout ce que les bots rouvriraient.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone

import numpy as np
import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Magics de la flotte : bot 1 (1001-3002), alpha (4001), trend (5001-5005)
SENTINEL_MAGICS = ({1001, 1002, 2001, 2002, 3001, 3002, 4001}
                   | set(range(5001, 5006)))

TARGET_VOL = 0.10             # volatilite cible du compte, annualisee
VOL_WINDOW = 20               # jours de rendements pour la vol realisee
MIN_SAMPLES = 5               # en dessous : scale neutre (1.0)
MIN_SCALE = 0.25              # plancher du facteur (jamais couper a zero)
GLOBAL_MAX_DD = 0.10          # verrou global a -10% du pic d'equite
MAX_SAME_DIRECTION = 4        # alerte concentration directionnelle

DEVIATION = 20
_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(_DIR, "orchestrator_state.json")
RISK_SCALE_FILE = os.path.join(_DIR, "risk_scale.json")

log = logging.getLogger("orchestrator")


def vol_scale(realized_vol: float, target: float = TARGET_VOL) -> float:
    """Facteur de reduction cible/realisee, borne a [MIN_SCALE, 1]."""
    if realized_vol <= 0:
        return 1.0
    return float(min(1.0, max(MIN_SCALE, target / realized_vol)))


def write_risk_scale(scale: float, path: str | None = None):
    try:
        with open(path or RISK_SCALE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"scale": round(scale, 4),
                       "updated": datetime.now(timezone.utc).isoformat()}, fh)
    except OSError as exc:
        log.warning("Echec ecriture risk_scale : %s", exc)


# ----------------------------------------------------------------------------
# Suivi d'equite et volatilite realisee
# ----------------------------------------------------------------------------
class EquityMonitor:
    """Un echantillon d'equite par jour UTC, persiste ; vol annualisee."""

    def __init__(self, state_file: str = STATE_FILE):
        self.state_file = state_file
        self.history: list[dict] = []      # [{"day", "equity"}]
        self.peak = 0.0
        self.locked = False
        try:
            with open(state_file, encoding="utf-8") as fh:
                st = json.load(fh)
            self.history = st.get("history", [])
            self.peak = st.get("peak", 0.0)
            self.locked = st.get("locked", False)
        except (OSError, ValueError):
            pass

    def _save(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as fh:
                json.dump({"history": self.history[-90:], "peak": self.peak,
                           "locked": self.locked}, fh)
        except OSError as exc:
            log.warning("Echec sauvegarde etat : %s", exc)

    def snapshot(self, now: datetime, equity: float):
        """Enregistre l'equite du jour (premier passage du jour UTC)."""
        day = now.date().isoformat()
        if not self.history or self.history[-1]["day"] != day:
            self.history.append({"day": day, "equity": float(equity)})
            self._save()
            log.info("Snapshot equite %s : %.2f", day, equity)

    def realized_vol(self) -> float | None:
        """Volatilite annualisee des rendements quotidiens ; None si trop court."""
        eq = [h["equity"] for h in self.history[-(VOL_WINDOW + 1):]]
        if len(eq) < MIN_SAMPLES + 1:
            return None
        rets = np.diff(np.log(eq))
        return float(np.std(rets, ddof=1) * np.sqrt(252))

    def check_drawdown(self, equity: float) -> bool:
        """True si le verrou global est (ou devient) actif."""
        if self.locked:
            return True
        if equity > self.peak:
            self.peak = equity
            self._save()
        elif self.peak > 0 and equity <= self.peak * (1 - GLOBAL_MAX_DD):
            self.locked = True
            self._save()
            log.critical("VERROU GLOBAL : equite %.2f <= -%.0f%% du pic %.2f. "
                         "Fermeture de toute la flotte Sentinel.",
                         equity, GLOBAL_MAX_DD * 100, self.peak)
            return True
        return False


# ----------------------------------------------------------------------------
# Surveillance des positions de la flotte
# ----------------------------------------------------------------------------
def sentinel_positions() -> list:
    return [p for p in (mt5.positions_get() or [])
            if p.magic in SENTINEL_MAGICS]


def direction_concentration(positions: list) -> tuple[int, int]:
    """(nb achats, nb ventes) parmi les positions de la flotte."""
    buys = sum(1 for p in positions if p.type == mt5.POSITION_TYPE_BUY)
    return buys, len(positions) - buys


def close_position(pos) -> bool:
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return False
    buy = pos.type == mt5.POSITION_TYPE_SELL
    result = mt5.order_send({
        "action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
        "volume": pos.volume,
        "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL,
        "position": pos.ticket, "price": tick.ask if buy else tick.bid,
        "deviation": DEVIATION, "magic": pos.magic,
        "comment": "orchestrator_kill", "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC})
    return result is not None and result.retcode == mt5.TRADE_RETCODE_DONE


def kill_fleet():
    """Ferme toutes les positions des magics Sentinel (les autres restent)."""
    for pos in sentinel_positions():
        if close_position(pos):
            log.warning("Position %s (%s, magic=%s) fermee par le verrou.",
                        pos.ticket, pos.symbol, pos.magic)


# ----------------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------------
def run_cycle(monitor: EquityMonitor, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    acc = mt5.account_info()
    if acc is None:
        raise ConnectionError(f"account_info() KO : {mt5.last_error()}")

    # 1. verrou global : purge la flotte tant qu'il est actif
    if monitor.check_drawdown(acc.equity):
        kill_fleet()
        write_risk_scale(MIN_SCALE)       # ceinture + bretelles
        return

    monitor.snapshot(now, acc.equity)

    # 2. volatility targeting -> facteur d'echelle partage
    rvol = monitor.realized_vol()
    scale = 1.0 if rvol is None else vol_scale(rvol)
    write_risk_scale(scale)
    if rvol is not None and scale < 1.0:
        log.info("Vol realisee %.1f%% > cible %.0f%% -> scale=%.2f",
                 rvol * 100, TARGET_VOL * 100, scale)

    # 3. concentration directionnelle de la flotte
    buys, sells = direction_concentration(sentinel_positions())
    if max(buys, sells) >= MAX_SAME_DIRECTION:
        log.warning("CONCENTRATION : %s achats / %s ventes Sentinel dans le "
                    "meme sens - les strategies sont correlees.", buys, sells)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Demarrage SENTINEL RISK ORCHESTRATOR (cible vol %.0f%%, "
             "DD global %.0f%%)", TARGET_VOL * 100, GLOBAL_MAX_DD * 100)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
        return 1
    monitor = EquityMonitor()
    while True:
        try:
            run_cycle(monitor)
        except ConnectionError as exc:
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(5)


if __name__ == "__main__":
    raise SystemExit(main())
