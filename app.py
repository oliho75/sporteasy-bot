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

import json
import logging
import os
import random
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
from pydantic import BaseModel

from sporteasy import Event, SporteasyClient

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")

TEAM_ID = int(os.environ.get("SPORTEASY_TEAM_ID", "2050454"))
TEAM_HOST = os.environ.get("SPORTEASY_TEAM_HOST", "sitebad.sporteasy.net")
PORT = int(os.environ.get("PORT", "8765"))
TICK = int(os.environ.get("TICK_SECONDS", "30"))
HOT_WINDOW = int(os.environ.get("HOT_WINDOW_SECONDS", "120"))
HOT_INTERVAL = float(os.environ.get("HOT_INTERVAL_SECONDS", "1.5"))
HOT_MAX_MIN = int(os.environ.get("HOT_MAX_MINUTES", "20"))
MAX_PER_WEEK = int(os.environ.get("MAX_PER_WEEK", "5"))
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
DATA = Path(os.environ.get("DATA_DIR", str(BASE)))
DATA.mkdir(parents=True, exist_ok=True)
LOGFILE = DATA / "bot.log"
PUBLIC = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PUBLIC"))

# Délai aléatoire après l'ouverture, minutes [min, max], par jour du créneau. Chaque compte a le sien.
DEFAULT_DELAYS: dict[str, list[float]] = {"mon": [0.17, 0.5], "thu": [4, 6], "default": [12, 18]}
_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(LOGFILE), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bot")


def notify(title: str, msg: str) -> None:
    log.info("NOTIF %s — %s", title, msg)
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
    def __init__(self, name: str, email: str, password: str, token: str, invite: str = ""):
        self.name = name
        self.token = token
        self.invite = invite.strip()
        self.dir = DATA / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wishlist = self.dir / "wishlist.json"
        self.lock = threading.RLock()
        self.recent: deque[str] = deque(maxlen=120)
        self.log = logging.getLogger(f"bot.{name}")
        self.log.addHandler(_Ring(self.recent))
        self.client = SporteasyClient(TEAM_ID, TEAM_HOST, email, password, session_file=self.dir / "session.json")
        self.cache: dict[str, Any] = {"at": 0.0, "events": []}

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
            notify(f"Sporteasy [{self.name}] — complet", ev.name); return
        try:
            ok, msg = self.client.book(ev)
        except PermissionError as e:
            self.log.warning("session morte pendant la résa : %s", e)
            self.client.login()
            ok, msg = self.client.book(ev)
        except Exception as e:  # noqa: BLE001
            self.record(wl, eid, "error", str(e)[:200]); self.save(wl)
            notify(f"Sporteasy [{self.name}] — erreur", f"{ev.name}: {e}"); return
        self.record(wl, eid, "booked" if ok else ("waitlist" if "attente" in msg else "error"), msg)
        self.save(wl)
        notify(f"Sporteasy [{self.name}]", msg)
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
                    notify(f"Sporteasy [{self.name}] — libéré", fe.name)
                except Exception as e:  # noqa: BLE001
                    self.log.warning("release fallback %s : %s", eid, e)

    def schedule_or_book(self, ev: Event, wl: dict[str, Any]) -> None:
        eid = str(ev.id); w = wl["wishes"][eid]
        now = datetime.now().astimezone()
        if not w.get("book_at"):
            d = self.pick_delay(wl, ev.start_at)
            w["book_at"] = (now + timedelta(seconds=d)).isoformat(timespec="seconds")
            self.record(wl, eid, "delaying", f"ouvert, je réserve dans {int(d // 60)}m{int(d % 60):02d}")
            self.save(wl); return
        if now >= datetime.fromisoformat(w["book_at"]):
            self.try_book(ev, wl)

    def hot_wait(self, ev: Event, wl: dict[str, Any]) -> None:
        deadline = (ev.registration_open_at or datetime.now().astimezone()) + timedelta(minutes=HOT_MAX_MIN)
        self.log.info("⏱  mode rapide sur %s (ouverture %s)", ev.name, ev.registration_open_at)
        while datetime.now().astimezone() < deadline:
            try:
                fresh = self.client.event(ev.id)
            except PermissionError:
                self.client.login(); continue
            except Exception as e:  # noqa: BLE001
                self.log.warning("poll rapide : %s", e); time.sleep(HOT_INTERVAL * 2); continue
            if fresh.is_cancelled:
                self.record(wl, str(ev.id), "cancelled", "créneau annulé par le club"); self.save(wl); return
            if fresh.step == "registration_opened" and fresh.update_url:
                self.schedule_or_book(fresh, wl); return
            time.sleep(HOT_INTERVAL)
        self.record(wl, str(ev.id), "pending", f"pas ouvert après {HOT_MAX_MIN} min, on repassera"); self.save(wl)

    # ---------- watcher ----------
    def watcher(self) -> None:
        self.log.info("watcher démarré")
        while True:
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
                        continue
                    if ev.is_cancelled:
                        self.record(wl, eid, "cancelled", "créneau annulé par le club"); self.save(wl); continue
                    if ev.my_group == "participant" and ev.my_status == "present":
                        self.record(wl, eid, "booked", "déjà inscrit"); self.save(wl); continue
                    if ev.step == "registration_opened" and ev.update_url:
                        self.schedule_or_book(ev, wl); continue
                    if ev.registration_open_at and 0 <= (ev.registration_open_at - now).total_seconds() <= HOT_WINDOW:
                        wait = (ev.registration_open_at - now).total_seconds() - 5
                        if wait > 0:
                            time.sleep(wait)
                        self.hot_wait(ev, wl)
                    elif w.get("state") != "waiting":
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


# ================================================================= accounts from env
def load_accounts() -> dict[str, Account]:
    accs: dict[str, Account] = {}
    i = 1
    while os.environ.get(f"USER{i}_EMAIL"):
        name = (os.environ.get(f"USER{i}_NAME") or f"user{i}").strip().lower()
        accs[name] = Account(name, os.environ[f"USER{i}_EMAIL"], os.environ.get(f"USER{i}_PASSWORD", ""),
                             os.environ.get(f"USER{i}_TOKEN", "").strip(), os.environ.get(f"USER{i}_INVITE", ""))
        i += 1
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


@app.get("/")
def root():
    if len(ACCOUNTS) == 1 and not PUBLIC:
        return RedirectResponse(f"/u/{next(iter(ACCOUNTS))}/")
    return JSONResponse({"detail": "utilise /u/<nom>/?token=…"}, status_code=404)


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
            "error": err, "now": datetime.now().astimezone().isoformat(timespec="seconds")}


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
