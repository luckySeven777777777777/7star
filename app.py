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
                status TEXT DEFAULT '待取餐',
                created_at TEXT
            )
        """))
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS riders (
                id SERIAL PRIMARY KEY,
                name TEXT DEFAULT '',
                phone TEXT UNIQUE NOT NULL,
                order_no TEXT DEFAULT ''
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
                status TEXT DEFAULT '待取餐',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS riders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT DEFAULT '',
                phone TEXT UNIQUE NOT NULL,
                order_no TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL,
                phone TEXT NOT NULL,
                lat REAL NOT NULL,
                lng REAL NOT NULL,
                status TEXT DEFAULT '配送中',
                report_time TEXT
            );
        """)
    conn.commit()
    conn.close()


init_db()


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
        _q("INSERT INTO riders (name, phone, order_no) VALUES (?,?,?) "
           "ON CONFLICT(phone) DO UPDATE SET name=excluded.name, order_no=excluded.order_no"),
        (name, phone, order_no),
    )
    cur.execute(_q("UPDATE orders SET status='配送中' WHERE order_no=?"), (order_no,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "ချိတ်ဆက်မှု အောင်မြင်ပါသည်။ အမှာစာ ပို့ဆောင်နေပြီ"})


@app.route("/api/rider/location", methods=["POST"])
def report_location():
    """外卖员上报当前位置（手机浏览器GPS，或模拟配送）"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    status = (data.get("status") or "配送中").strip()
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
        _q("INSERT INTO locations (order_no, phone, lat, lng, status, report_time) VALUES (?,?,?,?,?,?)"),
        (order_no, phone, lat, lng, status, now_str()),
    )
    cur.execute(_q("UPDATE orders SET status=? WHERE order_no=?"), (status, order_no))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "တည်နေရာ တင်ပြီးပါပြီ"})


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
        _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? ORDER BY id ASC"),
        (order_no,),
    )
    points = cur.fetchall()
    conn.close()
    track_list = _rows_to_dicts(cur, points)
    latest = track_list[-1] if track_list else None
    order_dict = _row_to_dict(cur, order)
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "status": order_dict["status"],
            "address": order_dict["address"],
            "customer_name": order_dict["customer_name"],
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
