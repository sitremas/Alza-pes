import sqlite3, requests, json, re, os, logging, time, threading
from bs4 import BeautifulSoup
from flask import Flask, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
DB = os.environ.get("DB_PATH", "tracker.db")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "60"))

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

SCRAPE_DO_TOKEN = os.environ.get("SCRAPE_DO_TOKEN", "")

def fetch_url(url):
    """Fetch via scrape.do proxy if token set, else direct."""
    if SCRAPE_DO_TOKEN:
        proxy_url = f"https://api.scrape.do?token={SCRAPE_DO_TOKEN}&url={requests.utils.quote(url)}&geoCode=cz"
        return requests.get(proxy_url, timeout=30)
    else:
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
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        log.error(f"Telegram: {e}")

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
        tg(f"📉 <b>Pokles ceny!</b>\n{name}\n\n<b>{price:,.0f} Kč</b>  (↓ {diff:,.0f} Kč)\n{p['url']}")
    if p["target"] and price <= p["target"]:
        tg(f"🎯 <b>Cílová cena!</b>\n{name}\n\n<b>{price:,.0f} Kč</b> ≤ {p['target']:,.0f} Kč\n{p['url']}")
    if result["alza_days"]:
        tg(f"🔥 <b>AlzaDny!</b>\n{name}\n\n{price:,.0f} Kč\n{p['url']}")
    if result["coupon"]:
        tg(f"🎫 <b>Kupon:</b> <code>{result['coupon']}</code>\n{name}\n{price:,.0f} Kč\n{p['url']}")
    log.info(f"[{name}] {price} Kč alza_days={result['alza_days']}")

def check_all():
    c = get_db()
    ids = [r["id"] for r in c.execute("SELECT id FROM products WHERE active=1").fetchall()]
    c.close()
    log.info(f"Checking {len(ids)} products")
    for pid in ids:
        check_product(pid)
        time.sleep(4)

# ── HTML helpers ───────────────────────────────────────────────────────────────

def fmt(n):
    if n is None: return "—"
    return f"{n:,.0f}".replace(",", " ") + " Kč"

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--bg:#f5f5f3;--s:#fff;--b:#e4e4e0;--t:#181816;--m:#686863;
  --red:#e52213;--redd:#b91c0f;--g:#166534;--gbg:#f0fdf4;
  --abg:#fffbeb;--ac:#92400e;--bbg:#eff6ff;--bc:#1d4ed8;--r:10px;--rs:6px}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:var(--bg);color:var(--t);font-size:14px;line-height:1.5}
a{color:inherit;text-decoration:none}
.bar{background:var(--s);border-bottom:1px solid var(--b);height:54px;
  display:flex;align-items:center;justify-content:space-between;
  padding:0 24px;position:sticky;top:0;z-index:50}
.logo{font-size:15px;font-weight:700;display:flex;align-items:center;gap:8px}
.dot{width:8px;height:8px;background:var(--red);border-radius:50%}
.ibadge{font-size:12px;color:var(--m);background:var(--bg);border:1px solid var(--b);
  border-radius:20px;padding:3px 10px}
.wrap{max-width:900px;margin:0 auto;padding:24px 16px}
.alert{display:flex;align-items:center;gap:8px;padding:11px 15px;
  border-radius:var(--rs);margin-bottom:18px;font-size:13px}
.aw{background:var(--abg);border:1px solid #fcd34d;color:var(--ac)}
.ag{background:var(--gbg);border:1px solid #86efac;color:var(--g)}
.addcard{background:var(--s);border:1px solid var(--b);border-radius:var(--r);
  padding:20px;margin-bottom:22px}
.addtitle{font-size:11px;font-weight:600;color:var(--m);text-transform:uppercase;
  letter-spacing:.05em;margin-bottom:14px}
.row{display:flex;gap:10px;flex-wrap:wrap}
.fg{display:flex;flex-direction:column;gap:4px;flex:1;min-width:190px}
.fgl{flex:3;min-width:270px}
.fg label{font-size:11px;color:var(--m);font-weight:500;text-transform:uppercase;letter-spacing:.04em}
.fg input{padding:8px 11px;border:1px solid var(--b);border-radius:var(--rs);
  font-size:13px;background:var(--bg);color:var(--t);outline:none}
.fg input:focus{border-color:var(--red);background:var(--s)}
.fgb{display:flex;align-items:flex-end}
.btn{display:inline-flex;align-items:center;gap:5px;padding:8px 15px;
  border-radius:var(--rs);border:1px solid var(--b);background:var(--s);
  color:var(--t);font-size:13px;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--bg)}
.btnr{background:var(--red);color:#fff;border-color:var(--red)}
.btnr:hover{background:var(--redd)}
.btns{padding:5px 11px;font-size:12px}
.btng{border-color:transparent;background:transparent}
.btng:hover{background:var(--bg)}
.sh{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px}
.st{font-size:11px;font-weight:600;color:var(--m);text-transform:uppercase;letter-spacing:.05em}
.card{background:var(--s);border:1px solid var(--b);border-radius:var(--r);
  margin-bottom:10px;overflow:hidden}
.card.off{opacity:.5}
.cb{display:flex;align-items:center;gap:12px;padding:14px 18px;flex-wrap:wrap}
.ci{flex:1;min-width:0}
.cn{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cu{font-size:11px;color:var(--m);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.cu a{color:var(--bc)}
.cbadges{display:flex;flex-wrap:wrap;gap:5px;min-width:160px}
.badge{display:inline-flex;align-items:center;font-size:11px;font-weight:500;padding:3px 8px;border-radius:20px}
.bf{background:var(--abg);color:var(--ac)}
.bt{background:var(--gbg);color:var(--g)}
.bco{background:var(--bbg);color:var(--bc)}
.bp{background:var(--bg);color:var(--m)}
.cp{text-align:right;min-width:110px}
.pv{font-size:20px;font-weight:700;line-height:1.2}
.pc{font-size:11px;margin-top:2px}
.dn{color:var(--g)}.up{color:var(--red)}.neu{color:var(--m)}
.ca{display:flex;gap:5px;align-items:center}
.cf{background:var(--bg);border-top:1px solid var(--b);padding:8px 18px;
  display:flex;align-items:center;justify-content:space-between;font-size:12px;color:var(--m)}
.cfl{display:flex;gap:14px}
.empty{text-align:center;padding:60px 20px;color:var(--m)}
.empty h3{font-size:16px;font-weight:600;color:var(--t);margin-bottom:4px}
"""

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    c = get_db()
    products = c.execute("SELECT * FROM products ORDER BY added_at DESC").fetchall()
    rows = []
    for p in products:
        lat   = c.execute("SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1",(p["id"],)).fetchone()
        prev  = c.execute("SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",(p["id"],)).fetchone()
        low   = c.execute("SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL",(p["id"],)).fetchone()
        chks  = c.execute("SELECT COUNT(*) as n FROM history WHERE product_id=?",(p["id"],)).fetchone()
        chg   = None
        if lat and lat["price"] and prev and prev["price"]:
            chg = lat["price"] - prev["price"]
        rows.append(dict(p=dict(p), lat=dict(lat) if lat else None,
                         low=low["m"], chg=chg, chks=chks["n"]))
    c.close()
    tg_ok = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)

    cards_html = ""
    if not rows:
        cards_html = """<div class="empty">
          <div style="font-size:40px;margin-bottom:12px">📦</div>
          <h3>Žádné produkty</h3>
          <p>Vlož URL produktu z Alzy a začni sledovat ceny.</p>
        </div>"""
    for item in rows:
        p   = item["p"]
        lat = item["lat"]
        # price block
        if lat and lat.get("price"):
            pval = f'<div class="pv">{fmt(lat["price"])}</div>'
            if item["chg"] is not None:
                if item["chg"] < 0:
                    pchg = f'<div class="pc dn">▼ {fmt(abs(item["chg"]))}</div>'
                elif item["chg"] > 0:
                    pchg = f'<div class="pc up">▲ {fmt(item["chg"])}</div>'
                else:
                    pchg = '<div class="pc neu">— beze změny</div>'
            else:
                pchg = '<div class="pc neu">— první záznam</div>'
        else:
            pval = '<div style="font-size:13px;color:var(--m)">Čekám…</div>'
            pchg = ""
        # badges
        badges = ""
        if not p["active"]:         badges += '<span class="badge bp">⏸ Pozastaveno</span>'
        if lat and lat.get("alza_days"): badges += '<span class="badge bf">🔥 AlzaDny</span>'
        if lat and lat.get("coupon"):    badges += f'<span class="badge bco">🎫 {lat["coupon"][:20]}</span>'
        if p["target"]:             badges += f'<span class="badge bt">Cíl: {fmt(p["target"])}</span>'
        # footer
        foot_l = ""
        if item["low"]: foot_l += f'<span>Min: <b>{fmt(item["low"])}</b></span>'
        foot_l += f'<span>{item["chks"]} kontrol</span>'
        foot_r = lat["checked_at"] if lat else "zatím nekontrolováno"
        pause_icon = "▶" if not p["active"] else "⏸"
        name_disp = p["name"] or "⏳ Načítám…"
        url_short = p["url"][:70] + ("…" if len(p["url"])>70 else "")
        cards_html += f"""
        <div class="card {'off' if not p['active'] else ''}">
          <div class="cb">
            <div class="ci">
              <div class="cn">{name_disp}</div>
              <div class="cu"><a href="{p['url']}" target="_blank">{url_short}</a></div>
            </div>
            <div class="cbadges">{badges}</div>
            <div class="cp">{pval}{pchg}</div>
            <div class="ca">
              <a href="/product/{p['id']}" class="btn btns">📈 Historie</a>
              <form method="post" action="/check/{p['id']}"><button class="btn btns btng" title="Zkontrolovat teď">↻</button></form>
              <form method="post" action="/toggle/{p['id']}"><button class="btn btns btng">{pause_icon}</button></form>
              <form method="post" action="/delete/{p['id']}" onsubmit="return confirm('Smazat produkt a historii?')">
                <button class="btn btns btng" style="color:var(--red)">✕</button>
              </form>
            </div>
          </div>
          <div class="cf">
            <div class="cfl">{foot_l}</div>
            <span>{foot_r}</span>
          </div>
        </div>"""

    tg_alert = (
        '<div class="alert ag">✓ Telegram notifikace jsou aktivní</div>' if tg_ok
        else '<div class="alert aw">⚠ Telegram není nastaven — přidej TELEGRAM_TOKEN a TELEGRAM_CHAT_ID v Render dashboardu.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="cs"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alza Tracker</title>
<style>{CSS}</style>
</head><body>
<div class="bar">
  <div class="logo"><div class="dot"></div>Alza Tracker</div>
  <div style="display:flex;align-items:center;gap:8px">
    <span class="ibadge">kontrola každých {CHECK_INTERVAL} min</span>
    <form method="post" action="/check_all">
      <button class="btn btns">↻ Zkontrolovat vše</button>
    </form>
  </div>
</div>
<div class="wrap">
  {tg_alert}
  <div class="addcard">
    <div class="addtitle">Přidat produkt</div>
    <form method="post" action="/add">
      <div class="row">
        <div class="fg fgl">
          <label>URL produktu na Alze</label>
          <input type="url" name="url" placeholder="https://www.alza.cz/nazev-produktu.htm" required>
        </div>
        <div class="fg">
          <label>Cílová cena (Kč) — nepovinné</label>
          <input type="number" name="target" placeholder="např. 15000" min="0" step="1">
        </div>
        <div class="fgb"><button class="btn btnr" type="submit">+ Přidat</button></div>
      </div>
    </form>
  </div>
  <div class="sh">
    <span class="st">Sledované produkty</span>
    <span style="font-size:12px;color:var(--m)">{len(rows)} celkem</span>
  </div>
  {cards_html}
</div>
</body></html>"""


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

    stats = f"""
    <div class="stats">
      <div class="sc"><div class="sl">Aktuální cena</div><div class="sv">{fmt(cur)}</div></div>
      <div class="sc"><div class="sl">Minimum</div><div class="sv" style="color:var(--g)">{fmt(minp)}</div></div>
      <div class="sc"><div class="sl">Maximum</div><div class="sv" style="color:var(--red)">{fmt(maxp)}</div></div>
      <div class="sc"><div class="sl">Změna celkem</div>
        <div class="sv" style="color:{'var(--g)' if chg and chg<0 else 'var(--red)' if chg and chg>0 else 'inherit'}">
          {('+' if chg and chg>0 else '')+fmt(chg) if chg is not None else '—'}
        </div>
      </div>
      <div class="sc"><div class="sl">Počet kontrol</div><div class="sv">{len(hist)}</div></div>
      <div class="sc"><div class="sl">Cílová cena</div><div class="sv">{fmt(p['target'])}</div></div>
    </div>"""

    rows_html = ""
    for i in range(len(hist)-1, -1, -1):
        row  = hist[i]
        prev = hist[i-1] if i > 0 else None
        if row.get("price") and prev and prev.get("price"):
            d = row["price"] - prev["price"]
            if d < 0:   chg_td = f'<span class="dn">▼ {fmt(abs(d))}</span>'
            elif d > 0: chg_td = f'<span class="up">▲ {fmt(d)}</span>'
            else:       chg_td = '<span class="neu">—</span>'
        else:
            chg_td = '<span class="neu">—</span>'
        alza_td   = '<span class="badge bf">🔥 Ano</span>' if row.get("alza_days") else "—"
        coupon_td = f'<span class="badge bco">{row["coupon"]}</span>' if row.get("coupon") else "—"
        rows_html += f"""<tr>
          <td>{row['checked_at']}</td>
          <td><b>{fmt(row.get('price'))}</b></td>
          <td>{chg_td}</td><td>{alza_td}</td><td>{coupon_td}</td>
        </tr>"""

    detail_css = """
    .stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:20px}
    .sc{background:var(--s);border:1px solid var(--b);border-radius:var(--r);padding:14px 16px}
    .sl{font-size:11px;color:var(--m);text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px}
    .sv{font-size:19px;font-weight:700}
    .chartcard{background:var(--s);border:1px solid var(--b);border-radius:var(--r);padding:20px;margin-bottom:20px}
    .chartcard h2{font-size:11px;font-weight:600;color:var(--m);text-transform:uppercase;letter-spacing:.05em;margin-bottom:16px}
    .chartwrap{height:260px;position:relative}
    .tc{background:var(--s);border:1px solid var(--b);border-radius:var(--r);overflow:hidden}
    .tc h2{font-size:11px;font-weight:600;color:var(--m);text-transform:uppercase;letter-spacing:.05em;padding:14px 18px;border-bottom:1px solid var(--b)}
    table{width:100%;border-collapse:collapse;font-size:13px}
    th{text-align:left;padding:9px 16px;font-size:11px;color:var(--m);text-transform:uppercase;
      letter-spacing:.04em;background:var(--bg);border-bottom:1px solid var(--b)}
    td{padding:10px 16px;border-bottom:1px solid var(--b)}
    tr:last-child td{border-bottom:none}
    tr:hover td{background:var(--bg)}
    """

    name_disp = p["name"] or p["url"]
    return f"""<!DOCTYPE html>
<html lang="cs"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name_disp} – Historie</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{CSS}{detail_css}</style>
</head><body>
<div class="bar">
  <a href="/" style="font-size:13px;color:var(--m)">← Zpět</a>
  <div style="font-size:14px;font-weight:600;flex:1;margin:0 12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{name_disp}</div>
  <a href="{p['url']}" target="_blank" style="font-size:12px;color:var(--bc)">Otevřít na Alze ↗</a>
</div>
<div class="wrap">
  {stats}
  <div class="chartcard">
    <h2>Vývoj ceny</h2>
    <div class="chartwrap"><canvas id="ch"></canvas></div>
  </div>
  <div class="tc">
    <h2>Záznamy</h2>
    <table>
      <thead><tr><th>Čas</th><th>Cena</th><th>Změna</th><th>AlzaDny</th><th>Kupon</th></tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</div>
<script>
const H={json.dumps(prices)};
const labels=H.map(h=>{{const d=new Date(h.checked_at);return d.toLocaleDateString('cs-CZ',{{day:'2-digit',month:'2-digit'}})+' '+d.toLocaleTimeString('cs-CZ',{{hour:'2-digit',minute:'2-digit'}});}});
const vals=H.map(h=>h.price);
const target={p['target'] or 'null'};
new Chart(document.getElementById('ch'),{{
  type:'line',
  data:{{labels,datasets:[
    {{label:'Cena (Kč)',data:vals,borderColor:'#e52213',borderWidth:2,
      backgroundColor:'rgba(229,34,19,0.06)',fill:true,tension:0.35,
      pointRadius:vals.length<30?4:2,pointBackgroundColor:'#e52213',pointHoverRadius:6}},
    ...(target?[{{label:'Cílová cena',data:vals.map(()=>target),
      borderColor:'#166534',borderWidth:1.5,borderDash:[5,4],pointRadius:0,fill:false}}]:[])
  ]}},
  options:{{responsive:true,maintainAspectRatio:false,
    interaction:{{intersect:false,mode:'index'}},
    plugins:{{
      legend:{{display:!!target,position:'top',labels:{{font:{{size:12}},boxWidth:18}}}},
      tooltip:{{callbacks:{{label:c=>`${{c.dataset.label}}: ${{c.parsed.y.toLocaleString('cs-CZ')}} Kč`}}}},
    }},
    scales:{{
      x:{{grid:{{display:false}},ticks:{{font:{{size:11}},maxTicksLimit:8}}}},
      y:{{grid:{{color:'rgba(0,0,0,0.04)'}},ticks:{{font:{{size:11}},callback:v=>v.toLocaleString('cs-CZ')+' Kč'}}}},
    }},
  }},
}});
</script>
</body></html>"""


@app.route("/health")
def health():
    return "ok"

@app.route("/api/history/<int:pid>")
def api_history(pid):
    c = get_db()
    rows = c.execute("SELECT checked_at,price,alza_days,coupon FROM history WHERE product_id=? AND price IS NOT NULL ORDER BY checked_at ASC",(pid,)).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

# ── Start ──────────────────────────────────────────────────────────────────────

init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(check_all, "interval", minutes=CHECK_INTERVAL)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
