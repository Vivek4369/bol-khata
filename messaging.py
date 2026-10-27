"""Persistent follow-ups. No provider secret is stored in the ledger or returned to clients."""
import base64
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

class DeliveryError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain

class Providers:
    def __init__(self):
        self.sid=os.getenv('TWILIO_ACCOUNT_SID','')
        self.token=os.getenv('TWILIO_AUTH_TOKEN','')
        self.sender=os.getenv('TWILIO_FROM','')
        self.email_key=os.getenv('RESEND_API_KEY','')
        self.email_from=os.getenv('EMAIL_FROM','')
    def available(self):
        return {'sms':bool(re.fullmatch(r'AC[0-9a-fA-F]{32}',self.sid) and self.token and self.sender),
                'email':bool(self.email_key and self.email_from)}
    def send(self,channel,to,message,key,store):
        if not self.available().get(channel):
            raise DeliveryError('Sending provider is not configured.')
        if channel=='sms':
            auth=base64.b64encode((self.sid+':'+self.token).encode()).decode()
            req=Request(f'https://api.twilio.com/2010-04-01/Accounts/{self.sid}/Messages.json',
                urlencode({'To':to,'From':self.sender,'Body':message}).encode(),
                {'Authorization':'Basic '+auth,'Content-Type':'application/x-www-form-urlencoded'})
        else:
            req=Request('https://api.resend.com/emails',json.dumps({'from':self.email_from,'to':[to],
                'subject':store+' · Payment reminder','text':message}).encode(),
                {'Authorization':'Bearer '+self.email_key,'Content-Type':'application/json','Idempotency-Key':key,'User-Agent':'BolKhata/1.0'})
        try:
            with urlopen(req,timeout=12) as response:
                result=json.load(response)
            ident=result.get('sid') if channel=='sms' else result.get('id')
            if not ident: raise DeliveryError('Provider response could not be verified. Check provider records before retrying.',True)
            return str(ident)
        except HTTPError as exc:
            # Do not expose credentials, provider response bodies or customer contact data.
            raise DeliveryError('Provider rejected this request (HTTP '+str(exc.code)+'). Check sender configuration.',exc.code>=500)
        except DeliveryError: raise
        except Exception:
            raise DeliveryError('Delivery outcome is unknown. Check provider records before trying again.',True)

class Followups:
    def __init__(self,db,providers=None):
        self.db=db
        self.providers=providers or Providers()
        self.lock=threading.Lock()
        with db.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS followup_settings(store_id TEXT PRIMARY KEY REFERENCES stores(id),
                enabled INTEGER NOT NULL DEFAULT 0, channel TEXT NOT NULL DEFAULT 'sms',
                hour INTEGER NOT NULL DEFAULT 10, cadence INTEGER NOT NULL DEFAULT 3);
            CREATE TABLE IF NOT EXISTS followup_messages(id TEXT PRIMARY KEY,store_id TEXT NOT NULL REFERENCES stores(id),
                customer_id TEXT NOT NULL,channel TEXT NOT NULL,recipient TEXT NOT NULL,message TEXT NOT NULL,
                mode TEXT NOT NULL,state TEXT NOT NULL,provider_id TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,request_id TEXT NOT NULL,balance_at_queue INTEGER NOT NULL DEFAULT 0,UNIQUE(store_id,request_id),
                FOREIGN KEY(customer_id,store_id) REFERENCES customers(id,store_id));
            ''')
            columns={r['name'] for r in c.execute('PRAGMA table_info(followup_messages)')}
            if 'balance_at_queue' not in columns: c.execute('ALTER TABLE followup_messages ADD COLUMN balance_at_queue INTEGER NOT NULL DEFAULT 0')
            # A process crash during a provider request must never silently resend it.
            c.execute("UPDATE followup_messages SET state='uncertain',error='Server restarted during delivery. Check provider records.' WHERE state='sending'")
    def status(self,store):
        with self.db.connect() as c:
            settings=c.execute('SELECT * FROM followup_settings WHERE store_id=?',(store,)).fetchone()
            history=[dict(r) for r in c.execute('SELECT * FROM followup_messages WHERE store_id=? ORDER BY created_at DESC LIMIT 100',(store,))]
        return {'providers':self.providers.available(),'settings':dict(settings) if settings else {'enabled':0,'channel':'sms','hour':10,'cadence':3},'history':history}
    def settings(self,store,data):
        from server import Problem
        enabled=data.get('enabled') is True
        channel=data.get('channel')
        hour,cadence=data.get('hour'),data.get('cadence')
        if channel not in ('sms','email') or type(hour)!=int or not 8<=hour<=20 or type(cadence)!=int or not 1<=cadence<=30:
            raise Problem('Choose a channel, hour between 8 and 20, and interval between 1 and 30 days.')
        if enabled and not self.providers.available().get(channel):
            raise Problem('Configure the sending provider before enabling automatic follow-ups.',409)
        with self.db.connect() as c:
            c.execute('INSERT INTO followup_settings VALUES(?,?,?,?,?) ON CONFLICT(store_id) DO UPDATE SET enabled=excluded.enabled,channel=excluded.channel,hour=excluded.hour,cadence=excluded.cadence',(store,int(enabled),channel,hour,cadence))
        return self.status(store)
    def enqueue(self,store,data,auto=False):
        from server import Problem,text
        channel=data.get('channel'); ident=text(data.get('customer_id'),'Customer',36)
        key=text(data.get('request_id'),'Request identifier',100)
        message=text(data.get('message'),'Message',1600)
        if channel not in ('sms','email'): raise Problem('Choose SMS or email.')
        if not self.providers.available().get(channel): raise Problem('Sending provider is not configured. Open delivery setup.',409)
        with self.db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing=c.execute('SELECT * FROM followup_messages WHERE store_id=? AND request_id=?',(store,key)).fetchone()
            if existing:
                if (existing['customer_id'],existing['channel'],existing['message']) != (ident,channel,message): raise Problem('This follow-up request was already used for a different message.',409)
                return existing['id']
            customer=self.db.customer(c,store,ident)
            recipient=self.recipient(customer,channel)
            balance=self.db.balance(c,store,ident)
            if balance<=0: raise Problem('This customer has no outstanding balance.',409)
            last=c.execute("SELECT created_at FROM followup_messages WHERE store_id=? AND customer_id=? AND channel=? AND state IN ('queued','sending','accepted','uncertain') ORDER BY created_at DESC LIMIT 1",(store,ident,channel)).fetchone()
            if last and time.time()-last[0]<86400: raise Problem('A follow-up was already queued or submitted in the last 24 hours.',409)
            row_id=str(uuid.uuid4())
            c.execute('INSERT INTO followup_messages(id,store_id,customer_id,channel,recipient,message,mode,state,created_at,request_id,balance_at_queue) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(row_id,store,ident,channel,recipient,message,'auto' if auto else 'manual','queued',time.time(),key,balance))
        return row_id
    def draft(self,store,customer):
        with self.db.connect() as c: shop=c.execute('SELECT name,language FROM stores WHERE id=?',(store,)).fetchone()
        rupees=f"₹{customer['balance']/100:,.2f}"
        if shop['language']=='hi': return f"नमस्ते {customer['name']} जी, आपके खाते में {rupees} बकाया है। कृपया भुगतान की तारीख बताएं। भुगतान कर चुके हैं तो विवरण साझा करें। धन्यवाद — {shop['name']}"
        return f"Hello {customer['name']}, your outstanding balance is {rupees}. Please let us know a payment date. If already paid, please share payment details. Thank you — {shop['name']}"

    def send_all(self,store,data):
        from server import Problem,text
        channel=data.get('channel',self.status(store)['settings']['channel'])
        if channel not in ('sms','email'): raise Problem('Choose SMS or email.')
        if not self.providers.available().get(channel): raise Problem('Sending provider is not configured. Open delivery setup.',409)
        key=text(data.get('request_id'),'Request identifier',64)
        queued=[];skipped=[]
        for customer in self.db.snapshot(store)['customers']:
            if not (customer['overdue'] or customer['due_today']): continue
            try:
                ident=self.enqueue(store,{'customer_id':customer['id'],'channel':channel,'message':self.draft(store,customer),'request_id':str(uuid.uuid5(uuid.NAMESPACE_URL,key+':'+customer['id']))})
                queued.append({'id':ident,'name':customer['name']})
            except Problem as exc: skipped.append({'name':customer['name'],'reason':exc.message})
        return {'queued':queued,'skipped':skipped,'channel':channel}

    @staticmethod
    def recipient(customer,channel):
        from server import Problem
        if not customer[channel+'_consent']: raise Problem('Record customer permission for this channel first.')
        if channel=='email':
            value=customer['email']
            if not value: raise Problem('Add this customer’s email first.')
            return value
        value=re.sub(r'[ ()-]','',customer['phone'])
        if re.fullmatch(r'[6-9]\d{9}',value): value='+91'+value
        if not re.fullmatch(r'\+[1-9]\d{7,14}',value): raise Problem('Add a valid phone with country code (or a 10-digit Indian mobile).')
        return value
    def run_once(self):
        from server import INDIA,today,Problem
        # One scheduler in this server. DB claims also prevent concurrent sends.
        if not self.lock.acquire(False): return
        try:
            now=datetime.now(INDIA)
            with self.db.connect() as c:
                settings=[dict(r) for r in c.execute('SELECT f.*,s.name,s.language FROM followup_settings f JOIN stores s ON s.id=f.store_id WHERE enabled=1')]
            for cfg in settings:
                if now.hour<cfg['hour'] or now.hour>20: continue
                for cust in self.db.snapshot(cfg['store_id'])['customers']:
                    if not (cust['overdue'] or cust['due_today']): continue
                    with self.db.connect() as c:
                        last=c.execute("SELECT max(created_at) FROM followup_messages WHERE store_id=? AND customer_id=? AND channel=? AND state IN ('queued','sending','accepted','uncertain','failed')",(cfg['store_id'],cust['id'],cfg['channel'])).fetchone()[0]
                    if last and time.time()-last<cfg['cadence']*86400: continue
                    rupees=f"₹{cust['balance']/100:,.2f}"
                    msg=(f"नमस्ते {cust['name']} जी, आपके खाते में {rupees} बकाया है। कृपया भुगतान की तारीख बताएं। भुगतान कर चुके हैं तो विवरण साझा करें। धन्यवाद — {cfg['name']}" if cfg['language']=='hi' else f"Hello {cust['name']}, your outstanding balance is {rupees}. Please let us know a payment date. If already paid, please share payment details. Thank you — {cfg['name']}")
                    try: self.enqueue(cfg['store_id'],{'customer_id':cust['id'],'channel':cfg['channel'],'message':msg,'request_id':'auto-'+cust['id']+'-'+cfg['channel']+'-'+today()},True)
                    except Problem: pass
            with self.db.connect() as c:
                rows=[dict(r) for r in c.execute("SELECT * FROM followup_messages WHERE state='queued' ORDER BY created_at LIMIT 20")]
            for row in rows:
                with self.db.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    if not c.execute("SELECT id FROM followup_messages WHERE id=? AND state='queued'",(row['id'],)).fetchone(): continue
                    customer=self.db.customer(c,row['store_id'],row['customer_id'])
                    balance=self.db.balance(c,row['store_id'],row['customer_id'])
                    cfg=c.execute('SELECT enabled,channel,hour FROM followup_settings WHERE store_id=?',(row['store_id'],)).fetchone()
                    if row['mode']=='auto' and cfg and cfg['enabled'] and not cfg['hour']<=now.hour<=20: continue
                    try:
                        recipient=self.recipient(customer,row['channel'])
                        valid=balance>0 and balance==row['balance_at_queue'] and recipient==row['recipient'] and (row['mode']!='auto' or cfg and cfg['enabled'] and cfg['channel']==row['channel'])
                    except Problem: valid=False
                    if not valid:
                        c.execute("UPDATE followup_messages SET state='cancelled',error='Balance, consent, contact or auto settings changed.' WHERE id=?",(row['id'],)); continue
                    store=c.execute('SELECT name FROM stores WHERE id=?',(row['store_id'],)).fetchone()[0]
                    c.execute("UPDATE followup_messages SET state='sending' WHERE id=?",(row['id'],))
                try:
                    provider_id=self.providers.send(row['channel'],row['recipient'],row['message'],row['id'],store)
                    state,error='accepted',''
                except DeliveryError as exc:
                    state,error=('uncertain' if exc.uncertain else 'failed'),str(exc); provider_id=''
                except Exception:
                    state,error,provider_id='uncertain','Unexpected delivery failure; check provider records.',''
                with self.db.connect() as c:
                    c.execute('UPDATE followup_messages SET state=?,error=?,provider_id=? WHERE id=?',(state,error,provider_id,row['id']))
        finally: self.lock.release()
    def start(self):
        def loop():
            while True:
                try: self.run_once()
                except Exception: pass
                time.sleep(30)
        threading.Thread(target=loop,daemon=True).start()
