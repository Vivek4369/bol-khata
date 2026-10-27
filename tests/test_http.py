import http.client
import json
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import make_server


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=make_server('127.0.0.1',0,Path(self.temp.name)/'http.sqlite3')
        self.port=self.server.server_address[1]
        self.server.origin=f'http://127.0.0.1:{self.port}'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,body=None,cookie=None,csrf=None,origin=None):
        client=http.client.HTTPConnection('127.0.0.1',self.port,timeout=10)
        headers={}
        if cookie:headers['Cookie']=cookie
        if csrf:headers['X-CSRF-Token']=csrf
        if origin:headers['Origin']=origin
        encoded=None
        if body is not None:
            encoded=json.dumps(body);headers['Content-Type']='application/json'
        client.request('POST' if body is not None else 'GET',path,encoded,headers)
        response=client.getresponse();status=response.status;out_headers=dict(response.getheaders());raw=response.read().decode('utf-8-sig');client.close()
        content=json.loads(raw) if 'application/json' in out_headers.get('Content-Type','') else raw
        return status,content,out_headers

    def account(self,name):
        status,data,headers=self.request('/api/register',{'email':name+'@example.test','name':name,'store':name+' Store','password':'temporary-password-2026'})
        self.assertEqual(status,201)
        self.assertIn('HttpOnly',headers['Set-Cookie']);self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        return data,headers['Set-Cookie'].split(';')[0]

    def test_anonymous_and_csrf_and_origin(self):
        self.assertEqual(self.request('/api/data')[0],401)
        owner,cookie=self.account('owner')
        body={'name':'Customer','opening':'0'}
        self.assertEqual(self.request('/api/customers',body,cookie)[0],403)
        self.assertEqual(self.request('/api/customers',body,cookie,owner['csrf'],'https://attacker.example')[0],403)
        self.assertEqual(self.request('/api/customers',body,cookie,owner['csrf'],self.server.origin)[0],201)
        self.assertEqual(self.request('/data/khata.sqlite3')[0],404)

    def test_http_owner_isolation_and_csv(self):
        a,cookie_a=self.account('a');b,cookie_b=self.account('b')
        status,customer,_=self.request('/api/customers',{'name':'=Customer','opening':'100'},cookie_a,a['csrf'])
        self.assertEqual(status,201)
        status,_,_=self.request('/api/entries',{'customer_id':customer['id'],'kind':'payment','paid':'10','method':'upi','request_id':str(uuid.uuid4())},cookie_b,b['csrf'])
        self.assertEqual(status,404)
        self.assertEqual(self.request('/api/data',cookie=cookie_b)[1]['customers'],[])
        status,export,_=self.request('/api/export',cookie=cookie_a)
        self.assertEqual(status,200);self.assertIn("'=Customer",export)
        self.assertNotIn('=Customer',self.request('/api/export',cookie=cookie_b)[1])

    def test_followup_and_chat_routes(self):
        class Provider:
            def available(self):return {'sms':True,'email':True}
            def send(self,*args):return 'fake-http-provider-id'
        self.server.followups.providers=Provider()
        a,ca=self.account('a');b,cb=self.account('b')
        status,customer,_=self.request('/api/customers',{'name':'Ravi','phone':'9876543210','email':'ravi@example.test','sms_consent':True,'opening':'500'},ca,a['csrf'])
        self.assertEqual(status,201)
        msg={'customer_id':customer['id'],'channel':'sms','message':'Reviewed reminder','request_id':str(uuid.uuid4())}
        self.assertEqual(self.request('/api/followups/send',msg,ca)[0],403)
        self.assertEqual(self.request('/api/followups/send',msg,cb,b['csrf'])[0],404)
        self.assertEqual(self.request('/api/followups/send',msg,ca,a['csrf'])[0],201)
        self.server.followups.run_once()
        self.assertEqual(self.request('/api/followups',cookie=ca)[1]['history'][0]['state'],'accepted')
        self.assertEqual(self.request('/api/followups',cookie=cb)[1]['history'],[])
        self.assertEqual(self.request('/api/chat',{'role':'user','content':'My query'},ca,a['csrf'])[0],200)
        self.assertEqual(len(self.request('/api/chat',cookie=ca)[1]),1)
        self.assertEqual(self.request('/api/chat',cookie=cb)[1],[])
        self.assertEqual(self.request('/api/followups/settings',{'enabled':True,'channel':'sms','hour':10,'cadence':3},ca,a['csrf'])[0],200)

    def test_logout_and_assets(self):
        owner,cookie=self.account('owner')
        self.assertEqual(self.request('/api/session',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/logout',{},cookie,owner['csrf'])[0],200)
        self.assertEqual(self.request('/api/data',cookie=cookie)[0],401)
        for path in ['/','/i18n.js','/style.css','/app.js','/manifest.webmanifest','/sw.js']:
            self.assertEqual(self.request(path)[0],200,path)


if __name__=='__main__':unittest.main()
