import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import captcha as c  # noqa: E402

SECRET = "123456:SECRET"


class DynamicTargetTests(unittest.TestCase):
    def setUp(self):
        self.cfg = c.generate_config("bot-token-1")

    def test_target_is_deterministic_and_among_fruits(self):
        for n in ("0000", "abcd", "ffff"):
            t = c.target_for(self.cfg, SECRET, 7, n)
            self.assertIn(t, self.cfg.fruits)
            self.assertEqual(t, c.target_for(self.cfg, SECRET, 7, n))

    def test_every_fruit_can_become_target(self):
        seen = {c.target_for(self.cfg, SECRET, 7, f"{i:04x}") for i in range(300)}
        self.assertEqual(seen, set(self.cfg.fruits))

    def test_new_nonce_changes_target_every_time(self):
        prev = None
        for _ in range(200):
            nonce = c.new_nonce(self.cfg, SECRET, 7, previous_target=prev)
            target = c.target_for(self.cfg, SECRET, 7, nonce)
            self.assertNotEqual(target, prev)
            prev = target

    def test_text_names_the_target_fruit(self):
        for key in self.cfg.fruits:
            self.assertIn(f"<b>{c.FRUITS[key].acc}</b>", c.render_text(self.cfg, key))

    def test_answer_is_checked_against_nonce_target(self):
        nonce = c.new_nonce(self.cfg, SECRET, 7)
        target = c.target_for(self.cfg, SECRET, 7, nonce)
        data = lambda k: f"{c.CALLBACK_PREFIX}{k}:{nonce}"  # noqa: E731
        self.assertEqual(c.parse_nonce(data(target)), nonce)
        self.assertTrue(c.is_correct(self.cfg, c.parse_answer(data(target)), target))
        wrong = next(k for k in self.cfg.fruits if k != target)
        self.assertFalse(c.is_correct(self.cfg, c.parse_answer(data(wrong)), target))

    def test_legacy_buttons_without_nonce_use_config_target(self):
        data = f"{c.CALLBACK_PREFIX}{self.cfg.target}"
        self.assertIsNone(c.parse_nonce(data))
        self.assertEqual(c.parse_answer(data), self.cfg.target)
        self.assertTrue(c.is_correct(self.cfg, c.parse_answer(data)))

    def test_garbage_nonce_is_ignored(self):
        self.assertIsNone(c.parse_nonce("cap:apple:zzzz"))
        self.assertIsNone(c.parse_nonce("cap:apple:12345"))
        self.assertEqual(c.parse_answer("cap:apple:zzzz"), "apple")

    def test_callback_data_fits_telegram_limit(self):
        for key in c.FRUITS:
            self.assertLessEqual(len((c.CALLBACK_PREFIX + key + ":ffff").encode()), 64)


if __name__ == "__main__":
    unittest.main()
