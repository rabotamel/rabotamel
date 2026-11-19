import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import captcha, greeting as g  # noqa: E402

FORBIDDEN = ("---", "***", "———", "═", "─", "│")


class GreetingTests(unittest.TestCase):
    def test_deterministic_and_roundtrip(self):
        for i in range(200):
            seed = f"{i}:token{i}"
            a, b = g.generate_config(seed), g.generate_config(seed)
            self.assertEqual(a, b)
            self.assertEqual(g.GreetingConfig.from_json(a.to_json()), a)

    def test_required_content_everywhere(self):
        for i in range(500):
            cfg = g.generate_config(f"seed{i}")
            out = g.render(cfg, "Тест", 42)
            self.assertIn("Тест", out)
            self.assertIn(f"42{g.CANDY}", out)                    # баланс сразу перед 🍬
            self.assertTrue(out.rstrip().endswith(g.CANDY) or out.rstrip().endswith(g.CANDY + "</blockquote>"))
            self.assertTrue(any(w in out for w in g.ANON_KEY))
            self.assertTrue(any(w in out for w in g.NOLOG_KEY))
            self.assertTrue(any(w in out for w in g.PAY))
            self.assertTrue(any(w in out for w in g.CHATS))
            self.assertTrue(any(w in out for w in g.HISTORY))
            self.assertTrue(any(w in out for w in g.BALANCE_LABEL))
            self.assertFalse(any(bad in out for bad in FORBIDDEN))

    def test_candy_emoji_is_never_used_as_a_variable_emoji(self):
        for pal in g.PALETTES:
            self.assertNotIn(g.CANDY, pal)

    def test_emoji_from_declared_sets(self):
        for pal in g.PALETTES:
            for slot, emo in zip(("hello", "anon", "nolog", "balance"), pal):
                self.assertIn(emo, g.EMOJI_SETS[slot])

    def test_all_layouts_and_variants_are_reachable(self):
        seen = {(g.LAYOUTS[c.layout].key, c.variant) for c in (g.generate_config(f"x{i}") for i in range(600))}
        expected = {(lay.key, v) for lay in g.LAYOUTS for v in lay.variants}
        self.assertEqual(seen, expected)

    def test_both_point_orders_occur(self):
        orders = {g.generate_config(f"o{i}").order for i in range(200)}
        self.assertEqual(orders, {"anon_first", "nolog_first"})

    def test_name_is_escaped_and_cannot_inject_placeholder(self):
        cfg = g.generate_config("esc")
        out = g.render(cfg, "<b>{balance}</b> & Co", 7)
        self.assertNotIn("<b>{balance}", out)
        self.assertIn("&lt;b&gt;{balance}&lt;/b&gt; &amp; Co", out)

    def test_taken_signatures_are_avoided(self):
        first = g.generate_config("same")
        second = g.generate_config("same", taken={first.signature()})
        self.assertNotEqual(first.signature(), second.signature())

    def test_number_format(self):
        self.assertEqual(g.format_number(0), "0")
        self.assertEqual(g.format_number(1234567), "1\u00a0234\u00a0567")

    def test_stored_template_is_independent_from_dictionaries(self):
        cfg = g.generate_config("stable")
        raw = cfg.to_json()
        old = g.HELLO
        try:
            g.HELLO = ("Совсем другое приветствие",) * 3  # имитируем правку словаря в новой версии кода
            restored = g.GreetingConfig.from_json(raw)
            self.assertEqual(g.render(restored, "Х", 1), g.render(cfg, "Х", 1))
        finally:
            g.HELLO = old


class CaptchaStillWorks(unittest.TestCase):
    def test_config_roundtrip(self):
        cfg = captcha.generate_config("abc")
        self.assertEqual(captcha.CaptchaConfig.from_json(cfg.to_json()), cfg)


if __name__ == "__main__":
    unittest.main()
