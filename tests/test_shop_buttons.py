import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import shop_buttons as sb  # noqa: E402

FORBIDDEN = ("---", "***", "———", "═", "─", "│")


class ShopButtonsTests(unittest.TestCase):
    def test_deterministic_and_roundtrip(self):
        for i in range(200):
            seed = f"{i}:token{i}"
            a, b = sb.generate_config(seed), sb.generate_config(seed)
            self.assertEqual(a, b)
            self.assertEqual(sb.ShopButtonsConfig.from_json(a.to_json()), a)

    def test_labels_come_from_declared_lists(self):
        for i in range(500):
            c = sb.generate_config(f"s{i}")
            self.assertIn(sb.SHOP_TEXTS[c.shop_text], c.shop_label)
            self.assertIn(sb.GALLERY_TEXTS[c.gallery_text], c.gallery_label)
            l, r = sb.SHOP_EMOJI[c.shop_emoji]
            self.assertTrue(c.shop_label.startswith(l) and c.shop_label.endswith(r))
            l, r = sb.GALLERY_EMOJI[c.gallery_emoji]
            self.assertTrue(c.gallery_label.startswith(l) and c.gallery_label.endswith(r))
            self.assertEqual(c.gallery_heading, c.gallery_label)
            self.assertFalse(any(bad in c.shop_label + c.gallery_label for bad in FORBIDDEN))

    def test_text_and_emoji_are_independent(self):
        pairs = {(c.shop_text, c.shop_emoji) for c in (sb.generate_config(f"i{n}") for n in range(600))}
        self.assertEqual(len(pairs), len(sb.SHOP_TEXTS) * len(sb.SHOP_EMOJI))
        pairs = {(c.gallery_text, c.gallery_emoji) for c in (sb.generate_config(f"g{n}") for n in range(600))}
        self.assertEqual(len(pairs), len(sb.GALLERY_TEXTS) * len(sb.GALLERY_EMOJI))

    def test_shop_always_first_and_layouts_occur(self):
        cfgs = [sb.generate_config(f"o{i}") for i in range(200)]
        self.assertEqual({c.order for c in cfgs}, {"shop_first"})
        self.assertEqual({c.layout for c in cfgs}, set(sb.LAYOUTS))

    def test_callback_data_unique_between_bots_and_valid(self):
        seen = set()
        for i in range(1000):
            c = sb.generate_config(f"bot{i}")
            for cb in (c.shop_cb, c.gallery_cb):
                self.assertLessEqual(len(cb.encode()), 64)
                self.assertNotIn(cb, seen)
                seen.add(cb)

    def test_taken_signatures_are_avoided(self):
        first = sb.generate_config("same")
        second = sb.generate_config("same", {first.signature()})
        self.assertNotEqual(first.signature(), second.signature())

    def test_rows_and_order(self):
        for i in range(100):
            c = sb.generate_config(f"r{i}")
            rows = sb.layout_rows(c)
            flat = [b for row in rows for b in row]
            self.assertEqual(len(flat), 2)
            self.assertEqual(len(rows), 1 if c.layout == "row" else 2)
            first_cb = flat[0][1]
            self.assertEqual(first_cb, c.shop_cb if c.order == "shop_first" else c.gallery_cb)

    def test_parse_action_only_own_callbacks(self):
        a, b = sb.generate_config("a"), sb.generate_config("b")
        self.assertEqual(sb.parse_action(a, a.shop_cb), "shop")
        self.assertEqual(sb.parse_action(a, a.gallery_cb), "gallery")
        self.assertIsNone(sb.parse_action(a, b.shop_cb))
        self.assertIsNone(sb.parse_action(a, None))

    def test_stored_config_is_not_affected_by_list_changes(self):
        c = sb.generate_config("keep")
        raw = c.to_json()
        old = sb.SHOP_TEXTS
        try:
            sb.SHOP_TEXTS = ("Другой",) + old
            self.assertEqual(sb.ShopButtonsConfig.from_json(raw).shop_label, c.shop_label)
        finally:
            sb.SHOP_TEXTS = old

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


class ButtonColorTests(unittest.TestCase):
    def test_greeting_buttons_are_all_blue(self):
        for i in range(100):
            c = sb.generate_config(f"col{i}")
            with _FakeAiogramTypes():
                kb = sb.build_keyboard(c)
            buttons = [b for row in kb.inline_keyboard for b in row]
            self.assertEqual(len(buttons), 2)
            self.assertTrue(all(b.style == "primary" for b in buttons))


if __name__ == "__main__":
    unittest.main()


class LegacyMigrationTests(unittest.TestCase):
    def test_old_stored_config_is_normalized(self):
        import json
        c = sb.generate_config("legacy")
        d = json.loads(c.to_json())
        d.update(shop_accent="left", gallery_accent="right", order="gallery_first",
                 shop_label="Магазин", gallery_label="Галерея")
        n = sb.ShopButtonsConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(n.order, "shop_first")
        l, r = sb.SHOP_EMOJI[n.shop_emoji]
        self.assertTrue(n.shop_label.startswith(l) and n.shop_label.endswith(r))
        l, r = sb.GALLERY_EMOJI[n.gallery_emoji]
        self.assertTrue(n.gallery_label.startswith(l) and n.gallery_label.endswith(r))

    def test_assortment_renamed(self):
        import json
        c = sb.generate_config("legacy2")
        d = json.loads(c.to_json())
        d.update(shop_accent="both", shop_label="🛒 Ассортимент 🏬")
        n = sb.ShopButtonsConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(n.shop_label, "🍫 Наш ассортимент 💫")  # переименование сохраняется, эмодзи — статичные

    def test_old_random_emoji_become_static(self):
        import json
        c = sb.generate_config("legacy3")
        d = json.loads(c.to_json())
        d.update(shop_label="📦 Каталог 🛍️", gallery_label="🖼️ Видео-рулетка 🩷", shop_emoji=2, gallery_emoji=0)
        n = sb.ShopButtonsConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(n.shop_label, "🍫 Каталог 💫")
        self.assertEqual(n.gallery_label, "🎥 Видео-рулетка 💖")
        self.assertEqual(n.gallery_heading, n.gallery_label)
        self.assertEqual(sb.ShopButtonsConfig.from_json(n.to_json()), n)


class StaticEmojiTests(unittest.TestCase):
    def test_emoji_are_the_same_for_every_bot(self):
        for i in range(300):
            c = sb.generate_config(f"e{i}")
            self.assertTrue(c.shop_label.startswith("🍫 ") and c.shop_label.endswith(" 💫"))
            self.assertTrue(c.gallery_label.startswith("🎥 ") and c.gallery_label.endswith(" 💖"))
            self.assertEqual((c.shop_emoji, c.gallery_emoji), (0, 0))

    def test_texts_and_callbacks_by_seed_are_unchanged_by_the_emoji_switch(self):
        # значения сняты ДО перехода на статичные эмодзи
        c = sb.generate_config("seed0")
        self.assertEqual((c.shop_text, c.gallery_text, c.layout), (0, 1, "column"))
        self.assertEqual((c.shop_cb, c.gallery_cb), ("sh:f38c245d7f", "gl:0087e0a48a"))
        c = sb.generate_config("seed2")
        self.assertEqual((c.shop_text, c.gallery_text, c.layout), (2, 0, "column"))
        self.assertNotIn("Ассортимент", sb.SHOP_TEXTS)
