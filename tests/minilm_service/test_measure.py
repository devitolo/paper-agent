import importlib.util
from pathlib import Path
import unittest

spec=importlib.util.spec_from_file_location('measure',Path(__file__).resolve().parents[2]/'scripts/minilm_service_measure.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class MeasurementTests(unittest.TestCase):
    def test_bounded_requests_without_embedded_model(self):
        class Client:
            calls=[]
            def score_pairs(self,pairs):self.calls.append(len(pairs));return [1.0]*len(pairs)
        client=Client();report=m.measure(client)
        self.assertTrue(report['complete'])
        self.assertEqual(client.calls,[1,1,1,1,1,32,32])
        self.assertNotIn('Synthetic reliability study',str(report))

    def test_failure_stops_without_retry_or_false_success(self):
        class Client:
            calls=0
            def score_pairs(self,pairs):self.calls+=1;raise TimeoutError('private content')
        client=Client();report=m.measure(client)
        self.assertFalse(report['complete']);self.assertEqual(client.calls,1)
        self.assertNotIn('private content',str(report))

    def test_elapsed_budget_prevents_more_calls(self):
        class Client:
            def score_pairs(self,pairs):raise AssertionError('must not call')
        clock=iter([0,61,61])
        report=m.measure(Client(),lambda:next(clock))
        self.assertFalse(report['complete']);self.assertEqual(report['requests'],[])

if __name__=='__main__':unittest.main()
