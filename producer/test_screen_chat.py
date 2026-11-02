"""Frame receipts and model-observed quotes; no fixture text or model calls."""

import hashlib
import unittest

from screen_chat import ScreenChat


class ScreenChatTests(unittest.TestCase):
    def setUp(self):
        self.chat = ScreenChat("selected-show-one")

    def tearDown(self):
        self.chat.close()

    def frame(self, source_time=4, jpeg=b"selected-programme-image"):
        ident = self.chat.frame_id(jpeg, source_time)
        metadata = self.chat.register_frame(ident, source_time, hashlib.sha256(jpeg).hexdigest())
        return ident, metadata

    def test_observation_preserves_the_exact_reported_author_and_body(self):
        ident, metadata = self.frame()
        body = "  First time here — excited! 🎃\nWhat’s that material?  "
        row = self.chat.observe(ident, "Maya · simulated viewer", body, [metadata], 4)
        self.assertEqual(row["text"], body)
        self.assertEqual(row["author"], "Maya · simulated viewer")
        self.assertEqual(row["frame_id"], ident)
        self.assertEqual(row["frame_source_time"], 4)
        self.assertEqual(row["source"], "screen")
        self.assertEqual(self.chat.poll("selected-show-one")["questions"], [row])
        self.assertNotIn("selected-programme-image", repr(self.chat.frames(4)))

    def test_requires_a_recent_registered_frame_in_the_examined_state(self):
        ident, metadata = self.frame()
        for examined, now in (([], 4), ([{**metadata, "sha256": "0" * 64}], 4), ([metadata], 3), ([metadata], 24.001)):
            with self.subTest(examined=examined, now=now), self.assertRaisesRegex(ValueError, "unavailable|stale|examined"):
                self.chat.observe(ident, "Viewer", "What is that?", examined, now)
            self.assertEqual(self.chat.questions, [])

    def test_session_identity_is_part_of_both_frame_and_observed_identity(self):
        ident, metadata = self.frame()
        other = ScreenChat("selected-show-two")
        try:
            with self.assertRaises(ValueError):
                other.observe(ident, "Viewer", "What is that?", [metadata], 4)
            second_frame = other.frame_id(b"selected-programme-image", 4)
            self.assertNotEqual(ident, second_frame)
            second = other.register_frame(second_frame, 4, metadata["sha256"])
            one = self.chat.observe(ident, "Viewer", "What is that?", [metadata], 4)
            two = other.observe(second_frame, "Viewer", "What is that?", [second], 4)
            self.assertNotEqual(one["id"], two["id"])
            with self.assertRaises(ValueError):
                self.chat.poll("selected-show-two")
        finally:
            other.close()

    def test_replay_reuses_recorded_frame_identity_without_original_api_session_token(self):
        ident, metadata = self.frame()
        original = self.chat.observe(ident, "Viewer", "What is that?", [metadata], 4)
        replay = ScreenChat("different-local-replay-owner")
        try:
            replay.register_frame(ident, metadata["source_time"], metadata["sha256"])
            observed = replay.observe(ident, "Viewer", "What is that?", [metadata], 4)
            self.assertEqual(observed, original)
        finally:
            replay.close()

    def test_unknown_conflicting_and_out_of_order_frames_are_rejected(self):
        ident, metadata = self.frame()
        self.assertEqual(self.chat.register_frame(ident, 4, metadata["sha256"]), metadata)
        for name, when, digest in ((ident, 4, "0" * 64), ("other", 3, "0" * 64), ("invalid id", 5, "0" * 64), ("other", 5, "not-a-digest")):
            with self.subTest(name=name, when=when), self.assertRaises(ValueError):
                self.chat.register_frame(name, when, digest)
        with self.assertRaises(ValueError):
            self.chat.observe("not-submitted", "Viewer", "Question", [metadata], 4)

    def test_same_exact_comment_deduplicates_but_a_changed_body_is_distinct(self):
        ident, metadata = self.frame()
        first = self.chat.observe(ident, "Viewer", "What is that?", [metadata], 4)
        ident2, metadata2 = self.frame(8)
        again = self.chat.observe(ident2, "Viewer", "What is that?", [metadata2], 8)
        changed = self.chat.observe(ident2, "Viewer", "What is that?!", [metadata2], 8)
        self.assertEqual(first["id"], again["id"])
        self.assertNotEqual(first["id"], changed["id"])
        self.assertEqual(len(self.chat.questions), 2)
        self.assertEqual(self.chat.questions[0]["frame_id"], ident2)
        ident3, metadata3 = self.frame(128)
        expired = self.chat.observe(ident3, "Viewer", "What is that?", [metadata3], 128)
        self.assertNotEqual(expired["id"], again["id"])

    def test_utf16_bounds_omit_whole_comment_without_shortening(self):
        ident, metadata = self.frame()
        self.chat.observe(ident, "Viewer", "🎃" * 90, [metadata], 4)
        for author, body in (("Viewer", "🎃" * 91), ("x" * 101, "A question"), ("", "Question"), ("Viewer", "  "), ("Viewer", "\ud800")):
            with self.subTest(author=author, body=repr(body)), self.assertRaises(ValueError):
                self.chat.observe(ident, author, body, [metadata], 4)
        self.assertEqual(len(self.chat.questions), 1)
        self.assertEqual(self.chat.questions[0]["text"], "🎃" * 90)

    def test_bounded_candidates_expire_but_visible_question_is_not_a_scroll_deletion(self):
        for number in range(25):
            ident, metadata = self.frame(number)
            self.chat.observe(ident, "Viewer", "Question " + str(number), [metadata], number)
        self.assertEqual(len(self.chat.questions), 20)
        self.assertEqual(len(self.chat.frames(24)), 8)
        selected = self.chat.questions[-1]["id"]
        self.assertTrue(self.chat.pin_question("selected-show-one", selected))
        self.assertEqual(self.chat.frames(45), [])
        self.assertTrue(self.chat.poll("selected-show-one", 45)["selection"]["valid"])
        expired = self.chat.poll("selected-show-one", 144)
        self.assertEqual(expired["questions"], [])
        self.assertEqual(expired["selection"]["message_id"], selected)
        self.assertFalse(self.chat.pin_question("selected-show-one", selected))
        self.chat.unpin_question("selected-show-one")
        self.assertIsNone(self.chat.selection)

    def test_stop_erases_source_candidates_frames_and_pin(self):
        ident, metadata = self.frame()
        row = self.chat.observe(ident, "Viewer", "Question", [metadata], 4)
        self.chat.pin_question("selected-show-one", row["id"])
        self.chat.close()
        self.assertEqual(self.chat.questions, [])
        self.assertEqual(self.chat._frames, [])
        self.assertIsNone(self.chat.selection)
        self.assertIsNone(self.chat.owner)
        for action in (lambda: self.chat.frames(4), lambda: self.chat.poll("selected-show-one"),
                       lambda: self.chat.register_frame(ident, 4, metadata["sha256"]),
                       lambda: self.chat.observe(ident, "Viewer", "Question", [metadata], 4)):
            with self.assertRaises(ValueError):
                action()


if __name__ == "__main__":
    unittest.main()
