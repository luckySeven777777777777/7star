# -*- coding: utf-8 -*-
"""
包子店外卖实时定位追踪系统（Railway 部署版）
=====================================
技术栈: Flask + PostgreSQL(Railway, 记录永久保存) / SQLite(本地测试回退) + Leaflet 地图

存储说明:
  Railway 部署时会自动注入 DATABASE_URL 环境变量 -> 自动使用 PostgreSQL，记录永久保存
  本地运行且没有 DATABASE_URL -> 自动使用 SQLite（仅供测试）

页面:
  /      首页
  /rider 外卖员端（上报定位）
  /track 客户/商家查询端（输入订单号 + 外卖员手机号 查实时位置）

部署:
  见 README.md（Railway + PostgreSQL 完整步骤）
"""

import os
import sqlite3
import json
import uuid
import urllib.request
import urllib.parse
import re
from datetime import datetime
from flask import Flask, request, jsonify, render_template, redirect
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "tracking.db")

# Railway 会自动提供 DATABASE_URL；本地测试未设置时使用 SQLite
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = Flask(__name__)

# ==================== Telegram 通知配置 ====================
# 机器人 Token 从环境变量 TELEGRAM_BOT_TOKEN 读取（Railway 部署时在 Dashboard 配置）
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "-1004317938803").strip()
TELEGRAM_BOT_ID = "9285749125"


def send_telegram_photo(photo_path, caption):
    """发送照片到 Telegram 群（multipart 上传，不依赖第三方库）"""
    if not TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN not configured"
    try:
        boundary = "----MarvisBoundary" + uuid.uuid4().hex
        with open(photo_path, "rb") as f:
            file_data = f.read()
        body = b""
        for k, v in (("chat_id", TELEGRAM_CHAT_ID), ("caption", caption)):
            body += ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, k, v)).encode("utf-8")
        body += ("--%s\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"proof.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" % boundary).encode("utf-8")
        body += file_data
        body += ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendPhoto" % TELEGRAM_BOT_TOKEN,
            data=body,
            headers={"Content-Type": "multipart/form-data; boundary=" + boundary},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True, "sent"
    except Exception as e:
        return False, str(e)


# ==================== 数据库连接 ====================
def get_db():
    if DATABASE_URL:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def is_pg():
    return bool(DATABASE_URL)


def _q(sql):
    """SQLite 用 ? 占位符，PostgreSQL 用 %s，统一转换"""
    return sql.replace("?", "%s") if is_pg() else sql


def _row_to_dict(cur, row):
    if row is None:
        return None
    if is_pg():
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))
    return dict(row)


def _rows_to_dicts(cur, rows):
    if not rows:
        return []
    if is_pg():
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in rows]
    return [dict(r) for r in rows]


def init_db():
    conn = get_db()
    cur = conn.cursor()
    if is_pg():
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                order_no TEXT UNIQUE NOT NULL,
                customer_name TEXT DEFAULT '',
                address TEXT DEFAULT '',
                delivery_address TEXT DEFAULT '',
                shop_lat DOUBLE PRECISION,
                shop_lng DOUBLE PRECISION,
                shop_link TEXT DEFAULT '',
                baozi_total INTEGER DEFAULT 0,
                shop_total INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                gps_enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT '待取餐',
                city TEXT DEFAULT '',
                created_at TEXT
            )
        """))
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS riders (
                id SERIAL PRIMARY KEY,
                name TEXT DEFAULT '',
                phone TEXT UNIQUE NOT NULL,
                order_no TEXT DEFAULT '',
                rider_name TEXT DEFAULT '',
                gps_enabled INTEGER DEFAULT 1,
                sim_mode INTEGER DEFAULT 0,
                created_at TEXT
            )
        """))
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS locations (
                id SERIAL PRIMARY KEY,
                order_no TEXT NOT NULL,
                phone TEXT NOT NULL,
                lat DOUBLE PRECISION NOT NULL,
                lng DOUBLE PRECISION NOT NULL,
                status TEXT DEFAULT '配送中',
                is_sim INTEGER DEFAULT 0,
                report_time TEXT
            )
        """))
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS order_history (
                id SERIAL PRIMARY KEY,
                order_no TEXT NOT NULL,
                rider_phone TEXT NOT NULL,
                rider_name TEXT DEFAULT '',
                bind_time TEXT
            )
        """))
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname TEXT DEFAULT '',
                role TEXT DEFAULT 'rider',
                status TEXT DEFAULT 'normal',
                avatar TEXT DEFAULT '',
                token TEXT DEFAULT '',
                created_at TEXT,
                phone TEXT DEFAULT '',
                status_updated_at TEXT,
                operator TEXT DEFAULT ''
            )
        """))
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                customer_name TEXT DEFAULT '',
                address TEXT DEFAULT '',
                delivery_address TEXT DEFAULT '',
                shop_lat REAL,
                shop_lng REAL,
                shop_link TEXT DEFAULT '',
                baozi_total INTEGER DEFAULT 0,
                shop_total INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                gps_enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT '待取餐',
                city TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS riders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                phone TEXT UNIQUE NOT NULL,
                order_no TEXT DEFAULT '',
                rider_name TEXT DEFAULT '',
                gps_enabled INTEGER DEFAULT 1,
                sim_mode INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL,
                phone TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                status TEXT DEFAULT '配送中',
                is_sim INTEGER DEFAULT 0,
                report_time TEXT
            );
            CREATE TABLE IF NOT EXISTS order_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL,
                rider_phone TEXT NOT NULL,
                rider_name TEXT DEFAULT '',
                bind_time TEXT
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                nickname TEXT DEFAULT '',
                role TEXT DEFAULT 'rider',
                status TEXT DEFAULT 'normal',
                avatar TEXT DEFAULT '',
                token TEXT DEFAULT '',
                created_at TEXT,
                phone TEXT DEFAULT '',
                status_updated_at TEXT,
                operator TEXT DEFAULT ''
            );
        """)
    conn.commit()
    conn.close()


init_db()


def ensure_user_columns():
    """旧库升级：为 users 表补充 phone / status_updated_at / operator 列（已存在则跳过）"""
    conn = get_db()
    cur = conn.cursor()
    cols = {
        "phone": "TEXT DEFAULT ''",
        "status_updated_at": "TEXT",
        "operator": "TEXT DEFAULT ''",
    }
    for col, ddl in cols.items():
        try:
            cur.execute(_q("ALTER TABLE users ADD COLUMN " + col + " " + ddl))
        except Exception:
            pass
    conn.commit()
    conn.close()


ensure_user_columns()


def ensure_order_columns():
    """旧库升级：为 orders 表补充 payment_method / amount 列（已存在则跳过）"""
    conn = get_db()
    cur = conn.cursor()
    cols = {
        "payment_method": "TEXT DEFAULT ''",
        "amount": "TEXT DEFAULT ''",
        "city": "TEXT DEFAULT ''",
        "shop_lat": "REAL",
        "shop_lng": "REAL",
        "shop_link": "TEXT DEFAULT ''",
    }
    for col, ddl in cols.items():
        try:
            cur.execute(_q("ALTER TABLE orders ADD COLUMN " + col + " " + ddl))
        except Exception:
            pass
    conn.commit()
    conn.close()


ensure_order_columns()


def parse_coords_text(text):
    """从用户输入文本提取 纬度,经度（支持英文逗号/缅甸逗号分隔）"""
    if not text:
        return None
    m = re.search(r"(-?\d{1,2}(?:\.\d+)?)\s*[,،]\s*(-?\d{1,3}(?:\.\d+)?)", str(text))
    if m:
        return (float(m.group(1)), float(m.group(2)))
    return None


def parse_link_coords(text):
    """从 Google Maps 链接提取坐标：@lat,lng / ?q=lat,lng / !3d..!4d"""
    s = str(text or "")
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    return None


def resolve_shop_location(link, coords_text):
    """骑手输入的商家位置解析：优先坐标文本，其次链接正则，最后展开 goo.gl 短链再解析。
    返回 (lat, lng, link)；解析失败时 lat/lng 为 None，仅保留链接原文。"""
    c = parse_coords_text(coords_text)
    if c:
        return c[0], c[1], (link or "").strip()
    if link:
        c = parse_link_coords(link)
        if c:
            return c[0], c[1], link.strip()
        try:
            req = urllib.request.Request(link, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=6) as resp:
                final_url = resp.geturl()
            c = parse_link_coords(final_url)
            if c:
                return c[0], c[1], link.strip()
        except Exception:
            pass
    return None, None, (link or "").strip()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_admin():
    """首次运行时自动创建超级管理员账号（admin / Admin@123456），登录后建议立即修改密码"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT COUNT(*) AS c FROM users WHERE role IN ('super_admin','admin')"))
    row = cur.fetchone()
    count = row[0] if is_pg() else row["c"]
    if count == 0:
        cur.execute(
            _q("INSERT INTO users (username, password_hash, nickname, role, status, created_at) VALUES (?,?,?,?,?,?)"),
            ("admin", generate_password_hash("Admin@123456"), "Super Admin", "super_admin", "normal", now_str()),
        )
        conn.commit()
        print("已自动创建超级管理员账号: admin / Admin@123456")
    conn.close()


ensure_admin()

# ==================== 反向地理编码（经纬度 -> 地名） ====================
_geo_cache = {}

def reverse_geocode(lat, lng):
    """将经纬度转换为可读地名（如 South Dagon - Min Bu Road），失败返回 None。
    使用 OpenStreetMap Nominatim 免费反向地理编码，带进程内缓存避免频繁请求。"""
    key = "%.5f,%.5f" % (lat, lng)
    if key in _geo_cache:
        return _geo_cache[key]
    try:
        url = "https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=17&addressdetails=1&lat=%s&lon=%s" % (lat, lng)
        req = urllib.request.Request(url, headers={"User-Agent": "Marvis-Delivery-Tracker/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        addr = data.get("address") or {}
        parts = []
        if addr.get("road"):
            parts.append(addr["road"])
        if addr.get("suburb"):
            parts.append(addr["suburb"])
        if addr.get("city"):
            parts.append(addr["city"])
        if addr.get("state"):
            parts.append(addr["state"])
        display = data.get("display_name") or ""
        if parts:
            name = ", ".join(parts)
        else:
            name = display.split(",")[0] if display else ""
        _geo_cache[key] = name or None
        return _geo_cache[key]
    except Exception:
        _geo_cache[key] = None
        return None


# ==================== 账号认证辅助 ====================
def get_current_user():
    """从请求头(Authorization)或 Cookie 中解析当前登录用户，未登录/被禁返回 None"""
    token = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if not token:
        token = (request.cookies.get("token") or "").strip()
    if not token:
        return None
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM users WHERE token=? AND status='normal'"), (token,))
    user = cur.fetchone()
    ud = _row_to_dict(cur, user) if user else None
    conn.close()
    return ud


def require_admin():
    """仅超级管理员/管理员可调用；否则返回 None"""
    u = get_current_user()
    if not u:
        return None
    if u.get("role") not in ("super_admin", "admin"):
        return None
    return u


# ==================== 页面路由 ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("login.html")


@app.route("/rider")
def rider_page():
    if not get_current_user():
        return redirect("/login?next=/rider")
    return render_template("rider.html")


@app.route("/track")
def track_page():
    if not get_current_user():
        return redirect("/login?next=/track")
    return render_template("track.html")


# ==================== 接口: 商家/订单 ====================
@app.route("/api/order/create", methods=["POST"])
def create_order():
    """商家创建订单，返回订单号（格式: BZ+时间，如 BZ1001102030）"""
    data = request.get_json(silent=True) or {}
    customer_name = (data.get("customer_name") or "").strip()
    address = (data.get("address") or "").strip()
    order_no = "BZ" + datetime.now().strftime("%m%d%H%M%S")
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("INSERT INTO orders (order_no, customer_name, address, status, created_at) VALUES (?,?,?,?,?)"),
        (order_no, customer_name, address, "待取餐", now_str()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "order_no": order_no, "msg": "အမှာစာ ဖန်တီးမှု အောင်မြင်ပါသည်"})


@app.route("/api/order/status", methods=["GET"])
def order_status():
    """商家查看订单当前状态"""
    order_no = (request.args.get("order_no") or "").strip()
    if not order_no:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    conn.close()
    if not order:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် မရှိပါ"}), 404
    return jsonify({"ok": True, "order": _row_to_dict(cur, order)})


# ==================== 接口: 外卖员 ====================
@app.route("/api/rider/bind", methods=["POST"])
def rider_bind():
    """外卖员绑定订单（手机号 + 订单号），绑定后才能上报位置"""
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    order_no = (data.get("order_no") or "").strip()
    rider_name = (data.get("rider_name") or name).strip()
    try:
        baozi_total = int(data.get("baozi_total") or 0)
        shop_total = int(data.get("shop_total") or 0)
    except (TypeError, ValueError):
        baozi_total = 0
        shop_total = 0
    delivery_address = (data.get("delivery_address") or "").strip()
    city = (data.get("city") or "").strip()
    shop_location_link = (data.get("shop_location_link") or "").strip()
    shop_location_coords = (data.get("shop_location_coords") or "").strip()
    shop_lat, shop_lng, shop_link = resolve_shop_location(shop_location_link, shop_location_coords)
    if not phone or not order_no:
        return jsonify({"ok": False, "msg": "ဖုန်းနံပါတ်နှင့် အမှာစာနံပါတ် နှစ်ခုလုံး ထည့်သွင်းပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် မရှိပါ။ ဦးစွာ အမှာစာ ဖန်တီးပါ"}), 404
    cur.execute(
        _q("INSERT INTO riders (name, phone, order_no, rider_name, gps_enabled, sim_mode) VALUES (?,?,?,?,1,0) "
           "ON CONFLICT(phone) DO UPDATE SET name=excluded.name, order_no=excluded.order_no, rider_name=excluded.rider_name, gps_enabled=1, sim_mode=0"),
        (name, phone, order_no, rider_name),
    )
    cur.execute(
        _q("UPDATE orders SET delivery_address=?, baozi_total=?, shop_total=?, city=?, gps_enabled=1, shop_lat=?, shop_lng=?, shop_link=? WHERE order_no=?"),
        (delivery_address, baozi_total, shop_total, city, shop_lat, shop_lng, shop_link, order_no),
    )
    # 永久订单历史：每次绑定订单都记入 order_history（不因刷新/重绑丢失）
    cur.execute(_q("SELECT id FROM order_history WHERE order_no=? AND rider_phone=?"), (order_no, phone))
    if cur.fetchone():
        cur.execute(
            _q("UPDATE order_history SET rider_name=?, bind_time=? WHERE order_no=? AND rider_phone=?"),
            (rider_name, now_str(), order_no, phone),
        )
    else:
        cur.execute(
            _q("INSERT INTO order_history (order_no, rider_phone, rider_name, bind_time) VALUES (?,?,?,?)"),
            (order_no, phone, rider_name, now_str()),
        )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "ချိတ်ဆက်မှု အောင်မြင်ပါသည်။ GPS ဖွင့်ပြီး အမှာစာ ပို့ဆောင်နေပြီ",
                    "shop_lat": shop_lat, "shop_lng": shop_lng, "shop_link": shop_link})


@app.route("/api/rider/location", methods=["POST"])
def report_location():
    """外卖员上报当前位置（手机浏览器GPS，或模拟配送）"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    status = (data.get("status") or "配送中").strip()
    is_sim = 1 if data.get("is_sim") else 0
    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "လတ္တီတွဒ်/လောင်ဂျီတွဒ် ပုံစံ မမှန်ပါ"}), 400
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM riders WHERE phone=? AND order_no=?"), (phone, order_no))
    rider = cur.fetchone()
    if not rider:
        conn.close()
        return jsonify({"ok": False, "msg": "ဤဖုန်းနံပါတ်သည် ဤအမှာစာနှင့် ချိတ်ဆက်မထားပါ။ ဦးစွာ ချိတ်ဆက်ပါ"}), 403
    cur.execute(
        _q("INSERT INTO locations (order_no, phone, lat, lng, status, is_sim, report_time) VALUES (?,?,?,?,?,?,?)"),
        (order_no, phone, lat, lng, status, is_sim, now_str()),
    )
    # 仅当快递员已选择并确认状态时才更新订单状态（未选择前保持待取餐，不显示配送中）
    if status:
        cur.execute(_q("UPDATE orders SET status=? WHERE order_no=?"), (status, order_no))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "တည်နေရာ တင်ပြီးပါပြီ"})


# ==================== 接口: 骑手确认收货 / 状态刷新 / GPS 开关 ====================
@app.route("/api/upload/proof", methods=["POST"])
def upload_proof():
    """骑手拍照上传收货凭证：保存到 static/uploads/proofs 并推送 Telegram 群"""
    order_no = (request.form.get("order_no") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    f = request.files.get("photo")
    if not f or not f.filename:
        return jsonify({"ok": False, "msg": "No photo uploaded"}), 400
    try:
        proofs_dir = os.path.join(app.static_folder or os.path.join(BASE_DIR, "static"), "uploads", "proofs")
        os.makedirs(proofs_dir, exist_ok=True)
        ext = os.path.splitext(f.filename or "")[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"):
            ext = ".jpg"
        fname = "proof_%s_%s%s" % (datetime.now().strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8], ext)
        path = os.path.join(proofs_dir, fname)
        f.save(path)
        # 查询订单 / 骑手 / 最新位置信息，组装通知内容
        order_dict = {}
        rider_name = ""
        latest = None
        conn = get_db()
        cur = conn.cursor()
        if order_no:
            cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
            o_row = cur.fetchone()
            if o_row:
                order_dict = _row_to_dict(cur, o_row)
        if order_no and phone:
            cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? AND rider_phone=? ORDER BY id DESC LIMIT 1"),
                        (order_no, phone))
            h_row = cur.fetchone()
            if h_row:
                rider_name = _row_to_dict(cur, h_row).get("rider_name", "") or ""
        if order_no:
            cur.execute(_q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id DESC LIMIT 1"),
                        (order_no,))
            lt_row = cur.fetchone()
            latest = _row_to_dict(cur, lt_row) if lt_row else None
            if latest:
                latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
        conn.close()
        baozi_total = int(order_dict.get("baozi_total") or 0)
        baozi_received = int(order_dict.get("baozi_received") or 0)
        shop_total = int(order_dict.get("shop_total") or 0)
        shop_received = int(order_dict.get("shop_received") or 0)
        loc_text = "-"
        if latest:
            loc_text = latest.get("address") or ("%.6f, %.6f" % (latest["lat"], latest["lng"]))
        caption = (
            "🚚 Delivery Proof\n"
            "📥Order: %s\n"
            "📲Rider Phone: %s\n"
            "👤Rider Name：%s\n"
            "🔍လက်ရှိအခြေအနေ：%s\n"
            "📍လက်ရှိနေရာ：%s\n"
            "🍙ကျန်ရှိသောပေါက်စီအလုံးအရေတွက်：%s / %s\n"
            "📝ကျန်ရှိသောဆိုင်အရေတွက်：%s / %s\n"
            "💳ငွေပေးချေမှုနည်းလမ်း：%s\n"
            "💵Amount (MMK)：%s"
        ) % (
            order_no or "-",
            phone or "-",
            rider_name,
            order_dict.get("status", "") or "",
            loc_text,
            baozi_total - baozi_received,
            baozi_total,
            shop_total - shop_received,
            shop_total,
            order_dict.get("payment_method", "") or "",
            order_dict.get("amount", "") or "",
        )
        tg_ok, tg_msg = send_telegram_photo(path, caption)
        return jsonify({
            "ok": True,
            "url": "/static/uploads/proofs/" + fname,
            "telegram": tg_ok,
            "telegram_msg": "sent" if tg_ok else tg_msg
        })
    except Exception as e:
        return jsonify({"ok": False, "msg": "Save failed: %s" % str(e)}), 500


@app.route("/api/rider/update-shop", methods=["POST"])
def rider_update_shop():
    """骑手绑定后补充/更新商家位置（Link 或 Coordinates），供地图标记与引导路线使用"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    link = (data.get("shop_location_link") or "").strip()
    coords_text = (data.get("shop_location_coords") or "").strip()
    if not order_no:
        return jsonify({"ok": False, "msg": "Order number required"}), 400
    shop_lat, shop_lng, shop_link = resolve_shop_location(link, coords_text)
    if shop_lat is None or shop_lng is None:
        return jsonify({"ok": False, "msg": "Cannot resolve shop location from link/coordinates"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("UPDATE orders SET shop_lat=?, shop_lng=?, shop_link=? WHERE order_no=?"),
                (shop_lat, shop_lng, shop_link, order_no))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "shop_lat": shop_lat, "shop_lng": shop_lng, "shop_link": shop_link})


@app.route("/api/rider/confirm", methods=["POST"])
def rider_confirm():
    """商家确认收货：接收确认包子数量与商家地址，扣减剩余数量并更新最新配送地址"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    try:
        confirm_baozi = int(data.get("confirm_baozi") or 0)
    except (TypeError, ValueError):
        confirm_baozi = 0
    address = (data.get("address") or "").strip()
    payment_method = (data.get("payment_method") or "").strip()
    amount = (data.get("amount") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM riders WHERE phone=? AND order_no=?"), (phone, order_no))
    rider = cur.fetchone()
    if not rider:
        conn.close()
        return jsonify({"ok": False, "msg": "ဤဖုန်းနံပါတ်သည် ဤအမှာစာနှင့် ချိတ်ဆက်မထားပါ"}), 403
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    order_dict = _row_to_dict(cur, order)
    new_baozi = (order_dict.get("baozi_received", 0) or 0) + confirm_baozi
    new_shop = (order_dict.get("shop_received", 0) or 0) + 1  # 每确认一个商家地址 +1
    cur.execute(
        _q("UPDATE orders SET baozi_received=?, shop_received=?, delivery_address=?, payment_method=?, amount=?, status='လက်ခံပြီးပါပြီ' WHERE order_no=?"),
        (new_baozi, new_shop, address, payment_method, amount, order_no),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "လက်ခံပြီးပါပြီ", "status": "လက်ခံပြီးပါပြီ", "baozi_received": new_baozi, "shop_received": new_shop, "payment_method": payment_method, "amount": amount})


@app.route("/api/rider/info", methods=["GET"])
def rider_info():
    """快递员端刷新最新位置与剩余数量"""
    order_no = (request.args.get("order_no") or "").strip()
    phone = (request.args.get("phone") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM riders WHERE phone=? AND order_no=?"), (phone, order_no))
    rider = cur.fetchone()
    if not rider:
        conn.close()
        return jsonify({"ok": False, "msg": "ချိတ်ဆက်မှု မရှိပါ"}), 403
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    order_dict = _row_to_dict(cur, order)
    cur.execute(
        _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id DESC LIMIT 1"),
        (order_no,),
    )
    latest_row = cur.fetchone()
    latest = _row_to_dict(cur, latest_row) if latest_row else None
    conn.close()
    if latest:
        latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "status": order_dict.get("status", ""),
            "delivery_address": order_dict.get("delivery_address", ""),
            "shop_lat": order_dict.get("shop_lat"),
            "shop_lng": order_dict.get("shop_lng"),
            "shop_link": order_dict.get("shop_link", "") or "",
            "city": order_dict.get("city", ""),
            "payment_method": order_dict.get("payment_method", ""),
            "amount": order_dict.get("amount", ""),
            "baozi_total": order_dict.get("baozi_total", 0),
            "shop_total": order_dict.get("shop_total", 0),
            "baozi_received": order_dict.get("baozi_received", 0),
            "shop_received": order_dict.get("shop_received", 0),
            "gps_enabled": order_dict.get("gps_enabled", 1),
            "latest": latest,
        }
    )


# ==================== 接口: 订单历史（永久记录） ====================
@app.route("/api/order/history", methods=["GET"])
def order_history_api():
    """骑手订单历史（永久记录，按手机号查询，刷新/重绑不丢失）"""
    phone = (request.args.get("phone") or "").strip()
    if not phone:
        return jsonify({"ok": False, "msg": "ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM order_history WHERE rider_phone=? ORDER BY id DESC"), (phone,))
    rows = cur.fetchall()
    history = []
    for row in rows:
        h = _row_to_dict(cur, row)
        order_no = h["order_no"]
        cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
        order = cur.fetchone()
        if not order:
            continue
        od = _row_to_dict(cur, order)
        cur.execute(
            _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id DESC LIMIT 1"),
            (order_no,),
        )
        latest_row = cur.fetchone()
        latest = _row_to_dict(cur, latest_row) if latest_row else None
        if latest:
            latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
        history.append(
            {
                "order_no": order_no,
                "rider_name": h.get("rider_name", "") or "",
                "rider_phone": h.get("rider_phone", "") or "",
                "bind_time": h.get("bind_time", "") or "",
                "status": od.get("status", "") or "",
                "delivery_address": od.get("delivery_address", "") or "",
                "city": od.get("city", "") or "",
                "payment_method": od.get("payment_method", "") or "",
                "amount": od.get("amount", "") or "",
                "baozi_total": od.get("baozi_total", 0) or 0,
                "shop_total": od.get("shop_total", 0) or 0,
                "baozi_received": od.get("baozi_received", 0) or 0,
                "shop_received": od.get("shop_received", 0) or 0,
                "baozi_remaining": (od.get("baozi_total", 0) or 0) - (od.get("baozi_received", 0) or 0),
                "shop_remaining": (od.get("shop_total", 0) or 0) - (od.get("shop_received", 0) or 0),
                "latest": latest,
            }
        )
    conn.close()
    return jsonify({"ok": True, "history": history})


@app.route("/api/rider/gps", methods=["POST"])
def rider_gps():
    """商家页开关快递员 GPS（rider 端绑定后自动开启且不可自行关闭）"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    gps_enabled = 1 if data.get("gps_enabled") else 0
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE orders SET gps_enabled=? WHERE order_no=?"),
        (gps_enabled, order_no),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "GPS အခြေအနေ ပြောင်းပြီးပါပြီ", "gps_enabled": gps_enabled})


# ==================== 接口: 客户/商家查询 ====================
@app.route("/api/track", methods=["GET"])
def track():
    """客户/商家查询: 订单号 + 外卖员手机号 -> 返回实时位置与轨迹"""
    order_no = (request.args.get("order_no") or "").strip()
    phone = (request.args.get("phone") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ်နှင့် ပို့ဆောင်သူ ဖုန်းနံပါတ် ထည့်သွင်းပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် မရှိပါ။ စစ်ဆေးပါ"}), 404
    cur.execute(_q("SELECT * FROM riders WHERE order_no=?"), (order_no,))
    rider = cur.fetchone()
    rider_dict = _row_to_dict(cur, rider) if rider else None
    if not rider_dict or rider_dict["phone"] != phone:
        conn.close()
        return jsonify({"ok": False, "msg": "ဖုန်းနံပါတ်နှင့် အမှာစာ မကိုက်ညီပါ။ တည်နေရာ ကြည့်ရှု၍ မရပါ"}), 403
    cur.execute(
        _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id ASC"),
        (order_no,),
    )
    points = cur.fetchall()
    track_list = _rows_to_dicts(cur, points)
    latest = track_list[-1] if track_list else None
    order_dict = _row_to_dict(cur, order)
    rider_dict = _row_to_dict(cur, rider) if rider else None
    conn.close()
    if latest:
        latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "status": order_dict["status"],
            "address": order_dict["address"],
            "delivery_address": order_dict.get("delivery_address") or order_dict["address"],
            "shop_lat": order_dict.get("shop_lat"),
            "shop_lng": order_dict.get("shop_lng"),
            "shop_link": order_dict.get("shop_link", "") or "",
            "city": order_dict.get("city", "") or "",
            "payment_method": order_dict.get("payment_method", "") or "",
            "amount": order_dict.get("amount", "") or "",
            "customer_name": order_dict["customer_name"],
            "rider_name": (rider_dict.get("rider_name") or rider_dict.get("name") or "") if rider_dict else "",
            "gps_enabled": order_dict.get("gps_enabled", 1),
            "created_at": order_dict.get("created_at", ""),
            "baozi_total": order_dict.get("baozi_total", 0),
            "shop_total": order_dict.get("shop_total", 0),
            "baozi_received": order_dict.get("baozi_received", 0),
            "shop_received": order_dict.get("shop_received", 0),
            "baozi_remaining": (order_dict.get("baozi_total", 0) or 0) - (order_dict.get("baozi_received", 0) or 0),
            "shop_remaining": (order_dict.get("shop_total", 0) or 0) - (order_dict.get("shop_received", 0) or 0),
            "latest": latest,
            "track": track_list,
            "update_time": latest["report_time"] if latest else None,
        }
    )


@app.route("/api/track/multi", methods=["POST"])
def track_multi():
    """多骑手同时查询: 一次 POST 多个 {order_no, phone, name}，返回每个骑手的位置与轨迹"""
    data = request.get_json(silent=True) or {}
    riders = data.get("riders") or []
    if not isinstance(riders, list) or len(riders) == 0:
        return jsonify({"ok": False, "msg": "Rider list is empty."}), 400
    conn = get_db()
    cur = conn.cursor()
    results = []
    for item in riders:
        order_no = (item.get("order_no") or "").strip()
        phone = (item.get("phone") or "").strip()
        if not order_no or not phone:
            results.append({"ok": False, "order_no": order_no, "msg": "Order Number + Phone are required."})
            continue
        cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
        order = cur.fetchone()
        if not order:
            results.append({"ok": False, "order_no": order_no, "msg": "အမှာစာနံပါတ် မရှိပါ။ စစ်ဆေးပါ"})
            continue
        order_dict = _row_to_dict(cur, order)
        cur.execute(_q("SELECT * FROM riders WHERE order_no=?"), (order_no,))
        rider = cur.fetchone()
        rider_dict = _row_to_dict(cur, rider) if rider else None
        if not rider_dict or rider_dict["phone"] != phone:
            results.append({"ok": False, "order_no": order_no, "msg": "ဖုန်းနံပါတ်နှင့် အမှာစာ မကိုက်ညီပါ။ တည်နေရာ ကြည့်ရှု၍ မရပါ"})
            continue
        cur.execute(
            _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id ASC"),
            (order_no,),
        )
        points = cur.fetchall()
        track_list = _rows_to_dicts(cur, points)
        latest = track_list[-1] if track_list else None
        # 批量接口不串行调用逆地理编码（10-20 个骑手时避免阻塞），前端以坐标兜底显示
        results.append(
            {
                "ok": True,
                "order_no": order_no,
                "status": order_dict["status"],
                "rider_name": (rider_dict.get("rider_name") or rider_dict.get("name") or "") if rider_dict else "",
                "city": order_dict.get("city", "") or "",
                "payment_method": order_dict.get("payment_method", "") or "",
                "amount": order_dict.get("amount", "") or "",
                "delivery_address": order_dict.get("delivery_address") or order_dict.get("address", ""),
                "shop_lat": order_dict.get("shop_lat"),
                "shop_lng": order_dict.get("shop_lng"),
                "shop_link": order_dict.get("shop_link", "") or "",
                "latest": latest,
                "track": track_list,
                "update_time": latest["report_time"] if latest else None,
            }
        )
    conn.close()
    return jsonify({"ok": True, "results": results})


# ==================== 接口: 登录 / 登出 / 当前用户 ====================
@app.route("/api/login", methods=["POST"])
def login_api():
    """骑手/管理员登录：校验账号密码，返回 token 并种 Cookie"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"ok": False, "msg": "Please enter User Name and Password."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM users WHERE username=?"), (username,))
    user = cur.fetchone()
    if not user:
        conn.close()
        return jsonify({"ok": False, "msg": "Incorrect User Name or Password."}), 401
    ud = _row_to_dict(cur, user)
    if not check_password_hash(ud["password_hash"], password):
        conn.close()
        return jsonify({"ok": False, "msg": "Incorrect User Name or Password."}), 401
    if ud.get("status") != "normal":
        conn.close()
        return jsonify({"ok": False, "msg": "Account is disabled. Please contact your administrator."}), 403
    # 页面角色隔离：rider 页仅允许骑手账号，track 页仅允许管理员账号
    page = (data.get("page") or "").strip()
    role = ud["role"]
    if page == "rider" and role != "rider":
        conn.close()
        return jsonify({"ok": False, "msg": "This page is for rider accounts only. Please login with a rider account."}), 403
    if page == "track" and role not in ("super_admin", "admin"):
        conn.close()
        return jsonify({"ok": False, "msg": "This page is for administrator accounts only. Please login with an administrator account."}), 403
    token = uuid.uuid4().hex
    cur.execute(_q("UPDATE users SET token=? WHERE id=?"), (token, ud["id"]))
    conn.commit()
    conn.close()
    resp = jsonify({
        "ok": True, "token": token,
        "username": ud["username"], "nickname": ud.get("nickname") or "",
        "avatar": ud.get("avatar") or "", "role": ud["role"],
    })
    resp.set_cookie("token", token, max_age=60 * 60 * 24 * 30, samesite="Lax")
    return resp


@app.route("/api/logout", methods=["POST"])
def logout_api():
    """登出：清除 token 与 Cookie"""
    user = get_current_user()
    if user:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(_q("UPDATE users SET token='' WHERE id=?"), (user["id"],))
        conn.commit()
        conn.close()
    resp = jsonify({"ok": True})
    resp.delete_cookie("token")
    return resp


@app.route("/api/user/me", methods=["GET"])
def user_me():
    """返回当前登录用户信息（前端用于头像/昵称/按钮显隐）。
    账号被禁止/删除时返回 403，前端据此强制退出登录。"""
    token = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    if not token:
        token = (request.cookies.get("token") or "").strip()
    if not token:
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM users WHERE token=?"), (token,))
    user = cur.fetchone()
    conn.close()
    if not user:
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    ud = _row_to_dict(cur, user)
    if ud.get("status") != "normal":
        return jsonify({"ok": False, "msg": "Account is disabled. Please contact your administrator."}), 403
    return jsonify({
        "ok": True,
        "username": ud["username"],
        "nickname": ud.get("nickname") or "",
        "avatar": ud.get("avatar") or "",
        "role": ud["role"],
        "phone": ud.get("phone") or "",
    })


@app.route("/api/user/avatar", methods=["POST"])
def upload_avatar():
    """上传头像（手机/电脑选图），保存到 static/uploads/avatars"""
    user = get_current_user()
    if not user:
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    f = request.files.get("avatar")
    if not f:
        return jsonify({"ok": False, "msg": "No file uploaded."}), 400
    ext = os.path.splitext(f.filename or "")[1].lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        ext = ".png"
    upload_dir = os.path.join(BASE_DIR, "static", "uploads", "avatars")
    os.makedirs(upload_dir, exist_ok=True)
    fname = user["username"] + "_" + uuid.uuid4().hex[:10] + ext
    f.save(os.path.join(upload_dir, fname))
    avatar_url = "/static/uploads/avatars/" + fname
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("UPDATE users SET avatar=? WHERE id=?"), (avatar_url, user["id"]))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "avatar": avatar_url})


# ==================== 接口: 管理员（超级管理员/管理员） ====================
@app.route("/api/admin/users", methods=["GET"])
def admin_users():
    """管理员查看骑手账号列表（已删除账号不显示）"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT id, username, nickname, phone, role, status, created_at, status_updated_at, operator FROM users WHERE role='rider' AND status!='deleted' ORDER BY id DESC"))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"ok": True, "users": _rows_to_dicts(cur, rows)})


@app.route("/api/admin/user/banned", methods=["GET"])
def admin_banned_users():
    """管理员查看禁止/删除账号记录列表（昵称+联系方式+状态+操作人+时间）"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT username, nickname, phone, status, status_updated_at, operator FROM users WHERE role='rider' AND status IN ('banned','deleted') ORDER BY status_updated_at DESC"))
    rows = cur.fetchall()
    conn.close()
    return jsonify({"ok": True, "records": _rows_to_dicts(cur, rows)})


@app.route("/api/admin/user/create", methods=["POST"])
def admin_create_user():
    """管理员创建骑手账号（User Name 唯一，昵称由管理员指定，骑手不可自改）"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    nickname = (data.get("nickname") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not username or not password:
        return jsonify({"ok": False, "msg": "User Name and Password are required."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT id FROM users WHERE username=?"), (username,))
    if cur.fetchone():
        conn.close()
        return jsonify({"ok": False, "msg": "User Name already exists."}), 400
    cur.execute(
        _q("INSERT INTO users (username, password_hash, nickname, role, status, created_at, phone) VALUES (?,?,?,?,?,?,?)"),
        (username, generate_password_hash(password), nickname or username, "rider", "normal", now_str(), phone),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "Rider account created.", "username": username, "nickname": nickname or username, "phone": phone})


@app.route("/api/admin/user/password", methods=["POST"])
def admin_reset_password():
    """管理员修改骑手登录密码（骑手忘记密码时使用）"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    new_password = data.get("new_password") or ""
    if not username or not new_password:
        return jsonify({"ok": False, "msg": "User Name and new Password are required."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT id FROM users WHERE username=? AND role='rider'"), (username,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"ok": False, "msg": "Rider account not found."}), 404
    cur.execute(
        _q("UPDATE users SET password_hash=? WHERE username=? AND role='rider'"),
        (generate_password_hash(new_password), username),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "Password updated."})


@app.route("/api/admin/user/status", methods=["POST"])
def admin_set_status():
    """管理员权限开关（单个骑手）：正常登录 / 禁止登录 / 删除账号，记录操作人与时间"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    status = (data.get("status") or "").strip()
    if not username or status not in ("normal", "banned", "deleted"):
        return jsonify({"ok": False, "msg": "Invalid parameters."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT id FROM users WHERE username=? AND role='rider'"), (username,))
    if not cur.fetchone():
        conn.close()
        return jsonify({"ok": False, "msg": "Rider account not found."}), 404
    cur.execute(
        _q("UPDATE users SET status=?, token='', status_updated_at=?, operator=? WHERE username=? AND role='rider'"),
        (status, now_str(), admin["username"], username),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "Account status updated."})


@app.route("/api/admin/user/status_all", methods=["POST"])
def admin_set_status_all():
    """管理员全局权限开关：一键作用于所有骑手账号。
    ban/delete 后所有骑手立即失效（token 清空），骑手端轮询 /api/user/me 检测到 403 后强制退出。"""
    admin = require_admin()
    if not admin:
        return jsonify({"ok": False, "msg": "Admin permission required. Please login with an administrator account."}), 403
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in ("normal", "banned", "deleted"):
        return jsonify({"ok": False, "msg": "Invalid parameters."}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE users SET status=?, token='', status_updated_at=?, operator=? WHERE role='rider'"),
        (status, now_str(), admin["username"]),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "All rider accounts set to " + status + "."})


if __name__ == "__main__":
    # Railway 通过 Procfile(gunicorn) 启动，不走这里；本地测试时使用
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("包子店外卖实时定位追踪系统（本地测试模式）")
    print("本地访问:  http://127.0.0.1:" + str(port))
    print("手机访问:  手机连同一 WiFi，访问 http://<电脑局域网IP>:" + str(port))
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False)
