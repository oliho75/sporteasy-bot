"""
Sporteasy bot — serveur + un watcher par compte.

Comptes : variables d'environnement, un bloc par utilisateur (N = 1, 2, 3…) :
  USER1_NAME=ao          USER1_EMAIL=…  USER1_PASSWORD=…  USER1_TOKEN=…
  USER2_NAME=pote1       USER2_EMAIL=…  USER2_PASSWORD=…  USER2_TOKEN=…
Chaque compte a son URL  /u/<name>/?token=<token>, sa wishlist, ses réglages, ses délais.
(Compat : SPORTEASY_EMAIL / SPORTEASY_PASSWORD / ACCESS_TOKEN = compte "ao" si USER1_* absent.)

Fichiers (DATA_DIR, = /data sur Railway) :
  <name>/wishlist.json   options + réglages de ce compte
  <name>/session.json    cookies Sporteasy
  bot.log                journal commun
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
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
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sporteasy import Event, LoginError, SporteasyClient

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")
os.environ.setdefault("TZ", "Europe/Paris")
if hasattr(time, "tzset"):
    time.tzset()

TEAM_ID = int(os.environ.get("SPORTEASY_TEAM_ID", "2050454"))
TEAM_HOST = os.environ.get("SPORTEASY_TEAM_HOST", "sitebad.sporteasy.net")
PORT = int(os.environ.get("PORT", "8765"))
TICK = int(os.environ.get("TICK_SECONDS", "30"))
HOT_WINDOW = int(os.environ.get("HOT_WINDOW_SECONDS", "120"))
HOT_INTERVAL = float(os.environ.get("HOT_INTERVAL_SECONDS", "1.0"))
HOT_MAX_MIN = int(os.environ.get("HOT_MAX_MINUTES", "20"))
MAX_PER_WEEK = int(os.environ.get("MAX_PER_WEEK", "5"))
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
RESEND_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM = os.environ.get("RESEND_FROM", "EZ bad <onboarding@resend.dev>").strip()
DATA = Path(os.environ.get("DATA_DIR", str(BASE)))
DATA.mkdir(parents=True, exist_ok=True)
LOGFILE = DATA / "bot.log"
PUBLIC = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PUBLIC"))
ADMINS = {a.strip().lower() for a in os.environ.get("ADMIN_USERS", "ao").split(",") if a.strip()}
INVITE_DAYS = int(os.environ.get("INVITE_DAYS", "7"))

# Délai aléatoire après l'ouverture, minutes [min, max], par jour du créneau. Chaque compte a le sien.
DEFAULT_DELAYS: dict[str, list[float]] = {"mon": [0, 0.0167], "thu": [0.08, 0.83], "default": [1, 5]}
_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(LOGFILE), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bot")


def notify(title: str, msg: str, ntfy: str = "", tags: str = "", email: str = "") -> None:
    log.info("NOTIF %s — %s", title, msg)
    if email and RESEND_KEY:
        try:
            r = requests.post("https://api.resend.com/emails", timeout=10,
                              headers={"Authorization": f"Bearer {RESEND_KEY}"},
                              json={"from": RESEND_FROM, "to": [email], "subject": f"{title} — {msg[:60]}",
                                    "text": f"{msg}\n\n— EZ bad 🏸"})
            if r.status_code >= 400:
                log.warning("resend %s : %s", r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001
            log.warning("resend : %s", e)
    if ntfy:
        try:
            requests.post(f"https://ntfy.sh/{ntfy}", data=msg.encode("utf-8"),
                          headers={"Title": title, "Tags": tags or "badminton", "Priority": "high"}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log.warning("ntfy : %s", e)
    if sys.platform == "darwin":
        try:
            subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}" sound name "Glass"'],
                           timeout=5, check=False)
        except Exception:  # noqa: BLE001
            pass
    if WEBHOOK:
        try:
            requests.post(WEBHOOK, json={"text": f"*{title}*\n{msg}"}, timeout=10)
        except Exception:  # noqa: BLE001
            pass


def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
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

    @property
    def ntfy(self) -> str:
        """Topic ntfy : celui réglé par la personne dans l'app, sinon celui des variables."""
        try:
            return (self.load()["settings"].get("ntfy") or self._ntfy or "").strip()
        except Exception:  # noqa: BLE001
            return self._ntfy

    # ---------- wishlist ----------
    def load(self) -> dict[str, Any]:
        if self.wishlist.exists():
            try:
                d = json.loads(self.wishlist.read_text())
                d.setdefault("wishes", {}); d.setdefault("settings", {}); d.setdefault("history", [])
                return d
            except Exception as e:  # noqa: BLE001
                self.log.error("wishlist corrompue : %s", e)
        return {"wishes": {}, "settings": {"paused": False, "max_per_week": MAX_PER_WEEK, "delays": dict(DEFAULT_DELAYS)}, "history": []}

    def save(self, d: dict[str, Any]) -> None:
        tmp = self.wishlist.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
        tmp.replace(self.wishlist)

    def record(self, wl: dict[str, Any], eid: str, state: str, msg: str) -> None:
        w = wl["wishes"].get(eid)
        if w is not None:
            w["state"] = state; w["last"] = msg
            w["updated_at"] = datetime.now().isoformat(timespec="seconds")
        wl["history"].insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "event_id": eid,
                                 "name": w["name"] if w else eid, "state": state, "msg": msg})
        del wl["history"][100:]
        self.log.info("%s → %s (%s)", w["name"] if w else eid, state, msg)

    # ---------- events ----------
    def events(self, force: bool = False) -> list[Event]:
        if not force and time.time() - self.cache["at"] < 60:
            return self.cache["events"]
        try:
            evs = self.client.upcoming(2)
        except PermissionError:
            self.client.login()
            evs = self.client.upcoming(2)
        self.cache.update(at=time.time(), events=evs)
        return evs

    # ---------- booking ----------
    def pick_delay(self, wl: dict[str, Any], start_at: datetime) -> float:
        delays = {**DEFAULT_DELAYS, **(wl["settings"].get("delays") or {})}
        rng = delays.get(_WD[start_at.weekday()]) or delays.get("default") or [0, 0]
        lo, hi = sorted(float(x) for x in rng[:2])
        return random.uniform(lo, hi) * 60

    def _booked_this_week(self, wl: dict[str, Any], dt: datetime) -> int:
        wk = _week_key(dt)
        return sum(1 for w in wl["wishes"].values()
                   if w.get("state") == "booked" and _week_key(datetime.fromisoformat(w["start_at"])) == wk)

    def _p1_booked_same_day(self, wl: dict[str, Any], w: dict[str, Any]) -> bool:
        d = w["start_at"][:10]
        return any(x.get("state") == "booked" and x.get("priority", 1) == 1 and x["start_at"][:10] == d
                   for x in wl["wishes"].values())

    def try_book(self, ev: Event, wl: dict[str, Any]) -> None:
        eid = str(ev.id); w = wl["wishes"][eid]
        if self._booked_this_week(wl, ev.start_at) >= int(wl["settings"].get("max_per_week", MAX_PER_WEEK)):
            self.record(wl, eid, "skipped", "quota hebdo atteint"); self.save(wl); return
        if w.get("priority", 1) > 1 and self._p1_booked_same_day(wl, w):
            self.record(wl, eid, "skipped", "un créneau prioritaire du même jour est déjà réservé"); self.save(wl); return
        if ev.full and not w.get("waitlist_ok"):
            self.record(wl, eid, "full", "complet (liste d'attente refusée)"); self.save(wl)
            notify("EZ bad — complet", f"{ev.name} ({ev.start_at:%a %d/%m %H:%M})", self.ntfy, "no_entry", self.email_notif); return
        try:
            ok, msg = self.client.book(ev)
        except PermissionError as e:
            self.log.warning("session morte pendant la résa : %s", e)
            self.client.login()
            ok, msg = self.client.book(ev)
        except Exception as e:  # noqa: BLE001
            self.record(wl, eid, "error", str(e)[:200]); self.save(wl)
            notify("EZ bad — erreur", f"{ev.name}: {e}", self.ntfy, "x", self.email_notif); return
        self.record(wl, eid, "booked" if ok else ("waitlist" if "attente" in msg else "error"), msg)
        self.save(wl)
        notify("EZ bad", msg, self.ntfy, "white_check_mark,badminton" if ok else "hourglass", self.email_notif)
        if ok and w.get("priority", 1) == 1:
            self.release_fallbacks(ev, wl)

    def release_fallbacks(self, ev: Event, wl: dict[str, Any]) -> None:
        d = ev.start_at.isoformat()[:10]
        for eid, x in wl["wishes"].items():
            if x.get("priority", 1) > 1 and x.get("state") == "booked" and x["start_at"][:10] == d and eid != str(ev.id):
                try:
                    fe = self.client.event(int(eid))
                    self.client.set_attendance(fe, "absent")
                    self.record(wl, eid, "released", "libéré : un créneau prioritaire du même jour est réservé")
                    self.save(wl)
                    notify("EZ bad — libéré", f"{fe.name} ({fe.start_at:%a %d/%m %H:%M})", self.ntfy, "leftwards_arrow_with_hook", self.email_notif)
                except Exception as e:  # noqa: BLE001
                    self.log.warning("release fallback %s : %s", eid, e)

    def schedule_or_book(self, ev: Event, _wl: dict[str, Any] | None = None) -> None:
        """Créneau ouvert : tire un délai aléatoire, puis réserve une fois le délai écoulé.
        Délais courts (≤ 90 s) : attente à la seconde près, hors verrou. Délais longs : repris au tick."""
        eid = str(ev.id)
        with self.lock:
            if eid in self.inflight:
                return
            wl = self.load(); w = wl["wishes"].get(eid)
            if not w or w.get("state") in ("booked", "released", "cancelled"):
                return
            now = datetime.now().astimezone()
            if not w.get("book_at"):
                d = self.pick_delay(wl, ev.start_at)
                w["book_at"] = (now + timedelta(seconds=d)).isoformat(timespec="seconds")
                self.record(wl, eid, "delaying", f"ouvert, je réserve dans {int(d // 60)}m{int(d % 60):02d}" if d >= 1 else "ouvert, je réserve tout de suite")
                self.save(wl)
            else:
                d = (datetime.fromisoformat(w["book_at"]) - now).total_seconds()
            if d > 90:
                return                      # le tick de 10 s reprendra
            self.inflight.add(eid)
        try:
            if d > 0:
                time.sleep(d)
            with self.lock:
                wl = self.load()
                if wl["wishes"].get(eid, {}).get("state") not in ("booked", "released", "cancelled"):
                    self.try_book(ev, wl)
        finally:
            with self.lock:
                self.inflight.discard(eid)

    def hot_wait(self, ev: Event, target: datetime, max_min: float) -> None:
        """Poll rapide autour d'une heure d'ouverture probable (fil dédié)."""
        deadline = target + timedelta(minutes=max_min)
        self.log.info("⏱  mode rapide sur %s (ouverture possible %s)", ev.name, target.strftime("%H:%M"))
        while datetime.now().astimezone() < deadline:
            try:
                fresh = self.client.event(ev.id)
            except PermissionError:
                try:
                    self.client.login()
                except Exception as e:  # noqa: BLE001
                    self.log.warning("relogin : %s", e); time.sleep(5)
                continue
            except Exception as e:  # noqa: BLE001
                self.log.warning("poll rapide : %s", e); time.sleep(HOT_INTERVAL * 2); continue
            if fresh.is_cancelled:
                return
            if fresh.step == "registration_opened" and fresh.update_url:
                self.log.info("🟢 %s ouvert à %s", ev.name, datetime.now().astimezone().strftime("%H:%M:%S"))
                self.schedule_or_book(fresh)
                return
            time.sleep(HOT_INTERVAL)

    def _hot_at(self, ev: Event, target: datetime, max_min: float) -> None:
        wait = (target - datetime.now().astimezone()).total_seconds() - 5
        if wait > 0:
            time.sleep(wait)
        self.hot_wait(ev, target, max_min)

    # ---------- watcher ----------
    def watcher(self) -> None:
        self.log.info("watcher démarré")
        while self.active:
            try:
                with self.lock:
                    wl = self.load()
                active = {k: v for k, v in wl["wishes"].items()
                          if v.get("state") in (None, "pending", "waiting", "delaying", "skipped", "error", "waitlist", "full")}
                if wl["settings"].get("paused") or not active:
                    time.sleep(TICK); continue
                events = {str(e.id): e for e in self.events(force=True)}
                now = datetime.now().astimezone()
                for eid, w in active.items():
                    ev = events.get(eid)
                    if ev is None:
                        # Créneau disparu du calendrier alors qu'il est à venir → supprimé par le club
                        try:
                            start = datetime.fromisoformat(w["start_at"])
                        except Exception:  # noqa: BLE001
                            continue
                        if start > now and events and w.get("state") != "cancelled":
                            self.record(wl, eid, "cancelled", "créneau supprimé par le club"); self.save(wl)
                            notify("EZ bad — créneau supprimé", f"{w['name']} ({start:%a %d/%m %H:%M}) n'existe plus sur Sporteasy",
                                   self.ntfy, "wastebasket", self.email_notif)
                        continue
                    if ev.is_cancelled:
                        self.record(wl, eid, "cancelled", "créneau annulé par le club"); self.save(wl); continue
                    if ev.my_group == "participant" and ev.my_status == "present":
                        self.record(wl, eid, "booked", "déjà inscrit"); self.save(wl); continue
                    if ev.step == "registration_opened" and ev.update_url:
                        threading.Thread(target=self.schedule_or_book, args=(ev,), daemon=True).start(); continue
                    if ev.registration_open_at:
                        # Sporteasy ouvre parfois 1 h avant l'heure affichée (changement d'heure) :
                        # on surveille en rapide autour des deux heures possibles.
                        for target, max_min in ((ev.registration_open_at - timedelta(hours=1), 8),
                                                (ev.registration_open_at, HOT_MAX_MIN)):
                            key = (eid, target.isoformat())
                            if key not in self.hot_started and -60 <= (target - now).total_seconds() <= HOT_WINDOW:
                                self.hot_started.add(key)
                                threading.Thread(target=self._hot_at, args=(ev, target, max_min), daemon=True).start()
                    if w.get("state") not in ("waiting", "delaying"):
                        self.record(wl, eid, "waiting", f"ouverture prévue {ev.registration_open_at:%a %d/%m %H:%M}" if ev.registration_open_at else "en attente")
                        self.save(wl)
            except Exception as e:  # noqa: BLE001
                self.log.exception("watcher : %s", e)
            try:
                delaying = any(v.get("state") == "delaying" for v in self.load()["wishes"].values())
            except Exception:  # noqa: BLE001
                delaying = False
            time.sleep(10 if delaying else TICK)


class _Ring(logging.Handler):
    def __init__(self, buf: deque[str]):
        super().__init__(); self.buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        self.buf.append(f"{datetime.now():%d/%m %H:%M:%S} {record.getMessage()}")


# ================================================================= store chiffré (auto-inscription)
DB_LOCK = threading.RLock()
ACCOUNTS_FILE = DATA / "accounts.json"
INVITES_FILE = DATA / "invites.json"
RESERVED = {"admin", "api", "static", "join", "i", "u", "root", "www"}


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
                raise SystemExit(f"USER…_TOKEN obligatoire pour {a.name} quand le bot est exposé sur Internet")
    return accs


ACCOUNTS = load_accounts()

# ================================================================= API
app = FastAPI(title="Sporteasy bot")


def _account(request: Request, name: str) -> Account:
    acc = ACCOUNTS.get(name.lower())
    if acc is None:
        raise HTTPException(404, "compte inconnu")
    if acc.token:
        tok = request.query_params.get("token") or request.cookies.get(f"token_{acc.name}")
        if tok != acc.token:
            raise HTTPException(401, "token invalide")
    return acc


@app.middleware("http")
async def _cookie(request: Request, call_next):
    resp = await call_next(request)
    tok = request.query_params.get("token")
    parts = request.url.path.split("/")
    if tok and len(parts) > 2 and parts[1] == "u":
        acc = ACCOUNTS.get(parts[2].lower())
        if acc and tok == acc.token:
            resp.set_cookie(f"token_{acc.name}", tok, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


class Wish(BaseModel):
    event_id: int
    priority: int = 1
    waitlist_ok: bool = True


class Settings(BaseModel):
    paused: bool | None = None
    max_per_week: int | None = None
    delays: dict[str, list[float]] | None = None


@app.get("/i/{code}")
def invite(code: str):
    """Lien court d'invitation : /i/<code> → pose le cookie du compte et redirige vers sa page.
    Variables : USERn_INVITE=<code> (6-12 caractères au choix) ; ou on dérive du token."""
    for acc in ACCOUNTS.values():
        if acc.token and code in (acc.invite, acc.token[:8]):
            resp = RedirectResponse(f"/u/{acc.name}/", status_code=302)
            resp.set_cookie(f"token_{acc.name}", acc.token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
            return resp
    raise HTTPException(404, "lien invalide")


def _known_account(request: Request) -> "Account | None":
    """Compte dont le cookie est présent et valide dans ce navigateur."""
    for acc in ACCOUNTS.values():
        if acc.token and request.cookies.get(f"token_{acc.name}") == acc.token:
            return acc
    return None


@app.get("/")
def root(request: Request):
    acc = _known_account(request)
    if acc:
        return RedirectResponse(f"/u/{acc.name}/", status_code=302)
    if len(ACCOUNTS) == 1 and not PUBLIC:
        return RedirectResponse(f"/u/{next(iter(ACCOUNTS))}/")
    return JSONResponse({"detail": "Ouvre ton lien d'invitation EZ bad (…/i/<ton-code>)."}, status_code=404)


@app.get("/static/")
def static_root(request: Request):
    return root(request)


@app.get("/u/{name}/manifest.json")
def manifest(name: str):
    acc = ACCOUNTS.get(name.lower())
    if acc is None:
        raise HTTPException(404, "compte inconnu")
    return JSONResponse({
        "name": "EZ bad", "short_name": "EZ bad", "id": f"/u/{acc.name}/",
        "start_url": f"/u/{acc.name}/", "scope": f"/u/{acc.name}/", "display": "standalone",
        "background_color": "#0f1115", "theme_color": "#0f1115",
        "icons": [{"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/u/{name}/")
def index(request: Request, name: str):
    _account(request, name)
    return FileResponse(BASE / "static" / "index.html")


@app.get("/u/{name}/api/state")
def state(request: Request, name: str):
    acc = _account(request, name)
    try:
        events = acc.events(); err = None
    except Exception as e:  # noqa: BLE001
        events, err = [], str(e)
    with acc.lock:
        wl = acc.load()
    return {"user": acc.name, "events": [e.to_json() for e in events], "wishes": wl["wishes"],
            "settings": wl["settings"], "history": wl["history"][:30], "log": list(acc.recent)[-40:],
            "error": err, "ntfy": bool(acc.ntfy or RESEND_KEY), "now": datetime.now().astimezone().isoformat(timespec="seconds"),
            "account": {"kind": acc.kind, "admin": acc.name in ADMINS, "club": acc.team_host, "email": acc.email,
                        "ntfy_topic": acc.ntfy, "link": f"/i/{acc.invite or acc.token[:8]}" if acc.token else None}}


@app.post("/u/{name}/api/wish")
def add_wish(request: Request, name: str, w: Wish):
    acc = _account(request, name)
    ev = next((e for e in acc.events() if e.id == w.event_id), None)
    if ev is None:
        raise HTTPException(404, "événement inconnu")
    with acc.lock:
        wl = acc.load()
        prev = wl["wishes"].get(str(ev.id), {})
        wl["wishes"][str(ev.id)] = {
            "name": ev.name, "start_at": ev.start_at.isoformat(),
            "registration_open_at": ev.registration_open_at.isoformat() if ev.registration_open_at else None,
            "priority": w.priority, "waitlist_ok": w.waitlist_ok,
            "state": prev.get("state") if prev.get("state") == "booked" else "pending",
            "added_at": prev.get("added_at") or datetime.now().isoformat(timespec="seconds"),
        }
        acc.save(wl)
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
    with acc.lock:
        wl = acc.load()
        for k, v in s.model_dump(exclude_none=True).items():
            wl["settings"][k] = v
        acc.save(wl)
    return wl["settings"]


@app.post("/u/{name}/api/test-notif")
def test_notif(request: Request, name: str):
    acc = _account(request, name)
    if not acc.ntfy and not RESEND_KEY:
        raise HTTPException(400, "aucun canal configuré (USERn_NTFY ou RESEND_API_KEY)")
    notify("EZ bad", "Test de notification : ça marche 🏸", acc.ntfy, "white_check_mark", acc.email_notif)
    return {"ok": True, "ntfy": acc.ntfy or None, "email": acc.email_notif if RESEND_KEY else None}


@app.post("/u/{name}/api/login")
def force_login(request: Request, name: str):
    acc = _account(request, name)
    try:
        acc.client.login(); acc.events(force=True)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/u/{name}/api/book-now/{event_id}")
def book_now(request: Request, name: str, event_id: int):
    acc = _account(request, name)
    ev = next((e for e in acc.events(force=True) if e.id == event_id), None)
    if ev is None or ev.step != "registration_opened":
        raise HTTPException(400, "créneau introuvable ou pas encore ouvert")
    with acc.lock:
        wl = acc.load()
        wl["wishes"].setdefault(str(ev.id), {"name": ev.name, "start_at": ev.start_at.isoformat(), "priority": 1,
                                             "waitlist_ok": True, "state": "pending",
                                             "added_at": datetime.now().isoformat(timespec="seconds")})
        acc.save(wl); acc.try_book(ev, wl); wl = acc.load()
    return wl["wishes"][str(ev.id)]


@app.post("/u/{name}/api/release/{event_id}")
def release(request: Request, name: str, event_id: int):
    acc = _account(request, name)
    ev = acc.client.event(event_id)
    acc.client.set_attendance(ev, "absent")
    with acc.lock:
        wl = acc.load(); wl["wishes"].pop(str(event_id), None)
        wl["history"].insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "event_id": str(event_id),
                                 "name": ev.name, "state": "released", "msg": "désinscrit manuellement"})
        acc.save(wl)
    return {"ok": True}



# ================================================================= invitations & auto-inscription
def _set_cookie(resp, acc: Account):
    resp.set_cookie(f"token_{acc.name}", acc.token, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
    return resp


def _valid_invite(code: str) -> dict[str, Any] | None:
    inv = _read(INVITES_FILE).get(code)
    if not inv or inv.get("used_by") or inv.get("revoked"):
        return None
    if datetime.fromisoformat(inv["expires_at"]) < datetime.now().astimezone():
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
        inv[code] = {"created_by": acc.name, "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "expires_at": (datetime.now().astimezone() + timedelta(days=INVITE_DAYS)).isoformat(timespec="seconds"),
                     "used_by": None, "fails": 0}
        _write(INVITES_FILE, inv)
    return {"code": code, "path": f"/join/{code}", "expires_days": INVITE_DAYS}


@app.get("/u/{name}/api/invites")
def list_invites(request: Request, name: str):
    _admin(request, name)
    now = datetime.now().astimezone()
    inv = _read(INVITES_FILE)
    pending = [{"code": c, "path": f"/join/{c}", "expires_at": v["expires_at"]} for c, v in inv.items()
               if not v.get("used_by") and not v.get("revoked") and datetime.fromisoformat(v["expires_at"]) > now]
    members = [{"name": a.name, "club": a.team_host, "kind": a.kind} for a in ACCOUNTS.values()]
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
def join_page(code: str):
    if not _valid_invite(code):
        return FileResponse(BASE / "static" / "join-invalid.html", status_code=404)
    return FileResponse(BASE / "static" / "join.html")


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
def join_submit(code: str, j: Join):
    with DB_LOCK:
        inv = _valid_invite(code)
    if not inv:
        raise HTTPException(410, "invitation expirée ou déjà utilisée — demande-en une nouvelle")
    name = re.sub(r"[^a-z0-9-]", "", j.name.strip().lower().replace(" ", "-"))[:20]
    if len(name) < 2:
        raise HTTPException(400, "prénom trop court (lettres et chiffres, sans accents)")
    if name in ACCOUNTS or name in RESERVED or (DATA / name).exists():
        raise HTTPException(409, f"« {name} » est déjà pris, choisis un autre nom")
    email = j.email.strip()
    if "@" not in email or not j.password:
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
        with DB_LOCK:
            invs = _read(INVITES_FILE)
            if code in invs:
                invs[code]["fails"] = invs[code].get("fails", 0) + 1
                if invs[code]["fails"] >= 5:
                    invs[code]["revoked"] = True
                _write(INVITES_FILE, invs)
        raise HTTPException(401, str(e))
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        log.warning("inscription %s : %s", name, e)
        raise HTTPException(502, "Sporteasy ne répond pas, réessaie dans une minute")

    with DB_LOCK:
        if not _valid_invite(code) or name in ACCOUNTS:
            shutil.rmtree(tmp, ignore_errors=True)
            raise HTTPException(409, "invitation déjà utilisée entre-temps")
        tmp.rename(DATA / name)
        rec = {"email": email, "pw": encrypt(j.password), "token": secrets.token_hex(24),
               "invite": f"{name}-{secrets.token_hex(3)}", "ntfy": ntfy, "team_id": client.team_id, "team_host": host,
               "invited_by": inv["created_by"], "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
        accs = _read(ACCOUNTS_FILE); accs[name] = rec; _write(ACCOUNTS_FILE, accs)
        invs = _read(INVITES_FILE); invs[code]["used_by"] = name; _write(INVITES_FILE, invs)
        acc = _db_account(name, rec)
        ACCOUNTS[name] = acc
    threading.Thread(target=acc.watcher, daemon=True, name=f"watcher-{name}").start()
    log.info("nouveau compte %s (club %s, invité par %s)", name, host, inv["created_by"])
    for a in ACCOUNTS.values():
        if a.name == inv["created_by"]:
            notify("EZ bad — nouveau membre", f"{name} a rejoint EZ bad ({host})", a.ntfy, "tada", "")
    return _set_cookie(JSONResponse({"ok": True, "redirect": f"/u/{name}/"}), acc)


class Pwd(BaseModel):
    password: str


@app.post("/u/{name}/api/account/password")
def change_password(request: Request, name: str, body: Pwd):
    acc = _account(request, name)
    if acc.kind != "db":
        raise HTTPException(400, "ce compte est géré dans les variables Railway, demande à l'admin")
    test = SporteasyClient(acc.team_id, acc.team_host, acc.email, body.password, session_file=acc.dir / "session.json")
    try:
        test.login()
    except LoginError as e:
        raise HTTPException(401, str(e))
    except Exception:  # noqa: BLE001
        raise HTTPException(502, "Sporteasy ne répond pas, réessaie dans une minute")
    with DB_LOCK:
        accs = _read(ACCOUNTS_FILE); accs[acc.name]["pw"] = encrypt(body.password); _write(ACCOUNTS_FILE, accs)
    acc.client.password = body.password
    acc.client._load_session()
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
    for acc in ACCOUNTS.values():
        try:
            acc.client.ensure_login()
        except Exception as e:  # noqa: BLE001
            acc.log.error("login impossible au démarrage : %s (relance via /api/login)", e)
        threading.Thread(target=acc.watcher, daemon=True, name=f"watcher-{acc.name}").start()
        log.info("compte %s : /u/%s/%s%s", acc.name, acc.name, f"?token={acc.token}" if acc.token else "",
                 f"  · lien court : /i/{acc.invite or acc.token[:8]}" if acc.token else "")
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=PORT, log_level="warning")
