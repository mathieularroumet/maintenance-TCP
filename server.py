#!/usr/bin/env python3
"""
Plateforme de signalement de maintenance pour club de sport.

- Chaque espace (court, salle, douches...) a un QR code unique.
- Le QR code ouvre un formulaire public : type de problème + description.
  La date et l'emplacement sont enregistrés automatiquement.
- Le back office (/admin) permet à l'équipe de maintenance de suivre et
  traiter les signalements : problème vu, correction planifiée (avec date),
  pas de solution, problème résolu.

Aucune dépendance : Python 3.9+ et sa bibliothèque standard (SQLite inclus).
Lancement : ADMIN_PASSWORD=monmotdepasse python3 server.py
"""

import csv
import hashlib
import hmac
import html
import io
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlencode, urlparse

# --------------------------------------------------------------------------
# Configuration (variables d'environnement)
# --------------------------------------------------------------------------

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(APP_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "maintenance.db")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))
CLUB_NAME = os.environ.get("CLUB_NAME", "Mon club de sport")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")  # ex. https://maintenance.monclub.fr
TRUST_PROXY = os.environ.get("TRUST_PROXY", "0") == "1"  # derrière nginx/Caddy/hébergeur
SESSION_TTL = 60 * 60 * 12  # 12 h

try:
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
except Exception:  # tzdata absent (Windows) : heure locale du serveur
    TZ = None

os.makedirs(DATA_DIR, exist_ok=True)


def _load_or_create(path, factory):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            value = f.read().strip()
        if value:
            return value
    value = factory()
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value


SECRET_KEY = os.environ.get("SECRET_KEY") or _load_or_create(
    os.path.join(DATA_DIR, "secret_key.txt"), lambda: secrets.token_hex(32)
)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or _load_or_create(
    os.path.join(DATA_DIR, "admin_password.txt"), lambda: secrets.token_urlsafe(10)
)
# La clé de signature dépend du mot de passe : le changer déconnecte tout le monde.
SIGNING_KEY = hashlib.sha256((SECRET_KEY + "|" + ADMIN_PASSWORD).encode()).digest()

# --------------------------------------------------------------------------
# Référentiels
# --------------------------------------------------------------------------

TYPES = {
    "nettoyage": ("Nettoyage", "🧹"),
    "equipement": ("Remplacement d'équipement", "🔧"),
    "danger": ("Défaut dangereux", "⚠️"),
    "autre": ("Autre", "💬"),
}

STATUSES = {
    "nouveau": "Nouveau",
    "vu": "Problème vu",
    "planifie": "Correction planifiée",
    "sans_solution": "Pas de solution",
    "resolu": "Problème résolu",
}
OPEN_STATUSES = ("nouveau", "vu", "planifie")
CLOSED_STATUSES = ("sans_solution", "resolu")

DEFAULT_SPACES = [
    ("Court de tennis n°1", "Tennis"),
    ("Court de tennis n°2", "Tennis"),
    ("Salle de musculation", "Salles"),
    ("Espace de vie", "Espaces communs"),
    ("Douches hommes", "Vestiaires"),
    ("Douches femmes", "Vestiaires"),
]

# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------


def now():
    return (datetime.now(TZ) if TZ else datetime.now()).replace(microsecond=0, tzinfo=None)


def now_str():
    return now().strftime("%Y-%m-%d %H:%M:%S")


def today():
    return now().date()


def esc(value):
    return html.escape("" if value is None else str(value), quote=True)


def fmt_dt(value):
    if not value:
        return ""
    try:
        d = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        return d.strftime("%d/%m/%Y à %H:%M")
    except ValueError:
        return value


def fmt_date(value):
    if not value:
        return ""
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return value


def parse_date(value):
    try:
        return datetime.strptime((value or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def slugify(text):
    text = text.lower()
    for a, b in (("àâä", "a"), ("éèêë", "e"), ("îï", "i"), ("ôö", "o"), ("ùûü", "u"), ("ç", "c")):
        for ch in a:
            text = text.replace(ch, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "espace"


def new_space_code():
    return secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")


# --------------------------------------------------------------------------
# Base de données
# --------------------------------------------------------------------------

_db_init_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with _db_init_lock, db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS spaces (
                id          INTEGER PRIMARY KEY,
                code        TEXT UNIQUE NOT NULL,
                name        TEXT NOT NULL,
                zone        TEXT NOT NULL DEFAULT '',
                active      INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reports (
                id            INTEGER PRIMARY KEY,
                space_id      INTEGER NOT NULL REFERENCES spaces(id),
                type          TEXT NOT NULL,
                description   TEXT NOT NULL,
                contact       TEXT NOT NULL DEFAULT '',
                status        TEXT NOT NULL DEFAULT 'nouveau',
                planned_date  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id            INTEGER PRIMARY KEY,
                report_id     INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                status        TEXT NOT NULL,
                planned_date  TEXT,
                note          TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
            CREATE INDEX IF NOT EXISTS idx_reports_space ON reports(space_id);
            CREATE INDEX IF NOT EXISTS idx_events_report ON events(report_id);
            """
        )
        if conn.execute("SELECT COUNT(*) FROM spaces").fetchone()[0] == 0:
            for name, zone in DEFAULT_SPACES:
                conn.execute(
                    "INSERT INTO spaces (code, name, zone, created_at) VALUES (?,?,?,?)",
                    (new_space_code(), name, zone, now_str()),
                )


# --------------------------------------------------------------------------
# Sessions administrateur (cookie signé HMAC + jeton CSRF)
# --------------------------------------------------------------------------


def _sign(payload):
    return hmac.new(SIGNING_KEY, payload.encode(), hashlib.sha256).hexdigest()


def make_session():
    exp = int(time.time()) + SESSION_TTL
    csrf = secrets.token_urlsafe(16)
    payload = f"{exp}.{csrf}"
    return f"{payload}.{_sign(payload)}"


def read_session(token):
    """Renvoie le jeton CSRF si la session est valide, sinon None."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        exp, csrf = payload.split(".", 1)
        if int(exp) < time.time():
            return None
        return csrf
    except (ValueError, AttributeError):
        return None


class RateLimiter:
    def __init__(self, limit, window):
        self.limit, self.window = limit, window
        self.hits = {}
        self.lock = threading.Lock()

    def allow(self, key):
        t = time.time()
        with self.lock:
            recent = [h for h in self.hits.get(key, []) if t - h < self.window]
            if len(recent) >= self.limit:
                self.hits[key] = recent
                return False
            recent.append(t)
            self.hits[key] = recent
            if len(self.hits) > 5000:  # nettoyage simple
                self.hits = {k: v for k, v in self.hits.items() if v and t - v[-1] < self.window}
            return True


report_limiter = RateLimiter(limit=8, window=600)  # 8 signalements / 10 min / IP
login_limiter = RateLimiter(limit=10, window=900)  # 10 essais / 15 min / IP

# --------------------------------------------------------------------------
# Gabarits HTML
# --------------------------------------------------------------------------

CSS = """
:root{
  --bg:#f4f6f3;--surface:#fff;--ink:#17201a;--muted:#5b6a60;--line:#dbe2dc;
  --accent:#1d6b47;--accent-ink:#fff;--accent-soft:#e2f0e8;
  --danger:#b3261e;--danger-soft:#fce8e6;--warn:#8f5400;--warn-soft:#fdf0d6;
  --info:#1c5ea6;--info-soft:#e3edf9;--seen:#5b3ea6;--seen-soft:#ece6f8;
  --grey:#555f58;--grey-soft:#eaedea;--radius:14px;
  --shadow:0 1px 2px rgba(20,30,25,.06),0 4px 14px rgba(20,30,25,.05);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#101512;--surface:#18201b;--ink:#e8eee9;--muted:#9fb0a5;--line:#2b362f;
  --accent:#4cc38a;--accent-ink:#06210f;--accent-soft:#183a28;
  --danger:#ff8a80;--danger-soft:#3d1a18;--warn:#f2b45c;--warn-soft:#3a2a10;
  --info:#82b4f0;--info-soft:#16283f;--seen:#b9a3f2;--seen-soft:#2a2240;
  --grey:#b3bfb7;--grey-soft:#262f29;--shadow:none;}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
h1,h2,h3{line-height:1.2;margin:0 0 .5rem}
h1{font-size:1.6rem;letter-spacing:-.01em}h2{font-size:1.15rem}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.narrow{max-width:560px}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow)}
.stack>*+*{margin-top:14px}
.muted{color:var(--muted)}.small{font-size:.875rem}
.club{font-size:.8rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:600}
.place{display:flex;gap:10px;align-items:center;background:var(--accent-soft);color:var(--ink);border-radius:12px;padding:12px 14px;font-weight:600}
.place small{display:block;font-weight:400;color:var(--muted)}
label{font-weight:600;display:block;margin-bottom:6px}
.opt{font-weight:400;color:var(--muted)}
input[type=text],input[type=password],input[type=date],input[type=search],input:not([type]),textarea,select{
  width:100%;font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:11px 12px}
textarea{resize:vertical;min-height:120px}
input:focus,textarea:focus,select:focus,button:focus-visible,a:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent);outline-offset:1px}
fieldset{border:0;margin:0;padding:0}
legend{font-weight:600;margin-bottom:8px;padding:0}
.types{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.type{position:relative;display:flex;flex-direction:column;gap:6px;align-items:flex-start;margin:0;padding:14px;border:1.5px solid var(--line);border-radius:12px;cursor:pointer;font-weight:600;min-height:92px;background:var(--surface)}
.type input{position:absolute;opacity:0;pointer-events:none}
.type .ico{font-size:1.5rem;line-height:1}
.type:has(input:checked){border-color:var(--accent);background:var(--accent-soft)}
.type.t-danger:has(input:checked){border-color:var(--danger);background:var(--danger-soft)}
.type:has(input:focus-visible){outline:3px solid color-mix(in srgb,var(--accent) 45%,transparent)}
.danger-note{display:none;background:var(--danger-soft);color:var(--danger);border-radius:10px;padding:10px 12px;font-size:.92rem;font-weight:600}
form:has(input[value=danger]:checked) .danger-note{display:block}
.hp{position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;font:inherit;font-weight:600;border-radius:10px;border:1px solid var(--line);background:var(--surface);color:var(--ink);padding:9px 14px;cursor:pointer;text-decoration:none;white-space:nowrap}
.btn:hover{border-color:var(--muted)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn.big{width:100%;padding:14px;font-size:1.05rem}
.btn.sm{padding:6px 10px;font-size:.875rem}
.btn.s-vu{border-color:var(--seen);color:var(--seen)}
.btn.s-planifie{border-color:var(--warn);color:var(--warn)}
.btn.s-sans_solution{border-color:var(--grey);color:var(--grey)}
.btn.s-resolu{background:var(--accent);border-color:var(--accent);color:var(--accent-ink)}
.btn[disabled]{opacity:.6;cursor:default}
.error{background:var(--danger-soft);color:var(--danger);border-radius:10px;padding:10px 12px;font-weight:600}
.notice{background:var(--accent-soft);border-radius:10px;padding:10px 12px;font-weight:600}
.badge{display:inline-flex;align-items:center;gap:4px;border-radius:999px;padding:2px 10px;font-size:.8rem;font-weight:600;white-space:nowrap;background:var(--grey-soft);color:var(--grey)}
.b-nouveau{background:var(--info-soft);color:var(--info)}
.b-vu{background:var(--seen-soft);color:var(--seen)}
.b-planifie{background:var(--warn-soft);color:var(--warn)}
.b-resolu{background:var(--accent-soft);color:var(--accent)}
.b-danger{background:var(--danger-soft);color:var(--danger)}
.b-late{background:var(--danger);color:#fff}
.existing{list-style:none;margin:0;padding:0}
.existing li{padding:10px 0;border-top:1px solid var(--line);display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.existing li:first-child{border-top:0}
.big-check{font-size:3rem;line-height:1}
/* back office */
.top{background:var(--surface);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.top .wrap{display:flex;gap:6px 18px;align-items:center;flex-wrap:wrap;padding-top:10px;padding-bottom:10px}
.brand{font-weight:700;margin-right:auto}
.brand span{color:var(--muted);font-weight:500}
.nav a{text-decoration:none;color:var(--muted);font-weight:600;padding:6px 2px;border-bottom:2px solid transparent}
.nav a.on,.nav a:hover{color:var(--ink);border-bottom-color:var(--accent)}
.nav{display:flex;gap:16px;flex-wrap:wrap}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{padding:14px 16px}
.kpi b{display:block;font-size:1.8rem;line-height:1.1}
.kpi.alert b{color:var(--danger)}
.filters{display:grid;grid-template-columns:2fr 1fr 1fr 1fr auto;gap:10px;align-items:end}
.filters label{font-size:.8rem;color:var(--muted);margin-bottom:4px}
.list{display:flex;flex-direction:column;gap:8px}
.row{display:grid;grid-template-columns:150px 1fr 190px;gap:12px;align-items:center;text-decoration:none;color:inherit;padding:14px 16px}
.row:hover{border-color:var(--muted)}
.row.is-danger{border-left:4px solid var(--danger)}
.row .desc{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.row .when{font-size:.85rem;color:var(--muted)}
.row .st{display:flex;flex-direction:column;gap:4px;align-items:flex-end}
.meta-grid{display:grid;grid-template-columns:170px 1fr;gap:8px 16px;margin:0}
.meta-grid dt{color:var(--muted)}.meta-grid dd{margin:0}
.desc-full{white-space:pre-wrap;background:var(--bg);border-radius:10px;padding:12px 14px;margin:0}
.actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.plan{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:12px;border:1px dashed var(--warn);border-radius:12px}
.plan input{width:auto}
.timeline{list-style:none;margin:0;padding:0}
.timeline li{position:relative;padding:0 0 14px 22px;border-left:2px solid var(--line);margin-left:6px}
.timeline li:last-child{border-left-color:transparent}
.timeline li::before{content:"";position:absolute;left:-7px;top:4px;width:12px;height:12px;border-radius:50%;background:var(--surface);border:2px solid var(--accent)}
.two{display:grid;grid-template-columns:1.4fr 1fr;gap:16px;align-items:start}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:10px 8px;border-top:1px solid var(--line);vertical-align:middle}
th{font-size:.8rem;color:var(--muted);font-weight:600;border-top:0}
.inline{display:flex;gap:6px;align-items:center}
.inline input{min-width:0}
.add{display:grid;grid-template-columns:2fr 1fr auto;gap:10px;align-items:end}
.empty{text-align:center;padding:40px 16px;color:var(--muted)}
.qr-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:16px}
.qr-card{text-align:center;break-inside:avoid;page-break-inside:avoid}
.qr-card .qr{width:190px;height:190px;margin:10px auto}
.qr-card .qr svg{width:100%;height:100%;display:block}
.qr-card h3{font-size:1.15rem;margin:4px 0 0}
.qr-card .cta{font-weight:700;color:var(--accent)}
.qr-card .url{font-size:.7rem;color:var(--muted);word-break:break-all}
@media (max-width:760px){
  .kpis{grid-template-columns:1fr 1fr}
  .filters{grid-template-columns:1fr 1fr}
  .filters .q{grid-column:1/-1}
  .row{grid-template-columns:1fr;gap:6px}
  .row .st{flex-direction:row;align-items:center;flex-wrap:wrap}
  .two{grid-template-columns:1fr}
  .meta-grid{grid-template-columns:1fr;gap:2px}
  .meta-grid dd{margin-bottom:8px}
  .add{grid-template-columns:1fr}
  .hide-sm{display:none}
}
@media print{
  body{background:#fff;color:#000}
  .top,.no-print{display:none!important}
  .wrap{max-width:none;padding:0}
  .qr-grid{grid-template-columns:1fr 1fr;gap:10mm}
  .qr-card{border:1.5px solid #000;box-shadow:none;padding:8mm;background:#fff;color:#000}
  .qr-card .cta{color:#000}
  .qr-card .qr{width:60mm;height:60mm}
}
"""

FORM_JS = """
document.querySelectorAll('form[data-once]').forEach(function(f){
  f.addEventListener('submit',function(){
    var b=f.querySelector('button[type=submit]'); if(b){setTimeout(function(){b.disabled=true;b.textContent='Envoi…';},0);}
  });
});
"""


def page(title, body, admin_nav=None, extra_head=""):
    nav = ""
    if admin_nav is not None:
        links = [
            ("signalements", "/admin", "Signalements"),
            ("espaces", "/admin/espaces", "Espaces"),
            ("qr", "/admin/qr", "QR codes"),
        ]
        items = "".join(
            f'<a href="{href}" class="{"on" if key == admin_nav else ""}">{label}</a>'
            for key, href, label in links
        )
        nav = (
            '<header class="top"><div class="wrap">'
            f'<div class="brand">{esc(CLUB_NAME)} <span>· Maintenance</span></div>'
            f'<nav class="nav">{items}<a href="/admin/logout">Déconnexion</a></nav>'
            "</div></header>"
        )
    return (
        "<!doctype html><html lang=fr><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{esc(title)} · {esc(CLUB_NAME)}</title>"
        "<link rel=icon href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛠️</text></svg>\">"
        f"<style>{CSS}</style>{extra_head}</head><body>{nav}{body}"
        f"<script>{FORM_JS}</script></body></html>"
    )


def status_badge(status):
    return f'<span class="badge b-{esc(status)}">{esc(STATUSES.get(status, status))}</span>'


def type_badge(t):
    label, ico = TYPES.get(t, (t, ""))
    cls = "b-danger" if t == "danger" else ""
    return f'<span class="badge {cls}">{ico} {esc(label)}</span>'


def late_badge(report):
    if report["status"] == "planifie" and report["planned_date"]:
        d = parse_date(report["planned_date"])
        if d and d < today():
            return '<span class="badge b-late">En retard</span>'
    return ""


# --------------------------------------------------------------------------
# Serveur HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "ClubMaintenance/1.0"
    sys_version = ""

    # ---- plomberie -------------------------------------------------------

    def log_message(self, fmt, *args):
        sys.stderr.write("%s [%s] %s\n" % (self.client_ip(), now_str(), fmt % args))

    def do_GET(self):
        self.dispatch("GET")

    def do_HEAD(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def client_ip(self):
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For", "") if hasattr(self, "headers") else ""
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "?"

    def base_url(self):
        if BASE_URL:
            return BASE_URL
        proto = "https" if (TRUST_PROXY and self.headers.get("X-Forwarded-Proto") == "https") else "http"
        host = self.headers.get("X-Forwarded-Host") if TRUST_PROXY else None
        return f"{proto}://{host or self.headers.get('Host', f'localhost:{PORT}')}"

    def cookies(self):
        c = SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            pass
        return c

    def session_csrf(self):
        morsel = self.cookies().get("cm_session")
        return read_session(morsel.value) if morsel else None

    def send(self, status, body, ctype="text/html; charset=utf-8", headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        if self.path.startswith("/admin"):
            self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            if isinstance(v, list):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def redirect(self, location, headers=None):
        h = {"Location": location}
        h.update(headers or {})
        self.send(303, "", headers=h)

    def not_found(self, msg="Cette page n'existe pas."):
        body = (
            '<main class="wrap narrow stack"><div class="card stack" style="margin-top:10vh;text-align:center">'
            f'<div class="big-check">🔎</div><h1>Introuvable</h1><p class="muted">{esc(msg)}</p></div></main>'
        )
        self.send(404, page("Introuvable", body))

    def dispatch(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        self.query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.form = {}
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length > 200_000:
                return self.send(413, "Requête trop volumineuse", "text/plain; charset=utf-8")
            raw = self.rfile.read(length).decode("utf-8", "replace")
            self.form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

        for m, pattern, func, admin in ROUTES:
            if m != method:
                continue
            match = re.fullmatch(pattern, path)
            if not match:
                continue
            try:
                if admin:
                    csrf = self.session_csrf()
                    if not csrf:
                        if method == "GET":
                            return self.redirect("/admin/login?" + urlencode({"next": self.path}))
                        return self.send(403, "Session expirée. Reconnectez-vous.", "text/plain; charset=utf-8")
                    self.csrf = csrf
                    if method == "POST" and not hmac.compare_digest(self.form.get("csrf", ""), csrf):
                        return self.send(403, "Jeton de sécurité invalide. Rechargez la page.", "text/plain; charset=utf-8")
                return func(self, *match.groups())
            except Exception:
                traceback.print_exc()
                return self.send(500, page("Erreur", '<main class="wrap narrow"><div class="card"><h1>Erreur serveur</h1><p>Une erreur inattendue est survenue.</p></div></main>'))
        self.not_found()

    def csrf_field(self):
        return f'<input type="hidden" name="csrf" value="{esc(self.csrf)}">'

    # ---- pages publiques -------------------------------------------------

    def home(self):
        body = (
            '<main class="wrap narrow stack" style="margin-top:8vh">'
            f'<div class="club">{esc(CLUB_NAME)}</div>'
            "<h1>Signaler un problème de maintenance</h1>"
            '<div class="card stack"><p>Pour signaler un problème, <b>scannez le QR code</b> affiché dans l\'espace '
            "concerné (court, salle, vestiaire…). Le lieu sera reconnu automatiquement.</p></div>"
            '<p class="small muted"><a href="/admin">Accès équipe maintenance</a></p></main>'
        )
        self.send(200, page("Signaler un problème", body))

    def get_space(self, code, active_only=True):
        with db() as conn:
            sql = "SELECT * FROM spaces WHERE code = ?" + (" AND active = 1" if active_only else "")
            return conn.execute(sql, (code,)).fetchone()

    def report_form(self, code, error=None, values=None):
        space = self.get_space(code)
        if not space:
            return self.not_found("Ce QR code n'est pas (ou plus) associé à un espace. Merci de prévenir l'accueil.")
        values = values or {}
        with db() as conn:
            existing = conn.execute(
                f"SELECT * FROM reports WHERE space_id = ? AND status IN ({','.join('?'*len(OPEN_STATUSES))}) "
                "ORDER BY created_at DESC LIMIT 5",
                (space["id"], *OPEN_STATUSES),
            ).fetchall()

        types_html = "".join(
            f'<label class="type t-{key}"><input type="radio" name="type" value="{key}" required '
            f'{"checked" if values.get("type") == key else ""}><span class="ico" aria-hidden="true">{ico}</span>'
            f"<span>{esc(label)}</span></label>"
            for key, (label, ico) in TYPES.items()
        )
        existing_html = ""
        if existing:
            items = "".join(
                f"<li>{type_badge(r['type'])} {status_badge(r['status'])}"
                + (f' <span class="small muted">prévue le {fmt_date(r["planned_date"])}</span>' if r["status"] == "planifie" else "")
                + f'<span class="small" style="flex-basis:100%">{esc(r["description"][:110])}{"…" if len(r["description"]) > 110 else ""}</span>'
                f'<span class="small muted">Signalé le {fmt_dt(r["created_at"])}</span></li>'
                for r in existing
            )
            existing_html = (
                '<section class="card stack"><h2>Déjà signalé ici</h2>'
                '<p class="small muted">Votre problème figure peut-être déjà dans cette liste.</p>'
                f'<ul class="existing">{items}</ul></section>'
            )

        zone = f"<small>{esc(space['zone'])}</small>" if space["zone"] else ""
        err = f'<div class="error" role="alert">{esc(error)}</div>' if error else ""
        body = f"""
<main class="wrap narrow stack">
  <div class="club" style="margin-top:8px">{esc(CLUB_NAME)}</div>
  <h1>Signaler un problème</h1>
  <div class="place"><span aria-hidden="true" style="font-size:1.4rem">📍</span><div>{esc(space['name'])}{zone}</div></div>
  {err}
  <form method="post" class="card stack" data-once>
    <fieldset>
      <legend>Type de problème</legend>
      <div class="types">{types_html}</div>
    </fieldset>
    <div class="danger-note">Danger immédiat ? Prévenez aussi l'accueil sans attendre et éloignez les autres usagers.</div>
    <div>
      <label for="description">Décrivez le problème</label>
      <textarea id="description" name="description" required minlength="5" maxlength="2000"
        placeholder="Ex. : le filet est déchiré côté gauche, une dalle bouge près du banc…">{esc(values.get('description', ''))}</textarea>
    </div>
    <div>
      <label for="contact">Nom ou e-mail <span class="opt">(facultatif, pour être recontacté)</span></label>
      <input id="contact" name="contact" maxlength="120" autocomplete="email" value="{esc(values.get('contact', ''))}">
    </div>
    <div class="hp" aria-hidden="true"><label>Ne pas remplir <input name="website" tabindex="-1" autocomplete="off"></label></div>
    <button type="submit" class="btn primary big">Envoyer le signalement</button>
    <p class="small muted" style="margin:8px 0 0;text-align:center">La date, l'heure et le lieu sont enregistrés automatiquement.</p>
  </form>
  {existing_html}
</main>"""
        self.send(400 if error else 200, page(f"Signaler · {space['name']}", body))

    def report_submit(self, code):
        space = self.get_space(code)
        if not space:
            return self.not_found("Ce QR code n'est pas (ou plus) associé à un espace.")
        f = self.form
        if f.get("website"):  # pot de miel anti-robots : on fait semblant
            return self.redirect(f"/s/{code}/merci")
        rtype = f.get("type", "")
        desc = (f.get("description") or "").strip()
        contact = (f.get("contact") or "").strip()[:120]
        if rtype not in TYPES:
            return self.report_form(code, "Choisissez un type de problème.", f)
        if len(desc) < 5:
            return self.report_form(code, "Décrivez le problème en quelques mots (5 caractères minimum).", f)
        if len(desc) > 2000:
            return self.report_form(code, "Description trop longue (2000 caractères maximum).", f)
        if not report_limiter.allow(self.client_ip()):
            return self.report_form(code, "Trop de signalements envoyés depuis cet appareil. Réessayez dans quelques minutes.", f)
        ts = now_str()
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO reports (space_id, type, description, contact, status, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (space["id"], rtype, desc, contact, "nouveau", ts, ts),
            )
            rid = cur.lastrowid
            conn.execute(
                "INSERT INTO events (report_id, status, note, created_at) VALUES (?,?,?,?)",
                (rid, "nouveau", "Signalement reçu via QR code", ts),
            )
        self.redirect(f"/s/{code}/merci?n={rid}")

    def report_thanks(self, code):
        space = self.get_space(code)
        if not space:
            return self.not_found()
        n = self.query.get("n", "")
        num = f"<p>Numéro de suivi : <b>#{esc(n)}</b></p>" if n.isdigit() else ""
        body = f"""
<main class="wrap narrow stack" style="margin-top:8vh;text-align:center">
  <div class="card stack">
    <div class="big-check">✅</div>
    <h1>Merci, c'est transmis !</h1>
    <p>Votre signalement pour <b>{esc(space['name'])}</b> a bien été enregistré. L'équipe de maintenance va le prendre en charge.</p>
    {num}
    <a class="btn" href="/s/{esc(code)}">Signaler un autre problème ici</a>
  </div>
</main>"""
        self.send(200, page("Merci", body))

    # ---- connexion -------------------------------------------------------

    def login_page(self, error=None, status=200):
        nxt = self.query.get("next") or self.form.get("next") or "/admin"
        err = f'<div class="error" role="alert">{esc(error)}</div>' if error else ""
        body = f"""
<main class="wrap narrow stack" style="margin-top:10vh;max-width:420px">
  <div class="club">{esc(CLUB_NAME)}</div>
  <h1>Back office maintenance</h1>
  {err}
  <form method="post" action="/admin/login" class="card stack" data-once>
    <input type="hidden" name="next" value="{esc(nxt)}">
    <div><label for="pw">Mot de passe</label>
    <input id="pw" type="password" name="password" required autofocus autocomplete="current-password"></div>
    <button type="submit" class="btn primary big">Se connecter</button>
  </form>
</main>"""
        self.send(status, page("Connexion", body))

    def login_submit(self):
        if not login_limiter.allow(self.client_ip()):
            return self.login_page("Trop de tentatives. Réessayez dans 15 minutes.", 429)
        pw = self.form.get("password", "")
        if not hmac.compare_digest(pw.encode(), ADMIN_PASSWORD.encode()):
            time.sleep(0.6)
            return self.login_page("Mot de passe incorrect.", 401)
        nxt = self.form.get("next") or "/admin"
        if not nxt.startswith("/admin") or nxt.startswith("/admin/login"):
            nxt = "/admin"
        secure = "; Secure" if self.base_url().startswith("https") else ""
        cookie = f"cm_session={make_session()}; Path=/admin; HttpOnly; SameSite=Lax; Max-Age={SESSION_TTL}{secure}"
        self.redirect(nxt, {"Set-Cookie": cookie})

    def logout(self):
        self.redirect("/admin/login", {"Set-Cookie": "cm_session=; Path=/admin; Max-Age=0; HttpOnly; SameSite=Lax"})

    # ---- back office : signalements -------------------------------------

    def dashboard(self):
        q = self.query
        f_status = q.get("statut", "ouverts")
        f_type = q.get("type", "")
        f_space = q.get("espace", "")
        f_text = (q.get("q") or "").strip()

        where, params = [], []
        if f_status == "ouverts":
            where.append(f"r.status IN ({','.join('?'*len(OPEN_STATUSES))})")
            params += OPEN_STATUSES
        elif f_status == "fermes":
            where.append(f"r.status IN ({','.join('?'*len(CLOSED_STATUSES))})")
            params += CLOSED_STATUSES
        elif f_status in STATUSES:
            where.append("r.status = ?")
            params.append(f_status)
        if f_type in TYPES:
            where.append("r.type = ?")
            params.append(f_type)
        if f_space.isdigit():
            where.append("r.space_id = ?")
            params.append(int(f_space))
        if f_text:
            where.append("(r.description LIKE ? OR r.contact LIKE ? OR s.name LIKE ? OR CAST(r.id AS TEXT) = ?)")
            like = f"%{f_text}%"
            params += [like, like, like, f_text.lstrip("#")]
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""

        with db() as conn:
            rows = conn.execute(
                f"""SELECT r.*, s.name AS space_name, s.zone AS space_zone
                    FROM reports r JOIN spaces s ON s.id = r.space_id {sql_where}
                    ORDER BY (r.status IN ('sans_solution','resolu')),
                             (r.type = 'danger') DESC, (r.status = 'nouveau') DESC, r.created_at DESC
                    LIMIT 500""",
                params,
            ).fetchall()
            counts = dict(conn.execute("SELECT status, COUNT(*) FROM reports GROUP BY status").fetchall())
            danger_open = conn.execute(
                f"SELECT COUNT(*) FROM reports WHERE type='danger' AND status IN ({','.join('?'*len(OPEN_STATUSES))})",
                OPEN_STATUSES,
            ).fetchone()[0]
            since = (now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
            solved_30 = conn.execute(
                "SELECT COUNT(*) FROM reports WHERE status='resolu' AND updated_at >= ?", (since,)
            ).fetchone()[0]
            spaces = conn.execute("SELECT id, name FROM spaces ORDER BY zone, name").fetchall()

        kpis = f"""
<div class="kpis">
  <a class="card kpi" href="/admin?statut=nouveau" style="text-decoration:none;color:inherit"><b>{counts.get('nouveau', 0)}</b><span class="muted small">Nouveaux à traiter</span></a>
  <a class="card kpi {'alert' if danger_open else ''}" href="/admin?statut=ouverts&type=danger" style="text-decoration:none;color:inherit"><b>{danger_open}</b><span class="muted small">Défauts dangereux ouverts</span></a>
  <a class="card kpi" href="/admin?statut=planifie" style="text-decoration:none;color:inherit"><b>{counts.get('planifie', 0)}</b><span class="muted small">Corrections planifiées</span></a>
  <a class="card kpi" href="/admin?statut=resolu" style="text-decoration:none;color:inherit"><b>{solved_30}</b><span class="muted small">Résolus (30 jours)</span></a>
</div>"""

        def opt(value, label, current):
            return f'<option value="{esc(value)}" {"selected" if str(current) == str(value) else ""}>{esc(label)}</option>'

        status_opts = opt("ouverts", "En cours (ouverts)", f_status) + opt("fermes", "Clôturés", f_status) + opt("tous", "Tous", f_status)
        status_opts += "".join(opt(k, v, f_status) for k, v in STATUSES.items())
        type_opts = opt("", "Tous les types", f_type) + "".join(opt(k, v[0], f_type) for k, v in TYPES.items())
        space_opts = opt("", "Tous les espaces", f_space) + "".join(opt(s["id"], s["name"], f_space) for s in spaces)

        filters = f"""
<form class="card filters" method="get" action="/admin">
  <div class="q"><label for="fq">Recherche</label><input id="fq" type="search" name="q" value="{esc(f_text)}" placeholder="Mot-clé, n° de signalement…"></div>
  <div><label for="fs">Statut</label><select id="fs" name="statut">{status_opts}</select></div>
  <div><label for="ft">Type</label><select id="ft" name="type">{type_opts}</select></div>
  <div><label for="fe">Espace</label><select id="fe" name="espace">{space_opts}</select></div>
  <div class="inline"><button class="btn primary" type="submit">Filtrer</button><a class="btn" href="/admin/export.csv?{esc(urlencode(q))}" title="Exporter la sélection">CSV</a></div>
</form>"""

        if rows:
            items = []
            for r in rows:
                planned = (
                    f'<span class="small muted">le {fmt_date(r["planned_date"])}</span>' if r["status"] == "planifie" and r["planned_date"] else ""
                )
                items.append(
                    f'<a class="card row {"is-danger" if r["type"] == "danger" and r["status"] in OPEN_STATUSES else ""}" href="/admin/r/{r["id"]}">'
                    f'<div><b>{esc(r["space_name"])}</b><div class="when">#{r["id"]} · {fmt_dt(r["created_at"])}</div></div>'
                    f'<div><div style="margin-bottom:4px">{type_badge(r["type"])}</div><div class="desc">{esc(r["description"])}</div></div>'
                    f'<div class="st">{status_badge(r["status"])}{late_badge(r)}{planned}</div></a>'
                )
            listing = f'<div class="list">{"".join(items)}</div>'
            if len(rows) == 500:
                listing += '<p class="small muted">Affichage limité aux 500 premiers résultats. Affinez les filtres.</p>'
        else:
            listing = '<div class="card empty"><div class="big-check">🎾</div><p>Aucun signalement pour ces critères.</p></div>'

        notice = '<div class="notice">Action enregistrée.</div>' if q.get("ok") else ""
        body = f'<main class="wrap stack"><h1>Signalements</h1>{notice}{kpis}{filters}<p class="small muted" style="margin-bottom:-4px">{len(rows)} résultat(s)</p>{listing}</main>'
        self.send(200, page("Signalements", body, admin_nav="signalements"))

    def report_detail(self, rid, error=None):
        with db() as conn:
            r = conn.execute(
                "SELECT r.*, s.name AS space_name, s.zone AS space_zone, s.code AS space_code "
                "FROM reports r JOIN spaces s ON s.id = r.space_id WHERE r.id = ?",
                (int(rid),),
            ).fetchone()
            if not r:
                return self.not_found("Signalement introuvable.")
            events = conn.execute("SELECT * FROM events WHERE report_id = ? ORDER BY id DESC", (r["id"],)).fetchall()

        timeline = "".join(
            f'<li><div>{status_badge(e["status"])}'
            + (f' <span class="small">pour le <b>{fmt_date(e["planned_date"])}</b></span>' if e["planned_date"] else "")
            + f'</div><div class="small muted">{fmt_dt(e["created_at"])}</div>'
            + (f'<div class="small" style="white-space:pre-wrap">{esc(e["note"])}</div>' if e["note"] else "")
            + "</li>"
            for e in events
        )
        planned = ""
        if r["status"] == "planifie" and r["planned_date"]:
            planned = f'<dt>Correction prévue</dt><dd><b>{fmt_date(r["planned_date"])}</b> {late_badge(r)}</dd>'
        contact = f'<dt>Contact</dt><dd>{esc(r["contact"])}</dd>' if r["contact"] else ""
        err = f'<div class="error" role="alert">{esc(error)}</div>' if error else ""
        default_date = parse_date(r["planned_date"]) if r["planned_date"] else today() + timedelta(days=1)
        is_closed = r["status"] in CLOSED_STATUSES

        reopen = (
            '<button class="btn sm" type="submit" name="action" value="rouvrir">Rouvrir le signalement</button>'
            if is_closed else ""
        )
        body = f"""
<main class="wrap stack">
  <p class="small no-print"><a href="/admin">← Tous les signalements</a></p>
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
    <h1 style="margin:0">Signalement #{r['id']}</h1>{type_badge(r['type'])}{status_badge(r['status'])}
  </div>
  {err}
  <div class="two">
    <section class="card stack">
      <dl class="meta-grid">
        <dt>Espace</dt><dd><b>{esc(r['space_name'])}</b>{(' · ' + esc(r['space_zone'])) if r['space_zone'] else ''}</dd>
        <dt>Signalé le</dt><dd>{fmt_dt(r['created_at'])}</dd>
        <dt>Dernière mise à jour</dt><dd>{fmt_dt(r['updated_at'])}</dd>
        {planned}{contact}
      </dl>
      <div><h2>Description</h2><p class="desc-full">{esc(r['description'])}</p></div>
    </section>
    <section class="card stack">
      <h2>Traiter</h2>
      <form method="post" action="/admin/r/{r['id']}/action" class="stack" data-once>
        {self.csrf_field()}
        <div><label for="note">Commentaire <span class="opt">(facultatif, visible dans l'historique)</span></label>
        <textarea id="note" name="note" maxlength="1000" style="min-height:70px" placeholder="Ex. : pièce commandée, intervention du prestataire…"></textarea></div>
        <div class="actions">
          <button class="btn s-vu" type="submit" name="action" value="vu">👀 Problème vu</button>
          <button class="btn s-sans_solution" type="submit" name="action" value="sans_solution">Pas de solution</button>
          <button class="btn s-resolu" type="submit" name="action" value="resolu">✓ Problème résolu</button>
        </div>
        <div class="plan">
          <label for="planned" style="margin:0">Correction planifiée le</label>
          <input id="planned" type="date" name="planned_date" value="{default_date.isoformat() if default_date else ''}">
          <button class="btn s-planifie" type="submit" name="action" value="planifie">📅 Planifier</button>
        </div>
        {reopen}
      </form>
    </section>
  </div>
  <section class="card stack"><h2>Historique</h2><ul class="timeline">{timeline}</ul></section>
</main>"""
        self.send(400 if error else 200, page(f"Signalement #{r['id']}", body, admin_nav="signalements"))

    def report_action(self, rid):
        action = self.form.get("action", "")
        note = (self.form.get("note") or "").strip()[:1000]
        target = {"vu": "vu", "planifie": "planifie", "sans_solution": "sans_solution", "resolu": "resolu", "rouvrir": "nouveau"}.get(action)
        if not target:
            return self.report_detail(rid, "Action inconnue.")
        planned = None
        if target == "planifie":
            d = parse_date(self.form.get("planned_date"))
            if not d:
                return self.report_detail(rid, "Indiquez une date valide pour planifier la correction.")
            planned = d.isoformat()
        if action == "rouvrir" and not note:
            note = "Signalement rouvert"
        ts = now_str()
        with db() as conn:
            exists = conn.execute("SELECT 1 FROM reports WHERE id = ?", (int(rid),)).fetchone()
            if not exists:
                return self.not_found("Signalement introuvable.")
            conn.execute(
                "UPDATE reports SET status = ?, planned_date = ?, updated_at = ? WHERE id = ?",
                (target, planned, ts, int(rid)),
            )
            conn.execute(
                "INSERT INTO events (report_id, status, planned_date, note, created_at) VALUES (?,?,?,?,?)",
                (int(rid), target, planned, note, ts),
            )
        self.redirect(f"/admin/r/{rid}")

    def export_csv(self):
        q = self.query
        f_status = q.get("statut", "tous")
        where, params = [], []
        if f_status == "ouverts":
            where.append(f"r.status IN ({','.join('?'*len(OPEN_STATUSES))})")
            params += OPEN_STATUSES
        elif f_status == "fermes":
            where.append(f"r.status IN ({','.join('?'*len(CLOSED_STATUSES))})")
            params += CLOSED_STATUSES
        elif f_status in STATUSES:
            where.append("r.status = ?")
            params.append(f_status)
        if q.get("type") in TYPES:
            where.append("r.type = ?")
            params.append(q["type"])
        if (q.get("espace") or "").isdigit():
            where.append("r.space_id = ?")
            params.append(int(q["espace"]))
        if (q.get("q") or "").strip():
            like = f"%{q['q'].strip()}%"
            where.append("(r.description LIKE ? OR r.contact LIKE ? OR s.name LIKE ?)")
            params += [like, like, like]
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with db() as conn:
            rows = conn.execute(
                f"SELECT r.*, s.name AS space_name, s.zone AS space_zone FROM reports r "
                f"JOIN spaces s ON s.id = r.space_id {sql_where} ORDER BY r.created_at DESC",
                params,
            ).fetchall()
        out = io.StringIO()
        w = csv.writer(out, delimiter=";")
        w.writerow(["N°", "Date du signalement", "Espace", "Zone", "Type", "Description", "Contact",
                    "Statut", "Correction prévue le", "Dernière mise à jour"])
        for r in rows:
            w.writerow([r["id"], fmt_dt(r["created_at"]), r["space_name"], r["space_zone"],
                        TYPES.get(r["type"], (r["type"],))[0], r["description"], r["contact"],
                        STATUSES.get(r["status"], r["status"]), fmt_date(r["planned_date"]), fmt_dt(r["updated_at"])])
        data = "﻿" + out.getvalue()  # BOM pour Excel
        fname = f"signalements-{today().isoformat()}.csv"
        self.send(200, data, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{fname}"'})

    # ---- back office : espaces ------------------------------------------

    def spaces_page(self, error=None, notice=None):
        with db() as conn:
            spaces = conn.execute(
                f"""SELECT s.*,
                       (SELECT COUNT(*) FROM reports r WHERE r.space_id = s.id
                          AND r.status IN ({','.join('?'*len(OPEN_STATUSES))})) AS open_count,
                       (SELECT COUNT(*) FROM reports r WHERE r.space_id = s.id) AS total_count
                    FROM spaces s ORDER BY s.active DESC, s.zone, s.name""",
                OPEN_STATUSES,
            ).fetchall()
        base = self.base_url()
        rows = []
        for s in spaces:
            url = f"{base}/s/{s['code']}"
            toggle_label = "Désactiver" if s["active"] else "Réactiver"
            delete_btn = (
                f'<button class="btn sm" name="op" value="delete" onclick="return confirm(\'Supprimer définitivement cet espace ?\')">Supprimer</button>'
                if s["total_count"] == 0 else ""
            )
            rows.append(f"""
<tr style="{'' if s['active'] else 'opacity:.55'}">
  <td>
    <form method="post" action="/admin/espaces/{s['id']}" class="inline">
      {self.csrf_field()}
      <input name="name" value="{esc(s['name'])}" required maxlength="80" aria-label="Nom de l'espace">
      <input name="zone" value="{esc(s['zone'])}" maxlength="40" placeholder="Zone" aria-label="Zone" class="hide-sm" style="max-width:150px">
      <button class="btn sm" name="op" value="rename">Enregistrer</button>
    </form>
  </td>
  <td><a href="/admin?statut=ouverts&espace={s['id']}">{s['open_count']}</a> <span class="muted small">/ {s['total_count']}</span></td>
  <td class="hide-sm"><a class="small" href="{esc(url)}" target="_blank" rel="noopener">/s/{esc(s['code'])}</a></td>
  <td>
    <form method="post" action="/admin/espaces/{s['id']}" class="inline">
      {self.csrf_field()}
      {'<a class="btn sm" href="/admin/qr?id=' + str(s['id']) + '">QR</a>' if s['active'] else ''}
      <button class="btn sm" name="op" value="toggle">{toggle_label}</button>
      {delete_btn}
    </form>
  </td>
</tr>""")
        err = f'<div class="error" role="alert">{esc(error)}</div>' if error else ""
        ok = f'<div class="notice">{esc(notice)}</div>' if notice else ""
        body = f"""
<main class="wrap stack">
  <h1>Espaces</h1>
  <p class="muted">Chaque espace possède son propre QR code. Renommer un espace ne change pas son QR code : les affiches déjà imprimées restent valables.</p>
  {err}{ok}
  <form method="post" action="/admin/espaces" class="card add">
    {self.csrf_field()}
    <div><label for="nn">Nouvel espace</label><input id="nn" name="name" required maxlength="80" placeholder="Ex. : Court de padel n°1"></div>
    <div><label for="nz">Zone <span class="opt">(facultatif)</span></label><input id="nz" name="zone" maxlength="40" placeholder="Ex. : Padel"></div>
    <button class="btn primary" type="submit">Ajouter</button>
  </form>
  <div class="card" style="overflow-x:auto">
    <table>
      <thead><tr><th>Nom · zone</th><th>Ouverts / total</th><th class="hide-sm">Lien du QR code</th><th>Actions</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
  </div>
  <p><a class="btn primary" href="/admin/qr">Imprimer tous les QR codes</a></p>
</main>"""
        self.send(400 if error else 200, page("Espaces", body, admin_nav="espaces"))

    def space_add(self):
        name = (self.form.get("name") or "").strip()[:80]
        zone = (self.form.get("zone") or "").strip()[:40]
        if not name:
            return self.spaces_page("Le nom de l'espace est obligatoire.")
        with db() as conn:
            conn.execute(
                "INSERT INTO spaces (code, name, zone, created_at) VALUES (?,?,?,?)",
                (new_space_code(), name, zone, now_str()),
            )
        self.redirect("/admin/espaces")

    def space_update(self, sid):
        op = self.form.get("op")
        with db() as conn:
            s = conn.execute("SELECT * FROM spaces WHERE id = ?", (int(sid),)).fetchone()
            if not s:
                return self.not_found("Espace introuvable.")
            if op == "rename":
                name = (self.form.get("name") or "").strip()[:80]
                if not name:
                    return self.spaces_page("Le nom de l'espace est obligatoire.")
                conn.execute("UPDATE spaces SET name = ?, zone = ? WHERE id = ?",
                             (name, (self.form.get("zone") or "").strip()[:40], s["id"]))
            elif op == "toggle":
                conn.execute("UPDATE spaces SET active = ? WHERE id = ?", (0 if s["active"] else 1, s["id"]))
            elif op == "delete":
                n = conn.execute("SELECT COUNT(*) FROM reports WHERE space_id = ?", (s["id"],)).fetchone()[0]
                if n:
                    return self.spaces_page("Impossible de supprimer un espace qui a des signalements : désactivez-le plutôt.")
                conn.execute("DELETE FROM spaces WHERE id = ?", (s["id"],))
        self.redirect("/admin/espaces")

    def qr_page(self):
        sid = self.query.get("id", "")
        with db() as conn:
            if sid.isdigit():
                spaces = conn.execute("SELECT * FROM spaces WHERE id = ? AND active = 1", (int(sid),)).fetchall()
            else:
                spaces = conn.execute("SELECT * FROM spaces WHERE active = 1 ORDER BY zone, name").fetchall()
        base = self.base_url()
        cards = "".join(
            f"""
<div class="card qr-card" data-url="{esc(base)}/s/{esc(s['code'])}" data-name="{esc(s['name'])}" data-slug="{esc(slugify(s['name']))}">
  <div class="club">{esc(CLUB_NAME)}</div>
  <h3>{esc(s['name'])}</h3>
  <div class="qr" role="img" aria-label="QR code de {esc(s['name'])}"></div>
  <div class="cta">Un problème ? Scannez-moi</div>
  <div class="small muted">Nettoyage · équipement · danger · autre</div>
  <div class="url">{esc(base)}/s/{esc(s['code'])}</div>
  <div class="no-print" style="margin-top:10px"><button class="btn sm dl" type="button">Télécharger en PNG</button></div>
</div>"""
            for s in spaces
        )
        local_warn = ""
        if any(h in base for h in ("localhost", "127.0.0.1", "0.0.0.0")):
            local_warn = (
                '<div class="error no-print">Attention : les QR codes pointent vers <b>' + esc(base) + "</b>, "
                "une adresse locale qui ne fonctionnera pas sur les téléphones des adhérents. "
                "Définissez la variable <code>BASE_URL</code> avec l'adresse publique du site avant d'imprimer.</div>"
            )
        empty = '<div class="card empty">Aucun espace actif.</div>' if not spaces else ""
        body = f"""
<main class="wrap stack">
  <div class="no-print stack">
    <h1>QR codes</h1>
    <p class="muted">Imprimez cette page (format A4, 4 affiches par page) et plastifiez chaque QR code avant de l'installer dans l'espace correspondant.</p>
    {local_warn}
    <p><button class="btn primary" type="button" onclick="window.print()">🖨️ Imprimer</button>
    {'<a class="btn" href="/admin/qr">Voir tous les espaces</a>' if sid else ''}</p>
  </div>
  {empty}
  <div class="qr-grid">{cards}</div>
</main>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcode-generator/1.4.4/qrcode.min.js"></script>
<script>
(function(){{
  function matrix(url){{
    var qr = qrcode(0, 'M'); qr.addData(url); qr.make();
    var n = qr.getModuleCount(), m = [];
    for (var r = 0; r < n; r++) {{ m.push([]); for (var c = 0; c < n; c++) m[r].push(qr.isDark(r, c)); }}
    return m;
  }}
  function svg(m){{
    var n = m.length, q = 4, size = n + 2*q, d = '';
    for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) if (m[r][c]) d += 'M' + (c+q) + ',' + (r+q) + 'h1v1h-1z';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' + size + ' ' + size + '" shape-rendering="crispEdges">'
      + '<rect width="' + size + '" height="' + size + '" fill="#fff"/><path d="' + d + '" fill="#000"/></svg>';
  }}
  function png(card, m){{
    var n = m.length, q = 4, cell = Math.floor(900 / (n + 2*q)), side = cell * (n + 2*q);
    var cv = document.createElement('canvas'); cv.width = side; cv.height = side + 120;
    var ctx = cv.getContext('2d');
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, cv.width, cv.height);
    ctx.fillStyle = '#000';
    for (var r = 0; r < n; r++) for (var c = 0; c < n; c++) if (m[r][c]) ctx.fillRect((c+q)*cell, (r+q)*cell, cell, cell);
    ctx.textAlign = 'center'; ctx.font = 'bold 44px system-ui, sans-serif';
    ctx.fillText(card.dataset.name, side/2, side + 50);
    ctx.font = '30px system-ui, sans-serif'; ctx.fillText('Un problème ? Scannez-moi', side/2, side + 98);
    cv.toBlob(function(b){{
      var a = document.createElement('a'); a.href = URL.createObjectURL(b);
      a.download = 'qr-' + card.dataset.slug + '.png'; document.body.appendChild(a); a.click();
      setTimeout(function(){{ URL.revokeObjectURL(a.href); a.remove(); }}, 1000);
    }});
  }}
  document.querySelectorAll('.qr-card').forEach(function(card){{
    var box = card.querySelector('.qr');
    if (typeof qrcode === 'undefined') {{
      box.innerHTML = '<p class="small error">Générateur de QR code indisponible (connexion internet requise).</p>';
      return;
    }}
    var m = matrix(card.dataset.url);
    box.innerHTML = svg(m);
    card.querySelector('.dl').addEventListener('click', function(){{ png(card, m); }});
  }});
}})();
</script>"""
        self.send(200, page("QR codes", body, admin_nav="qr"))

    def healthz(self):
        self.send(200, "ok", "text/plain; charset=utf-8")


# (méthode, motif d'URL, fonction, réservé à l'équipe ?)
ROUTES = [
    ("GET", r"/", Handler.home, False),
    ("GET", r"/healthz", Handler.healthz, False),
    ("GET", r"/s/([A-Za-z0-9]{4,32})", Handler.report_form, False),
    ("POST", r"/s/([A-Za-z0-9]{4,32})", Handler.report_submit, False),
    ("GET", r"/s/([A-Za-z0-9]{4,32})/merci", Handler.report_thanks, False),
    ("GET", r"/admin/login", Handler.login_page, False),
    ("POST", r"/admin/login", Handler.login_submit, False),
    ("GET", r"/admin/logout", Handler.logout, False),
    ("GET", r"/admin", Handler.dashboard, True),
    ("GET", r"/admin/r/(\d+)", Handler.report_detail, True),
    ("POST", r"/admin/r/(\d+)/action", Handler.report_action, True),
    ("GET", r"/admin/export\.csv", Handler.export_csv, True),
    ("GET", r"/admin/espaces", Handler.spaces_page, True),
    ("POST", r"/admin/espaces", Handler.space_add, True),
    ("POST", r"/admin/espaces/(\d+)", Handler.space_update, True),
    ("GET", r"/admin/qr", Handler.qr_page, True),
]


def main():
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"✔ Plateforme maintenance « {CLUB_NAME} » démarrée sur http://{HOST}:{PORT}")
    print(f"  Back office : http://localhost:{PORT}/admin")
    if not os.environ.get("ADMIN_PASSWORD"):
        print(f"  Mot de passe généré (fichier {os.path.join(DATA_DIR, 'admin_password.txt')}) : {ADMIN_PASSWORD}")
    if not BASE_URL:
        print("  Astuce : définissez BASE_URL (adresse publique) avant d'imprimer les QR codes.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArrêt.")
        server.server_close()


if __name__ == "__main__":
    main()
