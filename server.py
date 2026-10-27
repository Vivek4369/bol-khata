#!/usr/bin/env python3
"""Bol Khata: a dependency-free mobile web app with owner-scoped SQLite records."""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import sys
if __name__ == "__main__":
    sys.modules.setdefault("server", sys.modules[__name__])
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, date, timezone, timedelta
from decimal import Decimal, InvalidOperation
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
INDIA = ZoneInfo('Asia/Kolkata')
SESSION_SECONDS = 7 * 86400
MAX_AMOUNT = 1_000_000_000  # paise; INR 1 crore


class Problem(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status


def text(value, label, maximum=120, required=True):
    if not isinstance(value, str):
        raise Problem(f'{label} must be text.')
    value = value.strip()
    if (required and not value) or len(value) > maximum or any(ord(c) < 32 and not (label in ('Chat message','Message','Correction reason') and c in '\n\t') for c in value):
        raise Problem(f'Enter {label.lower()} using at most {maximum} characters.')
    return value


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise Problem('Amounts must be whole rupees or rupees with up to two decimal places.')
    if not re.fullmatch(r'\d{1,8}(?:\.\d{1,2})?', str(value)):
        raise Problem('Use a positive amount with at most two decimal places, without commas.')
    try:
        paise = int(Decimal(str(value)) * 100)
    except (InvalidOperation, ValueError):
        raise Problem('Invalid amount.')
    if paise < 0 or paise > MAX_AMOUNT:
        raise Problem('Amount must be between ₹0 and ₹1 crore.')
    return paise


def due_date(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise Problem('Choose a valid due date.')
    try:
        date.fromisoformat(value)
    except ValueError:
        raise Problem('Choose a valid due date.')
    return value


def today():
    return datetime.now(INDIA).date().isoformat()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


class Database:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS owners(
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, password_hash TEXT NOT NULL, salt TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS stores(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL UNIQUE REFERENCES owners(id),
                name TEXT NOT NULL, language TEXT NOT NULL DEFAULT 'hi', created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(
                token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES owners(id),
                csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS customers(
                id TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),
                name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
                alias TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, UNIQUE(id,store_id));
            CREATE TABLE IF NOT EXISTS entries(
                id TEXT PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),
                customer_id TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('opening','sale','payment')),
                sale INTEGER NOT NULL CHECK(sale>=0), paid INTEGER NOT NULL CHECK(paid>=0),
                due_date TEXT, method TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, business_date TEXT NOT NULL,
                request_id TEXT NOT NULL, voided_at TEXT, void_reason TEXT,
                FOREIGN KEY(customer_id,store_id) REFERENCES customers(id,store_id),
                UNIQUE(store_id,request_id));
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY, store_id TEXT NOT NULL REFERENCES stores(id),
                action TEXT NOT NULL, target TEXT NOT NULL, details TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(key TEXT NOT NULL, attempted_at REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS entries_by_store ON entries(store_id,created_at);
            CREATE INDEX IF NOT EXISTS customers_by_store ON customers(store_id);
            CREATE INDEX IF NOT EXISTS attempts_by_key ON attempts(key,attempted_at);
            ''')
            columns={row['name'] for row in db.execute('PRAGMA table_info(customers)')}
            for column,definition in [('email',"TEXT NOT NULL DEFAULT ''"),('sms_consent','INTEGER NOT NULL DEFAULT 0'),('email_consent','INTEGER NOT NULL DEFAULT 0')]:
                if column not in columns:
                    db.execute(f'ALTER TABLE customers ADD COLUMN {column} {definition}')
            db.execute('CREATE TABLE IF NOT EXISTS customer_requests(store_id TEXT NOT NULL,request_id TEXT NOT NULL,customer_id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(store_id,request_id))')
            db.execute("CREATE TABLE IF NOT EXISTS chat_messages(id INTEGER PRIMARY KEY, session_hash TEXT NOT NULL,store_id TEXT NOT NULL REFERENCES stores(id),role TEXT NOT NULL,content TEXT NOT NULL,created_at TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS balance_adjustments(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),customer_id TEXT NOT NULL,delta INTEGER NOT NULL,before_amount INTEGER NOT NULL,after_amount INTEGER NOT NULL,reason TEXT NOT NULL,due_date TEXT,created_at TEXT NOT NULL,business_date TEXT NOT NULL,request_id TEXT NOT NULL,FOREIGN KEY(customer_id,store_id) REFERENCES customers(id,store_id),UNIQUE(store_id,request_id))")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=64*1024*1024).hex()

    def throttle(self, ip, email):
        # Shared in SQLite across server threads; bounded window, no password data stored.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute('DELETE FROM attempts WHERE attempted_at < ?', (now-900,))
            keys = ['ip:'+digest(ip), 'email:'+digest(email)]
            for key in keys:
                count = db.execute('SELECT count(*) FROM attempts WHERE key=?', (key,)).fetchone()[0]
                if count >= (30 if key.startswith('ip:') else 10):
                    raise Problem('Too many attempts. Try again in 15 minutes.', 429)
            db.executemany('INSERT INTO attempts VALUES(?,?)', [(k,now) for k in keys])

    def start_session(self, db, owner_id):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('DELETE FROM sessions WHERE expires < ?', (time.time(),))
        db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(token),owner_id,csrf,time.time()+SESSION_SECONDS))
        return token, csrf

    def register(self, data, ip='local'):
        email = text(data.get('email'), 'Email', 254).lower()
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise Problem('Enter a valid email address.')
        name = text(data.get('name'), 'Owner name', 100)
        store_name = text(data.get('store'), 'Store name', 120)
        password = data.get('password')
        if not isinstance(password,str) or len(password)<12 or len(password)>128:
            raise Problem('Use a password between 12 and 128 characters.')
        self.throttle(ip,email)
        owner_id, store_id, salt = str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_hex(16)
        hashed = self.password_hash(password,salt)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT INTO owners VALUES(?,?,?,?,?,?)', (owner_id,email,name,hashed,salt,timestamp()))
            except sqlite3.IntegrityError:
                raise Problem('Unable to create this account. Try signing in instead.',409)
            db.execute('INSERT INTO stores VALUES(?,?,?,?,?)',(store_id,owner_id,store_name,'hi',timestamp()))
            token,csrf = self.start_session(db,owner_id)
        return token,csrf

    def login(self,data,ip='local'):
        email = text(data.get('email'),'Email',254).lower()
        password = data.get('password')
        if not isinstance(password,str) or len(password)>128:
            raise Problem('Email or password is incorrect.',401)
        self.throttle(ip,email)
        with self.connect() as db:
            owner = db.execute('SELECT * FROM owners WHERE email=?',(email,)).fetchone()
            hashed = self.password_hash(password, owner['salt'] if owner else '00'*16)
            if not owner or not hmac.compare_digest(hashed,owner['password_hash']):
                raise Problem('Email or password is incorrect.',401)
            token,csrf = self.start_session(db,owner['id'])
        return token,csrf

    def authenticate(self,token):
        if not token:
            raise Problem('Please sign in.',401)
        with self.connect() as db:
            row = db.execute('''SELECT owners.id owner_id, owners.name owner_name, owners.email,
                stores.id store_id, stores.name store_name, stores.language, sessions.csrf
                FROM sessions JOIN owners ON owners.id=sessions.owner_id
                JOIN stores ON stores.owner_id=owners.id WHERE token_hash=? AND expires>?''',
                (digest(token),time.time())).fetchone()
        if not row:
            raise Problem('Your session expired. Please sign in.',401)
        return dict(row)

    def logout(self,token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(digest(token),))

    def audit(self,db,store_id,action,target,details):
        db.execute('INSERT INTO audit(store_id,action,target,details,created_at) VALUES(?,?,?,?,?)',
                   (store_id,action,target,json.dumps(details,ensure_ascii=False),timestamp()))

    def customer(self,db,store_id,customer_id):
        row = db.execute('SELECT * FROM customers WHERE id=? AND store_id=?',(customer_id,store_id)).fetchone()
        if not row:
            raise Problem('Customer not found in your store.',404)
        return row

    def add_customer(self,store_id,data):
        name = text(data.get('name'),'Customer name',100)
        phone = text(data.get('phone',''),'Phone',25,False)
        if phone and not re.fullmatch(r'[+\d ()-]{7,25}',phone):
            raise Problem('Enter a valid phone number, or leave it blank.')
        alias = text(data.get('alias',''),'Voice alias',100,False)
        email,sms_consent,email_consent=self.contacts(data)
        opening = amount(data.get('opening','0'))
        due = due_date(data.get('due_date')) if opening else None
        request_id=data.get('request_id','')
        if request_id and (not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}',request_id)): raise Problem('Invalid request identifier.')
        payload=json.dumps([name,phone,alias,email,sms_consent,email_consent,opening,due],ensure_ascii=False)
        customer_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if request_id:
                previous=db.execute('SELECT * FROM customer_requests WHERE store_id=? AND request_id=?',(store_id,request_id)).fetchone()
                if previous:
                    if previous['payload']!=payload: raise Problem('This request was already used for a different customer.',409)
                    return previous['customer_id']
                db.execute('INSERT INTO customer_requests VALUES(?,?,?,?)',(store_id,request_id,customer_id,payload))
            db.execute('INSERT INTO customers(id,store_id,name,phone,alias,created_at,email,sms_consent,email_consent) VALUES(?,?,?,?,?,?,?,?,?)',(customer_id,store_id,name,phone,alias,timestamp(),email,sms_consent,email_consent))
            if opening:
                db.execute('''INSERT INTO entries(id,store_id,customer_id,kind,sale,paid,due_date,method,note,created_at,business_date,request_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(str(uuid.uuid4()),store_id,customer_id,'opening',opening,0,due,'none','Opening balance entered by owner',timestamp(),today(),str(uuid.uuid4())))
            self.audit(db,store_id,'customer_created',customer_id,{'opening':opening})
        return customer_id

    def edit_customer(self,store_id,customer_id,data):
        name=text(data.get('name'),'Customer name',100)
        phone=text(data.get('phone',''),'Phone',25,False)
        alias=text(data.get('alias',''),'Voice alias',100,False)
        email,sms_consent,email_consent=self.contacts(data)
        if phone and not re.fullmatch(r'[+\d ()-]{7,25}',phone):
            raise Problem('Enter a valid phone number, or leave it blank.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.customer(db,store_id,customer_id)
            db.execute('UPDATE customers SET name=?,phone=?,alias=?,email=?,sms_consent=?,email_consent=? WHERE id=? AND store_id=?',(name,phone,alias,email,sms_consent,email_consent,customer_id,store_id))
            self.audit(db,store_id,'customer_updated',customer_id,{'name':name,'phone':phone,'alias':alias})

    @staticmethod
    def contacts(data):
        email=text(data.get('email',''),'Email',254,False).lower()
        if email and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email):
            raise Problem('Enter a valid email address.')
        return email,int(data.get('sms_consent') in (True,'on')),int(data.get('email_consent') in (True,'on'))

    def chat(self,store,token,data=None):
        key=digest(token)
        with self.connect() as db:
            if data is not None:
                role=data.get('role')
                if role not in ('user','assistant'): raise Problem('Invalid chat role.')
                content=text(data.get('content'),'Chat message',8000)
                db.execute('INSERT INTO chat_messages(session_hash,store_id,role,content,created_at) VALUES(?,?,?,?,?)',(key,store,role,content,timestamp()))
                db.execute('DELETE FROM chat_messages WHERE session_hash=? AND id NOT IN (SELECT id FROM chat_messages WHERE session_hash=? ORDER BY id DESC LIMIT 200)',(key,key))
            return [dict(r) for r in db.execute('SELECT role,content,created_at FROM chat_messages WHERE session_hash=? AND store_id=? ORDER BY id',(key,store))]

    @staticmethod
    def balance(db,store_id,customer_id):
        ledger=db.execute('SELECT COALESCE(sum(sale-paid),0) FROM entries WHERE store_id=? AND customer_id=? AND voided_at IS NULL',(store_id,customer_id)).fetchone()[0]
        adjustments=db.execute('SELECT COALESCE(sum(delta),0) FROM balance_adjustments WHERE store_id=? AND customer_id=?',(store_id,customer_id)).fetchone()[0]
        return ledger+adjustments

    def adjust_balance(self,store_id,customer_id,data):
        target=amount(data.get('amount'))
        reason=text(data.get('reason'),'Correction reason',300)
        due=due_date(data.get('due_date'))
        request_id=text(data.get('request_id'),'Request identifier',64)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}',request_id): raise Problem('Invalid request identifier.')
        expected=amount(data['expected']) if 'expected' in data else None
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.customer(db,store_id,customer_id)
            previous=db.execute('SELECT * FROM balance_adjustments WHERE store_id=? AND request_id=?',(store_id,request_id)).fetchone()
            if previous:
                if (previous['customer_id'],previous['after_amount'],previous['reason'],previous['due_date'])!=(customer_id,target,reason,due): raise Problem('This request was already used for a different correction.',409)
                return dict(previous)
            before=self.balance(db,store_id,customer_id)
            if expected is not None and expected!=before: raise Problem('The balance changed. Refresh and review the current amount.',409)
            delta=target-before
            if not delta: raise Problem('The outstanding amount is already the same.')
            ident=str(uuid.uuid4());created=timestamp()
            db.execute('INSERT INTO balance_adjustments VALUES(?,?,?,?,?,?,?,?,?,?,?)',(ident,store_id,customer_id,delta,before,target,reason,due,created,today(),request_id))
            self.audit(db,store_id,'balance_corrected',ident,{'customer_id':customer_id,'before':before,'after':target,'reason':reason})
        return {'id':ident,'before_amount':before,'after_amount':target,'delta':delta,'reason':reason}

    def add_entry(self,store_id,data):
        customer_id=text(data.get('customer_id'),'Customer',36)
        kind=data.get('kind')
        if kind not in ('sale','payment'):
            raise Problem('Choose sale or payment.')
        sale=amount(data.get('sale','0')) if kind=='sale' else 0
        paid=amount(data.get('paid','0'))
        if (kind=='sale' and (not sale or paid>sale)) or (kind=='payment' and not paid):
            raise Problem('Enter a valid sale/payment amount. A sale payment cannot exceed its sale.')
        due=due_date(data.get('due_date')) if sale>paid else None
        method=data.get('method','none')
        if method not in ('cash','upi','card','bank','other','none') or (paid and method=='none'):
            raise Problem('Choose a payment method for money received.')
        note=text(data.get('note',''),'Note',300,False)
        request_id=text(data.get('request_id'),'Request identifier',64)
        if not re.fullmatch(r'[a-zA-Z0-9_-]{16,64}',request_id):
            raise Problem('Invalid request identifier.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.customer(db,store_id,customer_id)
            existing=db.execute('SELECT * FROM entries WHERE store_id=? AND request_id=?',(store_id,request_id)).fetchone()
            if existing:
                if existing['voided_at'] or (existing['customer_id'],existing['kind'],existing['sale'],existing['paid'],existing['due_date'],existing['method'],existing['note']) != (customer_id,kind,sale,paid,due,method,note):
                    raise Problem('This request was already used for a different entry.',409)
                return existing['id']
            balance=self.balance(db,store_id,customer_id)
            if kind=='payment' and paid>balance:
                raise Problem('Payment exceeds this customer’s current balance. Advance payments are not supported.',409)
            entry_id=str(uuid.uuid4())
            db.execute('''INSERT INTO entries(id,store_id,customer_id,kind,sale,paid,due_date,method,note,created_at,business_date,request_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(entry_id,store_id,customer_id,kind,sale,paid,due,method,note,timestamp(),today(),request_id))
            self.audit(db,store_id,'entry_created',entry_id,{'kind':kind,'sale':sale,'paid':paid})
        return entry_id

    def void_entry(self,store_id,entry_id,data):
        reason=text(data.get('reason'),'Correction reason',300)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            entry=db.execute('SELECT * FROM entries WHERE id=? AND store_id=?',(entry_id,store_id)).fetchone()
            if not entry:
                raise Problem('Entry not found in your store.',404)
            if entry['voided_at']:
                return
            rows=db.execute('SELECT * FROM entries WHERE customer_id=? AND store_id=? AND voided_at IS NULL ORDER BY rowid',(entry['customer_id'],store_id)).fetchall()
            rows=[dict(r) for r in rows]
            for a in db.execute('SELECT * FROM balance_adjustments WHERE customer_id=? AND store_id=?',(entry['customer_id'],store_id)):
                rows.append({'id':a['id'],'sale':max(a['delta'],0),'paid':max(-a['delta'],0),'created_at':a['created_at']})
            rows.sort(key=lambda r:r['created_at'])
            running=0
            for row in rows:
                if row['id']!=entry_id:
                    running+=row['sale']-row['paid']
                    if running<0:
                        raise Problem('Undo later payments first. This correction would leave a payment without enough credit.',409)
            db.execute('UPDATE entries SET voided_at=?,void_reason=? WHERE id=? AND store_id=?',(timestamp(),reason,entry_id,store_id))
            self.audit(db,store_id,'entry_voided',entry_id,{'reason':reason})

    def settings(self,store_id,owner_id,data):
        name=text(data.get('store'),'Store name',120)
        owner_name=text(data.get('name'),'Owner name',100)
        language=data.get('language')
        if language not in ('hi','en'):
            raise Problem('Choose Hindi or English.')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE stores SET name=?,language=? WHERE id=? AND owner_id=?',(name,language,store_id,owner_id))
            db.execute('UPDATE owners SET name=? WHERE id=?',(owner_name,owner_id))
            self.audit(db,store_id,'settings_updated',store_id,{'name':name,'language':language})

    def snapshot(self,store_id):
        with self.connect() as db:
            db.execute('BEGIN')
            customers=[dict(c) for c in db.execute('SELECT * FROM customers WHERE store_id=? ORDER BY name,id',(store_id,))]
            entries=[dict(e) for e in db.execute('SELECT * FROM entries WHERE store_id=? ORDER BY rowid',(store_id,))]
            adjustments=[dict(a) for a in db.execute('SELECT * FROM balance_adjustments WHERE store_id=? ORDER BY created_at',(store_id,))]
        for a in adjustments:
            entries.append({'id':a['id'],'store_id':store_id,'customer_id':a['customer_id'],'kind':'adjustment',
                'sale':max(a['delta'],0),'paid':max(-a['delta'],0),'method':'none','note':a['reason'],
                'due_date':a['due_date'],'created_at':a['created_at'],'business_date':a['business_date'],
                'voided_at':None,'void_reason':None,'before_amount':a['before_amount'],'after_amount':a['after_amount'],'delta':a['delta']})
        entries.sort(key=lambda e:e['created_at'])
        active=[e for e in entries if not e['voided_at']]
        for c in customers:
            lots=[]
            for e in active:
                if e['customer_id']!=c['id']:
                    continue
                if e['sale']:
                    if e['sale']>e['paid']:
                        lots.append({'remaining':e['sale']-e['paid'],'due_date':e['due_date'],'entry_id':e['id']})
                else:
                    payment=e['paid']
                    for lot in lots:
                        applied=min(payment,lot['remaining'])
                        lot['remaining']-=applied
                        payment-=applied
                        if not payment:
                            break
            c['lots']=[l for l in lots if l['remaining']]
            c['balance']=sum(l['remaining'] for l in c['lots'])
            c['overdue']=sum(l['remaining'] for l in c['lots'] if l['due_date'] and l['due_date']<today())
            c['due_today']=sum(l['remaining'] for l in c['lots'] if l['due_date']==today())
            c['undated']=sum(l['remaining'] for l in c['lots'] if not l['due_date'])
        day=[e for e in active if e['business_date']==today() and e['kind']!='adjustment']
        adjusted=sum(a['delta'] for a in adjustments if a['business_date']==today())
        closing=sum(c['balance'] for c in customers)
        new_credit=sum(e['sale']-e['paid'] for e in day if e['kind']=='sale')
        opening_added=sum(e['sale'] for e in day if e['kind']=='opening')
        old_collected=sum(e['paid'] for e in day if e['kind']=='payment')
        report={'date':today(),'closing':closing,'new_credit':new_credit,'opening_added':opening_added,
                'old_collected':old_collected,'receipts':sum(e['paid'] for e in day),
                'sales':sum(e['sale'] for e in day if e['kind']=='sale'),
                'opening':closing-new_credit-opening_added+old_collected-adjusted,'adjustments':adjusted,
                'overdue':sum(c['overdue'] for c in customers),'count':len(day),
                'methods':{m:sum(e['paid'] for e in day if e['method']==m) for m in ('cash','upi','card','bank','other')}}
        trend=[]
        for n in range(6,-1,-1):
            day_date=(date.fromisoformat(today())-timedelta(days=n)).isoformat()
            group=[e for e in active if e['business_date']==day_date and e['kind']!='adjustment']
            trend.append({'date':day_date,'sales':sum(e['sale'] for e in group if e['kind']=='sale'),'receipts':sum(e['paid'] for e in group)})
        return {'customers':customers,'entries':entries,'report':report,'trend':trend}


class Handler(BaseHTTPRequestHandler):
    server_version='BolKhata'
    def setup(self):
        super().setup()
        self.connection.settimeout(15)
    def log_message(self,fmt,*args):
        # Never log request bodies, identifiers in API paths, or cookies.
        pass

    def headers_out(self,code,content_type,cookie=None):
        self.send_response(code)
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header('Permissions-Policy','microphone=(self), camera=(), geolocation=()')
        if self.server.origin.startswith('https:'):
            self.send_header('Strict-Transport-Security','max-age=31536000')
        if cookie:
            self.send_header('Set-Cookie',cookie)
        self.end_headers()

    def cookie(self,token,clear=False):
        secure='; Secure' if self.server.origin.startswith('https:') else ''
        return 'khata_session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(0 if clear else SESSION_SECONDS)+secure

    def respond(self,data,code=200,cookie=None):
        self.headers_out(code,'application/json; charset=utf-8',cookie)
        self.wfile.write(json.dumps(data,ensure_ascii=False).encode())

    def session_token(self):
        cookie=SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie',''))
            return cookie['khata_session'].value if 'khata_session' in cookie else ''
        except Exception:
            return ''

    def read_json(self):
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            raise Problem('Use application/json.',415)
        try:
            length=int(self.headers.get('Content-Length','0'))
        except ValueError:
            raise Problem('Invalid request length.')
        if length<=0 or length>16384:
            raise Problem('Request body must be between 1 byte and 16 KB.',413)
        try:
            data=json.loads(self.rfile.read(length))
        except (ValueError,UnicodeDecodeError):
            raise Problem('Invalid JSON.')
        if not isinstance(data,dict):
            raise Problem('Expected a JSON object.')
        return data

    def check_origin(self):
        origin=self.headers.get('Origin')
        if origin and origin!=self.server.origin:
            raise Problem('Request origin is not allowed.',403)
        if self.headers.get('Sec-Fetch-Site')=='cross-site':
            raise Problem('Cross-site requests are not allowed.',403)

    def do_GET(self):
        try:
            path=urlparse(self.path).path
            if path.startswith('/api/'):
                auth=self.server.db.authenticate(self.session_token())
                if path=='/api/session':
                    self.respond(auth)
                elif path=='/api/data':
                    self.respond(self.server.db.snapshot(auth['store_id']))
                elif path=='/api/followups':
                    self.respond(self.server.followups.status(auth['store_id']))
                elif path=='/api/assistant/status':
                    self.respond(self.server.assistant.status())
                elif path=='/api/chat':
                    self.respond(self.server.db.chat(auth['store_id'],self.session_token()))
                elif path=='/api/export':
                    data=self.server.db.snapshot(auth['store_id'])
                    stream=io.StringIO()
                    fields=['id','customer','kind','sale_rupees','paid_rupees','method','due_date','note','created_at','voided_at','void_reason','before_outstanding_rupees','after_outstanding_rupees','correction_rupees']
                    writer=csv.DictWriter(stream,fieldnames=fields)
                    writer.writeheader()
                    names={c['id']:c['name'] for c in data['customers']}
                    def safe(value):
                        value=str(value or '')
                        return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
                    for e in data['entries']:
                        writer.writerow({'id':e['id'],'customer':safe(names[e['customer_id']]),'kind':e['kind'],
                            'sale_rupees':format(Decimal(0 if e['kind']=='adjustment' else e['sale'])/100,'.2f'),'paid_rupees':format(Decimal(0 if e['kind']=='adjustment' else e['paid'])/100,'.2f'),
                            **{label:format(Decimal(e[key])/100,'.2f') if key in e else '' for label,key in [('before_outstanding_rupees','before_amount'),('after_outstanding_rupees','after_amount'),('correction_rupees','delta')]},
                            'method':e['method'],'due_date':e['due_date'],'note':safe(e['note']),
                            'created_at':e['created_at'],'voided_at':e['voided_at'],'void_reason':safe(e['void_reason'])})
                    self.headers_out(200,'text/csv; charset=utf-8')
                    self.wfile.write(('\ufeff'+stream.getvalue()).encode())
                else:
                    raise Problem('Not found.',404)
                return
            routes={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/i18n.js':'i18n.js','/style.css':'style.css',
                    '/manifest.webmanifest':'manifest.webmanifest','/sw.js':'sw.js','/icon.svg':'icon.svg',
                    '/icon-192.png':'icon-192.png','/icon-512.png':'icon-512.png'}
            if path not in routes:
                raise Problem('Not found.',404)
            file=ROOT/'static'/routes[path]
            types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8',
                   '.webmanifest':'application/manifest+json','.svg':'image/svg+xml','.png':'image/png'}
            self.headers_out(200,types[file.suffix])
            self.wfile.write(file.read_bytes())
        except Problem as e:
            self.respond({'error':e.message},e.status)
        except Exception:
            self.respond({'error':'Server error. Please try again.'},500)

    def do_POST(self):
        try:
            self.check_origin()
            path=urlparse(self.path).path
            if path=='/api/transcribe':
                token=self.session_token();auth=self.server.db.authenticate(token)
                if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),auth['csrf']): raise Problem('Session verification failed. Refresh and try again.',403)
                mime=self.headers.get('Content-Type','').split(';')[0]
                if mime not in ('audio/webm','audio/mp4','audio/ogg','audio/wav'): raise Problem('Unsupported audio format.',415)
                try: length=int(self.headers.get('Content-Length','0'))
                except ValueError: raise Problem('Invalid request length.')
                if not 100<=length<=10*1024*1024: raise Problem('Audio must be between 100 bytes and 10 MB.',413)
                audio=self.rfile.read(length)
                self.respond({'text':self.server.assistant.transcribe(auth,audio,mime)})
                return
            data=self.read_json()
            db=self.server.db
            if path in ('/api/register','/api/login'):
                token,_=(db.register if path=='/api/register' else db.login)(data,self.client_address[0])
                self.respond(db.authenticate(token),201 if path=='/api/register' else 200,self.cookie(token))
                return
            token=self.session_token()
            auth=db.authenticate(token)
            if not hmac.compare_digest(self.headers.get('X-CSRF-Token',''),auth['csrf']):
                raise Problem('Session verification failed. Refresh and try again.',403)
            store=auth['store_id']
            if path=='/api/logout':
                db.logout(token)
                self.respond({'ok':True},cookie=self.cookie('',True))
            elif path=='/api/customers':
                self.respond({'id':db.add_customer(store,data)},201)
            elif re.fullmatch(r'/api/customers/[\w-]+',path):
                db.edit_customer(store,path.split('/')[-1],data)
                self.respond({'ok':True})
            elif re.fullmatch(r'/api/customers/[\w-]+/balance',path):
                self.respond(db.adjust_balance(store,path.split('/')[-2],data))
            elif path=='/api/assistant':
                self.respond(self.server.assistant.reply(auth,token,data))
            elif path=='/api/entries':
                self.respond({'id':db.add_entry(store,data)},201)
            elif re.fullmatch(r'/api/entries/[\w-]+/void',path):
                db.void_entry(store,path.split('/')[-2],data)
                self.respond({'ok':True})
            elif path=='/api/chat':
                self.respond(db.chat(store,token,data))
            elif path=='/api/followups/settings':
                self.respond(self.server.followups.settings(store,data))
            elif path=='/api/followups/send-all':
                self.respond(self.server.followups.send_all(store,data))
            elif path=='/api/followups/send':
                self.respond({'id':self.server.followups.enqueue(store,data),'state':'queued'},201)
            elif path=='/api/settings':
                db.settings(store,auth['owner_id'],data)
                self.respond(db.authenticate(token))
            else:
                raise Problem('Not found.',404)
        except Problem as e:
            self.respond({'error':e.message},e.status)
        except sqlite3.OperationalError:
            self.respond({'error':'Database is busy. Your entry was not confirmed; retry with the same request.'},503)
        except Exception:
            self.respond({'error':'Server error. Your entry was not confirmed; refresh before retrying.'},500)


def make_server(host,port,path,origin=None):
    origin=origin or f'http://{host}:{port}'
    parsed=urlparse(origin)
    if parsed.scheme not in ('http','https') or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('PUBLIC_ORIGIN must be an HTTP(S) origin without a path.')
    if parsed.scheme!='https' and parsed.hostname not in ('localhost','127.0.0.1','::1'):
        raise ValueError('Public access requires an HTTPS PUBLIC_ORIGIN.')
    db=Database(path)
    server=ThreadingHTTPServer((host,port),Handler)
    from messaging import Followups
    server.db,server.origin=db,origin.rstrip('/')
    server.followups=Followups(db)
    from assistant import Assistant
    server.assistant=Assistant(db,server.followups)
    return server


if __name__=='__main__':
    env_file=ROOT/'.env'
    if env_file.exists():
        allowed={'LLAMA_URL','LOCAL_AI_TOKEN','OLLAMA_URL','LOCAL_CHAT_MODEL','WHISPER_CLI','WHISPER_MODEL','TWILIO_ACCOUNT_SID','TWILIO_AUTH_TOKEN','TWILIO_FROM','RESEND_API_KEY','EMAIL_FROM','HOST','PORT','PUBLIC_ORIGIN','KHATA_DATABASE'}
        for line in env_file.read_text().splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key,value=line.split('=',1)
                if key.strip() in allowed: os.environ.setdefault(key.strip(),value.strip().strip('"').strip("'"))
    host=os.environ.get('HOST','127.0.0.1')
    port=int(os.environ.get('PORT','8787'))
    path=os.environ.get('KHATA_DATABASE',str(ROOT/'data'/'khata.sqlite3'))
    server=make_server(host,port,path,os.environ.get('PUBLIC_ORIGIN'))
    print(f'Bol Khata running at {server.origin}. Database: {path}',flush=True)
    server.followups.start()
    server.serve_forever()
