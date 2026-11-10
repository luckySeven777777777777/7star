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
import random
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


def build_proof_caption(order_no, phone, order_dict, rider_name, latest, qty=None):
    """组装 Telegram 通知内容（整单只发这一条，绝不重复）：骑手独立口径数量 + 最新位置 + 退回包子数量"""
    if qty:
        baozi_total = _to_int(qty.get("baozi_total"))
        baozi_received = _to_int(qty.get("baozi_received"))
        shop_total = _to_int(qty.get("shop_total"))
        shop_received = _to_int(qty.get("shop_received"))
        returned = _to_int(qty.get("baozi_returned"))
    else:
        baozi_total, baozi_received, shop_total, shop_received = order_counts(order_dict)
        returned = 0
    loc_text = "-"
    if latest:
        loc_text = latest.get("address") or ("%.6f, %.6f" % (latest["lat"], latest["lng"]))
    return (
        "✅ အော်ဒါ ပြီးဆုံးပါပြီ✅\n"
        "🚚 Delivery Proof\n"
        "📥Order: %s\n"
        "📲Rider Phone: %s\n"
        "👤Rider Name：%s\n"
        "🔍လက်ရှိအခြေအနေ：%s\n"
        "📍လက်ရှိနေရာ：%s\n"
        "🍙ကျန်ရှိသောပေါက်စီအလုံးအရေတွက်：%d / %d\n"
        "📝ကျန်ရှိသောဆိုင်အရေအတွက်：%d / %d\n"
        "‼️ပြန်အပ်သောပေါက်စီအရေအတွက်：%d\n"
        "💳ငွေပေးချေမှုနည်းလမ်း：%s\n"
        "💵Amount (MMK)：%s"
    ) % (
        order_no or "-",
        phone or "-",
        rider_name or "-",
        order_dict.get("status", "") or "-",
        loc_text,
        max(baozi_total - baozi_received, 0),
        baozi_total,
        max(shop_total - shop_received, 0),
        shop_total,
        returned,
        order_dict.get("payment_method", "") or "-",
        order_dict.get("amount", "") or "-",
    )


def build_cancel_caption(order_no, phone, rider_name, loc_text, cancel_baozi, cancel_reason):
    """骑手取消订单的 Telegram 通知（标签缅文，数量/原因中文）"""
    return (
        "🚚 Delivery Proof\n"
        "❌️Order Cancel❌️\n"
        "📥Order: %s\n"
        "📲Rider Phone: %s\n"
        "👤Rider Name：%s\n"
        "📍လက်ရှိနေရာ：%s\n"
        "🍙取消数量：%d\n"
        "❌️取消原因：%s"
    ) % (order_no or "-", phone or "-", rider_name or "-", loc_text or "-",
         _to_int(cancel_baozi), cancel_reason or "-")


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
        # 分母锁定的日期（仰光时区 YYYY-MM-DD）：同日只认第一次输入，跨天自动清零重设
        "qty_lock_day": "TEXT DEFAULT ''",
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


def ensure_rider_qty_table():
    """骑手独立数量表：每个骑手（手机号）按天单独管理自己的数量。
    分母 = 该骑手当天的第一次输入（之后重新下单/改单都不会被改写）；
    送出的数量只累加到该骑手自己名下，绝不与其他骑手混合计算。
    跨天（仰光时间 0 点）自动新建一行，等于自动清零。"""
    conn = get_db()
    cur = conn.cursor()
    if is_pg():
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS rider_daily_qty (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                day TEXT NOT NULL,
                baozi_orig INTEGER DEFAULT 0,
                shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                updated_at TEXT,
                UNIQUE(phone, day)
            )
        """))
    else:
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS rider_daily_qty (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                day TEXT NOT NULL,
                baozi_orig INTEGER DEFAULT 0,
                shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                updated_at TEXT,
                UNIQUE(phone, day)
            )
        """))
    # 骑手 × 订单 独立数量表：每个骑手对每一单各自一份数量，同一天不同订单互不影响，
    # 不同骑手之间更不会混算。分母 = 该骑手在该单第一次输入的数量，重新下单也不会变。
    if is_pg():
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS rider_order_qty (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                order_no TEXT NOT NULL,
                day TEXT,
                baozi_orig INTEGER DEFAULT 0,
                shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                updated_at TEXT,
                UNIQUE(phone, order_no)
            )
        """))
    else:
        cur.execute(_q("""
            CREATE TABLE IF NOT EXISTS rider_order_qty (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                order_no TEXT NOT NULL,
                day TEXT,
                baozi_orig INTEGER DEFAULT 0,
                shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0,
                shop_received INTEGER DEFAULT 0,
                updated_at TEXT,
                UNIQUE(phone, order_no)
            )
        """))
    conn.commit()
    conn.close()


try:
    ensure_rider_qty_table()
except Exception:
    pass


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


# ==================== 骑手独立数量（按 手机号 + 订单号 隔离，每个骑手每单各管一份） ====================
_QTY_TABLE_READY = False


def norm_phone(phone):
    """手机号统一规范：去掉空格/横线/括号/+95 与 0 前缀差异。
    同一骑手用 09xxx / +959xxx / 959xxx 输入时必须被认成同一个人，
    否则会被当成不同骑手，导致数量互相串台（看起来像“混在一起算”）。"""
    s = re.sub(r"[^0-9+]", "", str(phone or ""))
    s = s.replace("+", "")
    if s.startswith("0095"):
        s = s[4:]
    elif s.startswith("95") and len(s) >= 11:
        s = s[2:]
    if s.startswith("0") and len(s) >= 9:
        s = s[1:]
    return s


def _rq_key(phone):
    """骑手数量的存储键：手机号优先，非数字输入（如测试用的 aabb / abc）保留原样小写。
    否则不同骑手会被 norm_phone 折叠成同一个空键，导致两个骑手数量完全同步。"""
    p = norm_phone(phone)
    if p:
        return p
    raw = str(phone or "").strip().lower()
    if raw.startswith("n:"):
        return raw
    return ("n:" + raw) if raw else ""


def _rq_day_key(day=None, order_no=None):
    """数量记录存储键：按「骑手 + 订单 + 当天」各占一行。
    加入订单维度后，同一骑手换新订单不会沿用上一单的数字（这是「输入 1200 地图却显示 2000」的根因）；
    键里带日期，跨天自然生成新行，等于缅甸时间凌晨 0 点自动归零。"""
    day = day or now_str()[:10]
    on = (order_no or "").strip()
    if on:
        return "__O__" + on + "|" + day
    return "__DAY__" + day


def rider_qty_dayrec(cur, phone, day=None, order_no=None):
    """取该骑手「当天」的数量记录。
    传 order_no 时按「骑手+订单+当天」精确取（页面显示的就是这一单自己的数）；
    不传 order_no 时才退回旧的「骑手+当天」合并记录（仅兼容旧数据，页面逻辑不再使用）。"""
    day = day or now_str()[:10]
    phone = _rq_key(phone)
    if not phone:
        return None
    _ensure_qty_table(cur)
    order_no = (order_no or "").strip()
    key = _rq_day_key(day, order_no)
    try:
        cur.execute(_q("SELECT * FROM rider_order_qty WHERE phone=? AND order_no=?"), (phone, key))
        row = cur.fetchone()
        if row:
            return _row_to_dict(cur, row)
    except Exception:
        return None
    if order_no:
        # 本单今天还没有自己的记录：返回 None，绝不借用其它订单 / 其它天的数字
        return None
    try:
        cur.execute(_q("SELECT * FROM rider_order_qty WHERE phone=? AND day=?"), (phone, day))
        rows = cur.fetchall() or []
    except Exception:
        return None
    if not rows:
        return None
    bo = so = br = sr = rt = 0
    ids = []
    for row in rows:
        d = _row_to_dict(cur, row) or {}
        bo = max(bo, _to_int(d.get("baozi_orig")))
        so = max(so, _to_int(d.get("shop_orig")))
        br += _to_int(d.get("baozi_received"))
        sr += _to_int(d.get("shop_received"))
        rt += _to_int(d.get("baozi_returned"))
        ids.append(d.get("id"))
    try:
        cur.execute(_q("INSERT INTO rider_order_qty (phone, order_no, day, baozi_orig, shop_orig, baozi_received, shop_received, baozi_returned, updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)"), (phone, key, day, bo, so, br, sr, rt, now_str()))
    except Exception:
        return None
    for _id in ids:
        try:
            cur.execute(_q("DELETE FROM rider_order_qty WHERE id=?"), (_id,))
        except Exception:
            pass
    try:
        cur.execute(_q("SELECT * FROM rider_order_qty WHERE phone=? AND order_no=?"), (phone, key))
        row = cur.fetchone()
        return _row_to_dict(cur, row) if row else None
    except Exception:
        return None


def _ensure_qty_table(cur):
    """惰性建表兜底：无论启动迁移是否成功（线上库/新库/迁移被跳过），
    第一次读写骑手数量前都保证 rider_order_qty 存在，避免 INSERT 静默失败后
    整体退回“订单口径”，看起来就是所有骑手数量混在一起、分母变成剩余量。"""
    global _QTY_TABLE_READY
    if _QTY_TABLE_READY:
        return True
    try:
        if is_pg():
            cur.execute(_q("""CREATE TABLE IF NOT EXISTS rider_order_qty (
                id SERIAL PRIMARY KEY, phone TEXT NOT NULL, order_no TEXT NOT NULL, day TEXT,
                baozi_orig INTEGER DEFAULT 0, shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0, shop_received INTEGER DEFAULT 0,
                updated_at TEXT, UNIQUE(phone, order_no))"""))
        else:
            cur.execute(_q("""CREATE TABLE IF NOT EXISTS rider_order_qty (
                id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL, order_no TEXT NOT NULL, day TEXT,
                baozi_orig INTEGER DEFAULT 0, shop_orig INTEGER DEFAULT 0,
                baozi_received INTEGER DEFAULT 0, shop_received INTEGER DEFAULT 0,
                updated_at TEXT, UNIQUE(phone, order_no))"""))
        for _col_sql in (
            "ALTER TABLE rider_order_qty ADD COLUMN baozi_returned INTEGER DEFAULT 0",
            "ALTER TABLE rider_order_qty ADD COLUMN cancel_baozi INTEGER DEFAULT 0",
            "ALTER TABLE rider_order_qty ADD COLUMN cancel_reason TEXT",
        ):
            try:
                cur.execute(_q(_col_sql))
            except Exception:
                pass
        _QTY_TABLE_READY = True
        return True
    except Exception as e:
        print("[rider_order_qty] ensure table failed: %s" % e)
        return False



def rider_row_by_phone(cur, phone, order_no):
    """按「订单号 + 手机号」取该骑手自己的绑定记录，兼容历史脏数据：
    09xxxxxxxxx / +959xxxxxxxx / 959xxxxxxxx / 带空格横线 等写法都会认成同一个人。
    先精确查，查不到再按订单取出后用 norm_phone 逐一比对，
    避免格式不同被当成“没有绑定”或串到别的骑手身上导致数量混算。"""
    order_no = (order_no or "").strip()
    phone = (phone or "").strip()
    if not order_no:
        return None
    try:
        if phone:
            cur.execute(_q("SELECT * FROM riders WHERE phone=? AND order_no=?"), (phone, order_no))
            r = cur.fetchone()
            if r:
                return r
        target = norm_phone(phone)
        cur.execute(_q("SELECT * FROM riders WHERE order_no=?"), (order_no,))
        rows = cur.fetchall() or []
    except Exception:
        return None
    if not target:
        return None
    for r in rows:
        try:
            d = _row_to_dict(cur, r)
        except Exception:
            continue
        if norm_phone(d.get("phone")) == target:
            return r
    return None

def rider_qty_get(cur, phone, order_no=None, day=None):
    """读取某骑手在某订单下的独立数量记录。
    主键是 (phone, order_no)：不同骑手互不混合，同一骑手的不同订单也各自独立。"""
    phone = _rq_key(phone)
    if not phone:
        return None
    _ensure_qty_table(cur)
    try:
        if order_no:
            cur.execute(_q("SELECT * FROM rider_order_qty WHERE phone=? AND order_no=?"), (phone, order_no))
            r = cur.fetchone()
            if r:
                return _row_to_dict(cur, r)
            return None
        if day:
            cur.execute(_q("SELECT * FROM rider_order_qty WHERE phone=? AND day=?"), (phone, day))
            r = cur.fetchone()
            return _row_to_dict(cur, r) if r else None
        return None
    except Exception:
        return None


def rider_day_totals(cur, phone, day=None):
    """该骑手「当天」的汇总口径（每个骑手各自独立，绝不与其他骑手混合）：
    分母 = 当天首次输入量（各单 orig 的最大值，即第一次输入的那个数，如 600）；
    已送 = 当天各单累加（如 100）。这样重下单后仍显示 600 / 剩 500。
    返回 (baozi_orig, shop_orig, baozi_received, shop_received)"""
    day = day or now_str()[:10]
    phone = _rq_key(phone)
    if not phone:
        return 0, 0, 0, 0
    _ensure_qty_table(cur)
    rows = []
    try:
        cur.execute(_q("SELECT baozi_orig, shop_orig, baozi_received, shop_received FROM rider_order_qty WHERE phone=? AND day=?"), (phone, day))
        rows = cur.fetchall() or []
    except Exception:
        return 0, 0, 0, 0
    bo = so = br = sr = 0
    for r in rows:
        d = _row_to_dict(cur, r)
        bo = max(bo, _to_int(d.get("baozi_orig")))
        so = max(so, _to_int(d.get("shop_orig")))
        br += _to_int(d.get("baozi_received"))
        sr += _to_int(d.get("shop_received"))
    return bo, so, br, sr


def rider_qty_lock(cur, phone, order_no, baozi_in, shop_in):
    """锁定「该骑手 + 该订单」的分母：本单第一次输入即分母，之后重复绑定 / 改填都不改变；
    换新订单号则是新的一单，用新的输入量。跨天（缅甸时间 0 点后）自动重新锁定并清零。"""
    b_in = max(_to_int(baozi_in), 0)
    s_in = max(_to_int(shop_in), 0)
    phone = _rq_key(phone)
    if not phone:
        return b_in, s_in
    _ensure_qty_table(cur)
    day = now_str()[:10]
    key = _rq_day_key(day, order_no)
    rec = rider_qty_dayrec(cur, phone, day, order_no)
    if rec:
        b = _to_int(rec.get("baozi_orig"))
        s = _to_int(rec.get("shop_orig"))
        nb, ns = b, s
        # 本单还没有任何送出/退回进度时：分母 = 最新输入（输入多少就显示多少），
        # 避免「骑手改填 1200，页面和地图仍显示第一次输入的 2000」。
        _prog = (_to_int(rec.get("baozi_received")) + _to_int(rec.get("shop_received"))
                 + _to_int(rec.get("baozi_returned")) + _to_int(rec.get("cancel_baozi")))
        if _prog <= 0:
            if b_in > 0:
                nb = b_in
            if s_in > 0:
                ns = s_in
        else:
            if nb <= 0 and b_in > 0:
                nb = b_in
            if ns <= 0 and s_in > 0:
                ns = s_in
        try:
            cur.execute(_q("UPDATE rider_order_qty SET order_no=?, day=?, baozi_orig=?, shop_orig=?, updated_at=? WHERE phone=? AND id=?"),
                        (key, day, nb, ns, now_str(), phone, rec.get("id")))
        except Exception:
            pass
        return nb, ns
    try:
        cur.execute(_q("INSERT INTO rider_order_qty (phone, order_no, day, baozi_orig, shop_orig, baozi_received, shop_received, updated_at) "
                       "VALUES (?,?,?,?,?,0,0,?)"), (phone, key, day, b_in, s_in, now_str()))
    except Exception:
        pass
    return b_in, s_in


def rider_qty_add(cur, phone, order_no, baozi_add, shop_add, returned_add=0):
    """把本次送出 / 退回 / 取消退回累加到「该骑手 + 该订单 + 今天」这一行；
    不同骑手、不同订单互不影响，跨天自动归零。"""
    b_add = max(_to_int(baozi_add), 0)
    s_add = max(_to_int(shop_add), 0)
    r_add = max(_to_int(returned_add), 0)
    phone = _rq_key(phone)
    if not phone:
        return None
    _ensure_qty_table(cur)
    day = now_str()[:10]
    key = _rq_day_key(day, order_no)
    rec = rider_qty_dayrec(cur, phone, day, order_no)
    if not rec:
        try:
            cur.execute(_q("INSERT INTO rider_order_qty (phone, order_no, day, baozi_orig, shop_orig, baozi_received, shop_received, baozi_returned, updated_at) "
                           "VALUES (?,?,?,0,0,?,?,?,?)"), (phone, key, day, b_add, s_add, r_add, now_str()))
        except Exception:
            pass
        return rider_qty_dayrec(cur, phone, day, order_no)
    try:
        cur.execute(_q("UPDATE rider_order_qty SET order_no=?, day=?, baozi_received=?, shop_received=?, baozi_returned=?, updated_at=? WHERE phone=? AND id=?"),
                    (key, day, _to_int(rec.get("baozi_received")) + b_add,
                     _to_int(rec.get("shop_received")) + s_add,
                     _to_int(rec.get("baozi_returned")) + r_add,
                     now_str(), phone, rec.get("id")))
    except Exception:
        pass
    return rider_qty_dayrec(cur, phone, day, order_no)


def rider_qty_view(cur, phone, order_dict=None, order_no=None):
    """数量统一口径：按「骑手 + 当天」独立统计。
    分母 = 该骑手当天第一次输入量（第二次下单不改变），已送/退回 = 当天累计（跨订单），
    凌晨 0 点自动归零；没有该骑手记录时返回 rider_empty，绝不用订单级数字冒充。"""
    day = now_str()[:10]
    if order_no is None and order_dict:
        order_no = (order_dict.get("order_no") or "").strip()
    phone = _rq_key(phone)
    if phone:
        rec = rider_qty_dayrec(cur, phone, day, order_no)
        if rec:
            bo = _to_int(rec.get("baozi_orig"))
            so = _to_int(rec.get("shop_orig"))
            br = _to_int(rec.get("baozi_received"))
            sr = _to_int(rec.get("shop_received"))
            rt = _to_int(rec.get("baozi_returned"))
            # 问题2：退回数量 = 确认✅ 时填的退回量 + 取消❌ 时填的退回量，两者都要计；
            # 记录按「骑手 + 当天」取，所以缅甸时间凌晨 0 点后自动归零。
            rt += _to_int(rec.get("cancel_baozi"))
            if bo > 0 or so > 0 or br > 0 or sr > 0 or rt > 0:
                return {
                    "scope": "rider_order", "day": day, "order_no": order_no,
                    "baozi_total": bo, "baozi_received": br, "baozi_remaining": max(bo - br, 0),
                    "baozi_returned": rt,
                    "shop_total": so, "shop_received": sr, "shop_remaining": max(so - sr, 0),
                }
        return {
            "scope": "rider_empty", "day": day, "order_no": order_no,
            "baozi_total": 0, "baozi_received": 0, "baozi_remaining": 0, "baozi_returned": 0,
            "shop_total": 0, "shop_received": 0, "shop_remaining": 0,
        }
    if order_dict:
        bt, br, st, sr = order_counts(order_dict)
        return {
            "scope": "order", "day": day, "order_no": order_no,
            "baozi_total": bt, "baozi_received": br, "baozi_remaining": max(bt - br, 0),
            "baozi_returned": 0,
            "shop_total": st, "shop_received": sr, "shop_remaining": max(st - sr, 0),
        }
    return {
        "scope": "none", "day": day, "order_no": order_no,
        "baozi_total": 0, "baozi_received": 0, "baozi_remaining": 0, "baozi_returned": 0,
        "shop_total": 0, "shop_received": 0, "shop_remaining": 0,
    }


def order_rider_count(cur, order_no):
    """该订单历史上共出现过几个不同骑手。
    只有 <=1 个骑手时才允许用订单级数字兜底（那本来就是他自己的数）；
    >1 个骑手时严禁兜底，否则新骑手会把别人的数量当自己的显示。"""
    if not order_no:
        return 0
    seen = set()
    for sql in (
        "SELECT rider_phone AS p FROM order_history WHERE order_no=?",
        "SELECT phone AS p FROM riders WHERE order_no=?",
    ):
        try:
            cur.execute(_q(sql), (order_no,))
            for r in cur.fetchall() or []:
                d = _row_to_dict(cur, r) or {}
                k = _rq_key(d.get("p"))
                if k:
                    seen.add(k)
        except Exception:
            pass
    return len(seen)


# ==================== 骑手状态闸门（问题3 / 问题4） ====================
_STATUS_SEQ = ("အစားအစာယူရန် စောင့်ဆိုင်း", "ပို့ဆောင်နေသည်", "ရောက်ရှိပြီး")
_STATUS_FINAL_DONE = "လက်ခံပြီးပါပြီ"
_STATUS_FINAL_CANCEL = "ပယ်ဖျက်ထားသည်"
_STATUS_LOG_READY = False


def ensure_status_log(cur):
    """骑手状态点按留痕表：手机没有 GPS 时也能证明「三个状态」确实按顺序点过。
    只按 (order_no, phone) 记录，绝不跨骑手共享。"""
    global _STATUS_LOG_READY
    if _STATUS_LOG_READY:
        return True
    pk = "SERIAL PRIMARY KEY" if is_pg() else "INTEGER PRIMARY KEY AUTOINCREMENT"
    try:
        cur.execute(_q("CREATE TABLE IF NOT EXISTS rider_status_log ("
                       "id " + pk + ", order_no TEXT NOT NULL, phone TEXT NOT NULL, "
                       "status TEXT NOT NULL, created_at TEXT)"))
        try:
            cur.execute(_q("CREATE INDEX IF NOT EXISTS idx_rider_status_log ON rider_status_log (order_no, phone)"))
        except Exception:
            pass
        _STATUS_LOG_READY = True
        return True
    except Exception as e:
        print("[rider_status_log] ensure table failed: %s" % e)
        return False


def three_status_done(cur, order_no, phone):
    """三个状态（取餐 → 配送中 → 到达）是否已按顺序点完。
    判定来源三选一即可：订单状态、最新定位状态、状态留痕表（同一骑手同一订单）。"""
    order_no = (order_no or "").strip()
    if not order_no:
        return False
    key = _rq_key(phone)
    try:
        cur.execute(_q("SELECT status FROM orders WHERE order_no=?"), (order_no,))
        r = cur.fetchone()
        if r:
            st = (_row_to_dict(cur, r) or {}).get("status") or ""
            if st.strip() == "ရောက်ရှိပြီး":
                return True
    except Exception:
        pass
    try:
        cur.execute(_q("SELECT status, phone FROM locations WHERE order_no=? ORDER BY id DESC LIMIT 12"), (order_no,))
        for row in cur.fetchall() or []:
            d = _row_to_dict(cur, row) or {}
            if (d.get("status") or "").strip() != "ရောက်ရှိပြီး":
                continue
            # 定位表没有 phone 列时按订单判定（同一订单只会在本骑手名下推进）
            lp = _rq_key(d.get("phone")) if "phone" in d else ""
            if not lp or not key or lp == key:
                return True
    except Exception:
        pass
    try:
        ensure_status_log(cur)
        cur.execute(_q("SELECT phone, status FROM rider_status_log WHERE order_no=?"), (order_no,))
        for row in cur.fetchall() or []:
            d = _row_to_dict(cur, row) or {}
            if (d.get("status") or "").strip() != "ရောက်ရှိပြီး":
                continue
            lp = _rq_key(d.get("phone"))
            if not key or lp == key:
                return True
    except Exception:
        pass
    return False


_STATUS_IDX = {s: i for i, s in enumerate(_STATUS_SEQ)}


def status_predecessor_done(cur, order_no, phone, status):
    """问题3（服务端闸门）：前一个状态没点过，就不许推进到后一个。
    兼容旧数据：订单当前状态已经到前一个（或更后面）时，视为点过。"""
    idx = _STATUS_IDX.get(status, -1)
    if idx <= 0:
        return True
    prev = _STATUS_SEQ[idx - 1]
    key = _rq_key(phone)
    try:
        ensure_status_log(cur)
        cur.execute(_q("SELECT status FROM rider_status_log WHERE order_no=? AND phone=?"), (order_no, key))
        for r in cur.fetchall() or []:
            d = _row_to_dict(cur, r) or {}
            if (d.get("status") or "").strip() == prev:
                return True
    except Exception:
        pass
    try:
        cur.execute(_q("SELECT status FROM orders WHERE order_no=?"), (order_no,))
        r = cur.fetchone()
        if r:
            st = ((_row_to_dict(cur, r) or {}).get("status") or "").strip()
            if st in _STATUS_IDX and _STATUS_IDX[st] >= _STATUS_IDX[prev]:
                return True
    except Exception:
        pass
    return False


def _order_other_rider_bound(cur, order_no, phone):
    """该订单上是否已有「别的骑手」绑定记录（用于禁止后来者覆盖订单级数量）。"""
    me = _rq_key(phone)
    seen = set()
    for sql in (
        "SELECT rider_phone AS p FROM order_history WHERE order_no=?",
        "SELECT phone AS p FROM riders WHERE order_no=?",
    ):
        try:
            cur.execute(_q(sql), (order_no,))
            for r in cur.fetchall() or []:
                d = _row_to_dict(cur, r) or {}
                k = _rq_key(d.get("p"))
                if k:
                    seen.add(k)
        except Exception:
            pass
    if me:
        seen.discard(me)
    return len(seen) > 0


def order_single_rider_phone(cur, order_no):
    """该订单唯一绑定的骑手手机号（规范化）。
    只有「全订单只出现过 1 个骑手」时才返回，避免把别人的数量当成自己的。"""
    order_no = (order_no or "").strip()
    if not order_no:
        return ""
    seen = set()
    for sql in (
        "SELECT rider_phone AS p FROM order_history WHERE order_no=?",
        "SELECT phone AS p FROM riders WHERE order_no=?",
    ):
        try:
            cur.execute(_q(sql), (order_no,))
            for r in cur.fetchall() or []:
                d = _row_to_dict(cur, r) or {}
                k = _rq_key(d.get("p"))
                if k:
                    seen.add(k)
        except Exception:
            pass
    if len(seen) == 1:
        return list(seen)[0]
    return ""


_OLC_ALPHABET = "23456789CFGHJMPQRVWX"
_OLC_DEFAULT_REF = (16.8661, 96.1951)  # 仰光市中心，短码补位参考点


def _olc_val(ch):
    try:
        return _OLC_ALPHABET.index(ch)
    except ValueError:
        return -1


def olc_encode_prefix(lat, lng, length=4):
    """Open Location Code 高位编码（用于给短码补齐参考前缀）"""
    try:
        lat = max(min(float(lat), 90.0), -90.0)
        lng = ((float(lng) + 180.0) % 360.0) - 180.0
    except (TypeError, ValueError):
        return ""
    code = ""
    res = 400.0
    latv = lat + 90.0
    lngv = lng + 180.0
    while len(code) < length:
        res /= 20.0
        i = int(latv / res)
        j = int(lngv / res)
        i = max(0, min(i, 19))
        j = max(0, min(j, 19))
        code += _OLC_ALPHABET[i] + _OLC_ALPHABET[j]
        latv -= i * res
        lngv -= j * res
    return code


def decode_plus_code(text, ref_lat=None, ref_lng=None):
    """Open Location Code（Google Plus Code）解码，返回 (lat, lng) 单元格中心。
    支持完整码（7M8RV66W+FC3）与短码（V66W+FC3），短码按参考点补高位（默认仰光）。"""
    if not text:
        return None
    s = str(text).upper().replace(" ", "")
    m = re.search(r"([23456789CFGHJMPQRVWX]{2,8})\+([23456789CFGHJMPQRVWX]{0,3})", s)
    if not m:
        return None
    body, tail = m.group(1), m.group(2)
    if len(body) + len(tail) < 2:
        return None
    rl = ref_lat if ref_lat is not None else _OLC_DEFAULT_REF[0]
    rg = ref_lng if ref_lng is not None else _OLC_DEFAULT_REF[1]

    # '+' 前 8 位及以上是完整码，直接解
    if len(body) >= 8:
        full = (body + tail)[:15]
        got = _olc_decode_core(full)
        if got and in_region(got[0], got[1]):
            return got
        return None

    # 否则是短码（7M8R+FC / PXXQ+XX Yangon 都属此类）：必须按参考点补前若干位。
    # 补位不能只用参考点自己的前缀，否则会偏最多 1 个经度/纬度格（约 110km）；
    # 必须像 Google 官方 recoverNearest 那样在参考点周边网格里取「离参考点最近」的那个候选。
    need = 8 - len(body)
    best = None
    best_d = None
    for dla in (-1, 0, 1):
        for dln in (-1, 0, 1):
            la, ln = rl + dla * 1.0, rg + dln * 1.0
            if not in_region(la, ln):
                continue
            pfx = olc_encode_prefix(la, ln, 8)[:need]
            full = (pfx + body + tail)[:15]
            if len(full) < 8:
                full = full + _OLC_ALPHABET[0] * (8 - len(full))
            got = _olc_decode_core(full)
            if not got or not in_region(got[0], got[1]):
                continue
            d = (got[0] - rl) ** 2 + (got[1] - rg) ** 2
            if best_d is None or d < best_d:
                best_d, best = d, got
    return best


def _olc_decode_core(full):
    """纯 OLC 解码（输入须为补齐后的完整码），返回单元格中心；坐标非法返回 None"""
    lat, lng = -90.0, -180.0
    res = 400.0
    n = min(len(full), 10)
    for i in range(0, n, 2):
        res /= 20.0
        a = _olc_val(full[i])
        b = _olc_val(full[i + 1]) if i + 1 < n else 0
        if a < 0 or b < 0:
            return None
        lat += a * res
        lng += b * res
    half_lat = res / 2.0
    half_lng = res / 2.0
    if len(full) > 10:
        glat = res / 5.0
        glng = res / 4.0
        for i in range(10, len(full)):
            v = _olc_val(full[i])
            if v < 0:
                break
            lat += (v // 4) * glat
            lng += (v % 4) * glng
            half_lat = glat / 2.0
            half_lng = glng / 2.0
            glat /= 5.0
            glng /= 4.0
    if lat < -90 or lat > 90 or lng < -180 or lng > 180:
        return None
    return (lat + half_lat, lng + half_lng)


# 短码（如 M9VQ+XX Yangon）只有 6 位时，必须用参考点补高位。
# 手机端粘贴的短码后面通常跟着城市名，按城市取参考点，比固定仰光准得多（曼德勒/内比都等地不会差几百公里）。
_MM_CITY_REF = (
    ("naypyidaw", (19.7633, 96.0785)), ("nay pyi taw", (19.7633, 96.0785)), ("内比都", (19.7633, 96.0785)),
    ("yangon", (16.8661, 96.1951)), ("rangoon", (16.8661, 96.1951)), ("仰光", (16.8661, 96.1951)),
    ("mandalay", (21.9588, 96.0891)), ("曼德勒", (21.9588, 96.0891)),
    ("bago", (17.3350, 96.4813)), ("勃固", (17.3350, 96.4813)),
    ("mawlamyine", (16.4905, 97.6283)), ("maulmain", (16.4905, 97.6283)), ("毛淡棉", (16.4905, 97.6283)),
    ("taunggyi", (20.7892, 97.0378)), ("东枝", (20.7892, 97.0378)),
    ("pathein", (16.7790, 94.7320)), ("勃生", (16.7790, 94.7320)),
    ("monywa", (22.1083, 95.1333)), ("蒙育瓦", (22.1083, 95.1333)),
    ("myitkyina", (25.3833, 97.3964)), ("密支那", (25.3833, 97.3964)),
    ("sittwe", (20.1462, 92.8986)), ("实兑", (20.1462, 92.8986)),
    ("dawei", (14.0823, 98.1915)), ("土瓦", (14.0823, 98.1915)),
    ("hpa-an", (16.8895, 97.6339)), ("hpaan", (16.8895, 97.6339)), ("帕安", (16.8895, 97.6339)),
    ("pyay", (18.8192, 95.2114)), ("卑谬", (18.8192, 95.2114)),
    ("meiktila", (20.8778, 95.8583)), ("密铁拉", (20.8778, 95.8583)),
    ("lashio", (22.9333, 97.7500)), ("腊戌", (22.9333, 97.7500)),
    ("myeik", (12.4395, 98.6003)), ("丹老", (12.4395, 98.6003)),
    ("magway", (20.1500, 94.9320)), ("马圭", (20.1500, 94.9320)),
)


def _city_ref(text):
    """从文本（如 ...+XX Yangon）中识别缅甸城市，返回其参考坐标；识别不到返回 None"""
    s = str(text or "").lower()
    if not s:
        return None
    for name, ref in _MM_CITY_REF:
        if name in s:
            return ref
    return None


def parse_plus_code_text(text, ref_lat=None, ref_lng=None):
    """从任意文本中提取 Plus Code 并解码（兼容前后夹带地名）"""
    if not text:
        return None
    if ref_lat is None or ref_lng is None:
        cr = _city_ref(text)
        if cr:
            ref_lat, ref_lng = cr[0], cr[1]
    return decode_plus_code(text, ref_lat, ref_lng)


def parse_coords_text(text):
    """从用户输入文本提取 纬度,经度。支持：逗号分隔 / geo: 前缀 / 度分秒(DMS) / 16.8°N 96.2°E"""
    if not text:
        return None
    s = str(text).strip()
    s = re.sub(r"^geo:", "", s, flags=re.I)
    m = re.search(r"(-?\d{1,3}(?:\.\d+)?)\s*[,،]\s*(-?\d{1,3}(?:\.\d+)?)", s)
    if m:
        return swap_if_reversed(m.group(1), m.group(2))
    # DMS：16°51'22.0"N 96°14'18.0"E 或 16°51.37'N 96°14.3'E
    dms = re.findall(
        r"(\d{1,3})\s*[°º]\s*(?:(\d{1,2}(?:\.\d+)?)\s*['′]\s*)?(?:(\d{1,2}(?:\.\d+)?)\s*[\"″]\s*)?\s*([NSEW])",
        s, re.I,
    )
    if len(dms) >= 2:
        vals = {}
        for d, mi, se, hemi in dms:
            v = float(d) + (float(mi) / 60.0 if mi else 0.0) + (float(se) / 3600.0 if se else 0.0)
            if hemi.upper() in ("S", "W"):
                v = -v
            vals[hemi.upper()] = v
        lat = vals.get("N", vals.get("S"))
        lng = vals.get("E", vals.get("W"))
        if lat is not None and lng is not None:
            return swap_if_reversed(lat, lng)
    return None


# 业务所在区域（缅甸及周边）：手机端短链在服务器侧常抓到同意页/首页正文，
# 里面可能夹带无关坐标，一旦采用就会把骑手定位到国外。超出该范围一律判为无效。
REGION_BOX = (5.0, 30.0, 88.0, 108.0)  # lat_min, lat_max, lng_min, lng_max


def in_region(lat, lng):
    """坐标是否落在业务区域（缅甸及周边）内"""
    try:
        la = float(lat)
        ln = float(lng)
    except (TypeError, ValueError):
        return False
    if la != la or ln != ln:  # NaN
        return False
    a, b, c, d = REGION_BOX
    return a <= la <= b and c <= ln <= d


def swap_if_reversed(lat, lng):
    """经纬度反序纠正：手机端复制/上报的坐标可能是「经度,纬度」，
    直接当纬度用会把位置画反。反序后落在业务区域内就交换。"""
    try:
        la = float(lat)
        ln = float(lng)
    except (TypeError, ValueError):
        return lat, lng
    if la != la or ln != ln:
        return lat, lng
    if -90.0 <= la <= 90.0 and -180.0 <= ln <= 180.0:
        if in_region(la, ln):
            return la, ln
        if -90.0 <= ln <= 90.0 and in_region(ln, la):
            return ln, la
        return la, ln
    if -90.0 <= ln <= 90.0:
        return ln, la
    return la, ln


_LINK_PATTERNS = (
    r"!3d(-?\d{1,3}\.\d+)!4d(-?\d{1,3}\.\d+)",
    r"[?&](?:q|query|ll|daddr|destination|center|sll|viewpoint|location)=(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)",
    r"/place/(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)",
    r"/search/(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)",
    r"@(-?\d{1,3}\.\d+),(-?\d{1,3}\.\d+)",
)


def parse_link_coords(text, strict=True, allow_view_center=True):
    """从 Google Maps 链接提取坐标：!3d..!4d / ?q= / ?ll= / /place/lat,lng / @lat,lng（支持 URL 编码）。
    strict=True 时只返回业务区域内的坐标：宁可解析失败、让骑手改填坐标或 Plus Code，
    也绝不把商家位置落到国外去。"""
    s = str(text or "")
    # 手机端从 Google Maps App / 微信复制出来的链接常被转义成 &amp;、\u0026，先还原再解析
    s = s.replace("&amp;", "&").replace("&#38;", "&").replace("\\u0026", "&")
    try:
        if re.search(r"%[0-9A-Fa-f]{2}", s):
            s = urllib.parse.unquote(s)
    except Exception:
        pass
    if not s:
        return None
    # Google 跳转链里常见的反序写法：!2d<经度>!3d<纬度>（与 !3d!4d 等价，只是顺序相反）
    m = re.search(r"!2d(-?\d{1,3}\.\d+)!3d(-?\d{1,3}\.\d+)", s, re.I)
    if m:
        try:
            cand = (float(m.group(2)), float(m.group(1)))
            if (not strict) or in_region(cand[0], cand[1]):
                return cand
        except (TypeError, ValueError):
            pass
    first = None
    for pat in (_LINK_PATTERNS if allow_view_center else _LINK_PATTERNS[:-1]):
        for m in re.finditer(pat, s):
            try:
                cand = (float(m.group(1)), float(m.group(2)))
            except (TypeError, ValueError):
                continue
            if first is None:
                first = cand
            if in_region(cand[0], cand[1]):
                return cand
    if strict:
        return None
    return first


# 手机端分享的链接常见形态：无协议头、前后夹带文字、maps.app.goo.gl 短链
_MAPS_UA_LIST = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
)


def normalize_maps_link(raw):
    """把用户粘贴的内容规范成可请求的 Google 地图链接"""
    s = str(raw or "").strip()
    if not s:
        return ""
    m = re.search(r"https?://[^\s\"'<>]+", s, re.I)
    if m:
        return m.group(0)
    m = re.search(r"(?:maps\.app\.goo\.gl|goo\.gl/maps|(?:www\.)?google\.[a-z.]{2,}/maps)[^\s\"'<>]*", s, re.I)
    if m:
        return "https://" + m.group(0)
    return s


# 短链跳转后的页面正文里常见的结构化坐标（均要求 3 位以上小数，避免误配无关数字）
_BODY_PATTERNS = (
    r'\[null,null,(-?\d{1,3}\.\d{3,}),(-?\d{1,3}\.\d{3,})\]',
    r'"lat"\s*:\s*(-?\d{1,3}\.\d{3,})\s*,\s*"lng"\s*:\s*(-?\d{1,3}\.\d{3,})',
    r'\[(-?\d{1,3}\.\d{4,}),(-?\d{1,3}\.\d{4,})\]',
)


def parse_body_coords(body):
    """从短链落地页正文里提取坐标，只接受业务区域内的结果"""
    if not body:
        return None
    for pat in _BODY_PATTERNS:
        for m in re.finditer(pat, body):
            try:
                la, ln = float(m.group(1)), float(m.group(2))
            except (TypeError, ValueError):
                continue
            if in_region(la, ln):
                return (la, ln)
    return None


def _fetch_maps_page(url):
    """抓取地图链接（跟随重定向）；返回 (final_url, body)。多 UA 轮询 + 证书兜底，兼容手机端短链。"""
    try:
        import ssl as _ssl
        _no_verify = _ssl._create_unverified_context()
    except Exception:
        _no_verify = None
    for ua in _MAPS_UA_LIST:
        for ctx in (None, _no_verify):
            if ctx is None and _no_verify is not None and ua == _MAPS_UA_LIST[-1]:
                continue
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": ua,
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Cookie": "CONSENT=YES+cb; SOCS=CAI",
                })
                with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                    final_url = resp.geturl()
                    try:
                        body = resp.read(400000).decode("utf-8", "ignore")
                    except Exception:
                        body = ""
                if final_url or body:
                    return final_url or url, body or ""
            except Exception:
                continue
    return url, ""


def resolve_shop_location(link, coords_text, plus_code=""):
    """骑手输入的商家位置解析（服务端权威解析，手机/电脑结果完全一致）：
    优先级 坐标文本（含 DMS/geo:）→ Plus Code → 链接正则 → 展开短链（多 UA，含重定向链与页面正文）。
    所有结果都会校验是否落在业务区域内，绝不让手机端链接把位置定位到国外。
    返回 (lat, lng, link)；解析失败时 lat/lng 为 None。"""
    # 坐标文本必须是业务区域内的：手机端把 Google 链接解析成坐标时若带出国外坐标，
    # 这里直接丢弃，继续走链接/Plus Code 解析，绝不把商家点落到国外
    c = parse_coords_text(coords_text)
    if c and in_region(c[0], c[1]):
        return c[0], c[1], (link or "").strip()
    pc = parse_plus_code_text(coords_text)
    if pc and in_region(pc[0], pc[1]):
        return pc[0], pc[1], (link or "").strip()
    pc = parse_plus_code_text(plus_code)
    if pc and in_region(pc[0], pc[1]):
        return pc[0], pc[1], (link or "").strip()
    # 链接里若夹带了 plus code（/place/7M8R+XX...）也一并识别
    url = normalize_maps_link(link)
    if not url:
        return None, None, ""
    pc = parse_plus_code_text(url)
    if pc and in_region(pc[0], pc[1]):
        return pc[0], pc[1], url
    # 链接里已明确写死区外坐标（如 @40.71,-74.00）：判定为不支持的链接，直接拒绝，
    # 不再去抓页面乱配一个区域内坐标，避免骑手被导到错误位置
    _any = parse_link_coords(url, strict=False)
    if _any and not in_region(_any[0], _any[1]):
        return None, None, url
    c = parse_link_coords(url, allow_view_center=False)
    if c:
        return c[0], c[1], url
    if not url.lower().startswith(("http://", "https://")):
        return None, None, url
    final_url, body = _fetch_maps_page(url)
    c = parse_link_coords(final_url, allow_view_center=False)
    if c:
        return c[0], c[1], url
    if body:
        c = parse_link_coords(body, allow_view_center=False)
        if c:
            return c[0], c[1], url
        pc = parse_plus_code_text(body)
        if pc and in_region(pc[0], pc[1]):
            return pc[0], pc[1], url
        c = parse_body_coords(body)
        if c:
            return c[0], c[1], url
        # 二次跳转：部分手机端短链落地页只给一个中转链接（maps.google.com/?cid=… 或 /maps/place/…），
        # 再抓一次即可拿到带 !3d/!4d 的真实地址，避免手机端解析失败后退回原链接、定位到国外。
        m2 = re.search(r"https?://(?:www\.)?google\.[a-z.]{2,}/maps[^\s\"'<>\\]+", body)
        if m2:
            u2 = m2.group(0).replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")
            if u2 and u2 != url:
                f2, b2 = _fetch_maps_page(u2)
                c2 = parse_link_coords(f2) or (parse_link_coords(b2) if b2 else None) \
                    or (parse_body_coords(b2) if b2 else None)
                if c2:
                    return c2[0], c2[1], url
    return None, None, url


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
    conn = get_db()
    cur = conn.cursor()
    # 订单号曾用「秒级时间戳」，同一秒内重复点击/多人同时下单会撞主键，
    # 直接抛 UNIQUE 约束异常 → 500 → 前端误报「服务未启动」。
    # 现在加 3 位随机后缀，并做冲突自动重试，彻底消除该 500。
    order_no = None
    last_err = None
    for _try in range(8):
        cand = "BZ" + datetime.now().strftime("%m%d%H%M%S") + str(random.randint(100, 999))
        try:
            cur.execute(
                _q("INSERT INTO orders (order_no, customer_name, address, status, created_at) VALUES (?,?,?,?,?)"),
                (cand, customer_name, address, "待取餐", now_str()),
            )
            order_no = cand
            break
        except Exception as _e:  # 主键冲突等：回滚后换号重试
            last_err = _e
            try:
                conn.rollback()
            except Exception:
                pass
    if not order_no:
        conn.close()
        return jsonify({"ok": False,
                        "msg": "အမှာစာနံပါတ် ဖန်တီး၍ မရပါ၊ ထပ်မံကြိုးစားပါ (订单号生成冲突，请重试)",
                        "err": str(last_err)[:120]}), 500
    try:
        cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                    (order_no, "", "order", "新订单：" + order_no + "（客户 " + (customer_name or "-") + "）", utc_iso_now()))
        conn.commit()
    except Exception:
        # 通知写失败不能连带把订单判为失败
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn2 = get_db()
            cur2 = conn2.cursor()
            cur2.execute(_q("INSERT INTO orders (order_no, customer_name, address, status, created_at) VALUES (?,?,?,?,?)"),
                         (order_no, customer_name, address, "待取餐", now_str()))
            conn2.commit()
            conn2.close()
        except Exception:
            pass
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
    shop_location_plus = (data.get("shop_location_plus_code") or data.get("shop_location_plus") or "").strip()
    shop_lat, shop_lng, shop_link = resolve_shop_location_ex(shop_location_link, shop_location_coords, shop_location_plus)
    # 手机端链接解析不出区域内坐标时（短链被 Google 定向到国外/被拦截），绝不把原链接当成可用位置存下去：
    # 否则骑手在手机上一点就跑到国外。此时置空链接并回告骑手改用坐标或 Plus Code。
    shop_warn = ""
    if shop_lat is None and (shop_location_link or shop_location_coords or shop_location_plus):
        shop_warn = "ဆိုင်တည်နေရာ ဖတ်၍မရပါ — Google link သည် နိုင်ငံခြားသို့ ရောက်နေပါသည်။ Coordinates သို့မဟုတ် Plus Code ဖြင့် ပြန်ထည့်ပါ"
        shop_link = ""
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
    # 数量口径（严格锁定）：分母 = 骑手第一次输入的数量，同一天内第二次/第三次输入任何值都不改动；
    # 跨天（缅甸时间凌晨 0 点）自动清零，允许按新的第一次输入重新锁定。
    old = _row_to_dict(cur, order)
    # 【数量校验】一切按「这个骑手 + 这个订单」自己的数字判断：
    # 同一单已经送出过一部分后再填写，输入值必须等于剩余量；
    # 换了新订单号则属于这一单的首次录入，允许填写新的数量（不再被上一单的数字拦住）。
    _sv = rider_qty_view(cur, phone, None, order_no)
    if _sv.get("scope") == "rider_order":
        _sv_br = _to_int(_sv.get("baozi_received"))
        _sv_sr = _to_int(_sv.get("shop_received"))
        if _sv_br > 0 or _sv_sr > 0:
            _sv_rb = _to_int(_sv.get("baozi_remaining"))
            _sv_rs = _to_int(_sv.get("shop_remaining"))
            if baozi_total != _sv_rb or shop_total != _sv_rs:
                conn.close()
                return jsonify({"ok": False, "msg": "အရေအတွက် မကိုက်ညီပါ — ကျန်ရှိသည် ပေါက်စီ %d / ဆိုင် %d သာ ဖြစ်သည်။ ကျန်ရှိသော အရေအတွက်အတိုင်းသာ ထည့်ပါ" % (_sv_rb, _sv_rs)}), 400
    _in_bt, _in_st = baozi_total, shop_total
    old_bt_orig = int(old.get("baozi_total_orig") or 0)
    old_st_orig = int(old.get("shop_total_orig") or 0)
    today = now_str()[:10]
    lock_day = (old.get("qty_lock_day") or "").strip()
    locked = bool(lock_day) and lock_day == today and (old_bt_orig > 0 or old_st_orig > 0)
    if locked:
        # 本单还没送出过任何数量时，最新输入即分母（否则地图端永远显示第一次输入的旧数）
        try:
            _p_bt, _p_br, _p_st, _p_sr = order_counts(old)
        except Exception:
            _p_br, _p_sr = 0, 0
        if _p_br <= 0 and _p_sr <= 0:
            locked = False
    if locked:
        baozi_orig, shop_orig = old_bt_orig, old_st_orig
    else:
        # 跨天 / 首次绑定：以本次输入为准；本次未填（0）时回退到已有原始量，避免清成 0
        baozi_orig = baozi_total if baozi_total > 0 else old_bt_orig
        shop_orig = shop_total if shop_total > 0 else old_st_orig
        lock_day = today
    baozi_total = baozi_orig
    shop_total = shop_orig
    # 骑手独立分母：以「这个骑手今天第一次输入的数量」为准，同一天内重新下单/换订单号都不会改写
    rider_bt_orig, rider_st_orig = rider_qty_lock(cur, phone, order_no, _in_bt or baozi_orig, _in_st or shop_orig)
    rider_qty = rider_qty_view(cur, phone, None, order_no)
    if rider_qty["scope"] == "none":
        _o_bt, _o_br, _o_st, _o_sr = order_counts(old)
        rider_qty = {"scope": "order", "day": today,
                     "baozi_total": _o_bt, "baozi_received": _o_br, "baozi_remaining": max(_o_bt - _o_br, 0),
                     "shop_total": _o_st, "shop_received": _o_sr, "shop_remaining": max(_o_st - _o_sr, 0)}
    # 【多骑手保护】该订单已被多个骑手用过时，绝不改写订单级数量字段，否则会把 A 骑手的数写成 B 骑手的数
    _multi = (order_rider_count(cur, order_no) > 1) or _order_other_rider_bound(cur, order_no, phone)
    _upd_bt = old_bt_orig if _multi else baozi_total
    _upd_st = old_st_orig if _multi else shop_total
    _upd_bo = old_bt_orig if _multi else baozi_orig
    _upd_so = old_st_orig if _multi else shop_orig
    cur.execute(
        _q("UPDATE orders SET delivery_address=?, baozi_total=?, shop_total=?, baozi_total_orig=?, shop_total_orig=?, qty_lock_day=?, city=?, gps_enabled=1, shop_lat=?, shop_lng=?, shop_link=? WHERE order_no=?"),
        (delivery_address, _upd_bt, _upd_st, _upd_bo, _upd_so, lock_day, city, shop_lat, shop_lng, shop_link, order_no),
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
                    "shop_lat": shop_lat, "shop_lng": shop_lng, "shop_link": shop_link,
                    "shop_warn": shop_warn,
                    # 骑手独立口径：分母是该骑手今天第一次输入的数量，已送只算这个骑手自己送的
                    "baozi_total_orig": rider_bt_orig, "shop_total_orig": rider_st_orig,
                    "baozi_received": rider_qty.get("baozi_received", 0),
                    "shop_received": rider_qty.get("shop_received", 0),
                    "baozi_remaining": rider_qty.get("baozi_remaining", 0),
                    "shop_remaining": rider_qty.get("shop_remaining", 0),
                    "scope": rider_qty.get("scope", "order"),
                    "day": today,
                    "qty_lock_day": lock_day})


@app.after_request
def _api_no_store(resp):
    """接口一律禁缓存：商家端轮询到取消/完成状态必须立即生效，不能拿到旧响应。"""
    try:
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
    except Exception:
        pass
    return resp


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
        lat, lng = swap_if_reversed(lat, lng)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "msg": "လတ္တီတွဒ်/လောင်ဂျီတွဒ် ပုံစံ မမှန်ပါ"}), 400
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    rider = rider_row_by_phone(cur, phone, order_no)
    if not rider:
        conn.close()
        return jsonify({"ok": False, "msg": "ဤဖုန်းနံပါတ်သည် ဤအမှာစာနှင့် ချိတ်ဆက်မထားပါ။ ဦးစွာ ချိတ်ဆက်ပါ"}), 403
    cur.execute(
        _q("INSERT INTO locations (order_no, phone, lat, lng, status, is_sim, report_time) VALUES (?,?,?,?,?,?,?)"),
        (order_no, phone, lat, lng, status, is_sim, now_str()),
    )
    # 仅当快递员已选择并确认状态时才更新订单状态（未选择前保持待取餐，不显示配送中）
    # 终态保护：订单已完成(✅)或已取消(❌)后，定时的 GPS 上报绝不允许把状态覆盖回「配送中」，
    # 否则商家端会看到状态卡住/回跳，造成手机与电脑不一致。
    _FINAL_STATUS = ("လက်ခံပြီးပါပြီ", "ပယ်ဖျက်ထားသည်", "待取餐")
    if status and status not in _FINAL_STATUS:
        cur.execute(
            _q("UPDATE orders SET status=? WHERE order_no=? AND (status IS NULL OR status='' OR (status<>? AND status<>?))"),
            (status, order_no, "လက်ခံပြီးပါပြီ", "ပယ်ဖျက်ထားသည်"),
        )
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


def canonical_maps_link(url, lat, lng):
    """把商家地图链接统一成「带区域内坐标的 Google 链接」：
    手机端分享的短链（maps.app.goo.gl/xxx）本身不带坐标，手机点开时可能被 Google 落在别的点；
    只要链接里没有区域内坐标，就一律替换成按坐标搜索的链接，
    保证手机端与电脑端打开后落到同一个位置（骑手不会再被导到国外）。"""
    u = (url or "").strip()
    if u:
        try:
            c = parse_link_coords(u, strict=False)
        except Exception:
            c = None
        if c and in_region(c[0], c[1]):
            return u
    if lat is None or lng is None:
        return u
    try:
        return "https://www.google.com/maps/search/?api=1&query=%.6f,%.6f" % (float(lat), float(lng))
    except (TypeError, ValueError):
        return u


def resolve_shop_location_ex(link, coords_text, plus_code=""):
    """在 resolve_shop_location 基础上，把返回的链接规范成坐标链接（手机/电脑一致）"""
    la, ln, lk = resolve_shop_location(link, coords_text, plus_code)
    if la is None or ln is None:
        # 解析失败绝不把原始链接（很可能指向国外默认点）交给前端，统一返回空，由前端提示重填
        return None, None, ""
    return la, ln, canonical_maps_link(lk, la, ln)


@app.route("/api/rider/update-shop", methods=["POST"])
def rider_update_shop():
    """骑手绑定后补充/更新商家位置（Link 或 Coordinates），供地图标记与引导路线使用"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    link = (data.get("shop_location_link") or "").strip()
    coords_text = (data.get("shop_location_coords") or "").strip()
    plus_text = (data.get("shop_location_plus_code") or data.get("shop_location_plus") or "").strip()
    if not order_no:
        return jsonify({"ok": False, "msg": "Order number required"}), 400
    shop_lat, shop_lng, shop_link = resolve_shop_location_ex(link, coords_text, plus_text)
    if shop_lat is None or shop_lng is None:
        return jsonify({"ok": False, "msg": "Cannot resolve shop location. Please paste a Google Maps link that contains coordinates, or fill Plus Code inside Myanmar"}), 400
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
    try:
        return_baozi = max(int(data.get("return_baozi") or 0), 0)
    except (TypeError, ValueError):
        return_baozi = 0
    address = (data.get("address") or "").strip()
    payment_method = (data.get("payment_method") or "").strip()
    amount = (data.get("amount") or "").strip()
    proof_url = (data.get("proof_url") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    rider = rider_row_by_phone(cur, phone, order_no)
    if not rider:
        conn.close()
        return jsonify({"ok": False, "msg": "ဤဖုန်းနံပါတ်သည် ဤအမှာစာနှင့် ချိတ်ဆက်မထားပါ"}), 403
    cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
    order = cur.fetchone()
    order_dict = _row_to_dict(cur, order)
    _st_now = (order_dict.get("status") or "").strip()
    # 幂等保护：Confirm ✅ 只允许生效一次，重复点击 / 手机电脑同时点击都不会二次累加数量或重复通知
    if _st_now == "လက်ခံပြီးပါပြီ":
        _rvA = rider_qty_view(cur, phone, order_dict)
        conn.close()
        return jsonify({"ok": True, "already": True, "msg": "ဤအမှာစာ လက်ခံပြီးသားဖြစ်ပါသည်",
                        "status": "လက်ခံပြီးပါပြီ",
                        "baozi_received": _rvA["baozi_received"], "baozi_remaining": _rvA["baozi_remaining"],
                        "baozi_total_orig": _rvA["baozi_total"], "shop_received": _rvA["shop_received"],
                        "shop_remaining": _rvA["shop_remaining"], "shop_total_orig": _rvA["shop_total"],
                        "scope": _rvA["scope"], "day": _rvA["day"],
                        "telegram": False, "telegram_msg": "duplicate ignored"})
    if _st_now == "ပယ်ဖျက်ထားသည်":
        conn.close()
        return jsonify({"ok": False, "msg": "ဤအမှာစာကို ပယ်ဖျက်ပြီးဖြစ်ပါသည်။ Confirm လုပ်၍ မရပါ"})
    # 问题3：确认✅ 必须先按顺序点完「အစားအစာယူရန် → စောင့်ဆိုင်းပို့ဆောင်နေသည် → ရောက်ရှိပြီး」三个状态
    # 问题4：已取消❌ 的订单在后端同样禁止确认（前端禁用之外的第二道闸门，防作弊）
    if not three_status_done(cur, order_no, phone):
        conn.close()
        return jsonify({"ok": False, "gate": "status_order",
                        "msg": "အခြေအနေ ၃ ခု（အစားအစာယူရန် → စောင့်ဆိုင်းပို့ဆောင်နေသည် → ရောက်ရှိပြီး）အစဉ်လိုက် အတည်ပြုပြီးမှ Confirm လုပ်နိုင်ပါသည်။（请先按顺序点完三个状态再确认）"})
    new_baozi = (order_dict.get("baozi_received", 0) or 0) + confirm_baozi
    new_shop = (order_dict.get("shop_received", 0) or 0) + 1  # 每确认一个商家地址 +1
    cur.execute(
        _q("UPDATE orders SET baozi_received=?, shop_received=?, delivery_address=?, payment_method=?, amount=?, status='လက်ခံပြီးပါပြီ', completed_at=? "
           "WHERE order_no=? AND (status IS NULL OR status='' OR status<>'လက်ခံပြီးပါပြီ') AND (status IS NULL OR status<>'ပယ်ဖျက်ထားသည်')"),
        (new_baozi, new_shop, address, payment_method, amount, now_str(), order_no),
    )
    if cur.rowcount == 0:
        # 并发重复提交：另一请求已先完成，本次不再累加、不再发通知
        cur.execute(_q("SELECT * FROM orders WHERE order_no=?"), (order_no,))
        _dup = _row_to_dict(cur, cur.fetchone())
        _rvB = rider_qty_view(cur, phone, _dup)
        conn.close()
        return jsonify({"ok": True, "already": True, "msg": "ဤအမှာစာ လက်ခံပြီးသားဖြစ်ပါသည်",
                        "status": _dup.get("status") or "",
                        "baozi_received": _rvB["baozi_received"], "baozi_remaining": _rvB["baozi_remaining"],
                        "baozi_total_orig": _rvB["baozi_total"], "shop_received": _rvB["shop_received"],
                        "shop_remaining": _rvB["shop_remaining"], "shop_total_orig": _rvB["shop_total"],
                        "scope": _rvB["scope"], "day": _rvB["day"],
                        "telegram": False, "telegram_msg": "duplicate ignored"})
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
    tg_ok, tg_msg = False, "not sent"
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
        # 【只发一条】本次送出/退回数量先并入口径，避免通知里的剩余数量滞后一条
        _qv = rider_qty_view(cur, phone, od, order_no)
        _qcap = {
            "baozi_total": _to_int(_qv.get("baozi_total")),
            "baozi_received": _to_int(_qv.get("baozi_received")) + confirm_baozi,
            "baozi_returned": _to_int(_qv.get("baozi_returned")) + return_baozi,
            "shop_total": _to_int(_qv.get("shop_total")),
            "shop_received": _to_int(_qv.get("shop_received")) + 1,
        }
        if _to_int(_qcap.get("baozi_total")) <= 0:
            # 该骑手还没填过首次数量：通知里退回订单口径总量，避免出现 0 / 0
            _obt, _obr, _ost, _osr = order_counts(od)
            _qcap["baozi_total"] = _obt
            _qcap["shop_total"] = _ost
            if _to_int(_qcap.get("baozi_received")) <= _obr:
                _qcap["baozi_received"] = _obr + confirm_baozi
            if _to_int(_qcap.get("shop_received")) <= _osr:
                _qcap["shop_received"] = _osr + 1
        caption = build_proof_caption(order_no, phone, od, rider_name, latest, _qcap)
        photo_path = ""
        if proof_url:
            photo_rel = proof_url.lstrip("/")
            _pp = os.path.join(BASE_DIR, photo_rel.replace("/", os.sep))
            if os.path.exists(_pp):
                photo_path = _pp
        if photo_path:
            tg_ok, tg_msg = send_telegram_photo(photo_path, caption)
        else:
            tg_ok, tg_msg = send_telegram_text(caption)
        text_sent = bool(tg_ok)
    if not tg_ok and text_sent:
        tg_ok, tg_msg = True, "text sent"
    # 送出的数量只累加到「这个骑手自己」名下：其他骑手的数量完全不受影响
    _day = now_str()[:10]
    rider_qty_add(cur, phone, order_no, confirm_baozi, 1, return_baozi)
    conn.commit()
    _rvC = rider_qty_view(cur, phone, order_dict, order_no)
    conn.close()
    # 兜底：若该骑手该单还没有记录（没填过数量），按订单口径返回，保证前端不会显示 0
    if _rvC["scope"] != "rider_order":
        _bt_all, _, _st_all, _ = order_counts(order_dict)
        _rvC.update({
            "baozi_total": _bt_all, "baozi_received": new_baozi, "baozi_remaining": max(_bt_all - new_baozi, 0),
            "shop_total": _st_all, "shop_received": new_shop, "shop_remaining": max(_st_all - new_shop, 0),
        })
    # 问题1：退回数量必须回传真实值，绝不显示 0
    _rvC["baozi_returned"] = max(_to_int(_rvC.get("baozi_returned")), return_baozi)
    return jsonify({"ok": True, "msg": "လက်ခံပြီးပါပြီ", "status": "လက်ခံပြီးပါပြီ", "completed_at": now_str(),
                    "baozi_received": _rvC["baozi_received"], "baozi_remaining": _rvC["baozi_remaining"],
                    "baozi_total_orig": _rvC["baozi_total"],
                    "shop_received": _rvC["shop_received"], "shop_remaining": _rvC["shop_remaining"],
                    "shop_total_orig": _rvC["shop_total"],
                    "scope": _rvC["scope"], "day": _day,
                    "baozi_returned": _to_int(_rvC.get("baozi_returned")),
                    "payment_method": payment_method, "amount": amount,
                    "telegram": tg_ok, "telegram_msg": tg_msg})


@app.route("/api/rider/cancel", methods=["POST"])
def rider_cancel():
    """骑手取消订单：记录取消数量与原因，立即置为已取消并推送 Telegram 通知"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    cancel_baozi = max(_to_int(data.get("cancel_baozi") or 0), 0)
    cancel_reason = (data.get("cancel_reason") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    rider = rider_row_by_phone(cur, phone, order_no)
    rider_name = (data.get("rider_name") or "").strip()
    if rider:
        rd = _row_to_dict(cur, rider)
        rider_name = rd.get("rider_name", "") or rider_name
    else:
        # 该订单必须存在；不再因为「绑定已解除」而拒绝取消，
        # 否则骑手点了取消却静默失败，页面上仍停留在 ✅ Completed。
        cur.execute(_q("SELECT order_no FROM orders WHERE order_no=?"), (order_no,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "msg": "အမှာစာ မတွေ့ပါ"}), 404
    if not rider_name:
        cur.execute(_q("SELECT rider_name FROM order_history WHERE order_no=? AND rider_phone=? ORDER BY id DESC LIMIT 1"),
                    (order_no, phone))
        _hr = cur.fetchone()
        if _hr:
            rider_name = _row_to_dict(cur, _hr).get("rider_name", "") or ""
    # 最新位置（写进通知，与地图一致）
    loc_text = "-"
    cur.execute(_q("SELECT lat, lng FROM locations WHERE order_no=? AND is_sim=0 ORDER BY id DESC LIMIT 1"), (order_no,))
    _lr = cur.fetchone()
    if _lr:
        _ld = _row_to_dict(cur, _lr)
        try:
            loc_text = reverse_geocode(_ld.get("lat"), _ld.get("lng")) or "-"
        except Exception:
            loc_text = "%.6f, %.6f" % (_to_int(_ld.get("lat")), _to_int(_ld.get("lng")))
    # 问题4：已确认✅ 的订单禁止再取消❌（互斥，防作弊）；已取消的重复请求直接返回
    cur.execute(_q("SELECT status FROM orders WHERE order_no=?"), (order_no,))
    _oc_row = cur.fetchone()
    _st_now2 = ((_row_to_dict(cur, _oc_row) or {}).get("status") or "").strip() if _oc_row else ""
    if _st_now2 == "လက်ခံပြီးပါပြီ":
        conn.close()
        return jsonify({"ok": False, "gate": "already_done",
                        "msg": "ဤအမှာစာကို လက်ခံပြီးဖြစ်ပါသည်။ ပယ်ဖျက်၍ မရပါ（订单已确认完成，无法再取消）"})
    if _st_now2 == "ပယ်ဖျက်ထားသည်":
        try:
            _rvC = rider_qty_view(cur, phone, None, order_no)
            _retC = _to_int(_rvC.get("baozi_returned"))
        except Exception:
            _retC = 0
        conn.close()
        return jsonify({"ok": True, "already": True, "status": "ပယ်ဖျက်ထားသည်",
                        "baozi_returned": _retC,
                        "msg": "ဤအမှာစာကို ပယ်ဖျက်ပြီးသားဖြစ်ပါသည်"})
    # 问题3：取消❌ 同样必须先按顺序点完三个状态（确保骑手已到店/已到达），不需要等确认✅
    if not three_status_done(cur, order_no, phone):
        conn.close()
        return jsonify({"ok": False, "gate": "status_order",
                        "msg": "အခြေအနေ ၃ ခု（အစားအစာယူရန် → စောင့်ဆိုင်းပို့ဆောင်နေသည် → ရောက်ရှိပြီး）အစဉ်လိုက် အတည်ပြုပြီးမှ ပယ်ဖျက်နိုင်ပါသည်。（请先按顺序点完三个状态再取消）"})
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
    # 问题2：取消❌ 时填写的退回包子数量同样计入「ပြန်အပ်သောပေါက်စီအရေအတွက်」，
    # 与确认✅ 的退回量合并显示（只写本骑手「当天」那一条，绝不扣减任何送出量；凌晨 0 点自动归零）
    _ret_total = 0
    try:
        _day = now_str()[:10]
        _cr = rider_qty_dayrec(cur, phone, _day, order_no)
        if _cr and _cr.get("id"):
            _cb_old = _to_int(_cr.get("cancel_baozi"))
            cur.execute(_q("UPDATE rider_order_qty SET cancel_baozi=?, cancel_reason=?, updated_at=? WHERE id=?"),
                        (_cb_old + cancel_baozi, cancel_reason, now_str(), _cr.get("id")))
            _ret_total = _to_int(_cr.get("baozi_returned")) + _cb_old + cancel_baozi
        elif cancel_baozi > 0:
            cur.execute(_q("INSERT INTO rider_order_qty (phone, order_no, day, baozi_orig, shop_orig, baozi_received, shop_received, baozi_returned, cancel_baozi, cancel_reason, updated_at) "
                           "VALUES (?,?,?,0,0,0,0,0,?,?,?)"),
                        (phone, _rq_day_key(_day, order_no), _day, cancel_baozi, cancel_reason, now_str()))
            _ret_total = cancel_baozi
    except Exception:
        pass
    _msg = "订单已取消：%s（骑手 %s）取消数量 %d 原因 %s" % (order_no, rider_name or phone, cancel_baozi, cancel_reason or "-")
    cur.execute(_q("INSERT INTO notifications (order_no, phone, type, message, created_at) VALUES (?,?,?,?,?)"),
                (order_no, phone, "cancel", _msg, utc_iso_now()))
    conn.commit()
    conn.close()
    # 取消通知：与确认同样推送到 Telegram 群
    tg_ok, tg_msg = False, "telegram disabled"
    if TELEGRAM_BOT_TOKEN:
        try:
            tg_ok, tg_msg = send_telegram_text(build_cancel_caption(order_no, phone, rider_name, loc_text, cancel_baozi, cancel_reason))
        except Exception as e:
            tg_ok, tg_msg = False, str(e)
    return jsonify({"ok": True, "msg": "အော်ဒါ ပယ်ဖျက်ပြီးပါပြီ", "status": "ပယ်ဖျက်ထားသည်",
                    "cancel_baozi": cancel_baozi, "cancel_reason": cancel_reason,
                    "baozi_returned": _to_int(_ret_total),
                    "telegram": tg_ok, "telegram_msg": tg_msg})


@app.route("/api/rider/status", methods=["POST"])
def rider_status_set():
    """骑手点按状态时的服务端留痕：保证「三个状态按顺序点完」这件事在服务端可校验。
    不写定位（没有 GPS 的手机也能用），只更新订单状态 + 记一行留痕，绝不覆盖终态。"""
    data = request.get_json(silent=True) or {}
    order_no = (data.get("order_no") or "").strip()
    phone = (data.get("phone") or "").strip()
    status = (data.get("status") or "").strip()
    if not order_no or not phone or status not in _STATUS_SEQ:
        return jsonify({"ok": False, "msg": "အချက်အလက် မပြည့်စုံပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    rider = rider_row_by_phone(cur, phone, order_no)
    if not rider:
        # 订单必须存在且该骑手曾经绑定过，否则不允许推进状态
        cur.execute(_q("SELECT order_no FROM orders WHERE order_no=?"), (order_no,))
        if not cur.fetchone():
            conn.close()
            return jsonify({"ok": False, "msg": "အမှာစာ မတွေ့ပါ"}), 404
    # 问题4：订单已终态（确认✅/取消❌）后，状态不允许再改
    try:
        cur.execute(_q("SELECT status FROM orders WHERE order_no=?"), (order_no,))
        _ost = ((_row_to_dict(cur, cur.fetchone()) or {}).get("status") or "").strip()
    except Exception:
        _ost = ""
    if _ost in ("လက်ခံပြီးပါပြီ", "ပယ်ဖျက်ထားသည်"):
        conn.close()
        return jsonify({"ok": False, "gate": "final", "msg": "ဤအမှာစာ ပြီးဆုံးပြီးဖြစ်ပါသည်（该订单已完成或已取消，状态不能再改）"}), 400
    # 问题3：必须按顺序推进（取餐 → 配送中 → 到达），跳步一律拒绝
    if not status_predecessor_done(cur, order_no, phone, status):
        conn.close()
        _idx_now = _STATUS_IDX.get(status, 0)
        _need = _STATUS_SEQ[_idx_now - 1] if _idx_now > 0 else ""
        return jsonify({"ok": False, "gate": "status_order",
                        "msg": "အစဉ်လိုက် အတည်ပြုပါ（请先点前一个状态）：" + _need}), 400
    try:
        ensure_status_log(cur)
        cur.execute(_q("INSERT INTO rider_status_log (order_no, phone, status, created_at) VALUES (?,?,?,?)"),
                    (order_no, _rq_key(phone), status, now_str()))
    except Exception:
        pass
    # 订单状态同步（与 report_location 一致，但不覆盖完成/取消终态）
    try:
        cur.execute(
            _q("UPDATE orders SET status=? WHERE order_no=? AND (status IS NULL OR status='' OR (status<>'လက်ခံပြီးပါပြီ' AND status<>'ပယ်ဖျက်ထားသည်'))"),
            (status, order_no),
        )
    except Exception:
        pass
    conn.commit()
    _done3 = three_status_done(cur, order_no, phone)
    conn.close()
    return jsonify({"ok": True, "status": status, "three_done": bool(_done3)})


@app.route("/api/rider/info", methods=["GET"])
def rider_info():
    """快递员端刷新最新位置与剩余数量"""
    order_no = (request.args.get("order_no") or "").strip()
    phone = (request.args.get("phone") or "").strip()
    if not order_no or not phone:
        return jsonify({"ok": False, "msg": "အမှာစာနံပါတ် သို့မဟုတ် ဖုန်းနံပါတ် မပါရှိပါ"}), 400
    conn = get_db()
    cur = conn.cursor()
    rider = rider_row_by_phone(cur, phone, order_no)
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
    # 骑手独立口径：只返回这个骑手自己的数量，绝不与其他骑手混算
    _rv = rider_qty_view(cur, phone, order_dict)
    _three = three_status_done(cur, order_no, phone)
    conn.close()
    if latest:
        latest["address"] = reverse_geocode(latest["lat"], latest["lng"])
    return jsonify(
        {
            "ok": True,
            "order_no": order_no,
            "phone": phone,
            "three_done": bool(_three),
            "status": order_dict.get("status", ""),
            "delivery_address": order_dict.get("delivery_address", ""),
            "shop_lat": order_dict.get("shop_lat"),
            "shop_lng": order_dict.get("shop_lng"),
            "shop_link": order_dict.get("shop_link", "") or "",
            "city": order_dict.get("city", ""),
            "payment_method": order_dict.get("payment_method", ""),
            "amount": order_dict.get("amount", ""),
            "scope": _rv["scope"],
            "day": _rv["day"],
            "baozi_total": _rv["baozi_total"],
            "shop_total": _rv["shop_total"],
            "baozi_received": _rv["baozi_received"],
            "shop_received": _rv["shop_received"],
            "baozi_remaining": _rv["baozi_remaining"],
            "shop_remaining": _rv["shop_remaining"],
            "baozi_total_orig": _rv["baozi_total"],
            "shop_total_orig": _rv["shop_total"],
            "baozi_returned": _to_int(_rv.get("baozi_returned")),
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
        # 每个骑手只看自己这份数量：历史列表里也不会出现别的骑手 / 别的订单的数字
        _hrv = rider_qty_view(cur, phone, od, order_no)
        if _hrv["scope"] in ("rider_order", "rider_empty"):
            _h_bt, _h_br = _hrv["baozi_total"], _hrv["baozi_received"]
            _h_st, _h_sr = _hrv["shop_total"], _hrv["shop_received"]
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
                "baozi_remaining": max(_h_bt - _h_br, 0),
                "shop_remaining": max(_h_st - _h_sr, 0),
                "baozi_returned": _to_int(_hrv.get("baozi_returned")), "baozi_total_orig": _h_bt,
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
    # 一个订单可能被多个骑手先后绑定：必须按 phone+order_no 精确匹配，
    # 否则会取到别人的绑定记录，导致商家端状态/轨迹串号、显示卡住。
    rider = rider_row_by_phone(cur, phone, order_no)
    rider_dict = _row_to_dict(cur, rider) if rider else None
    rider_name_fallback = ""
    if not rider_dict or norm_phone(rider_dict.get("phone")) != norm_phone(phone):
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
    # 【同步骑手地图】第二次下单时新单号下的点数可能还只有 1 个，商家端会看不到路线；
    # 此时按同一骑手（手机号）当天的轨迹补齐，保证与骑手端地图显示一致。
    if len(track_list) < 2:
        _alt = []
        try:
            if phone:
                cur.execute(_q("SELECT lat, lng, status, report_time FROM locations WHERE phone=? AND is_sim=0 AND report_time>=? ORDER BY id ASC"),
                            (phone, now_str()[:10]))
                _alt = _rows_to_dicts(cur, cur.fetchall())
        except Exception:
            _alt = []
        if len(_alt) > len(track_list):
            track_list = _alt
    if len(track_list) < 2:
        try:
            cur.execute(_q("SELECT lat, lng, status, report_time FROM locations WHERE order_no=? ORDER BY id ASC"), (order_no,))
            _alt2 = _rows_to_dicts(cur, cur.fetchall())
            if len(_alt2) > len(track_list):
                track_list = _alt2
        except Exception:
            pass
    latest = track_list[-1] if track_list else None
    order_dict = _row_to_dict(cur, order)
    rider_dict = _row_to_dict(cur, rider) if rider else None
    _d_bt, _d_br, _d_st, _d_sr = order_counts(order_dict)
    # 商家端数量口径：只要带了骑手手机号，就必须用「这个骑手自己的第一次输入量」当分母、
    # 只算这个骑手自己的已送量，绝不与其它骑手或其它订单混算（与骑手页面完全一致）
    try:
        _rv = rider_qty_view(cur, phone, None, order_no)
        if _rv.get("scope") == "rider_order":
            _d_bt = _to_int(_rv.get("baozi_total"))
            _d_br = _to_int(_rv.get("baozi_received"))
            _d_st = _to_int(_rv.get("shop_total"))
            _d_sr = _to_int(_rv.get("shop_received"))
        elif _rv.get("scope") == "rider_empty":
            # 该骑手在本单还没有独立记录：如果这单只有一个骑手，订单级数字就是他自己的数，
            # 直接用（避免地图端突兀显示 0/0）；若出现过多骑手，保持 0，不用别人的量冒充。
            try:
                _cnt = order_rider_count(cur, order_no)
            except Exception:
                _cnt = 9
            if _cnt <= 1:
                _d_bt, _d_br, _d_st, _d_sr = order_counts(order_dict)
            else:
                _d_bt = _d_br = _d_st = _d_sr = 0
    except Exception as _e_rv:
        print("[track] rider qty view failed: %s" % _e_rv)
    # 说明：上面的 rider_qty_view 已严格按「本骑手+本单/本天」取值，无记录即 rider_empty(0)，
    # 不存在订单口径兜底，因此这里不能再对同单多骑手做清零——否则会误把本骑手自己的数量清成 0。
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
        # 按 phone+order_no 精确匹配骑手，避免多骑手绑定同一订单号时轨迹/状态串号
        rider = rider_row_by_phone(cur, phone, order_no)
        rider_dict = _row_to_dict(cur, rider) if rider else None
        rname = ""
        if not rider_dict or norm_phone(rider_dict.get("phone")) != norm_phone(phone):
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
        if len(track_list) < 2 and phone:
            try:
                cur.execute(_q("SELECT lat, lng, status, report_time FROM locations WHERE phone=? AND is_sim=0 AND report_time>=? ORDER BY id ASC"),
                            (phone, now_str()[:10]))
                _alt = _rows_to_dicts(cur, cur.fetchall())
                if len(_alt) > len(track_list):
                    track_list = _alt
            except Exception:
                pass
        latest = track_list[-1] if track_list else None
        # 批量接口不串行调用逆地理编码（10-20 个骑手时避免阻塞），前端以坐标兜底显示
        # 数量口径必须按「这个骑手自己」取，绝不共用订单口径，否则多骑手列表里所有人的数量会一模一样
        _m_bt, _m_br, _m_st, _m_sr = order_counts(order_dict)
        try:
            _rvm = rider_qty_view(cur, phone, order_dict, order_no)
            if _rvm.get("scope") in ("rider_order", "rider_empty"):
                _m_bt = _to_int(_rvm.get("baozi_total"))
                _m_br = _to_int(_rvm.get("baozi_received"))
                _m_st = _to_int(_rvm.get("shop_total"))
                _m_sr = _to_int(_rvm.get("shop_received"))
        except Exception as _e_m:
            print("[track/multi] rider qty view failed: %s" % _e_m)
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
APP_VERSION = "20261110-v3"


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
    # 骑手独立口径：每个骑手只看自己那份数量——
    # 分母 = 该骑手在该单第一次输入的量（重新下单也不会变成 500），已收 = 该骑手在该单累计送出的量
    rv = rider_qty_view(cur, phone, od, (od or {}).get("order_no") or order_no)
    _multi_rider = order_rider_count(cur, (od or {}).get("order_no") or order_no)
    conn.close()
    if rv["scope"] == "none":
        return jsonify({"ok": False, "msg": "no order"}), 404
    today = now_str()[:10]
    if rv["scope"] in ("rider_order", "rider_empty"):
        bt, br_day = rv["baozi_total"], rv["baozi_received"]
        st, sr_day = rv["shop_total"], rv["shop_received"]
    elif not phone:
        bt, br, st, sr = order_counts(od)
        # 订单口径兜底：只统计「当天」已收数量，跨天（凌晨 0 点后）自动归零；分母仍是首次输入量
        done_day = (od.get("completed_at") or "")[:10]
        if done_day and done_day != today:
            br_day, sr_day = 0, 0
        else:
            br_day, sr_day = br, sr
    else:
        # 有骑手手机号但本单还没有他自己的数量记录：一律显示「待首次输入」，
        # 绝不用订单级数字（整单/别的骑手的量）冒充本骑手的分母 —— 这是 20 变 60、两骑手串号的根源。
        bt, st = 0, 0
        br_day, sr_day = 0, 0
    # 隔离兜底：同一订单有多个骑手时，绝不允许把订单级数字（别人的量）当成这个骑手的量。
    # 但只对"订单级兜底"（scope=order）生效——骑手自己已有记录时（scope=rider_order）
    # 那本来就是他自己的数，一律保留，绝不归零。
    if phone and _multi_rider > 1 and rv["scope"] == "order":
        bt, br_day, st, sr_day = 0, 0, 0, 0
    return jsonify({
        "ok": True,
        "day": today,
        "scope": rv["scope"],
        "multi_rider": _multi_rider,
        "phone": phone,
        "first_input": (bt <= 0),
        # 本单是否已有任何送出/退回进度：只有「已有进度」才要求再次输入必须等于剩余量，
        # 未送出前允许随意改填（分母 = 最新输入），否则骑手把 2000 改成 1200 会被前端拦死
        "has_progress": (_to_int(rv.get("baozi_received")) + _to_int(rv.get("shop_received"))
                         + _to_int(rv.get("baozi_returned"))) > 0,
        "order_no": (od or {}).get("order_no") or order_no or "",
        "status": (od or {}).get("status") or "",
        "completed_at": (od or {}).get("completed_at") or "",
        "baozi_total_orig": bt, "baozi_received": br_day, "baozi_remaining": max(bt - br_day, 0),
        "shop_total_orig": st, "shop_received": sr_day, "shop_remaining": max(st - sr_day, 0),
        "baozi_returned": _to_int(rv.get("baozi_returned")),
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
