"""SENTINEL TELEGRAM - Suivi de la flotte et des trades sur mobile.

Ne trade pas. Toutes les POLL_SECONDS :

1. push automatique : ouverture de position, cloture (avec PnL du deal),
   activation d'un verrou coupe-circuit, rapport quotidien a
   DAILY_REPORT_HOUR UTC (gains/pertes jour/7j/30j/total + equite) ;
2. commandes recues : /status (equite, positions, verrous, processus),
   /pnl (total des gains/pertes par fenetre et par strategie).

Configuration : bots/telegram_config.json -> {"token": "..."} (gitignore,
voir telegram_config.example.json ; token cree via @BotFather).
Le chat_id est capture au premier message recu par le bot (/start) et
persiste dans telegram_state.json : envoyer un message au bot suffit.
Seul ce chat est ensuite ecoute. API https://api.telegram.org (HTTPS).
"""

import csv
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests
import MetaTrader5 as mt5

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
# Magic -> strategie (copie volontaire : pas d'imports croises entre bots)
MAGIC_STRATEGY = {
    1001: "breakout", 2001: "breakout", 3001: "breakout",
    1002: "reversion", 2002: "reversion", 3002: "reversion",
    4001: "statarb",
    5001: "trend", 5002: "trend", 5003: "trend", 5004: "trend",
    5005: "trend",
}
POLL_SECONDS = 30
DAILY_REPORT_HOUR = 18        # rapport quotidien apres la fenetre du bot 1

# Couples strategie/symbole suspendus ou a risque reduit (decisions de
# recherche appliquees dans les configs des bots 1 et 3, voir
# docs/AMELIORATION_CONTINUE.md section 5). Copie volontaire, comme
# MAGIC_STRATEGY : a tenir a jour a chaque decision. Le rapport quotidien
# rappelle les trades reels accumules depuis et la reevaluation
# trimestrielle - la boucle de reevaluation ne repose plus sur la memoire.
# aliases : noms broker possibles du symbole dans trades.csv.
SUSPENSIONS = (
    {"strategy": "breakout", "symbol": "EURUSD", "action": "suspendu",
     "since": "2026-07-15", "aliases": ("EURUSD",)},
    {"strategy": "breakout", "symbol": "GBPUSD", "action": "suspendu",
     "since": "2026-07-15", "aliases": ("GBPUSD",)},
    {"strategy": "trend", "symbol": "EURUSD", "action": "risque /2",
     "since": "2026-07-15", "aliases": ("EURUSD",)},
    {"strategy": "trend", "symbol": "GBPUSD", "action": "risque /2",
     "since": "2026-07-15", "aliases": ("GBPUSD",)},
    {"strategy": "trend", "symbol": "XTIUSD", "action": "risque /2",
     "since": "2026-07-15", "aliases": ("XTIUSD", "SpotCrude", "USOIL")},
)
REVIEW_AFTER_DAYS = 91        # reevaluation trimestrielle
REVIEW_MIN_TRADES = 30        # seuil du journal reel (AMELIORATION section 4)

_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.dirname(_DIR), "logs")
TRADES_CSV = os.path.join(LOG_DIR, "trades.csv")
CONFIG_FILE = os.path.join(_DIR, "telegram_config.json")
STATE_FILE = os.path.join(_DIR, "telegram_state.json")
RISK_SCALE_FILE = os.path.join(_DIR, "risk_scale.json")

# state file -> libelle du verrou surveille
LOCK_SOURCES = {
    "sentinel_state.json": "bot 1 (verrou journalier -4%)",
    "alpha_state.json": "bot 2 (verrou -15% du pic)",
    "trend_state.json": "bot 3 (verrou -15% du pic)",
    "orchestrator_state.json": "verrou GLOBAL -10% (toute la flotte)",
}
FLEET_BOTS = ("sentinel_risk_orchestrator.py", "sentinel_bot.py",
              "sentinel_alpha_compound.py", "sentinel_trend.py",
              "sentinel_trade_analytics.py")

API_URL = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("telegram")


# ----------------------------------------------------------------------------
# Lecture des donnees (fonctions pures, testables)
# ----------------------------------------------------------------------------
HEARTBEAT_FILE = os.path.join(LOG_DIR, "sentinel_telegram.hb")


def write_heartbeat(path: str = HEARTBEAT_FILE,
                    now: datetime | None = None):
    """Estampille de vie apres chaque cycle reussi (lue par le watchdog)."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write((now or datetime.now(timezone.utc)).isoformat())
    except OSError:
        pass


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def read_trades(path: str = TRADES_CSV) -> list[dict]:
    """Journal ecrit par sentinel_trade_analytics (coordination par fichier,
    comme risk_scale.json). Liste de {pnl, strategy, close_time}."""
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return [{"pnl": float(r["pnl"]), "strategy": r["strategy"],
                     "symbol": r.get("symbol", ""),
                     "close_time": datetime.fromisoformat(r["close_time"])}
                    for r in csv.DictReader(fh)]
    except (OSError, ValueError, KeyError):
        return []


def pnl_summary(rows: list[dict], now: datetime) -> dict:
    """Totaux nets jour / 7j / 30j / historique + ventilation par strategie."""
    def total(days=None):
        if days is None:
            sel = rows
        else:
            lim = now - timedelta(days=days)
            sel = [r for r in rows if r["close_time"] >= lim]
        return round(sum(r["pnl"] for r in sel), 2)

    today = [r for r in rows if r["close_time"].date() == now.date()]
    by_strategy: dict[str, dict] = {}
    for r in rows:
        st = by_strategy.setdefault(r["strategy"], {"pnl": 0.0, "count": 0})
        st["pnl"] = round(st["pnl"] + r["pnl"], 2)
        st["count"] += 1
    return {"day": round(sum(r["pnl"] for r in today), 2),
            "d7": total(7), "d30": total(30), "total": total(),
            "count": len(rows), "by_strategy": by_strategy}


def fmt_eur(v: float) -> str:
    return f"{v:+.2f} EUR"


def _badge(v: float) -> str:
    return "✅" if v >= 0 else "\U0001f53b"   # coche verte / triangle


def format_pnl_message(s: dict) -> str:
    lines = ["\U0001f4b0 Gains/Pertes (nets de frais)",
             f"Aujourd'hui : {_badge(s['day'])} {fmt_eur(s['day'])}",
             f"7 jours : {_badge(s['d7'])} {fmt_eur(s['d7'])}",
             f"30 jours : {_badge(s['d30'])} {fmt_eur(s['d30'])}",
             f"Total ({s['count']} trades) : "
             f"{_badge(s['total'])} {fmt_eur(s['total'])}"]
    if s["by_strategy"]:
        lines.append("")
        lines.append("Par strategie :")
        for name, st in sorted(s["by_strategy"].items()):
            lines.append(f"- {name} : {fmt_eur(st['pnl'])} "
                         f"({st['count']} trades)")
    return "\n".join(lines)


def suspension_lines(rows: list[dict], now: datetime) -> list[str]:
    """Rappel des couples suspendus/reduits : trades reels accumules depuis
    la decision et date de reevaluation trimestrielle (⚠️ si echue)."""
    lines = []
    for s in SUSPENSIONS:
        since = datetime.fromisoformat(s["since"]).replace(tzinfo=timezone.utc)
        due = since + timedelta(days=REVIEW_AFTER_DAYS)
        aliases = tuple(a.upper() for a in s["aliases"])
        n = sum(1 for r in rows
                if r["strategy"] == s["strategy"]
                and r["close_time"] >= since
                and r.get("symbol", "").upper().startswith(aliases))
        due_txt = (f"reevaluation ECHUE ({due:%Y-%m-%d}) ⚠️" if now >= due
                   else f"reevaluation le {due:%Y-%m-%d}")
        lines.append(f"- {s['strategy']} {s['symbol']} : {s['action']} "
                     f"depuis le {s['since']}, {n} trades depuis "
                     f"(seuil {REVIEW_MIN_TRADES}), {due_txt}")
    return lines


def active_locks(dir_path: str = _DIR) -> list[str]:
    """Libelles des coupe-circuits actuellement verrouilles."""
    return [label for name, label in LOCK_SOURCES.items()
            if load_json(os.path.join(dir_path, name)).get("locked")]


def new_closing_deals(deals, since: float) -> list:
    """Deals de sortie des magics Sentinel posterieurs a `since`."""
    return [d for d in deals or []
            if d.entry == mt5.DEAL_ENTRY_OUT
            and getattr(d, "magic", None) in MAGIC_STRATEGY
            and d.time > since]


def should_send_daily(last_report_day: str | None, now: datetime) -> bool:
    return (now.hour >= DAILY_REPORT_HOUR
            and last_report_day != now.date().isoformat())


def bots_processes() -> dict[str, bool]:
    """{script: processus vivant} via une requete CIM (Windows)."""
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" "
           "| Select-Object -ExpandProperty CommandLine")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True, text=True, timeout=25).stdout or ""
    except Exception as exc:                      # pragma: no cover
        log.warning("Verification des processus impossible : %s", exc)
        out = ""
    return {b: (b in out) for b in FLEET_BOTS}


def sentinel_positions() -> list:
    return [p for p in (mt5.positions_get() or [])
            if p.magic in MAGIC_STRATEGY]


def status_text(now: datetime) -> str:
    acc = mt5.account_info()
    lines = ["\U0001f916 Flotte Sentinel - "
             + now.strftime("%Y-%m-%d %H:%M UTC")]
    if acc is not None:
        lines.append(f"Equite : {acc.equity:.2f} {acc.currency} "
                     f"(balance {acc.balance:.2f})")
    scale = load_json(RISK_SCALE_FILE).get("scale")
    if scale is not None:
        lines.append(f"Risk scale : {scale}")
    pos = sentinel_positions()
    lines.append(f"Positions ouvertes ({len(pos)}) :")
    for p in pos:
        sens = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
        strat = MAGIC_STRATEGY.get(p.magic, "?")
        lines.append(f"- {sens} {p.symbol} {p.volume} lot ({strat}) "
                     f"PnL {fmt_eur(p.profit)}")
    locks = active_locks()
    lines.append("Verrous : " + ("aucun"
                 if not locks else "\U0001f512 " + " ; ".join(locks)))
    lines.append("Processus :")
    for name, alive in bots_processes().items():
        lines.append(f"- {name} : {'OK' if alive else 'ARRETE !'}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Notifier : etat persistant + API Telegram
# ----------------------------------------------------------------------------
class TelegramNotifier:
    def __init__(self, token: str, state_file: str = STATE_FILE):
        self.token = token
        self.state_file = state_file
        st = load_json(state_file)
        self.chat_id = st.get("chat_id")
        self.last_update_id = st.get("last_update_id", 0)
        self.last_deal_ts = st.get("last_deal_ts", 0)
        self.last_report_day = st.get("last_report_day")
        self.open_tickets = st.get("open_tickets", [])
        self.known_locks = st.get("known_locks", [])

    def _save(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"chat_id": self.chat_id,
                           "last_update_id": self.last_update_id,
                           "last_deal_ts": self.last_deal_ts,
                           "last_report_day": self.last_report_day,
                           "open_tickets": self.open_tickets,
                           "known_locks": self.known_locks}, fh)
            os.replace(tmp, self.state_file)
        except OSError as exc:
            log.warning("Echec sauvegarde etat : %s", exc)

    def api(self, method: str, **params) -> dict:
        try:
            resp = requests.post(API_URL.format(token=self.token,
                                                method=method),
                                 json=params, timeout=15)
            return resp.json()
        except Exception as exc:
            log.warning("API Telegram %s KO : %s", method, exc)
            return {}

    def send(self, text: str):
        if self.chat_id:
            self.api("sendMessage", chat_id=self.chat_id, text=text)

    # --- commandes entrantes -------------------------------------------------
    def poll_commands(self, now: datetime | None = None):
        now = now or datetime.now(timezone.utc)
        resp = self.api("getUpdates", offset=self.last_update_id + 1,
                        timeout=0)
        for upd in (resp or {}).get("result", []):
            self.last_update_id = max(self.last_update_id,
                                      upd.get("update_id", 0))
            msg = upd.get("message") or {}
            chat = (msg.get("chat") or {}).get("id")
            text = (msg.get("text") or "").strip().lower()
            if chat and self.chat_id is None:      # premier contact = maitre
                self.chat_id = chat
                self._save()
                log.info("Chat maitre enregistre : %s", chat)
                self.send("Sentinel connecte ✅\n"
                          "Commandes : /status /pnl")
                continue
            if chat != self.chat_id:               # un seul chat ecoute
                continue
            self._handle(text, now)
        self._save()

    def _handle(self, text: str, now: datetime):
        if text.startswith("/status"):
            self.send(status_text(now))
        elif text.startswith("/pnl"):
            self.send(format_pnl_message(pnl_summary(read_trades(), now)))
        elif text.startswith("/start") or text.startswith("/aide"):
            self.send("Commandes : /status (flotte, positions, verrous) "
                      "et /pnl (gains/pertes)")


# ----------------------------------------------------------------------------
# Evenements pousses automatiquement
# ----------------------------------------------------------------------------
def check_closed_deals(notif: TelegramNotifier, now: datetime):
    """Notifie chaque deal de sortie (PnL net du deal) depuis le dernier vu."""
    if not notif.last_deal_ts:                    # premier cycle : reference
        notif.last_deal_ts = int(now.timestamp())
        return
    deals = mt5.history_deals_get(
        datetime.fromtimestamp(notif.last_deal_ts, tz=timezone.utc)
        - timedelta(days=1), now + timedelta(days=1))
    for d in new_closing_deals(deals, notif.last_deal_ts):
        pnl = d.profit + d.commission + d.swap
        strat = MAGIC_STRATEGY[d.magic]
        notif.send(f"{_badge(pnl)} Cloture {d.symbol} ({strat}) : "
                   f"{fmt_eur(pnl)}")
        notif.last_deal_ts = max(notif.last_deal_ts, int(d.time))
    notif._save()


def check_position_events(notif: TelegramNotifier):
    """Notifie les nouvelles positions ouvertes (magics Sentinel)."""
    current = {p.ticket: p for p in sentinel_positions()}
    for ticket, p in current.items():
        if ticket not in notif.open_tickets:
            sens = "LONG" if p.type == mt5.POSITION_TYPE_BUY else "SHORT"
            strat = MAGIC_STRATEGY.get(p.magic, "?")
            notif.send(f"\U0001f4c8 Ouverture {sens} {p.symbol} "
                       f"{p.volume} lot ({strat})")
    notif.open_tickets = list(current)
    notif._save()


def check_locks(notif: TelegramNotifier):
    """Alerte a l'activation d'un coupe-circuit (une seule fois)."""
    locks = active_locks()
    for label in locks:
        if label not in notif.known_locks:
            notif.send(f"\U0001f6a8 COUPE-CIRCUIT ACTIVE : {label}")
    notif.known_locks = locks
    notif._save()


def maybe_daily_report(notif: TelegramNotifier, now: datetime):
    if not should_send_daily(notif.last_report_day, now):
        return
    acc = mt5.account_info()
    rows = read_trades()
    msg = format_pnl_message(pnl_summary(rows, now))
    if acc is not None:
        msg += f"\n\nEquite : {acc.equity:.2f} {acc.currency}"
    susp = suspension_lines(rows, now)
    if susp:
        msg += ("\n\n⏳ Couples sous surveillance :\n"
                + "\n".join(susp))
    notif.send("\U0001f4c5 Rapport quotidien\n" + msg)
    notif.last_report_day = now.date().isoformat()
    notif._save()


def run_cycle(notif: TelegramNotifier, now: datetime | None = None):
    now = now or datetime.now(timezone.utc)
    if mt5.account_info() is None:
        raise ConnectionError(f"account_info() KO : {mt5.last_error()}")
    notif.poll_commands(now)
    check_position_events(notif)
    check_closed_deals(notif, now)
    check_locks(notif)
    maybe_daily_report(notif, now)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    def _token():
        return (load_json(CONFIG_FILE).get("token")
                or os.environ.get("TELEGRAM_BOT_TOKEN"))

    token = _token()
    if not token:
        log.warning("Pas de token : creer bots/telegram_config.json (voir "
                    "telegram_config.example.json, token via @BotFather). "
                    "En attente...")
    while not token:                  # attente passive, watchdog-friendly
        time.sleep(60)
        token = _token()
    if not mt5.initialize(
            path="C:/Program Files/Pepperstone MetaTrader 5/terminal64.exe"):
        log.error("mt5.initialize() a echoue : %s", mt5.last_error())
        return 1
    notif = TelegramNotifier(token)
    log.info("Demarrage SENTINEL TELEGRAM (chat_id %s)",
             notif.chat_id or "en attente du premier message")
    while True:
        try:
            run_cycle(notif)
            write_heartbeat()
        except ConnectionError as exc:
            log.error("Connexion perdue : %s - reconnexion...", exc)
            mt5.shutdown()
            time.sleep(5)
            mt5.initialize()
        except Exception as exc:
            log.exception("Erreur inattendue : %s", exc)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
