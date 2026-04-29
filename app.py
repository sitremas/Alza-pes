import sqlite3, requests, json, re, os, logging, time, threading
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
DB               = os.environ.get("DB_PATH", "tracker.db")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SCRAPE_DO_TOKEN  = os.environ.get("SCRAPE_DO_TOKEN", "")
CHECK_HOUR       = int(os.environ.get("CHECK_HOUR", "9"))

# ── DB ─────────────────────────────────────────────────────────────────────────

def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                url      TEXT UNIQUE NOT NULL,
                name     TEXT,
                target   REAL,
                active   INTEGER DEFAULT 1,
                added_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                price      REAL,
                alza_days  INTEGER DEFAULT 0,
                coupon     TEXT,
                checked_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );
        """)

# ── Scraper ────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "cs-CZ,cs;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

def fetch_url(url):
    if SCRAPE_DO_TOKEN:
        proxy = f"https://api.scrape.do?token={SCRAPE_DO_TOKEN}&url={requests.utils.quote(url)}&geoCode=cz"
        return requests.get(proxy, timeout=30)
    return requests.get(url, headers=HEADERS, timeout=20)

def scrape(url):
    try:
        r = fetch_url(url)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Fetch failed {url}: {e}")
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
    alza_days = any(k in page for k in ["alzadny","alza dny","alza-dny"])
    coupon = None
    for cls in ["coupon","voucher","kupon","promo-code"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            t = el.get_text(strip=True)[:60]
            if t: coupon = t; break
    return {"name": name, "price": price, "alza_days": alza_days, "coupon": coupon}

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Telegram: {e}"); return False

def tg_summary(pid):
    c = get_db()
    p    = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p: c.close(); return
    lat  = c.execute("SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1",(pid,)).fetchone()
    minp = c.execute("SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL",(pid,)).fetchone()
    chks = c.execute("SELECT COUNT(*) as n FROM history WHERE product_id=?",(pid,)).fetchone()
    c.close()
    name  = p["name"] or "Produkt"
    price = lat["price"] if lat and lat["price"] else None
    lines = [f"📦 <b>{name}</b>"]
    if price:
        lines.append(f"\n💰 Aktuální cena: <b>{price:,.0f} Kč</b>".replace(",", "\u00a0"))
    if minp and minp["m"]:
        lines.append(f"📉 Historické minimum: <b>{minp['m']:,.0f} Kč</b>".replace(",", "\u00a0"))
    if p["target"]:
        lines.append(f"🎯 Cílová cena: <b>{p['target']:,.0f} Kč</b>".replace(",", "\u00a0"))
        if price:
            diff = price - p["target"]
            if diff <= 0: lines.append("✅ Cílová cena <b>dosažena!</b>")
            else: lines.append(f"⏳ Chybí: <b>{diff:,.0f} Kč</b>".replace(",", "\u00a0"))
    if lat and lat["alza_days"]: lines.append("🔥 <b>AlzaDny jsou aktivní!</b>")
    if lat and lat["coupon"]:    lines.append(f"🎫 Kupon: <code>{lat['coupon']}</code>")
    lines.append(f"\n🔍 Kontrol provedeno: {chks['n']}")
    lines.append(f"🔗 <a href='{p['url']}'>Otevřít na Alze</a>")
    tg("\n".join(lines))

# ── Check ──────────────────────────────────────────────────────────────────────

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
    prev = c.execute("SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",(pid,)).fetchone()
    c.close()
    if price is None: return
    if prev and prev["price"] and price < prev["price"]:
        diff = prev["price"] - price
        tg(f"📉 <b>Pokles ceny!</b>\n{name}\n\n<b>{price:,.0f} Kč</b>  (↓ {diff:,.0f} Kč)\n{p['url']}".replace(",","\u00a0"))
    if p["target"] and price <= p["target"]:
        tg(f"🎯 <b>Cílová cena dosažena!</b>\n{name}\n\n<b>{price:,.0f} Kč</b> ≤ {p['target']:,.0f} Kč\n{p['url']}".replace(",","\u00a0"))
    if result["alza_days"]:
        tg(f"🔥 <b>AlzaDny jsou aktivní!</b>\n{name}\n\n{price:,.0f} Kč\n{p['url']}".replace(",","\u00a0"))
    if result["coupon"]:
        tg(f"🎫 <b>Kupon:</b> <code>{result['coupon']}</code>\n{name}\n{price:,.0f} Kč\n{p['url']}".replace(",","\u00a0"))
    log.info(f"[{name}] {price} Kč alza_days={result['alza_days']}")

def check_all():
    c = get_db()
    ids = [r["id"] for r in c.execute("SELECT id FROM products WHERE active=1").fetchall()]
    c.close()
    log.info(f"Daily check — {len(ids)} products")
    for pid in ids:
        check_product(pid)
        time.sleep(4)

# ── Helpers ────────────────────────────────────────────────────────────────────

def fmt(n):
    if n is None: return "—"
    return f"{n:,.0f}".replace(",", "\u00a0") + "\u00a0Kč"

CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f2f2f7;--surface:#fff;--surface2:#f9f9fb;
  --border:rgba(0,0,0,0.08);--border2:rgba(0,0,0,0.05);
  --text:#1c1c1e;--text2:#3a3a3c;--muted:#8e8e93;
  --accent:#007aff;--accent-hover:#0062cc;
  --red:#ff3b30;--green:#34c759;
  --green-text:#1a7f3c;--green-bg:#f0fdf4;
  --amber:#ff9500;--amber-bg:#fff8f0;--amber-text:#7d4400;
  --blue-bg:#f0f6ff;
  --r:14px;--rs:10px;--rxs:8px;
  --shadow:0 1px 3px rgba(0,0,0,0.06),0 4px 16px rgba(0,0,0,0.04);
  --shadow-sm:0 1px 2px rgba(0,0,0,0.05);
}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  background:var(--bg);color:var(--text);font-size:15px;line-height:1.5;
  -webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.nav{background:rgba(255,255,255,.85);
  backdrop-filter:saturate(180%) blur(20px);
  -webkit-backdrop-filter:saturate(180%) blur(20px);
  border-bottom:1px solid var(--border);height:52px;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;position:sticky;top:0;z-index:100}
.nav-logo{font-size:17px;font-weight:600;letter-spacing:-.3px}
.nav-logo span{color:var(--accent)}
.nav-right{display:flex;gap:8px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;
  padding:8px 16px;border-radius:var(--rxs);border:none;
  font-family:inherit;font-size:14px;font-weight:500;cursor:pointer;
  transition:all .15s;white-space:nowrap}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-hover)}
.btn-secondary{background:var(--surface);color:var(--text);
  border:1px solid var(--border);box-shadow:var(--shadow-sm)}
.btn-secondary:hover{background:var(--surface2)}
.btn-ghost{background:transparent;color:var(--muted);padding:6px 10px}
.btn-ghost:hover{background:var(--border2);color:var(--text)}
.btn-danger{background:transparent;color:var(--red);padding:6px 10px}
.btn-danger:hover{background:rgba(255,59,48,.08)}
.btn-sm{padding:6px 12px;font-size:13px}
.btn-tg{background:#229ED9;color:#fff}
.btn-tg:hover{background:#1a8ab8}
.main{max-width:860px;margin:0 auto;padding:28px 20px}
.banner{display:flex;align-items:center;gap:10px;padding:12px 16px;
  border-radius:var(--rs);margin-bottom:20px;font-size:13px;font-weight:500}
.bwarn{background:var(--amber-bg);color:var(--amber-text);border:1px solid rgba(255,149,0,.2)}
.bok{background:var(--green-bg);color:var(--green-text);border:1px solid rgba(52,199,89,.2)}
.add-card{background:var(--surface);border-radius:var(--r);
  box-shadow:var(--shadow);padding:22px 24px;margin-bottom:28px}
.add-label{font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.06em;margin-bottom:14px}
.form-row{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
.fg{display:flex;flex-direction:column;gap:6px;flex:1;min-width:180px}
.fgl{flex:3;min-width:260px}
.fg label{font-size:12px;font-weight:500;color:var(--text2)}
.fg input{padding:10px 13px;border:1.5px solid var(--border);border-radius:var(--rxs);
  font-family:inherit;font-size:14px;background:var(--surface2);color:var(--text);
  outline:none;transition:border .15s,background .15s}
.fg input:focus{border-color:var(--accent);background:var(--surface)}
.fg input::placeholder{color:var(--muted)}
.sec{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.sec-title{font-size:12px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.card{background:var(--surface);border-radius:var(--r);
  box-shadow:var(--shadow);margin-bottom:12px;overflow:hidden;
  transition:box-shadow .2s}
.card:hover{box-shadow:0 2px 8px rgba(0,0,0,.08),0 8px 24px rgba(0,0,0,.06)}
.card.paused{opacity:.5}
.card-body{display:flex;align-items:center;gap:14px;padding:16px 20px;flex-wrap:wrap}
.card-info{flex:1;min-width:0}
.card-name{font-size:15px;font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;letter-spacing:-.2px}
.card-url{font-size:12px;color:var(--muted);margin-top:2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-url a{color:var(--accent)}
.badges{display:flex;gap:6px;flex-wrap:wrap;min-width:130px}
.badge{display:inline-flex;align-items:center;gap:3px;
  font-size:11px;font-weight:600;padding:4px 9px;border-radius:20px}
.bf{background:var(--amber-bg);color:var(--amber-text)}
.bt{background:var(--green-bg);color:var(--green-text)}
.bco{background:var(--blue-bg);color:#1a5fa8}
.bp{background:var(--bg);color:var(--muted)}
.card-price{text-align:right;min-width:120px}
.price-main{font-size:22px;font-weight:700;letter-spacing:-.5px;line-height:1.2}
.price-delta{font-size:12px;font-weight:500;margin-top:3px}
.dd{color:var(--green-text)}.du{color:var(--red)}.dn2{color:var(--muted)}
.card-actions{display:flex;gap:4px}
.card-foot{background:var(--surface2);border-top:1px solid var(--border2);
  padding:10px 20px;display:flex;justify-content:space-between;
  font-size:12px;color:var(--muted)}
.foot-l{display:flex;gap:16px}
.empty{text-align:center;padding:64px 20px;color:var(--muted)}
.empty-icon{font-size:44px;margin-bottom:14px}
.empty h3{font-size:17px;font-weight:600;color:var(--text);margin-bottom:6px}
.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin-bottom:22px}
.sc{background:var(--surface);border-radius:var(--rs);box-shadow:var(--shadow);padding:14px 16px}
.sl{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px}
.sv{font-size:20px;font-weight:700;letter-spacing:-.3px}
.svg{color:var(--green-text)}.svr{color:var(--red)}
.chart-card{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:22px;margin-bottom:22px}
.chart-card h2{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:18px}
.chart-wrap{height:260px;position:relative}
.table-card{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden}
.table-card h2{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;padding:16px 20px;border-bottom:1px solid var(--border)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:10px 18px;font-size:11px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.05em;background:var(--surface2);border-bottom:1px solid var(--border)}
td{padding:11px 18px;border-bottom:1px solid var(--border2)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--surface2)}
.tdn{color:var(--green-text);font-weight:600}
.tup{color:var(--red);font-weight:600}
.tneu{color:var(--muted)}
"""

# ── Index ──────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    c = get_db()
    products = c.execute("SELECT * FROM products ORDER BY added_at DESC").fetchall()
    rows = []
    for p in products:
        lat  = c.execute("SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1",(p["id"],)).fetchone()
        prev = c.execute("SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",(p["id"],)).fetchone()
        low  = c.execute("SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL",(p["id"],)).fetchone()
        chks = c.execute("SELECT COUNT(*) as n FROM history WHERE product_id=?",(p["id"],)).fetchone()
        chg  = None
        if lat and lat["price"] and prev and prev["price"]:
            chg = lat["price"] - prev["price"]
        rows.append(dict(p=dict(p), lat=dict(lat) if lat else None,
                         low=low["m"], chg=chg, chks=chks["n"]))
    c.close()
    tg_ok = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    banner = (
        '<div class="banner bok">✓ Telegram aktivní · automatická kontrola každý den v 9:00 UTC</div>'
        if tg_ok else
        '<div class="banner bwarn">⚠ Telegram není nastaven — přidej TELEGRAM_TOKEN a TELEGRAM_CHAT_ID v Render dashboardu.</div>'
    )

    cards_html = ""
    if not rows:
        cards_html = '<div class="empty"><div class="empty-icon">📦</div><h3>Žádné produkty</h3><p>Přidej první produkt výše.</p></div>'

    for item in rows:
        p   = item["p"]
        lat = item["lat"]
        if lat and lat.get("price"):
            ph = f'<div class="price-main">{fmt(lat["price"])}</div>'
            if item["chg"] is not None:
                if item["chg"] < 0:   ph += f'<div class="price-delta dd">↓ {fmt(abs(item["chg"]))}</div>'
                elif item["chg"] > 0: ph += f'<div class="price-delta du">↑ {fmt(item["chg"])}</div>'
                else:                 ph += '<div class="price-delta dn2">beze změny</div>'
            else:                     ph += '<div class="price-delta dn2">první záznam</div>'
        else:
            ph = '<div style="font-size:14px;color:var(--muted);font-weight:500">Načítám…</div>'

        badges = ""
        if not p["active"]:              badges += '<span class="badge bp">Pozastaveno</span>'
        if lat and lat.get("alza_days"): badges += '<span class="badge bf">🔥 AlzaDny</span>'
        if lat and lat.get("coupon"):    badges += f'<span class="badge bco">🎫 {lat["coupon"][:18]}</span>'
        if p["target"]:                  badges += f'<span class="badge bt">Cíl {fmt(p["target"])}</span>'

        fl = ""
        if item["low"]: fl += f'<span>Min&nbsp;<b>{fmt(item["low"])}</b></span>'
        fl += f'<span>{item["chks"]}&nbsp;kontrol</span>'
        fr = lat["checked_at"] if lat else "zatím nekontrolováno"
        nd = p["name"] or "Načítám…"
        us = p["url"][:65] + ("…" if len(p["url"])>65 else "")

        cards_html += f"""
        <div class="card {'paused' if not p['active'] else ''}">
          <div class="card-body">
            <div class="card-info">
              <div class="card-name">{nd}</div>
              <div class="card-url"><a href="{p['url']}" target="_blank">{us}</a></div>
            </div>
            <div class="badges">{badges}</div>
            <div class="card-price">{ph}</div>
            <div class="card-actions">
              <a href="/product/{p['id']}" class="btn btn-secondary btn-sm">📈 Historie</a>
              <form method="post" action="/notify/{p['id']}">
                <button class="btn btn-tg btn-sm" title="Poslat souhrn do Telegramu">✈ TG</button>
              </form>
              <form method="post" action="/check/{p['id']}">
                <button class="btn btn-ghost btn-sm" title="Zkontrolovat cenu teď">↻</button>
              </form>
              <form method="post" action="/toggle/{p['id']}">
                <button class="btn btn-ghost btn-sm">{'⏸' if p['active'] else '▶'}</button>
              </form>
              <form method="post" action="/delete/{p['id']}" onsubmit="return confirm('Smazat?')">
                <button class="btn btn-danger btn-sm">✕</button>
              </form>
            </div>
          </div>
          <div class="card-foot">
            <div class="foot-l">{fl}</div>
            <span>{fr}</span>
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="cs"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AlzaTracker</title><style>{CSS}</style>
</head><body>
<nav class="nav">
  <div class="nav-logo">Alza<span>Tracker</span></div>
  <div class="nav-right">
    <form method="post" action="/check_all">
      <button class="btn btn-secondary btn-sm">↻ Zkontrolovat vše</button>
    </form>
  </div>
</nav>
<div class="main">
  {banner}
  <div class="add-card">
    <div class="add-label">Přidat produkt</div>
    <form method="post" action="/add">
      <div class="form-row">
        <div class="fg fgl">
          <label>URL produktu na Alze</label>
          <input type="url" name="url" placeholder="https://www.alza.cz/nazev-produktu.htm" required>
        </div>
        <div class="fg">
          <label>Cílová cena (Kč) — nepovinné</label>
          <input type="number" name="target" placeholder="např. 15000" min="0" step="1">
        </div>
        <button class="btn btn-primary" type="submit">Přidat</button>
      </div>
    </form>
  </div>
  <div class="sec">
    <span class="sec-title">Sledované produkty</span>
    <span style="font-size:13px;color:var(--muted)">{len(rows)} celkem</span>
  </div>
  {cards_html}
</div></body></html>"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/add", methods=["POST"])
def add():
    url = request.form.get("url","").strip()
    target = request.form.get("target","").strip()
    if not url: return redirect(url_for("index"))
    target_val = float(target) if target else None
    c = get_db()
    try:
        c.execute("INSERT INTO products (url,target) VALUES (?,?)", (url, target_val))
        c.commit()
        pid = c.execute("SELECT id FROM products WHERE url=?", (url,)).fetchone()["id"]
        c.close()
        threading.Thread(target=check_product, args=(pid,), daemon=True).start()
    except sqlite3.IntegrityError:
        c.close()
    return redirect(url_for("index"))

@app.route("/delete/<int:pid>", methods=["POST"])
def delete(pid):
    c = get_db()
    c.execute("DELETE FROM history WHERE product_id=?", (pid,))
    c.execute("DELETE FROM products WHERE id=?", (pid,))
    c.commit(); c.close()
    return redirect(url_for("index"))

@app.route("/toggle/<int:pid>", methods=["POST"])
def toggle(pid):
    c = get_db()
    c.execute("UPDATE products SET active = 1 - active WHERE id=?", (pid,))
    c.commit(); c.close()
    return redirect(url_for("index"))

@app.route("/check/<int:pid>", methods=["POST"])
def check_now(pid):
    threading.Thread(target=check_product, args=(pid,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/check_all", methods=["POST"])
def check_all_route():
    threading.Thread(target=check_all, daemon=True).start()
    return redirect(url_for("index"))

@app.route("/notify/<int:pid>", methods=["POST"])
def notify(pid):
    threading.Thread(target=tg_summary, args=(pid,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/product/<int:pid>")
def detail(pid):
    c = get_db()
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p: return redirect(url_for("index"))
    hist = [dict(r) for r in c.execute(
        "SELECT * FROM history WHERE product_id=? ORDER BY checked_at ASC",(pid,)).fetchall()]
    c.close()
    p = dict(p)
    prices = [h for h in hist if h.get("price")]
    cur  = prices[-1]["price"] if prices else None
    minp = min(h["price"] for h in prices) if prices else None
    maxp = max(h["price"] for h in prices) if prices else None
    chg  = (cur - prices[0]["price"]) if prices and len(prices)>1 else None
    cc   = "var(--green-text)" if chg and chg<0 else "var(--red)" if chg and chg>0 else "inherit"
    cf   = (("+" if chg>0 else "")+fmt(chg)) if chg is not None else "—"

    sg = f"""<div class="stat-grid">
      <div class="sc"><div class="sl">Aktuální cena</div><div class="sv">{fmt(cur)}</div></div>
      <div class="sc"><div class="sl">Minimum</div><div class="sv svg">{fmt(minp)}</div></div>
      <div class="sc"><div class="sl">Maximum</div><div class="sv svr">{fmt(maxp)}</div></div>
      <div class="sc"><div class="sl">Změna celkem</div><div class="sv" style="color:{cc}">{cf}</div></div>
      <div class="sc"><div class="sl">Počet kontrol</div><div class="sv">{len(hist)}</div></div>
      <div class="sc"><div class="sl">Cílová cena</div><div class="sv">{fmt(p['target'])}</div></div>
    </div>"""

    trs = ""
    for i in range(len(hist)-1, -1, -1):
        row  = hist[i]
        prev = hist[i-1] if i>0 else None
        if row.get("price") and prev and prev.get("price"):
            d = row["price"] - prev["price"]
            if d<0:   td = f'<span class="tdn">↓ {fmt(abs(d))}</span>'
            elif d>0: td = f'<span class="tup">↑ {fmt(d)}</span>'
            else:     td = '<span class="tneu">—</span>'
        else: td = '<span class="tneu">—</span>'
        at = '<span class="badge bf">🔥</span>' if row.get("alza_days") else "—"
        ct = f'<span class="badge bco">{row["coupon"]}</span>' if row.get("coupon") else "—"
        trs += f'<tr><td style="color:var(--muted)">{row["checked_at"]}</td><td><b>{fmt(row.get("price"))}</b></td><td>{td}</td><td>{at}</td><td>{ct}</td></tr>'

    nd = p["name"] or p["url"]
    return f"""<!DOCTYPE html>
<html lang="cs"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{nd}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{CSS}</style>
</head><body>
<nav class="nav">
  <a href="/" style="font-size:14px;color:var(--muted);font-weight:500">← Zpět</a>
  <div style="font-size:15px;font-weight:600;flex:1;margin:0 16px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.2px">{nd}</div>
  <a href="{p['url']}" target="_blank" style="font-size:13px;font-weight:500;color:var(--accent)">Otevřít na Alze ↗</a>
</nav>
<div class="main">
  {sg}
  <div class="chart-card">
    <h2>Vývoj ceny</h2>
    <div class="chart-wrap"><canvas id="ch"></canvas></div>
  </div>
  <div class="table-card">
    <h2>Záznamy</h2>
    <table><thead><tr><th>Čas</th><th>Cena</th><th>Změna</th><th>AlzaDny</th><th>Kupon</th></tr></thead>
    <tbody>{trs}</tbody></table>
  </div>
</div>
<script>
const H={json.dumps(prices)};
const labels=H.map(h=>{{const d=new Date(h.checked_at);
  return d.toLocaleDateString('cs-CZ',{{day:'2-digit',month:'2-digit'}})+' '+
         d.toLocaleTimeString('cs-CZ',{{hour:'2-digit',minute:'2-digit'}});}});
const vals=H.map(h=>h.price);
const target={p['target'] or 'null'};
new Chart(document.getElementById('ch'),{{
  type:'line',data:{{labels,datasets:[
    {{label:'Cena',data:vals,borderColor:'#007aff',borderWidth:2.5,
      backgroundColor:'rgba(0,122,255,0.07)',fill:true,tension:0.4,
      pointRadius:vals.length<40?4:0,pointBackgroundColor:'#007aff',
      pointBorderColor:'#fff',pointBorderWidth:2,pointHoverRadius:6}},
    ...(target?[{{label:'Cílová cena',data:vals.map(()=>target),
      borderColor:'#34c759',borderWidth:1.5,borderDash:[6,4],pointRadius:0,fill:false}}]:[])
  ]}},
  options:{{responsive:true,maintainAspectRatio:false,
    interaction:{{intersect:false,mode:'index'}},
    plugins:{{
      legend:{{display:!!target,position:'top',labels:{{font:{{size:12}},boxWidth:14,padding:14}}}},
      tooltip:{{backgroundColor:'rgba(28,28,30,.92)',titleFont:{{size:12,weight:'600'}},
        bodyFont:{{size:13}},padding:10,cornerRadius:10,
        callbacks:{{label:c=>`  ${{c.dataset.label}}: ${{c.parsed.y.toLocaleString('cs-CZ')}} Kč`}}}},
    }},
    scales:{{
      x:{{grid:{{display:false}},ticks:{{font:{{size:11}},color:'#8e8e93',maxTicksLimit:8}}}},
      y:{{grid:{{color:'rgba(0,0,0,0.04)'}},ticks:{{font:{{size:11}},color:'#8e8e93',callback:v=>v.toLocaleString('cs-CZ')+' Kč'}}}},
    }},
  }},
}});
</script></body></html>"""

@app.route("/health")
def health(): return "ok"

@app.route("/api/history/<int:pid>")
def api_history(pid):
    c = get_db()
    rows = c.execute("SELECT checked_at,price,alza_days,coupon FROM history WHERE product_id=? AND price IS NOT NULL ORDER BY checked_at ASC",(pid,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ── Start ──────────────────────────────────────────────────────────────────────

init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(check_all, "cron", hour=CHECK_HOUR, minute=0)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
