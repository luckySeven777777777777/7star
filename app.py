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
import urllib.request
import urllib.parse
from datetime import datetime
from flask import Flask, request, jsonify, render_template

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "tracking.db")

# Railway 会自动提供 DATABASE_URL；本地测试未设置时使用 SQLite
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = Flask(__name__)


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
                baozi_total INTEGER DEFAULT 0,
                shop_total INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                gps_enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT '待取餐',
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
    else:
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT UNIQUE NOT NULL,
                customer_name TEXT DEFAULT '',
                address TEXT DEFAULT '',
                delivery_address TEXT DEFAULT '',
                baozi_total INTEGER DEFAULT 0,
                shop_total INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                gps_enabled INTEGER DEFAULT 1,
                status TEXT DEFAULT '待取餐',
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
        """)
    conn.commit()
    conn.close()


init_db()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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


# ==================== 页面路由 ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/rider")
def rider_page():
    return render_template("rider.html")


@app.route("/track")
def track_page():
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
        _q("UPDATE orders SET status='配送中', delivery_address=?, baozi_total=?, shop_total=?, gps_enabled=1 WHERE order_no=?"),
        (delivery_address, baozi_total, shop_total, order_no),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "ချိတ်ဆက်မှု အောင်မြင်ပါသည်။ GPS ဖွင့်ပြီး အမှာစာ ပို့ဆောင်နေပြီ"})


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
    cur.execute(_q("UPDATE orders SET status=? WHERE order_no=?"), (status, order_no))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "တည်နေရာ တင်ပြီးပါပြီ"})


# ==================== 接口: 骑手确认收货 / 状态刷新 / GPS 开关 ====================
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
        _q("UPDATE orders SET baozi_received=?, shop_received=?, delivery_address=?, status='လက်ခံပြီးပါပြီ' WHERE order_no=?"),
        (new_baozi, new_shop, address, order_no),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "လက်ခံပြီးပါပြီ", "status": "လက်ခံပြီးပါပြီ", "baozi_received": new_baozi, "shop_received": new_shop})


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
    conn.close()
    latest = _row_to_dict(cur, latest_row) if latest_row else None
    if latest:
        latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "status": order_dict.get("status", ""),
            "delivery_address": order_dict.get("delivery_address", ""),
            "baozi_total": order_dict.get("baozi_total", 0),
            "shop_total": order_dict.get("shop_total", 0),
            "baozi_received": order_dict.get("baozi_received", 0),
            "shop_received": order_dict.get("shop_received", 0),
            "gps_enabled": order_dict.get("gps_enabled", 1),
            "latest": latest,
        }
    )


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
    conn.close()
    track_list = _rows_to_dicts(cur, points)
    latest = track_list[-1] if track_list else None
    if latest:
        latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
    order_dict = _row_to_dict(cur, order)
    rider_dict = _row_to_dict(cur, rider) if rider else None
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "status": order_dict["status"],
            "address": order_dict["address"],
            "delivery_address": order_dict.get("delivery_address") or order_dict["address"],
            "customer_name": order_dict["customer_name"],
            "rider_name": (rider_dict.get("rider_name") or rider_dict.get("name") or "") if rider_dict else "",
            "gps_enabled": order_dict.get("gps_enabled", 1),
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


if __name__ == "__main__":
    # Railway 通过 Procfile(gunicorn) 启动，不走这里；本地测试时使用
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("包子店外卖实时定位追踪系统（本地测试模式）")
    print("本地访问:  http://127.0.0.1:" + str(port))
    print("手机访问:  手机连同一 WiFi，访问 http://<电脑局域网IP>:" + str(port))
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False)
