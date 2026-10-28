"""
Client Sporteasy : login (Chrome headless via Playwright), lecture des événements,
réservation d'un créneau (PUT attendance_status).

Tout passe par l'API JSON officielle utilisée par le site :
  https://api.sporteasy.net/v2.1/teams/<team_id>/events/
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("sporteasy")

API = "https://api.sporteasy.net/v2.1"
LOGIN_URL = "https://app.sporteasy.net/en/login/"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


@dataclass
class Event:
    id: int
    name: str
    start_at: datetime
    end_at: datetime
    registration_open_at: datetime | None
    step: str                 # before_registration | registration_opened | ...
    is_cancelled: bool
    is_past: bool
    full: bool                # available_threshold_reached
    my_group: str | None      # participant | waiting_list | rsvp | unavailable
    my_status: str | None     # present | absent | None
    update_url: str | None    # PUT url pour changer mon statut
    location: str | None
    raw: dict[str, Any]

    @property
    def capacity(self) -> int | None:
        # Le club met la capacité dans le nom : "Rigal 19h30-21h30 [35]"
        import re
        m = re.search(r"\[(\d+)\]", self.name)
        return int(m.group(1)) if m else None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "start_at": self.start_at.isoformat(), "end_at": self.end_at.isoformat(),
            "registration_open_at": self.registration_open_at.isoformat() if self.registration_open_at else None,
            "step": self.step, "is_cancelled": self.is_cancelled, "is_past": self.is_past,
            "full": self.full, "my_group": self.my_group, "my_status": self.my_status,
            "capacity": self.capacity, "location": self.location,
            "counts": {g["slug_name"]: g.get("count") for g in (self.raw.get("attendance_groups") or [])},
        }


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class LoginError(RuntimeError):
    """Échec de connexion à Sporteasy, avec un message lisible par l'utilisateur."""


class SporteasyClient:
    def __init__(self, team_id: int, team_host: str, email: str, password: str, session_file: Path | None = None):
        self.session_file = Path(session_file) if session_file else Path(os.environ.get("DATA_DIR", ".")) / "session.json"
        self.team_id = team_id
        self.team_host = team_host            # ex: sitebad.sporteasy.net
        self.email = email
        self.password = password
        self._login_lock = threading.Lock()
        self._last_login_try = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json",
            "Origin": f"https://{team_host}",
            "Referer": f"https://{team_host}/",
        })
        self._load_session()

    # ---------- session ----------
    def _load_session(self) -> None:
        if self.session_file.exists():
            try:
                for c in json.loads(self.session_file.read_text()):
                    self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
                log.info("session chargée depuis %s", self.session_file)
            except Exception as e:  # noqa: BLE001
                log.warning("session.json illisible (%s), on relogue", e)

    def _save_session(self, cookies: list[dict[str, Any]]) -> None:
        self.session_file.write_text(json.dumps(cookies, indent=1))

    def is_logged_in(self) -> bool:
        try:
            now = datetime.now()
            r = self.s.get(f"{API}/teams/{self.team_id}/events/",
                           params={"month": now.month, "year": now.year, "web": 1}, timeout=15)
            if r.status_code != 200:
                log.info("check session : HTTP %s %s", r.status_code, r.text[:120])
            return r.status_code == 200
        except requests.RequestException:
            return False

    def login(self) -> None:
        """Login via Chrome headless : remplit le vrai formulaire, récupère les cookies."""
        if not self.email or not self.password:
            raise RuntimeError("SPORTEASY_EMAIL / SPORTEASY_PASSWORD manquants dans .env")
        # anti-rafale : un seul login en cours, et pas plus d'un toutes les 90 s
        with self._login_lock:
            if time.time() - self._last_login_try < 90:
                raise RuntimeError("login déjà tenté il y a moins de 90 s, on attend")
            self._last_login_try = time.time()
            self._login_browser()

    def _login_browser(self) -> None:
        from playwright.sync_api import sync_playwright

        log.info("login Sporteasy via Chrome headless…")
        import re as _re
        seen_team: list[int] = []

        def _on_request(req: Any) -> None:
            m = _re.search(r"api\.sporteasy\.net/v[\d.]+/teams/(\d+)/", req.url)
            if m:
                seen_team.append(int(m.group(1)))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(user_agent=UA, locale="fr-FR")
            page = ctx.new_page()
            page.on("request", _on_request)
            page.goto(f"{LOGIN_URL}?next=https://{self.team_host}/calendar/grid/", wait_until="networkidle")
            # bannière cookies (Didomi) si présente
            for sel in ["button:has-text('Disagree and close')", "button:has-text('Refuser')",
                        "button:has-text('Continuer sans accepter')", "#didomi-notice-disagree-button"]:
                try:
                    page.locator(sel).first.click(timeout=1500)
                    break
                except Exception:  # noqa: BLE001
                    pass
            email_box = page.locator("input[type='email'], input[name='email'], input[name='username']").first
            email_box.wait_for(state="visible", timeout=15000)
            email_box.click()
            email_box.fill(self.email)
            pwd_box = page.locator("input[type='password']").first
            pwd_box.click()
            pwd_box.fill(self.password)
            pwd_box.press("Enter")
            # Succès = on quitte la page de login ET un cookie de session apparaît.
            ok = False
            for _ in range(60):  # 30 s max
                page.wait_for_timeout(500)
                url = page.url
                if "/login" not in url and self.team_host in url:
                    ok = True
                    break
            cookies = ctx.cookies()
            if not ok:
                still_login = "/login" in page.url
                try:
                    err = page.locator(".error, [class*='error'], [role='alert']").first.inner_text(timeout=1000)
                except Exception:  # noqa: BLE001
                    err = ""
                browser.close()
                if still_login:
                    raise LoginError("email ou mot de passe Sporteasy incorrect" + (f" ({err.strip()[:80]})" if err else ""))
                raise LoginError(f"connexion refusée par le club {self.team_host} (tu en es bien membre ?)")
            # Identifiant du club : vu dans les appels de la page calendrier
            if not self.team_id:
                for _ in range(20):
                    if seen_team:
                        break
                    page.wait_for_timeout(500)
                if seen_team:
                    self.team_id = seen_team[0]
                    log.info("club %s → team_id %s", self.team_host, self.team_id)
            browser.close()
        if not self.team_id:
            raise LoginError(f"connecté, mais impossible de trouver le calendrier du club {self.team_host}")

        self.s.cookies.clear()
        for c in cookies:
            self.s.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
        self._save_session(cookies)
        if not self.is_logged_in():
            raise RuntimeError("login OK dans le navigateur mais l'API refuse les cookies")
        log.info("login OK")

    def ensure_login(self) -> None:
        if not self.is_logged_in():
            self.login()

    # ---------- events ----------
    def _parse(self, e: dict[str, Any]) -> Event:
        me = e.get("me") or {}
        grp = me.get("group") or {}
        links = me.get("_links") or {}
        return Event(
            id=e["id"], name=e.get("name") or "",
            start_at=_dt(e["start_at"]), end_at=_dt(e["end_at"]),
            registration_open_at=_dt(e.get("registration_open_at")),
            step=e.get("step") or "", is_cancelled=bool(e.get("is_cancelled")),
            is_past=bool(e.get("is_past")), full=bool(e.get("available_threshold_reached")),
            my_group=grp.get("slug_name"), my_status=grp.get("attendance_status"),
            update_url=(links.get("update_attendance_status") or {}).get("url") or me.get("url"),
            location=(e.get("location") or {}).get("name"),
            raw=e,
        )

    def events_month(self, year: int, month: int) -> list[Event]:
        r = self.s.get(f"{API}/teams/{self.team_id}/events/",
                       params={"month": month, "year": year, "web": 1}, timeout=20)
        if r.status_code in (401, 403):
            raise PermissionError("session expirée")
        r.raise_for_status()
        return [self._parse(e) for e in r.json().get("results", [])]

    def event(self, event_id: int) -> Event:
        r = self.s.get(f"{API}/teams/{self.team_id}/events/{event_id}/", timeout=20)
        if r.status_code in (401, 403):
            raise PermissionError("session expirée")
        r.raise_for_status()
        return self._parse(r.json())

    def upcoming(self, months_ahead: int = 2) -> list[Event]:
        now = datetime.now().astimezone()
        out: list[Event] = []
        y, m = now.year, now.month
        for _ in range(months_ahead + 1):
            out += self.events_month(y, m)
            m += 1
            if m > 12:
                m, y = 1, y + 1
        seen: set[int] = set()
        res = []
        for e in sorted(out, key=lambda e: e.start_at):
            if e.id in seen or e.is_past:
                continue
            seen.add(e.id)
            res.append(e)
        return res

    # ---------- booking ----------
    def set_attendance(self, ev: Event, status: str = "present") -> dict[str, Any]:
        """status: present | absent. Retourne la réponse JSON."""
        url = ev.update_url
        if not url:
            raise RuntimeError(f"pas d'URL de mise à jour pour {ev.name} (inscription pas ouverte ?)")
        csrf = next((c.value for c in self.s.cookies if c.name in ("se_csrftoken", "csrftoken")), None)
        headers = {"Content-Type": "application/json"}
        if csrf:
            headers["X-CSRFToken"] = csrf
        r = self.s.put(url, json={"attendance_status": status}, headers=headers, timeout=20)
        if r.status_code in (401, 403):
            raise PermissionError(f"refusé ({r.status_code}) : {r.text[:200]}")
        if r.status_code >= 400:
            raise RuntimeError(f"PUT {r.status_code} : {r.text[:300]}")
        try:
            return r.json()
        except ValueError:
            return {"status_code": r.status_code, "text": r.text[:300]}

    def book(self, ev: Event) -> tuple[bool, str]:
        """Tente de réserver. Retourne (succès, message)."""
        self.set_attendance(ev, "present")
        time.sleep(0.5)
        fresh = self.event(ev.id)
        if fresh.my_group == "participant" and fresh.my_status == "present":
            return True, f"✅ Réservé : {ev.name} ({ev.start_at:%a %d/%m %H:%M})"
        if fresh.my_group == "waiting_list":
            return False, f"⏳ Liste d'attente : {ev.name} ({ev.start_at:%a %d/%m %H:%M})"
        return False, f"❓ Statut après tentative : {fresh.my_group}/{fresh.my_status} sur {ev.name}"
