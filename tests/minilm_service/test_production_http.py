import os
from pathlib import Path
import unittest
from unittest.mock import patch
from test_service import running, ABSTRACT
from paper_agents.minilm_http import HTTPMiniLMScorer
from paper_agents.minilm_eval import default_eval_scorer, INTERESTS
from paper_agents import package_runtime

class ProductionHTTPTests(unittest.TestCase):
    def test_raw_endpoint_preserves_legacy_short_and_whitespace_inputs(self):
        class Recorder:
            calls=[]
            def score_pair(self,*args):self.calls.append(args);return len(args[2])*.5
        recorder=Recorder()
        with running(recorder) as (_,url):
            client=HTTPMiniLMScorer(url,raw_pairs=True)
            self.assertEqual(client.score_pair(' T\n title ','short\n abstract',' interest '),5)
            self.assertEqual(recorder.calls[-1],(' T\n title ','short\n abstract',' interest '))
            with patch.dict(os.environ,{'PAPER_AGENT_MINILM_BACKEND':'http','PAPER_AGENT_MINILM_URL':url}):
                self.assertEqual(default_eval_scorer()('T','short'),max(len(i)*.5 for i in INTERESTS))
                self.assertEqual([c[2] for c in recorder.calls[-3:]],list(INTERESTS))

    def test_startup_fails_before_state_initialization_when_service_unavailable(self):
        with patch.dict(os.environ,{'PAPER_AGENT_MINILM_BACKEND':'http','PAPER_AGENT_MINILM_URL':'http://127.0.0.1:8091'}),patch('sys.argv',['runtime','start']),patch('paper_agents.minilm_http.HTTPMiniLMScorer.check_ready',side_effect=RuntimeError('unavailable')),patch.object(package_runtime,'initialize') as init:
            self.assertEqual(package_runtime.main(),1)
            init.assert_not_called()

    def test_readiness_and_explicit_inference_check_use_http(self):
        with running() as (_,url),patch.dict(os.environ,{'PAPER_AGENT_MINILM_BACKEND':'http','PAPER_AGENT_MINILM_URL':url}):
            self.assertTrue(HTTPMiniLMScorer(url).check_ready()['ready'])
            with patch('sys.argv',['runtime','check-minilm']),patch('paper_agents.curator_interest.MiniLMScorer') as embedded:
                self.assertEqual(package_runtime.main(),0);embedded.assert_not_called()

    def test_selected_target_excludes_inference_install_and_legacy_target_retained(self):
        text=(Path(__file__).resolve().parents[2]/'Dockerfile').read_text()
        target=text.split('FROM migration-gemini AS mini-production-http',1)[1].split('FROM base AS runtime',1)[0]
        self.assertNotIn('requirements-minilm',target)
        self.assertIn('ENV PAPER_AGENT_MINILM_BACKEND=http',target)
        self.assertIn('FROM migration-gemini AS mini-production\n',text)

if __name__=='__main__':unittest.main()
