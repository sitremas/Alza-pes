import sqlite3, requests, json, re, os, logging, time, threading
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__)
DB = os.environ.get("DB_PATH", "tracker.db")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL", "60"))

# ── Database ───────────────────────────────────────────────────────────────────

def get_db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with get_db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS products (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                url       TEXT UNIQUE NOT NULL,
                name      TEXT,
                target    REAL,
                active    INTEGER DEFAULT 1,
                added_at  TEXT DEFAULT (datetime('now','localtime'))
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
    "Accept-Encoding": "gzip, deflate, br",
}

def scrape(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Fetch failed {url}: {e}")
        return None

    soup = BeautifulSoup(r.text, "lxml")
    name, price = None, None

    # Name + price from JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            d = json.loads(script.string or "")
            offers = d.get("offers", {})
            if isinstance(offers, list):
                offers = offers[0]
            raw = offers.get("price")
            if raw:
                price = float(str(raw).replace(",", ".").replace("\xa0", "").strip())
                name = d.get("name", "")[:120]
                break
        except Exception:
            continue

    # Price from meta tag
    if not price:
        meta = soup.find("meta", itemprop="price")
        if meta:
            try:
                price = float(meta["content"].replace(",", "."))
            except Exception:
                pass

    # Price from CSS classes
    if not price:
        for cls in ["price-box__price", "js-product-price", "pricebox__price"]:
            el = soup.find(class_=cls)
            if el:
                nums = re.findall(r"[\d]+(?:[.,]\d+)?", el.get_text().replace("\xa0", "").replace(" ", ""))
                for n in nums:
                    try:
                        v = float(n.replace(",", "."))
                        if v > 10:
                            price = v
                            break
                    except Exception:
                        continue
            if price:
                break

    # Name fallback
    if not name:
        h1 = soup.find("h1")
        if h1:
            name = h1.get_text(strip=True)[:120]

    page_lower = r.text.lower()
    alza_days = any(k in page_lower for k in ["alzadny", "alza dny", "alza-dny", "alzaday"])

    coupon = None
    for cls in ["coupon", "voucher", "kupon", "promo-code", "discount-code"]:
        el = soup.find(class_=re.compile(cls, re.I))
        if el:
            txt = el.get_text(strip=True)[:60]
            if txt:
                coupon = txt
                break

    return {"name": name, "price": price, "alza_days": alza_days, "coupon": coupon}

# ── Telegram ───────────────────────────────────────────────────────────────────

def tg(text, parse_mode="HTML"):
    token = TELEGRAM_TOKEN
    chat_id = TELEGRAM_CHAT_ID
    if not token or not chat_id:
        log.warning("Telegram not configured")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Telegram error: {e}")

# ── Price check ────────────────────────────────────────────────────────────────

def check_product(pid):
    c = get_db()
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p or not p["active"]:
        c.close()
        return

    result = scrape(p["url"])
    if not result:
        c.close()
        return

    price = result["price"]
    name = result["name"] or p["name"] or "Produkt"

    if result["name"] and result["name"] != p["name"]:
        c.execute("UPDATE products SET name=? WHERE id=?", (result["name"], pid))

    c.execute(
        "INSERT INTO history (product_id, price, alza_days, coupon) VALUES (?,?,?,?)",
        (pid, price, 1 if result["alza_days"] else 0, result["coupon"]),
    )
    c.commit()

    if price is None:
        c.close()
        return

    prev = c.execute(
        "SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1",
        (pid,),
    ).fetchone()
    c.close()

    url_short = p["url"][:60]

    # Pokles ceny
    if prev and prev["price"] and price < prev["price"]:
        diff = prev["price"] - price
        tg(
            f"📉 <b>Pokles ceny!</b>\n"
            f"{name}\n\n"
            f"<b>{price:,.0f} Kč</b>  (↓ {diff:,.0f} Kč)\n"
            f"<a href='{p['url']}'>Otevřít na Alze</a>"
        )

    # Cílová cena
    if p["target"] and price <= p["target"]:
        tg(
            f"🎯 <b>Cílová cena dosažena!</b>\n"
            f"{name}\n\n"
            f"Cena <b>{price:,.0f} Kč</b> ≤ tvůj limit {p['target']:,.0f} Kč\n"
            f"<a href='{p['url']}'>Otevřít na Alze</a>"
        )

    # AlzaDny
    if result["alza_days"]:
        tg(
            f"🔥 <b>AlzaDny jsou aktivní!</b>\n"
            f"{name}\n\n"
            f"Aktuální cena: <b>{price:,.0f} Kč</b>\n"
            f"<a href='{p['url']}'>Otevřít na Alze</a>"
        )

    # Kupon
    if result["coupon"]:
        tg(
            f"🎫 <b>Nalezen kupon!</b>\n"
            f"{name}\n\n"
            f"Kód: <code>{result['coupon']}</code>\n"
            f"Cena: <b>{price:,.0f} Kč</b>\n"
            f"<a href='{p['url']}'>Otevřít na Alze</a>"
        )

    log.info(f"[{name}] {price} Kč | AlzaDny={result['alza_days']} | Kupon={result['coupon']}")

def check_all():
    c = get_db()
    ids = [r["id"] for r in c.execute("SELECT id FROM products WHERE active=1").fetchall()]
    c.close()
    log.info(f"Scheduled check — {len(ids)} products")
    for pid in ids:
        check_product(pid)
        time.sleep(4)

# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    c = get_db()
    products = c.execute("SELECT * FROM products ORDER BY added_at DESC").fetchall()
    data = []
    for p in products:
        latest = c.execute(
            "SELECT * FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1", (p["id"],)
        ).fetchone()
        prev = c.execute(
            "SELECT price FROM history WHERE product_id=? ORDER BY id DESC LIMIT 1 OFFSET 1", (p["id"],)
        ).fetchone()
        lowest = c.execute(
            "SELECT MIN(price) as m FROM history WHERE product_id=? AND price IS NOT NULL", (p["id"],)
        ).fetchone()
        checks = c.execute(
            "SELECT COUNT(*) as n FROM history WHERE product_id=?", (p["id"],)
        ).fetchone()
        change = None
        if latest and latest["price"] and prev and prev["price"]:
            change = latest["price"] - prev["price"]
        data.append({
            "p": dict(p),
            "latest": dict(latest) if latest else None,
            "lowest": lowest["m"] if lowest else None,
            "change": change,
            "checks": checks["n"],
        })
    c.close()
    tg_ok = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    return render_template("index.html", data=data, tg_ok=tg_ok,
                           interval=CHECK_INTERVAL_MINUTES)

@app.route("/add", methods=["POST"])
def add():
    url = request.form.get("url", "").strip()
    target = request.form.get("target", "").strip()
    if not url:
        return redirect(url_for("index"))
    target_val = float(target) if target else None
    c = get_db()
    try:
        c.execute("INSERT INTO products (url, target) VALUES (?,?)", (url, target_val))
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
    c.commit()
    c.close()
    return redirect(url_for("index"))

@app.route("/toggle/<int:pid>", methods=["POST"])
def toggle(pid):
    c = get_db()
    c.execute("UPDATE products SET active = 1 - active WHERE id=?", (pid,))
    c.commit()
    c.close()
    return redirect(url_for("index"))

@app.route("/check/<int:pid>", methods=["POST"])
def check_now(pid):
    threading.Thread(target=check_product, args=(pid,), daemon=True).start()
    return redirect(url_for("index"))

@app.route("/product/<int:pid>")
def detail(pid):
    c = get_db()
    p = c.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return redirect(url_for("index"))
    rows = c.execute(
        "SELECT * FROM history WHERE product_id=? ORDER BY checked_at ASC", (pid,)
    ).fetchall()
    c.close()
    return render_template("detail.html", p=dict(p), history=[dict(r) for r in rows])

@app.route("/api/history/<int:pid>")
def api_history(pid):
    c = get_db()
    rows = c.execute(
        "SELECT checked_at, price, alza_days, coupon FROM history "
        "WHERE product_id=? AND price IS NOT NULL ORDER BY checked_at ASC", (pid,)
    ).fetchall()
    c.close()
    return jsonify([dict(r) for r in rows])

@app.route("/health")
def health():
    return "ok"

# ── Start ──────────────────────────────────────────────────────────────────────

init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(check_all, "interval", minutes=CHECK_INTERVAL_MINUTES)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
