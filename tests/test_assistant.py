import sys,tempfile,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Database,Problem
from assistant import Assistant,basic_plan
from messaging import Followups
class Provider:
 def available(self):return {'sms':True,'email':True}
 def send(self,*args):raise AssertionError('Tests must not send messages')
class Model:
 model='test'
 def ready(self):return False
 def audio_ready(self):return False
class Tests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.db=Database(Path(self.temp.name)/'test.db');self.token,_=self.db.register({'email':'test@example.test','password':'long-password-test','name':'Owner','store':'Shop'});self.auth=self.db.authenticate(self.token);self.auth['language']='en';self.store=self.auth['store_id'];self.f=Followups(self.db,Provider());self.a=Assistant(self.db,self.f,Model());self.c=self.db.add_customer(self.store,{'name':'Ravi','alias':'रवि','opening':'500','due_date':'2000-01-01','phone':'9876543210','sms_consent':True})
 def tearDown(self):self.temp.cleanup()
 def ask(self,message,key=None):return self.a.reply(self.auth,self.token,{'message':message,'request_id':key or str(uuid.uuid4())})
 def correct(self,amount,**kw):return self.db.adjust_balance(self.store,self.c,{'amount':amount,'reason':'Corrected old entry','request_id':str(uuid.uuid4()),**kw})
 def test_correction_is_history_not_payment(self):
  r=self.correct('300',expected='500');s=self.db.snapshot(self.store);self.assertEqual(s['customers'][0]['balance'],30000);self.assertEqual(s['report']['receipts'],0);self.assertEqual(s['report']['adjustments'],-20000);e=next(e for e in s['entries'] if e['kind']=='adjustment');self.assertEqual(e['before_amount'],50000);self.assertEqual(e['note'],'Corrected old entry');self.assertEqual(sum(d['receipts'] for d in s['trend']),0)
 def test_increase_then_payment(self):
  self.correct('600');self.ask('Ravi paid 600 cash');self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],0)
 def test_correction_replay_and_stale(self):
  body={'amount':'300','reason':'Correction','expected':'500','request_id':str(uuid.uuid4())};r=self.db.adjust_balance(self.store,self.c,body);self.assertEqual(r['id'],self.db.adjust_balance(self.store,self.c,body)['id']);
  with self.assertRaises(Problem):self.correct('250',expected='500')
  with self.assertRaises(Problem):self.db.adjust_balance(self.store,self.c,{**body,'reason':''})
 def test_direct_payment_no_confirmation_and_replay(self):
  key=str(uuid.uuid4());r=self.ask('Ravi paid 200 upi',key);self.assertIn('Recorded',r['reply']);self.assertEqual(r['results'][0]['type'],'add_payment');self.assertEqual(r,self.ask('Ravi paid 200 upi',key));self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],30000)
 def test_method_clarification(self):
  r=self.ask('Ravi paid 200');self.assertEqual(r['results'],[]);r=self.ask('cash');self.assertEqual(r['results'][0]['type'],'add_payment');self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],30000)
 def test_overpayment_does_not_claim_success(self):
  r=self.ask('Ravi paid 600 cash');self.assertEqual(r['results'],[]);self.assertNotIn('Recorded',r['reply']);self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],50000)
 def test_customer_direct_and_history(self):
  r=self.ask('Add customer Amit');self.assertEqual(r['results'][0]['type'],'add_customer');self.assertEqual(len(self.db.chat(self.store,self.token)),2)
 def test_batch_eligible_only_and_no_consent_skip(self):
  self.db.add_customer(self.store,{'name':'No permission','opening':'100','due_date':'2000-01-01'});self.db.add_customer(self.store,{'name':'Future','opening':'100','due_date':'2099-01-01'});r=self.ask('Send followup to all');self.assertEqual(r['results'][0]['queued'],1);self.assertEqual(r['results'][0]['skipped'],1)
 def test_auto_chat_preserves_settings(self):
  self.ask('Set automatic followups email after 11 repeat every 4 days');cfg=self.f.status(self.store)['settings'];self.assertEqual((cfg['channel'],cfg['hour'],cfg['cadence']),('email',11,4));self.ask('Turn on automatic followups');self.assertTrue(self.f.status(self.store)['settings']['enabled']);self.ask('Turn off automatic followups');self.assertFalse(self.f.status(self.store)['settings']['enabled'])
 def test_cross_store_correction_and_model_action(self):
  token,_=self.db.register({'email':'other@example.test','password':'long-password-test','name':'Other','store':'Other'});other=self.db.authenticate(token)
  with self.assertRaises(Problem):self.db.adjust_balance(other['store_id'],self.c,{'amount':'1','reason':'Other','request_id':str(uuid.uuid4())})
  with self.assertRaises(Problem):self.a.execute(other,{'type':'add_payment','customer_id':self.c,'paid':'200','method':'cash'},str(uuid.uuid4()))
 def test_hindi_money(self):
  r=self.ask('रवि ने दो सौ रुपये नकद दिए');self.assertEqual(r['results'][0]['type'],'add_payment');self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],30000)

 def test_method_reply_after_completed_action_does_not_repeat(self):
  self.ask('Ravi paid 200 cash');self.ask('cash');self.assertEqual(self.db.snapshot(self.store)['customers'][0]['balance'],30000)
 def test_due_date_digits_are_not_received_money(self):
  r=self.ask('Ravi sale 10000 due 2099-01-01');self.assertEqual(r['results'][0]['type'],'add_sale');self.assertEqual(self.db.snapshot(self.store)['report']['receipts'],0)
