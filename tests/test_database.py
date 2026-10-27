import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import Database, Problem, amount, today


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/'test.sqlite3')
        self.token_a,_=self.db.register({'email':'owner-a@example.test','password':'strong-password-a','name':'Owner A','store':'Store A'})
        self.token_b,_=self.db.register({'email':'owner-b@example.test','password':'strong-password-b','name':'Owner B','store':'Store B'})
        self.a=self.db.authenticate(self.token_a)
        self.b=self.db.authenticate(self.token_b)

    def tearDown(self):
        self.temp.cleanup()

    def add(self,store=None,opening='1000',due='2000-01-01'):
        return self.db.add_customer(store or self.a['store_id'],{'name':'Actual customer','opening':opening,'due_date':due})

    def entry(self,customer,kind='payment',paid='200',sale='0',**kw):
        return {'customer_id':customer,'kind':kind,'sale':sale,'paid':paid,'method':'cash','request_id':str(uuid.uuid4()),**kw}

    def test_accounts_start_empty(self):
        for auth in (self.a,self.b):
            s=self.db.snapshot(auth['store_id'])
            self.assertEqual(s['customers'],[])
            self.assertEqual(s['entries'],[])
            self.assertEqual(s['report']['closing'],0)

    def test_store_isolation_reads(self):
        self.add()
        self.assertEqual(len(self.db.snapshot(self.a['store_id'])['customers']),1)
        self.assertEqual(self.db.snapshot(self.b['store_id'])['customers'],[])

    def test_cross_store_writes_rejected(self):
        customer=self.add()
        with self.assertRaises(Problem) as p:
            self.db.add_entry(self.b['store_id'],self.entry(customer))
        self.assertEqual(p.exception.status,404)
        with self.assertRaises(Problem):
            self.db.edit_customer(self.b['store_id'],customer,{'name':'Intruder'})
        original=self.db.snapshot(self.a['store_id'])['entries'][0]
        with self.assertRaises(Problem):
            self.db.void_entry(self.b['store_id'],original['id'],{'reason':'Intruder'})

    def test_payment_fifo_and_report(self):
        c=self.add()
        self.db.add_entry(self.a['store_id'],self.entry(c))
        self.db.add_entry(self.a['store_id'],self.entry(c,'sale','100','500',due_date='2099-01-01'))
        s=self.db.snapshot(self.a['store_id']);p=s['customers'][0];r=s['report']
        self.assertEqual(p['balance'],120000)
        self.assertEqual(p['overdue'],80000)
        self.assertEqual(r['receipts'],30000)
        self.assertEqual(r['sales'],50000)
        self.assertEqual(r['opening_added'],100000)
        self.assertEqual(r['new_credit'],40000)
        self.assertEqual(r['opening'],0)
        self.assertEqual(r['closing'],120000)
        self.assertEqual(r['methods']['cash'],30000)

    def test_overpayment_and_concurrent_payments(self):
        c=self.add(opening='100')
        with self.assertRaises(Problem):
            self.db.add_entry(self.a['store_id'],self.entry(c,paid='101'))
        def pay(_):
            try:
                self.db.add_entry(self.a['store_id'],self.entry(c,paid='75'))
                return True
            except Problem:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(pay,range(2))),1)
        self.assertEqual(self.db.snapshot(self.a['store_id'])['customers'][0]['balance'],2500)

    def test_duplicate_save_and_changed_replay(self):
        c=self.add();body=self.entry(c)
        first=self.db.add_entry(self.a['store_id'],body)
        self.assertEqual(first,self.db.add_entry(self.a['store_id'],body))
        self.assertEqual(self.db.snapshot(self.a['store_id'])['customers'][0]['balance'],80000)
        with self.assertRaises(Problem):
            self.db.add_entry(self.a['store_id'],{**body,'paid':'201'})

    def test_undo_keeps_history_and_prevents_orphan_payments(self):
        c=self.add();opening=self.db.snapshot(self.a['store_id'])['entries'][0]['id']
        payment=self.db.add_entry(self.a['store_id'],self.entry(c))
        with self.assertRaises(Problem):
            self.db.void_entry(self.a['store_id'],opening,{'reason':'Wrong opening'})
        self.db.void_entry(self.a['store_id'],payment,{'reason':'Wrong payment'})
        self.db.void_entry(self.a['store_id'],opening,{'reason':'Wrong opening'})
        s=self.db.snapshot(self.a['store_id'])
        self.assertEqual(s['customers'][0]['balance'],0)
        self.assertEqual(len(s['entries']),2)
        self.assertTrue(all(e['voided_at'] for e in s['entries']))
        with self.db.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM audit WHERE action='entry_voided'").fetchone()[0],2)

    def test_due_today_and_undated(self):
        c=self.add(opening='100',due=today())
        self.db.add_entry(self.a['store_id'],self.entry(c,'sale','0','50',due_date=None,method='none'))
        p=self.db.snapshot(self.a['store_id'])['customers'][0]
        self.assertEqual(p['overdue'],0)
        self.assertEqual(p['due_today'],10000)
        self.assertEqual(p['undated'],5000)

    def test_database_persists_after_restart(self):
        c=self.add();other=Database(self.db.path)
        self.assertEqual(other.authenticate(self.token_a)['store_id'],self.a['store_id'])
        self.assertEqual(other.snapshot(self.a['store_id'])['customers'][0]['id'],c)

    def test_password_and_logout(self):
        with self.assertRaises(Problem):
            self.db.login({'email':'owner-a@example.test','password':'wrong'})
        token,_=self.db.login({'email':'owner-a@example.test','password':'strong-password-a'})
        self.assertEqual(self.db.authenticate(token)['owner_id'],self.a['owner_id'])
        self.db.logout(token)
        with self.assertRaises(Problem):
            self.db.authenticate(token)
        with self.db.connect() as db:
            stored=db.execute('SELECT password_hash FROM owners WHERE id=?',(self.a['owner_id'],)).fetchone()[0]
        self.assertNotIn('strong-password',stored)

    def test_amount_validation(self):
        self.assertEqual(amount('0.01'),1)
        self.assertEqual(amount('12.34'),1234)
        for invalid in ['-1','1.001','NaN','1e4',True,1.1,'10000001','1,000']:
            with self.assertRaises(Problem,msg=str(invalid)):
                amount(invalid)

    def test_settings_are_store_specific(self):
        self.db.settings(self.a['store_id'],self.a['owner_id'],{'name':'New owner','store':'New name','language':'en'})
        self.assertEqual(self.db.authenticate(self.token_a)['store_name'],'New name')
        self.assertEqual(self.db.authenticate(self.token_b)['store_name'],'Store B')


if __name__=='__main__':
    unittest.main()
