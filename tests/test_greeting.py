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
            self.assertTrue(out.rstrip().endswith(g.CANDY))        # баланс последней строкой и не в цитате
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

    def test_emoji_are_static_for_every_bot(self):
        self.assertEqual(g.PALETTES, (("💖", "🤍", "💫", "🍭"),))
        for i in range(300):
            out = g.render(g.generate_config(f"st{i}"), "Тест", 3)
            lines = out.splitlines()
            self.assertTrue(lines[0].startswith("<b>💖 "))             # заголовок
            self.assertTrue(lines[-1].startswith("🍭 ") and lines[-1].endswith("3🍬"))  # баланс
            self.assertIn("🤍 ", out)
            self.assertIn("💫 ", out)
            for old in sum(g._LEGACY_EMOJI.values(), ()):
                self.assertNotIn(old, out)

    def test_texts_by_seed_are_unchanged_by_the_emoji_switch(self):
        # значения сняты ДО перехода на статичные эмодзи
        c = g.generate_config("seed0")
        self.assertEqual((c.layout, c.variant, c.order), (3, "quote_points", "anon_first"))
        self.assertEqual((c.hello, c.anon_key, c.pay, c.nolog_key, c.chats, c.history, c.balance_label),
                         (2, 3, 2, 3, 2, 1, 0))
        c = g.generate_config("seed2")
        self.assertEqual((c.layout, c.variant, c.order), (1, "plain", "nolog_first"))
        self.assertEqual((c.hello, c.anon_key, c.pay, c.nolog_key, c.chats, c.history, c.balance_label),
                         (1, 3, 0, 1, 3, 2, 0))

    def test_template_saved_with_old_random_emoji_is_normalized(self):
        import json
        c = g.generate_config("legacy")
        d = json.loads(c.to_json())
        d["template"] = ("<b>✨ Рады видеть вас в {name}</b>\n\n🕶️ Приватность — оплата без привязок и без риска\n"
                         "🙈 Без следов — чаты не хранятся, историю никто не увидит.\n\n🍭 Ваш баланс: {balance}🍬")
        d["palette"] = 3
        n = g.GreetingConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(n.template, ("<b>💖 Рады видеть вас в {name}</b>\n\n🤍 Приватность — оплата без привязок и без риска\n"
                                      "💫 Без следов — чаты не хранятся, историю никто не увидит.\n\n🍭 Ваш баланс: {balance}🍬"))
        self.assertEqual(g.GreetingConfig.from_json(n.to_json()), n)   # повторное чтение ничего не меняет

    def test_all_layouts_and_variants_are_reachable(self):
        seen = {(g.LAYOUTS[c.layout].key, c.variant) for c in (g.generate_config(f"x{i}") for i in range(600))}
        expected = {(lay.key, v) for lay in g.LAYOUTS for v in lay.variants}
        self.assertEqual(seen, expected)

    def test_balance_is_never_in_a_quote(self):
        for i in range(600):
            out = g.render(g.generate_config(f"q{i}"), "Тест", 5)
            last = out.rstrip().splitlines()[-1]
            self.assertNotIn("blockquote", last)
        self.assertNotIn("quote_balance", {v for lay in g.LAYOUTS for v in lay.variants})

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
        self.assertEqual(g.format_number(1234567), "1234567")

    def test_quote_is_separated_by_blank_lines(self):
        quote_cfgs = [g.generate_config(f"q{i}") for i in range(300)]
        quote_cfgs = [c for c in quote_cfgs if c.variant == "quote_points"]
        self.assertTrue(quote_cfgs)
        for cfg in quote_cfgs:
            t = cfg.template
            self.assertIn("}</b>\n\n<blockquote>", t)
            self.assertIn("</blockquote>\n\n", t)

    def test_old_stored_quote_template_gets_spacing_when_loaded(self):
        cfg = next(c for c in (g.generate_config(f"q{i}") for i in range(300)) if c.variant == "quote_points")
        old = cfg.template.replace("\n\n<blockquote>", "\n<blockquote>").replace("</blockquote>\n\n", "</blockquote>\n")
        self.assertNotEqual(old, cfg.template)
        import json
        raw = json.loads(cfg.to_json())
        raw["template"] = old
        restored = g.GreetingConfig.from_json(json.dumps(raw))
        self.assertEqual(restored.template, cfg.template)

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


class NoGapBetweenPointsTests(unittest.TestCase):
    def test_points_never_separated_by_blank_line(self):
        for i in range(600):
            t = g.generate_config(f"gap{i}").template
            body = t.split("\n\n")
            # заголовок, блок пунктов, баланс (цитата тоже один блок)
            self.assertEqual(len(body), 3, t)

    def test_legacy_airy_template_is_tightened(self):
        import json
        cfg = g.generate_config("airy")
        d = json.loads(cfg.to_json())
        parts = d["template"].split("\n\n")
        pts = parts[1].replace("<blockquote>", "").replace("</blockquote>", "").split("\n")
        d["variant"] = "airy"
        d["template"] = "\n\n".join([parts[0], pts[0], pts[1], parts[2]])
        n = g.GreetingConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(len(n.template.split("\n\n")), 3)
