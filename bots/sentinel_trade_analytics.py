"""SENTINEL TRADE ANALYTICS - Journal et analyse des trades de la flotte.

Ne trade pas : il lit l'historique des deals du terminal MT5 (magics
Sentinel uniquement), reconstitue les trades fermes et publie :

1. logs/trades.csv       journal complet, un trade ferme par ligne ;
2. logs/analytics.html   rapport auto-rafraichi : win rate, profit factor,
                         expectancy, PnL net, max drawdown, ventiles par
                         strategie et par symbole sur 7 jours / 30 jours /
                         tout l'historique, plus les derniers trades.

Objectif : mesurer chaque strategie en continu pour l'ameliorer (couper ce
qui perd, renforcer ce qui gagne) sans fouiller le terminal a la main.
Toutes les donnees viennent du terminal (history_deals_get) : aucun etat
persistant, le rapport est reconstruit a chaque cycle.
"""

import io
import csv
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Magic -> strategie (bot 1 : 1001-3002, alpha : 4001, trend : 5001-5005).
# Copie volontaire : pas d'imports croises entre bots (voir README).
MAGIC_STRATEGY = {
    1001: "breakout", 2001: "breakout", 3001: "breakout",
    1002: "reversion", 2002: "reversion", 3002: "reversion",
    4001: "statarb",
    5001: "trend", 5002: "trend", 5003: "trend", 5004: "trend",
    5005: "trend",
}
HISTORY_DAYS = 365            # profondeur d'historique demandee au terminal
CYCLE_SECONDS = 900           # un rapport toutes les 15 minutes
LAST_TRADES_SHOWN = 20        # derniers trades affiches dans le rapport
WINDOWS = (("7 jours", 7), ("30 jours", 30), ("Depuis le debut", None))

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
REPORT_HTML = os.path.join(LOG_DIR, "analytics.html")

CSV_FIELDS = ("close_time", "open_time", "strategy", "symbol", "direction",
              "volume", "pnl", "duration_h", "magic", "position_id")

log = logging.getLogger("analytics")


# ----------------------------------------------------------------------------
# Reconstitution des trades a partir des deals bruts
# ----------------------------------------------------------------------------
_SERVER_OFFSET = {"hours": 0.0, "at": None}
_OFFSET_SYMBOLS = ("XAUUSD", "XAUUSD.p", "GOLD", "EURUSD", "EURUSD.p")


def server_offset_hours(now: datetime | None = None) -> float:
    """Decalage (heures) entre l'horloge du serveur MT5 et l'UTC reel.

    Les deals MT5 sont estampilles en heure serveur (UTC+2/+3 chez
    Pepperstone) : on mesure l'ecart tick recent vs horloge locale UTC,
    arrondi a la demi-heure, memorise 1 h. Sans tick frais (week-end),
    la derniere valeur connue est conservee.
    """
    now = now or datetime.now(timezone.utc)
    cache = _SERVER_OFFSET
    if cache["at"] is not None and now - cache["at"] < timedelta(hours=1):
        return cache["hours"]
    for name in _OFFSET_SYMBOLS:
        if not mt5.symbol_select(name, True):
            continue
        ts = getattr(mt5.symbol_info_tick(name), "time", None)
        if isinstance(ts, (int, float)) and ts > 0:
            delta_h = (ts - now.timestamp()) / 3600
            if abs(delta_h) <= 13:        # tick frais, offset plausible
                cache["hours"] = round(delta_h * 2) / 2
                cache["at"] = now
                break
    return cache["hours"]


def build_trades(deals, offset_h: float = 0.0) -> list[dict]:
    """Un trade ferme par position_id (magics Sentinel uniquement).

    Les sorties partielles sont sommees ; une position dont le volume de
    sortie ne couvre pas l'entree (encore ouverte) est ignoree.
    PnL net = profit + commission + swap de tous les deals de la position.
    offset_h (heure serveur - UTC) convertit les horodatages en UTC reel.
    """
    by_pos: dict[int, list] = {}
    for d in deals or []:
        if getattr(d, "magic", None) in MAGIC_STRATEGY:
            by_pos.setdefault(d.position_id, []).append(d)

    trades = []
    for pos_id, group in by_pos.items():
        ins = [d for d in group if d.entry == mt5.DEAL_ENTRY_IN]
        outs = [d for d in group if d.entry == mt5.DEAL_ENTRY_OUT]
        if not ins or not outs:
            continue
        vol_in = sum(d.volume for d in ins)
        if sum(d.volume for d in outs) < vol_in - 1e-8:
            continue
        entry = min(ins, key=lambda d: d.time)
        close_ts = max(d.time for d in outs) - offset_h * 3600
        open_ts = entry.time - offset_h * 3600
        open_dt = datetime.fromtimestamp(open_ts, tz=timezone.utc)
        pnl = sum(d.profit + d.commission + d.swap for d in group)
        trades.append({
            "position_id": pos_id,
            "symbol": entry.symbol,
            "magic": entry.magic,
            "strategy": MAGIC_STRATEGY[entry.magic],
            "direction": ("long" if entry.type == mt5.DEAL_TYPE_BUY
                          else "short"),
            "volume": vol_in,
            "open_time": open_dt,
            "open_hour": f"{open_dt.hour:02d}h",
            "close_time": datetime.fromtimestamp(close_ts, tz=timezone.utc),
            "duration_h": round((close_ts - open_ts) / 3600, 2),
            "pnl": round(pnl, 2),
        })
    trades.sort(key=lambda t: t["close_time"])
    return trades


# ----------------------------------------------------------------------------
# Statistiques
# ----------------------------------------------------------------------------
def compute_stats(trades: list[dict]) -> dict:
    """Win rate, profit factor, expectancy, PnL net, max drawdown du cumul.

    profit_factor est None quand il n'y a aucune perte (indefini).
    Le drawdown est mesure sur le PnL cumule dans l'ordre des clotures.
    """
    if not trades:
        return {"trades": 0, "win_rate": None, "profit_factor": None,
                "expectancy": None, "pnl": 0.0, "max_dd": 0.0,
                "avg_duration_h": None}
    pnls = [t["pnl"] for t in trades]
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p < 0)
    cum = peak = max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "trades": len(pnls),
        "win_rate": sum(1 for p in pnls if p > 0) / len(pnls),
        "profit_factor": (round(gross_win / gross_loss, 2)
                          if gross_loss > 0 else None),
        "expectancy": round(sum(pnls) / len(pnls), 2),
        "pnl": round(sum(pnls), 2),
        "max_dd": round(max_dd, 2),
        "avg_duration_h": round(sum(t["duration_h"] for t in trades)
                                / len(trades), 1),
    }


def in_window(trades: list[dict], now: datetime,
              days: int | None) -> list[dict]:
    if days is None:
        return trades
    limit = now - timedelta(days=days)
    return [t for t in trades if t["close_time"] >= limit]


def split_by(trades: list[dict], key: str) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in trades:
        out.setdefault(t[key], []).append(t)
    return out


# ----------------------------------------------------------------------------
# Sorties : journal CSV et rapport HTML
# ----------------------------------------------------------------------------
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_trade_analytics.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Estampille de vie apres chaque cycle reussi (lue par le watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def _write_atomic(path: str, text: str):
    """Ecrit via un fichier temporaire + rename : jamais de fichier corrompu."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_trades_csv(trades: list[dict], path: str):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for t in trades:
        row = dict(t)
        row["open_time"] = t["open_time"].isoformat()
        row["close_time"] = t["close_time"].isoformat()
        writer.writerow(row)
    _write_atomic(path, buf.getvalue())


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def _num(v, unit: str = "") -> str:
    return "-" if v is None else f"{v:,.2f}{unit}"


def _stats_cells(st: dict) -> str:
    pf = "-" if st["profit_factor"] is None else f"{st['profit_factor']:.2f}"
    return (f"<td>{st['trades']}</td><td>{_pct(st['win_rate'])}</td>"
            f"<td>{pf}</td><td>{_num(st['expectancy'])}</td>"
            f"<td class='{_sign(st['pnl'])}'>{_num(st['pnl'])}</td>"
            f"<td>{_num(st['max_dd'])}</td>"
            f"<td>{_num(st['avg_duration_h'], ' h')}</td>")


def _sign(pnl) -> str:
    return "pos" if (pnl or 0) >= 0 else "neg"


def _stats_head(first_col: str) -> str:
    return (f"<tr><th>{first_col}</th><th>Trades</th><th>Win rate</th>"
            "<th>Profit factor</th><th>Expectancy</th><th>PnL net</th>"
            "<th>Max DD</th><th>Duree moy.</th></tr>")


def _stats_table(trades: list[dict], group_key: str,
                 total_label: str = "TOUTES",
                 first_col: str = "Strategie") -> str:
    rows = [f"<tr class='total'><td>{total_label}</td>"
            f"{_stats_cells(compute_stats(trades))}</tr>"]
    for name, sub in sorted(split_by(trades, group_key).items()):
        rows.append(f"<tr><td>{name}</td>{_stats_cells(compute_stats(sub))}"
                    "</tr>")
    return "<table>" + _stats_head(first_col) + "\n".join(rows) + "</table>"


def render_html(trades: list[dict], now: datetime) -> str:
    sections = []
    for label, days in WINDOWS:
        sub = in_window(trades, now, days)
        sections.append(f"<h2>{label} ({len(sub)} trades)</h2>"
                        + _stats_table(sub, "strategy"))
    sections.append("<h2>Par symbole (tout l'historique)</h2>"
                    + _stats_table(trades, "symbol", first_col="Symbole"))

    # Ventilation par heure d'ouverture UTC : instruit les fenetres d'entree
    # avec des trades reels (AMELIORATION_CONTINUE.md, roadmap 2). Les
    # conclusions restent soumises aux seuils d'echantillon de la section 3.
    sections.append("<h2>Par heure d'ouverture UTC (tout l'historique)</h2>")
    for strat, sub in sorted(split_by(trades, "strategy").items()):
        sections.append(f"<h3>{strat}</h3>"
                        + _stats_table(sub, "open_hour", "TOUTES HEURES",
                                       first_col="Heure (UTC)"))

    last = [(f"<tr><td>{t['close_time']:%Y-%m-%d %H:%M}</td>"
             f"<td>{t['strategy']}</td><td>{t['symbol']}</td>"
             f"<td>{t['direction']}</td><td>{t['volume']}</td>"
             f"<td class='{_sign(t['pnl'])}'>{t['pnl']}</td>"
             f"<td>{t['duration_h']} h</td></tr>")
            for t in reversed(trades[-LAST_TRADES_SHOWN:])]
    sections.append(
        f"<h2>{LAST_TRADES_SHOWN} derniers trades</h2><table>"
        "<tr><th>Cloture (UTC)</th><th>Strategie</th><th>Symbole</th>"
        "<th>Sens</th><th>Volume</th><th>PnL</th><th>Duree</th></tr>"
        + "\n".join(last) + "</table>")

    body = "\n".join(sections)
    return f"""<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Sentinel - analyse des trades</title>
<style>
 body {{ font-family: Segoe UI, sans-serif; margin: 2em;
        background: #1b1e24; color: #d8dde6; }}
 h1 {{ font-size: 1.3em; }} h2 {{ font-size: 1.1em; margin-top: 1.6em; }}
 h3 {{ font-size: 1em; margin: 1em 0 0; color: #aab3c2; }}
 small {{ color: #8a93a3; }}
 table {{ border-collapse: collapse; margin-top: .6em; }}
 td, th {{ padding: .4em .8em; border-bottom: 1px solid #333a45;
          text-align: left; }}
 tr.total td {{ font-weight: bold; }}
 td.pos {{ color: #4cc36a; }} td.neg {{ color: #e05555; }}
</style>
</head><body>
<h1>Analyse des trades Sentinel <small>mise a jour
{now:%Y-%m-%d %H:%M} UTC - PnL nets de frais et swap, horaires convertis
en UTC reel, magics Sentinel uniquement</small></h1>
{body}
</body></html>
"""


# ----------------------------------------------------------------------------
# Boucle principale
# ----------------------------------------------------------------------------
def run_cycle(now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    # marge d'un jour : les deals sont estampilles en heure serveur (UTC+2/3)
    deals = mt5.history_deals_get(now - timedelta(days=HISTORY_DAYS),
                                  now + timedelta(days=1))
    if deals is None:
        raise ConnectionError(f"history_deals_get() KO : {mt5.last_error()}")
    trades = build_trades(deals, server_offset_hours(now))
    os.makedirs(LOG_DIR, exist_ok=True)
    write_trades_csv(trades, TRADES_CSV)
    _write_atomic(REPORT_HTML, render_html(trades, now))
    log.info("%d trades fermes analyses -> analytics.html + trades.csv",
             len(trades))


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    log.info("Demarrage SENTINEL TRADE ANALYTICS (cycle %ds, historique "
             "%d jours)", CYCLE_SECONDS, HISTORY_DAYS)
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
        return 1
    while True:
        try:
            run_cycle()
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
