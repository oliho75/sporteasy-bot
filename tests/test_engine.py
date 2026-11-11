"""
Tests du moteur EZ bad avec un Sporteasy simulé (aucun appel réseau réel).

    python tests/test_engine.py            # tout
    python tests/test_engine.py waitlist   # seulement les tests dont le nom contient « waitlist »
"""
from __future__ import annotations

import http.server
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp(prefix="ezbad-test-")
os.environ.update(
    DATA_DIR=TMP, TZ="Europe/Paris", PUBLIC_URL="https://ezbad.test",
    USER1_NAME="ao", USER1_EMAIL="ao@example.com", USER1_PASSWORD="x",
    USER1_TOKEN="tok-ao-0123456789abcdef0123456789abcdef", USER1_INVITE="ao-test", USER1_NTFY="topic-ao",
    USER2_NAME="dom", USER2_EMAIL="dom@example.com", USER2_PASSWORD="x",
    USER2_TOKEN="tok-dom-0123456789abcdef0123456789abcde", USER2_INVITE="dom-test",
)
for k in ("RAILWAY_ENVIRONMENT", "PUBLIC", "RESEND_API_KEY", "NOTIFY_WEBHOOK", "SECRET_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

import sporteasy  # noqa: E402
from sporteasy import Event, EventGone, LoginError, SporteasyClient  # noqa: E402

sporteasy.time = SimpleNamespace(sleep=lambda s: None, time=time.time)   # book() sans pauses

import app  # noqa: E402

app.HOT_INTERVAL = 0.05
NOTIFS: list[tuple[str, str]] = []
REAL_NOTIFY = app.notify
app.notify = lambda title, msg, *a, **k: NOTIFS.append((title, msg))


# ------------------------------------------------------------------ Sporteasy simulé
class World:
    def __init__(self) -> None:
        self.events: dict[int, dict] = {}
        self.puts: list[tuple[int, str]] = []
        self.fail_put = 0            # nombre de PUT à faire échouer
        self.fail_get = 0            # nombre de lectures de liste à faire échouer
        self.dead_session = False
        self.bad_password = False
        self.logins = 0
        self.hidden: set[int] = set()   # créneaux absents de la liste mais qui existent (liste incomplète)
        self.on_fetch = None

    def add(self, eid: int, name: str, start: datetime, opens: datetime | None = None, open: bool = False,
            full: bool = False) -> dict:
        self.events[eid] = {"name": name, "start": start, "opens": opens, "open": open, "full": full,
                            "cancelled": False, "group": None, "status": None}
        return self.events[eid]


class FakeClient(SporteasyClient):
    def __init__(self, world: World, tag: str) -> None:
        super().__init__(1, "club.test", "x@example.com", "x", session_file=Path(TMP) / f"session-{tag}.json")
        self.w = world

    def is_logged_in(self) -> bool:
        return not self.w.dead_session

    def login(self, force: bool = False) -> None:
        self.w.logins += 1
        if self.w.bad_password:
            raise LoginError("email ou mot de passe Sporteasy incorrect")
        self.w.dead_session = False

    def _ev(self, eid: int) -> Event:
        d = self.w.events[eid]
        return Event(id=eid, name=d["name"], start_at=d["start"], end_at=d["start"] + timedelta(hours=2),
                     registration_open_at=d["opens"], step="registration_opened" if d["open"] else "before_registration",
                     is_cancelled=d["cancelled"], is_past=d["start"] < app.now(), full=d["full"],
                     my_group=d["group"], my_status=d["status"], update_url=f"fake://{eid}" if d["open"] else None,
                     location="Gymnase", raw={})

    def events_month(self, year: int, month: int) -> list[Event]:
        if self.w.dead_session:
            raise PermissionError("session expirée")
        if self.w.fail_get:
            self.w.fail_get -= 1
            raise requests.ConnectionError("boom")
        if self.w.on_fetch:
            hook, self.w.on_fetch = self.w.on_fetch, None
            hook()
        return [self._ev(i) for i, d in self.w.events.items()
                if d["start"].year == year and d["start"].month == month and i not in self.w.hidden]

    def event(self, event_id: int, timeout=None) -> Event:
        if self.w.dead_session:
            raise PermissionError("session expirée")
        if event_id not in self.w.events:
            raise EventGone(str(event_id))
        return self._ev(event_id)

    def set_attendance(self, ev: Event, status: str = "present", timeout=None) -> dict:
        if self.w.dead_session:
            raise PermissionError("session expirée")
        self.w.puts.append((ev.id, status))
        if self.w.fail_put:
            self.w.fail_put -= 1
            raise RuntimeError("Sporteasy a refusé (500) : boom")
        d = self.w.events[ev.id]
        if status == "present":
            d["group"], d["status"] = ("waiting_list", "present") if d["full"] else ("participant", "present")
        else:
            d["group"], d["status"] = "unavailable", "absent"
        return {}


# ------------------------------------------------------------------ outils
AO = app.ACCOUNTS["ao"]
DAY = (app.now() + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)


def at(h: int, m: int = 0, days: int = 0) -> datetime:
    return DAY.replace(hour=h, minute=m) + timedelta(days=days)


def fresh(acc=AO) -> World:
    """Remet le compte à zéro avec un monde vide ; délais nuls."""
    acc.active = False          # arrête les fils rapides laissés par le test précédent
    try:
        settle(acc)
    finally:
        acc.active = True
    w = World()
    acc.client = FakeClient(w, acc.name)
    acc.cache = {"at": 0.0, "events": []}
    acc.inflight.clear(); acc.hot_started.clear()
    acc._last_sync = 0.0; acc._auth_alert = False; acc._saved = None; acc._last_test = 0.0; acc.last_err = None
    acc.recent.clear()
    acc.save({"wishes": {}, "history": [],
              "settings": {"paused": False, "max_per_week": 5,
                           "delays": {d: [0, 0] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun", "default")}}})
    NOTIFS.clear()
    with app._SENT_LOCK:
        app._SENT.clear()
    with app._HITS_LOCK:
        app._HITS.clear()
    return w


def wish(acc, w: World, eid: int, prio: int = 1, **extra) -> None:
    d = w.events[eid]
    with acc.lock:
        wl = acc.load()
        wl["wishes"][str(eid)] = {"name": d["name"], "start_at": d["start"].isoformat(),
                                  "registration_open_at": d["opens"].isoformat() if d["opens"] else None,
                                  "priority": prio, "waitlist_ok": True, "state": "pending",
                                  "added_at": app._iso(), **extra}
        acc.save(wl)


def settle(acc=AO, timeout: float = 15.0) -> None:
    """Attend la fin des fils de réservation lancés par un passage."""
    end = time.time() + timeout
    while time.time() < end:
        busy = [t for t in threading.enumerate() if "schedule_or_book" in t.name or "_hot_at" in t.name]
        if not busy and not acc.inflight:
            return
        time.sleep(0.02)
    raise AssertionError("fils de réservation toujours actifs")


def tick(acc=AO, n: int = 1) -> None:
    for _ in range(n):
        acc.cache["at"] = 0.0       # en vrai les passages sont espacés de 30 s : on relit Sporteasy à chaque fois
        acc.tick()
        settle(acc)


def st(acc, eid: int) -> str | None:
    return acc.load()["wishes"].get(str(eid), {}).get("state")


def hist(acc) -> list[str]:
    return [f"{h['state']}:{h['msg']}" for h in acc.load()["history"]]


def age(acc, eid: int, seconds: float = 120) -> None:
    """Vieillit la dernière action sur une option (pour les décisions qui demandent une minute de recul)."""
    with acc.lock:
        wl = acc.load(); wl["wishes"][str(eid)]["ts"] = time.time() - seconds; acc.save(wl)


def eq(a, b, what: str = "") -> None:
    assert a == b, f"{what} : attendu {b!r}, obtenu {a!r}"


# ------------------------------------------------------------------ moteur
def test_fuseau_du_club():
    eq(str(app.TZ), "Europe/Paris")
    eq(sporteasy.fr_when(datetime(2026, 11, 12, 19, 0, tzinfo=timezone.utc)), "jeu. 12/11 20:00", "heure d'hiver")
    eq(sporteasy.fr_when(datetime(2026, 7, 6, 17, 30, tzinfo=timezone.utc)), "lun. 06/07 19:30", "heure d'été")
    eq(app.now().utcoffset(), datetime.now(app.TZ).utcoffset())
    ev = sporteasy._dt("2026-11-12T19:00:00Z")
    eq(ev.isoformat(), "2026-11-12T20:00:00+01:00", "date en Z ramenée à l'heure du club")


def test_programme_puis_reserve_sans_repetition():
    w = fresh()
    e = w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), opens=app.now() + timedelta(hours=5))
    wish(AO, w, 1)
    tick(n=5)
    eq(st(AO, 1), "waiting")
    eq(len([h for h in hist(AO) if h.startswith("waiting")]), 1, "une seule ligne « programmé » en 5 passages")
    assert "ouverture prévue" in hist(AO)[0] and "." in hist(AO)[0], hist(AO)   # jour en français (« jeu. »)
    eq(w.puts, [])
    e["open"] = True
    tick(n=3)
    eq(st(AO, 1), "booked")
    eq(w.puts, [(1, "present")], "une seule demande envoyée")
    eq(len(NOTIFS), 1); assert NOTIFS[0][1].startswith("✅ Réservé"), NOTIFS
    tick(n=5)
    eq(len(NOTIFS), 1, "pas de notification en double")
    eq(w.puts, [(1, "present")])


def test_waitlist_aucune_relance_puis_promotion():
    """Le bug d'origine : en liste d'attente, le bot renvoyait une demande + une notif toutes les 30 s."""
    w = fresh()
    p1 = w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), open=True, full=True)
    w.add(2, "Rigal 21h30-00h [35]", at(21, 30), open=True)
    wish(AO, w, 1, prio=1); wish(AO, w, 2, prio=2)
    tick(n=2)
    eq(st(AO, 1), "waitlist"); eq(st(AO, 2), "booked")
    puts, notifs = list(w.puts), list(NOTIFS)
    eq(sorted(puts), [(1, "present"), (2, "present")])
    tick(n=20)
    eq(w.puts, puts, "aucune nouvelle demande pendant la liste d'attente")
    eq(NOTIFS, notifs, "aucune nouvelle notification pendant la liste d'attente")
    eq(len([h for h in hist(AO) if h.startswith("waitlist")]), 1, "une seule ligne d'historique")
    # une place se libère : Sporteasy me passe inscrit
    p1["group"], p1["status"] = "participant", "present"
    tick()
    eq(st(AO, 1), "booked")
    assert any("place s'est libérée" in m for _, m in NOTIFS), NOTIFS
    eq(st(AO, 2), "released", "le fallback du même jour est libéré")
    assert (2, "absent") in w.puts
    assert any(t == "EZ bad — libéré" for t, _ in NOTIFS), NOTIFS
    n = len(NOTIFS); tick(n=5); eq(len(NOTIFS), n)


def test_waitlist_quittee_sur_sporteasy():
    w = fresh()
    e = w.add(1, "Candie 20h-22h [65]", at(20), open=True, full=True)
    wish(AO, w, 1)
    tick()
    eq(st(AO, 1), "waitlist")
    e["group"], e["status"] = "unavailable", "absent"      # la personne se retire elle-même sur Sporteasy
    tick()
    eq(st(AO, 1), "waitlist", "pas de conclusion à chaud (la liste peut être en retard)")
    age(AO, 1)
    tick()
    eq(st(AO, 1), "waitlist", "un seul constat ne suffit pas")
    tick(n=2)
    eq(st(AO, 1), "released")
    eq(len(w.puts), 1, "le bot ne la réinscrit pas")


def test_fallback_ignore_une_seule_fois():
    """L'autre bug d'origine : « ignoré » réécrit dans l'historique toutes les 30 s."""
    w = fresh()
    w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), open=True)
    w.add(2, "Rigal 21h30-00h [35]", at(21, 30), opens=app.now() + timedelta(hours=3))
    wish(AO, w, 1, prio=1); wish(AO, w, 2, prio=2)
    tick()
    eq(st(AO, 1), "booked")
    w.events[2]["open"] = True
    tick(n=15)
    eq(st(AO, 2), "skipped")
    eq(len([h for h in hist(AO) if h.startswith("skipped")]), 1, "une seule ligne « ignoré » en 15 passages")
    eq(w.puts, [(1, "present")])
    eq(len(NOTIFS), 1)


def test_fallback_attend_le_resultat_de_la_priorite():
    """Les deux ouvrent en même temps : le fallback ne se réserve pas pour être libéré 2 s après."""
    w = fresh()
    w.add(1, "P1", at(19, 30), open=True)
    w.add(2, "P2", at(21, 30), open=True)
    wish(AO, w, 1, prio=1); wish(AO, w, 2, prio=2)
    with AO.lock:
        wl = AO.load()
        wl["wishes"]["1"].update(state="delaying", book_at=app._iso(app.now() + timedelta(seconds=2)))
        wl["wishes"]["2"].update(state="delaying", book_at=app._iso(app.now() + timedelta(seconds=0)))
        AO.save(wl)
    AO.tick(); settle(AO, 20)
    eq(st(AO, 1), "booked"); eq(st(AO, 2), "skipped")
    eq(w.puts, [(1, "present")], "le fallback n'a jamais été demandé")


def test_fallback_pris_si_la_priorite_est_complete():
    w = fresh()
    w.add(1, "P1", at(19, 30), open=True, full=True)
    w.add(2, "P2", at(21, 30), open=True)
    wish(AO, w, 1, prio=1); wish(AO, w, 2, prio=2)
    with AO.lock:
        wl = AO.load()
        wl["wishes"]["1"].update(state="delaying", book_at=app._iso(app.now() + timedelta(seconds=1)))
        wl["wishes"]["2"].update(state="delaying", book_at=app._iso(app.now()))
        AO.save(wl)
    AO.tick(); settle(AO, 20)
    eq(st(AO, 1), "waitlist"); eq(st(AO, 2), "booked")


def test_erreur_une_notif_puis_reprise():
    w = fresh()
    w.add(1, "Candie 20h-22h [65]", at(20), open=True)
    wish(AO, w, 1)
    w.fail_put = 2
    AO.tick(); settle(AO, 30)          # 1er essai raté, 2e à +3 s raté, 3e à +6 s réussi
    eq(st(AO, 1), "booked")
    eq(len(w.puts), 3)
    errs = [n for n in NOTIFS if n[0] == "EZ bad — erreur"]
    eq(len(errs), 1, "une seule notification d'erreur")
    assert any(m.startswith("✅ Réservé") for _, m in NOTIFS), NOTIFS
    eq(len([h for h in hist(AO) if h.startswith("error")]), 1)


def test_erreur_persistante_espacee():
    w = fresh()
    w.add(1, "Candie 20h-22h [65]", at(20), open=True)
    wish(AO, w, 1)
    w.fail_put = 10 ** 6
    with AO.lock:      # on part d'un 6e échec : prochain essai dans plus de 90 s → le fil rend la main
        wl = AO.load(); wl["wishes"]["1"].update(state="error", tries=6, book_at=app._iso(), last="x",
                                                 retry_at=app._iso(app.now() - timedelta(seconds=1))); AO.save(wl)
    tick(n=10)
    eq(len(w.puts), 1, "un seul nouvel essai sur 10 passages (le suivant est dans plusieurs minutes)")
    wl = AO.load(); eq(wl["wishes"]["1"]["tries"], 7)
    assert app._parse(wl["wishes"]["1"]["retry_at"]) - app.now() > timedelta(seconds=150)
    eq([n for n in NOTIFS if n[0] == "EZ bad — erreur"], [], "pas de nouvelle notif (déjà prévenu au 1er échec)")


def test_session_expiree_pendant_la_resa():
    w = fresh()
    w.add(1, "Candie 20h-22h [65]", at(20), open=True)
    wish(AO, w, 1)
    AO.events(force=True)
    w.dead_session = True
    AO.schedule_or_book(AO.client._ev(1))
    eq(st(AO, 1), "booked"); eq(w.logins, 1)


def test_mot_de_passe_refuse_une_seule_alerte():
    w = fresh()
    w.add(1, "Candie 20h-22h [65]", at(20), opens=app.now() + timedelta(hours=3))
    wish(AO, w, 1)
    w.dead_session = w.bad_password = True
    for _ in range(6):
        try:
            AO.tick()
        except Exception:  # noqa: BLE001
            pass
    alerts = [n for n in NOTIFS if n[0] == "EZ bad — connexion impossible"]
    eq(len(alerts), 1, "une seule alerte de connexion")
    assert AO.last_err
    w.dead_session = w.bad_password = False
    tick()
    eq(AO.last_err, None); eq(st(AO, 1), "waiting")


def test_creneau_supprime_ou_liste_incomplete():
    w = fresh()
    w.add(1, "Candie 14h-16h [65]", at(14), opens=app.now() + timedelta(hours=3))
    w.add(2, "Candie 16h-18h [65]", at(16), opens=app.now() + timedelta(hours=3))
    wish(AO, w, 1); wish(AO, w, 2)
    tick()
    w.hidden.add(2)            # absent de la liste, mais existe toujours → on ne doit PAS l'annuler
    del w.events[1]            # vraiment supprimé par le club
    tick(n=4)
    eq(st(AO, 1), "cancelled"); eq(st(AO, 2), "waiting")
    eq(len([n for n in NOTIFS if n[0] == "EZ bad — créneau supprimé"]), 1)


def test_creneau_reserve_puis_annule_par_le_club():
    w = fresh()
    e = w.add(1, "Candie 20h-22h [65]", at(20), open=True)
    wish(AO, w, 1)
    tick()
    eq(st(AO, 1), "booked")
    e["cancelled"] = True
    AO._last_sync = 0
    tick(n=3)
    eq(st(AO, 1), "cancelled")
    eq(len([n for n in NOTIFS if n[0] == "EZ bad — créneau annulé"]), 1)


def test_desinscrit_sur_sporteasy_libere_le_quota():
    w = fresh()
    a = w.add(1, "A", at(19, 0), open=True)
    w.add(2, "B", at(19, 0, days=1) if at(19, 0, days=1).isocalendar()[1] == at(19).isocalendar()[1] else at(21), open=False,
          opens=app.now() + timedelta(hours=3))
    with AO.lock:
        wl = AO.load(); wl["settings"]["max_per_week"] = 1; AO.save(wl)
    wish(AO, w, 1); wish(AO, w, 2)
    tick()
    eq(st(AO, 1), "booked")
    w.events[2]["open"] = True
    tick(n=2)
    eq(st(AO, 2), "skipped", "quota de 1 atteint")
    a["group"], a["status"] = "unavailable", "absent"      # désinscription à la main sur Sporteasy
    age(AO, 1)
    AO._last_sync = 0
    tick(n=3)
    eq(st(AO, 1), "released"); eq(st(AO, 2), "booked", "le quota est libéré, B est réservé")


def test_quota_compte_les_inscriptions_manuelles():
    w = fresh()
    m = w.add(1, "Manuel", at(18), open=True)
    m["group"], m["status"] = "participant", "present"     # inscrit à la main, sans option dans EZ bad
    w.add(2, "Voulu", at(20), open=True)
    with AO.lock:
        wl = AO.load(); wl["settings"]["max_per_week"] = 1; AO.save(wl)
    wish(AO, w, 2)
    tick(n=2)
    eq(st(AO, 2), "skipped"); eq(w.puts, [])


def test_pas_de_fausse_liberation_juste_apres_une_resa():
    """Une liste Sporteasy lue juste avant la résa ne doit pas faire passer l'option en « libéré »."""
    w = fresh()
    e = w.add(1, "Candie 20h-22h [65]", at(20), open=True)
    wish(AO, w, 1)
    tick()
    eq(st(AO, 1), "booked")
    e["group"] = e["status"] = None          # liste en retard : elle ne me montre pas encore inscrit
    AO._last_sync = 0
    tick(n=3)
    eq(st(AO, 1), "booked", "résa toute fraîche : on ne conclut rien")


def test_option_ajoutee_pendant_un_passage_conservee():
    """Avant : le watcher réécrivait une copie périmée et pouvait effacer un clic fait au même moment."""
    w = fresh()
    w.add(1, "A", at(19), opens=app.now() + timedelta(hours=3))
    w.add(2, "B", at(21), opens=app.now() + timedelta(hours=3))
    wish(AO, w, 1)
    w.on_fetch = lambda: wish(AO, w, 2)      # clic de l'utilisateur pendant que le watcher lit Sporteasy
    tick()
    assert st(AO, 2) is not None, "l'option ajoutée pendant le passage a disparu"
    tick()
    eq(st(AO, 1), "waiting"); eq(st(AO, 2), "waiting")


def test_pause_bloque_tout():
    w = fresh()
    w.add(1, "A", at(19), open=True)
    wish(AO, w, 1)
    with AO.lock:
        wl = AO.load(); wl["settings"]["paused"] = True; AO.save(wl)
    tick(n=3)
    AO.schedule_or_book(AO.client._ev(1))
    eq(w.puts, []); eq(st(AO, 1), "pending")


def test_mode_rapide_detecte_l_ouverture():
    w = fresh()
    e = w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), opens=app.now() + timedelta(seconds=2))
    wish(AO, w, 1)
    AO.tick()
    assert any("_hot_at" in t.name for t in threading.enumerate()), "fil rapide non lancé"
    time.sleep(0.4)
    eq(w.puts, [])
    e["open"] = True
    t0 = time.time()
    while st(AO, 1) != "booked" and time.time() - t0 < 5:
        time.sleep(0.02)
    eq(st(AO, 1), "booked")
    assert time.time() - t0 < 1.0, f"détection trop lente : {time.time() - t0:.2f} s"
    settle()


def test_mode_rapide_s_arrete_si_option_retiree():
    w = fresh()
    w.add(1, "A", at(19), opens=app.now() + timedelta(seconds=1))
    wish(AO, w, 1)
    AO.tick()
    with AO.lock:
        wl = AO.load(); wl["wishes"].pop("1"); AO.save(wl)
    settle(AO, 10)          # le fil rapide rend la main tout seul


def test_donnees_existantes_de_la_prod():
    """Format écrit par l'ancienne version (dates naïves, book_at en +00:00, pas de « ts »)."""
    w = fresh()
    b = w.add(51794397, "Candie 20h-22h [65]", at(20), open=True)
    b["group"], b["status"] = "participant", "present"
    w.add(51794411, "Candie 20h-22h [65]", at(20, days=7), opens=at(20, days=5))
    old = (app.now() - timedelta(days=30)).replace(microsecond=0)
    recent = (app.now() - timedelta(days=3)).replace(microsecond=0)
    AO._saved = None
    AO.save({"wishes": {
        "51801534": {"name": "Candie 14h-16h [65]", "start_at": old.isoformat(), "registration_open_at": None, "priority": 1,
                     "waitlist_ok": True, "state": "cancelled", "added_at": "2026-10-22T17:40:05", "last": "créneau supprimé par le club"},
        "51792686": {"name": "Rigal 19h30-21h30 [35]", "start_at": recent.isoformat(), "priority": 2, "waitlist_ok": True,
                     "state": "waitlist", "book_at": "2026-11-01T21:40:04+00:00", "added_at": "2026-11-01T21:39:00",
                     "last": "⏳ Liste d'attente : Rigal 19h30-21h30 [35] (Mon 02/11 19:30)", "updated_at": "2026-11-02T18:33:44"},
        "51794397": {"name": "Candie 20h-22h [65]", "start_at": b["start"].isoformat(), "priority": 1, "waitlist_ok": True,
                     "state": "booked", "book_at": "2026-11-10T19:03:26+00:00", "added_at": "2026-11-10T19:03:00",
                     "last": "✅ Réservé : Candie 20h-22h [65] (Thu 12/11 20:00)", "updated_at": "2026-11-10T19:03:28"},
        "51794411": {"name": "Candie 20h-22h [65]", "start_at": at(20, days=7).isoformat(),
                     "registration_open_at": at(20, days=5).isoformat(), "priority": 1, "waitlist_ok": True, "state": "waiting",
                     "added_at": "2026-11-10T19:03:50", "last": "ouverture prévue Tue 17/11 20:00", "updated_at": "2026-11-10T19:03:50"},
    }, "settings": {"paused": False, "max_per_week": 5, "delays": {"mon": [0.033, 0.083], "thu": [0.08, 0.83], "default": [1.0, 5.0]}},
        "history": [{"at": "2026-11-10T19:03:28", "event_id": "51794397", "name": "Candie 20h-22h [65]", "state": "booked", "msg": "x"}]})
    tick(n=3)
    wl = AO.load()
    assert "51801534" not in wl["wishes"], "option vieille de 30 jours purgée"
    eq(wl["wishes"]["51792686"]["state"], "waitlist", "créneau passé : on n'y touche plus")
    eq(wl["wishes"]["51794397"]["state"], "booked")
    eq(wl["wishes"]["51794411"]["state"], "waiting")
    eq(w.puts, []); eq(NOTIFS, [])
    eq(wl["settings"]["delays"]["mon"], [0.033, 0.083], "réglages intacts")


def test_delai_tire_selon_le_jour():
    fresh()
    wl = AO.load(); wl["settings"]["delays"] = {"mon": [0.033, 0.083], "thu": [0.08, 0.83], "default": [1, 5]}
    monday = datetime(2026, 11, 16, 19, 30, tzinfo=app.TZ); thursday = datetime(2026, 11, 19, 20, 0, tzinfo=app.TZ)
    saturday = datetime(2026, 11, 21, 14, 0, tzinfo=app.TZ)
    for _ in range(200):
        assert 1.9 <= AO.pick_delay(wl, monday) <= 5.0
        assert 4.7 <= AO.pick_delay(wl, thursday) <= 49.9
        assert 60 <= AO.pick_delay(wl, saturday) <= 300
    wl["settings"]["delays"] = {"mon": ["x"], "default": None}        # réglage cassé : on retombe sur les valeurs par défaut
    assert 60 <= AO.pick_delay(wl, monday) <= 300


def test_http_reprise_si_sporteasy_coupe_la_connexion():
    """Journal de prod : « Remote end closed connection without response » plusieurs fois par jour.
    Le client réessaie maintenant tout seul, pour les lectures comme pour la demande de place."""
    import socket
    drops = {"left": 1, "seen": []}
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(8)
    port = srv.getsockname()[1]

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            data = conn.recv(65536)
            drops["seen"].append(data.split(b" ", 1)[0].decode())
            if drops["left"] > 0:
                drops["left"] -= 1
                conn.close(); continue                     # coupe sans répondre
            body = b'{"ok": true}'
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        c = SporteasyClient(1, "club.test", "x@example.com", "x", session_file=Path(TMP) / "session-http.json")
        r = c.s.get(f"http://127.0.0.1:{port}/events/", timeout=(2, 2))
        eq(r.status_code, 200, "lecture reprise après une coupure")
        drops["left"] = 1
        ev = Event(id=7, name="X", start_at=app.now(), end_at=app.now(), registration_open_at=None, step="registration_opened",
                   is_cancelled=False, is_past=False, full=False, my_group=None, my_status=None,
                   update_url=f"http://127.0.0.1:{port}/events/7/profiles/1/", location=None, raw={})
        eq(c.set_attendance(ev, "present", timeout=(2, 2)), {"ok": True}, "demande de place reprise après 1 coupure")
        eq(drops["seen"], ["GET", "GET", "PUT", "PUT"])
    finally:
        srv.close()


# ------------------------------------------------------------------ notifications
class _Sink:
    def __init__(self, status: int = 200) -> None:
        self.got: list[dict] = []
        sink = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sink.got.append(json.loads(body))
                self.send_response(status); self.end_headers(); self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = socketserver.TCPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def close(self) -> None:
        self.srv.shutdown(); self.srv.server_close()


def test_notif_titre_avec_tiret_long_et_accents():
    """Avant : tout titre contenant « — » ou un accent hors latin-1 faisait échouer l'envoi (jamais reçu)."""
    fresh()
    sink = _Sink(); app.NTFY_URL = sink.url
    try:
        res = app._deliver({"title": "EZ bad — créneau supprimé", "msg": "Rigal 19h30 n'existe plus 🏸", "ntfy": "topic-ao",
                            "tags": "wastebasket", "email": "", "click": "https://ezbad.test/u/ao/"})
        eq(res.get("ntfy"), "ok")
        eq(sink.got[0]["title"], "EZ bad — créneau supprimé"); eq(sink.got[0]["topic"], "topic-ao")
        eq(sink.got[0]["message"], "Rigal 19h30 n'existe plus 🏸"); eq(sink.got[0]["tags"], ["wastebasket"])
        eq(sink.got[0]["click"], "https://ezbad.test/u/ao/")
    finally:
        sink.close()


def test_notif_anti_doublon_et_file():
    fresh()
    sink = _Sink(); app.NTFY_URL = sink.url
    try:
        for _ in range(5):
            REAL_NOTIFY("EZ bad", "⏳ Liste d'attente : Rigal", "topic-ao")
        REAL_NOTIFY("EZ bad", "✅ Réservé : Rigal", "topic-ao")
        REAL_NOTIFY("EZ bad", "⏳ Liste d'attente : Rigal", "topic-dom")     # autre destinataire : passe
        app._NQ.join()
        eq(len(sink.got), 3, "5 notifications identiques → 1 seul envoi")
    finally:
        sink.close()


# ------------------------------------------------------------------ API
def _client():
    from fastapi.testclient import TestClient
    return TestClient(app.app, follow_redirects=False)


def test_api_acces_et_cookies():
    fresh()
    c = _client()
    r = c.get("/u/ao/"); eq(r.status_code, 401); assert "lien perso" in r.text
    eq(c.get("/u/ao/api/state").status_code, 401)
    eq(c.get("/u/inconnu/api/state").status_code, 404)
    r = c.get("/i/ao-test"); eq(r.status_code, 302); eq(r.headers["location"], "/u/ao/")
    assert "httponly" in r.headers["set-cookie"].lower()
    r = c.get("/u/ao/"); eq(r.status_code, 200); eq(r.headers["cache-control"], "no-cache")
    s = c.get("/u/ao/api/state").json()
    eq(s["version"], app.VERSION); eq(s["user"], "ao"); eq(s["account"]["link"], "/i/ao-test")
    assert s["now"].endswith(app.now().strftime("%z")[:3] + ":00"), s["now"]
    eq(c.get("/u/dom/api/state").status_code, 401, "le cookie d'ao n'ouvre pas le compte de dom")
    eq(c.get("/i/ancien-code").status_code, 302, "ancien lien + téléphone déjà connu → sa page")
    eq(c.get("/healthz").json(), {"ok": True, "version": app.VERSION})
    eq(c.get("/docs").status_code, 404); eq(c.get("/openapi.json").status_code, 404)
    assert "secure" not in r.headers.get("set-cookie", "").lower(), "en http local, pas de cookie Secure"
    viahttps = _client().get("/i/ao-test", headers={"x-forwarded-proto": "https"})
    assert "secure" in viahttps.headers["set-cookie"].lower(), "derrière l'hébergeur (https) : cookie Secure"


def test_api_limite_les_essais_de_liens():
    fresh()
    c = _client()
    codes = [c.get(f"/i/devine-{i}").status_code for i in range(18)]
    eq(codes[:15], [404] * 15); eq(codes[15:], [429] * 3)
    eq(c.get("/i/ao-test").status_code, 429, "même un bon code est refusé pendant le blocage")
    c2 = _client(); c2.headers["x-real-ip"] = "203.0.113.9"
    eq(c2.get("/i/ao-test").status_code, 302, "les autres adresses ne sont pas bloquées")


def test_api_options_reglages_desinscription():
    w = fresh()
    w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), opens=app.now() + timedelta(hours=3))
    e2 = w.add(2, "Candie 20h-22h [65]", at(20), open=True)
    e2["group"], e2["status"] = "participant", "present"
    c = _client(); c.get("/i/ao-test")
    eq(c.post("/u/ao/api/wish", json={"event_id": 1, "priority": 3}).status_code, 400)
    eq(c.post("/u/ao/api/wish", json={"event_id": 999}).status_code, 404)
    AO._wake.clear()
    r = c.post("/u/ao/api/wish", json={"event_id": 1, "priority": 1}); eq(r.status_code, 200); eq(r.json()["state"], "pending")
    assert AO._wake.is_set(), "le watcher est réveillé tout de suite"
    eq(c.post("/u/ao/api/settings", json={"delays": {"mon": [1]}}).status_code, 400)
    eq(c.post("/u/ao/api/settings", json={"delays": {"lundi": [1, 2]}}).status_code, 400)
    eq(c.post("/u/ao/api/settings", json={"delays": {"mon": [-1, 2]}}).status_code, 400)
    eq(c.post("/u/ao/api/settings", json={"max_per_week": 0}).status_code, 400)
    r = c.post("/u/ao/api/settings", json={"delays": {"mon": [0.083, 0.033], "default": [1, 5]}, "max_per_week": 4})
    eq(r.json()["delays"]["mon"], [0.033, 0.083]); eq(r.json()["max_per_week"], 4)
    eq(c.post("/u/ao/api/release/2").status_code, 200)
    eq(w.puts, [(2, "absent")])
    eq(c.post("/u/ao/api/release/424242").status_code, 404)
    eq(c.delete("/u/ao/api/wish/1").json(), {"ok": True}); eq(st(AO, 1), None)


def test_api_calendrier_de_secours_si_sporteasy_tombe():
    w = fresh()
    w.add(1, "Rigal 19h30-21h30 [35]", at(19, 30), opens=app.now() + timedelta(hours=3))
    c = _client(); c.get("/i/ao-test")
    eq(len(c.get("/u/ao/api/state").json()["events"]), 1)
    assert w.events
    AO.cache["at"] = 0; w.fail_get = 5
    s = c.get("/u/ao/api/state").json()
    eq(len(s["events"]), 1, "dernier calendrier connu affiché"); eq(s["stale"], True); eq(s["error"], "Sporteasy injoignable")


def test_api_test_notif_dit_la_verite():
    fresh()
    c = _client(); c.get("/i/ao-test")
    ok = _Sink(); app.NTFY_URL = ok.url
    try:
        eq(c.post("/u/ao/api/test-notif").status_code, 200); eq(len(ok.got), 1)
        eq(c.post("/u/ao/api/test-notif").status_code, 429, "pas deux tests coup sur coup")
    finally:
        ok.close()
    ko = _Sink(status=429); app.NTFY_URL = ko.url; AO._last_test = 0
    try:
        r = c.post("/u/ao/api/test-notif"); eq(r.status_code, 502); assert "429" in r.json()["detail"]
    finally:
        ko.close()


def test_api_invitation_admin_seulement():
    fresh(); fresh(app.ACCOUNTS["dom"])
    c = _client(); c.get("/i/dom-test")
    eq(c.post("/u/dom/api/invites").status_code, 403)
    a = _client(); a.get("/i/ao-test")
    code = a.post("/u/ao/api/invites").json()["code"]
    eq(_client().get(f"/join/{code}").status_code, 200)
    eq(_client().get("/join/nimportequoi").status_code, 404)
    members = a.get("/u/ao/api/invites").json()["members"]
    eq(sorted(m["name"] for m in members), ["ao", "dom"]); assert all(m["ok"] for m in members)


class JoinClient(FakeClient):
    """Remplace SporteasyClient pendant l'inscription : « mauvais » est refusé, le reste passe."""
    def __init__(self, team_id, team_host, email, password, session_file=None) -> None:
        SporteasyClient.__init__(self, team_id or 4242, team_host, email, password, session_file=session_file)
        self.w = World()

    def login(self, force: bool = False) -> None:
        if self.password == "mauvais":
            raise LoginError("email ou mot de passe Sporteasy incorrect")
        self.session_file.write_text("[]")


def test_api_inscription_par_invitation():
    fresh()
    real, app.SporteasyClient = app.SporteasyClient, JoinClient
    try:
        a = _client(); a.get("/i/ao-test")
        code = a.post("/u/ao/api/invites").json()["code"]
        c = _client()
        r = c.post(f"/join/{code}", json={"name": "Léa Test", "email": "lea@example.com", "password": "mauvais"})
        eq(r.status_code, 401)
        r = c.post(f"/join/{code}", json={"name": "ao", "email": "lea@example.com", "password": "bon"})
        eq(r.status_code, 409, "nom déjà pris")
        r = c.post(f"/join/{code}", json={"name": "Lea Test", "email": "lea@example.com", "password": "S3cret-bon",
                                          "club": "https://autreclub.sporteasy.net/calendar/", "ntfy": "ezbad-lea-x1"})
        eq(r.status_code, 200); eq(r.json()["redirect"], "/u/lea-test/")
        acc = app.ACCOUNTS["lea-test"]
        eq(acc.kind, "db"); eq(acc.team_host, "autreclub.sporteasy.net"); eq(acc.team_id, 4242); eq(acc.ntfy, "ezbad-lea-x1")
        raw = (Path(TMP) / "accounts.json").read_text()
        assert "S3cret-bon" not in raw, "le mot de passe ne doit jamais être stocké en clair"
        eq(app.decrypt(json.loads(raw)["lea-test"]["pw"]), "S3cret-bon")
        eq(c.get("/u/lea-test/api/state").status_code, 200, "cookie posé à l'inscription")
        eq(_client().get(f"/join/{code}").status_code, 404, "invitation à usage unique")
        eq(c.post(f"/join/{code}", json={"name": "zoe", "email": "z@example.com", "password": "bon"}).status_code, 410)
        assert any(t == "EZ bad — nouveau membre" for t, _ in NOTIFS), NOTIFS
        eq(c.post("/u/lea-test/api/account/password", json={"password": "mauvais"}).status_code, 401)
        eq(c.post("/u/lea-test/api/account/password", json={"password": "Nouveau-9"}).status_code, 200)
        eq(app.decrypt(json.loads((Path(TMP) / "accounts.json").read_text())["lea-test"]["pw"]), "Nouveau-9")
        eq(a.post("/u/ao/api/account/password", json={"password": "x"}).status_code, 400, "compte géré par variables")
        eq(c.delete("/u/lea-test/api/account").status_code, 200)
        assert "lea-test" not in app.ACCOUNTS and not (Path(TMP) / "lea-test").exists()
        eq(c.get("/u/lea-test/api/state").status_code, 404)
    finally:
        app.SporteasyClient = real


# ------------------------------------------------------------------ lanceur
if __name__ == "__main__":
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f) and only in n]
    failed = 0
    for name, fn in tests:
        t0 = time.time()
        try:
            fn()
            print(f"  ok   {name}  ({time.time() - t0:.1f} s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}\n{traceback.format_exc()}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passés")
    sys.exit(1 if failed else 0)
