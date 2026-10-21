import json, os, tempfile, time, unittest, shutil
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from paper_agents.db import init_db,connect_db
from paper_agents.sync_exchange import *
from paper_agents.sync_worker import work
from paper_agents.web import load_review_page

class ExchangeTests(unittest.TestCase):
 def setUp(self):
  self.env=patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'s'*32,'PAPER_AGENT_SYNC_ENVIRONMENT':'nonprod','PAPER_AGENT_SYNC_PRODUCER':'mini-nonprod'});self.env.start();self.addCleanup(self.env.stop)
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.aws=self.root/'aws';self.mini=self.root/'mini'
  self.schema=Path('sql/schema.sql').resolve()
  self.profile={'interests':['agent reliability'],'positive_signals':[],'negative_signals':[],'feedback_history':[]}
  for root in (self.aws,self.mini):
   (root/'sql').mkdir(parents=True);shutil.copy(self.schema,root/'sql/schema.sql')
   (root/'config').mkdir(parents=True);(root/'data').mkdir();(root/'config/topics.yaml').write_text('topics: []\n');(root/'config/industry.json').write_text('{}');(root/'data/profile.json').write_text(json.dumps(self.profile));init_db(root/'data/paper_agent.db')
  self.db=self.aws/'data/paper_agent.db';self.mdb=self.mini/'data/paper_agent.db'
 def header(self,key=''):
  h=Message();h['X-Paper-Sync-Idempotency']=key;return h
 def snapshot(self):return build_input_snapshot(self.db,self.aws)
 def batch(self,seq=1,key='doi:10.1/example',summary='concrete'):
  return {'schema':1,'input_revision':self.snapshot()['revision'],'producer_id':'mini-nonprod','environment':'nonprod','run_sequence':seq,'results':[{'canonical_key':key,'title':'Paper','abstract':'Valid source abstract','source':'openalex','source_id':'W1','score':80,'rationale':'Relevant','summary':{'merged':{'research_problem':summary,'why_it_matters':'Evidence','approach':'Method'}},'model':'qwen','provenance':{'abstract_only':True}}]}
 def receive(self,p,key='batch'):
  return receive_result_batch(self.db,encoded(p),self.header(key),self.aws)
 def feedback(self):
  with connect_db(self.db) as c:
   pid=c.execute("INSERT INTO papers(canonical_key,title) VALUES('doi:feedback','Original')").lastrowid
   raw=c.execute('INSERT INTO raw_feedback(paper_id,content,content_hash) VALUES(?,?,?)',(pid,'useful','hash')).lastrowid
   parse=c.execute("INSERT INTO feedback_parse_attempts(raw_feedback_id,parser_name,parser_version,status) VALUES(?,'test','1','succeeded')",(raw,)).lastrowid
   sf=c.execute("INSERT INTO structured_feedback(parse_attempt_id,paper_id,decision,observations_json) VALUES(?,?,'keep','[\"good\"]')",(parse,pid)).lastrowid
   c.execute('INSERT INTO saved_papers(paper_id) VALUES(?)',(pid,));c.commit();return pid,sf
 def test_snapshot_stable_and_preserves_feedback_fk_closure(self):
  pid,sf=self.feedback()
  with connect_db(self.mdb) as c:c.execute("INSERT INTO papers(canonical_key,title) VALUES('unrelated','Other')");c.commit()
  a=self.snapshot();self.assertEqual(a['revision'],self.snapshot()['revision']);apply_input_snapshot(self.mdb,a,self.mini);apply_input_snapshot(self.mdb,a,self.mini)
  with connect_db(self.mdb) as c:
   row=c.execute('SELECT p.canonical_key,s.decision,r.content FROM structured_feedback s JOIN papers p ON p.id=s.paper_id JOIN feedback_parse_attempts f ON f.id=s.parse_attempt_id JOIN raw_feedback r ON r.id=f.raw_feedback_id').fetchone();self.assertEqual(tuple(row),('doi:feedback','keep','useful'));self.assertIsNone(c.execute('PRAGMA foreign_key_check').fetchone());self.assertEqual(c.execute('SELECT COUNT(*) FROM structured_feedback').fetchone()[0],1)
  bad=dict(a);bad['revision']='broken'
  with self.assertRaises(ValueError):apply_input_snapshot(self.mdb,bad,self.mini)
 def test_auth_binding_expiry_nonfinite(self):
  h=self.header('one');h['X-Paper-Sync-Timestamp']=str(time.time());h['X-Paper-Sync-Signature']=signature('POST','/sync/results',h['X-Paper-Sync-Timestamp'],b'{}','one')
  self.assertTrue(verify_request(h,b'{}','POST','/sync/results'));self.assertFalse(verify_request(h,b'[]','POST','/sync/results'));h.replace_header('X-Paper-Sync-Idempotency','two');self.assertFalse(verify_request(h,b'{}','POST','/sync/results'));h.replace_header('X-Paper-Sync-Timestamp','nan');self.assertFalse(verify_request(h,b'{}','POST','/sync/results'))
 def test_import_real_review_ui_and_preserve_summary_feedback(self):
  p=self.batch();self.receive(p)
  with connect_db(self.db) as c:
   pid=c.execute('SELECT id FROM papers').fetchone()[0];aid=c.execute('SELECT id FROM artifacts').fetchone()[0]
   c.execute("INSERT INTO summary_field_quality_signals(paper_id,artifact_id,field_name,signal,field_text) VALUES(?,?,'research_problem','too_generic','concrete')",(pid,aid));c.execute('INSERT INTO saved_papers(paper_id) VALUES(?)',(pid,));c.commit()
  p=self.batch(2,summary='improved');self.receive(p,'batch2')
  oldcwd=Path.cwd()
  try:
   os.chdir(self.aws);page=load_review_page(self.db,filter_value="all",source_value="all",sort_value="newest")
   self.assertTrue(page['cards']);card=page['cards'][0];self.assertEqual(card['summary']['research_problem'],'improved');self.assertTrue(card['is_saved']);self.assertFalse(card['summary_feedback_fields'])
  finally:os.chdir(oldcwd)
  with connect_db(self.db) as c:self.assertEqual(c.execute('SELECT artifact_id FROM summary_field_quality_signals').fetchone()[0],aid);self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_summary_versions').fetchone()[0],2)
 def test_atomic_rollback_alias_protection_and_monotonic_replay(self):
  p=self.batch();self.receive(p);self.assertTrue(self.receive(p)['replayed'])
  p['results'][0]['title']='changed'
  with self.assertRaises(ValueError):self.receive(p)
  with self.assertRaises(ValueError):self.receive(self.batch(1),'stale')
  alias=self.batch(2,key='doi:different')
  with self.assertRaises(ValueError):self.receive(alias,'alias')
  with connect_db(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM papers').fetchone()[0],1);self.assertEqual(c.execute('SELECT COUNT(*) FROM sync_result_batches').fetchone()[0],1)
  wrong=self.batch(2);wrong['environment']='production'
  with self.assertRaises(ValueError):self.receive(wrong,'wrong')
 def test_profile_proposal_cas_and_consumed_feedback(self):
  pid,fid=self.feedback();snap=self.snapshot()
  p={'schema':1,'proposal_id':'prop','producer_id':'mini-nonprod','environment':'nonprod','base_profile_version':0,'input_revision':snap['revision'],'consumed_feedback_keys':['structured:'+str(fid)],'profile':self.profile,'change_summary':'update'}
  result=receive_profile_proposal(self.db,encoded(p),self.header('prop'));self.assertEqual(result['profile_version'],1);self.assertEqual(receive_profile_proposal(self.db,encoded(p),self.header('prop')),result)
  p['proposal_id']='other'
  with self.assertRaises(ValueError):receive_profile_proposal(self.db,encoded(p),self.header('other'))
  with connect_db(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM feedback_profile_applications').fetchone()[0],1)
 def test_worker_lost_publish_retry_does_not_repeat_inference(self):
  calls=[];fail=[True]
  def transport(url,method,body,secret,path,key=''):
   if path=='/sync/input':return self.snapshot()
   result=receive_result_batch(self.db,body,self.header(key),self.aws)
   if fail[0]:fail[0]=False;raise OSError('reply lost after application')
   return result
  def runner(source,db,root,snapshot):calls.append(source);return self.batch()['results']
  args=dict(db_path=self.mdb,state_dir=self.mini/'state',root=self.mini,aws_url='https://aws.example',transport=transport,runner=runner)
  work(**args,run=True)
  with self.assertRaises(OSError):work(**args,publish=True)
  with self.assertRaises(ValueError):work(**args,run=True)
  work(**args,publish=True);self.assertEqual(calls,['arxiv'])
  with connect_db(self.db) as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM recommendations').fetchone()[0],1)
 def test_profile_generation_worker_uses_cas_without_repeating_provider(self):
  self.feedback();calls=[];fail=[True]
  def provider(payload,model):calls.append(payload);return {'profile':self.profile,'change_summary':'specific'}
  def transport(url,method,body,secret,path,key=''):
   if path=='/sync/input':return self.snapshot()
   result=receive_profile_proposal(self.db,body,self.header(key))
   if fail[0]:fail[0]=False;raise OSError('receipt lost')
   return result
  args=dict(db_path=self.mdb,state_dir=self.mini/'state',root=self.mini,aws_url='https://aws',transport=transport,propose=True,profile_provider=provider)
  with self.assertRaises(OSError):work(**args)
  work(**args);self.assertEqual(len(calls),1)
  with connect_db(self.db) as c:self.assertEqual(c.execute('SELECT version FROM profile_versions WHERE active=1').fetchone()[0],1)
 def test_interrupted_inference_is_not_repeated(self):
  calls=[]
  def transport(*args):return self.snapshot()
  def runner(*args):calls.append(1);raise RuntimeError('model interrupted')
  args=dict(db_path=self.mdb,state_dir=self.mini/'state',root=self.mini,aws_url='https://aws',transport=transport,runner=runner,run=True)
  with self.assertRaises(RuntimeError):work(**args)
  with self.assertRaises(ValueError):work(**args)
  self.assertEqual(calls,[1])
 def test_input_failure_never_calls_runner(self):
  calls=[]
  def transport(*args):raise OSError('offline')
  with self.assertRaises(OSError):work(db_path=self.mdb,state_dir=self.mini/'state',root=self.mini,aws_url='https://aws',transport=transport,runner=lambda *a:calls.append(a),run=True)
  self.assertFalse(calls)
 def test_worker_run_and_publish_completes_in_one_invocation(self):
  calls=[]
  def transport(url,method,body,secret,path,key=''):
   if path=='/sync/input':return self.snapshot()
   if path=='/sync/results':return receive_result_batch(self.db,body,self.header(key),self.aws)
   raise AssertionError(path)
  def runner(*args):calls.append(1);return self.batch()['results']
  state=self.mini/'exchange'
  result=work(db_path=self.mdb,state_dir=state,root=self.mini,aws_url='https://candidate.test',run=True,publish=True,transport=transport,runner=runner)
  self.assertEqual(result['status'],'published');self.assertEqual(calls,[1]);self.assertFalse(list(state.glob('outbox-*.json')));self.assertEqual(len(list(state.glob('receipt-*.json'))),1)
if __name__=='__main__':unittest.main()
