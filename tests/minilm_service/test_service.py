from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from paper_agents.curator_interest import score_interest_fit, default_interest_scorer
from paper_agents.minilm_http import HTTPMiniLMScorer, IDENTITY, MAX_BYTES
from paper_agents.minilm_service import InferenceServer, create_server

ABSTRACT='This research examines incident diagnosis and agent reliability with controlled experiments. '*3

class FakeScorer:
    def score_pair(self,title,abstract,interest):
        return float(len(interest)) - 3.5

@contextmanager
def running(scorer=None,budget=10):
    server=InferenceServer(('127.0.0.1',0),scorer or FakeScorer(),{'identity':IDENTITY,'api_version':1},budget)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
    thread.start()
    try: yield server,'http://127.0.0.1:'+str(server.server_port)
    finally:
        server.shutdown();server.server_close();thread.join()

class ServiceTests(unittest.TestCase):
    def test_transport_matches_existing_aggregation_and_stable_order(self):
        with running() as (_,url):
            remote=HTTPMiniLMScorer(url)
            candidates=[{'title':x,'abstract':ABSTRACT} for x in ('first','second')]
            local=[score_interest_fit(c,['ops','agent reliability'],FakeScorer().score_pair) for c in candidates]
            actual=[score_interest_fit(c,['ops','agent reliability'],remote.score_pair) for c in candidates]
            self.assertEqual(local,actual)
            self.assertEqual(sorted(range(2),key=lambda i:actual[i]['score'],reverse=True),[0,1])
            self.assertEqual(actual[0]['matched_interest'],'agent reliability')
            self.assertEqual(remote.score_pairs([dict(title='T',abstract=ABSTRACT,interest=i) for i in ('ops','agents')]),[-.5,2.5])
            for abstract in (None,'short','none','1234567890'*10):
                self.assertEqual(score_interest_fit({'title':'T','abstract':abstract},['ops'],remote.score_pair),
                                 score_interest_fit({'title':'T','abstract':abstract},['ops'],FakeScorer().score_pair))

    def test_load_once_and_readiness(self):
        with patch('paper_agents.minilm_service.MiniLMScorer',return_value=FakeScorer()) as constructor,patch('paper_agents.minilm_service.metadata',return_value={'identity':IDENTITY}):
            server=create_server(('127.0.0.1',0))
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
            try:
                url='http://127.0.0.1:'+str(server.server_port)
                for _ in range(2): HTTPMiniLMScorer(url).score_pair('T',ABSTRACT,'ops')
                with urllib.request.urlopen(url+'/ready') as r:self.assertTrue(json.load(r)['ready'])
                constructor.assert_called_once()
            finally:server.shutdown();server.server_close();thread.join()

    def test_invalid_oversized_and_overload(self):
        with running() as (server,url):
            client=HTTPMiniLMScorer(url)
            with self.assertRaisesRegex(RuntimeError,'422'):client.score_pair('T','short','ops')
            server.inference.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError,'503'):client.score_pair('T',ABSTRACT,'ops')
            finally:server.inference.release()
            request=urllib.request.Request(url+'/v1/score',data=b'x'*(MAX_BYTES+1),headers={'Content-Type':'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as error:urllib.request.urlopen(request)
            self.assertEqual(error.exception.code,413)
            error.exception.close()
            for _ in range(4):server.connections.acquire()
            try:
                with self.assertRaisesRegex(RuntimeError,'503'):client.score_pair('T',ABSTRACT,'ops')
            finally:
                for _ in range(4):server.connections.release()
            with self.assertRaises(ValueError):client.score_pairs([])
            with self.assertRaises(ValueError):client.score_pairs([{}]*33)

    def test_timeout_unavailable_and_no_fallback(self):
        class Slow:
            def score_pair(self,*args):time.sleep(.15);return 1
        with running(Slow()) as (_,url):
            with self.assertRaisesRegex(RuntimeError,'unavailable'):
                HTTPMiniLMScorer(url,timeout=.02).score_pair('T',ABSTRACT,'ops')
            time.sleep(.17)
        with self.assertRaises(RuntimeError):HTTPMiniLMScorer(url,timeout=.02).score_pair('T',ABSTRACT,'ops')
        with patch.dict(os.environ,{'PAPER_AGENT_MINILM_BACKEND':'http','PAPER_AGENT_MINILM_URL':url}),patch('paper_agents.curator_interest.MiniLMScorer') as embedded:
            result=score_interest_fit({'title':'T','abstract':ABSTRACT},['ops'],default_interest_scorer())
            self.assertEqual(result['status'],'unavailable');self.assertIsNone(result['score'])
            embedded.assert_not_called()

    def test_budget_nonfinite_and_model_identity_failure(self):
        with running(budget=0) as (_,url):
            with self.assertRaisesRegex(RuntimeError,'504'):HTTPMiniLMScorer(url).score_pair('T',ABSTRACT,'ops')
        class Invalid:
            def score_pair(self,*args):return float('nan')
        with running(Invalid()) as (_,url):
            with self.assertRaisesRegex(RuntimeError,'503'):HTTPMiniLMScorer(url).score_pair('T',ABSTRACT,'ops')
        with patch('paper_agents.minilm_service.MiniLMScorer',side_effect=RuntimeError('bad model')):
            with self.assertRaises(RuntimeError):create_server(('127.0.0.1',0))

    def test_client_rejects_invalid_response_and_origin(self):
        from contextlib import closing
        import io
        client=HTTPMiniLMScorer('http://127.0.0.1:8091')
        pair=[dict(title='T',abstract=ABSTRACT,interest='ops')]
        for payload in ({'api_version':1,'identity':{},'scores':[1]},
                        {'api_version':1,'identity':IDENTITY,'scores':[True]},
                        {'api_version':1,'identity':IDENTITY,'scores':[]}, []):
            with patch.object(client.opener,'open',return_value=closing(io.BytesIO(json.dumps(payload).encode()))):
                with self.assertRaises(RuntimeError):client.score_pairs(pair)
        for url in ('https://example.test','http://user:pass@host','http://host/path'):
            with self.assertRaises(ValueError):HTTPMiniLMScorer(url)

    def test_embedded_remains_default_and_bad_backend_rejected(self):
        with patch.dict(os.environ,{},clear=True),patch('paper_agents.curator_interest.MiniLMScorer',return_value=FakeScorer()) as constructor:
            self.assertEqual(default_interest_scorer()('T',ABSTRACT,'ops'),-.5)
            constructor.assert_called_once()
        with patch.dict(os.environ,{'PAPER_AGENT_MINILM_BACKEND':'typo'}):
            with self.assertRaises(ValueError):default_interest_scorer()


class RealModelParity(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('onnxruntime') and importlib.util.find_spec('transformers') and Path(os.environ.get('PAPER_AGENT_MINILM_MODEL_DIR','/models/minilm'),'onnx/model.onnx').is_file(), 'offline ONNX runtime/model unavailable')
    def test_real_engine_http_parity(self):
        from paper_agents.minilm_eval import MiniLMScorer
        path=Path(__file__).resolve().parents[2]/'scripts/minilm_api_parity.py'
        spec=importlib.util.spec_from_file_location('parity',path)
        parity=importlib.util.module_from_spec(spec);spec.loader.exec_module(parity)
        scorer=MiniLMScorer()
        with running(scorer) as (_,url):
            result=parity.compare(scorer.score_pair,HTTPMiniLMScorer(url).score_pair)
            self.assertTrue(result['order_equal'])

if __name__=='__main__':unittest.main()
