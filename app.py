"""
EZ bad — serveur + un watcher par compte.

Comptes : variables d'environnement, un bloc par utilisateur (N = 1, 2, 3…) :
  USER1_NAME=ao          USER1_EMAIL=…  USER1_PASSWORD=…  USER1_TOKEN=…
  USER2_NAME=pote1       USER2_EMAIL=…  USER2_PASSWORD=…  USER2_TOKEN=…
ou comptes créés par invitation (/join/<code>), stockés chiffrés dans accounts.json.
Chaque compte a son URL  /u/<name>/, sa wishlist, ses réglages, ses délais.
(Compat : SPORTEASY_EMAIL / SPORTEASY_PASSWORD / ACCESS_TOKEN = compte "ao" si USER1_* absent.)

Fichiers (DATA_DIR, = /data sur Railway) :
  <name>/wishlist.json   options + réglages de ce compte
  <name>/session.json    cookies Sporteasy
  accounts.json          comptes créés par invitation (mot de passe chiffré)
  invites.json           invitations
  bot.log                journal commun (rotation automatique)

Cycle d'une option (champ "state") :
  pending → waiting (heure d'ouverture connue) → delaying (ouvert, délai aléatoire en cours)
  → booked | waitlist | full | error | skipped ; cancelled / released = terminé.
Sporteasy fait foi : à chaque passage, l'état est recalé sur ce que dit le site (inscrit, liste
d'attente, annulé…), et le bot ne renvoie jamais une demande quand il est déjà en liste d'attente.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")
os.environ.setdefault("TZ", "Europe/Paris")
if hasattr(time, "tzset"):
    time.tzset()

# après load_dotenv : sporteasy lit le fuseau (TZ) à l'import
from sporteasy import FAST, TZ, Event, EventGone, LoginError, LoginThrottled, SporteasyClient, fr_when  # noqa: E402

VERSION = "2026.11.11"

TEAM_ID = int(os.environ.get("SPORTEASY_TEAM_ID", "2050454"))
TEAM_HOST = os.environ.get("SPORTEASY_TEAM_HOST", "sitebad.sporteasy.net")
PORT = int(os.environ.get("PORT", "8765"))
TICK = int(os.environ.get("TICK_SECONDS", "30"))
HOT_WINDOW = int(os.environ.get("HOT_WINDOW_SECONDS", "120"))
HOT_INTERVAL = float(os.environ.get("HOT_INTERVAL_SECONDS", "1.0"))
HOT_MAX_MIN = int(os.environ.get("HOT_MAX_MINUTES", "20"))
WARM_MIN = int(os.environ.get("WARM_MINUTES", "40"))       # après le mode rapide : un coup d'œil toutes les 5 s
MAX_PER_WEEK = int(os.environ.get("MAX_PER_WEEK", "5"))
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM = os.environ.get("RESEND_FROM", "EZ bad <onboarding@resend.dev>").strip()
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/") + "/"
DATA = Path(os.environ.get("DATA_DIR", str(BASE)))
DATA.mkdir(parents=True, exist_ok=True)
LOGFILE = DATA / "bot.log"
# Exposé sur Internet ? (Railway, Fly, ou toute écoute hors de la machine locale)
PUBLIC = bool(os.environ.get("PUBLIC") or os.environ.get("FLY_APP_NAME")
              or any(k.startswith("RAILWAY_") for k in os.environ)
              or os.environ.get("HOST", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"))
PUBLIC_URL = (os.environ.get("PUBLIC_URL") or (f"https://{os.environ['RAILWAY_PUBLIC_DOMAIN']}"
                                               if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")).rstrip("/")
ADMINS = {a.strip().lower() for a in os.environ.get("ADMIN_USERS", "ao").split(",") if a.strip()}
INVITE_DAYS = int(os.environ.get("INVITE_DAYS", "7"))
KEEP_DAYS = 14                                             # les options de créneaux passés sont purgées après ça
YEAR = 60 * 60 * 24 * 365

# Délai aléatoire après l'ouverture, minutes [min, max], par jour du créneau. Chaque compte a le sien.
DEFAULT_DELAYS: dict[str, list[float]] = {"mon": [0.033, 0.083], "thu": [0.08, 0.83], "default": [1, 5]}
_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DONE = ("booked", "released", "cancelled")


def now() -> datetime:
    """Heure courante au fuseau du club (indépendante du fuseau de la machine)."""
    return datetime.now(TZ)


def _iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def _parse(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=TZ)


class _ClubTime(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created, TZ).strftime("%Y-%m-%d %H:%M:%S")


_fmt = _ClubTime("%(asctime)s %(levelname)s %(name)s: %(message)s")
_handlers: list[logging.Handler] = [RotatingFileHandler(LOGFILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8"),
                                    logging.StreamHandler(sys.stdout)]
for _h in _handlers:
    _h.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=_handlers)
log = logging.getLogger("bot")


def _short_err(e: BaseException) -> str:
    """Message d'erreur court et lisible pour l'interface."""
    if isinstance(e, requests.Timeout):
        return "Sporteasy ne répond pas (délai dépassé)"
    if isinstance(e, requests.ConnectionError):
        return "Sporteasy injoignable"
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"Sporteasy a répondu {e.response.status_code}"
    return str(e)[:160] or e.__class__.__name__


# ================================================================= notifications
_NQ: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=200)
_SENT: dict[tuple[str, str, str, str], float] = {}
_SENT_LOCK = threading.Lock()
NOTIF_DEDUP = 600          # la même notification au même destinataire : au plus une fois par 10 min


def notify(title: str, msg: str, ntfy: str = "", tags: str = "", email: str = "", click: str = "",
           force: bool = False) -> None:
    """Met une notification en file : l'envoi ne bloque jamais une réservation."""
    log.info("NOTIF %s — %s", title, msg)
    key = (ntfy, email, title, msg)
    t = time.time()
    with _SENT_LOCK:
        if not force and t - _SENT.get(key, 0) < NOTIF_DEDUP:
            log.info("notification identique déjà envoyée il y a moins de %d min : ignorée", NOTIF_DEDUP // 60)
            return
        _SENT[key] = t
        for k in [k for k, v in _SENT.items() if t - v > NOTIF_DEDUP]:
            del _SENT[k]
    try:
        _NQ.put_nowait({"title": title, "msg": msg, "ntfy": ntfy, "tags": tags, "email": email, "click": click})
    except queue.Full:
        log.warning("file de notifications pleine, notification abandonnée")


def _deliver(n: dict[str, Any]) -> dict[str, str]:
    """Envoie réellement une notification. Retourne le résultat par canal."""
    title, msg, out = n["title"], n["msg"], {}
    if n.get("email") and RESEND_KEY:
        try:
            r = requests.post("https://api.resend.com/emails", timeout=10,
                              headers={"Authorization": f"Bearer {RESEND_KEY}"},
                              json={"from": RESEND_FROM, "to": [n["email"]], "subject": f"{title} — {msg[:60]}",
                                    "text": f"{msg}\n\n— EZ bad 🏸"})
            out["email"] = "ok" if r.status_code < 400 else f"{r.status_code}"
            if r.status_code >= 400:
                log.warning("resend %s : %s", r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001
            out["email"] = "erreur"
            log.warning("resend : %s", e)
    if n.get("ntfy"):
        # Publication en JSON : le titre peut contenir accents, tirets longs et emojis
        # (en en-tête HTTP, tout caractère hors latin-1 faisait échouer l'envoi).
        body: dict[str, Any] = {"topic": n["ntfy"], "title": title, "message": msg, "priority": 4,
                                "tags": [t for t in (n.get("tags") or "badminton").split(",") if t]}
        if n.get("click"):
            body["click"] = n["click"]
        try:
            r = requests.post(NTFY_URL, json=body, timeout=10)
            out["ntfy"] = "ok" if r.status_code < 400 else f"refus {r.status_code}"
            if r.status_code >= 400:
                log.warning("ntfy %s : %s", r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001
            out["ntfy"] = "injoignable"
            log.warning("ntfy : %s", e)
    if sys.platform == "darwin":
        try:   # texte passé en argument, jamais interpolé dans le script
            subprocess.run(["osascript", "-e", "on run a", "-e",
                            'display notification (item 2 of a) with title (item 1 of a) sound name "Glass"',
                            "-e", "end run", title, msg], timeout=5, check=False)
        except Exception:  # noqa: BLE001
            pass
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"text": f"*{title}*\n{msg}"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass
    return out


def _notif_worker() -> None:
    while True:
        n = _NQ.get()
        try:
            _deliver(n)
        except Exception as e:  # noqa: BLE001
            log.warning("notification : %s", e)
        finally:
            _NQ.task_done()


threading.Thread(target=_notif_worker, daemon=True, name="notifs").start()


def _week_key(dt: datetime) -> str:
    iso = dt.astimezone(TZ).isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


# ================================================================= Account
class Account:
    def __init__(self, name: str, email: str, password: str, token: str, invite: str = "", ntfy: str = "",
                 email_notif: str = "", team_id: int = 0, team_host: str = "", kind: str = "env"):
        self.name = name
        self.token = token
        self.invite = invite.strip()
        self._ntfy = ntfy.strip()
        self.email = email
        self.email_notif = (email_notif or email).strip()
        self.kind = kind                      # "env" (variables Railway) ou "db" (auto-inscription)
        self.team_host = team_host or TEAM_HOST
        self.team_id = team_id or (TEAM_ID if self.team_host == TEAM_HOST else 0)
        self.active = True
        self.dir = DATA / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wishlist = self.dir / "wishlist.json"
        self.lock = threading.RLock()
        self.recent: deque[str] = deque(maxlen=120)
        self.log = logging.getLogger(f"bot.{name}")
        self.log.addHandler(_Ring(self.recent))
        self.client = SporteasyClient(self.team_id, self.team_host, email, password, session_file=self.dir / "session.json")
        self.cache: dict[str, Any] = {"at": 0.0, "events": []}
        self.inflight: set[str] = set()          # créneaux en cours de réservation
        self.hot_started: set[tuple[str, str]] = set()   # fenêtres rapides déjà lancées
        self.last_err: str | None = None         # dernière erreur de lecture Sporteasy (None = tout va bien)
        self._saved: str | None = None
        self._ev_lock = threading.Lock()
        self._wake = threading.Event()
        self._auth_alert = False
        self._last_test = 0.0
        self._last_sync = 0.0

    @property
    def ntfy(self) -> str:
        """Topic ntfy : celui réglé par la personne dans l'app, sinon celui des variables."""
        try:
            return (self.load()["settings"].get("ntfy") or self._ntfy or "").strip()
        except Exception:  # noqa: BLE001
            return self._ntfy

    @property
    def link(self) -> str | None:
        return f"/i/{self.invite or self.token[:8]}" if self.token else None

    def note(self, title: str, msg: str, tags: str = "") -> None:
        """Notification à la personne (ntfy / email). Le clic ouvre sa page, jamais son lien perso."""
        notify(title, msg, self.ntfy, tags, self.email_notif, f"{PUBLIC_URL}/u/{self.name}/" if PUBLIC_URL else "")

    def kick(self) -> None:
        """Réveille le watcher sans attendre le prochain passage."""
        self._wake.set()

    # ---------- wishlist ----------
    def load(self) -> dict[str, Any]:
        if self.wishlist.exists():
            try:
                d = json.loads(self.wishlist.read_text())
                d.setdefault("wishes", {}); d.setdefault("settings", {}); d.setdefault("history", [])
                return d
            except Exception as e:  # noqa: BLE001
                self.log.error("wishlist corrompue : %s", e)
                try:   # on garde une copie : la prochaine sauvegarde repartira de zéro
                    shutil.copy(self.wishlist, self.wishlist.with_suffix(".corrompu"))
                except OSError:
                    pass
        return {"wishes": {}, "settings": {"paused": False, "max_per_week": MAX_PER_WEEK, "delays": dict(DEFAULT_DELAYS)}, "history": []}

    def save(self, d: dict[str, Any]) -> None:
        s = json.dumps(d, indent=1, ensure_ascii=False)
        if s == self._saved:
            return
        tmp = self.wishlist.with_suffix(".tmp")
        tmp.write_text(s)
        tmp.replace(self.wishlist)
        self._saved = s

    def record(self, wl: dict[str, Any], eid: str, state: str, msg: str) -> bool:
        """Note un changement d'état. Ne fait rien (et retourne False) si rien n'a changé :
        l'historique et les notifications ne se répètent pas."""
        w = wl["wishes"].get(eid)
        if w is not None:
            if w.get("state") == state and w.get("last") == msg:
                return False
            w["state"] = state; w["last"] = msg
            w["updated_at"] = _iso(); w["ts"] = time.time()
        wl["history"].insert(0, {"at": _iso(), "event_id": eid, "name": w["name"] if w else eid,
                                 "state": state, "msg": msg})
        del wl["history"][100:]
        self.log.info("%s → %s (%s)", w["name"] if w else eid, state, msg)
        return True

    # ---------- Sporteasy ----------
    def relogin(self, force: bool = False) -> None:
        try:
            self.client.login(force=force)
        except LoginError as e:
            if not self._auth_alert:
                self._auth_alert = True
                fix = ("mets-le à jour dans EZ bad (ⓘ → Mon compte)" if self.kind == "db"
                       else "préviens l'admin pour qu'il le mette à jour")
                self.note("EZ bad — connexion impossible",
                          f"Sporteasy refuse la connexion ({e}). Le bot ne peut plus réserver pour toi. "
                          f"Si tu as changé ton mot de passe, {fix}.", "key")
            raise
        self._auth_alert = False

    def call(self, fn: Callable[[], Any]) -> Any:
        """Appel Sporteasy ; si la session a expiré, on se reconnecte une fois et on recommence."""
        try:
            return fn()
        except PermissionError:
            self.relogin()
            return fn()

    def events(self, force: bool = False) -> list[Event]:
        c = self.cache
        if not force and time.time() - c["at"] < 60:
            return c["events"]
        with self._ev_lock:
            if time.time() - c["at"] < (2 if force else 60):
                return c["events"]              # un autre fil vient de rafraîchir
            t0 = time.time()
            try:
                evs = self.call(lambda: self.client.upcoming(2))
            except Exception as e:
                self.last_err = _short_err(e)
                raise
            self.last_err = None
            c.update(at=time.time(), snap=t0, events=evs)
            return evs

    # ---------- règles ----------
    def pick_delay(self, wl: dict[str, Any], start_at: datetime) -> float:
        """Délai aléatoire (secondes) avant de réserver un créneau qui vient d'ouvrir."""
        try:
            delays = {**DEFAULT_DELAYS, **(wl["settings"].get("delays") or {})}
            rng = delays.get(_WD[start_at.astimezone(TZ).weekday()]) or delays.get("default") or [0, 0]
            lo, hi = sorted(max(0.0, float(x)) for x in list(rng)[:2])
        except Exception:  # noqa: BLE001
            lo, hi = DEFAULT_DELAYS["default"]
        return random.uniform(lo, hi) * 60

    def _week_count(self, wl: dict[str, Any], ev: Event, prio: int) -> int:
        """Nombre de créneaux où je suis inscrit la même semaine que ev (hors ev)."""
        wk, day = _week_key(ev.start_at), ev.start_at.isoformat()[:10]
        ids: set[str] = set()
        for eid, w in wl["wishes"].items():
            try:
                if w.get("state") == "booked" and _week_key(_parse(w["start_at"])) == wk:
                    ids.add(eid)
            except Exception:  # noqa: BLE001
                continue
        for e in self.cache["events"]:           # inscriptions faites à la main sur Sporteasy
            if e.mine and _week_key(e.start_at) == wk:
                ids.add(str(e.id))
        ids.discard(str(ev.id))
        if prio == 1:                            # un fallback du même jour sera libéré : il ne compte pas
            for eid, w in wl["wishes"].items():
                if int(w.get("priority", 1)) > 1 and w["start_at"][:10] == day:
                    ids.discard(eid)
        return len(ids)

    def _p1_same_day(self, wl: dict[str, Any], w: dict[str, Any]) -> tuple[str | None, datetime | None]:
        """Pour un fallback : où en est la priorité du même jour ? ("booked" | "soon" | None, heure de résa prévue)"""
        day, res, at = w["start_at"][:10], None, None
        for x in wl["wishes"].values():
            if x is w or int(x.get("priority", 1)) != 1 or x["start_at"][:10] != day:
                continue
            if x.get("state") == "booked":
                return "booked", None
            if x.get("state") == "delaying" and x.get("book_at"):
                try:
                    t = _parse(x["book_at"])
                except Exception:  # noqa: BLE001
                    continue
                if (now() - t).total_seconds() < 60:      # pas une résa restée coincée
                    res, at = "soon", max(t, at) if at else t
        return res, at

    def _trusted(self, w: dict[str, Any], snap: float | None) -> bool:
        """Peut-on conclure d'une *absence* (plus inscrit, plus en liste d'attente) ?
        Seulement si la photo de Sporteasy a été prise nettement après la dernière action du bot
        sur cette option (une minute de marge : la liste peut être en retard sur une action toute fraîche)."""
        return snap is not None and float(w.get("ts") or 0) < snap - 60

    def _sync_truth(self, wl: dict[str, Any], eid: str, w: dict[str, Any], ev: Event, snap: float | None = None) -> bool:
        """Recale l'option sur ce que dit Sporteasy. True = rien d'autre à faire pour cette option."""
        st = w.get("state") or "pending"
        if ev.is_cancelled:
            if self.record(wl, eid, "cancelled", "créneau annulé par le club"):
                self.note("EZ bad — créneau annulé", f"{ev.name} ({ev.when}) est annulé par le club", "wastebasket")
            return True
        if ev.mine:
            w.pop("miss", None)
            if st == "released" and not self._trusted(w, snap):
                return True                       # on vient de le libérer : la liste n'est pas encore à jour
            if st != "booked":
                w.pop("retry_at", None); w.pop("tries", None)
                if st == "waitlist":
                    msg = f"✅ Une place s'est libérée, tu es inscrit : {ev.name} ({ev.when})"
                elif st in ("delaying", "error"):
                    msg = f"✅ Réservé : {ev.name} ({ev.when})"
                else:
                    msg = "déjà inscrit"
                self.record(wl, eid, "booked", msg)
                if msg != "déjà inscrit":
                    self.note("EZ bad", msg, "white_check_mark,badminton")
                if int(w.get("priority", 1)) == 1 and st != "released":
                    self.save(wl)
                    self.release_fallbacks(ev, wl)
            return True
        if st == "released":
            return True
        if ev.my_group == "waiting_list":
            w.pop("miss", None)
            self.record(wl, eid, "waitlist", f"⏳ Liste d'attente : {ev.name} ({ev.when})")
            return True                           # déjà dans la file : on ne renvoie surtout pas de demande
        if st in ("booked", "waitlist"):
            # Sporteasy ne me montre plus inscrit / dans la file. On ne conclut qu'après deux constats
            # d'affilée, sur des lectures nettement postérieures à la dernière action du bot.
            if self._trusted(w, snap):
                w["miss"] = int(w.get("miss", 0)) + 1
                if w["miss"] >= 2:
                    w.pop("miss", None)
                    self.record(wl, eid, "released", "désinscrit sur Sporteasy" if st == "booked"
                                else "retiré de la liste d'attente sur Sporteasy")
            return True
        return False

    # ---------- réservation ----------
    def try_book(self, ev: Event, wl: dict[str, Any]) -> None:
        """Créneau ouvert, délai écoulé : applique les règles puis réserve.
        À appeler verrou tenu, avec une wishlist fraîchement chargée."""
        eid = str(ev.id); w = wl["wishes"][eid]; prio = int(w.get("priority", 1))
        if self._sync_truth(wl, eid, w, ev):
            self.save(wl); return
        w.pop("retry_at", None)
        if self._week_count(wl, ev, prio) >= int(wl["settings"].get("max_per_week", MAX_PER_WEEK)):
            self.record(wl, eid, "skipped", "quota hebdo atteint"); self.save(wl); return
        if prio > 1:
            p1, at = self._p1_same_day(wl, w)
            if p1 == "booked":
                self.record(wl, eid, "skipped", "un créneau prioritaire du même jour est déjà réservé")
                self.save(wl); return
            if p1 == "soon":                      # la priorité est en train de passer : on attend son résultat
                w["retry_at"] = _iso(max(at or now(), now()) + timedelta(seconds=3))
                self.save(wl); return
        if ev.full and not w.get("waitlist_ok", True):
            if self.record(wl, eid, "full", "complet (liste d'attente refusée)"):
                self.note("EZ bad — complet", f"{ev.name} ({ev.when})", "no_entry")
            self.save(wl); return
        try:
            result, _ = self.call(lambda: self.client.book(ev))
        except Exception as e:  # noqa: BLE001
            self._book_failed(wl, eid, ev, e); return
        self.cache["at"] = 0.0                    # l'interface relira Sporteasy
        if result == "booked":
            w.pop("tries", None)
            msg = f"✅ Réservé : {ev.name} ({ev.when})"
            self.record(wl, eid, "booked", msg); self.save(wl)
            self.note("EZ bad", msg, "white_check_mark,badminton")
            if prio == 1:
                self.release_fallbacks(ev, wl)
        elif result == "waitlist":
            w.pop("tries", None)
            msg = f"⏳ Liste d'attente : {ev.name} ({ev.when})"
            self.record(wl, eid, "waitlist", msg); self.save(wl)
            self.note("EZ bad", msg, "hourglass")
        else:
            self._book_failed(wl, eid, ev, RuntimeError("demande envoyée mais Sporteasy n'a pas confirmé l'inscription"))

    def _book_failed(self, wl: dict[str, Any], eid: str, ev: Event, e: BaseException) -> None:
        """Échec de réservation : on réessaie seul, de plus en plus espacé, et on prévient une seule fois."""
        w = wl["wishes"][eid]
        n = int(w.get("tries", 0)) + 1
        wait = min(3 * 2 ** (n - 1), 900)          # 3 s, 6 s, 12 s… plafonné à 15 min
        w["tries"] = n
        w["retry_at"] = _iso(now() + timedelta(seconds=wait))
        err = _short_err(e)
        self.record(wl, eid, "error", err)
        self.save(wl)
        self.log.warning("réservation de %s : échec n°%d (%s), nouvel essai dans %d s", ev.name, n, err, wait)
        if n == 1:
            self.note("EZ bad — erreur", f"{ev.name} ({ev.when}) : {err}. Je réessaie tout seul, "
                                          "mais jette un œil sur Sporteasy si c'est urgent.", "x")

    def release_fallbacks(self, ev: Event, wl: dict[str, Any]) -> None:
        """Une priorité est réservée : on libère les fallbacks du même jour (inscrits ou en liste d'attente)."""
        d = ev.start_at.isoformat()[:10]
        for eid, x in list(wl["wishes"].items()):
            if eid == str(ev.id) or int(x.get("priority", 1)) <= 1 or x["start_at"][:10] != d:
                continue
            if x.get("state") not in ("booked", "waitlist"):
                continue
            try:
                fe = self.call(lambda eid=eid: self.client.event(int(eid)))
                self.call(lambda fe=fe: self.client.set_attendance(fe, "absent"))
                self.record(wl, eid, "released", "libéré : un créneau prioritaire du même jour est réservé")
                self.save(wl)
                self.cache["at"] = 0.0
                self.note("EZ bad — libéré", f"{fe.name} ({fe.when})", "leftwards_arrow_with_hook")
            except Exception as e:  # noqa: BLE001
                self.log.warning("libération du fallback %s : %s", x.get("name", eid), e)
                self.note("EZ bad — à libérer à la main",
                          f"Ta priorité est réservée mais je n'ai pas réussi à te désinscrire de "
                          f"{x.get('name', 'ton fallback')} : fais-le sur Sporteasy.", "warning")

    def schedule_or_book(self, ev: Event) -> None:
        """Créneau ouvert : tire un délai aléatoire, puis réserve une fois le délai écoulé.
        Attentes courtes (≤ 90 s) : à la seconde près, dans ce fil. Plus longues : reprises au passage suivant."""
        eid = str(ev.id)
        with self.lock:
            if eid in self.inflight:
                return
            wl = self.load(); w = wl["wishes"].get(eid)
            if not w or wl["settings"].get("paused") or w.get("state") in DONE or w.get("state") == "waitlist":
                return
            t = now()
            if not w.get("book_at"):
                d = self.pick_delay(wl, ev.start_at)
                w["book_at"] = _iso(t + timedelta(seconds=d))
                self.record(wl, eid, "delaying", f"ouvert, je réserve dans {int(d // 60)}m{int(d % 60):02d}" if d >= 1 else "ouvert, je réserve tout de suite")
                self.save(wl)
            else:
                due = w.get("retry_at") or w["book_at"]
                try:
                    d = (_parse(due) - t).total_seconds()
                except Exception:  # noqa: BLE001
                    d = 0.0
            if d > 90:
                return
            self.inflight.add(eid)
        try:
            for _ in range(12):
                if d > 0:
                    time.sleep(d)
                fresh = ev
                if d > 30:                         # longue attente : le créneau a pu être annulé ou réservé à la main
                    try:
                        fresh = self.client.event(ev.id, timeout=(2, 3))
                    except Exception:  # noqa: BLE001
                        fresh = ev
                with self.lock:
                    wl = self.load(); w = wl["wishes"].get(eid)
                    if not w or wl["settings"].get("paused") or w.get("state") in DONE or w.get("state") == "waitlist":
                        return
                    self.try_book(fresh, wl)
                    w = self.load()["wishes"].get(eid) or {}
                # échec ou fallback en attente de sa priorité : si le prochain essai est proche, on reste dessus
                if w.get("state") not in ("error", "delaying") or not w.get("retry_at"):
                    return
                d = (_parse(w["retry_at"]) - now()).total_seconds()
                if d > 90:
                    return
                d = max(d, 0.5)
        finally:
            with self.lock:
                self.inflight.discard(eid)

    def _wanted(self, eid: str) -> bool:
        try:
            wl = self.load(); w = wl["wishes"].get(eid)
        except Exception:  # noqa: BLE001
            return True
        return bool(w) and not wl["settings"].get("paused") and w.get("state") not in DONE and w.get("state") != "waitlist"

    def hot_wait(self, ev: Event, target: datetime, max_min: float) -> None:
        """Poll rapide autour d'une heure d'ouverture probable (fil dédié) : 1 s pendant max_min,
        puis, pour l'heure officielle, encore WARM_MIN minutes à 5 s si le club ouvre très en retard."""
        eid = str(ev.id)
        hot_until = target + timedelta(minutes=max_min)
        deadline = hot_until + timedelta(minutes=WARM_MIN if max_min >= HOT_MAX_MIN else 0)
        self.log.info("⏱  mode rapide sur %s (ouverture possible %s)", ev.name, target.astimezone(TZ).strftime("%H:%M"))
        n = 0
        while now() < deadline and self.active:
            if n % 15 == 0 and not self._wanted(eid):
                return                              # option retirée, déjà traitée, ou bot en pause
            n += 1
            try:
                fresh = self.client.event(ev.id, timeout=FAST)
            except PermissionError:
                try:
                    self.relogin()
                except Exception as e:  # noqa: BLE001
                    self.log.warning("reconnexion : %s", e); time.sleep(10)
                continue
            except EventGone:
                return
            except Exception as e:  # noqa: BLE001
                self.log.warning("poll rapide : %s", _short_err(e)); time.sleep(HOT_INTERVAL * 2); continue
            if fresh.is_cancelled:
                return
            if fresh.open:
                self.log.info("🟢 %s ouvert à %s", ev.name, now().strftime("%H:%M:%S"))
                self.schedule_or_book(fresh)
                return
            time.sleep(HOT_INTERVAL if now() < hot_until else 5.0)

    def _hot_at(self, ev: Event, target: datetime, max_min: float) -> None:
        wait = (target - now()).total_seconds() - 5
        if wait > 0:
            time.sleep(wait)
        self.hot_wait(ev, target, max_min)

    # ---------- watcher ----------
    def _step(self, wl: dict[str, Any], eid: str, w: dict[str, Any], ev: Event | None, gone: bool,
              snap: float, spawn: list[tuple[Callable[..., None], tuple[Any, ...]]]) -> None:
        """Fait avancer une option d'un cran. Verrou tenu ; aucun appel réseau lent ici."""
        st = w.get("state") or "pending"
        try:
            begins = _parse(w["start_at"])
        except Exception:  # noqa: BLE001
            return
        t = now()
        if begins <= t:
            if t - begins > timedelta(days=KEEP_DAYS):
                del wl["wishes"][eid]               # vieux créneau : on fait le ménage
            return
        if st == "cancelled" or float(w.get("ts") or 0) > snap:
            return                                  # terminé, ou modifié depuis la photo : au prochain passage
        if ev is None:
            # Absent du calendrier ET introuvable en direct → supprimé par le club
            if gone and st != "released" and self.record(wl, eid, "cancelled", "créneau supprimé par le club"):
                self.note("EZ bad — créneau supprimé", f"{w['name']} ({fr_when(begins)}) n'existe plus sur Sporteasy", "wastebasket")
            return
        # le club peut renommer ou décaler un créneau : on garde l'option à jour
        w["name"] = ev.name; w["start_at"] = ev.start_at.isoformat()
        w["registration_open_at"] = ev.registration_open_at.isoformat() if ev.registration_open_at else None
        if self._sync_truth(wl, eid, w, ev, snap):
            return
        if ev.open:
            spawn.append((self.schedule_or_book, (ev,)))
            return
        if ev.registration_open_at:
            # Sporteasy ouvre parfois 1 h avant l'heure affichée (changement d'heure) :
            # on surveille en rapide autour des deux heures possibles.
            for target, max_min in ((ev.registration_open_at - timedelta(hours=1), 8),
                                    (ev.registration_open_at, HOT_MAX_MIN)):
                key = (eid, target.isoformat())
                if key not in self.hot_started and -60 <= (target - t).total_seconds() <= HOT_WINDOW:
                    self.hot_started.add(key)
                    spawn.append((self._hot_at, (ev, target, max_min)))
        if st != "delaying":
            w.pop("retry_at", None); w.pop("book_at", None)
            self.record(wl, eid, "waiting", f"ouverture prévue {fr_when(ev.registration_open_at)}" if ev.registration_open_at else "en attente")

    def tick(self) -> None:
        """Un passage du watcher : lit Sporteasy, recale chaque option, lance ce qui doit l'être."""
        with self.lock:
            wl = self.load()
        if wl["settings"].get("paused"):
            return
        t = now()
        live: set[str] = set()                      # options à venir encore suivies
        for eid, w in wl["wishes"].items():
            try:
                if (w.get("state") or "pending") not in ("cancelled", "released") and _parse(w["start_at"]) > t:
                    live.add(eid)
            except Exception:  # noqa: BLE001
                continue
        todo = {eid for eid in live if wl["wishes"][eid].get("state") != "booked"}
        # Rien à réserver : on se contente de vérifier toutes les 5 min que les réservations tiennent toujours.
        if not live or (not todo and time.time() - self._last_sync < 300):
            if len(wl["wishes"]) > len(live):       # ménage des vieilles options, sans appel réseau
                with self.lock:
                    wl = self.load()
                    for eid, w in list(wl["wishes"].items()):
                        self._step(wl, eid, w, None, False, 0.0, [])
                    self.save(wl)
            return
        events = {str(e.id): e for e in self.events(force=True)}
        snap = float(self.cache.get("snap") or 0.0)
        gone: set[str] = set()
        for eid in live - set(events):
            # Absent de la liste : on vérifie ce créneau en direct avant de le déclarer supprimé
            try:
                events[eid] = self.call(lambda eid=eid: self.client.event(int(eid)))
            except EventGone:
                gone.add(eid)
            except Exception as e:  # noqa: BLE001
                self.log.warning("vérification du créneau %s : %s", eid, _short_err(e))
        spawn: list[tuple[Callable[..., None], tuple[Any, ...]]] = []
        with self.lock:
            wl = self.load()
            if wl["settings"].get("paused"):
                return
            for eid, w in list(wl["wishes"].items()):
                self._step(wl, eid, w, events.get(eid), eid in gone, snap, spawn)
            self.save(wl)
        self._last_sync = time.time()
        for fn, args in spawn:
            threading.Thread(target=fn, args=args, daemon=True).start()

    def watcher(self) -> None:
        self.log.info("watcher démarré")
        try:
            if not self.client.is_logged_in():
                self.relogin()
        except Exception as e:  # noqa: BLE001
            self.log.error("connexion impossible au démarrage : %s (bouton Re-login pour réessayer)", e)
        while self.active:
            try:
                self.tick()
            except (requests.RequestException, LoginThrottled, LoginError) as e:
                self.log.warning("Sporteasy : %s — nouvel essai dans %d s", _short_err(e), TICK)
            except Exception as e:  # noqa: BLE001
                self.log.exception("watcher : %s", e)
            self._wake.wait(TICK)
            self._wake.clear()


class _Ring(logging.Handler):
    def __init__(self, buf: deque[str]):
        super().__init__(); self.buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        self.buf.append(f"{datetime.fromtimestamp(record.created, TZ):%d/%m %H:%M:%S} {record.getMessage()}")


def start(acc: Account) -> None:
    threading.Thread(target=acc.watcher, daemon=True, name=f"watcher-{acc.name}").start()


# ================================================================= store chiffré (auto-inscription)
DB_LOCK = threading.RLock()
ACCOUNTS_FILE = DATA / "accounts.json"
INVITES_FILE = DATA / "invites.json"
RESERVED = {"admin", "api", "static", "join", "i", "u", "root", "www", "healthz"}


def _fernet():
    from cryptography.fernet import Fernet
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest()))
    kf = DATA / "secret.key"                  # repli : clé générée une fois sur le volume
    if not kf.exists():
        kf.write_text(Fernet.generate_key().decode()); kf.chmod(0o600)
    return Fernet(kf.read_text().strip().encode())


def encrypt(txt: str) -> str:
    return _fernet().encrypt(txt.encode()).decode()


def decrypt(tok: str) -> str:
    return _fernet().decrypt(tok.encode()).decode()


def _read(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception as e:  # noqa: BLE001
        log.error("%s illisible : %s", path.name, e); return {}


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp"); tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False)); tmp.chmod(0o600)
    tmp.replace(path)


def _db_account(name: str, rec: dict[str, Any]) -> Account:
    return Account(name, rec["email"], decrypt(rec["pw"]), rec["token"], rec.get("invite", ""), rec.get("ntfy", ""),
                   rec.get("email_notif", ""), int(rec.get("team_id") or 0), rec.get("team_host", ""), kind="db")


# ================================================================= accounts from env
def load_accounts() -> dict[str, Account]:
    accs: dict[str, Account] = {}
    i = 1
    while os.environ.get(f"USER{i}_EMAIL"):
        name = (os.environ.get(f"USER{i}_NAME") or f"user{i}").strip().lower()
        accs[name] = Account(name, os.environ[f"USER{i}_EMAIL"], os.environ.get(f"USER{i}_PASSWORD", ""),
                             os.environ.get(f"USER{i}_TOKEN", "").strip(), os.environ.get(f"USER{i}_INVITE", ""),
                             os.environ.get(f"USER{i}_NTFY", ""), os.environ.get(f"USER{i}_EMAIL_NOTIF", ""))
        i += 1
    with DB_LOCK:
        for name, rec in _read(ACCOUNTS_FILE).items():
            if name in accs:
                continue
            try:
                accs[name] = _db_account(name, rec)
            except Exception as e:  # noqa: BLE001
                log.error("compte %s illisible (clé de chiffrement changée ?) : %s", name, e)
    if not accs and os.environ.get("SPORTEASY_EMAIL"):   # compat mono-compte
        accs["ao"] = Account("ao", os.environ["SPORTEASY_EMAIL"], os.environ.get("SPORTEASY_PASSWORD", ""),
                             os.environ.get("ACCESS_TOKEN", "").strip())
    if not accs:
        raise SystemExit("aucun compte : définis USER1_EMAIL / USER1_PASSWORD / USER1_TOKEN (et USER1_NAME)")
    if PUBLIC:
        for a in accs.values():
            if not a.token:
                # Sans token, la page du compte serait ouverte à tout le monde : on la verrouille
                # (le bot continue de réserver pour ce compte).
                a.token = secrets.token_hex(24)
                log.error("USER…_TOKEN manquant pour %s : sa page est verrouillée tant que la variable n'est pas définie", a.name)
    return accs


ACCOUNTS = load_accounts()

# ================================================================= API
app = FastAPI(title="EZ bad", docs_url=None, redoc_url=None, openapi_url=None)


def _same(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode(), b.encode())


def _account(request: Request, name: str) -> Account:
    acc = ACCOUNTS.get(name.lower())
    if acc is None:
        raise HTTPException(404, "compte inconnu")
    if acc.token:
        tok = request.query_params.get("token") or request.cookies.get(f"token_{acc.name}") or ""
        if not _same(tok, acc.token):
            raise HTTPException(401, "token invalide")
    return acc


def _https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip() == "https"


def _set_cookie(resp, acc: Account, request: Request):
    if acc.token:
        resp.set_cookie(f"token_{acc.name}", acc.token, max_age=YEAR, httponly=True, samesite="lax",
                        secure=_https(request))
    return resp


@app.middleware("http")
async def _cookie(request: Request, call_next):
    resp = await call_next(request)
    tok = request.query_params.get("token")
    parts = request.url.path.split("/")
    if tok and len(parts) > 2 and parts[1] == "u":
        acc = ACCOUNTS.get(parts[2].lower())
        if acc and acc.token and _same(tok, acc.token):
            _set_cookie(resp, acc, request)
    return resp


# ---------- limite d'essais (liens d'invitation, inscription) ----------
_HITS: dict[str, deque[float]] = {}
_HITS_LOCK = threading.Lock()


def _ip(request: Request) -> str:
    return (request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
            or (request.client.host if request.client else "?")).strip()


def _limited(request: Request, bucket: str, per_ip: int = 15, overall: int = 150, window: int = 600) -> bool:
    """Trop d'essais ratés récemment (par adresse, ou tous confondus) ?"""
    t = time.time()
    with _HITS_LOCK:
        for key, lim in ((f"{bucket}:{_ip(request)}", per_ip), (f"{bucket}:*", overall)):
            q = _HITS.get(key)
            if not q:
                continue
            while q and t - q[0] > window:
                q.popleft()
            if len(q) >= lim:
                return True
    return False


def _fail(request: Request, bucket: str) -> None:
    t = time.time()
    with _HITS_LOCK:
        if len(_HITS) > 5000:
            _HITS.clear()
        for key in (f"{bucket}:{_ip(request)}", f"{bucket}:*"):
            _HITS.setdefault(key, deque(maxlen=500)).append(t)


def _page(name: str, status: int = 200, cache: str = "no-store") -> FileResponse:
    return FileResponse(BASE / "static" / name, status_code=status, headers={"Cache-Control": cache})


class Wish(BaseModel):
    event_id: int
    priority: int = 1
    waitlist_ok: bool = True


class Settings(BaseModel):
    paused: bool | None = None
    max_per_week: int | None = None
    delays: dict[str, list[float]] | None = None


@app.get("/healthz")
def healthz():
    return {"ok": True, "version": VERSION}


def _known_account(request: Request) -> "Account | None":
    """Compte dont le cookie est présent et valide dans ce navigateur."""
    for acc in ACCOUNTS.values():
        if acc.token and _same(request.cookies.get(f"token_{acc.name}") or "", acc.token):
            return acc
    return None


@app.get("/i/{code}")
def invite(request: Request, code: str):
    """Lien perso : /i/<code> → pose le cookie du compte et redirige vers sa page.
    Variables : USERn_INVITE=<code> ; ou on dérive du token."""
    known = _known_account(request)
    if known is None and _limited(request, "lien"):
        raise HTTPException(429, "trop d'essais, réessaie dans quelques minutes")
    for acc in ACCOUNTS.values():
        if acc.token and code in (acc.invite, acc.token[:8]):
            return _set_cookie(RedirectResponse(f"/u/{acc.name}/", status_code=302), acc, request)
    if known:       # ancien lien (code changé depuis) mais ce téléphone est déjà connecté : on l'amène chez lui
        return _set_cookie(RedirectResponse(f"/u/{known.name}/", status_code=302), known, request)
    _fail(request, "lien")
    return _page("locked.html", 404)


@app.get("/")
def root(request: Request):
    acc = _known_account(request)
    if acc:
        return RedirectResponse(f"/u/{acc.name}/", status_code=302)
    if len(ACCOUNTS) == 1 and not PUBLIC:
        return RedirectResponse(f"/u/{next(iter(ACCOUNTS))}/")
    return _page("locked.html", 404)


@app.get("/static/")
def static_root(request: Request):
    return root(request)


@app.get("/u/{name}/manifest.json")
def manifest(request: Request, name: str):
    acc = ACCOUNTS.get(name.lower())
    if acc is None:
        raise HTTPException(404, "compte inconnu")
    # L'app de l'écran d'accueil iOS n'a pas toujours les cookies de Safari : si la personne est
    # authentifiée, l'icône démarre sur son lien perso (/i/…) qui repose le cookie puis ouvre sa page.
    authed = bool(acc.token) and _same(request.cookies.get(f"token_{acc.name}") or "", acc.token)
    start_url = acc.link if authed else f"/u/{acc.name}/"
    return JSONResponse({
        "name": "EZ bad", "short_name": "EZ bad", "id": f"/u/{acc.name}/",
        "start_url": start_url, "scope": "/", "display": "standalone",
        "background_color": "#0f1115", "theme_color": "#0f1115",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json", headers={"Cache-Control": "no-store"})


@app.get("/u/{name}/")
def index(request: Request, name: str):
    try:
        acc = _account(request, name)
    except HTTPException as e:
        return _page("locked.html", e.status_code)
    # no-cache : après un déploiement, le téléphone prend la nouvelle version au prochain lancement
    return _set_cookie(_page("index.html", cache="no-cache"), acc, request)


@app.get("/u/{name}/api/state")
def state(request: Request, name: str):
    acc = _account(request, name)
    stale = False
    try:
        events = acc.events(); err = None
    except Exception as e:  # noqa: BLE001
        t = now()   # Sporteasy ne répond pas : on montre le dernier calendrier connu plutôt qu'une page vide
        events, err, stale = [e2 for e2 in acc.cache["events"] if e2.start_at > t], _short_err(e), True
    with acc.lock:
        wl = acc.load()
    return {"user": acc.name, "events": [e.to_json() for e in events], "wishes": wl["wishes"],
            "settings": wl["settings"], "history": wl["history"][:30], "log": list(acc.recent)[-40:],
            "error": err, "stale": stale, "ntfy": bool(acc.ntfy or RESEND_KEY), "now": _iso(), "version": VERSION,
            "account": {"kind": acc.kind, "admin": acc.name in ADMINS, "club": acc.team_host, "email": acc.email,
                        "ntfy_topic": acc.ntfy, "link": acc.link}}


def _events(acc: Account, force: bool = False) -> list[Event]:
    try:
        return acc.events(force=force)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, _short_err(e)) from None


@app.post("/u/{name}/api/wish")
def add_wish(request: Request, name: str, w: Wish):
    acc = _account(request, name)
    if w.priority not in (1, 2):
        raise HTTPException(400, "priorité invalide")
    ev = next((e for e in _events(acc) if e.id == w.event_id), None)
    if ev is None:
        raise HTTPException(404, "événement inconnu")
    with acc.lock:
        wl = acc.load()
        prev = wl["wishes"].get(str(ev.id), {})
        keep = prev.get("state") in ("booked", "waitlist")      # déjà acquis : on ne change que la priorité
        wl["wishes"][str(ev.id)] = {
            "name": ev.name, "start_at": ev.start_at.isoformat(),
            "registration_open_at": ev.registration_open_at.isoformat() if ev.registration_open_at else None,
            "priority": w.priority, "waitlist_ok": w.waitlist_ok,
            "state": prev["state"] if keep else "pending",
            "added_at": prev.get("added_at") or _iso(), "ts": time.time(),
        }
        if keep:
            for k in ("last", "updated_at", "ts", "book_at"):
                if k in prev:
                    wl["wishes"][str(ev.id)][k] = prev[k]
        acc.save(wl)
    acc.kick()          # si le créneau est déjà ouvert, on n'attend pas le prochain passage
    return wl["wishes"][str(ev.id)]


@app.delete("/u/{name}/api/wish/{event_id}")
def del_wish(request: Request, name: str, event_id: int):
    acc = _account(request, name)
    with acc.lock:
        wl = acc.load(); wl["wishes"].pop(str(event_id), None); acc.save(wl)
    return {"ok": True}


@app.post("/u/{name}/api/settings")
def set_settings(request: Request, name: str, s: Settings):
    acc = _account(request, name)
    if s.max_per_week is not None and not 1 <= s.max_per_week <= 21:
        raise HTTPException(400, "max par semaine : entre 1 et 21")
    delays = None
    if s.delays is not None:
        delays = {}
        for k, v in s.delays.items():
            if k not in (*_WD, "default") or len(v) != 2 or not all(0 <= x <= 180 for x in v):
                raise HTTPException(400, "délais invalides : deux valeurs en minutes (0 à 180) par jour")
            delays[k] = sorted(float(x) for x in v)
    with acc.lock:
        wl = acc.load()
        if s.paused is not None:
            wl["settings"]["paused"] = s.paused
        if s.max_per_week is not None:
            wl["settings"]["max_per_week"] = s.max_per_week
        if delays is not None:
            wl["settings"]["delays"] = delays
        acc.save(wl)
    acc.kick()
    return wl["settings"]


@app.post("/u/{name}/api/test-notif")
def test_notif(request: Request, name: str):
    acc = _account(request, name)
    if not acc.ntfy and not RESEND_KEY:
        raise HTTPException(400, "aucun canal : renseigne ton canal ntfy dans ⓘ → Mon compte")
    if time.time() - acc._last_test < 5:
        raise HTTPException(429, "une notif vient de partir, patiente quelques secondes")
    acc._last_test = time.time()
    res = _deliver({"title": "EZ bad", "msg": f"Test de notification : ça marche 🏸 ({now():%H:%M:%S})",
                    "ntfy": acc.ntfy, "tags": "white_check_mark", "email": acc.email_notif,
                    "click": f"{PUBLIC_URL}/u/{acc.name}/" if PUBLIC_URL else ""})
    if acc.ntfy and res.get("ntfy") != "ok":
        raise HTTPException(502, f"ntfy n'a pas pris la notification ({res.get('ntfy')}) — réessaie plus tard")
    return {"ok": True, "ntfy": acc.ntfy or None, "email": acc.email_notif if RESEND_KEY else None}


@app.post("/u/{name}/api/login")
def force_login(request: Request, name: str):
    acc = _account(request, name)
    try:
        acc.relogin(force=True); acc.events(force=True); acc.kick()
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, _short_err(e)) from None


@app.post("/u/{name}/api/book-now/{event_id}")
def book_now(request: Request, name: str, event_id: int):
    acc = _account(request, name)
    ev = next((e for e in _events(acc, force=True) if e.id == event_id), None)
    if ev is None or not ev.open:
        raise HTTPException(400, "créneau introuvable ou pas encore ouvert")
    with acc.lock:
        wl = acc.load()
        wl["wishes"].setdefault(str(ev.id), {"name": ev.name, "start_at": ev.start_at.isoformat(), "priority": 1,
                                             "waitlist_ok": True, "state": "pending", "added_at": _iso()})
        acc.save(wl); acc.try_book(ev, wl); wl = acc.load()
    return wl["wishes"][str(ev.id)]


@app.post("/u/{name}/api/release/{event_id}")
def release(request: Request, name: str, event_id: int):
    """Se désinscrire d'un créneau (ou quitter sa liste d'attente) et retirer l'option."""
    acc = _account(request, name)
    try:
        ev = acc.call(lambda: acc.client.event(event_id))
        acc.call(lambda: acc.client.set_attendance(ev, "absent"))
    except EventGone:
        raise HTTPException(404, "ce créneau n'existe plus sur Sporteasy") from None
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, _short_err(e)) from None
    acc.cache["at"] = 0.0
    with acc.lock:
        wl = acc.load(); wl["wishes"].pop(str(event_id), None)
        wl["history"].insert(0, {"at": _iso(), "event_id": str(event_id),
                                 "name": ev.name, "state": "released", "msg": "désinscrit manuellement"})
        del wl["history"][100:]
        acc.save(wl)
    acc.log.info("%s → désinscrit à la demande", ev.name)
    return {"ok": True}


# ================================================================= invitations & auto-inscription
def _valid_invite(code: str) -> dict[str, Any] | None:
    inv = _read(INVITES_FILE).get(code)
    if not inv or inv.get("used_by") or inv.get("revoked"):
        return None
    if _parse(inv["expires_at"]) < now():
        return None
    return inv


def _admin(request: Request, name: str) -> Account:
    acc = _account(request, name)
    if acc.name not in ADMINS:
        raise HTTPException(403, "réservé à l'admin")
    return acc


@app.post("/u/{name}/api/invites")
def create_invite(request: Request, name: str):
    acc = _admin(request, name)
    code = secrets.token_urlsafe(8)
    with DB_LOCK:
        inv = _read(INVITES_FILE)
        inv[code] = {"created_by": acc.name, "created_at": _iso(),
                     "expires_at": _iso(now() + timedelta(days=INVITE_DAYS)),
                     "used_by": None, "fails": 0}
        _write(INVITES_FILE, inv)
    return {"code": code, "path": f"/join/{code}", "expires_days": INVITE_DAYS}


@app.get("/u/{name}/api/invites")
def list_invites(request: Request, name: str):
    _admin(request, name)
    t = now()
    inv = _read(INVITES_FILE)
    pending = [{"code": c, "path": f"/join/{c}", "expires_at": v["expires_at"]} for c, v in inv.items()
               if not v.get("used_by") and not v.get("revoked") and _parse(v["expires_at"]) > t]
    members = [{"name": a.name, "club": a.team_host, "kind": a.kind, "ok": a.last_err is None and not a._auth_alert}
               for a in ACCOUNTS.values()]
    return {"pending": pending, "members": members}


@app.delete("/u/{name}/api/invites/{code}")
def revoke_invite(request: Request, name: str, code: str):
    _admin(request, name)
    with DB_LOCK:
        inv = _read(INVITES_FILE)
        if code in inv:
            inv[code]["revoked"] = True; _write(INVITES_FILE, inv)
    return {"ok": True}


@app.get("/join/{code}")
def join_page(request: Request, code: str):
    if _limited(request, "invitation"):
        raise HTTPException(429, "trop d'essais, réessaie dans quelques minutes")
    if not _valid_invite(code):
        _fail(request, "invitation")
        return _page("join-invalid.html", 404)
    return _page("join.html")


class Join(BaseModel):
    name: str
    email: str
    password: str
    club: str = ""
    ntfy: str = ""


def _club_host(club: str) -> str:
    c = (club or "").strip().lower()
    if not c:
        return TEAM_HOST
    c = re.sub(r"^https?://", "", c).split("/")[0]
    if "." not in c:
        c = f"{c}.sporteasy.net"
    if not re.fullmatch(r"[a-z0-9-]+\.sporteasy\.net", c):
        raise HTTPException(400, "adresse de club invalide (ex. monclub.sporteasy.net)")
    return c


@app.post("/join/{code}")
def join_submit(request: Request, code: str, j: Join):
    if _limited(request, "invitation"):
        raise HTTPException(429, "trop d'essais, réessaie dans quelques minutes")
    with DB_LOCK:
        inv = _valid_invite(code)
    if not inv:
        _fail(request, "invitation")
        raise HTTPException(410, "invitation expirée ou déjà utilisée — demande-en une nouvelle")
    name = re.sub(r"[^a-z0-9-]", "", j.name.strip().lower().replace(" ", "-"))[:20].strip("-")
    if len(name) < 2:
        raise HTTPException(400, "prénom trop court (lettres et chiffres, sans accents)")
    if name in ACCOUNTS or name in RESERVED or (DATA / name).exists():
        raise HTTPException(409, f"« {name} » est déjà pris, choisis un autre nom")
    email = j.email.strip()
    if "@" not in email or not j.password or len(email) > 200 or len(j.password) > 200:
        raise HTTPException(400, "email et mot de passe Sporteasy obligatoires")
    host = _club_host(j.club)
    if any(a.email.lower() == email.lower() and a.team_host == host for a in ACCOUNTS.values()):
        raise HTTPException(409, "ce compte Sporteasy est déjà branché sur EZ bad")
    ntfy = re.sub(r"[^A-Za-z0-9_-]", "", j.ntfy.strip())[:64]

    # Test de connexion en direct (session stockée dans un dossier temporaire)
    tmp = DATA / f".pending-{secrets.token_hex(4)}"
    tmp.mkdir(parents=True, exist_ok=True)
    client = SporteasyClient(TEAM_ID if host == TEAM_HOST else 0, host, email, j.password, session_file=tmp / "session.json")
    try:
        client.login()
    except LoginError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        _fail(request, "invitation")
        with DB_LOCK:
            invs = _read(INVITES_FILE)
            if code in invs:
                invs[code]["fails"] = invs[code].get("fails", 0) + 1
                if invs[code]["fails"] >= 5:
                    invs[code]["revoked"] = True
                _write(INVITES_FILE, invs)
        raise HTTPException(401, str(e)) from None
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        log.warning("inscription %s : %s", name, e)
        raise HTTPException(502, "Sporteasy ne répond pas, réessaie dans une minute") from None

    with DB_LOCK:
        if not _valid_invite(code) or name in ACCOUNTS:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(409, "invitation déjà utilisée entre-temps")
        tmp.rename(DATA / name)
        rec = {"email": email, "pw": encrypt(j.password), "token": secrets.token_hex(24),
               "invite": f"{name}-{secrets.token_hex(4)}", "ntfy": ntfy, "team_id": client.team_id, "team_host": host,
               "invited_by": inv["created_by"], "created_at": _iso()}
        accs = _read(ACCOUNTS_FILE); accs[name] = rec; _write(ACCOUNTS_FILE, accs)
        invs = _read(INVITES_FILE); invs[code]["used_by"] = name; _write(INVITES_FILE, invs)
        acc = _db_account(name, rec)
        ACCOUNTS[name] = acc
    start(acc)
    log.info("nouveau compte %s (club %s, invité par %s)", name, host, inv["created_by"])
    for a in list(ACCOUNTS.values()):
        if a.name == inv["created_by"]:
            a.note("EZ bad — nouveau membre", f"{name} a rejoint EZ bad ({host})", "tada")
    return _set_cookie(JSONResponse({"ok": True, "redirect": f"/u/{name}/"}), acc, request)


class Pwd(BaseModel):
    password: str


@app.post("/u/{name}/api/account/password")
def change_password(request: Request, name: str, body: Pwd):
    acc = _account(request, name)
    if acc.kind != "db":
        raise HTTPException(400, "ce compte est géré dans les variables Railway, demande à l'admin")
    if not body.password or len(body.password) > 200:
        raise HTTPException(400, "mot de passe vide")
    test = SporteasyClient(acc.team_id, acc.team_host, acc.email, body.password, session_file=acc.dir / "session.json")
    try:
        test.login()
    except LoginError as e:
        raise HTTPException(401, str(e)) from None
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Sporteasy ne répond pas, réessaie dans une minute") from None
    with DB_LOCK:
        accs = _read(ACCOUNTS_FILE); accs[acc.name]["pw"] = encrypt(body.password); _write(ACCOUNTS_FILE, accs)
    acc.client.set_password(body.password)
    acc._auth_alert = False
    acc.kick()
    return {"ok": True}


class Topic(BaseModel):
    topic: str


@app.post("/u/{name}/api/account/ntfy")
def set_ntfy(request: Request, name: str, body: Topic):
    acc = _account(request, name)
    topic = re.sub(r"[^A-Za-z0-9_-]", "", body.topic.strip())[:64]
    with acc.lock:
        wl = acc.load(); wl["settings"]["ntfy"] = topic; acc.save(wl)
    return {"ok": True, "topic": topic}


@app.delete("/u/{name}/api/account")
def delete_account(request: Request, name: str):
    acc = _account(request, name)
    if acc.kind != "db":
        raise HTTPException(400, "ce compte est géré dans les variables Railway, demande à l'admin")
    acc.active = False
    acc.kick()
    with DB_LOCK:
        accs = _read(ACCOUNTS_FILE); accs.pop(acc.name, None); _write(ACCOUNTS_FILE, accs)
        ACCOUNTS.pop(acc.name, None)
    shutil.rmtree(acc.dir, ignore_errors=True)
    resp = JSONResponse({"ok": True}); resp.delete_cookie(f"token_{acc.name}")
    log.info("compte %s supprimé par son propriétaire", acc.name)
    return resp


# monté en dernier : les routes explicites (dont /static/) passent avant
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


if __name__ == "__main__":
    import uvicorn
    log.info("EZ bad %s — %d compte(s), fuseau %s", VERSION, len(ACCOUNTS), TZ)
    for acc in ACCOUNTS.values():
        start(acc)      # chaque watcher se connecte de son côté : le serveur répond tout de suite
        if not PUBLIC:      # en ligne, on n'écrit pas les liens d'accès dans les logs
            log.info("compte %s : http://127.0.0.1:%s%s", acc.name, PORT, acc.link or f"/u/{acc.name}/")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=PORT, log_level="warning")
