"""Model-managed Realtime topic orientation; no model, hardware or Twitch."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audience import Audience
from on_air import OnAir
import server


TOPIC = {"subject": "Practical AI", "purpose": "One useful task, from idea to result"}


class ViewerTopicLifetimeTests(unittest.TestCase):
    def test_realtime_topic_outlives_independent_editorial_card(self):
        output = OnAir(True, context_persistent=True)
        output.update_context(TOPIC, now=0)
        output.apply_card("show", {"kind": "takeaway", "line": "Check the result."}, 10, now=0)
        state = output.snapshot(600)
        self.assertEqual(state["context"], TOPIC)
        self.assertIsNone(state["graphic"])
        self.assertNotIn("until", state["context"])

    def test_model_can_keep_replace_or_clear_durable_topic(self):
        output = OnAir(True, context_persistent=True)
        self.assertTrue(output.update_context(TOPIC, now=0))
        self.assertFalse(output.update_context(None, now=120, action="keep"))
        self.assertEqual(output.snapshot(900)["context"], TOPIC)
        changed = {"subject": "Checking the result", "purpose": "A complete example"}
        self.assertTrue(output.update_context(changed, now=901))
        self.assertEqual(output.snapshot(1800)["context"], changed)
        self.assertTrue(output.update_context(None, now=1801, action="clear"))
        self.assertFalse(output.update_context(None, now=1802, action="keep"))
        self.assertIsNone(output.snapshot(1802)["context"])

    def test_legacy_context_still_expires_and_cannot_be_revived_by_keep(self):
        output = OnAir(True)
        output.update_context(TOPIC, now=0)
        output.update_context(None, now=100, action="keep")
        self.assertIsNone(output.snapshot(100)["context"])
        output.update_context(TOPIC, now=101)
        self.assertEqual(output.snapshot(190)["context"]["subject"], TOPIC["subject"])
        self.assertIsNone(output.snapshot(191)["context"])

    def test_disabling_persistent_output_clears_topic_before_reenable(self):
        output = OnAir(True, context_persistent=True)
        output.update_context(TOPIC, now=0)
        output.enabled = False
        self.assertIsNone(output.snapshot(1)["context"])
        output.enabled = True
        self.assertIsNone(output.snapshot(2)["context"])
        output.update_context(TOPIC, now=3)
        output.enabled = False
        self.assertFalse(output.update_context(None, now=4, action="keep"))
        output.enabled = True
        self.assertIsNone(output.snapshot(5)["context"])

    def test_stop_and_source_clear_remove_durable_topic(self):
        output = OnAir(True, context_persistent=True)
        output.update_context(TOPIC, now=0)
        output.clear_output()
        self.assertIsNone(output.snapshot(900)["context"])
        output.update_context(TOPIC, now=901)
        output.close()
        self.assertIsNone(output.snapshot(902)["context"])
        self.assertFalse(output.update_context(TOPIC, now=903))

    def test_closed_realtime_source_withdraws_topic_via_actual_public_state(self):
        output = OnAir(True, context_persistent=True)
        output.update_context(TOPIC, now=0)
        core = SimpleNamespace(snapshot=lambda: {"closed": False})
        session = {"director": SimpleNamespace(core=core), "on_air": output,
                   "visual": None, "confidence": {"line": "Private cue"}}
        with patch("server.audience", Audience()):
            self.assertEqual(server.public_state("fixture-show", session)["context"], TOPIC)
            core.snapshot = lambda: {"closed": True}
            state = server.public_state("fixture-show", session)
        self.assertIsNone(state["context"])
        self.assertIsNone(state["graphic"])
        self.assertIsNone(session["confidence"])


if __name__ == "__main__":
    unittest.main()
