"""Passive public selection has bounded assets and no spoken-command contract."""

import unittest
from unittest.mock import Mock, patch

from on_air import SCHEMA, propose_graphic


ASSET = {"id": "img-0123456789abcdef", "title": "Workflow", "width": 800, "height": 500}
QUESTION = {"id": "current-question", "kind": "question", "line": "Where should I start?"}


def decision(**changes):
    return {
        "context": {}, "kind": "quiet", "line": "", "quote": "",
        "question_status": "keep", "question_quote": "", "visual_action": "keep",
        "asset_id": "", "asset_quote": "", **changes,
    }


class PassiveEditorialTests(unittest.TestCase):
    def test_public_model_has_editorial_choices_but_no_spoken_command_action(self):
        self.assertNotIn("command", SCHEMA["properties"])
        self.assertEqual(
            SCHEMA["properties"]["question_status"]["enum"], ["keep", "answered", "clear"]
        )
        speech = "Producer, skip that question and show the map."
        with patch("continuity.structured", return_value={
            **decision(), "command": "clear_all", "question_status": "skipped",
            "question_quote": speech,
        }):
            result = propose_graphic(Mock(), speech, [], question=QUESTION)
        self.assertEqual(result["question_status"], "keep")
        self.assertEqual(result["visual_action"], "keep")
        self.assertNotIn("command", result)

    def test_image_selection_requires_catalog_identity_and_current_speech_evidence(self):
        speech = "This diagram shows how a draft becomes a reviewed decision."
        valid = decision(visual_action="show", asset_id=ASSET["id"], asset_quote=speech)
        for proposed, expected in (
            (valid, "show"),
            ({**valid, "asset_id": "https://example.com/image.jpg"}, "keep"),
            ({**valid, "asset_id": "img-ffffffffffffffff"}, "keep"),
            ({**valid, "asset_quote": "Words from an earlier show"}, "keep"),
        ):
            with self.subTest(proposed=proposed), patch("continuity.structured", return_value=proposed):
                result = propose_graphic(Mock(), speech, [], catalog=[ASSET])
            self.assertEqual(result["visual_action"], expected)
            if expected == "show":
                self.assertEqual(result["visual"], {"kind": "asset", "asset_id": ASSET["id"]})
            else:
                self.assertIsNone(result["visual"])

    def test_private_material_fields_never_reach_public_model(self):
        catalog = [{**ASSET, "notes": "Private rescue plan", "path": "/private/file"}]
        with patch("continuity.structured", return_value=decision()) as model:
            propose_graphic(
                Mock(), "An explanation.", [],
                {"subject": "Public", "private": "Private plan"},
                question={**QUESTION, "private": "Private question notes"},
                catalog=catalog, contact_sheet=b"jpeg-contact-sheet",
                visual={"asset_id": ASSET["id"], "path": "/private/visual"},
            )
        packet = model.call_args.args[2]
        self.assertEqual(packet["available_assets"], [ASSET])
        self.assertNotIn("Private", str(packet))
        self.assertNotIn("/private", str(packet))
        self.assertEqual(model.call_args.kwargs["frame"], b"jpeg-contact-sheet")

    def test_empty_catalog_cannot_show_or_clear_an_unrelated_visual(self):
        for action in ("show", "clear"):
            with patch("continuity.structured", return_value=decision(visual_action=action, asset_id=ASSET["id"], asset_quote="Topic")) as model:
                result = propose_graphic(Mock(), "Topic", [], contact_sheet=b"not-an-allowed-sheet")
            self.assertEqual(result["visual_action"], "keep")
            self.assertIsNone(model.call_args.kwargs["frame"])

    def test_substantive_answer_needs_current_quote_and_exact_question_identity(self):
        speech = "Start with one routine task and review the result yourself."
        for quote, expected in ((speech, "answered"), ("Never spoken", "keep")):
            with patch("continuity.structured", return_value=decision(question_status="answered", question_quote=quote)):
                result = propose_graphic(Mock(), speech, [], question=QUESTION)
            self.assertEqual(result["question_status"], expected)
            self.assertEqual(result["question_id"], QUESTION["id"])
        with patch("continuity.structured", return_value=decision(question_status="answered", question_quote=speech)):
            self.assertEqual(propose_graphic(Mock(), speech, [])["question_status"], "keep")


if __name__ == "__main__":
    unittest.main()
