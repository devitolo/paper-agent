import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_agents import resource_status, web
from tests.test_observability_transport import fixture


class AWSHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'status.json';self.now=datetime.now(timezone.utc)
        self.status=fixture(self.now.timestamp())['status']
        self.status.update(environment='aws-production',host='aws-production')
        self.status['latest']['disk']['path']='/private/must-not-render'
        self.status['secrets']={'password':'never-render-this'}
        self.status['cpu'].update(average_percent=0,max_observed_percent=0)
        self.save()

    def save(self):self.path.write_text(json.dumps(self.status))
    def read(self):return resource_status.load_aws_resource_status(self.path,now=self.now)
    def page(self):
        with patch.dict(os.environ,{'PAPER_AGENT_AWS_RESOURCE_STATUS_FILE':str(self.path)}):
            return web.render_health_infrastructure({'db':{'integrity':'ok','path':'test.db'}})

    def test_actual_export_projection_and_ui_zero_metrics(self):
        before=self.path.read_bytes();value=self.read();self.assertEqual(value['freshness'],'fresh')
        self.assertNotIn('secrets',value);self.assertNotIn('path',value['disk'])
        html=self.page()
        self.assertNotIn('Telemetry Freshness</dt>',html)
        self.assertIn('just now',html)
        for item in ('AWS','0% average','0% maximum observed','50% used','0.00 GiB available average'):self.assertIn(item,html)
        self.assertNotIn('never-render-this',html);self.assertNotIn('/private/must-not-render',html)
        self.assertEqual(self.path.read_bytes(),before)

    def test_missing_stale_future_and_malformed_are_placeholders(self):
        self.path.unlink();self.assertEqual(self.read()['freshness'],'unavailable')
        for delta in (-2701,301):
            self.status['observed_at']=(self.now+timedelta(seconds=delta)).isoformat();self.save()
            self.assertIn('placeholder',self.page())
            self.assertNotIn('50% used',self.page())
        for payload in ('[]','{bad','x'*32769):
            self.path.write_text(payload);self.assertEqual(self.read()['freshness'],'unavailable')

    def test_old_disk_hidden_while_current_cpu_remains_fresh(self):
        self.status['latest']['observation_times']={'disk':(self.now-timedelta(seconds=2701)).isoformat()};self.save()
        value=self.read();self.assertEqual(value['freshness'],'fresh');self.assertEqual(value['disk_state'],'stale')
        html=self.page();self.assertIn('0% average',html);self.assertIn('stale disk observation',html);self.assertNotIn('50% used',html)

    def test_wrong_identity_and_invalid_numbers_fail_closed(self):
        for key,value in [('average_percent',True),('max_observed_percent',101),('average_percent',float('nan'))]:
            changed=copy.deepcopy(self.status);changed['cpu'][key]=value;self.path.write_text(json.dumps(changed))
            self.assertEqual(self.read()['freshness'],'unavailable')
        self.status['environment']='mini-production';self.save();self.assertEqual(self.read()['freshness'],'unavailable')

    def test_contradictory_complete_window_and_memory_bounds_fail_closed(self):
        cases=[]
        for key,value in (('coverage_seconds',0),('sample_count',0),('sample_count',True)):
            item=copy.deepcopy(self.status);item[key]=value;cases.append(item)
        item=copy.deepcopy(self.status);item['window']['sample_gaps']=1;cases.append(item)
        item=copy.deepcopy(self.status);item['cpu']['average_percent']=None;cases.append(item)
        item=copy.deepcopy(self.status);item['memory']['available_min_bytes']=1;cases.append(item)
        item=copy.deepcopy(self.status);item['memory'].update(available_average_bytes=2,available_min_bytes=1,available_max_bytes=0);cases.append(item)
        for item in cases:
            self.path.write_text(json.dumps(item));result=self.read()
            self.assertEqual(result['freshness'],'unavailable')
            self.assertIn('AWS telemetry export is invalid.',self.page())
            self.assertNotIn('complete · 15-minute window',self.page())

    def test_consistent_partial_window_is_warming(self):
        self.status.update(coverage_seconds=0,sample_count=0)
        self.status['window'].update(complete=False,sample_gaps=1)
        self.status['cpu'].update(average_percent=None,max_observed_percent=None)
        self.save()
        self.assertEqual(self.read()['freshness'],'fresh')
        self.assertIn('warming · incomplete window',self.page())

    def test_parser_recursion_is_invalid_export_and_does_not_break_mini_panel(self):
        with patch.object(resource_status.json,'loads',side_effect=RecursionError('synthetic parser depth')):
            self.assertEqual(self.read()['freshness'],'unavailable')
        self.path.write_text('['*16000+'0'+']'*16000)
        self.assertEqual(self.read()['freshness'],'unavailable')
        self.assertIn('AWS telemetry export is invalid.',self.page())
