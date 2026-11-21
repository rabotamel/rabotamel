"""«Бесплатные конфеты»: рандомизация, кнопки, разбор нажатий, проверка ввода с сайта и сценарии в боте."""
import asyncio
import os
import sys
import types
import unittest
import unittest.mock
from datetime import timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import test_handlers  # noqa: E402,F401  (ставит заглушки aiogram и умеет импортировать app.handlers без sqlalchemy)
from app import deco, shop, tasks, texts  # noqa: E402

handlers = test_handlers.handlers
FORBIDDEN = ("---", "***", "———", "═", "─", "│")


def T(id=1, button="Подписаться", about="Подпишитесь на канал", reward=10, shot=False, once=True):
    return tasks.Task(id=id, button_text=button, about=about, reward=reward, require_screenshot=shot, one_time=once)


class ConfigTests(unittest.TestCase):
    def test_deterministic_and_roundtrip(self):
        for i in range(200):
            a, b = tasks.generate_config(f"{i}:tok"), tasks.generate_config(f"{i}:tok")
            self.assertEqual(a, b)
            self.assertEqual(tasks.TasksConfig.from_json(a.to_json()), a)

    def test_registered_in_texts(self):
        self.assertIn("tasks_menu", [k.key for k in texts.KINDS])
        raw = {k.key: k.generate("t", set()).to_json() for k in texts.KINDS}
        self.assertEqual(texts.BotTexts(raw).tasks_menu, tasks.generate_config("t"))

    def test_different_bots_differ_and_taken_is_respected(self):
        sigs = {tasks.generate_config(f"b{i}").signature() for i in range(100)}
        self.assertGreater(len(sigs), 95)
        first = tasks.generate_config("x")
        self.assertNotEqual(tasks.generate_config("x", {first.signature()}).signature(), first.signature())

    def test_callbacks_unique_ascii_short_with_id(self):
        for i in range(200):
            c = tasks.generate_config(f"cb{i}")
            self.assertEqual(len(set(c.cb.values())), len(c.cb))
            for action, cb in c.cb.items():
                self.assertTrue(cb.startswith(tasks.CB_PREFIXES[action]))
                self.assertTrue(cb.isascii())
                self.assertLessEqual(len(f"{cb}:{10**18 - 1}".encode()), 64)

    def test_emoji_static_and_no_separators(self):
        for i in range(100):
            c = tasks.generate_config(f"e{i}")
            self.assertIn(c.text("label"), tasks.FREE_LABELS)            # «💝 Бесплатно 🌸» и близкие, без слов о конфетах
            self.assertEqual(c.heading, c.text("label"))                 # заголовок раздела = подпись кнопки
            self.assertNotIn("конфет", c.text("label").lower())
            self.assertTrue(c.text("daily_label").startswith("🤍 ") and c.text("daily_label").endswith(" 🤍"))
            self.assertTrue(c.text("ref_label").startswith("💫 ") and c.text("ref_label").endswith(" 💫"))
            self.assertFalse(any(bad in v for v in c.t.values() for bad in FORBIDDEN))

    def test_section_button_label_variants(self):
        self.assertEqual(len(tasks.FREE_LABELS), 8)
        for lab in tasks.FREE_LABELS:
            low = lab.lower()
            self.assertTrue(lab.split(" ")[0] in ("💝", "💖", "🎁") and lab.split(" ")[-1] in ("🌸", "🌷", "✨", "🎁"))
            self.assertFalse(any(w in low for w in ("конфет", "халяв", "бесплатные бонусы", "🍬")))
        seen = {tasks.generate_config(f"fl{i}").text("label") for i in range(300)}
        self.assertEqual(seen, set(tasks.FREE_LABELS))                   # все варианты используются
        c = tasks.generate_config("fl1")
        self.assertEqual(tasks.TasksConfig.from_json(c.to_json()).text("label"), c.text("label"))   # постоянная у бота
        # уже сохранённый конфиг со старой подписью получает новую, callback_data не меняется
        import json
        d = json.loads(c.to_json()); d["t"]["label"] = "🍭 Бесплатные конфеты 🍬"
        old = tasks.TasksConfig.from_json(json.dumps(d, ensure_ascii=False))
        self.assertEqual(old.text("label"), c.text("label"))
        self.assertEqual(old.cb, c.cb)

    def test_all_variants_have_required_placeholders(self):
        for key, needed in tasks.REQUIRED.items():
            words = next(w for k, w, _ in tasks.FIELDS if k == key)
            for w in words:
                self.assertTrue(all(n in w for n in needed), (key, w))

    def test_broken_config_is_rejected(self):
        c = tasks.generate_config("z")
        bad = c.to_json().replace(c.cb["open"], "zz:123")
        with self.assertRaises(ValueError):
            tasks.TasksConfig.from_json(bad)


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tasks.generate_config("p")

    def test_parse_callbacks(self):
        c = self.cfg
        self.assertEqual(tasks.parse_callback(c, c.cb["open"]), ("open", None))
        self.assertEqual(tasks.parse_callback(c, c.cb["daily"]), ("daily", None))
        self.assertEqual(tasks.parse_callback(c, f"{c.cb['task']}:42"), ("task", 42))
        self.assertEqual(tasks.parse_callback(c, f"{c.cb['send']}:7"), ("send", 7))
        self.assertEqual(tasks.parse_callback(c, f"{c.cb['done']}:7"), ("done", 7))
        # id только у заданий; чужие, пустые и испорченные данные — None
        self.assertIsNone(tasks.parse_callback(c, f"{c.cb['daily']}:5"))
        self.assertIsNone(tasks.parse_callback(c, f"{c.cb['task']}:x"))
        self.assertIsNone(tasks.parse_callback(c, None))
        self.assertIsNone(tasks.parse_callback(tasks.generate_config("other"), c.cb["open"]))

    def test_start_payload(self):
        self.assertEqual(tasks.parse_start_referrer("/start ref_123"), 123)
        self.assertEqual(tasks.parse_start_referrer("/start@my_bot ref_5"), 5)
        for bad in ("/start", "/start ref_", "/start ref_0", "/start ref_abc", "/start other", None, "ref_5"):
            self.assertIsNone(tasks.parse_start_referrer(bad), bad)

    def test_ref_link(self):
        self.assertEqual(tasks.ref_link("my_bot", 55), "https://t.me/my_bot?start=ref_55")
        self.assertEqual(tasks.ref_link("@my_bot", 55), "https://t.me/my_bot?start=ref_55")
        self.assertIsNone(tasks.ref_link(None, 55))

    def test_fmt_left(self):
        self.assertEqual(tasks.fmt_left(5 * 3600 + 12 * 60 + 9), "5ч 12м")
        self.assertEqual(tasks.fmt_left(3 * 3600), "3ч")
        self.assertEqual(tasks.fmt_left(12 * 60), "12м")
        self.assertEqual(tasks.fmt_left(20), "меньше минуты")
        self.assertEqual(tasks.fmt_left(-5), "меньше минуты")

    def test_cooldown_is_24h(self):
        self.assertEqual(tasks.DAILY_COOLDOWN, timedelta(hours=24))


class NormalizeTests(unittest.TestCase):
    def test_button_text(self):
        self.assertEqual(tasks.normalize_button_text("  Подписаться   на канал "), "Подписаться на канал")
        for bad in ("", "   ", "я" * 65):
            with self.assertRaises(ValueError):
                tasks.normalize_button_text(bad)

    def test_reward(self):
        self.assertEqual(tasks.normalize_reward("15"), 15)
        self.assertEqual(tasks.normalize_reward(" 1 000 "), 1000)
        for bad in ("", "0", "-3", "1.5", "abc", "9" * 13, tasks.MAX_REWARD + 1):
            with self.assertRaises(ValueError):
                tasks.normalize_reward(bad)

    def test_comment_is_optional_plain_text_with_limit(self):
        self.assertEqual(tasks.normalize_comment(None), "")
        self.assertEqual(tasks.normalize_comment("   "), "")
        self.assertEqual(tasks.normalize_comment("  Подписка и отзыв \r\n"), "Подписка и отзыв")
        self.assertEqual(len(tasks.normalize_comment("я" * tasks.MAX_COMMENT)), tasks.MAX_COMMENT)
        with self.assertRaises(ValueError):
            tasks.normalize_comment("я" * (tasks.MAX_COMMENT + 1))

    def test_description(self):
        self.assertEqual(tasks.normalize_description("  Сделайте это \r\n"), "Сделайте это")
        with self.assertRaises(ValueError):
            tasks.normalize_description("   ")
        with self.assertRaises(ValueError):
            tasks.normalize_description("а" * (tasks.MAX_DESC + 1))
        # длина считается в худшем случае по вариантам [а/б]
        with self.assertRaises(ValueError):
            tasks.normalize_description("[" + "а" * tasks.MAX_DESC + "/б]" + "в")


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.cfg = tasks.generate_config("r")
        self.deco = deco.generate_config("r")

    def test_menu_text(self):
        text = tasks.render_menu(self.cfg, self.deco, 12)
        self.assertTrue(text.startswith(f"<b>{self.cfg.text('label')}</b>\n\n💫 "))
        self.assertTrue(text.endswith("🍭 Ваш баланс: 12🍬"))

    def test_menu_rows_order_and_styles(self):
        items = [T(3, "Первое"), T(1, "Второе")]
        rows = tasks.menu_rows(self.cfg, items)
        self.assertEqual([r[0][0] for r in rows],
                         [self.cfg.text("daily_label"), self.cfg.text("ref_label"), "Первое", "Второе", tasks.BACK_LABEL])
        self.assertEqual(rows[2][0][1], f"{self.cfg.cb['task']}:3")
        self.assertEqual(rows[-1][0][1], self.cfg.cb["back"])
        self.assertEqual(tasks.menu_styles(items)[-1], "danger")
        self.assertEqual(len(tasks.menu_styles(items)), len(rows))

    def test_daily_and_referral_button_colors_and_cooldown_label(self):
        self.assertEqual(tasks.menu_styles([])[:2], ("success", "success"))                 # подарок готов, ссылка — зелёные
        self.assertEqual(tasks.menu_styles([], 3600)[:2], ("primary", "success"))           # кулдаун — подарок синий
        self.assertEqual(tasks.menu_styles([], 0)[0], "success")
        base = self.cfg.text("daily_label")
        self.assertEqual(tasks.daily_button_label(self.cfg, None), base)
        for left, shown in ((66600, "(18ч 30м)"), (3600, "(1ч 0м)"), (300, "(0ч 5м)"), (20, "(0ч 1м)"), (86400, "(24ч 0м)")):
            label = tasks.daily_button_label(self.cfg, left)
            self.assertIn(" " + shown, label)
            self.assertTrue(label.startswith("🤍 ") and label.endswith(" 🤍"))   # эмодзи остаются по краям
            self.assertEqual(label.replace(" " + shown, ""), base)
        rows = tasks.menu_rows(self.cfg, [], 66600)
        self.assertEqual(rows[0][0][1], self.cfg.cb["daily"])                    # callback_data тот же
        self.assertEqual(tasks.menu_rows(self.cfg, [])[0][0][0], base)

    def test_menu_without_tasks_has_gift_link_and_back(self):
        self.assertEqual(len(tasks.menu_rows(self.cfg, [])), 3)

    def test_task_card_and_buttons(self):
        shot, plain = T(2, shot=True, reward=25), T(3, reward=5)
        rows, styles = tasks.task_rows(self.cfg, shot, None)
        self.assertEqual(rows[0][0], (self.cfg.text("send_label"), f"{self.cfg.cb['send']}:2"))
        self.assertEqual(tasks.task_rows(self.cfg, plain, None)[0][0][0], (self.cfg.text("done_label"), f"{self.cfg.cb['done']}:3"))
        # пока на проверке действия нет — только «Назад»
        r, st = tasks.task_rows(self.cfg, shot, "pending")
        self.assertEqual(r, [[(tasks.BACK_LABEL, self.cfg.cb["menu"])]])
        self.assertEqual(st, ("danger",))
        # после принятия (одноразовое) кнопка действия остаётся, но серая; «Назад» красная
        r, st = tasks.task_rows(self.cfg, shot, "approved")
        self.assertEqual(r, [[(self.cfg.text("send_label"), f"{self.cfg.cb['send']}:2")], [(tasks.BACK_LABEL, self.cfg.cb["menu"])]])
        self.assertEqual(st, ("", "danger"))
        # отклонённое можно отправить заново
        self.assertEqual(tasks.task_rows(self.cfg, shot, "rejected")[0][0][0][1], f"{self.cfg.cb['send']}:2")
        text = tasks.render_task(self.cfg, shot, "seed", None)
        self.assertIn("<b>Подписаться</b>", text)
        self.assertIn("25🍬", text)

    def test_one_time_task_buttons_green_until_reward_then_blue(self):
        once, many = T(1, "А"), T(2, "Б", once=False)
        base = tasks.menu_styles([once, many])
        self.assertEqual(base[2:4], ("success", "success"))                       # изначально зелёные
        self.assertEqual(tasks.menu_styles([once, many], None, {1: "approved", 2: "approved"})[2:4], ("primary", "success"))
        for status in ("pending", "rejected"):                                    # на проверке и отклонённое — не награда
            self.assertEqual(tasks.menu_styles([once], None, {1: status})[2], "success")
        self.assertEqual(tasks.menu_styles([once], None, {7: "approved"})[2], "success")   # чужое задание не влияет
        self.assertEqual(tasks.menu_styles([once], None, {1: "approved"})[-1], "danger")   # «Назад» по-прежнему красная
        self.assertEqual(len(tasks.menu_styles([once, many], None, {1: "approved"})), len(tasks.menu_rows(self.cfg, [once, many])))

    def test_one_time_done_card_text_and_grey_action_button(self):
        for shot in (True, False):
            t = T(2, shot=shot)
            text = tasks.render_task(self.cfg, t, "seed", "approved")
            self.assertIn(self.cfg.text("status_done"), text)
            self.assertIn(tasks.ONE_TIME_NOTE, text)                              # написано, что повторить нельзя
            rows, styles = tasks.task_rows(self.cfg, t, "approved")
            key = "send" if shot else "done"
            self.assertEqual(rows[0][0], (self.cfg.text(f"{key}_label"), f"{self.cfg.cb[key]}:2"))   # кнопка на месте
            self.assertEqual(styles, ("", "danger"))                              # серая (без цвета), «Назад» красная
        # до награды кнопка действия зелёная, у отклонённого — тоже, и текста «выполнено» нет
        for status in (None, "rejected"):
            self.assertEqual(tasks.task_rows(self.cfg, T(2), status)[1][0], "success")
            self.assertNotIn(tasks.ONE_TIME_NOTE, tasks.render_task(self.cfg, T(2), "seed", status))
        rows, styles = tasks.task_rows(self.cfg, T(2), "pending")                 # на проверке действия нет
        self.assertEqual((len(rows), styles), (1, ("danger",)))

    def test_repeatable_task_never_shows_done_state(self):
        t = T(2, once=False)
        self.assertFalse(tasks.is_done(t, "approved"))
        self.assertIsNone(tasks.card_status(t, "approved"))
        self.assertEqual(tasks.card_status(t, "pending"), "pending")
        self.assertTrue(tasks.is_done(T(2), "approved"))
        self.assertEqual(tasks.card_status(T(2), "approved"), "approved")
        text = tasks.render_task(self.cfg, t, "seed", tasks.card_status(t, "approved"))
        self.assertNotIn(tasks.ONE_TIME_NOTE, text)
        self.assertEqual(tasks.task_rows(self.cfg, t, tasks.card_status(t, "approved"))[1][0], "success")

    def test_task_from_row_defaults_to_one_time(self):
        row = types.SimpleNamespace(id=1, button_text="a", about="", reward=1, require_screenshot=False)
        self.assertTrue(tasks.task_from_row(row).one_time)
        row.one_time = False
        self.assertFalse(tasks.task_from_row(row).one_time)

    def test_task_from_row_carries_admin_comment(self):
        row = types.SimpleNamespace(id=1, button_text="a", about="", reward=1, require_screenshot=False)
        self.assertEqual(tasks.task_from_row(row).comment, "")        # старые строки без колонки
        row.comment = None
        self.assertEqual(tasks.task_from_row(row).comment, "")
        row.comment = "Подписаться на канал"
        self.assertEqual(tasks.task_from_row(row).comment, "Подписаться на канал")

    def test_rejected_notice_is_the_fixed_owner_text(self):
        self.assertEqual(
            tasks.REJECTED_NOTICE,
            "❌ Запрос о выполнении задания был отклонён, так как задание выполнено не полностью, "
            "либо Вы прикрепили не все скриншоты.",
        )

    def test_task_text_is_escaped_and_formatted(self):
        t = T(1, button="<b>x</b>", about="**жирно** <script>")
        text = tasks.render_task(self.cfg, t, "s")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("<b>жирно</b> &lt;script&gt;", text)

    def test_task_variants_are_stable_per_bot(self):
        t = T(5, about="Привет [товарищ/друг]!")
        self.assertEqual(tasks.render_task(self.cfg, t, "s1"), tasks.render_task(self.cfg, t, "s1"))
        self.assertEqual(len({tasks.render_task(self.cfg, t, f"s{i}") for i in range(40)}), 2)

    def test_ref_screen_has_copyable_link(self):
        text = tasks.render_ref(self.cfg, "https://t.me/b?start=ref_9", 3)
        self.assertIn("<code>https://t.me/b?start=ref_9</code>", text)
        self.assertIn("3", text.splitlines()[-1])

    def test_image_detection(self):
        photo = types.SimpleNamespace(photo=[types.SimpleNamespace(file_id="small"), types.SimpleNamespace(file_id="big")], document=None)
        self.assertEqual(tasks.is_image_message(photo), ("big", "photo"))
        doc = types.SimpleNamespace(photo=None, document=types.SimpleNamespace(file_id="d", mime_type="image/png"))
        self.assertEqual(tasks.is_image_message(doc), ("d", "document"))
        pdf = types.SimpleNamespace(photo=None, document=types.SimpleNamespace(file_id="d", mime_type="application/pdf"))
        self.assertIsNone(tasks.is_image_message(pdf))
        self.assertIsNone(tasks.is_image_message(types.SimpleNamespace(photo=None, document=None, text="hi")))

    def test_no_hardcoded_tasks_in_code(self):
        """Заданий в коде нет: ни одного готового Task/кнопки-задания в модуле."""
        public = [n for n in dir(tasks) if isinstance(getattr(tasks, n), tasks.Task)]
        self.assertEqual(public, [])


# ---------- сценарии в боте (БД и Telegram подменены) ----------

class FakeStats:
    def __init__(self, known):
        self.known, self.bot_id = set(known), 1

    def has(self, uid):
        return uid in self.known

    def touch(self, *a, **k):
        pass

    def touch_activity(self, *a, **k):
        pass

    def add(self, uid, name=None):
        self.known.add(uid)
        return True

    async def flush(self):
        pass

    def remove(self, uid):
        return self.known.discard(uid) or True


class BotFlowTests(unittest.TestCase):
    def setUp(self):
        from app import captcha, greeting, shop_buttons, suggest_ui, gallery

        repo, messaging = handlers.repo, handlers.messaging   # именно те модули, которыми пользуется handlers

        self.sent, self.answers, self.deleted, self.edited = [], [], [], []
        self.db = {"candies": 7, "daily": (False, None, None), "tasks": [], "completion": None, "instant": 17,
                   "referral": 12, "submitted": [], "refs": []}
        self._saved = {}

        def patch(mod, name, fn):
            self._saved[(mod, name)] = getattr(mod, name, None)
            setattr(mod, name, fn)

        self.nid = 100

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            self.nid += 1
            self.sent.append((self.nid, chat_id, text, reply_markup))
            return types.SimpleNamespace(message_id=self.nid)

        async def get_candies(bot_id, uid):
            return self.db["candies"]

        async def get_balance_and_daily(bot_id, uid):
            return self.db["candies"], self.db.get("daily_at")

        async def fake_edit(b, chat_id, message_id, text, reply_markup=None, *, has_photo=False):
            if self.db.get("edit_fails"):
                raise test_handlers.handlers.TelegramAPIError("message to edit not found")
            self.edited.append((message_id, chat_id, text, reply_markup, has_photo))

        async def list_tasks(only_visible=True, limit=None):
            return self.db["tasks"]

        async def get_task(task_id, only_visible=True):
            return next((t for t in self.db["tasks"] if t.id == task_id), None)

        async def get_completion(bot_id, uid, task_id):
            return self.db["completion"]

        async def claim_daily(bot_id, uid, amount, cooldown):
            return self.db["daily"]

        async def get_task_statuses(bot_id, uid):
            return dict(self.db.get("statuses", {}))

        async def complete(bot_id, uid, task_id, reward, username=None, repeatable=False):
            self.db.setdefault("repeatable", []).append(repeatable)
            return self.db["instant"]

        async def submit(bot_id, uid, task_id, reward, files, username=None, repeatable=False):
            self.db.setdefault("repeatable", []).append(repeatable)
            self.db["submitted"].append((uid, task_id, reward, list(files)))
            return 55

        async def is_blocked(uid):
            return self.db.get("blocked", False)

        async def is_receipt_blocked(uid):   # блокировка чеков не должна влиять на задания
            return self.db.get("receipt_blocked", False)

        async def register_referral(bot_id, invited, referrer, reward):
            self.db["refs"].append((invited, referrer, reward))
            return self.db["referral"]

        async def count_referrals(bot_id, uid):
            return 4

        async def deliver(cid, targets=None):
            self.db["delivered"] = cid
            return 1

        async def touch(uid, name):
            return False

        patch(messaging, "send_message", fake_send)
        patch(messaging, "edit_message", fake_edit)
        for name, fn in (("get_candies", get_candies), ("get_balance_and_daily", get_balance_and_daily), ("list_tasks", list_tasks), ("get_task", get_task),
                         ("get_completion", get_completion), ("get_task_statuses", get_task_statuses), ("claim_daily", claim_daily),
                         ("complete_task_instant", complete), ("submit_task_screenshot", submit),
                         ("register_referral", register_referral), ("count_referrals", count_referrals),
                         ("is_task_blocked", is_blocked), ("is_receipt_blocked", is_receipt_blocked)):
            patch(repo, name, fn)
        patch(handlers.task_review, "deliver", deliver)
        patch(tasks, "COLLECT_REFRESH_DELAY", 0.01)
        patch(handlers.moderation.cache, "touch", touch)

        self.texts = types.SimpleNamespace(
            greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
            suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"),
            shop_menu=shop.generate_config("t"), captcha=captcha.generate_config("t"), version=1)
        self.cfg = self.texts.tasks_menu
        self.profile = types.SimpleNamespace(name="Бот", message_image_url=None, username="my_bot", token="1:tok")
        self.stats = FakeStats({5})
        self.router = handlers.build_router(self.stats, self.texts, self.profile)
        self.bot = test_handlers.FakeBot()
        self.bot.delete_message = self._delete

    async def _delete(self, chat_id, message_id):
        self.deleted.append(message_id)

    def tearDown(self):
        for (mod, name), old in self._saved.items():
            setattr(mod, name, old)

    def call(self, data, uid=5, msg_id=10):
        async def answer(text=None, show_alert=False, **k):
            self.answers.append((text, show_alert))

        return types.SimpleNamespace(
            data=data, answer=answer, from_user=types.SimpleNamespace(id=uid, username="u"),
            message=types.SimpleNamespace(message_id=msg_id, chat=types.SimpleNamespace(id=uid)))

    def press(self, data, **kw):
        asyncio.run(self.router.handlers["on_tasks_button"](self.call(data, **kw), self.bot))

    def last_text(self):
        return self.sent[-1][2]

    def test_open_menu_lists_visible_tasks_and_replaces_screen(self):
        self.db["tasks"] = [types.SimpleNamespace(id=4, button_text="Канал", about="a", reward=3, require_screenshot=False)]
        self.press(self.cfg.cb["open"], msg_id=10)
        self.assertIn("🍭 Ваш баланс: 7🍬", self.last_text())
        labels = [row[0].text for row in self.sent[-1][3].inline_keyboard]
        self.assertEqual(labels[2], "Канал")
        self.assertIn(10, self.deleted)   # прежнее сообщение удалено, новое — отдельным сообщением

    def test_foreign_or_old_buttons_do_nothing(self):
        self.press("fo:ffffffffff")
        self.assertEqual(self.sent, [])
        self.stats.known.clear()
        self.press(self.cfg.cb["open"])
        self.assertEqual(self.sent, [])

    def test_daily_gift_given_edits_message_in_place_and_turns_button_blue(self):
        self.db["daily"] = (True, 17, None)
        self.press(self.cfg.cb["daily"], msg_id=10)
        self.assertEqual(self.sent, [])               # новое сообщение не отправлялось
        self.assertEqual(self.deleted, [])            # и исходное не удалялось
        msg_id, chat_id, text, markup, has_photo = self.edited[-1]
        self.assertEqual((msg_id, chat_id), (10, 5))  # отредактировано именно исходное сообщение
        self.assertIn("🍭 Ваш баланс: 17🍬", text)
        daily = markup.inline_keyboard[0][0]
        self.assertEqual(daily.callback_data, self.cfg.cb["daily"])
        self.assertEqual(daily.style, "primary")      # на время кулдауна — синяя
        self.assertIn("(24ч 0м)", daily.text)
        self.assertEqual(markup.inline_keyboard[1][0].style, "success")   # реферальная — зелёная
        self.assertTrue(self.answers[-1][1])
        self.assertIn(str(tasks.DAILY_REWARD), self.answers[-1][0])

    def test_daily_gift_edit_failure_falls_back_to_new_message(self):
        self.db["daily"] = (True, 17, None)
        self.db["edit_fails"] = True
        self.press(self.cfg.cb["daily"], msg_id=10)
        self.assertIn("17🍬", self.last_text())       # не удалось отредактировать — пришло новое меню
        self.assertIn(10, self.deleted)

    def test_daily_gift_cooldown_alert_keeps_screen(self):
        from datetime import datetime, timezone
        self.db["daily"] = (False, None, datetime.now(timezone.utc) - timedelta(hours=18, minutes=30))
        self.press(self.cfg.cb["daily"])
        self.assertEqual((self.sent, self.edited, self.deleted), ([], [], []))   # экран не меняется
        self.assertIn("5ч", self.answers[-1][0])      # осталось около 5 часов
        self.assertTrue(self.answers[-1][1])

    def test_open_menu_shows_green_daily_button_when_ready(self):
        self.press(self.cfg.cb["open"])
        rows = self.sent[-1][3].inline_keyboard
        self.assertEqual((rows[0][0].style, rows[0][0].text), ("success", self.cfg.text("daily_label")))
        self.assertEqual(rows[1][0].style, "success")

    def test_open_menu_shows_blue_daily_button_with_time_left_during_cooldown(self):
        from datetime import datetime, timezone
        self.db["daily_at"] = datetime.now(timezone.utc) - timedelta(hours=18, minutes=30)
        self.press(self.cfg.cb["open"])
        daily = self.sent[-1][3].inline_keyboard[0][0]
        self.assertEqual(daily.style, "primary")
        self.assertRegex(daily.text, r"\(5ч (2[89]|30)м\)")
        self.db["daily_at"] = datetime.now(timezone.utc) - timedelta(hours=25)   # кулдаун прошёл — снова зелёная
        self.press(self.cfg.cb["open"])
        self.assertEqual(self.sent[-1][3].inline_keyboard[0][0].style, "success")

    def test_referral_screen_shows_personal_link(self):
        self.press(self.cfg.cb["ref"])
        self.assertIn("<code>https://t.me/my_bot?start=ref_5</code>", self.last_text())

    def test_referral_screen_without_bot_username(self):
        self.profile.username = None
        self.press(self.cfg.cb["ref"])
        self.assertEqual(self.sent, [])
        self.assertTrue(self.answers[-1][1])

    def test_instant_task_gives_reward_once(self):
        row = types.SimpleNamespace(id=4, button_text="Канал", about="a", reward=3, require_screenshot=False)
        self.db["tasks"] = [row]
        self.press(f"{self.cfg.cb['done']}:4")
        self.assertIn("17🍬", self.last_text())
        self.assertIn("3", self.answers[-1][0])
        # повторное нажатие: награды нет (repo вернул None) — показывается карточка
        self.db["instant"] = None
        n = len(self.sent)
        self.press(f"{self.cfg.cb['done']}:4")
        self.assertEqual(len(self.sent), n + 1)
        self.assertIn("<b>Канал</b>", self.last_text())

    def test_menu_task_button_blue_after_reward_for_one_time_only(self):
        self.db["tasks"] = [
            types.SimpleNamespace(id=4, button_text="Канал", about="a", reward=3, require_screenshot=False, one_time=True),
            types.SimpleNamespace(id=5, button_text="Чат", about="a", reward=3, require_screenshot=False, one_time=True),
            types.SimpleNamespace(id=6, button_text="Опрос", about="a", reward=3, require_screenshot=False, one_time=False),
        ]
        self.press(self.cfg.cb["open"])
        self.assertEqual([r[0].style for r in self.sent[-1][3].inline_keyboard[2:5]], ["success"] * 3)
        self.db["statuses"] = {4: "approved", 5: "rejected", 6: "approved"}
        self.press(self.cfg.cb["open"])
        self.assertEqual([r[0].style for r in self.sent[-1][3].inline_keyboard[2:5]], ["primary", "success", "success"])

    def test_done_one_time_task_card_and_dead_button(self):
        row = types.SimpleNamespace(id=4, button_text="Канал", about="a", reward=3, require_screenshot=True, one_time=True)
        self.db["tasks"] = [row]
        self.db["completion"] = types.SimpleNamespace(status="approved")
        self.press(f"{self.cfg.cb['task']}:4")
        self.assertIn(tasks.ONE_TIME_NOTE, self.last_text())
        button = self.sent[-1][3].inline_keyboard[0][0]
        self.assertEqual(button.text, self.cfg.text("send_label"))
        self.assertFalse(getattr(button, "style", ""))                            # серая
        n_sent, n_edit = len(self.sent), len(self.edited)
        self.press(button.callback_data)                                          # нажатие ничего не делает
        self.assertEqual((len(self.sent), len(self.edited), self.deleted[-1:]), (n_sent, n_edit, self.deleted[-1:]))
        self.assertTrue(self.answers[-1][1])
        self.assertIn(tasks.ONE_TIME_NOTE, self.answers[-1][0])
        self.assertEqual(self.db["submitted"], [])
        self.assertNotIn(5, getattr(self.router, "task_wait", {}))
        # то же для задания без скриншота: награда не просится у repo
        row.require_screenshot = False
        self.db["instant"] = 999
        self.press(f"{self.cfg.cb['done']}:4")
        self.assertNotIn("repeatable", self.db)
        self.assertIn(tasks.ONE_TIME_NOTE, self.answers[-1][0])

    def test_repeatable_task_can_be_done_again(self):
        row = types.SimpleNamespace(id=4, button_text="Опрос", about="a", reward=3, require_screenshot=False, one_time=False)
        self.db["tasks"] = [row]
        self.db["completion"] = types.SimpleNamespace(status="approved")
        self.press(f"{self.cfg.cb['task']}:4")
        self.assertNotIn(tasks.ONE_TIME_NOTE, self.last_text())
        self.assertEqual(self.sent[-1][3].inline_keyboard[0][0].style, "success")
        self.press(f"{self.cfg.cb['done']}:4")
        self.assertEqual(self.db["repeatable"], [True])                           # repo получил разрешение на повтор
        self.assertIn("17🍬", self.last_text())
        self.assertIn("3", self.answers[-1][0])

    def test_one_time_task_passes_repeatable_false(self):
        row = types.SimpleNamespace(id=4, button_text="Канал", about="a", reward=3, require_screenshot=False)   # без поля — одноразовое
        self.db["tasks"] = [row]
        self.press(f"{self.cfg.cb['done']}:4")
        self.assertEqual(self.db["repeatable"], [False])

    SHOT_TASK = types.SimpleNamespace(id=9, button_text="Отзыв", about="a", reward=30, require_screenshot=True)

    def msg(self, photo=None, text=None, mid=500):
        return types.SimpleNamespace(
            text=text, photo=[types.SimpleNamespace(file_id=photo)] if photo else None, document=None, message_id=mid,
            from_user=types.SimpleNamespace(id=5, username="u"))

    def converse(self, *steps):
        """Шаги — сообщения (Message) или callback_data (str) в ОДНОМ цикле событий: так успевает сработать отложенное
        обновление экрана-счётчика."""
        async def go():
            for step in steps:
                if isinstance(step, str):
                    await self.router.handlers["on_tasks_button"](self.call(step), self.bot)
                else:
                    await self.router.handlers["on_private_message"](step, self.bot)
                await asyncio.sleep(0.05)
        asyncio.run(go())

    def test_screenshot_task_collects_several_shots_with_counter_then_submits(self):
        self.db["tasks"] = [self.SHOT_TASK]
        self.press(f"{self.cfg.cb['send']}:9")
        # экран приёма: описание и подсказка про несколько скринов, «Назад», но «Отправить» пока нет (скринов нет)
        self.assertIn(tasks.fill(tasks.SHOTS_PROMPT, max=tasks.MAX_SHOTS), self.last_text())
        self.assertEqual(len(self.sent[-1][3].inline_keyboard), 1)
        # не фото -> просим скриншот, ждём дальше
        self.converse(self.msg(text="привет"))
        self.assertEqual(self.last_text(), self.cfg.text("not_photo"))
        self.assertEqual(self.db["submitted"], [])
        # два скрина подряд (как альбом) -> ОДИН обновлённый экран со счётчиком и кнопкой «Отправить на проверку (2)»
        before = len(self.sent)
        self.converse(self.msg(photo="F1", mid=501), self.msg(photo="F2", mid=502))
        self.assertIn(tasks.fill(tasks.SHOTS_COUNTER, count=2, max=tasks.MAX_SHOTS), self.last_text())
        self.assertGreaterEqual(len(self.sent), before + 1)
        labels = [row[0].text for row in self.sent[-1][3].inline_keyboard]
        self.assertIn("(2)", labels[0])
        self.assertEqual(self.db["submitted"], [])   # пока не нажали «Отправить»
        # «Отправить на проверку» -> заявка со ВСЕМИ скринами, доставка в бот для чеков, возврат в меню
        done = self.sent[-1][3].inline_keyboard[0][0].callback_data
        self.converse(done)
        self.assertEqual(self.db["submitted"], [(5, 9, 30, [("F1", "photo"), ("F2", "photo")])])
        self.assertEqual(self.db["delivered"], 55)
        self.assertEqual([m[2] for m in self.sent if m[2] == self.cfg.text("sent")].__len__(), 1)
        self.assertIn("🍭 Ваш баланс", self.last_text())
        # повторное нажатие по старой кнопке не отправляет набор второй раз
        self.converse(done)
        self.assertEqual(len(self.db["submitted"]), 1)

    def test_back_cancels_collected_shots(self):
        self.db["tasks"] = [self.SHOT_TASK]
        self.press(f"{self.cfg.cb['send']}:9")
        self.converse(self.msg(photo="F1"), self.cfg.cb["menu"])
        self.assertIn("🍭 Ваш баланс", self.last_text())   # вернулись в меню раздела
        self.converse(self.msg(photo="F2", mid=502))      # после отмены фото не принимаются
        self.assertEqual(self.db["submitted"], [])
        self.press(f"{self.cfg.cb['send']}:9")
        self.converse(self.msg(photo="F3", mid=503))
        done = self.sent[-1][3].inline_keyboard[0][0].callback_data
        self.converse(done)
        self.assertEqual(self.db["submitted"], [(5, 9, 30, [("F3", "photo")])])   # старый F1 не просочился

    def test_shots_limit(self):
        self.db["tasks"] = [self.SHOT_TASK]
        self.press(f"{self.cfg.cb['send']}:9")
        self.converse(*[self.msg(photo=f"P{i}", mid=600 + i) for i in range(tasks.MAX_SHOTS + 3)])
        self.assertIn(tasks.fill(tasks.SHOTS_COUNTER, count=tasks.MAX_SHOTS, max=tasks.MAX_SHOTS), self.last_text())
        self.assertIn(tasks.fill(tasks.SHOTS_LIMIT, max=tasks.MAX_SHOTS), self.last_text())
        self.converse(self.sent[-1][3].inline_keyboard[0][0].callback_data)
        self.assertEqual(len(self.db["submitted"][0][3]), tasks.MAX_SHOTS)

    def test_blocked_user_cannot_send_screenshots(self):
        self.db["tasks"] = [self.SHOT_TASK]
        self.db["blocked"] = True
        self.press(f"{self.cfg.cb['send']}:9")
        self.assertEqual(self.answers[-1], (tasks.TASK_BLOCKED_ALERT, True))
        self.assertEqual(self.sent, [])
        # заблокировали, пока человек готовил скрины: набор не уходит
        self.db["blocked"] = False
        self.press(f"{self.cfg.cb['send']}:9")
        self.converse(self.msg(photo="F1"))
        self.db["blocked"] = True
        self.converse(self.sent[-1][3].inline_keyboard[0][0].callback_data)
        self.assertEqual(self.db["submitted"], [])
        self.assertEqual(self.answers[-1], (tasks.TASK_BLOCKED_ALERT, True))

    def test_receipt_block_does_not_block_tasks(self):
        # «Отправка чеков» и «Задания» — разные блокировки
        self.db["tasks"] = [self.SHOT_TASK]
        self.db["receipt_blocked"] = True
        self.press(f"{self.cfg.cb['send']}:9")
        self.assertNotEqual(self.answers[-1], (tasks.TASK_BLOCKED_ALERT, True))
        self.converse(self.msg(photo="F1"))
        self.converse(self.sent[-1][3].inline_keyboard[0][0].callback_data)
        self.assertEqual(self.db["submitted"], [(5, 9, 30, [("F1", "photo")])])

    def test_task_screens_have_no_url_or_support_buttons(self):
        self.db["tasks"] = [self.SHOT_TASK]
        self.press(self.cfg.cb["open"])
        self.press(f"{self.cfg.cb['task']}:9")
        self.press(f"{self.cfg.cb['send']}:9")
        for _, _, text, markup in self.sent:
            self.assertNotIn("t.me", text)
            for row in markup.inline_keyboard:
                for b in row:
                    self.assertIsNone(getattr(b, "url", None))

    def test_admin_decision_notifies_user_with_this_bots_texts(self):
        # решение из бота для чеков: пользователю пишет ЭТОТ бот-магазин (hook зарегистрирован в build_router)
        from app import task_review
        from unittest import mock

        with mock.patch.object(handlers.topup, "ensure_started", lambda: None):   # без фоновых задач оплат
            handlers.build_router(self.stats, self.texts, self.profile, self.bot)
        try:
            hook = task_review.HOOKS[self.stats.bot_id]
            asyncio.run(hook.notify(5, "approved", "Отзыв <b>", 30))
            self.assertIn("Отзыв &lt;b&gt;", self.sent[-1][2])
            self.assertIn("30", self.sent[-1][2])
            self.assertEqual(self.sent[-1][1], 5)
            asyncio.run(hook.notify(5, "rejected", "Отзыв", 30))
            self.assertEqual(self.sent[-1][1], 5)
            self.assertEqual(self.sent[-1][2], tasks.REJECTED_NOTICE)   # фиксированный текст, без названия задания
            self.assertIsNone(self.sent[-1][3])                         # короткое уведомление: без кнопок
        finally:
            task_review.detach(self.stats.bot_id)
            handlers.topup.detach(self.stats.bot_id)

    def test_pending_task_has_no_action_button(self):
        self.db["tasks"] = [types.SimpleNamespace(id=9, button_text="Отзыв", about="a", reward=30, require_screenshot=True)]
        self.db["completion"] = types.SimpleNamespace(status="pending")
        self.press(f"{self.cfg.cb['send']}:9")
        rows = self.sent[-1][3].inline_keyboard
        self.assertEqual(len(rows), 1)   # только «Назад»
        self.assertIn(self.cfg.text("status_pending"), self.last_text())

    def test_hidden_task_button_returns_menu(self):
        self.db["tasks"] = []
        self.press(f"{self.cfg.cb['task']}:99")
        self.assertIn("🍭 Ваш баланс", self.last_text())

    def test_referral_start_and_reward_after_captcha(self):
        self.stats.known.clear()
        self.router = handlers.build_router(self.stats, self.texts, self.profile)
        # проходящая капчу сторона: проверяем только, что /start с payload запоминает приглашение
        async def captcha_ok():
            pass
        msg = types.SimpleNamespace(text="/start ref_77", message_id=1, from_user=types.SimpleNamespace(id=5, username="u"))
        self.router.handlers["on_start"]  # хендлер зарегистрирован
        with unittest.mock.patch.object(handlers.captcha, "is_correct", lambda *a, **k: True):
            async def go():
                try:
                    await self.router.handlers["on_start"](msg, self.bot)
                except Exception:
                    pass   # отправка капчи нам не важна: нужен только факт запоминания ссылки
                call = self.call("cap:x:y")
                call.data = handlers.captcha.CALLBACK_PREFIX + "x:n"
                await self.router.handlers["on_captcha_answer"](call, self.bot)
            asyncio.run(go())
        self.assertEqual(self.db["refs"], [(5, 77, tasks.REFERRAL_REWARD)])
        self.assertTrue(any(self.cfg.text("invited").split("{")[0] in m[2] for m in self.sent if m[1] == 77))

    def test_self_referral_ignored(self):
        self.stats.known.clear()
        msg = types.SimpleNamespace(text="/start ref_5", message_id=1, from_user=types.SimpleNamespace(id=5, username="u"))
        try:
            asyncio.run(self.router.handlers["on_start"](msg, self.bot))
        except Exception:
            pass
        self.assertEqual(self.db["refs"], [])


class ModerationTests(unittest.TestCase):
    def test_review_callbacks(self):
        from app import task_review

        self.assertEqual(task_review.parse("tkchk:ok:12"), ("approve", 12))
        self.assertEqual(task_review.parse("tkchk:no:3"), ("reject", 3))
        self.assertEqual(task_review.parse("tkchk:bl:8"), ("block", 8))
        for bad in (None, "tkchk:x:1", "tkchk:ok:x", "mod:a:1", "tkchk:ok", "tkm:a:1", "tpchk:ok:1"):
            self.assertIsNone(task_review.parse(bad), bad)
        self.assertEqual(
            task_review.keyboard_rows(5),
            [[("✅ Подтвердить", "tkchk:ok:5"), ("❌ Отклонить", "tkchk:no:5")], [("🚫 Заблокировать", "tkchk:bl:5")]],
        )
        self.assertTrue(all(len(cb.encode()) <= 64 for row in task_review.keyboard_rows(10**12) for _, cb in row))

    def test_request_text_marks_task_completion(self):
        from app import repo, task_review

        r = repo.TaskReview(
            id=7, bot_id=1, user_id=42, username="vasya", task_id=3, task_name="Отзыв <b>", reward=30, status="pending",
            file_id="F", file_kind="photo", moderator_id=None, moderator_username=None, moderator_name=None,
        )
        text = task_review.request_text(r, 3)
        for part in ("Выполнение задания #7", "Отзыв &lt;b&gt;", "@vasya", "<code>42</code>", "30🍬", "Скриншотов:</b> 3"):
            self.assertIn(part, text)

    def test_request_text_shows_comment_instead_of_task_name(self):
        from app import repo, task_review

        r = repo.TaskReview(
            id=7, bot_id=1, user_id=42, username="vasya", task_id=3, task_name="Отзыв", reward=30, status="pending",
            file_id="F", file_kind="photo", moderator_id=None, moderator_username=None, moderator_name=None,
            comment="Подписаться на канал <и> оставить отзыв",
        )
        text = task_review.request_text(r, 2)
        self.assertIn("<b>Задание:</b> Подписаться на канал &lt;и&gt; оставить отзыв", text)
        self.assertNotIn("Отзыв\n", text)   # название заменено комментарием
        r.comment = "   "
        self.assertIn("<b>Задание:</b> Отзыв\n", task_review.request_text(r, 2))   # пустой комментарий — как раньше

    def test_prefix_does_not_clash_with_other_handlers(self):
        from app import moderation, task_review, topup

        self.assertFalse(task_review.PREFIX.startswith(moderation.PREFIX))
        self.assertFalse(task_review.PREFIX.startswith(topup.CHECK_PREFIX))
        self.assertFalse(topup.CHECK_PREFIX.startswith(task_review.PREFIX))
        cfg = tasks.generate_config("clash")
        self.assertFalse(any("tkchk:ok:1".startswith(p) for p in tasks.PREFIXES))
        self.assertFalse(any(cfg.cb["open"].startswith(p) for p in ("sh:", "gl:", "sp:", "sk:", "sb:", "sx:", "gb:", "gp:", "gk:", "cap:", "mod:", "tkchk:", "tpchk")))


if __name__ == "__main__":
    import unittest.mock  # noqa: F401
    unittest.main()
