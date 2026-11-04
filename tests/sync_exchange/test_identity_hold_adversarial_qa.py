"""Offline crash-boundary and publication guards for held identity evidence."""
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from paper_agents import whole_cycle_exchange as exchange
from tests.sync_exchange import test_identity_hold as fixture


class IdentityHoldAdversarialQA(unittest.TestCase):
    setUp=fixture.IdentityHoldTests.setUp
    hold=fixture.IdentityHoldTests.hold

    def committed_but_uncleared(self):
        with patch.object(exchange,'resume_identity_hold',side_effect=OSError('QA crash before slot clear')):
            with self.assertRaisesRegex(OSError,'before slot clear'):self.hold()
        self.assertTrue((self.state/'identity-restore-required.json').exists())
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)

    def test_push_never_transmits_payload_after_durable_hold_even_before_retry_slot_is_cleared(self):
        self.committed_but_uncleared()
        transport=Mock(return_value={'status':'applied','batch_key':'held33'})
        with patch.dict(os.environ,{'PAPER_AGENT_SYNC_SHARED_SECRET':'offline-fixture'}):
            try:
                exchange.push(self.db,self.root,self.state,'https://unused.invalid',transport=transport)
            except ValueError:
                pass  # Failing closed is also acceptable; retransmission is not.
        transport.assert_not_called()
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],32)
        self.assertEqual(list(self.state.glob('published-*.json')),[])
        capsule=next((self.state/'held').iterdir())
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),self.raw)

    def test_changed_pending_payload_blocks_recovery_without_deleting_either_evidence(self):
        self.committed_but_uncleared()
        changed=b'{"batch_id":"different-pending"}'
        (self.state/'pipeline-outbox.json').write_bytes(changed)
        with self.assertRaisesRegex(ValueError,'differs from held evidence'):
            exchange.resume_identity_hold(self.state)
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),changed)
        capsule=next((self.state/'held').iterdir())
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),self.raw)

    def test_corruption_of_committed_capsule_database_blocks_slot_release(self):
        self.committed_but_uncleared()
        capsule=next((self.state/'held').iterdir())
        (capsule/'paper_agent.db').write_bytes(b'corrupt held database')
        with self.assertRaisesRegex(ValueError,'integrity'):
            exchange.resume_identity_hold(self.state)
        self.assertEqual((self.state/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertTrue((self.state/'identity-restore-required.json').exists())

    def test_interruption_after_slot_unlink_still_has_durable_evidence_and_can_resume(self):
        self.committed_but_uncleared()
        with patch.object(exchange,'_sync_directory',side_effect=OSError('QA after unlink')):
            with self.assertRaisesRegex(OSError,'after unlink'):
                exchange.resume_identity_hold(self.state)
        self.assertTrue((self.state/'identity-restore-required.json').exists())
        result=exchange.resume_identity_hold(self.state)
        capsule=self.state/result['capsule']
        manifest=json.loads((capsule/'manifest.json').read_text())
        self.assertEqual(manifest['files']['pipeline-outbox.json'],hashlib.sha256(self.raw).hexdigest())
        self.assertEqual((capsule/'pipeline-outbox.json').read_bytes(),self.raw)
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],32)

    def test_allocator_uses_maximum_acknowledged_or_reserved_value_and_persists_gaps(self):
        (self.state/'sequence.json').write_text('{"sequence":45}')
        (self.state/'reserved-sequence.json').write_text('{"sequence":33}')
        self.assertEqual(exchange.next_sequence(self.state),46)
        self.assertEqual(exchange.next_sequence(self.state),47)
        self.assertEqual(json.loads((self.state/'sequence.json').read_text())['sequence'],45)

    def test_corrupt_acknowledged_sequence_is_not_silently_reallocated(self):
        for value in (True,-1,'32',None):
            with self.subTest(value=value):
                (self.state/'sequence.json').write_text(json.dumps({'sequence':value}))
                with self.assertRaisesRegex(ValueError,'invalid sequence'):
                    exchange.next_sequence(self.state)
        self.assertFalse((self.state/'reserved-sequence.json').exists())


if __name__=='__main__':unittest.main()
