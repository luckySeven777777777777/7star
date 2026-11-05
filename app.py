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
from datetime import datetime, timedelta
try:
    from zoneinfo import ZoneInfo
    _YANGON = ZoneInfo("Asia/Yangon")
except Exception:
    _YANGON = None

def to_yangon_text(ts):
    if not ts: return ts or ""
    if "06:30" in str(ts): return str(ts)
    try:
        s = str(ts)
        fmt = "%Y-%m-%d %H:%M:%S" if "T" not in s else "%Y-%m-%dT%H:%M:%S"
        dt = datetime.strptime(s[:19], fmt)
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        if _YANGON:
            return dt.astimezone(_YANGON).strftime("%Y-%m-%d %H:%M:%S")
        return (dt + timedelta(hours=6, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(ts)

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


def send_telegram_text(text):
    """发送纯文本到 Telegram 群（无照片时也能发通知，与照片通知互为双保险）"""
    if not TELEGRAM_BOT_TOKEN:
        return False, "TELEGRAM_BOT_TOKEN not configured"
    try:
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True, "sent"
    except Exception as e:
        return False, str(e)


def build_proof_caption(order_no, phone, order_dict, rider_name, latest):
    """组装 Telegram 通知内容：订单信息 + 扣减后的剩余数量 + 最新位置（与订单记录一致）"""
    baozi_total, baozi_received, shop_total, shop_received = order_counts(order_dict)
    loc_text = "-"
    if latest:
        loc_text = latest.get("address") or ("%.6f, %.6f" % (latest["lat"], latest["lng"]))
    return (
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
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id SERIAL PRIMARY KEY,
                order_no TEXT NOT NULL,
                phone TEXT DEFAULT '',
                type TEXT DEFAULT 'order',
                message TEXT DEFAULT '',
                created_at TEXT
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
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL,
                phone TEXT DEFAULT '',
                type TEXT DEFAULT 'order',
                message TEXT DEFAULT '',
                created_at TEXT
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
        "completed_at": "TEXT",
        # 骑手最初输入的原始总量：只写一次，之后永不参与扣减，显示分母永远用它
        "baozi_total_orig": "INTEGER DEFAULT 0",
        "shop_total_orig": "INTEGER DEFAULT 0",
    }
    for col, ddl in cols.items():
        try:
            cur.execute(_q("ALTER TABLE orders ADD COLUMN " + col + " " + ddl))
        except Exception:
            pass
    # 一次性迁移：把现有总量固化为原始输入量（之后 total 与本列都不再被扣减）
    for _col in ("baozi_total", "shop_total"):
        try:
            cur.execute(_q(
                "UPDATE orders SET " + _col + "_orig = " + _col +
                " WHERE (" + _col + "_orig IS NULL OR " + _col + "_orig = 0) AND " + _col + " > 0"
            ))
        except Exception:
            pass
    conn.commit()
    conn.close()


ensure_order_columns()


def _to_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def order_counts(od):
    """统一数量口径：总量（分母）永远取骑手最初输入量，绝不被扣减；remaining = 总量 - 已收"""
    total_bt = _to_int(od.get("baozi_total"))
    total_st = _to_int(od.get("shop_total"))
    orig_bt = _to_int(od.get("baozi_total_orig"))
    orig_st = _to_int(od.get("shop_total_orig"))
    br = _to_int(od.get("baozi_received"))
    sr = _to_int(od.get("shop_received"))
    # 分母 = 原始输入量的最大值：老数据 orig 缺失或曾被扣减时，用 total/received 兜底，绝不小于任何已知量
    bo = max(orig_bt, total_bt, br)
    so = max(orig_st, total_st, sr)
    return bo, br, so, sr


def orig_counts(od):
    """独立返回原始总量（分母专用），与已收数量无关"""
    bo, br, so, sr = order_counts(od)
    return bo, so


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
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"[?&]q=(-?\d+\.\d+),(-?\d+\.\d+)", s)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", s)
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
    """统一返回缅甸仰光时间（UTC+6:30）文本，与服务器所在时区无关。
    订单时间、完成时间、通知时间均基于它写入，前端直接显示即为仰光时间。"""
    try:
        if _YANGON:
            return datetime.now(_YANGON).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    return (datetime.utcnow() + timedelta(hours=6, minutes=30)).strftime("%Y-%m-%d %H:%M:%S")


def utc_iso_now():
    """通知专用：UTC ISO8601（带 Z 后缀），前端按缅甸时区 UTC+6:30 换算显示，与服务器时区无关"""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


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
# 登录态域：admin=商家管理后台(/track)，rider=骑手端(/rider)。Cookie 也按域分开，彻底避免互相顶号
AUTH_COOKIE_ADMIN = "admin_token"
AUTH_COOKIE_RIDER = "rider_token"

# 仰光市中心默认坐标：订单无任何坐标时用于兜底显示标记，保证地图标记不丢失
DEFAULT_MAP_LAT = 16.8661
DEFAULT_MAP_LNG = 96.1951


def _page_scope():
    """从请求头 X-Page（/track 商家、/rider 骑手）或查询参数解析登录域。
    前端两个页面都会显式带上该头，后端据此只认本域 cookie，彻底避免串号。"""
    p = (request.headers.get("X-Page") or request.args.get("page") or "").strip().lower()
    if p in ("track", "admin"):
        return "admin"
    if p == "rider":
        return "rider"
    return None


def _token_candidates(scope=None):
    """按登录域给出 token 候选顺序（去重）：
    商家域 admin_token → Authorization；骑手域 rider_token → Authorization；
    未指定域则 Authorization → admin_token → rider_token → 老版 token。"""
    scope = scope or _page_scope()
    auth = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip()
    ck_admin = (request.cookies.get(AUTH_COOKIE_ADMIN) or "").strip()
    ck_rider = (request.cookies.get(AUTH_COOKIE_RIDER) or "").strip()
    ck_old = (request.cookies.get("token") or "").strip()
    if scope == "rider":
        seq = [ck_rider, auth]
    elif scope == "admin":
        seq = [ck_admin, auth]
    else:
        seq = [auth, ck_admin, ck_rider, ck_old]
    out = []
    for t in seq:
        if t and t not in out:
            out.append(t)
    return out


def _token_from_request(scope=None):
    """取该域的首选 token；域明确时以该域专属 Cookie 为准，Authorization 兜底。"""
    seq = _token_candidates(scope)
    return seq[0] if seq else ""


def get_current_user(scope=None, allow_any_role=False):
    """从请求头(Authorization)或按域 Cookie 解析当前登录用户，未登录/被禁返回 None。
    scope: 'admin' 仅商家/管理员域，'rider' 仅骑手域；None 表示任一域。
    allow_any_role: 为 True 时不校验角色与域是否匹配（页面壳层放行用）。
    同一浏览器里商家与骑手各持独立 token、各自独立 Cookie，本函数只在本域内查找，
    绝不会把骑手的登录态当成商家登录态。"""
    scope = scope or _page_scope()
    for token in _token_candidates(scope):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(_q("SELECT * FROM users WHERE token=? AND status='normal'"), (token,))
        user = cur.fetchone()
        ud = _row_to_dict(cur, user) if user else None
        conn.close()
        if not ud:
            continue
        if allow_any_role:
            return ud
        role = ud.get("role")
        if scope == "rider" and role not in ("rider", "normal"):
            continue
        if scope == "admin" and role not in ("super_admin", "admin"):
            continue
        return ud
    return None



def require_admin():
    """仅超级管理员/管理员可调用；否则返回 None"""
    u = get_current_user(_page_scope() or "admin")
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
    # 只认骑手域（rider_token）：商家登录态绝不能放行进骑手页面，反之亦然
    if not get_current_user("rider"):
        return redirect("/login?next=/rider")
    return render_template("rider.html")


@app.route("/track")
def track_page():
    # 只认商家域（admin_token）：骑手登录态绝不能放行进商家后台
    if not get_current_user("admin"):
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
    cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                (order_no, "", "order", "新订单：" + order_no + "（客户 " + (customer_name or "-") + "）", utc_iso_now()))
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
    # 数量口径：分母 = 骑手最初输入的原始总量，写入 *_total_orig 后永久固定；
    # 之后任何提交（包括剩余量回填）都不得覆盖它，只有「已收数量」参与扣减
    old = _row_to_dict(cur, order)
    old_bt = int(old.get("baozi_total") or 0)
    old_br = int(old.get("baozi_received") or 0)
    old_st = int(old.get("shop_total") or 0)
    old_sr = int(old.get("shop_received") or 0)
    old_bt_orig = int(old.get("baozi_total_orig") or 0)
    old_st_orig = int(old.get("shop_total_orig") or 0)
    baozi_orig = old_bt_orig or old_bt or 0
    shop_orig = old_st_orig or old_st or 0
    # 分母只增不减：骑手重新提交更大的原始总量时以新值为准（可自愈历史被误扣的分母），
    # 提交较小值/空值一律不覆盖，保证「200/200」永远不被扣成 196/196
    if baozi_total > baozi_orig:
        baozi_orig = baozi_total
    if shop_total > shop_orig:
        shop_orig = shop_total
    baozi_total = baozi_orig
    shop_total = shop_orig
    cur.execute(
        _q("UPDATE orders SET delivery_address=?, baozi_total=?, shop_total=?, baozi_total_orig=?, shop_total_orig=?, city=?, gps_enabled=1, shop_lat=?, shop_lng=?, shop_link=? WHERE order_no=?"),
        (delivery_address, baozi_total, shop_total, baozi_orig, shop_orig, city, shop_lat, shop_lng, shop_link, order_no),
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
    cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                (order_no, phone, "bind", "骑手已接单：" + order_no + "（骑手 " + (rider_name or phone) + "）", utc_iso_now()))
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
        # 仅保存照片，不发送 Telegram；通知在骑手点击 Confirm ✅ 后由 /api/rider/confirm 发送
        return jsonify({
            "ok": True,
            "url": "/static/uploads/proofs/" + fname,
            "telegram": False,
            "telegram_msg": "pending confirm"
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


@app.route("/api/rider/active", methods=["GET"])
def rider_active():
    """骑手未完成任务恢复：查询该骑手最近一条未完成的订单"""
    phone = (request.args.get("phone") or "").strip()
    if not phone:
        return jsonify({"ok": False, "msg": "phone required"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT h.order_no, h.rider_name, o.* FROM order_history h JOIN orders o ON h.order_no=o.order_no JOIN riders r ON r.phone=? AND r.order_no=o.order_no WHERE h.rider_phone=? AND (o.status IS NULL OR o.status='' OR (o.status != 'လက်ခံပြီးပါပြီ' AND o.status != 'ပယ်ဖျက်ထားသည်')) ORDER BY h.id DESC LIMIT 1"), (phone, phone))
    row = cur.fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": True, "active": None})
    od = _row_to_dict(cur, row)
    conn.close()
    return jsonify({"ok": True, "active": {
        "order_no": od.get("order_no"), "rider_name": od.get("rider_name", "") or "",
        "status": od.get("status", "") or "", "city": od.get("city", "") or "",
        "shop_lat": od.get("shop_lat"), "shop_lng": od.get("shop_lng"),
        "shop_link": od.get("shop_link", "") or "", "gps_enabled": od.get("gps_enabled", 1),
        "baozi_total": od.get("baozi_total", 0) or 0, "baozi_received": od.get("baozi_received", 0) or 0,
        "shop_total": od.get("shop_total", 0) or 0, "shop_received": od.get("shop_received", 0) or 0,
        "amount": od.get("amount", "") or "", "payment_method": od.get("payment_method", "") or "",
        "delivery_address": od.get("delivery_address", "") or od.get("address", "") or ""
    }})


@app.route("/api/rider/confirm", methods=["POST"])
def rider_confirm():
    """商家确认收货：接收确认包子数量与商家地址，扣减剩余数量并更新最新配送地址；确认后推送 Telegram 照片通知"""
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
    proof_url = (data.get("proof_url") or "").strip()
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
        _q("UPDATE orders SET baozi_received=?, shop_received=?, delivery_address=?, payment_method=?, amount=?, status='လက်ခံပြီးပါပြီ', completed_at=? WHERE order_no=?"),
        (new_baozi, new_shop, address, payment_method, amount, now_str(), order_no),
    )
    conn.commit()
    # 通知 + Telegram：无论是否有照片都发文字通知，有照片再发照片（双保险，避免静默失败）
    cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? AND rider_phone=? ORDER BY id DESC LIMIT 1"),
                (order_no, phone))
    h_row = cur.fetchone()
    rider_name = ""
    if h_row:
        rider_name = _row_to_dict(cur, h_row).get("rider_name", "") or ""
    cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                (order_no, phone, "done", "订单已完成：" + order_no + "（骑手 " + (rider_name or phone) + "）", utc_iso_now()))
    conn.commit()
    tg_ok, tg_msg = False, "no photo"
    text_sent = False
    if TELEGRAM_BOT_TOKEN:
        cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
        od_row = cur.fetchone()
        od = _row_to_dict(cur, od_row)
        latest = None
        cur.execute(_q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id DESC LIMIT 1"),
                    (order_no,))
        lt_row = cur.fetchone()
        if lt_row:
            latest = _row_to_dict(cur, lt_row)
            latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
        text_caption = ("✅ အော်ဒါ ပြီးဆုံးပါပြီ\n"
                        "📥Order: %s\n📲Rider Phone: %s\n👤Rider Name: %s\n"
                        "📍လိပ်စာ: %s\n💵Amount (MMK): %s") % (
            order_no or "-", phone or "-", rider_name or "-",
            (od.get("delivery_address") or od.get("address") or "-"),
            (od.get("amount") or "-"))
        t_ok, t_msg = send_telegram_text(text_caption)
        text_sent = t_ok
        if proof_url:
            photo_rel = proof_url.lstrip("/")
            photo_path = os.path.join(BASE_DIR, photo_rel.replace("/", os.sep))
            if os.path.exists(photo_path):
                caption = build_proof_caption(order_no, phone, od, rider_name, latest)
                tg_ok, tg_msg = send_telegram_photo(photo_path, caption)
            else:
                tg_ok, tg_msg = False, "photo file missing"
    if not tg_ok and text_sent:
        tg_ok, tg_msg = True, "text sent"
    conn.close()
    _rem_bt_all, _, _rem_st_all, _ = order_counts(order_dict)
    _rem_bt = max(_rem_bt_all - new_baozi, 0)
    _rem_st = max(_rem_st_all - new_shop, 0)
    return jsonify({"ok": True, "msg": "လက်ခံပြီးပါပြီ", "status": "လက်ခံပြီးပါပြီ", "completed_at": now_str(), "baozi_received": new_baozi, "baozi_remaining": _rem_bt, "baozi_total_orig": _rem_bt_all, "shop_remaining": _rem_st, "shop_total_orig": _rem_st_all, "shop_received": new_shop, "payment_method": payment_method, "amount": amount, "telegram": tg_ok, "telegram_msg": tg_msg})


@app.route("/api/rider/cancel", methods=["POST"])
def rider_cancel():
    """骑手取消订单：标记订单已取消并解除骑手绑定，保留订单历史记录（不扣减任何数量字段）"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM riders WHERE phone=? AND order_no=?"), (phone, order_no))
    rider = cur.fetchone()
    rider_name = ""
    if rider:
        rd = _row_to_dict(cur, rider)
        rider_name = rd.get("rider_name", "") or ""
    else:
        # 无当前绑定：若该手机号历史确有此订单（已解绑残留），仍允许取消以解除死锁
        cur.execute(_q("SELECT id FROM order_history WHERE order_no=? AND rider_phone=?"), (order_no, phone))
        if not cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "msg": "ဤဖုန်းနံပါတ်သည် ဤအမှာစာနှင့် ချိတ်ဆက်မထားပါ"}), 403
    cur.execute(_q("UPDATE orders SET status='ပယ်ဖျက်ထားသည်', completed_at=? WHERE order_no=?"), (now_str(), order_no))
    cur.execute(_q("UPDATE riders SET order_no='', rider_name='' WHERE phone=? AND order_no=?"), (phone, order_no))
    # 保留订单历史：存在则更新骑手名与绑定时间，不存在则补记一条
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
    cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                (order_no, phone, "cancel", "订单已取消：" + order_no + "（骑手 " + (rider_name or phone) + "）", utc_iso_now()))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "msg": "အော်ဒါ ပယ်ဖျက်ပြီးပါပြီ", "status": "ပယ်ဖျက်ထားသည်"})


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
    _s_bt, _s_br, _s_st, _s_sr = order_counts(order_dict)
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
            "baozi_total": _s_bt,
            "shop_total": _s_st,
            "baozi_received": _s_br,
            "shop_received": _s_sr,
            "baozi_remaining": _s_bt - _s_br,
            "shop_remaining": _s_st - _s_sr,
            "baozi_total_orig": _s_bt,
            "shop_total_orig": _s_st,
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
        _h_bt, _h_br, _h_st, _h_sr = order_counts(od)
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
                "baozi_total": _h_bt,
                "shop_total": _h_st,
                "baozi_received": _h_br,
                "shop_received": _h_sr,
                "baozi_remaining": _h_bt - _h_br,
                "shop_remaining": _h_st - _h_sr,
                "baozi_total_orig": _h_bt,
                "shop_total_orig": _h_st,
                "shop_link": od.get("shop_link", "") or "",
                "shop_lat": od.get("shop_lat"),
                "shop_lng": od.get("shop_lng"),
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
    rider_name_fallback = ""
    if not rider_dict or rider_dict["phone"] != phone:
        st0 = _row_to_dict(cur, order).get("status", "")
        # 取消/完成订单骑手已解绑：放行最终状态查看，路线由前端清除
        if st0 not in ("ပယ်ဖျက်ထားသည်", "လက်ခံပြီးပါပြီ"):
            conn.close()
            return jsonify({"ok": False, "msg": "ဖုန်းနံပါတ်နှင့် အမှာစာ မကိုက်ညီပါ။ တည်နေရာ ကြည့်ရှု၍ မရပါ"}), 403
        cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? ORDER BY id DESC LIMIT 1"), (order_no,))
        hn = cur.fetchone()
        if hn:
            rider_name_fallback = _row_to_dict(cur, hn).get("rider_name", "") or ""
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
    _d_bt, _d_br, _d_st, _d_sr = order_counts(order_dict)
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
            "rider_name": (rider_name_fallback or ((rider_dict.get("rider_name") or rider_dict.get("name") or "") if rider_dict else "")),
            "gps_enabled": order_dict.get("gps_enabled", 1),
            "created_at": order_dict.get("created_at", ""),
            "completed_at": order_dict.get("completed_at", "") or "",
            "baozi_total": _d_bt,
            "shop_total": _d_st,
            "baozi_received": _d_br,
            "shop_received": _d_sr,
            "baozi_remaining": _d_bt - _d_br,
            "shop_remaining": _d_st - _d_sr,
            "baozi_total_orig": _d_bt,
            "shop_total_orig": _d_st,
            "latest": latest,
            "track": track_list,
            "update_time": latest["report_time"] if latest else None,
        }
    )


@app.route("/api/admin/notifications", methods=["GET"])
def admin_notifications():
    """商家端通知中心：after_id 之后的新通知增量返回（前端轮询去重，新订单/接单/完成/取消）"""
    try:
        after_id = int(request.args.get("after_id") or 0)
    except (TypeError, ValueError):
        after_id = 0
    conn = get_db()
    cur = conn.cursor()
    if after_id > 0:
        cur.execute(_q("SELECT id, order_no, phone, type, message, created_at FROM notifications WHERE id>? ORDER BY id ASC"), (after_id,))
        rows = cur.fetchall()
    else:
        cur.execute(_q("SELECT id, order_no, phone, type, message, created_at FROM notifications ORDER BY id DESC LIMIT 30"))
        rows = list(reversed(cur.fetchall()))
    out = _rows_to_dicts(cur, rows)
    conn.close()
    return jsonify({"ok": True, "notifications": out, "server_time": now_str()})


@app.route("/api/today-cancelled", methods=["GET"])
def today_cancelled():
    """骑手端：返回今天（缅甸时间）被取消的订单，用于地图红圈 ❌ 标记（凌晨12点自动清零）"""
    phone = (request.args.get("phone") or "").strip()
    day = now_str()[:10]
    conn = get_db()
    cur = conn.cursor()
    if phone:
        cur.execute(_q("SELECT o.order_no, o.shop_lat, o.shop_lng, o.completed_at, h.rider_name "
                       "FROM orders o JOIN order_history h ON h.order_no=o.order_no "
                       "WHERE h.rider_phone=? AND o.status='ပယ်ဖျက်ထားသည်' AND o.completed_at LIKE ? "
                       "ORDER BY o.completed_at DESC LIMIT 50"), (phone, day + "%"))
    else:
        cur.execute(_q("SELECT o.order_no, o.shop_lat, o.shop_lng, o.completed_at, '' AS rider_name FROM orders o "
                       "WHERE o.status='ပယ်ဖျက်ထားသည်' AND o.completed_at LIKE ? "
                       "ORDER BY o.completed_at DESC LIMIT 50"), (day + "%",))
    rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
    out = []
    for o in rows:
        lat, lng = o.get("shop_lat"), o.get("shop_lng")
        if lat is None or lng is None:
            # 兜底1：该订单最后一次上报的位置；兜底2：仰光默认中心（保证 ❌ 标记一定画得出来）
            cur.execute(_q("SELECT lat, lng FROM locations WHERE order_no=? ORDER BY id DESC LIMIT 1"), (o.get("order_no"),))
            p = cur.fetchone()
            if p:
                pd = _row_to_dict(cur, p)
                lat, lng = pd.get("lat"), pd.get("lng")
        if lat is None or lng is None:
            lat, lng = DEFAULT_MAP_LAT, DEFAULT_MAP_LNG
        out.append({"order_no": o.get("order_no"), "lat": lat, "lng": lng,
                    "shop_lat": lat, "shop_lng": lng, "cancel_time": o.get("completed_at"),
                    "rider_name": o.get("rider_name") or ""})
    conn.close()
    return jsonify({"ok": True, "day": day, "cancelled": out})


@app.route("/api/today-completed", methods=["GET"])
def today_completed():
    """骑手端：返回今天（缅甸时间）已完成订单，用于地图确认打钩永久保留（凌晨12点自动清零）"""
    phone = (request.args.get("phone") or "").strip()
    day = now_str()[:10]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT o.order_no, o.shop_lat, o.shop_lng, o.completed_at FROM orders o "
                   "WHERE o.status='လက်ခံပြီးပါပြီ' AND o.completed_at LIKE ? "
                   "AND o.order_no IN (SELECT order_no FROM order_history WHERE rider_phone=?)"),
                (day + "%", phone))
    rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
    out = []
    for o in rows:
        lat, lng = o.get("shop_lat"), o.get("shop_lng")
        if lat is None or lng is None:
            cur.execute(_q("SELECT lat, lng FROM locations WHERE order_no=? ORDER BY id DESC LIMIT 1"), (o.get("order_no"),))
            p = cur.fetchone()
            if p:
                pd = _row_to_dict(cur, p)
                lat, lng = pd.get("lat"), pd.get("lng")
        if lat is None or lng is None:
            # 兜底：与取消标记一致，绝不让 ✅ 标记因缺坐标而消失
            lat, lng = DEFAULT_MAP_LAT, DEFAULT_MAP_LNG
        out.append({"order_no": o.get("order_no"), "lat": lat, "lng": lng,
                    "shop_lat": lat, "shop_lng": lng, "completed_at": o.get("completed_at")})
    conn.close()
    return jsonify({"ok": True, "day": day, "completed": out})


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
        rname = ""
        if not rider_dict or rider_dict["phone"] != phone:
            st0 = order_dict.get("status", "")
            if st0 not in ("ပယ်ဖျက်ထားသည်", "လက်ခံပြီးပါပြီ"):
                results.append({"ok": False, "order_no": order_no, "msg": "ဖုန်းနံပါတ်နှင့် အမှာစာ မကိုက်ညီပါ။ တည်နေရာ ကြည့်ရှု၍ မရပါ"})
                continue
            cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? ORDER BY id DESC LIMIT 1"), (order_no,))
            hn = cur.fetchone()
            if hn:
                rname = _row_to_dict(cur, hn).get("rider_name", "") or ""
        elif rider_dict:
            rname = rider_dict.get("rider_name") or rider_dict.get("name") or ""
        cur.execute(
            _q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id ASC"),
            (order_no,),
        )
        points = cur.fetchall()
        track_list = _rows_to_dicts(cur, points)
        latest = track_list[-1] if track_list else None
        # 批量接口不串行调用逆地理编码（10-20 个骑手时避免阻塞），前端以坐标兜底显示
        _m_bt, _m_br, _m_st, _m_sr = order_counts(order_dict)
        results.append(
            {
                "ok": True,
                "order_no": order_no,
                "status": order_dict["status"],
                "rider_name": rname,
                "city": order_dict.get("city", "") or "",
                "payment_method": order_dict.get("payment_method", "") or "",
                "amount": order_dict.get("amount", "") or "",
                "delivery_address": order_dict.get("delivery_address") or order_dict.get("address", ""),
                "baozi_total": _m_bt,
                "baozi_received": _m_br,
                "shop_total": _m_st,
                "shop_received": _m_sr,
                "baozi_remaining": _m_bt - _m_br,
                "shop_remaining": _m_st - _m_sr,
                "baozi_total_orig": _m_bt,
                "shop_total_orig": _m_st,
                "shop_lat": order_dict.get("shop_lat"),
                "shop_lng": order_dict.get("shop_lng"),
                "shop_link": order_dict.get("shop_link", "") or "",
                "latest": latest,
                "track": track_list,
                "update_time": latest["report_time"] if latest else None,
                "completed_at": order_dict.get("completed_at", "") or "",
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
    page = (data.get("page") or request.args.get("page") or "").strip().lower()
    role = ud["role"]
    is_admin_acc = role in ("super_admin", "admin")
    is_rider_acc = (role == "rider" or role == "normal")
    if not page:
        # 前端未传域时按账号角色自动归域，绝不让骑手 token 落进商家 cookie
        page = "track" if is_admin_acc else "rider"
    if page == "rider" and not is_rider_acc:
        conn.close()
        return jsonify({"ok": False, "msg": "This page is for rider accounts only. Please login with a rider account."}), 403
    if page == "track" and not is_admin_acc:
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
    resp.set_cookie(AUTH_COOKIE_RIDER if page == "rider" else AUTH_COOKIE_ADMIN,
                    token, max_age=60 * 60 * 24 * 30, samesite="Lax", path="/")
    return resp


@app.route("/api/logout", methods=["POST"])
def logout_api():
    """登出：按页面域（商家 /track、骑手 /rider）分别清理，互不影响"""
    data = request.get_json(silent=True) or {}
    page = (data.get("page") or request.args.get("page") or "").strip()
    scope = "rider" if page == "rider" else ("admin" if page == "track" else None)
    token = _token_from_request(scope)
    user = get_current_user(scope, allow_any_role=True) if token else None
    if user:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(_q("UPDATE users SET token='' WHERE id=? AND token=?"), (user["id"], token))
        conn.commit()
        conn.close()
    resp = jsonify({"ok": True})
    if scope == "rider":
        resp.delete_cookie(AUTH_COOKIE_RIDER, path="/")
    elif scope == "admin":
        resp.delete_cookie(AUTH_COOKIE_ADMIN, path="/")
    else:
        resp.delete_cookie(AUTH_COOKIE_RIDER, path="/")
        resp.delete_cookie(AUTH_COOKIE_ADMIN, path="/")
        resp.delete_cookie("token")
    return resp


@app.route("/api/user/me", methods=["GET"])
def user_me():
    """返回当前登录用户信息（前端用于头像/昵称/按钮显隐）。
    账号被禁止/删除时返回 403，前端据此强制退出登录。"""
    token = _token_from_request()
    if not token:
        token = (request.cookies.get("token") or "").strip()
    ud = None
    if token:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(_q("SELECT * FROM users WHERE token=?"), (token,))
        user = cur.fetchone()
        ud = _row_to_dict(cur, user) if user else None
        conn.close()
    if not ud:
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    # 域校验：商家页只认管理员账号，骑手页只认骑手账号，避免相互顶号后显示错账号
    scope = _page_scope()
    if scope == "admin" and ud["role"] not in ("super_admin", "admin"):
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    if scope == "rider" and ud["role"] not in ("rider", "normal"):
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
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
    """上传头像（手机/电脑选图）。支持两种方式：
    1) JSON {avatar_data: data:image/...;base64,...}：压缩后的头像直接存数据库，服务器重置/重新部署后依然保留；
    2) 表单文件 avatar：保存到 static/uploads/avatars（兼容旧流程）。"""
    user = get_current_user(_page_scope())
    if not user:
        return jsonify({"ok": False, "msg": "Not logged in."}), 401
    data_url = ""
    try:
        if request.is_json:
            data_url = (request.get_json(silent=True) or {}).get("avatar_data") or ""
        else:
            data_url = request.form.get("avatar_data") or ""
    except Exception:
        data_url = ""
    data_url = (data_url or "").strip()
    if data_url:
        if not data_url.startswith("data:image/"):
            return jsonify({"ok": False, "msg": "Invalid image data."}), 400
        if len(data_url) > 3 * 1024 * 1024:
            return jsonify({"ok": False, "msg": "Image too large."}), 400
        conn = get_db()
        cur = conn.cursor()
        cur.execute(_q("UPDATE users SET avatar=? WHERE id=?"), (data_url, user["id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "avatar": data_url})
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
    records = _rows_to_dicts(cur, rows)
    # status_updated_at 由 now_str() 写入，已是仰光时间，无需再次转换
    return jsonify({"ok": True, "records": records})


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
    try:
        cur.execute(
            _q("INSERT INTO users (username, password_hash, nickname, role, status, created_at, phone) VALUES (?,?,?,?,?,?,?)"),
            (username, generate_password_hash(password), nickname or username, "rider", "normal", now_str(), phone),
        )
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()
        return jsonify({"ok": False, "msg": "Server error: " + str(e)}), 500
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


# ================= 手机 / 电脑 功能一致性补丁 =================
# 说明：全局禁用缓存（手机端不会读到旧页面/旧接口），并补充“剩余数量”“今日已完成/已取消标记”接口
APP_VERSION = "20261105-v1"


@app.after_request
def _patch_no_cache(resp):
    try:
        ctype = (resp.headers.get("Content-Type") or "")
        if request.path.startswith("/api/") or "text/html" in ctype:
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp


@app.route("/api/version", methods=["GET"])
def _patch_version():
    return jsonify({"ok": True, "version": APP_VERSION, "server_time": now_str()})


@app.route("/api/rider/remaining", methods=["GET"])
def _patch_rider_remaining():
    """返回某骑手最新一单 / 指定订单的原始总量与剩余数量（分母永不扣减）"""
    phone = (request.args.get("phone") or "").strip()
    order_no = (request.args.get("order_no") or "").strip()
    if not phone and not order_no:
        return jsonify({"ok": False, "msg": "phone required"}), 400
    conn = get_db()
    cur = conn.cursor()
    od = None
    if order_no:
        cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
        r = cur.fetchone()
        if r:
            od = _row_to_dict(cur, r)
    if od is None and phone:
        cur.execute(_q("SELECT o.* FROM orders o JOIN order_history h ON h.order_no=o.order_no WHERE h.rider_phone=? ORDER BY h.id DESC LIMIT 1"), (phone,))
        r = cur.fetchone()
        if r:
            od = _row_to_dict(cur, r)
    conn.close()
    if not od:
        return jsonify({"ok": False, "msg": "no order"}), 404
    bt, br, st, sr = order_counts(od)
    # 每日口径：只统计「当天」已收数量，跨天（凌晨 0 点后）自动归零，
    # 分母永远取骑手最初输入量（ba_*_total_orig），所以显示回到 500 / 500
    today = now_str()[:10]
    done_day = (od.get("completed_at") or "")[:10]
    if done_day and done_day != today:
        br_day, sr_day = 0, 0
    else:
        br_day, sr_day = br, sr
    return jsonify({
        "ok": True,
        "day": today,
        "order_no": od.get("order_no") or "",
        "status": od.get("status") or "",
        "completed_at": od.get("completed_at") or "",
        "baozi_total_orig": bt, "baozi_received": br_day, "baozi_remaining": max(bt - br_day, 0),
        "shop_total_orig": st, "shop_received": sr_day, "shop_remaining": max(st - sr_day, 0),
        "server_time": now_str(),
    })


@app.route("/api/today-marks", methods=["GET"])
def _patch_today_marks():
    """今日已完成 ✅ / 已取消 ❌ 全量标记（管理员页与骑手端地图保持一致；取消不隐藏已完成店）"""
    day = (request.args.get("day") or "").strip() or now_str()[:10]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT order_no, shop_lat, shop_lng, status, completed_at FROM orders "
                   "WHERE completed_at LIKE ? AND status IN ('လက်ခံပြီးပါပြီ','ပယ်ဖျက်ထားသည်') "
                   "ORDER BY completed_at DESC LIMIT 300"), (day + "%",))
    rows = [_row_to_dict(cur, r) for r in cur.fetchall()]
    done, cancelled = [], []
    for o in rows:
        lat, lng = o.get("shop_lat"), o.get("shop_lng")
        if lat is None or lng is None:
            cur.execute(_q("SELECT lat,lng FROM locations WHERE order_no=? ORDER BY id DESC LIMIT 1"), (o.get("order_no"),))
            pp = cur.fetchone()
            if pp:
                pd = _row_to_dict(cur, pp)
                lat, lng = pd.get("lat"), pd.get("lng")
        name = ""
        cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? ORDER BY id DESC LIMIT 1"), (o.get("order_no"),))
        hr = cur.fetchone()
        if hr:
            name = (_row_to_dict(cur, hr).get("rider_name") or "")
        item = {"order_no": o.get("order_no"), "lat": lat, "lng": lng, "rider_name": name, "time": o.get("completed_at") or ""}
        if (o.get("status") or "") == "လက်ခံပြီးပါပြီ":
            done.append(item)
        else:
            cancelled.append(item)
    conn.close()
    return jsonify({"ok": True, "day": day, "done": done, "cancelled": cancelled})


if __name__ == "__main__":
    # Railway 通过 Procfile(gunicorn) 启动，不走这里；本地测试时使用
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("包子店外卖实时定位追踪系统（本地测试模式）")
    print("本地访问:  http://127.0.0.1:" + str(port))
    print("手机访问:  手机连同一 WiFi，访问 http://<电脑局域网IP>:" + str(port))
    print("=" * 55)
    app.run(host="0.0.0.0", port=port, debug=False)
