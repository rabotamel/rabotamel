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
        # тексты кнопок по-прежнему выбираются по seed, эмодзи статичны (индекс всегда 0)
        self.assertEqual({(c.buy_text, c.buy_emoji) for c in cfgs}, {(a, 0) for a in range(len(gl.BUY_TEXTS))})
        self.assertEqual({(c.premium_text, c.premium_emoji) for c in cfgs}, {(a, 0) for a in range(len(gl.PREMIUM_TEXTS))})
        self.assertEqual({c.desc for c in cfgs}, set(range(len(gl.DESCRIPTIONS))))
        self.assertEqual({deco.generate_config(f"d{i}").left for i in range(300)}, {"💫"})

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

class StaticEmojiTests(unittest.TestCase):
    """Эмодзи галереи статичны: у всех ботов одни и те же, по seed не подбираются."""

    def test_gallery_emoji_are_fixed_for_every_bot(self):
        for i in range(300):
            c = gl.generate_config(f"st{i}")
            self.assertTrue(c.buy_label.startswith("🎥 ") and c.buy_label.endswith(" 💖"))
            self.assertTrue(c.premium_label.startswith("💫 ") and c.premium_label.endswith(" 💖"))
            self.assertEqual(gl.layout_rows(c)[2][0][0], "📩 Предложка 🤍")
            self.assertEqual(gl.layout_rows(c)[3][0][0], "⬅️ Назад")

    def test_menu_matches_the_spec_for_every_bot(self):
        for i in range(300):
            s, g = sb.generate_config(f"m{i}"), greeting.generate_config(f"m{i}")
            out = gl.render(gl.generate_config(f"m{i}"), deco.generate_config(f"m{i}"), s.gallery_heading,
                            g.template, 7, "Предложка")
            title, desc, hint, balance = out.split("\n\n")
            self.assertEqual(title, f"<b>🎥 {s.gallery_title} 💖</b>")            # заголовок: 🎥 … 💖
            self.assertTrue(desc.startswith("💫 ") and desc.endswith(" 💫"))      # описание: 💫 … 💫
            self.assertTrue(hint.startswith("🤍 ") and hint.endswith(" 🍬"))      # подсказка через название предложки
            self.assertIn("«Предложка»", hint)
            self.assertTrue(balance.startswith("🍭 ") and balance.endswith("7🍬"))  # баланс: 🍫 … 🍬

    def test_hint_templates_carry_no_emoji_and_stay_varied(self):
        for h in gl.HINTS:
            self.assertEqual(gl._strip_lead(h), h)
        self.assertEqual({gl.generate_config(f"h{i}").hint for i in range(300)}, set(gl.HINTS))

    def test_texts_by_seed_are_unchanged_by_the_emoji_switch(self):
        # значения сняты ДО перехода на статичные эмодзи: выбор текстов и callback_data по seed не сдвинулся
        c = gl.generate_config("seed0")
        self.assertEqual((c.desc, c.buy_text, c.premium_text), (3, 2, 1))
        self.assertEqual((c.buy_cb, c.premium_cb, c.back_cb), ("gb:43bb275893", "gp:b45dca5bc8", "gk:592ce89bcf"))
        self.assertEqual(c.hint, "Заработать конфеты можно через «{proposal_title}»")
        c = gl.generate_config("seed2")
        self.assertEqual((c.desc, c.buy_text, c.premium_text), (1, 3, 0))
        self.assertEqual(c.buy_cb, "gb:89bb13f879")

    def test_saved_config_with_old_random_emoji_is_normalized(self):
        import json
        d = json.loads(gl.generate_config("old2").to_json())
        d.update(buy_label="🎬 Смотреть видео (3🍬) ✨", premium_label="💎 VIP-подписка 👑",
                 hint="✨ Нажмите «{proposal_title}», чтобы заработать конфеты", buy_emoji=1, premium_emoji=2)
        n = gl.GalleryConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(n.buy_label, "🎥 Смотреть видео (3🍬) 💖")     # цена 3🍬 в тексте кнопки сохранена
        self.assertEqual(n.premium_label, "💫 VIP-подписка 💖")
        self.assertEqual(n.hint_template, "Нажмите «{proposal_title}», чтобы заработать конфеты")
        self.assertEqual((n.buy_emoji, n.premium_emoji), (0, 0))
        self.assertEqual(gl.GalleryConfig.from_json(n.to_json()), n)   # повторное чтение ничего не меняет

    def test_old_deco_marker_from_db_becomes_static(self):
        import json
        for old in ("🌸", "✨", "🌷", "🪻"):
            n = deco.DecoConfig.from_json(json.dumps({"v": 1, "left": old, "right": old}, ensure_ascii=False))
            self.assertEqual((n.left, n.right), ("💫", "💫"))
            self.assertEqual(n.wrap("текст"), "💫 текст 💫")
        with self.assertRaises(ValueError):
            deco.DecoConfig.from_json(json.dumps({"v": 1, "left": "🍬", "right": "🍬"}, ensure_ascii=False))


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


class PremiumUiTests(unittest.TestCase):
    GT = greeting.generate_config("pu").template

    def test_stable_per_bot_and_varies_between_bots(self):
        uis = set()
        for i in range(100):
            c = gl.generate_config(f"pu{i}")
            self.assertEqual(gl.premium_ui(c), gl.premium_ui(gl.GalleryConfig.from_json(c.to_json())))
            u = gl.premium_ui(c)
            uis.add((u.title, u.info, u.status_head, u.status_none, u.status_temp, u.status_forever, u.back, tuple(u.tariffs.values())))
        self.assertGreater(len(uis), 20)
        self.assertEqual(gl.premium_ui(None).status_none, "У вас нет премиума")

    def test_three_statuses_and_dynamic_left(self):
        from datetime import datetime, timedelta, timezone
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(60):
            c = gl.generate_config(f"ps{i}")
            ui = gl.premium_ui(c)
            none = gl.render_premium(c, self.GT, 5, None, False, now)
            temp = gl.render_premium(c, self.GT, 5, now + timedelta(days=2, hours=3, minutes=4), False, now)
            fv = gl.render_premium(c, self.GT, 5, None, True, now)
            self.assertIn(f"🤷🏻‍♀️ {ui.status_none}", none)
            self.assertIn(f"❤️‍🔥 {ui.status_temp.format(left='2д 3ч 4м')}", temp)
            self.assertIn(f"💎 {ui.status_forever}", fv)
            self.assertNotIn("{left}", temp)
            for t in (none, temp, fv):
                self.assertTrue(t.startswith(f"<b>💫 {ui.title} 💖</b>\n\n🌸 {ui.info} 🌸\n\n<b>🔥 {ui.status_head} 🩷</b>\n"))
                self.assertTrue(t.endswith(gl.balance_line(self.GT, "5")))
            # просроченный временный премиум = «нет премиума»
            self.assertIn(ui.status_none, gl.render_premium(c, self.GT, 5, now - timedelta(minutes=1), False, now))

    def test_buttons_prices_and_callbacks_unchanged(self):
        for i in range(60):
            c = gl.generate_config(f"pb{i}")
            rows = gl.premium_rows(c)
            self.assertEqual([r[0][1] for r in rows], [f"{c.premium_cb}:{k}" for k in ("3", "7", "forever", "b")])
            for (text, _), k in zip((r[0] for r in rows[:3]), gl.PREMIUM_KEYS):
                self.assertIn(f"({gl.PREMIUM_OPTIONS[k]['price']}🍬)", text)
                self.assertTrue(text.endswith(" 👑"))
            self.assertTrue(rows[3][0][0].startswith("⬅️ "))
            for _, cb in (r[0] for r in rows):
                self.assertIsNotNone(gl.parse_premium_key(c, cb))
                self.assertLessEqual(len(cb.encode()), 64)


if __name__ == "__main__":
    unittest.main()
