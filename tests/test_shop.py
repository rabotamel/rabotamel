import html
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import captcha, gallery, moderation, shop, shop_buttons, texts  # noqa: E402

FORBIDDEN = ("---", "***", "———", "═", "─", "│")


class ShopConfigTests(unittest.TestCase):
    def test_deterministic_and_roundtrip(self):
        for i in range(200):
            seed = f"{i}:token{i}"
            a, b = shop.generate_config(seed), shop.generate_config(seed)
            self.assertEqual(a, b)
            self.assertEqual(shop.ShopConfig.from_json(a.to_json()), a)

    def test_description_built_from_declared_variants(self):
        for i in range(300):
            c = shop.generate_config(f"d{i}")
            plain = html.unescape(c.about)
            for words, idx in ((shop.INTROS, c.intro), (shop.COMPOSITION_TITLES, c.title), (shop.ITEMS_1, c.item1),
                               (shop.ITEMS_2, c.item2), (shop.ITEMS_3, c.item3), (shop.OUTROS, c.outro)):
                self.assertIn(words[idx], plain)
            self.assertFalse(any(bad in c.about for bad in FORBIDDEN))

    def test_all_variants_occur_and_places_are_independent(self):
        cfgs = [shop.generate_config(f"v{i}") for i in range(2000)]
        self.assertEqual({c.intro for c in cfgs}, {0, 1})
        self.assertEqual({c.title for c in cfgs}, {0, 1})
        self.assertEqual({(c.item1, c.item2, c.item3) for c in cfgs}, {(a, b, d) for a in (0, 1) for b in (0, 1) for d in (0, 1)})
        self.assertEqual({c.outro for c in cfgs}, {0, 1})
        self.assertEqual(len({c.about for c in cfgs}), 64)

    def test_taken_signatures_are_avoided_until_exhausted(self):
        first = shop.generate_config("same")
        second = shop.generate_config("same", {first.signature()})
        self.assertNotEqual(first.signature(), second.signature())
        # все 64 комбинации заняты — повтор допустим, ошибок нет
        every = {shop.generate_config(f"x{i}").signature() for i in range(3000)}
        self.assertEqual(len(every), 64)
        shop.generate_config("same", every)

    def test_callback_data_unique_valid_and_not_clashing_with_other_modules(self):
        seen = set()
        for i in range(1000):
            c = shop.generate_config(f"bot{i}")
            for cb in (c.product_cb, c.back_cb, c.buy_cb, c.product_back_cb):
                self.assertLessEqual(len(cb.encode()), 64)
                self.assertTrue(cb.isascii())
                self.assertNotIn(cb, seen)
                seen.add(cb)
        others = (shop_buttons.SHOP_PREFIX, shop_buttons.GALLERY_PREFIX, gallery.BUY_PREFIX, gallery.PREMIUM_PREFIX,
                  gallery.BACK_PREFIX, gallery.SUGGEST_PREFIX, gallery.SUGGEST_BACK_PREFIX,
                  captcha.CALLBACK_PREFIX, moderation.PREFIX)
        for mine in shop.PREFIXES:
            self.assertFalse(any(o.startswith(mine) or mine.startswith(o) for o in others), mine)

    def test_registered_in_texts_registry(self):
        kind = next(k for k in texts.KINDS if k.key == "shop_menu")
        cfg = kind.generate("t", set())
        self.assertEqual(kind.from_json(cfg.to_json()), cfg)
        raw = {k.key: k.generate("t", set()).to_json() for k in texts.KINDS}
        self.assertEqual(texts.BotTexts(raw).shop_menu, cfg)

    def test_broken_json_is_rejected(self):
        d = shop.generate_config("t")
        for bad in (d.to_json().replace("sp:", "zz:"), d.to_json().replace(d.about.split("\n")[0].split(" — ")[0][3:], "Другое")):
            with self.assertRaises(Exception):
                shop.ShopConfig.from_json(bad)


P = shop.Product(id=5, name="Абсолют | MAXIMUM POWER 🚀", price=6789, about="**Жирный** текст", color="danger")


class ShopRenderTests(unittest.TestCase):
    def test_menu_text_exact_and_heading_is_bold(self):
        self.assertEqual(
            shop.render_menu("🍫 Магазин 💫", 150),
            "<b>🍫 Магазин 💫</b>\n\n"
            "🌸 Чтобы ознакомиться с товаром, выберите необходимый, нажав на соответствующую кнопку 🌸\n\n"
            "🍭 Ваш баланс: 150🍬",
        )

    def test_menu_heading_is_escaped_and_follows_shop_button(self):
        self.assertIn("<b>A &lt;b&gt; &amp; B</b>", shop.render_menu("A <b> & B", 0))
        for i in range(100):
            sb = shop_buttons.generate_config(f"h{i}")
            self.assertEqual(sb.shop_heading, sb.shop_label)
            self.assertTrue(shop.render_menu(sb.shop_heading, 1).startswith(f"<b>{sb.shop_label}</b>\n\n🌸"))

    def test_product_card_structure(self):
        lines = shop.render_product(P).split("\n")
        self.assertEqual(lines[0], "<b>📦 Товар:</b> Абсолют | MAXIMUM POWER 🚀")
        self.assertEqual(lines[1], "🍭 <b>Цена:</b> 6789🍬")
        self.assertEqual(lines[2:5], ["", "<b>ℹ️ О товаре:</b>", ""])
        self.assertEqual(lines[5], "<b>Жирный</b> текст")
        self.assertNotIn("— 6789", lines[0])          # название без суффикса цены
        self.assertNotIn("**", "\n".join(lines))

    def test_product_card_without_description_has_no_trailing_blank_lines(self):
        text = shop.render_product(shop.Product(id=1, name="X", price=1))
        self.assertTrue(text.endswith("<b>ℹ️ О товаре:</b>"))

    def test_name_is_escaped(self):
        text = shop.render_product(shop.Product(id=1, name="A <b> & B", price=2))
        self.assertIn("<b>📦 Товар:</b> A &lt;b&gt; &amp; B", text)

    def test_labels(self):
        self.assertEqual(shop.product_label(P), "Абсолют | MAXIMUM POWER 🚀 — 6789🍬")
        self.assertEqual(shop.buy_label(P), "🍭 Купить (6789🍬)")

    def test_product_from_row_tolerates_garbage(self):
        row = types.SimpleNamespace(id=2, name="N", price=10, about=None, color="purple")
        self.assertEqual(shop.product_from_row(row), shop.Product(id=2, name="N", price=10, about="", color=""))


class FormatAboutTests(unittest.TestCase):
    def test_markers_become_html(self):
        f = shop.format_about
        self.assertEqual(f("**жирный**"), "<b>жирный</b>")
        self.assertEqual(f("__подчёркнутый__"), "<u>подчёркнутый</u>")
        self.assertEqual(f("*курсив*"), "<i>курсив</i>")
        self.assertEqual(f("~~зачёркнутый~~"), "<s>зачёркнутый</s>")
        self.assertEqual(f("||спойлер||"), "<tg-spoiler>спойлер</tg-spoiler>")
        self.assertEqual(f("`код`"), "<code>код</code>")
        self.assertEqual(f("[сайт](https://x.ru/?a=1&b=2)"), '<a href="https://x.ru/?a=1&amp;b=2">сайт</a>')
        self.assertEqual(f("# Заголовок\nтекст"), "<b>Заголовок</b>\nтекст")

    def test_nesting_and_multiline(self):
        self.assertEqual(shop.format_about("**a *b* c**\n\n__d__"), "<b>a <i>b</i> c</b>\n\n<u>d</u>")
        self.assertEqual(shop.format_about("**a\nb**"), "<b>a\nb</b>")

    def test_html_is_escaped_so_markup_cannot_break_or_inject(self):
        out = shop.format_about("<b>x</b> <script>alert(1)</script> a & b")
        self.assertEqual(out, "&lt;b&gt;x&lt;/b&gt; &lt;script&gt;alert(1)&lt;/script&gt; a &amp; b")
        self.assertEqual(shop.format_about("**<i>**"), "<b>&lt;i&gt;</b>")

    def test_code_span_is_not_formatted_inside(self):
        self.assertEqual(shop.format_about("`**не жирный**`"), "<code>**не жирный**</code>")

    def test_non_link_schemes_stay_text(self):
        out = shop.format_about("[x](javascript:alert(1))")
        self.assertNotIn("<a", out)

    def test_unpaired_and_ordinary_symbols_stay_untouched(self):
        for raw in ("**не закрыт", "2*3*4", "snake_case_name", "a * b * c", "цена: 5 ** 2", "50% | 30%"):
            self.assertEqual(shop.format_about(raw), html.escape(raw, quote=False), raw)

    def test_crossed_tags_fall_back_to_plain_text(self):
        self.assertEqual(shop.format_about("**a *b** c*"), "**a *b** c*")

    def test_always_balanced_html_on_random_input(self):
        import random
        rng = random.Random(1)
        alphabet = ["**", "__", "*", "~~", "||", "`", "[", "](", "https://a.b", ")", "a", " ", "\n", "<", "&", "#", "_"]
        for _ in range(3000):
            raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 14)))
            self.assertTrue(shop._balanced(shop.format_about(raw)), raw)

    def test_empty(self):
        self.assertEqual(shop.format_about(""), "")
        self.assertEqual(shop.format_about(None), "")
        self.assertEqual(shop.format_about("  \n "), "")


class VariantsTests(unittest.TestCase):
    R = staticmethod(shop.resolve_variants)

    def test_picks_one_of_the_options_and_trims(self):
        for i in range(50):
            out = self.R("Привет [ товарищ / друг ]!", f"s{i}", 1)
            self.assertIn(out, ("Привет товарищ!", "Привет друг!"))

    def test_deterministic_per_bot_and_differs_between_bots(self):
        text = "[a/b] [c/d] [e/f] [g/h] [i/j] [k/l] [m/n] [o/p]"
        self.assertEqual(self.R(text, "bot-1", 7), self.R(text, "bot-1", 7))
        outs = {self.R(text, f"bot-{i}", 7) for i in range(40)}
        self.assertGreater(len(outs), 20)

    def test_all_options_occur_across_bots(self):
        self.assertEqual({self.R("[x/y/z]", f"b{i}") for i in range(200)}, {"x", "y", "z"})

    def test_same_slots_choose_independently_and_adding_slot_keeps_others(self):
        seen = {self.R("[a/b] [a/b]", f"b{i}") for i in range(100)}
        self.assertEqual(seen, {"a a", "a b", "b a", "b b"})          # одинаковые слоты не обязаны совпадать
        for i in range(100):
            seed = f"b{i}"
            first = self.R("[a/b] и [c/d]", seed, 3).split(" и ")
            second = self.R("[e/f] [a/b] и [c/d]", seed, 3).split(" ")
            self.assertEqual(second[1:], [first[0], "и", first[1]])    # прежние слоты не поменялись

    def test_product_id_changes_choices(self):
        text = "[a/b] [c/d] [e/f] [g/h] [i/j] [k/l]"
        self.assertGreater(len({self.R(text, "bot", pid) for pid in range(1, 30)}), 5)

    def test_nesting_and_multiline(self):
        for i in range(60):
            out = self.R("[а/б [в/г]]\nстрока [д/е]", f"n{i}")
            first, second = out.split("\n")
            self.assertIn(first, ("а", "б в", "б г"))
            self.assertIn(second, ("строка д", "строка е"))

    def test_left_untouched(self):
        for raw in ("[нет слэша]", "[не закрыт/ нет", "лишняя ] скобка", "a/b", "[]", "[x] [y]", "[https://a.b/c]",
                    "[текст/вариант](https://a.b/c)", "`[a/b]`", "[[a]]", ""):
            self.assertEqual(self.R(raw, "s", 1), raw, raw)

    def test_unclosed_outer_does_not_break_inner(self):
        out = self.R("[не закрыт [a/b]", "s")
        self.assertIn(out, ("[не закрыт a", "[не закрыт b"))

    def test_empty_option_allowed(self):
        self.assertEqual({self.R("очень [ красиво/]", f"e{i}") for i in range(100)}, {"очень красиво", "очень "})

    def test_works_with_formatting_and_escaping(self):
        outs = {shop.format_about("**[Жирный/Крепкий]** <b> [a&b/a&b] [нет]", f"f{i}", 2) for i in range(60)}
        self.assertEqual(outs, {"<b>Жирный</b> &lt;b&gt; a&amp;b [нет]", "<b>Крепкий</b> &lt;b&gt; a&amp;b [нет]"})
        # вариант с маркерами оформления форматируется после выбора
        self.assertIn(shop.format_about("[**а**/**а**]", "s"), ("<b>а</b>",))
        # ссылка с «/» в тексте остаётся ссылкой
        self.assertEqual(shop.format_about("[a/b](https://x.ru)", "s"), '<a href="https://x.ru">a/b</a>')
        # без seed варианты не разворачиваются
        self.assertEqual(shop.format_about("[a/b]"), "[a/b]")

    def test_html_stays_balanced_on_random_input(self):
        import random
        rng = random.Random(3)
        alphabet = ["[", "]", "/", "**", "__", "*", "a", " ", "(https://a.b)", "`", "~~", "||", "<", "&"]
        for _ in range(3000):
            raw = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
            self.assertTrue(shop._balanced(shop.format_about(raw, "seed", 1)), raw)

    def test_render_product_uses_bot_seed_and_is_stable(self):
        p = shop.Product(id=4, name="N", price=1, about="Привет [товарищ/друг]! Как [твои дела/поживаешь]?")
        cards = {shop.render_product(p, f"tok{i}") for i in range(60)}
        self.assertEqual(len(cards), 4)
        for i in range(10):
            self.assertEqual(shop.render_product(p, "tok1"), shop.render_product(p, "tok1"))
            self.assertNotIn("[", shop.render_product(p, f"tok{i}"))


class ShopInputTests(unittest.TestCase):
    def test_color(self):
        self.assertEqual([shop.normalize_color(c) for c in ("", None, "Красный", "green", "success", "синий", "default")],
                         ["", "", "danger", "success", "success", "primary", ""])
        with self.assertRaises(ValueError):
            shop.normalize_color("purple")

    def test_price(self):
        self.assertEqual((shop.normalize_price("6789"), shop.normalize_price(5), shop.normalize_price(" 1 000 ")), (6789, 5, 1000))
        for bad in ("", "0", "-5", "1.5", "abc", 10 ** 10, None):
            with self.assertRaises(ValueError):
                shop.normalize_price(bad)

    def test_name_and_about(self):
        self.assertEqual(shop.normalize_name("  Два   слова "), "Два слова")
        for bad in ("", "   ", "x" * 65):
            with self.assertRaises(ValueError):
                shop.normalize_name(bad)
        self.assertEqual(shop.normalize_about("  **a**\r\nb \n"), "**a**\nb")
        with self.assertRaises(ValueError):
            shop.normalize_about("x" * 3901)


class ShopKeyboardTests(unittest.TestCase):
    def test_menu_rows_order_and_back_last(self):
        c = shop.generate_config("k")
        ps = [shop.Product(1, "A", 10, color="success"), shop.Product(2, "B", 20), shop.Product(3, "C", 30, color="danger")]
        rows = shop.menu_rows(c, ps)
        self.assertEqual(rows, [[("A — 10🍬", f"{c.product_cb}:1")], [("B — 20🍬", f"{c.product_cb}:2")],
                                [("C — 30🍬", f"{c.product_cb}:3")], [("⬅️ Назад", c.back_cb)]])
        self.assertEqual(shop.menu_styles(ps), ("success", "", "danger", "danger"))

    def test_menu_without_products_is_only_back(self):
        c = shop.generate_config("k")
        self.assertEqual(shop.menu_rows(c, []), [[("⬅️ Назад", c.back_cb)]])
        self.assertEqual(shop.menu_styles([]), ("danger",))

    def test_callback_data_fits_telegram_limit_even_for_huge_ids(self):
        c = shop.generate_config("k")
        big = shop.Product(2 ** 62, "A", 1)
        for _, cb in shop.menu_rows(c, [big])[0] + shop.product_rows(c, big)[0]:
            self.assertLessEqual(len(cb.encode()), 64)

    def test_product_rows_and_styles(self):
        c = shop.generate_config("k")
        self.assertEqual(shop.product_rows(c, P), [[("🍭 Купить (6789🍬)", f"{c.buy_cb}:5")], [("⬅️ Назад", c.product_back_cb)]])
        self.assertEqual(shop.PRODUCT_STYLES, ("success", "danger"))

    def test_parse_callback_only_for_this_bot(self):
        a, b = shop.generate_config("bot-a"), shop.generate_config("bot-b")
        self.assertEqual(shop.parse_callback(a, f"{a.product_cb}:12"), ("product", 12))
        self.assertEqual(shop.parse_callback(a, f"{a.buy_cb}:12"), ("buy", 12))
        self.assertEqual(shop.parse_callback(a, a.product_cb), ("product", None))     # кнопка старого сообщения без id
        self.assertEqual(shop.parse_callback(a, a.back_cb), ("back", None))
        self.assertEqual(shop.parse_callback(a, a.product_back_cb), ("product_back", None))
        self.assertEqual(shop.parse_action(a, f"{a.product_cb}:12"), "product")
        self.assertEqual(shop.parse_action(a, a.buy_cb), "buy")
        for cb in (f"{b.product_cb}:1", b.back_cb, b.buy_cb, b.product_back_cb, "", None, "sp:foreign",
                   f"{a.product_cb}:", f"{a.product_cb}:x1", f"{a.product_cb}:-1", f"{a.back_cb}:1", f"{a.product_cb}:{'9' * 30}"):
            self.assertIsNone(shop.parse_callback(a, cb), cb)

    def test_color_is_passed_to_buttons_only_when_set(self):
        # без aiogram проверяем через подмену типов кнопок
        import types as _t
        created = []

        class Btn:
            def __init__(self, **kw):
                created.append(kw)

        fake = _t.ModuleType("aiogram.types")
        fake.InlineKeyboardButton, fake.InlineKeyboardMarkup = Btn, lambda **kw: kw
        saved = {k: sys.modules.get(k) for k in ("aiogram", "aiogram.types")}
        sys.modules.setdefault("aiogram", _t.ModuleType("aiogram"))
        sys.modules["aiogram.types"] = fake
        try:
            c = shop.generate_config("k")
            shop.build_menu_keyboard(c, [shop.Product(1, "A", 1, color="success"), shop.Product(2, "B", 1)])
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.assertEqual([k.get("style") for k in created], ["success", None, "danger"])
        self.assertNotIn("style", created[1])


if __name__ == "__main__":
    unittest.main()
