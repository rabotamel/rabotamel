import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import deco, gallery as gl, greeting, shop_buttons as sb  # noqa: E402

FORBIDDEN = ("---", "***", "———", "═", "─", "│")


class GalleryTests(unittest.TestCase):
    def test_deterministic_and_roundtrip(self):
        for i in range(200):
            a, b = gl.generate_config(f"t{i}"), gl.generate_config(f"t{i}")
            self.assertEqual(a, b)
            self.assertEqual(gl.GalleryConfig.from_json(a.to_json()), a)
            d = deco.generate_config(f"t{i}")
            self.assertEqual(deco.DecoConfig.from_json(d.to_json()), d)

    def test_price_fixed_and_back_fixed(self):
        for i in range(300):
            c = gl.generate_config(f"p{i}")
            self.assertIn("(3🍬)", c.buy_label)
            self.assertEqual(gl.layout_rows(c)[-1][0][0], "⬅️ Назад")
            self.assertFalse(any(bad in c.description + c.buy_label + c.premium_label for bad in FORBIDDEN))

    def test_independent_choice_and_all_values_reachable(self):
        cfgs = [gl.generate_config(f"x{i}") for i in range(800)]
        self.assertEqual({(c.buy_text, c.buy_emoji) for c in cfgs}, {(a, b) for a in range(4) for b in range(4)})
        self.assertEqual({(c.premium_text, c.premium_emoji) for c in cfgs}, {(a, b) for a in range(len(gl.PREMIUM_TEXTS)) for b in range(4)})
        self.assertEqual({c.desc for c in cfgs}, set(range(len(gl.DESCRIPTIONS))))
        self.assertEqual({deco.generate_config(f"d{i}").left for i in range(300)}, set(deco.EMOJI))

    def test_callbacks_unique_and_valid(self):
        seen = set()
        for i in range(1000):
            c = gl.generate_config(f"b{i}")
            for cb in (c.buy_cb, c.premium_cb, c.back_cb):
                self.assertLessEqual(len(cb.encode()), 64)
                self.assertNotIn(cb, seen)
                seen.add(cb)
        a, b = gl.generate_config("a"), gl.generate_config("b")
        self.assertEqual(gl.parse_action(a, a.back_cb), "back")
        self.assertIsNone(gl.parse_action(a, b.back_cb))

    def test_render_uses_bot_title_marker_and_greeting_balance_line(self):
        for i in range(300):
            g = greeting.generate_config(f"g{i}")
            s = sb.generate_config(f"g{i}")
            out = gl.render(gl.generate_config(f"g{i}"), deco.generate_config(f"g{i}"), s.gallery_heading, g.template, 12)
            lines = out.split("\n")
            self.assertEqual(lines[0], f"<b>{s.gallery_heading}</b>")
            d = deco.generate_config(f"g{i}")
            self.assertTrue(lines[2].startswith(d.left + " ") and lines[2].endswith(" " + d.right))
            self.assertEqual(lines[-1], greeting.render(g, "x", 12).rstrip().splitlines()[-1])
            self.assertTrue(out.endswith(f"12{greeting.CANDY}"))
            self.assertFalse(any(bad in out for bad in FORBIDDEN))

class _FakeAiogramTypes:
    """Подмена aiogram.types на время теста: запоминает kwargs кнопок (aiogram может быть не установлен)."""

    def __enter__(self):
        import types
        self._saved = {k: sys.modules.get(k) for k in ("aiogram", "aiogram.types")}

        class _Simple:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        mod = types.ModuleType("aiogram.types")
        mod.InlineKeyboardButton = mod.InlineKeyboardMarkup = _Simple
        pkg = types.ModuleType("aiogram")
        pkg.types = mod
        sys.modules["aiogram"], sys.modules["aiogram.types"] = pkg, mod
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


class GalleryColorTests(unittest.TestCase):
    def test_gallery_buttons_blue_green_blue_red(self):
        for i in range(100):
            c = gl.generate_config(f"gc{i}")
            with _FakeAiogramTypes():
                kb = gl.build_keyboard(c)
            rows = kb.inline_keyboard
            self.assertEqual([len(r) for r in rows], [1, 1, 1, 1])
            # «Предложка» — синяя, как и покупка видео
            self.assertEqual([getattr(r[0], "style", None) for r in rows], ["primary", "success", "primary", "danger"])
            self.assertEqual(rows[2][0].text, "📩 Предложка 🤍")
            self.assertEqual(rows[3][0].text, "⬅️ Назад")


class LegacyPremiumTests(unittest.TestCase):
    def test_removed_premium_text_is_replaced_and_not_generated(self):
        import json
        self.assertNotIn("Безлимит и скачивание", gl.PREMIUM_TEXTS)
        for i in range(300):
            self.assertNotIn("Безлимит", gl.generate_config(f"p{i}").premium_label)
        d = json.loads(gl.generate_config("old").to_json())
        d["premium_label"] = "💎 Безлимит и скачивание 👑"
        n = gl.GalleryConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertNotIn("Безлимит", n.premium_label)


if __name__ == "__main__":
    unittest.main()
