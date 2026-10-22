"""
Sporteasy bot — serveur local + watcher.

  python app.py            → UI sur http://localhost:8765 + watcher qui réserve à l'ouverture

Fichiers :
  .env            identifiants + réglages
  wishlist.json   tes options (écrit par l'UI)
  session.json    cookies Sporteasy (généré au login)
  bot.log         journal
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
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from sporteasy import Event, SporteasyClient

BASE = Path(__file__).resolve().parent
os.chdir(BASE)
load_dotenv(BASE / ".env")

TEAM_ID = int(os.environ.get("SPORTEASY_TEAM_ID", "2050454"))
TEAM_HOST = os.environ.get("SPORTEASY_TEAM_HOST", "sitebad.sporteasy.net")
EMAIL = os.environ.get("SPORTEASY_EMAIL", "")
PASSWORD = os.environ.get("SPORTEASY_PASSWORD", "")
PORT = int(os.environ.get("PORT", "8765"))
TICK = int(os.environ.get("TICK_SECONDS", "30"))          # poll normal
HOT_WINDOW = int(os.environ.get("HOT_WINDOW_SECONDS", "120"))  # on passe en mode rapide X s avant l'ouverture
HOT_INTERVAL = float(os.environ.get("HOT_INTERVAL_SECONDS", "1.5"))
HOT_MAX_MIN = int(os.environ.get("HOT_MAX_MINUTES", "20"))   # on abandonne le mode rapide après X min
MAX_PER_WEEK = int(os.environ.get("MAX_PER_WEEK", "5"))
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "")
# Accès distant (iPhone via Tailscale Serve) : si ACCESS_TOKEN est défini, chaque requête doit
# porter ?token=… (posé ensuite en cookie). Le serveur reste sur localhost ; Tailscale fait le pont.
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN", "").strip()

# Données persistantes (wishlist, session, log). Sur Railway : volume monté sur /data.
DATA = Path(os.environ.get("DATA_DIR", str(BASE)))
DATA.mkdir(parents=True, exist_ok=True)
WISHLIST = DATA / "wishlist.json"
LOGFILE = DATA / "bot.log"
PUBLIC = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("PUBLIC"))
if PUBLIC and not ACCESS_TOKEN:
    raise SystemExit("ACCESS_TOKEN obligatoire quand le bot est exposé sur Internet")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                    handlers=[logging.FileHandler(LOGFILE), logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bot")
RECENT: deque[str] = deque(maxlen=200)


class _RingHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        RECENT.append(f"{datetime.now():%d/%m %H:%M:%S} {record.getMessage()}")


logging.getLogger().addHandler(_RingHandler())

# ---------------------------------------------------------------- wishlist
LOCK = threading.RLock()


def load_wl() -> dict[str, Any]:
    if WISHLIST.exists():
        try:
            d = json.loads(WISHLIST.read_text())
            d.setdefault("wishes", {})
            d.setdefault("settings", {})
            d.setdefault("history", [])
            return d
        except Exception as e:  # noqa: BLE001
            log.error("wishlist.json corrompu : %s", e)
    return {"wishes": {}, "settings": {"paused": False, "max_per_week": MAX_PER_WEEK, "delays": dict(DEFAULT_DELAYS)}, "history": []}


# Délai aléatoire après l'ouverture avant de réserver, en minutes [min, max], par jour du créneau
# (mon..sun). Évite d'être systématiquement le premier inscrit à la seconde près.
DEFAULT_DELAYS: dict[str, list[float]] = {"mon": [0.5, 1.5], "thu": [4, 6], "default": [12, 18]}
_WD = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def pick_delay(wl: dict[str, Any], start_at: datetime) -> float:
    """Secondes à attendre après l'ouverture pour ce créneau."""
    delays = {**DEFAULT_DELAYS, **(wl["settings"].get("delays") or {})}
    rng = delays.get(_WD[start_at.weekday()]) or delays.get("default") or [0, 0]
    lo, hi = sorted(float(x) for x in rng[:2])
    return random.uniform(lo, hi) * 60


def save_wl(d: dict[str, Any]) -> None:
    tmp = WISHLIST.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False))
    tmp.replace(WISHLIST)


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


# ---------------------------------------------------------------- client
client = SporteasyClient(TEAM_ID, TEAM_HOST, EMAIL, PASSWORD)
EVENTS_CACHE: dict[str, Any] = {"at": 0.0, "events": []}


def get_events(force: bool = False) -> list[Event]:
    if not force and time.time() - EVENTS_CACHE["at"] < 60:
        return EVENTS_CACHE["events"]
    try:
        evs = client.upcoming(2)
    except PermissionError:
        client.login()
        evs = client.upcoming(2)
    EVENTS_CACHE.update(at=time.time(), events=evs)
    return evs


# ---------------------------------------------------------------- watcher
def _week_key(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _booked_this_week(wl: dict[str, Any], dt: datetime) -> int:
    wk = _week_key(dt)
    return sum(1 for w in wl["wishes"].values()
               if w.get("state") == "booked" and _week_key(datetime.fromisoformat(w["start_at"])) == wk)


def _p1_booked_same_day(wl: dict[str, Any], w: dict[str, Any]) -> bool:
    d = w["start_at"][:10]
    return any(x.get("state") == "booked" and x.get("priority", 1) == 1 and x["start_at"][:10] == d
               for x in wl["wishes"].values())


def _record(wl: dict[str, Any], eid: str, state: str, msg: str) -> None:
    w = wl["wishes"].get(eid)
    if w is not None:
        w["state"] = state
        w["last"] = msg
        w["updated_at"] = datetime.now().isoformat(timespec="seconds")
    wl["history"].insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "event_id": eid,
                             "name": w["name"] if w else eid, "state": state, "msg": msg})
    del wl["history"][100:]


def try_book(ev: Event, wl: dict[str, Any]) -> None:
    eid = str(ev.id)
    w = wl["wishes"][eid]
    if _booked_this_week(wl, ev.start_at) >= int(wl["settings"].get("max_per_week", MAX_PER_WEEK)):
        _record(wl, eid, "skipped", "quota hebdo atteint")
        save_wl(wl)
        return
    if w.get("priority", 1) > 1 and _p1_booked_same_day(wl, w):
        _record(wl, eid, "skipped", "un créneau prioritaire du même jour est déjà réservé")
        save_wl(wl)
        return
    if ev.full and not w.get("waitlist_ok"):
        _record(wl, eid, "full", "complet (liste d'attente refusée)")
        save_wl(wl)
        notify("Sporteasy — complet", ev.name)
        return
    try:
        ok, msg = client.book(ev)
    except PermissionError as e:
        log.warning("session morte pendant la résa : %s", e)
        client.login()
        ok, msg = client.book(ev)
    except Exception as e:  # noqa: BLE001
        _record(wl, eid, "error", str(e)[:200])
        save_wl(wl)
        notify("Sporteasy — erreur", f"{ev.name}: {e}")
        return
    _record(wl, eid, "booked" if ok else ("waitlist" if "attente" in msg else "error"), msg)
    save_wl(wl)
    notify("Sporteasy", msg)
    # libère les fallbacks du même jour si un prioritaire vient d'être pris
    if ok and w.get("priority", 1) == 1:
        release_fallbacks(ev, wl)


def release_fallbacks(ev: Event, wl: dict[str, Any]) -> None:
    d = ev.start_at.isoformat()[:10]
    for eid, x in wl["wishes"].items():
        if x.get("priority", 1) > 1 and x.get("state") == "booked" and x["start_at"][:10] == d and eid != str(ev.id):
            try:
                fe = client.event(int(eid))
                client.set_attendance(fe, "absent")
                _record(wl, eid, "released", "libéré : un créneau prioritaire du même jour est réservé")
                save_wl(wl)
                notify("Sporteasy — libéré", fe.name)
            except Exception as e:  # noqa: BLE001
                log.warning("release fallback %s : %s", eid, e)


def schedule_or_book(ev: Event, wl: dict[str, Any]) -> None:
    """Le créneau est ouvert : au premier passage on tire un délai aléatoire, ensuite on réserve
    une fois le délai écoulé (le watcher repasse toutes les TICK secondes)."""
    eid = str(ev.id)
    w = wl["wishes"][eid]
    now = datetime.now().astimezone()
    if not w.get("book_at"):
        d = pick_delay(wl, ev.start_at)
        w["book_at"] = (now + timedelta(seconds=d)).isoformat(timespec="seconds")
        _record(wl, eid, "delaying", f"ouvert, je réserve dans {int(d // 60)}m{int(d % 60):02d}")
        save_wl(wl)
        return
    if now >= datetime.fromisoformat(w["book_at"]):
        try_book(ev, wl)


def hot_wait(ev: Event, wl: dict[str, Any]) -> None:
    """Mode rapide autour de l'heure d'ouverture : poll l'événement jusqu'à ce qu'il ouvre."""
    deadline = (ev.registration_open_at or datetime.now().astimezone()) + timedelta(minutes=HOT_MAX_MIN)
    log.info("⏱  mode rapide sur %s (ouverture %s)", ev.name, ev.registration_open_at)
    while datetime.now().astimezone() < deadline:
        try:
            fresh = client.event(ev.id)
        except PermissionError:
            client.login()
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("poll rapide : %s", e)
            time.sleep(HOT_INTERVAL * 2)
            continue
        if fresh.is_cancelled:
            _record(wl, str(ev.id), "cancelled", "créneau annulé par le club")
            save_wl(wl)
            return
        if fresh.step == "registration_opened" and fresh.update_url:
            schedule_or_book(fresh, wl)
            return
        time.sleep(HOT_INTERVAL)
    _record(wl, str(ev.id), "pending", f"pas ouvert après {HOT_MAX_MIN} min, on repassera")
    save_wl(wl)


def watcher() -> None:
    log.info("watcher démarré (tick %ss, fenêtre rapide %ss)", TICK, HOT_WINDOW)
    while True:
        try:
            with LOCK:
                wl = load_wl()
            active = {k: v for k, v in wl["wishes"].items()
                      if v.get("state") in (None, "pending", "waiting", "delaying", "skipped", "error", "waitlist", "full")}
            if wl["settings"].get("paused") or not active:
                time.sleep(TICK)
                continue
            events = {str(e.id): e for e in get_events(force=True)}
            now = datetime.now().astimezone()
            for eid, w in active.items():
                ev = events.get(eid)
                if ev is None:
                    continue
                if ev.is_cancelled:
                    _record(wl, eid, "cancelled", "créneau annulé par le club"); save_wl(wl); continue
                if ev.my_group == "participant" and ev.my_status == "present":
                    _record(wl, eid, "booked", "déjà inscrit"); save_wl(wl); continue
                if ev.step == "registration_opened" and ev.update_url:
                    schedule_or_book(ev, wl); continue
                if ev.registration_open_at and 0 <= (ev.registration_open_at - now).total_seconds() <= HOT_WINDOW:
                    # on dort jusqu'à T-5s puis mode rapide
                    wait = (ev.registration_open_at - now).total_seconds() - 5
                    if wait > 0:
                        time.sleep(wait)
                    hot_wait(ev, wl)
                elif w.get("state") != "waiting":
                    _record(wl, eid, "waiting", f"ouverture prévue {ev.registration_open_at:%a %d/%m %H:%M}" if ev.registration_open_at else "en attente")
                    save_wl(wl)
        except Exception as e:  # noqa: BLE001
            log.exception("watcher : %s", e)
        try:
            delaying = any(v.get("state") == "delaying" for v in load_wl()["wishes"].values())
        except Exception:  # noqa: BLE001
            delaying = False
        time.sleep(10 if delaying else TICK)


# ---------------------------------------------------------------- API
app = FastAPI(title="Sporteasy bot")


@app.middleware("http")
async def _auth(request: Request, call_next):
    if ACCESS_TOKEN:
        tok = request.query_params.get("token") or request.cookies.get("token")
        if tok != ACCESS_TOKEN:
            return JSONResponse({"detail": "token invalide"}, status_code=401)
        resp = await call_next(request)
        if request.query_params.get("token"):
            resp.set_cookie("token", ACCESS_TOKEN, max_age=60 * 60 * 24 * 365, httponly=True, samesite="lax")
        return resp
    return await call_next(request)


class Wish(BaseModel):
    event_id: int
    priority: int = 1
    waitlist_ok: bool = True


class Settings(BaseModel):
    paused: bool | None = None
    max_per_week: int | None = None
    delays: dict[str, list[float]] | None = None


@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/state")
def state():
    try:
        events = get_events()
        err = None
    except Exception as e:  # noqa: BLE001
        events, err = [], str(e)
    with LOCK:
        wl = load_wl()
    return {"events": [e.to_json() for e in events], "wishes": wl["wishes"], "settings": wl["settings"],
            "history": wl["history"][:30], "log": list(RECENT)[-40:], "error": err,
            "now": datetime.now().astimezone().isoformat(timespec="seconds")}


@app.post("/api/wish")
def add_wish(w: Wish):
    ev = next((e for e in get_events() if e.id == w.event_id), None)
    if ev is None:
        raise HTTPException(404, "événement inconnu")
    with LOCK:
        wl = load_wl()
        prev = wl["wishes"].get(str(ev.id), {})
        wl["wishes"][str(ev.id)] = {
            "name": ev.name, "start_at": ev.start_at.isoformat(),
            "registration_open_at": ev.registration_open_at.isoformat() if ev.registration_open_at else None,
            "priority": w.priority, "waitlist_ok": w.waitlist_ok,
            "state": prev.get("state") if prev.get("state") == "booked" else "pending",
            "added_at": prev.get("added_at") or datetime.now().isoformat(timespec="seconds"),
        }
        save_wl(wl)
    return wl["wishes"][str(ev.id)]


@app.delete("/api/wish/{event_id}")
def del_wish(event_id: int):
    with LOCK:
        wl = load_wl()
        wl["wishes"].pop(str(event_id), None)
        save_wl(wl)
    return {"ok": True}


@app.post("/api/settings")
def set_settings(s: Settings):
    with LOCK:
        wl = load_wl()
        for k, v in s.model_dump(exclude_none=True).items():
            wl["settings"][k] = v
        save_wl(wl)
    return wl["settings"]


@app.post("/api/login")
def force_login():
    try:
        client.login()
        get_events(force=True)
        return {"ok": True}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/book-now/{event_id}")
def book_now(event_id: int):
    """Réserve immédiatement un créneau déjà ouvert (test manuel)."""
    ev = next((e for e in get_events(force=True) if e.id == event_id), None)
    if ev is None or ev.step != "registration_opened":
        raise HTTPException(400, "créneau introuvable ou pas encore ouvert")
    with LOCK:
        wl = load_wl()
        wl["wishes"].setdefault(str(ev.id), {"name": ev.name, "start_at": ev.start_at.isoformat(),
                                             "priority": 1, "waitlist_ok": True, "state": "pending",
                                             "added_at": datetime.now().isoformat(timespec="seconds")})
        save_wl(wl)
        try_book(ev, wl)
        wl = load_wl()
    return wl["wishes"][str(ev.id)]


@app.post("/api/release/{event_id}")
def release(event_id: int):
    """Se désinscrire d'un créneau (passe en absent) et retirer la wish."""
    ev = client.event(event_id)
    client.set_attendance(ev, "absent")
    with LOCK:
        wl = load_wl()
        wl["wishes"].pop(str(event_id), None)
        wl["history"].insert(0, {"at": datetime.now().isoformat(timespec="seconds"), "event_id": str(event_id),
                                 "name": ev.name, "state": "released", "msg": "désinscrit manuellement"})
        save_wl(wl)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    try:
        client.ensure_login()
    except Exception as e:  # noqa: BLE001
        log.error("login impossible au démarrage : %s (l'UI tourne quand même, relance /api/login)", e)
    threading.Thread(target=watcher, daemon=True, name="watcher").start()
    log.info("UI : http://localhost:%s%s", PORT, f"/?token={ACCESS_TOKEN}" if ACCESS_TOKEN else "")
    # Sur le Mac : localhost uniquement. Dans le conteneur Railway : HOST=0.0.0.0 (défini par le Dockerfile).
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=PORT, log_level="warning")
