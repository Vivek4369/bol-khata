import sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Database,Problem,INDIA
from messaging import Followups,DeliveryError
from datetime import datetime
class Fake:
 def __init__(self):self.sent=[];self.failure=None
 def available(self):return {'sms':True,'email':True}
 def send(self,*args):
  self.sent.append(args)
  if self.failure:raise self.failure
  return 'test-provider-id'
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=Database(Path(self.temp.name)/'test.db')
  self.token,_=self.db.register({'email':'a@example.test','password':'test-password-long','name':'Owner','store':'Shop'})
  self.store=self.db.authenticate(self.token)['store_id'];self.provider=Fake();self.f=Followups(self.db,self.provider)
  self.customer=self.db.add_customer(self.store,{'name':'Ravi','phone':'9876543210','email':'ravi@example.test','opening':'500','due_date':'2000-01-01','sms_consent':True,'email_consent':True})
 def tearDown(self):self.temp.cleanup()
 def message(self):return {'customer_id':self.customer,'channel':'sms','message':'Reviewed reminder','request_id':str(uuid.uuid4())}
 def test_customer_validation_and_idempotency(self):
  with self.assertRaises(Problem):self.db.add_customer(self.store,{'name':'Bad','email':'invalid'})
  body={'name':'New','request_id':str(uuid.uuid4())};a=self.db.add_customer(self.store,body);self.assertEqual(a,self.db.add_customer(self.store,body));self.assertEqual(len(self.db.snapshot(self.store)['customers']),2)
 def test_manual_dedupe_submission(self):
  body=self.message();ident=self.f.enqueue(self.store,body);self.assertEqual(ident,self.f.enqueue(self.store,body));self.f.run_once();self.f.run_once();self.assertEqual(len(self.provider.sent),1);self.assertEqual(self.provider.sent[0][1],'+919876543210');self.assertEqual(self.f.status(self.store)['history'][0]['state'],'accepted')
  with self.assertRaises(Problem):self.f.enqueue(self.store,self.message())
 def test_consent_and_store_isolation(self):
  token,_=self.db.register({'email':'b@example.test','password':'test-password-long','name':'Other','store':'Other'});other=self.db.authenticate(token)['store_id']
  with self.assertRaises(Problem):self.f.enqueue(other,self.message())
  self.assertEqual(self.f.status(other)['history'],[])
  with self.db.connect() as c:c.execute('UPDATE customers SET sms_consent=0 WHERE id=?',(self.customer,))
  with self.assertRaises(Problem):self.f.enqueue(self.store,self.message())
 def test_changed_balance_cancels(self):
  self.f.enqueue(self.store,self.message());self.db.add_entry(self.store,{'customer_id':self.customer,'kind':'payment','paid':'200','method':'upi','request_id':str(uuid.uuid4())});self.f.run_once();self.assertEqual(self.provider.sent,[]);self.assertEqual(self.f.status(self.store)['history'][0]['state'],'cancelled')
 def test_unknown_outcome_never_retries(self):
  self.provider.failure=DeliveryError('Timeout',True);self.f.enqueue(self.store,self.message());self.f.run_once();self.f.run_once();self.assertEqual(len(self.provider.sent),1);self.assertEqual(self.f.status(self.store)['history'][0]['state'],'uncertain')
 def test_auto_schedule_cadence(self):
  self.f.settings(self.store,{'enabled':True,'channel':'email','hour':8,'cadence':3})
  with patch('messaging.datetime') as clock:
   clock.now.return_value=datetime(2026,10,11,10,tzinfo=INDIA);self.f.run_once();self.f.run_once()
  self.assertEqual(len(self.provider.sent),1);self.assertEqual(self.provider.sent[0][0],'email');self.assertEqual(self.f.status(self.store)['history'][0]['mode'],'auto')
 def test_disable_and_restart(self):
  self.f.settings(self.store,{'enabled':True,'channel':'sms','hour':8,'cadence':3});ident=self.f.enqueue(self.store,self.message(),True);self.f.settings(self.store,{'enabled':False,'channel':'sms','hour':8,'cadence':3});self.f.run_once();self.assertEqual(self.provider.sent,[])
  with self.db.connect() as c:c.execute("UPDATE followup_messages SET state='sending' WHERE id=?",(ident,))
  self.assertEqual(Followups(self.db,self.provider).status(self.store)['history'][0]['state'],'uncertain')
 def test_chat_session_isolation(self):
  self.db.chat(self.store,self.token,{'role':'user','content':'My query'});self.db.chat(self.store,self.token,{'role':'assistant','content':'Line one\nLine two'});token,_=self.db.login({'email':'a@example.test','password':'test-password-long'});self.assertEqual(self.db.chat(self.store,token),[]);self.assertEqual(len(Database(self.db.path).chat(self.store,self.token)),2)
