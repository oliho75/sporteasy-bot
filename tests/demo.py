"""
Serveur de démonstration : EZ bad branché sur un Sporteasy simulé (aucun compte, aucun appel réel).
Pratique pour retoucher l'interface.

    python tests/demo.py            # puis ouvrir le lien affiché
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_engine as T  # noqa: E402  (prépare un dossier de données temporaire et deux comptes : ao, dom)

app = T.app
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799


def build() -> None:
    for other in app.ACCOUNTS.values():      # tous les comptes sont simulés : rien ne part vers le vrai Sporteasy
        T.fresh(other)
    acc = T.AO
    w = T.fresh(acc)
    now = app.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    plan = {0: [("Rigal 19h30-21h30 [35]", 19, 30), ("Rigal 21h30-00h [35]", 21, 35)],
            1: [("Japy 20h-22h [30]", 20, 0)],
            3: [("Candie 20h-22h [65]", 20, 0), ("Candie 22h-0h [65]", 22, 5)],
            5: [("Candie 14h-16h jeu libre [25]", 14, 0)],
            6: [("Candie 16h-18h [65]", 16, 0)]}
    eid = 1000
    ids: list[int] = []
    for d in range(0, 24):
        day = day0 + timedelta(days=d)
        for name, h, m in plan.get(day.weekday(), []):
            start = day.replace(hour=h, minute=m)
            if start <= now:
                continue
            opens = (start - timedelta(days=2)).replace(hour=20 if h >= 20 else 19, minute=30 if h < 20 else (5 if m == 5 else 0))
            eid += 1
            w.add(eid, name, start, opens=opens, open=opens <= now)
            ids.append(eid)
    opened = [i for i in ids if w.events[i]["open"]]
    closed = [i for i in ids if not w.events[i]["open"]]
    if opened:
        w.events[opened[0]].update(group="participant", status="present")
        T.wish(acc, w, opened[0], prio=1, state="booked", last="✅ Réservé")
    if len(opened) > 1:
        w.events[opened[1]].update(group="waiting_list", status="present", full=True)
        T.wish(acc, w, opened[1], prio=2, state="waitlist", last="⏳ Liste d'attente")
    for i, prio in zip(closed[:4], (1, 2, 1, 1), strict=False):
        T.wish(acc, w, i, prio=prio)
    acc.tick()


if __name__ == "__main__":
    import uvicorn
    build()
    for a in app.ACCOUNTS.values():
        app.start(a)
    print(f"\n  EZ bad (démo) : http://127.0.0.1:{PORT}{T.AO.link}\n", flush=True)
    uvicorn.run(app.app, host="127.0.0.1", port=PORT, log_level="warning")
