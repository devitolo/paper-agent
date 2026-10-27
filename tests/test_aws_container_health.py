import io
from unittest.mock import patch
import unittest
from paper_agents import aws_container_status as c,web
from tests.test_aws_container_status import facts


class AWSContainerHealthTests(unittest.TestCase):
    def test_actual_projection_has_matching_columns_and_collapsed_digests(self):
        status=c.validate(c.from_stream(io.BytesIO(facts())))
        page=web.render_aws_container_health(status)
        for text in ('AWS containers','Production app','healthy','0% CPU','5.5 / 64.0 MiB'):
            self.assertIn(text,page)
        self.assertNotIn('<th>Image Digest</th>',page)
        self.assertEqual(page.count('<details class="container-details">'),2)
        self.assertNotIn('MiniLM',page)

    def test_infrastructure_places_aws_next_to_mini(self):
        status=c.validate(c.from_stream(io.BytesIO(facts())))
        with patch.object(web,'mini_telemetry_health',return_value={'freshness':'unavailable'}),patch.object(web,'load_aws_resource_status',return_value={'freshness':'unavailable'}),patch.object(web,'load_aws_container_status',return_value=status):
            page=web.render_health_infrastructure({})
        self.assertIn('resource-columns container-host-columns',page)
        self.assertLess(page.index('mini-container-health'),page.index('aws-container-health'))
        self.assertIn('Production app',page)

    def test_stale_values_are_hidden_and_completed_collector_is_identified(self):
        page=web.render_aws_container_health({'freshness':'stale','items':None})
        self.assertIn('No fresh AWS container observations',page);self.assertNotIn('sha256:',page)
        value=c.from_stream(io.BytesIO(facts()));item=value['items'][1]
        item.update(status='unavailable',health='unavailable',image_digest=None,image_id=None,cpu_percent=None,memory_bytes=None,memory_limit_bytes=None)
        page=web.render_aws_container_health(c.validate(value))
        self.assertIn('Last collection OK',page)
        self.assertIn('Scheduled',page)


if __name__=='__main__':unittest.main()
