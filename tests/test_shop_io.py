import os
import random
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import shop, shop_io  # noqa: E402


def row(id, name, price, about="", color="", visible=True):
    return types.SimpleNamespace(id=id, name=name, price=price, about=about, color=color, is_visible=visible)


TRICKY = (
    "**Жирный** [товарищ/друг] 🚀\n\n"
    "=== END PRODUCT ===\n"
    "about-lines: 99\n"
    "=== PRODUCT ===\n"
    "name: подделка\n"
    "  отступ в начале строки\n"
    "последняя строка с `[a/b]` и ~~зачёркнутым~~"
)

ROWS = [
    row(7, "Абсолют | MAXIMUM POWER 🚀", 6789, TRICKY, "danger"),
    row(3, "Без описания: name: x", 50, "", ""),
    row(12, "Скрытый", 1, "Привет [а/б]!", "success", visible=False),
    row(4, "Синий", 1000000000, "# Заголовок\n*курсив* и __подчёркнутый__", "primary"),
]


def to_rows(items):
    return [row(it.id, it.name, it.price, it.about, it.color, it.visible) for it in items]


class ExportParseTests(unittest.TestCase):
    def test_roundtrip_keeps_everything_and_order(self):
        items = shop_io.parse_products(shop_io.export_products(ROWS))
        self.assertEqual([(i.id, i.name, i.price, i.about, i.color, i.visible) for i in items],
                         [(r.id, r.name, r.price, r.about, r.color, r.is_visible) for r in ROWS])

    def test_export_is_stable_and_idempotent(self):
        text = shop_io.export_products(ROWS)
        self.assertEqual(shop_io.export_products(to_rows(shop_io.parse_products(text))), text)
        self.assertTrue(text.startswith("TG-BOT-ROTATOR SHOP v1\nproducts: 4\n"))

    def test_empty_catalog_roundtrip(self):
        text = shop_io.export_products([])
        self.assertEqual(shop_io.parse_products(text), [])

    def test_bot_view_is_identical_after_restore(self):
        restored = to_rows(shop_io.parse_products(shop_io.export_products(ROWS)))
        cfg = shop.generate_config("tok")
        for seed in ("tok1", "tok2", "tok3"):
            for a, b in zip(ROWS, restored):
                pa, pb = shop.product_from_row(a), shop.product_from_row(b)
                self.assertEqual(shop.render_product(pa, seed), shop.render_product(pb, seed))   # текст, форматирование и варианты
        visible_a = [shop.product_from_row(r) for r in ROWS if r.is_visible]
        visible_b = [shop.product_from_row(r) for r in restored if r.is_visible]
        self.assertEqual(shop.menu_rows(cfg, visible_a), shop.menu_rows(cfg, visible_b))        # кнопки, id, порядок
        self.assertEqual(shop.menu_styles(visible_a), shop.menu_styles(visible_b))             # цвета

    def test_random_descriptions_roundtrip(self):
        rng = random.Random(5)
        alphabet = ["a", " ", "\n", "\n\n", "**", "[x/y]", "🚀", "=== END PRODUCT ===", "about-lines: 2", ":", "<b>", "&", "\t", "я"]
        for n in range(300):
            about = shop.normalize_about("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 25))))
            text = shop_io.export_products([row(1, "N", 5, about, "danger"), row(2, "M", 6, about)])
            items = shop_io.parse_products(text)
            self.assertEqual([i.about for i in items], [about, about], repr(about))

    def test_tolerates_crlf_bom_and_blank_lines_between_blocks(self):
        text = "\ufeff" + shop_io.export_products(ROWS).replace("\n", "\r\n") + "\r\n\r\n"
        items = shop_io.parse_products(text)
        self.assertEqual([i.about for i in items], [r.about for r in ROWS])

    def test_hand_written_file_without_ids_and_optional_fields(self):
        text = ("TG-BOT-ROTATOR SHOP v1\n\n=== PRODUCT ===\nname: A\nprice: 10\nabout-lines: 1\nтекст\n=== END PRODUCT ===\n"
                "=== PRODUCT ===\nname: B\nprice: 20\ncolor: Красный\nvisible: no\nabout-lines: 0\n=== END PRODUCT ===\n")
        a, b = shop_io.parse_products(text)
        self.assertEqual((a.id, a.name, a.price, a.about, a.color, a.visible), (None, "A", 10, "текст", "", True))
        self.assertEqual((b.id, b.color, b.visible, b.about), (None, "danger", False, ""))

    def test_corrupted_files_are_rejected_with_clear_message(self):
        good = shop_io.export_products(ROWS)
        bad = {
            "empty": "", "spaces": "  \n ", "no header": "name: A\nprice: 1\n",
            "truncated": good[: good.index("Скрытый")],
            "count mismatch": good.replace("products: 4", "products: 5"),
            "bad lines count": good.replace("about-lines: 0", "about-lines: 3"),
            "negative lines": good.replace("about-lines: 0", "about-lines: -1"),
            "bad price": good.replace("price: 50", "price: много"),
            "zero price": good.replace("price: 50", "price: 0"),
            "bad color": good.replace("color: red", "color: purple"),
            "bad visible": good.replace("visible: no", "visible: maybe"),
            "unknown field": good.replace("price: 50", "cost: 50"),
            "dup field": good.replace("price: 50", "price: 50\nprice: 51"),
            "dup id": good.replace("id: 3", "id: 7"),
            "bad id": good.replace("id: 3", "id: abc"),
            "garbage between": good.replace("=== PRODUCT ===\nid: 3", "мусор\n=== PRODUCT ===\nid: 3"),
            "no name": good.replace("name: Без описания: name: x\n", ""),
            "long name": good.replace("name: Скрытый", "name: " + "я" * 65),
            "binary": "\x00\x01\x02",
        }
        for label, text in bad.items():
            with self.assertRaises(ValueError, msg=label) as cm:
                shop_io.parse_products(text)
            self.assertTrue(str(cm.exception).strip(), label)

    def test_limits(self):
        many = [row(i, "N", 1) for i in range(1, shop_io.MAX_ITEMS + 2)]
        with self.assertRaises(ValueError):
            shop_io.parse_products(shop_io.export_products(many))
        with self.assertRaises(ValueError):
            shop_io.parse_products("TG-BOT-ROTATOR SHOP v1\n" + "x" * (shop_io.MAX_FILE_CHARS + 1))

    def test_random_damage_never_crashes_with_other_errors(self):
        good = shop_io.export_products(ROWS)
        rng = random.Random(9)
        for _ in range(500):
            cut = rng.randrange(len(good))
            damaged = good[:cut] + good[cut + rng.randint(0, 20):]
            try:
                shop_io.parse_products(damaged)
            except ValueError:
                pass


def trow(id, button, reward, about, shot=False, once=True, visible=True, comment=""):
    return types.SimpleNamespace(id=id, button_text=button, reward=reward, about=about, require_screenshot=shot,
                                 one_time=once, is_visible=visible, comment=comment)


TASK_ROWS = [
    trow(5, "Подписаться: на канал 🚀", 10, "**Подпишитесь** [сюда](https://t.me/x)\n\n=== END TASK ===\nabout-lines: 9\nпоследняя", True, True,
         True, "Проверить подписку\n=== TASK ==="),
    trow(2, "Ежедневное", 1000000, "Привет [а/б]!", False, False, False, ""),
]


class ExportAllTests(unittest.TestCase):
    def test_roundtrip_products_and_tasks(self):
        text = shop_io.export_all(ROWS, TASK_ROWS)
        self.assertTrue(text.startswith("TG-BOT-ROTATOR BUTTONS v2\nproducts: 4\ntasks: 2\n"))
        p = shop_io.parse_all(text)
        self.assertEqual(to_rows(p.products)[0].name, ROWS[0].name)
        self.assertEqual(len(p.products), 4)
        self.assertEqual([(t.id, t.button_text, t.reward, t.about, t.screenshot, t.one_time, t.visible, t.comment) for t in p.tasks],
                         [(r.id, r.button_text, r.reward, r.about, r.require_screenshot, r.one_time, r.is_visible, r.comment)
                          for r in TASK_ROWS])
        again = shop_io.export_all(to_rows(p.products), [trow(t.id, t.button_text, t.reward, t.about, t.screenshot, t.one_time,
                                                              t.visible, t.comment) for t in p.tasks])
        self.assertEqual(again, text)

    def test_empty_and_old_v1_files(self):
        p = shop_io.parse_all(shop_io.export_all([], []))
        self.assertEqual((p.products, p.tasks), ([], []))
        old = shop_io.parse_all(shop_io.export_products(ROWS))
        self.assertEqual(len(old.products), 4)
        self.assertIsNone(old.tasks)          # старый файл: задания не трогаем

    def test_hand_written_task_and_defaults(self):
        text = ("TG-BOT-ROTATOR BUTTONS v2\nproducts: 0\ntasks: 1\n\n=== TASK ===\nbutton: Тест\nreward: 5\nabout-lines: 1\n"
                "comment-lines: 0\nОписание\n=== END TASK ===\n")
        t = shop_io.parse_all(text).tasks[0]
        self.assertEqual((t.id, t.screenshot, t.one_time, t.visible, t.comment), (None, False, True, True, ""))

    def test_broken_task_files_rejected(self):
        good = shop_io.export_all(ROWS[:1], TASK_ROWS)
        bad = [
            good.replace("tasks: 2", "tasks: 3"),
            good.replace("reward: 10", "reward: 0"),
            good.replace("reward: 10", "reward: abc"),
            good.replace("id: 2\nbutton", "id: 5\nbutton"),           # повтор id
            good.replace("screenshot: yes", "screenshot: maybe"),
            good.replace("about-lines: 5", "about-lines: 50"),
            good[: good.index("=== TASK ===") + 40],                    # обрезан
            "TG-BOT-ROTATOR BUTTONS v2\nproducts: 0\n",              # нет ни строки tasks, ни заданий — не стираем задания молча
        ]
        for text in bad:
            with self.assertRaises(ValueError, msg=text[:80]):
                shop_io.parse_all(text)
        with self.assertRaises(ValueError):
            shop_io.parse_all(good.replace("about-lines: 5", "about-lines: 4"))

    def test_task_block_not_allowed_in_v1(self):
        with self.assertRaises(ValueError):
            shop_io.parse_products(shop_io.export_all([], TASK_ROWS))


if __name__ == "__main__":
    unittest.main()
