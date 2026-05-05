import sqlite3, requests, json, re, os, logging, time, threading
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, jsonify, session
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

try:
    import psycopg2, psycopg2.extras
    _PSYCOPG2_OK = True
except ImportError:
    _PSYCOPG2_OK = False
    log.warning("psycopg2 not available, using SQLite")

app = Flask(__name__)
app.secret_key   = os.environ.get("SECRET_KEY", "alza-tracker-secret-change-me")
DATABASE_URL     = os.environ.get("DATABASE_URL", "")
_SQLITE_PATH     = os.environ.get("DB_PATH", "tracker.db")
_USE_PG          = bool(DATABASE_URL) and _PSYCOPG2_OK
_DBError         = (psycopg2.IntegrityError if _USE_PG else sqlite3.IntegrityError)

log.info("DB backend: %s", "PostgreSQL/Supabase" if _USE_PG else "SQLite")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCRAPE_DO_TOKEN  = os.environ.get("SCRAPE_DO_TOKEN", "")
CHECK_HOUR       = int(os.environ.get("CHECK_HOUR", "9"))
APP_PIN          = os.environ.get("APP_PIN", "")  # pokud prazdne, login se nezobrazuje

# ── Auth ────────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if APP_PIN and not session.get("auth"):
            return redirect(url_for("login_page", next=request.path))
        return f(*args, **kwargs)
    return decorated

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not APP_PIN:
        return redirect(url_for("index"))
    error = False
    if request.method == "POST":
        if request.form.get("pin", "").strip() == APP_PIN:
            session["auth"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("index"))
        error = True
        time.sleep(0.8)

    err_html = '<p class="pin-err">Spatny PIN, zkus znovu.</p>' if error else ''
    shake_style = ' style="animation:shake .35s"' if error else ''

    return (
        '<!DOCTYPE html><html id="R" lang="cs"><head>'
        '<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>AlzaTracker - Prihlaseni</title>'
        '<link rel="preconnect" href="https://fonts.googleapis.com">'
        '<link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700'
        '&family=DM+Mono&display=swap" rel="stylesheet">'
        '<style>' + LOGIN_CSS + '</style>'
        '<script>'
        '(function(){var d=localStorage.getItem("alza-theme");'
        'if(d==="dark")document.getElementById("R").classList.add("dark")})();'
        'function T(){var r=document.getElementById("R");var dark=r.classList.toggle("dark");'
        'localStorage.setItem("alza-theme",dark?"dark":"light");'
        'document.getElementById("ti").textContent=dark?"sun":"moon";}'
        '</script>'
        '</head><body>'
        '<button class="tbtn" onclick="T()" title="Prepnout tema"><span id="ti">moon</span></button>'
        '<div class="card">'
        '<div class="icon">&#x1F512;</div>'
        '<h1>AlzaTracker</h1>'
        '<p class="sub">Zadej PIN pro pristup</p>'
        '<form method="post">'
        '<input type="password" name="pin" inputmode="numeric" placeholder="&bull;&bull;&bull;&bull;&bull;&bull;" autofocus' + shake_style + '>'
        + err_html +
        '<button type="submit">Vstoupit &rarr;</button>'
        '</form></div>'
        '<script>document.getElementById("ti").textContent='
        'document.getElementById("R").classList.contains("dark")?"&#x2600;":"&#x1F319;";</script>'
        '</body></html>'
    )

@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login_page") if APP_PIN else url_for("index"))

# ── DB ─────────────────────────────────────────────────────────────────────────


# ── Database abstraction (PostgreSQL via Supabase OR SQLite fallback) ──────────

class _Db:
    """Unified wrapper — same interface for both PostgreSQL and SQLite."""

    def __init__(self):
        if _USE_PG:
            # Build connection — avoid duplicate sslmode if URL already contains it
            dsn = DATABASE_URL
            if "sslmode" not in dsn:
                dsn = dsn + ("&" if "?" in dsn else "?") + "sslmode=require"
            self._conn = psycopg2.connect(dsn)
        else:
            self._conn = sqlite3.connect(_SQLITE_PATH)
            self._conn.row_factory = sqlite3.Row

    def execute(self, sql, params=None):
        if _USE_PG:
            cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # SQLite uses ? placeholders, PostgreSQL uses %s
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self._conn.execute(sql, params or ())

    def commit(self):   self._conn.commit()
    def close(self):
        try: self._conn.close()
        except Exception: pass

    def __enter__(self):  return self
    def __exit__(self, exc_type, *_):
        if exc_type: 
            try: self._conn.rollback()
            except Exception: pass
        else:
            self._conn.commit()
        self.close()

def get_db():
    return _Db()

def init_db():
    if not _USE_PG:
        d = os.path.dirname(_SQLITE_PATH)
        if d: os.makedirs(d, exist_ok=True)
    with get_db() as c:
        if _USE_PG:
            c.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id       SERIAL PRIMARY KEY,
                    url      TEXT UNIQUE NOT NULL,
                    name     TEXT,
                    target   FLOAT,
                    active   INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT NOW()
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id         SERIAL PRIMARY KEY,
                    product_id INTEGER NOT NULL,
                    price      FLOAT,
                    alza_days  INTEGER DEFAULT 0,
                    coupon     TEXT,
                    checked_at TIMESTAMP DEFAULT NOW(),
                    FOREIGN KEY(product_id) REFERENCES products(id)
                )
            """)
        else:
            c.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    url      TEXT UNIQUE NOT NULL,
                    name     TEXT,
                    target   REAL,
                    active   INTEGER DEFAULT 1,
                    added_at TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_id INTEGER NOT NULL,
                    price      REAL,
                    alza_days  INTEGER DEFAULT 0,
                    coupon     TEXT,
                    checked_at TEXT DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY(product_id) REFERENCES products(id)
                )
            """)
    log.info("DB ready (%s)", "PostgreSQL/Supabase" if _USE_PG else "SQLite")

# ── Scraper ─────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_url(url):
    if SCRAPE_DO_TOKEN:
        proxy = "https://api.scrape.do?token={}&url={}&geoCode=cz".format(
            SCRAPE_DO_TOKEN, requests.utils.quote(url))
        return requests.get(proxy, timeout=30)
    return requests.get(url, headers=HEADERS, timeout=20)

def scrape(url):
    try:
        r = fetch_url(url)
        r.raise_for_status()
    except Exception as e:
        log.error("Fetch failed %s: %s", url, e)
        return None
    soup = BeautifulSoup(r.text, "lxml")
    name, price = None, None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string or "")
            offers = d.get("offers", {})
            if isinstance(offers, list): offers = offers[0]
            raw = offers.get("price")
            if raw:
                price = float(str(raw).replace(",", ".").replace("\xa0", "").strip())
                name = d.get("name", "")[:120]
                break
        except Exception:
            continue
    if not price:
        meta = soup.find("meta", itemprop="price")
        if meta:
            try: price = float(meta["content"].replace(",", "."))
            except Exception: pass
    if not price:
        for cls in ["price-box__price", "js-product-price", "pricebox__price"]:
            el = soup.find(class_=cls)
            if el:
                nums = re.findall(r"\d+", el.get_text().replace("\xa0","").replace(" ",""))
                for n in nums:
                    v = float(n)
                    if v > 10: price = v; break
            if price: break
    if not name:
        h1 = soup.find("h1")
        if h1: name = h1.get_text(strip=True)[:120]
    page = r.text.lower()
    alza_days = any(k in page for k in ["alzadny", "alza dny", "alza-dny"])
    coupon = None
    for cls in ["coupon", "voucher", "kupon", "promo-code"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            t = el.get_text(strip=True)[:60]
            if t: coupon = t; break
    return {"name": name, "price": price, "alza_days": alza_days, "coupon": coupon}

# ── Telegram ────────────────────────────────────────────────────────────────────

def tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        r = requests.post(
            "https://api.telegram.org/bot{}/sendMessage".format(TELEGRAM_TOKEN),
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error("Telegram: %s", e); return False

def tg_summary(pid):
    c = get_db()
    p    = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p: c.close(); return
    lat  = c.execute("SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1", (pid,)).fetchone()
    minp = c.execute("SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL", (pid,)).fetchone()
    chks = c.execute("SELECT COUNT(*) as n FROM history WHERE product_id=?", (pid,)).fetchone()
    c.close()
    name  = p["name"] or "Produkt"
    price = lat["price"] if lat and lat["price"] else None
    lines = ["<b>{}</b>".format(name)]
    if price:
        lines.append("\n Aktualni cena: <b>{:,.0f} Kc</b>".format(price).replace(",", "\u00a0"))
    if minp and minp["m"]:
        lines.append("Historicke minimum: <b>{:,.0f} Kc</b>".format(minp["m"]).replace(",", "\u00a0"))
    if p["target"]:
        lines.append("Cilova cena: <b>{:,.0f} Kc</b>".format(p["target"]).replace(",", "\u00a0"))
        if price:
            diff = price - p["target"]
            if diff <= 0: lines.append("Cilova cena <b>dosazena!</b>")
            else: lines.append("Chybi: <b>{:,.0f} Kc</b>".format(diff).replace(",", "\u00a0"))
    if lat and lat["alza_days"]: lines.append("<b>AlzaDny jsou aktivni!</b>")
    if lat and lat["coupon"]:    lines.append("Kupon: <code>{}</code>".format(lat["coupon"]))
    lines.append("\nKontrol provedeno: {}".format(chks["n"]))
    lines.append("<a href='{}'>Otevrit na Alze</a>".format(p["url"]))
    tg("\n".join(lines))

# ── Check ────────────────────────────────────────────────────────────────────────

def check_product(pid):
    c = get_db()
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p or not p["active"]: c.close(); return
    result = scrape(p["url"])
    if not result: c.close(); return
    price = result["price"]
    name  = result["name"] or p["name"] or "Produkt"
    if result["name"] and result["name"] != p["name"]:
        c.execute("UPDATE products SET name=? WHERE id=?", (result["name"], pid))
    c.execute("INSERT INTO history (product_id,price,alza_days,coupon) VALUES (?,?,?,?)",
              (pid, price, 1 if result["alza_days"] else 0, result["coupon"]))
    c.commit()
    prev = c.execute("SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1", (pid,)).fetchone()
    c.close()
    if price is None: return
    if prev and prev["price"] and price < prev["price"]:
        diff = prev["price"] - price
        tg("Pokles ceny!\n{}\n\n<b>{:,.0f} Kc</b> (pokles {:,.0f} Kc)\n{}".format(
            name, price, diff, p["url"]).replace(",", "\u00a0"))
    if p["target"] and price <= p["target"]:
        tg("Cilova cena dosazena!\n{}\n\n<b>{:,.0f} Kc</b>\n{}".format(
            name, price, p["url"]).replace(",", "\u00a0"))
    if result["alza_days"]:
        tg("AlzaDny jsou aktivni!\n{}\n\n{:,.0f} Kc\n{}".format(
            name, price, p["url"]).replace(",", "\u00a0"))
    if result["coupon"]:
        tg("Kupon: <code>{}</code>\n{}\n{:,.0f} Kc\n{}".format(
            result["coupon"], name, price, p["url"]).replace(",", "\u00a0"))
    log.info("[%s] %.0f Kc alza_days=%s", name, price, result["alza_days"])

def check_all():
    c = get_db()
    ids = [r["id"] for r in c.execute("SELECT id FROM products WHERE active=1").fetchall()]
    c.close()
    log.info("Daily check - %d products", len(ids))
    for pid in ids:
        check_product(pid)
        time.sleep(4)

# ── Helpers ──────────────────────────────────────────────────────────────────────

def fmt(n):
    if n is None: return "&#8212;"
    return "{:,.0f}".format(n).replace(",", "\u00a0") + "\u00a0K&ccedil;"

def logout_btn_html():
    if not APP_PIN: return ""
    return '<form method="post" action="/logout"><button class="btn btn-ghost btn-sm">Odhlasit</button></form>'

# ── CSS ────────────────────────────────────────────────────────────────────────────

LOGIN_CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f2f2f7;--surf:#fff;--text:#1c1c1e;--muted:#8e8e93;
  --acc:#007aff;--red:#ff3b30;--brd:rgba(0,0,0,.08);--sh:0 4px 28px rgba(0,0,0,.08)}
html.dark{--bg:#0d0d0f;--surf:#1c1c1e;--text:#f5f5f7;--muted:#636366;
  --acc:#0a84ff;--brd:rgba(255,255,255,.08);--sh:0 4px 28px rgba(0,0,0,.4)}
@keyframes shake{0%,100%{transform:translateX(0)}25%{transform:translateX(-8px)}75%{transform:translateX(8px)}}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  transition:background .2s}
.card{background:var(--surf);border:1px solid var(--brd);border-radius:20px;
  box-shadow:var(--sh);padding:44px 40px;text-align:center;width:100%;max-width:360px}
.icon{font-size:40px;margin-bottom:18px}
h1{font-size:22px;font-weight:700;letter-spacing:-.4px;margin-bottom:6px}
.sub{color:var(--muted);font-size:14px;margin-bottom:28px}
input{width:100%;padding:13px 16px;border:1.5px solid var(--brd);border-radius:12px;
  font-family:'DM Mono',monospace;font-size:22px;letter-spacing:8px;text-align:center;
  background:var(--bg);color:var(--text);outline:none;margin-bottom:8px;
  transition:border .15s,background .15s}
input:focus{border-color:var(--acc);background:var(--surf)}
.pin-err{color:var(--red);font-size:13px;margin-bottom:10px}
button[type=submit]{width:100%;padding:13px;border:none;border-radius:12px;
  background:var(--acc);color:#fff;font-family:'DM Sans',sans-serif;
  font-size:15px;font-weight:600;cursor:pointer;margin-top:4px;
  transition:opacity .15s,transform .1s}
button[type=submit]:hover{opacity:.88;transform:translateY(-1px)}
.tbtn{position:fixed;top:14px;right:14px;width:36px;height:36px;border-radius:50%;
  border:1px solid var(--brd);background:var(--surf);font-size:18px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;transition:all .2s}
.tbtn:hover{transform:rotate(15deg)}
"""

CSS = """
@import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg:#f2f2f7;--surf:#fff;--surf2:#f7f7fa;
  --brd:rgba(0,0,0,.08);--brd2:rgba(0,0,0,.04);
  --text:#1c1c1e;--text2:#3a3a3c;--muted:#8e8e93;
  --acc:#007aff;--acc2:#0062cc;
  --red:#ff3b30;
  --gt:#1a7f3c;--gb:rgba(52,199,89,.1);
  --at:#7d4400;--ab:rgba(255,149,0,.12);
  --bb:rgba(0,122,255,.1);
  --r:14px;--rs:10px;--rxs:8px;
  --sh:0 1px 3px rgba(0,0,0,.06),0 4px 16px rgba(0,0,0,.04);
  --sh2:0 1px 2px rgba(0,0,0,.04);
  --nav:rgba(255,255,255,.88);
}
html.dark{
  --bg:#0d0d0f;--surf:#1c1c1e;--surf2:#2c2c2e;
  --brd:rgba(255,255,255,.08);--brd2:rgba(255,255,255,.04);
  --text:#f5f5f7;--text2:#aeaeb2;--muted:#636366;
  --acc:#0a84ff;--acc2:#409cff;
  --red:#ff453a;
  --gt:#30d158;--gb:rgba(48,209,88,.12);
  --at:#ff9f0a;--ab:rgba(255,159,10,.12);
  --bb:rgba(10,132,255,.12);
  --sh:0 1px 3px rgba(0,0,0,.3),0 4px 16px rgba(0,0,0,.2);
  --sh2:0 1px 2px rgba(0,0,0,.2);
  --nav:rgba(28,28,30,.88);
}

body{font-family:'DM Sans',-apple-system,sans-serif;background:var(--bg);color:var(--text);
  font-size:15px;line-height:1.5;-webkit-font-smoothing:antialiased;
  transition:background .2s,color .2s}
a{color:inherit;text-decoration:none}

.nav{background:var(--nav);backdrop-filter:saturate(180%) blur(20px);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid var(--brd);height:52px;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;position:sticky;top:0;z-index:100;transition:background .2s}
.nav-logo{font-size:17px;font-weight:700;letter-spacing:-.4px}
.nav-logo span{color:var(--acc)}
.nav-right{display:flex;gap:8px;align-items:center}
.tbtn{width:34px;height:34px;padding:0;border-radius:50%;
  border:1px solid var(--brd);background:var(--surf);color:var(--text);
  font-size:16px;cursor:pointer;display:inline-flex;align-items:center;
  justify-content:center;transition:all .2s}
.tbtn:hover{background:var(--surf2);transform:rotate(15deg)}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:8px 16px;border-radius:var(--rxs);border:none;
  font-family:inherit;font-size:14px;font-weight:500;cursor:pointer;
  transition:all .15s;white-space:nowrap}
.btn-primary{background:var(--acc);color:#fff}
.btn-primary:hover{background:var(--acc2)}
.btn-secondary{background:var(--surf);color:var(--text);
  border:1px solid var(--brd);box-shadow:var(--sh2)}
.btn-secondary:hover{background:var(--surf2)}
.btn-ghost{background:transparent;color:var(--muted);padding:6px 10px;
  border:1px solid transparent}
.btn-ghost:hover{background:var(--brd2);color:var(--text)}
.btn-danger{background:transparent;color:var(--red);padding:6px 10px;
  border:1px solid transparent}
.btn-danger:hover{background:rgba(255,59,48,.1)}
.btn-sm{padding:6px 12px;font-size:13px}
.btn-tg{background:#229ED9;color:#fff;border:none}
.btn-tg:hover{background:#1a8ab8}

.main{max-width:860px;margin:0 auto;padding:28px 20px}
.banner{display:flex;align-items:center;gap:10px;padding:12px 16px;
  border-radius:var(--rs);margin-bottom:20px;font-size:13px;font-weight:500;
  border:1px solid transparent}
.bwarn{background:var(--ab);color:var(--at);border-color:rgba(255,149,0,.2)}
.bok{background:var(--gb);color:var(--gt);border-color:rgba(52,199,89,.2)}

.add-card{background:var(--surf);border-radius:var(--r);border:1px solid var(--brd);
  box-shadow:var(--sh);padding:22px 24px;margin-bottom:28px}
.add-label{font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px}
.form-row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:6px;flex:1;min-width:180px}
.fgl{flex:3;min-width:260px}
.fg label{font-size:12px;font-weight:500;color:var(--text2)}
.fg input{padding:10px 13px;border:1.5px solid var(--brd);border-radius:var(--rxs);
  font-family:inherit;font-size:14px;background:var(--surf2);color:var(--text);
  outline:none;transition:border .15s,background .15s}
.fg input:focus{border-color:var(--acc);background:var(--surf)}
.fg input::placeholder{color:var(--muted)}

.sec{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.sec-title{font-size:12px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em}

.card{background:var(--surf);border-radius:var(--r);border:1px solid var(--brd);
  box-shadow:var(--sh);margin-bottom:12px;overflow:hidden;
  transition:box-shadow .2s,background .2s}
.card:hover{box-shadow:0 2px 8px rgba(0,0,0,.1),0 8px 24px rgba(0,0,0,.08)}
.card.paused{opacity:.5}
.card-body{display:flex;align-items:center;gap:14px;padding:16px 20px;flex-wrap:wrap}
.card-info{flex:1;min-width:0}
.card-name{font-size:15px;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;letter-spacing:-.2px}
.card-url{font-size:12px;color:var(--muted);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-url a{color:var(--acc)}
.badges{display:flex;gap:6px;flex-wrap:wrap;min-width:130px}
.badge{display:inline-flex;align-items:center;gap:3px;
  font-size:11px;font-weight:600;padding:4px 9px;border-radius:20px}
.bf{background:var(--ab);color:var(--at)}
.bt{background:var(--gb);color:var(--gt)}
.bco{background:var(--bb);color:var(--acc)}
.bp{background:var(--surf2);color:var(--muted)}
.card-price{text-align:right;min-width:120px}
.price-main{font-size:22px;font-weight:700;letter-spacing:-.5px;
  line-height:1.2;font-family:'DM Mono',monospace}
.price-delta{font-size:12px;font-weight:500;margin-top:3px}
.dd{color:var(--gt)}.du{color:var(--red)}.dn2{color:var(--muted)}
.card-actions{display:flex;gap:4px}
.card-foot{background:var(--surf2);border-top:1px solid var(--brd2);
  padding:10px 20px;display:flex;justify-content:space-between;
  font-size:12px;color:var(--muted);transition:background .2s}
.foot-l{display:flex;gap:16px}
.empty{text-align:center;padding:64px 20px;color:var(--muted)}
.empty-icon{font-size:44px;margin-bottom:14px}
.empty h3{font-size:17px;font-weight:600;color:var(--text);margin-bottom:6px}

.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));
  gap:10px;margin-bottom:22px}
.sc{background:var(--surf);border-radius:var(--rs);border:1px solid var(--brd);
  box-shadow:var(--sh);padding:14px 16px}
.sl{font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.sv{font-size:20px;font-weight:700;letter-spacing:-.3px;font-family:'DM Mono',monospace}
.svg{color:var(--gt)}.svr{color:var(--red)}
.chart-card{background:var(--surf);border-radius:var(--r);border:1px solid var(--brd);
  box-shadow:var(--sh);padding:22px;margin-bottom:22px}
.chart-card h2{font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:18px}
.chart-wrap{height:260px;position:relative}
.table-card{background:var(--surf);border-radius:var(--r);border:1px solid var(--brd);
  box-shadow:var(--sh);overflow:hidden}
.table-card h2{font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;padding:16px 20px;
  border-bottom:1px solid var(--brd)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 18px;font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.05em;background:var(--surf2);
  border-bottom:1px solid var(--brd)}
td{padding:11px 18px;border-bottom:1px solid var(--brd2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surf2)}
.tdn{color:var(--gt);font-weight:600}
.tup{color:var(--red);font-weight:600}
.tneu{color:var(--muted)}
"""

THEME_SCRIPT = """
<script>
(function(){
  var r = document.getElementById('R');
  if (localStorage.getItem('alza-theme') === 'dark') r.classList.add('dark');
  var btns = document.querySelectorAll('.ti');
  var dark = r.classList.contains('dark');
  for (var i=0;i<btns.length;i++) btns[i].textContent = dark ? '\u2600\ufe0f' : '\U0001F319';
})();
function toggleTheme() {
  var r = document.getElementById('R');
  var dark = r.classList.toggle('dark');
  localStorage.setItem('alza-theme', dark ? 'dark' : 'light');
  var btns = document.querySelectorAll('.ti');
  for (var i=0;i<btns.length;i++) btns[i].textContent = dark ? '\u2600\ufe0f' : '\U0001F319';
}
</script>
"""

THEME_INIT_SCRIPT = """
<script>
(function(){
  if (localStorage.getItem('alza-theme') === 'dark')
    document.getElementById('R').classList.add('dark');
})();
</script>
"""

THEME_BTN_HTML = '<button class="tbtn" onclick="toggleTheme()" title="Prepnout tema"><span class="ti">&#x1F319;</span></button>'

# ── Index ──────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    c = get_db()
    products = c.execute("SELECT * FROM products ORDER BY added_at DESC").fetchall()
    rows = []
    for p in products:
        lat  = c.execute("SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1", (p["id"],)).fetchone()
        prev = c.execute("SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1", (p["id"],)).fetchone()
        low  = c.execute("SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL", (p["id"],)).fetchone()
        chks = c.execute("SELECT COUNT(*) as n FROM history WHERE product_id=?", (p["id"],)).fetchone()
        chg  = None
        if lat and lat["price"] and prev and prev["price"]:
            chg = lat["price"] - prev["price"]
        rows.append(dict(p=dict(p), lat=dict(lat) if lat else None,
                         low=low["m"], chg=chg, chks=chks["n"]))
    c.close()
    tg_ok = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    if tg_ok:
        banner = '<div class="banner bok">&#x2713; Telegram aktivni &middot; automaticka kontrola kazdy den v 9:00 UTC</div>'
    else:
        banner = '<div class="banner bwarn">&#x26A0; Telegram neni nastaven &mdash; pridej TELEGRAM_TOKEN a TELEGRAM_CHAT_ID v Render dashboardu.</div>'

    cards_html = ""
    if not rows:
        cards_html = '<div class="empty"><div class="empty-icon">&#x1F4E6;</div><h3>Zadne produkty</h3><p>Pridej prvni produkt vys.</p></div>'

    for item in rows:
        p   = item["p"]
        lat = item["lat"]
        if lat and lat.get("price"):
            ph = '<div class="price-main">{}</div>'.format(fmt(lat["price"]))
            if item["chg"] is not None:
                if item["chg"] < 0:
                    ph += '<div class="price-delta dd">&#x2193; {}</div>'.format(fmt(abs(item["chg"])))
                elif item["chg"] > 0:
                    ph += '<div class="price-delta du">&#x2191; {}</div>'.format(fmt(item["chg"]))
                else:
                    ph += '<div class="price-delta dn2">beze zmeny</div>'
            else:
                ph += '<div class="price-delta dn2">prvni zaznam</div>'
        else:
            ph = '<div style="font-size:14px;color:var(--muted);font-weight:500">Nacitam&hellip;</div>'

        badges = ""
        if not p["active"]:
            badges += '<span class="badge bp">Pozastaveno</span>'
        if lat and lat.get("alza_days"):
            badges += '<span class="badge bf">&#x1F525; AlzaDny</span>'
        if lat and lat.get("coupon"):
            badges += '<span class="badge bco">&#x1F3AB; {}</span>'.format(lat["coupon"][:18])
        if p["target"]:
            badges += '<span class="badge bt">Cil {}</span>'.format(fmt(p["target"]))

        fl = ""
        if item["low"]:
            fl += '<span>Min&nbsp;<b>{}</b></span>'.format(fmt(item["low"]))
        fl += '<span>{}&nbsp;kontrol</span>'.format(item["chks"])
        fr  = lat["checked_at"] if lat else "zatim nekontrolovano"
        nd  = p["name"] or "Nacitam&hellip;"
        us  = p["url"][:65] + ("&hellip;" if len(p["url"]) > 65 else "")
        cls = "card paused" if not p["active"] else "card"
        tog = "&#x23F8;" if p["active"] else "&#x25B6;"

        cards_html += (
            '<div class="{cls}">'
            '<div class="card-body">'
            '<div class="card-info">'
            '<div class="card-name">{nd}</div>'
            '<div class="card-url"><a href="{url}" target="_blank">{us}</a></div>'
            '</div>'
            '<div class="badges">{badges}</div>'
            '<div class="card-price">{ph}</div>'
            '<div class="card-actions">'
            '<a href="/product/{pid}" class="btn btn-secondary btn-sm">&#x1F4C8; Historie</a>'
            '<form method="post" action="/notify/{pid}">'
            '<button class="btn btn-tg btn-sm">&#x2708; TG</button></form>'
            '<form method="post" action="/check/{pid}">'
            '<button class="btn btn-ghost btn-sm" title="Zkontrolovat ted">&#x21BB;</button></form>'
            '<form method="post" action="/toggle/{pid}">'
            '<button class="btn btn-ghost btn-sm">{tog}</button></form>'
            '<form method="post" action="/delete/{pid}"'
            ' onsubmit="return confirm(\'Smazat produkt a historii?\')">'
            '<button class="btn btn-danger btn-sm">&#x2715;</button></form>'
            '</div></div>'
            '<div class="card-foot">'
            '<div class="foot-l">{fl}</div>'
            '<span>{fr}</span>'
            '</div></div>'
        ).format(cls=cls, nd=nd, url=p["url"], us=us, badges=badges,
                 ph=ph, pid=p["id"], tog=tog, fl=fl, fr=fr)

    page = (
        '<!DOCTYPE html>'
        '<html id="R" lang="cs"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>AlzaTracker</title>'
        + THEME_INIT_SCRIPT +
        '<style>' + CSS + '</style>'
        '</head><body>'
        '<nav class="nav">'
        '<div class="nav-logo">Alza<span>Tracker</span></div>'
        '<div class="nav-right">'
        '<form method="post" action="/check_all">'
        '<button class="btn btn-secondary btn-sm">&#x21BB; Zkontrolovat vse</button>'
        '</form>'
        + THEME_BTN_HTML +
        logout_btn_html() +
        '</div></nav>'
        '<div class="main">'
        + banner +
        '<div class="add-card">'
        '<div class="add-label">Pridat produkt</div>'
        '<form method="post" action="/add">'
        '<div class="form-row">'
        '<div class="fg fgl">'
        '<label>URL produktu na Alze</label>'
        '<input type="url" name="url" placeholder="https://www.alza.cz/nazev-produktu.htm" required>'
        '</div>'
        '<div class="fg">'
        '<label>Cilova cena (Kc) &mdash; nepovinne</label>'
        '<input type="number" name="target" placeholder="napr. 15000" min="0" step="1">'
        '</div>'
        '<button class="btn btn-primary" type="submit">Pridat</button>'
        '</div></form></div>'
        '<div class="sec">'
        '<span class="sec-title">Sledovane produkty</span>'
        '<span style="font-size:13px;color:var(--muted)">' + str(len(rows)) + ' celkem</span>'
        '</div>'
        + cards_html +
        '</div>'
        + THEME_SCRIPT +
        '</body></html>'
    )

    return page

# ── Routes ──────────────────────────────────────────────────────────────────────

@app.route("/add", methods=["POST"])
@login_required
def add():
    url = request.form.get("url", "").strip()
    target = request.form.get("target", "").strip()
    if not url: return redirect(url_for("index"))
    target_val = float(target) if target else None
    c = get_db()
    try:
        c.execute("INSERT INTO products (url,target) VALUES (?,?)", (url, target_val))
        c.commit()
        pid = c.execute("SELECT id FROM products WHERE url=?", (url,)).fetchone()["id"]
        c.close()
        threading.Thread(target=check_product, args=(pid,), daemon=True).start()
    except _DBError:
        c.close()
    return redirect(url_for("index"))

@app.route("/delete/<int:pid>", methods=["POST"])
@login_required
def delete(pid):
    c = get_db()
    c.execute("DELETE FROM history WHERE product_id=?", (pid,))
    c.execute("DELETE FROM products WHERE id=?", (pid,))
    c.commit(); c.close()
    return redirect(url_for("index"))

@app.route("/toggle/<int:pid>", methods=["POST"])
@login_required
def toggle(pid):
    c = get_db()
    c.execute("UPDATE products SET active = 1 - active WHERE id=?", (pid,))
    c.commit(); c.close()
    return redirect(url_for("index"))

@app.route("/check/<int:pid>", methods=["POST"])
@login_required
def check_now(pid):
    threading.Thread(target=check_product, args=(pid,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/check_all", methods=["POST"])
@login_required
def check_all_route():
    threading.Thread(target=check_all, daemon=True).start()
    return redirect(url_for("index"))

@app.route("/notify/<int:pid>", methods=["POST"])
@login_required
def notify(pid):
    threading.Thread(target=tg_summary, args=(pid,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/product/<int:pid>")
@login_required
def detail(pid):
    c = get_db()
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p: return redirect(url_for("index"))
    hist = [dict(r) for r in c.execute(
        "SELECT * FROM history WHERE product_id=? ORDER BY checked_at ASC", (pid,)).fetchall()]
    c.close()
    p = dict(p)
    prices = [h for h in hist if h.get("price")]
    cur  = prices[-1]["price"] if prices else None
    minp = min(h["price"] for h in prices) if prices else None
    maxp = max(h["price"] for h in prices) if prices else None
    chg  = (cur - prices[0]["price"]) if prices and len(prices) > 1 else None
    cc   = "var(--gt)" if chg and chg < 0 else "var(--red)" if chg and chg > 0 else "inherit"
    cf   = (("+" if chg > 0 else "") + fmt(chg)) if chg is not None else "&#8212;"

    sg = (
        '<div class="stat-grid">'
        '<div class="sc"><div class="sl">Aktualni cena</div><div class="sv">{cur}</div></div>'
        '<div class="sc"><div class="sl">Minimum</div><div class="sv svg">{mn}</div></div>'
        '<div class="sc"><div class="sl">Maximum</div><div class="sv svr">{mx}</div></div>'
        '<div class="sc"><div class="sl">Zmena celkem</div>'
        '<div class="sv" style="color:{cc}">{cf}</div></div>'
        '<div class="sc"><div class="sl">Pocet kontrol</div><div class="sv">{cnt}</div></div>'
        '<div class="sc"><div class="sl">Cilova cena</div><div class="sv">{tgt}</div></div>'
        '</div>'
    ).format(cur=fmt(cur), mn=fmt(minp), mx=fmt(maxp),
             cc=cc, cf=cf, cnt=len(hist), tgt=fmt(p["target"]))

    trs = ""
    for i in range(len(hist) - 1, -1, -1):
        row  = hist[i]
        prev = hist[i-1] if i > 0 else None
        if row.get("price") and prev and prev.get("price"):
            d = row["price"] - prev["price"]
            if d < 0:   td = '<span class="tdn">&#x2193; {}</span>'.format(fmt(abs(d)))
            elif d > 0: td = '<span class="tup">&#x2191; {}</span>'.format(fmt(d))
            else:       td = '<span class="tneu">&mdash;</span>'
        else:
            td = '<span class="tneu">&mdash;</span>'
        at = '<span class="badge bf">&#x1F525;</span>' if row.get("alza_days") else "&mdash;"
        ct = '<span class="badge bco">{}</span>'.format(row["coupon"]) if row.get("coupon") else "&mdash;"
        trs += (
            "<tr>"
            "<td style='color:var(--muted)'>{t}</td>"
            "<td><b>{p}</b></td><td>{d}</td><td>{a}</td><td>{c}</td>"
            "</tr>"
        ).format(t=row["checked_at"], p=fmt(row.get("price")), d=td, a=at, c=ct)

    nd = p["name"] or p["url"]
    target_js = str(p["target"]) if p["target"] else "null"
    hist_json = json.dumps(prices)

    page = (
        '<!DOCTYPE html>'
        '<html id="R" lang="cs"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>{nd}</title>'
        '<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>'
        + THEME_INIT_SCRIPT +
        '<style>' + CSS + '</style>'
        '</head><body>'
        '<nav class="nav">'
        '<a href="/" style="font-size:14px;color:var(--muted);font-weight:500">&larr; Zpet</a>'
        '<div style="font-size:15px;font-weight:600;flex:1;margin:0 16px;'
        'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.2px">{nd}</div>'
        '<div style="display:flex;gap:8px;align-items:center">'
        '<a href="{url}" target="_blank" style="font-size:13px;font-weight:500;color:var(--acc)">'
        'Otevrit &#x2197;</a>'
        + THEME_BTN_HTML +
        logout_btn_html() +
        '</div></nav>'
        '<div class="main">'
        '{sg}'
        '<div class="chart-card"><h2>Vyvoj ceny</h2>'
        '<div class="chart-wrap"><canvas id="ch"></canvas></div></div>'
        '<div class="table-card"><h2>Zaznamy</h2>'
        '<table><thead><tr>'
        '<th>Cas</th><th>Cena</th><th>Zmena</th><th>AlzaDny</th><th>Kupon</th>'
        '</tr></thead><tbody>{trs}</tbody></table></div>'
        '</div>'
        + THEME_SCRIPT +
        '<script>'
        '(function() {'
        '  var dark = document.getElementById("R").classList.contains("dark");'
        '  var H = ' + hist_json + ';'
        '  var labels = H.map(function(h) {'
        '    var d = new Date(h.checked_at);'
        '    return d.toLocaleDateString("cs-CZ",{day:"2-digit",month:"2-digit"}) + " " +'
        '           d.toLocaleTimeString("cs-CZ",{hour:"2-digit",minute:"2-digit"});'
        '  });'
        '  var vals = H.map(function(h) { return h.price; });'
        '  var target = ' + target_js + ';'
        '  var acc = dark ? "#0a84ff" : "#007aff";'
        '  var grid = dark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.04)";'
        '  var tick = dark ? "#636366" : "#8e8e93";'
        '  var bg = dark ? "rgba(10,132,255,0.1)" : "rgba(0,122,255,0.07)";'
        '  var ptb = dark ? "#1c1c1e" : "#fff";'
        '  var datasets = [{'
        '    label: "Cena", data: vals, borderColor: acc, borderWidth: 2.5,'
        '    backgroundColor: bg, fill: true, tension: 0.4,'
        '    pointRadius: vals.length < 40 ? 4 : 0,'
        '    pointBackgroundColor: acc, pointBorderColor: ptb,'
        '    pointBorderWidth: 2, pointHoverRadius: 6'
        '  }];'
        '  if (target) {'
        '    datasets.push({'
        '      label: "Cilova cena",'
        '      data: vals.map(function() { return target; }),'
        '      borderColor: "#34c759", borderWidth: 1.5,'
        '      borderDash: [6, 4], pointRadius: 0, fill: false'
        '    });'
        '  }'
        '  new Chart(document.getElementById("ch"), {'
        '    type: "line",'
        '    data: { labels: labels, datasets: datasets },'
        '    options: {'
        '      responsive: true, maintainAspectRatio: false,'
        '      interaction: { intersect: false, mode: "index" },'
        '      plugins: {'
        '        legend: { display: !!target, position: "top",'
        '          labels: { font: { size: 12 }, boxWidth: 14, padding: 14,'
        '            color: dark ? "#aeaeb2" : "#3a3a3c" } },'
        '        tooltip: {'
        '          backgroundColor: dark ? "rgba(44,44,46,.95)" : "rgba(28,28,30,.92)",'
        '          titleFont: { size: 12, weight: "600" },'
        '          bodyFont: { size: 13 }, padding: 10, cornerRadius: 10,'
        '          callbacks: { label: function(c) {'
        '            return "  " + c.dataset.label + ": " + c.parsed.y.toLocaleString("cs-CZ") + " Kc";'
        '          }}'
        '        }'
        '      },'
        '      scales: {'
        '        x: { grid: { display: false },'
        '             ticks: { font: { size: 11 }, color: tick, maxTicksLimit: 8 } },'
        '        y: { grid: { color: grid },'
        '             ticks: { font: { size: 11 }, color: tick,'
        '               callback: function(v) { return v.toLocaleString("cs-CZ") + " Kc"; } } }'
        '      }'
        '    }'
        '  });'
        '})();'
        '</script>'
        '</body></html>'
    ).replace('{nd}', nd).replace('{url}', p["url"]).replace('{sg}', sg).replace('{trs}', trs)

    return page

@app.route("/health")
def health(): return "ok"

@app.route("/api/history/<int:pid>")
@login_required
def api_history(pid):
    c = get_db()
    rows = c.execute(
        "SELECT checked_at,price,alza_days,coupon FROM history "
        "WHERE product_id=? AND price IS NOT NULL ORDER BY checked_at ASC", (pid,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ── Start ────────────────────────────────────────────────────────────────────────

try:
    init_db()
    log.info("init_db OK")
except Exception as e:
    log.error("init_db FAILED: %s", e, exc_info=True)
    raise SystemExit(1)

scheduler = BackgroundScheduler()
scheduler.add_job(check_all, "cron", hour=CHECK_HOUR, minute=0)
try:
    scheduler.start()
    log.info("scheduler started")
except Exception as e:
    log.error("scheduler failed: %s", e)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
