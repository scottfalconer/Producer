import collections
import copy
import json
import math
import random
import unittest
from unittest.mock import patch

from recorded_inputs import EVENT_FIELDS, validate_events
from simulated_chat import GENERATOR_VERSION, THEMES, generate_chat, parse_seed


class SimulatedChatTests(unittest.TestCase):
    def test_saved_numeric_seed_round_trips_through_cli(self):
        original = generate_chat(duration=120, seed=18446744073709551615)
        self.assertEqual(original, generate_chat(duration=120, seed=parse_seed(str(original["simulation"]["seed"]))))
        self.assertEqual(parse_seed("demo-night"), "demo-night")

    def test_explicit_seed_replays_all_inputs_and_labels_exactly(self):
        for seed in (0, 2**64 - 1, "demo-night"):
            with self.subTest(seed=seed):
                original = generate_chat(seed=seed)
                frozen = json.loads(json.dumps(original))
                self.assertEqual(frozen, generate_chat(seed=seed))
                self.assertEqual(frozen["simulation"]["seed"], seed)
                self.assertEqual(frozen["simulation"]["generator_version"], GENERATOR_VERSION)
                self.assertEqual(frozen["eval_labels"], original["eval_labels"])

    def test_implicit_seed_uses_fresh_crypto_randomness_and_is_replayable(self):
        with patch("simulated_chat.secrets.randbits", side_effect=[123, 456]) as entropy:
            first, second = generate_chat(), generate_chat()
        self.assertEqual(entropy.call_args_list[0].args, (64,))
        self.assertEqual(entropy.call_count, 2)
        self.assertNotEqual(first["events"], second["events"])
        self.assertEqual(first, generate_chat(seed=first["simulation"]["seed"]))
        self.assertEqual(second, generate_chat(seed=second["simulation"]["seed"]))

    def test_other_runs_do_not_mutate_a_frozen_run_or_global_random_state(self):
        original = generate_chat(seed="frozen")
        saved = copy.deepcopy(original)
        state = random.getstate()
        changed = generate_chat(seed="different")
        changed["events"][0]["text"] = "edited after generation"
        changed["eval_labels"].clear()
        self.assertEqual(random.getstate(), state)
        self.assertEqual(original, saved)
        self.assertEqual(original, generate_chat(seed="frozen"))

    def test_different_seeds_change_useful_content_author_and_timing(self):
        def candidates(data):
            return [row for row in data["events"]
                    if data["eval_labels"][row["id"]]["relevance"] == "high"]
        for theme in THEMES:
            with self.subTest(theme=theme):
                a = candidates(generate_chat(theme=theme, seed="demo-a"))
                b = candidates(generate_chat(theme=theme, seed="demo-b"))
                for field in ("text", "author", "at"):
                    self.assertNotEqual([row[field] for row in a], [row[field] for row in b])
                self.assertNotEqual({row["text"] for row in a}, {row["text"] for row in b})

    def test_all_events_are_valid_synthetic_and_bounded_and_labels_are_separate(self):
        for theme in THEMES:
            for seed in range(24):
                with self.subTest(theme=theme, seed=seed):
                    data = generate_chat(theme=theme, seed=seed)
                    self.assertEqual(validate_events(data), data)
                    self.assertLessEqual(len(data["events"]), 200)
                    self.assertEqual(set(data["eval_labels"]), {row["id"] for row in data["events"]})
                    self.assertTrue(data["synthetic"])
                    self.assertEqual(data["simulation"]["delivery"], "screen_pixels_only")
                    for row in data["events"]:
                        self.assertEqual(set(row), {"at", "type", "id"} | EVENT_FIELDS[row["type"]])
                        self.assertIn("simulated viewer", row["author"])
                        self.assertNotIn("eval_labels", row["text"])

    def test_short_recording_is_exact_prefix_without_future_events_or_labels(self):
        full = generate_chat(seed="prefix")
        for duration in (.1, 30, 120, 599.5):
            with self.subTest(duration=duration):
                short = generate_chat(duration=duration, seed="prefix")
                expected = [row for row in full["events"] if row["at"] <= duration]
                self.assertEqual(short["events"], expected)
                self.assertEqual(short["eval_labels"], {row["id"]: full["eval_labels"][row["id"]] for row in expected})
                self.assertEqual(validate_events(short), short)

    def test_noise_majority_varied_arrivals_recurring_people_and_early_candidates(self):
        for theme in THEMES:
            for seed in range(64):
                with self.subTest(theme=theme, seed=seed):
                    data = generate_chat(theme=theme, seed=seed)
                    rows, labels = data["events"], data["eval_labels"]
                    counts = collections.Counter(label["relevance"] for label in labels.values())
                    self.assertGreater(counts["low"] / len(rows), .65)
                    self.assertLess(counts["high"] / len(rows), .22)
                    self.assertTrue(any(row["at"] <= 120 and labels[row["id"]]["relevance"] == "high" for row in rows))
                    self.assertGreater(max(collections.Counter(row["author"] for row in rows).values()), 2)
                    gaps = [right["at"] - left["at"] for left, right in zip(rows, rows[1:])]
                    self.assertLess(min(gaps), 1.5)
                    self.assertGreater(max(gaps), 9)
                    self.assertGreater(len({row["text"] for row in rows}) / len(rows), .75)

    def test_questions_have_more_than_one_four_second_image_opportunity(self):
        # Five following arrivals is a stricter window than the seven-row
        # stream painter; no candidate receives special visual pinning.
        for seed in range(64):
            data = generate_chat(seed=seed)
            rows = data["events"]
            for index, row in enumerate(rows):
                if data["eval_labels"][row["id"]]["relevance"] != "high":
                    continue
                departure = rows[index + 5]["at"] if index + 5 < len(rows) else data["duration"]
                first_frame = math.ceil(row["at"] / 4) * 4
                self.assertLess(first_frame + 4, departure, (seed, row, departure))

    def test_invalid_inputs_fail_without_silent_theme_or_seed_fallback(self):
        cases = ({"theme": "other"}, {"duration": True}, {"duration": 0}, {"duration": 601},
                 {"duration": float("nan")}, {"duration": float("inf")}, {"seed": True},
                 {"seed": -1}, {"seed": 2**64}, {"seed": ""}, {"seed": "x" * 129}, {"seed": .5})
        for kwargs in cases:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                generate_chat(**kwargs)


if __name__ == "__main__":
    unittest.main()
