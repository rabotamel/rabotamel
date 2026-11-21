"""«Предложка»: проверка видео, меню, кнопки галереи, модерация (в т. ч. гонка), покупка видео из пула.
aiogram и БД подменяются заглушками (см. test_handlers)."""
import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import test_handlers as th  # noqa: E402  (ставит заглушки aiogram и даёт импорт app.handlers)
from test_gallery import _FakeAiogramTypes  # noqa: E402

from app import deco, gallery, greeting, messaging, moderation, shop_buttons, suggest, suggest_ui, tasks, video_pool  # noqa: E402

handlers = th.handlers
repo = handlers.repo
NS = types.SimpleNamespace


def video(duration, file_id="F1"):
    return NS(duration=duration, file_id=file_id, file_unique_id="U" + file_id)


class BadRequest(handlers.TelegramBadRequest):
    def __init__(self, text="bad"):
        Exception.__init__(self, text)


class Forbidden(handlers.TelegramForbiddenError):
    def __init__(self, text="blocked"):
        Exception.__init__(self, text)


# ---------------------------------------------------------------- чистые функции

class CheckTests(unittest.TestCase):
    def test_limits(self):
        self.assertEqual(suggest.check(NS(video=None)), suggest.MSG_NOT_VIDEO)
        self.assertEqual(suggest.check(NS()), suggest.MSG_NOT_VIDEO)
        self.assertEqual(suggest.check(NS(video=video(29))), suggest.MSG_TOO_SHORT)
        self.assertIsNone(suggest.check(NS(video=video(30))))
        self.assertIsNone(suggest.check(NS(video=video(3600))))
        self.assertEqual(suggest.check(NS(video=video(3601))), suggest.MSG_TOO_LONG)

    def test_menu_text(self):
        for i in range(200):
            cfg = suggest_ui.generate_config(f"d{i}")
            text = cfg.render_menu()
            self.assertTrue(text.startswith(f"<b>{cfg.label}</b>\n\n{cfg.mark} "))
            self.assertTrue(text.endswith(f" {cfg.mark}"))
            self.assertIn("+2🍬", text)  # ключевое условие есть в каждом варианте


class GalleryHintTests(unittest.TestCase):
    def test_hint_is_between_description_and_balance_with_current_title(self):
        for i in range(300):
            g, c, d = gallery.generate_config(f"h{i}"), suggest_ui.generate_config(f"h{i}"), deco.generate_config(f"h{i}")
            text = gallery.render(g, d, "Галерея", "Привет\n🍭 Баланс: {balance}🍬", 90, c.name)
            parts = text.split("\n\n")
            self.assertEqual(len(parts), 4)
            self.assertEqual(parts[0], "<b>Галерея</b>")
            self.assertEqual(parts[1], d.wrap(g.description))                   # описание на своём месте
            self.assertIn(f"«{c.name}»", parts[2])                               # актуальное название предложки
            self.assertNotIn("{proposal_title}", parts[2])
            self.assertFalse(any(ch.isdigit() for ch in parts[2]))               # без количества конфет
            self.assertEqual(parts[3], "🍭 Баланс: 90🍬")                        # баланс внизу

    def test_name_matches_the_button_on_screen(self):
        for i in range(300):
            c = suggest_ui.generate_config(f"n{i}")
            self.assertEqual(c.label, f"{suggest_ui.LEFT_EMOJI[c.left]} {c.name} {suggest_ui.RIGHT_EMOJI[c.right]}")
            self.assertEqual(c.name, suggest_ui.TITLES[c.title])

    def test_hint_pool_is_deterministic_and_varies_between_bots(self):
        gs = [gallery.generate_config(f"x{i}") for i in range(300)]
        self.assertEqual(gs[7], gallery.generate_config("x7"))
        self.assertEqual({g.hint for g in gs}, set(gallery.HINTS))
        for g in gs[:50]:
            self.assertEqual(gallery.GalleryConfig.from_json(g.to_json()), g)

    def test_old_saved_config_without_hint_gets_stable_variant(self):
        g = gallery.generate_config("old")
        raw = json.loads(g.to_json())
        del raw["hint"]
        a = gallery.GalleryConfig.from_json(json.dumps(raw, ensure_ascii=False))
        b = gallery.GalleryConfig.from_json(json.dumps(raw, ensure_ascii=False))
        self.assertEqual(a.hint_template, b.hint_template)
        self.assertIn(a.hint_template, gallery.HINTS)
        self.assertEqual(a.buy_label, g.buy_label)  # остальное не тронуто

    def test_title_is_html_escaped(self):
        g = gallery.generate_config("e")
        self.assertIn("&lt;b&gt;", gallery.hint_line(g, "<b>"))


class SuggestStaticEmojiTests(unittest.TestCase):
    def test_button_and_menu_emoji_are_the_same_for_every_bot(self):
        for i in range(300):
            c = suggest_ui.generate_config(f"se{i}")
            self.assertTrue(c.label.startswith("📩 ") and c.label.endswith(" 🤍"))      # кнопка: 📩 … 🤍
            menu = c.render_menu()
            self.assertTrue(menu.startswith(f"<b>{c.label}</b>\n\n💫 "))               # заголовок 📩 … 🤍, текст 💫 …
            self.assertTrue(menu.endswith(" 💫"))                                          # … 💫
            self.assertIn("+2🍬", menu)

    def test_texts_by_seed_are_unchanged_by_the_emoji_switch(self):
        # значения сняты ДО перехода на статичные эмодзи
        self.assertEqual((suggest_ui.generate_config("seed0").title, suggest_ui.generate_config("seed0").body), (0, 4))
        self.assertEqual((suggest_ui.generate_config("seed1").title, suggest_ui.generate_config("seed1").body), (1, 2))
        self.assertEqual((suggest_ui.generate_config("seed2").title, suggest_ui.generate_config("seed2").body), (4, 1))

    def test_saved_config_with_old_random_emoji_is_normalized(self):
        c = suggest_ui.generate_config("legacy")
        raw = json.loads(c.to_json())
        raw.update(label="📥 Предложить видео ✨", mark="🌟", left=1, right=1, deco=2)
        n = suggest_ui.SuggestConfig.from_json(json.dumps(raw, ensure_ascii=False))
        self.assertEqual(n.label, "📩 Предложить видео 🤍")
        self.assertEqual(n.name, "Предложить видео")          # название для подсказки в галерее осталось
        self.assertEqual(n.mark, "💫")
        self.assertEqual((n.left, n.right, n.deco), (0, 0, 0))
        self.assertEqual(suggest_ui.SuggestConfig.from_json(n.to_json()), n)


class SuggestUiTests(unittest.TestCase):
    def test_pools_match_the_spec(self):
        self.assertEqual(suggest_ui.TITLES, ("Предложка", "Предложить видео", "Прислать видео", "Предложить контент", "Поделиться видео"))
        self.assertEqual(suggest_ui.LEFT_EMOJI, ("📩",))   # эмодзи статичны
        self.assertEqual(suggest_ui.RIGHT_EMOJI, ("🤍",))
        self.assertEqual(suggest_ui.DECO_EMOJI, ("💫",))
        self.assertEqual(len(suggest_ui.BODIES), 5)

    def test_deterministic_per_seed_and_json_roundtrip(self):
        for i in range(100):
            a, b = suggest_ui.generate_config(f"tok{i}"), suggest_ui.generate_config(f"tok{i}")
            self.assertEqual(a, b)  # один бот — один вариант при любом числе перезапусков
            self.assertEqual(suggest_ui.SuggestConfig.from_json(a.to_json()), a)

    def test_every_pool_value_is_reachable_and_independent(self):
        cfgs = [suggest_ui.generate_config(f"s{i}") for i in range(600)]
        self.assertEqual({c.title for c in cfgs}, set(range(5)))
        self.assertEqual({c.body for c in cfgs}, set(range(5)))
        self.assertEqual({(c.left, c.right, c.deco) for c in cfgs}, {(0, 0, 0)})   # эмодзи статичны
        self.assertEqual(len({(c.title, c.body) for c in cfgs}), 25)               # название и описание — независимо

    def test_label_shape_and_candy_untouched(self):
        for i in range(300):
            c = suggest_ui.generate_config(f"l{i}")
            self.assertEqual(c.label, f"{suggest_ui.LEFT_EMOJI[c.left]} {suggest_ui.TITLES[c.title]} {suggest_ui.RIGHT_EMOJI[c.right]}")
            self.assertNotIn("🍬", c.label + c.mark)
            self.assertNotIn("---", c.label + c.text)

    def test_new_bot_avoids_taken_combinations(self):
        # эмодзи статичны, поэтому различаются только название и описание: всего 5 × 5 = 25 комбинаций
        total = len(suggest_ui.TITLES) * len(suggest_ui.BODIES)
        taken = set()
        for i in range(total):
            c = suggest_ui.generate_config(f"bot{i}", taken)
            self.assertNotIn(c.signature(), taken)
            taken.add(c.signature())
        self.assertEqual(len(taken), total)
        # комбинации кончились — бот всё равно получает рабочий вариант (повтор допустим, падения нет)
        suggest_ui.generate_config("one-more", taken).validate()

    def test_registered_in_texts_and_gallery_uses_its_label(self):
        from app import texts
        self.assertIn("suggest", [k.key for k in texts.KINDS])
        c, g = suggest_ui.generate_config("z"), gallery.generate_config("z")
        rows = gallery.layout_rows(g, c.label)
        self.assertEqual([r[0][0] for r in rows][-2:], [c.label, "⬅️ Назад"])
        with _FakeAiogramTypes():
            kb = gallery.build_keyboard(g, c.label)
        self.assertEqual(kb.inline_keyboard[2][0].text, c.label)
        self.assertEqual(kb.inline_keyboard[2][0].callback_data, g.suggest_cb)
        self.assertEqual(getattr(kb.inline_keyboard[2][0], "style", None), "primary")


class GalleryButtonsTests(unittest.TestCase):
    def test_suggest_button_is_before_back(self):
        for i in range(100):
            rows = gallery.layout_rows(gallery.generate_config(f"s{i}"))
            labels = [r[0][0] for r in rows]
            self.assertEqual(labels[-2:], ["📩 Предложка 🤍", "⬅️ Назад"])
            self.assertEqual(len(rows), 4)

    def test_callbacks_unique_valid_and_parsed(self):
        seen = set()
        for i in range(500):
            c = gallery.generate_config(f"u{i}")
            cbs = [c.buy_cb, c.premium_cb, c.back_cb, c.suggest_cb, c.suggest_back_cb]
            for cb in cbs:
                self.assertLessEqual(len(cb.encode()), 64)
                self.assertTrue(cb.isascii())
                self.assertNotIn(cb, seen)
                seen.add(cb)
            self.assertEqual(
                [gallery.parse_action(c, cb) for cb in cbs],
                ["buy", "premium", "back", "suggest", "suggest_back"],
            )
        a, b = gallery.generate_config("a"), gallery.generate_config("b")
        self.assertIsNone(gallery.parse_action(a, b.suggest_cb))

    def test_old_saved_config_without_suggest_still_works(self):
        c = gallery.generate_config("old")
        raw = json.loads(c.to_json())
        self.assertNotIn("suggest_cb", raw)  # в БД ничего нового не пишется
        restored = gallery.GalleryConfig.from_json(json.dumps(raw, ensure_ascii=False))
        self.assertEqual(restored.suggest_cb, c.suggest_cb)  # и callback_data стабилен между запусками

    def test_video_keyboard_has_exactly_buy_and_back(self):
        c = gallery.generate_config("v")
        self.assertEqual(gallery.video_rows(c), [[(c.buy_label, c.buy_cb)], [("⬅️ Назад", c.back_cb)]])
        with _FakeAiogramTypes():
            kb = gallery.build_video_keyboard(c)
            sk = gallery.build_suggest_keyboard(c)
        self.assertEqual([[b.text for b in r] for r in kb.inline_keyboard], [[c.buy_label], ["⬅️ Назад"]])
        self.assertEqual([[b.callback_data for b in r] for r in sk.inline_keyboard], [[c.suggest_back_cb]])
        self.assertEqual(sk.inline_keyboard[0][0].text, "⬅️ Назад")
        self.assertEqual(sk.inline_keyboard[0][0].style, "danger")   # «Назад» в меню предложки — красная


class ModerationFormatTests(unittest.TestCase):
    def test_callbacks_roundtrip(self):
        for sub_id in (1, 123456789012):
            for key, action in moderation.ACTIONS.items():
                data = moderation.callback(key, sub_id)
                self.assertLessEqual(len(data.encode()), 64)
                self.assertEqual(moderation.parse(data), (action, sub_id))
        for bad in (None, "", "mod:", "mod:x:1", "mod:a:", "mod:a:x", "gb:1"):
            self.assertIsNone(moderation.parse(bad))

    def test_three_buttons(self):
        rows = moderation.keyboard_rows(7)
        self.assertEqual([r[0][0] for r in rows], ["✅ Принять", "❌ Отклонить", "🚫 Заблокировать"])
        self.assertEqual(len(rows), 3)

    def test_status_lines_name_the_moderator(self):
        base = dict(id=1, user_id=5, author_username="au", duration=75, status="approved")
        sub = NS(**base, decision="approve", moderator_id=9, moderator_username="mod_a", moderator_name="Имя")
        self.assertIn("@mod_a", moderation.status_line(sub))
        self.assertIn("принята", moderation.status_line(sub))
        no_name = NS(**base, decision="reject", moderator_id=9, moderator_username=None, moderator_name="Иван <b>")
        line = moderation.status_line(no_name)
        self.assertIn("Иван &lt;b&gt; (ID 9)", line)
        only_id = NS(**base, decision="block", moderator_id=9, moderator_username=None, moderator_name=None)
        self.assertIn("ID 9", moderation.status_line(only_id))
        self.assertIn("заблокирован", moderation.status_line(only_id))
        self.assertIn("автор заблокирован", moderation.status_line(NS(**base, decision="block_auto", moderator_id=9, moderator_username="m", moderator_name=None)))

    def test_duration(self):
        self.assertEqual(moderation.fmt_duration(75), "1:15")
        self.assertEqual(moderation.fmt_duration(3600), "1:00:00")


class VideoPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = video_pool.VideoPool()
        self._orig = getattr(repo, "load_approved_pool", None)
        self.items = [(i, f"f{i}") for i in range(1, 6)]

        async def load(bot_id):
            return list(self.items)

        repo.load_approved_pool = load

    def tearDown(self):
        if self._orig is not None:
            repo.load_approved_pool = self._orig
        else:
            del repo.load_approved_pool

    def test_no_repeats_until_pool_is_used_up(self):
        async def go():
            seen = []
            for _ in range(5):
                sub_id, _f = await self.pool.pick(1, 7)
                self.pool.mark_shown(1, 7, sub_id)
                seen.append(sub_id)
            self.assertEqual(sorted(seen), [1, 2, 3, 4, 5])
            for _ in range(30):  # дальше хотя бы не два одинаковых подряд
                sub_id, _f = await self.pool.pick(1, 7)
                self.assertNotEqual(sub_id, seen[-1])
                self.pool.mark_shown(1, 7, sub_id)
                seen.append(sub_id)

        asyncio.run(go())

    def test_single_video_pool_and_empty_pool(self):
        async def go():
            self.items[:] = [(1, "only")]
            self.pool.mark_shown(1, 7, 1)
            self.assertEqual(await self.pool.pick(1, 7), (1, "only"))
            self.items[:] = []
            self.pool.invalidate()
            self.assertIsNone(await self.pool.pick(1, 7))

        asyncio.run(go())

    def test_bad_file_is_skipped_and_new_approved_added(self):
        async def go():
            self.items[:] = [(1, "a"), (2, "b")]
            self.pool.mark_bad(1)
            for _ in range(10):
                self.assertEqual((await self.pool.pick(1, 7))[0], 2)
            self.pool.add(1, 3, "c")  # пул уже загружен — новое видео доступно сразу
            got = {(await self.pool.pick(1, u))[0] for u in range(100, 140)}
            self.assertEqual(got, {2, 3})

        asyncio.run(go())


# ---------------------------------------------------------------- сценарии бота

class _RemovableStats(th.FakeStats):
    def __init__(self, known):
        super().__init__(known)
        self.removed, self.flushed = [], False

    def remove(self, uid):
        self.removed.append(uid)
        existed = uid in self.known
        self.known.discard(uid)
        return existed

    async def flush(self):
        self.flushed = True


class FakeDB:
    """Память вместо БД; decide_submission между проверкой статуса и записью не ждёт — как один UPDATE."""

    NAMES = (
        "list_moderators", "is_author_blocked", "create_submission", "get_submissions", "save_submission_message",
        "submission_messages", "decide_submission", "spend_candies", "add_candies", "get_candies",
        "load_approved_pool", "bind_moderator", "get_premium", "buy_premium",
    )

    def __init__(self):
        self.subs, self.msgs, self.blocked = {}, {}, set()
        self.candies = {(1, 5): 10}
        self.pool = []
        self.mods = [NS(user_id=100, username="mod_a"), NS(user_id=200, username=None)]
        self.rewards = 0
        self.premium = {}   # user_id -> (until, forever)

    def sub(self, sub_id):
        return NS(**self.subs[sub_id])

    def install(self):
        db = self
        self._saved = {n: getattr(repo, n, None) for n in self.NAMES}

        async def list_moderators():
            return db.mods

        async def is_author_blocked(uid):
            return uid in db.blocked

        async def create_submission(bot_id, uid, username, file_id, unique, duration):
            sid = len(db.subs) + 1
            db.subs[sid] = dict(id=sid, bot_id=bot_id, user_id=uid, author_username=username, file_id=file_id,
                                duration=duration, status="pending", decision=None, moderator_id=None,
                                moderator_username=None, moderator_name=None)
            return db.sub(sid)

        async def get_submissions(ids):
            return [db.sub(i) for i in ids if i in db.subs]

        async def save_submission_message(sid, chat, mid):
            db.msgs.setdefault(sid, {})[chat] = mid

        async def submission_messages(ids):
            return {i: list(db.msgs.get(i, {}).items()) for i in ids}

        async def decide_submission(bot_id, sid, action, mod_id, mod_username, mod_name, reward=0):
            s = db.subs.get(sid)
            if s is None or s["bot_id"] != bot_id or s["status"] != "pending":
                return None
            s.update(status="approved" if action == "approve" else "rejected", decision=action,
                     moderator_id=mod_id, moderator_username=mod_username, moderator_name=mod_name)
            rewarded, auto = False, []
            if action == "approve" and (bot_id, s["user_id"]) in db.candies:
                db.candies[(bot_id, s["user_id"])] += reward
                rewarded = True
                db.rewards += 1
                db.pool.append((sid, s["file_id"]))
            if action == "block":
                db.blocked.add(s["user_id"])
                for o in db.subs.values():
                    if o["user_id"] == s["user_id"] and o["status"] == "pending":
                        o.update(status="rejected", decision="block_auto", moderator_id=mod_id,
                                 moderator_username=mod_username, moderator_name=mod_name)
                        auto.append(o["id"])
            return NS(sub=db.sub(sid), rewarded=rewarded, auto_rejected=tuple(auto))

        async def spend_candies(bot_id, uid, amount):
            if db.candies.get((bot_id, uid), 0) < amount:
                return None
            db.candies[(bot_id, uid)] -= amount
            return db.candies[(bot_id, uid)]

        async def add_candies(bot_id, uid, delta):
            if (bot_id, uid) not in db.candies:
                return None
            db.candies[(bot_id, uid)] = max(0, db.candies[(bot_id, uid)] + delta)
            return db.candies[(bot_id, uid)]

        async def get_candies(bot_id, uid):
            return db.candies.get((bot_id, uid), 0)

        async def load_approved_pool(bot_id):
            return list(db.pool)

        async def get_premium(uid):
            return db.premium.get(uid, (None, False))

        async def buy_premium(bot_id, uid, price, days):
            if db.candies.get((bot_id, uid), 0) < price:
                return None
            db.candies[(bot_id, uid)] -= price
            until, forever = db.premium.get(uid, (None, False))
            if days is None:
                until, forever = None, True
            elif not forever:
                base = until if until is not None and until > datetime.now(timezone.utc) else datetime.now(timezone.utc)
                until = base + timedelta(days=days)
            db.premium[uid] = (until, forever)
            return db.candies[(bot_id, uid)], until, forever

        async def bind_moderator(uid, username):
            for m in db.mods:
                if m.user_id is None and m.username == username:
                    m.user_id = uid

        for name, fn in locals().items():
            if name in self.NAMES:
                setattr(repo, name, fn)

    def uninstall(self):
        for n, fn in self._saved.items():
            if fn is None:
                if hasattr(repo, n):
                    delattr(repo, n)
            else:
                setattr(repo, n, fn)


class Bot:
    def __init__(self):
        self.next_id = 1000
        self.deleted, self.videos, self.edits, self.texts = [], [], [], []
        self.video_errors = []   # исключения, которые по очереди бросит send_video

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)

    async def send_message(self, chat_id, text, **kw):   # служебное сообщение с клавиатурой «Меню»
        self.next_id += 1
        return NS(message_id=self.next_id)

    async def send_video(self, chat_id, video, **kw):
        if self.video_errors:
            raise self.video_errors.pop(0)
        self.next_id += 1
        self.videos.append(dict(chat_id=chat_id, video=video, message_id=self.next_id, **kw))
        return NS(message_id=self.next_id)

    async def edit_message_caption(self, **kw):
        self.edits.append(kw)


class Flow(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.db.install()
        moderation.cache.invalidate()
        video_pool.pool.invalidate()
        video_pool.pool._recent.clear()
        video_pool.pool._bad.clear()
        self.bot = Bot()
        self.sent = []   # сообщения бота пользователю: (id, chat, text, markup)
        self.images = []  # картинка, с которой ушло каждое сообщение из self.sent (None — без картинки)
        self.send_error = None  # если задано — отправка падает с этой ошибкой

        async def fake_send(b, chat_id, text, image=None, reply_markup=None):
            if self.send_error is not None:
                raise self.send_error
            b.next_id += 1
            self.images.append(image)
            self.sent.append((b.next_id, chat_id, text, reply_markup))
            return NS(message_id=b.next_id)

        self._orig_send = messaging.send_message
        messaging.send_message = fake_send
        profile = NS(name="Бот", message_image_url="https://img.example/pic.jpg", token="1:SECRET")
        self.texts = NS(
            greeting=greeting.generate_config("t"), shop_buttons=shop_buttons.generate_config("t"),
            deco=deco.generate_config("t"), gallery=gallery.generate_config("t"),
            suggest=suggest_ui.generate_config("t"), tasks_menu=tasks.generate_config("t"), version=1)
        self.stats = _RemovableStats({5, 6, 777})
        self.router = handlers.build_router(self.stats, self.texts, profile)
        self.h = self.router.handlers if hasattr(self.router, "handlers") else {
            c.callback.__name__: c.callback
            for ev in (self.router.message, self.router.callback_query) for c in ev.handlers}

    def tearDown(self):
        messaging.send_message = self._orig_send
        self.db.uninstall()
        moderation.cache.invalidate()
        video_pool.pool.invalidate()

    # --- помощники
    def call(self, data, user=5, msg_id=10, is_video=False, username="u", name="Имя"):
        answers = []

        async def answer(text=None, show_alert=False, **k):
            answers.append((text, show_alert))

        message = NS(message_id=msg_id, chat=NS(id=user))
        if is_video:
            message.video = NS(duration=60)
        return NS(data=data, answer=answer, answers=answers,
                  from_user=NS(id=user, username=username, full_name=name), message=message)

    def msg(self, user=5, **kw):
        return NS(from_user=NS(id=user, username="author"), **kw)

    def arun(self, coro):
        return asyncio.run(coro)

    async def open_suggest(self, user=5):
        await self.h["on_gallery_button"](self.call(self.texts.gallery.suggest_cb, user=user), self.bot)

    # --- меню и приём видео
    def test_suggest_menu_replaces_gallery_and_back_returns(self):
        async def go():
            await self.h["on_gallery_button"](self.call(self.texts.gallery.suggest_cb, msg_id=10), self.bot)
            self.assertEqual(self.bot.deleted, [10])
            mid, _chat, text, markup = self.sent[-1]
            self.assertEqual(text, self.texts.suggest.render_menu())  # текст меню — вариант этого бота
            self.assertIn("+2🍬", text)
            self.assertEqual([[b.callback_data for b in r] for r in markup.inline_keyboard],
                             [[self.texts.gallery.suggest_back_cb]])
            await self.h["on_gallery_button"](self.call(self.texts.gallery.suggest_back_cb, msg_id=mid), self.bot)
            self.assertEqual(self.bot.deleted, [10, mid])
            self.assertIn(f"<b>{self.texts.shop_buttons.gallery_heading}</b>", self.sent[-1][2])  # снова меню галереи
            self.assertEqual(self.sent[-1][3].inline_keyboard[2][0].text, self.texts.suggest.label)  # кнопка этого бота
            self.assertIn(f"«{self.texts.suggest.name}»", self.sent[-1][2])  # и подсказка с тем же названием
            n = len(self.sent)
            await self.h["on_private_message"](self.msg(video=video(60)), self.bot)  # меню закрыто — не предложка
            self.assertEqual(len(self.sent), n)
            self.assertEqual(self.db.subs, {})

        self.arun(go())

    def test_validation_messages_and_no_cooldown(self):
        async def go():
            await self.open_suggest()
            n = len(self.sent)
            await self.h["on_private_message"](self.msg(video=None, text="привет"), self.bot)
            await self.h["on_private_message"](self.msg(video=video(29)), self.bot)
            await self.h["on_private_message"](self.msg(video=video(3601)), self.bot)
            self.assertEqual([m[2] for m in self.sent[n:]],
                             [suggest.MSG_NOT_VIDEO, suggest.MSG_TOO_SHORT, suggest.MSG_TOO_LONG])
            self.assertEqual(self.db.subs, {})
            # без кулдауна: подряд пять видео — пять заявок
            for i in range(5):
                await self.h["on_private_message"](self.msg(video=video(30 + i, f"F{i}")), self.bot)
            self.assertEqual(len(self.db.subs), 5)
            self.assertEqual(len(self.bot.videos), 5 * 2)  # каждая заявка ушла обоим модераторам

        self.arun(go())

    def test_replies_have_no_picture_and_valid_video_is_acknowledged(self):
        async def go():
            await self.open_suggest()
            n = len(self.sent)
            self.assertIsNotNone(self.images[n - 1])  # само меню предложки — с картинкой бота
            await self.h["on_private_message"](self.msg(video=None, text="привет"), self.bot)
            await self.h["on_private_message"](self.msg(video=video(29)), self.bot)
            await self.h["on_private_message"](self.msg(video=video(75)), self.bot)
            self.assertEqual([m[2] for m in self.sent[n:]],
                             [suggest.MSG_NOT_VIDEO, suggest.MSG_TOO_SHORT, "✅ Ваше видео принято на рассмотрение."])
            self.assertEqual(self.images[n:], [None, None, None])
            self.assertEqual(len(self.db.subs), 1)

        self.arun(go())

    def test_blocked_author_gets_no_acknowledgement(self):
        async def go():
            await self.open_suggest()
            self.db.blocked.add(5)
            n = len(self.sent)
            await self.h["on_private_message"](self.msg(video=video(75)), self.bot)
            self.assertEqual(len(self.sent), n)

        self.arun(go())

    def test_user_who_blocked_bot_is_removed_from_db(self):
        async def go():
            self.send_error = Forbidden("Forbidden: bot was blocked by the user")
            await self.h["on_start"](NS(from_user=NS(id=5, username="u")), self.bot)
            self.assertEqual(self.stats.removed, [5])
            self.assertTrue(self.stats.flushed)
            self.assertFalse(self.stats.has(5))

        self.arun(go())

    def test_other_forbidden_errors_keep_user(self):
        async def go():
            for text in ("Forbidden: user is deactivated", "Forbidden: bot can't initiate conversation with a user"):
                self.send_error = Forbidden(text)
                await self.h["on_start"](NS(from_user=NS(id=6, username="u")), self.bot)
            self.assertEqual(self.stats.removed, [])
            self.assertTrue(self.stats.has(6))

        self.arun(go())

    def test_blocking_event_removes_user(self):
        async def go():
            await self.h["on_blocked_by_user"](NS(chat=NS(id=777, type="private")))
            self.assertEqual(self.stats.removed, [777])
            self.assertTrue(self.stats.flushed)

        self.arun(go())

    def test_video_goes_to_all_moderators_protected_with_three_buttons(self):
        async def go():
            await self.open_suggest()
            await self.h["on_private_message"](self.msg(video=video(75)), self.bot)
            self.assertEqual({v["chat_id"] for v in self.bot.videos}, {100, 200})
            for v in self.bot.videos:
                self.assertIs(v["protect_content"], True)
                self.assertEqual(len(v["reply_markup"].inline_keyboard), 3)
                self.assertIn("1:15", v["caption"])
            self.assertEqual(self.db.subs[1]["status"], "pending")
            self.assertEqual(set(self.db.msgs[1]), {100, 200})

        self.arun(go())

    def test_blocked_author_is_ignored_silently(self):
        async def go():
            await self.open_suggest()
            self.db.blocked.add(5)
            n = len(self.sent)
            await self.h["on_private_message"](self.msg(video=video(60)), self.bot)
            await self.h["on_private_message"](self.msg(video=None, text="x"), self.bot)
            self.assertEqual(self.db.subs, {})
            self.assertEqual(self.bot.videos, [])
            self.assertEqual(len(self.sent), n)

        self.arun(go())

    def test_moderator_unreachable_does_not_break_delivery(self):
        async def go():
            self.bot.video_errors = [Forbidden()]  # первый модератор не запускал бота
            await self.open_suggest()
            await self.h["on_private_message"](self.msg(video=video(60)), self.bot)
            self.assertEqual(len(self.bot.videos), 1)
            self.assertEqual(len(self.db.subs), 1)

        self.arun(go())

    def test_start_closes_suggest_menu(self):
        async def go():
            await self.open_suggest()
            menu_id = self.sent[-1][0]
            await self.h["on_start"](self.msg(), self.bot)
            self.assertIn(menu_id, self.bot.deleted)
            n = len(self.sent)
            await self.h["on_private_message"](self.msg(video=None, text="x"), self.bot)
            self.assertEqual(len(self.sent), n)

        self.arun(go())

    # --- модерация
    async def submit(self, user=5):
        await self.open_suggest(user)
        await self.h["on_private_message"](self.msg(user=user, video=video(60)), self.bot)
        return max(self.db.subs)

    def test_approve_rewards_once_and_syncs_all_moderators(self):
        async def go():
            sid = await self.submit()
            data = moderation.callback("a", sid)
            c1 = self.call(data, user=100, username="mod_a")
            await self.h["on_moderation"](c1, self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 12)               # +2🍬
            self.assertEqual(self.db.pool, [(sid, "F1")])               # видео в пуле покупки
            self.assertEqual(c1.answers, [(None, False)])
            self.assertEqual({e["chat_id"] for e in self.bot.edits}, {100, 200})
            for e in self.bot.edits:                                    # кнопки у всех убраны, подпись обновлена
                self.assertEqual(e["reply_markup"].inline_keyboard, [])
                self.assertIn("принята модератором @mod_a", e["caption"])
            # второй модератор нажимает на устаревшее сообщение: повторного начисления нет
            c2 = self.call(moderation.callback("a", sid), user=200, username=None, name="Второй")
            await self.h["on_moderation"](c2, self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 12)
            self.assertEqual(c2.answers, [("Заявка уже обработана", False)])
            await self.h["on_moderation"](self.call(moderation.callback("r", sid), user=200), self.bot)
            self.assertEqual(self.db.subs[sid]["status"], "approved")

        self.arun(go())

    def test_simultaneous_clicks_pay_only_once(self):
        async def go():
            sid = await self.submit()
            calls = [self.call(moderation.callback("a", sid), user=u, username=f"m{u}") for u in (100, 200)]
            await asyncio.gather(*(self.h["on_moderation"](c, self.bot) for c in calls))
            self.assertEqual(self.db.rewards, 1)
            self.assertEqual(self.db.candies[(1, 5)], 12)
            self.assertEqual(sorted(a[0] is None for c in calls for a in c.answers), [False, True])

        self.arun(go())

    def test_reject_gives_nothing_and_stays_out_of_pool(self):
        async def go():
            sid = await self.submit()
            await self.h["on_moderation"](self.call(moderation.callback("r", sid), user=100, username="mod_a"), self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 10)
            self.assertEqual(self.db.pool, [])
            self.assertEqual(self.db.subs[sid]["status"], "rejected")
            self.assertTrue(all("отклонена модератором @mod_a" in e["caption"] for e in self.bot.edits))

        self.arun(go())

    def test_block_blocks_author_and_closes_his_pending_requests(self):
        async def go():
            first = await self.submit()
            second = await self.submit()
            await self.h["on_moderation"](self.call(moderation.callback("b", first), user=200, username=None, name="Иван"), self.bot)
            self.assertIn(5, self.db.blocked)
            self.assertEqual(self.db.subs[second]["status"], "rejected")   # остальные ролики автора закрыты
            self.assertEqual(self.db.candies[(1, 5)], 10)
            captions = [e["caption"] for e in self.bot.edits]
            self.assertTrue(any("Автор заблокирован модератором Иван (ID 200)" in c for c in captions))
            self.assertTrue(any(f"Заявка #{second}" in c and "автор заблокирован" in c for c in captions))
            n = len(self.bot.videos)                                       # и новые видео не приходят
            await self.h["on_private_message"](self.msg(video=video(60)), self.bot)
            self.assertEqual(len(self.bot.videos), n)

        self.arun(go())

    def test_not_a_moderator_is_denied(self):
        async def go():
            sid = await self.submit()
            c = self.call(moderation.callback("a", sid), user=6, username="stranger")
            await self.h["on_moderation"](c, self.bot)
            self.assertEqual(c.answers, [("У вас нет доступа к модерации", True)])
            self.assertEqual(self.db.subs[sid]["status"], "pending")
            self.assertEqual(self.db.candies[(1, 5)], 10)

        self.arun(go())

    def test_moderator_added_by_username_gets_bound_on_first_message(self):
        async def go():
            self.db.mods = [NS(user_id=None, username="newmod")]
            moderation.cache.invalidate()
            self.assertEqual(await moderation.cache.recipients(), [])
            await self.h["on_private_message"](NS(from_user=NS(id=777, username="NewMod")), self.bot)
            self.assertEqual(await moderation.cache.recipients(), [777])

        self.arun(go())

    # --- покупка видео
    def test_buy_without_money_shows_alert(self):
        async def go():
            self.db.candies[(1, 5)] = 2
            self.db.pool = [(1, "V1")]
            c = self.call(self.texts.gallery.buy_cb)
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(c.answers, [("❌ У вас недостаточно средств", True)])
            self.assertEqual(self.bot.videos, [])
            self.assertEqual(self.db.candies[(1, 5)], 2)

        self.arun(go())

    def test_buy_sends_clean_protected_video_with_two_buttons(self):
        async def go():
            self.db.pool = [(1, "V1")]
            c = self.call(self.texts.gallery.buy_cb)
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(c.answers, [(None, False)])           # успех — без всплывающих уведомлений
            self.assertEqual(self.db.candies[(1, 5)], 7)           # -3🍬
            (v,) = self.bot.videos
            self.assertEqual((v["chat_id"], v["video"]), (5, "V1"))
            self.assertIs(v["protect_content"], True)
            self.assertEqual(v["caption"], gallery.balance_line(self.texts.greeting.template, "7"))   # подпись — строка баланса после списания
            self.assertEqual(v["parse_mode"], "HTML")
            kb = v["reply_markup"].inline_keyboard
            self.assertEqual([[b.text for b in r] for r in kb], [[self.texts.gallery.buy_label], ["⬅️ Назад"]])
            self.assertEqual([r[0].callback_data for r in kb], [self.texts.gallery.buy_cb, self.texts.gallery.back_cb])

        self.arun(go())

    # --- премиум
    def test_premium_menu_opens_from_gallery_and_back_returns(self):
        g = self.texts.gallery

        async def go():
            await self.h["on_gallery_button"](self.call(g.premium_cb, msg_id=10), self.bot)
            self.assertEqual(self.bot.deleted, [10])
            mid, _chat, text, markup = self.sent[-1]
            self.assertTrue(text.startswith(f"<b>{g.premium_label}</b>"))
            self.assertIn("🤷🏻‍♀️ У вас нет премиума", text)
            self.assertIn("бесплатно", text)
            self.assertEqual([r[0].text for r in markup.inline_keyboard],
                             ["💕 3 дня (650🍬) 👑", "🔥 Неделя (1250🍬) 👑", "💎 Безлимит (1750🍬) 👑", "⬅️ Назад"])
            self.assertEqual([r[0].callback_data for r in markup.inline_keyboard],
                             [f"{g.premium_cb}:3", f"{g.premium_cb}:7", f"{g.premium_cb}:forever", f"{g.premium_cb}:b"])
            self.assertEqual([r[0].style for r in markup.inline_keyboard], ["success"] * 3 + ["danger"])
            await self.h["on_gallery_button"](self.call(f"{g.premium_cb}:b", msg_id=mid), self.bot)
            self.assertEqual(self.bot.deleted, [10, mid])
            self.assertIn(f"<b>{self.texts.shop_buttons.gallery_heading}</b>", self.sent[-1][2])   # снова меню галереи
        self.arun(go())

    def test_premium_purchase_needs_money_then_charges_and_extends(self):
        g = self.texts.gallery

        async def go():
            self.db.candies[(1, 5)] = 600
            c = self.call(f"{g.premium_cb}:3")
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual((c.answers, self.db.premium), ([("❌ У вас недостаточно средств", True)], {}))
            self.db.candies[(1, 5)] = 2000
            c = self.call(f"{g.premium_cb}:3", msg_id=10)
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 1350)
            self.assertEqual(c.answers, [("❤️‍🔥 Вы успешно приобрели премиум на 3 дня.", True)])
            until, forever = self.db.premium[5]
            self.assertFalse(forever)
            self.assertAlmostEqual((until - datetime.now(timezone.utc)).total_seconds(), 3 * 86400, delta=60)
            self.assertIn("истечёт через 2д 23ч", self.sent[-1][2])              # экран обновлён: статус и новый баланс
            self.assertIn("1350🍬", self.sent[-1][2])
            await self.h["on_gallery_button"](self.call(f"{g.premium_cb}:7"), self.bot)   # продление: дни складываются
            self.assertAlmostEqual((self.db.premium[5][0] - datetime.now(timezone.utc)).total_seconds(), 10 * 86400, delta=60)
            self.db.candies[(1, 5)] = 5000
            c = self.call(f"{g.premium_cb}:forever")
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(self.db.premium[5], (None, True))
            self.assertIn("💎 Ваш премиум безлимитный", self.sent[-1][2])
            before = self.db.candies[(1, 5)]
            c = self.call(f"{g.premium_cb}:3")
            await self.h["on_gallery_button"](c, self.bot)                       # безлимит уже есть — ничего не списывается
            self.assertEqual((self.db.candies[(1, 5)], c.answers[0][1]), (before, True))
        self.arun(go())

    def test_foreign_or_bad_premium_callbacks_are_ignored(self):
        g = self.texts.gallery
        self.assertIsNone(gallery.parse_premium_key(g, f"{g.premium_cb}:999"))
        self.assertIsNone(gallery.parse_premium_key(g, "gp:other:3"))
        self.assertEqual(gallery.parse_action(g, f"{g.premium_cb}:forever"), "premium_buy")
        self.assertEqual(gallery.parse_action(g, f"{g.premium_cb}:b"), "premium_back")
        self.assertEqual(gallery.parse_action(g, g.premium_cb), "premium")

        async def go():
            self.db.candies[(1, 5)] = 5000
            await self.h["on_gallery_button"](self.call(f"{g.premium_cb}:zzz"), self.bot)
            self.assertEqual(self.db.premium, {})
        self.arun(go())

    def test_premium_user_gets_free_unprotected_video(self):
        g = self.texts.gallery

        async def go():
            self.db.pool = [(1, "V1")]
            self.db.premium[5] = (None, True)
            c = self.call(g.buy_cb)
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 10)                       # ничего не списано
            (v,) = self.bot.videos
            self.assertIs(v["protect_content"], False)                           # можно пересылать и сохранять
            self.assertIn("(0🍬)", v["reply_markup"].inline_keyboard[0][0].text)
            self.assertEqual(v["caption"], gallery.balance_line(self.texts.greeting.template, "10"))
            # даже при нуле на балансе
            self.db.candies[(1, 5)] = 0
            self.db.pool = [(1, "V1")]
            await self.h["on_gallery_button"](self.call(g.buy_cb), self.bot)
            self.assertEqual(len(self.bot.videos), 2)
            # меню галереи показывает цену 0🍬 только премиуму
            await self.h["on_gallery_button"](self.call(g.premium_cb), self.bot)
            await self.h["on_gallery_button"](self.call(f"{g.premium_cb}:b"), self.bot)
            self.assertIn("(0🍬)", self.sent[-1][3].inline_keyboard[0][0].text)
        self.arun(go())

    def test_expired_premium_is_not_premium(self):
        self.assertFalse(gallery.premium_active(datetime.now(timezone.utc) - timedelta(seconds=1), False))
        self.assertTrue(gallery.premium_active(datetime.now(timezone.utc) + timedelta(minutes=1), False))
        self.assertFalse(gallery.premium_active(None, False))
        self.assertEqual(gallery.format_left(timedelta(days=2, hours=3, minutes=4)), "2д 3ч 4м")

    def test_purchased_videos_are_not_deleted_and_back_opens_new_gallery(self):
        async def go():
            self.db.pool = [(1, "V1"), (2, "V2")]
            await self.h["on_shop_button"](self.call(self.texts.shop_buttons.gallery_cb, msg_id=10), self.bot)
            gallery_id = self.sent[-1][0]
            for _ in range(2):  # покупки одна за другой, в том числе кнопкой под видео
                await self.h["on_gallery_button"](self.call(self.texts.gallery.buy_cb, msg_id=gallery_id), self.bot)
            video_id = self.bot.videos[-1]["message_id"]
            self.assertNotIn(video_id, self.bot.deleted)
            self.assertNotIn(self.bot.videos[0]["message_id"], self.bot.deleted)
            n = len(self.sent)
            await self.h["on_gallery_button"](self.call(self.texts.gallery.back_cb, msg_id=video_id, is_video=True), self.bot)
            self.assertIn(gallery_id, self.bot.deleted)             # меню, из которого открывали видео, удалено
            self.assertNotIn(video_id, self.bot.deleted)            # видео осталось
            self.assertEqual(len(self.sent), n + 1)                 # новое меню галереи
            new_menu = self.sent[-1][0]
            self.assertIn(f"<b>{self.texts.shop_buttons.gallery_heading}</b>", self.sent[-1][2])
            await self.h["on_start"](self.msg(), self.bot)          # /start тоже убирает меню галереи
            self.assertIn(new_menu, self.bot.deleted)
            self.assertNotIn(video_id, self.bot.deleted)

        self.arun(go())

    def test_empty_pool_and_send_failures_refund(self):
        async def go():
            c = self.call(self.texts.gallery.buy_cb)
            await self.h["on_gallery_button"](c, self.bot)           # пул пуст
            self.assertEqual(self.db.candies[(1, 5)], 10)
            self.assertTrue(c.answers[0][1])
            self.db.pool = [(1, "V1")]
            video_pool.pool.invalidate()
            self.bot.video_errors = [Forbidden()]
            await self.h["on_gallery_button"](self.call(self.texts.gallery.buy_cb), self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 10)            # конфеты возвращены
            self.bot.video_errors = [BadRequest(), BadRequest(), BadRequest()]
            await self.h["on_gallery_button"](self.call(self.texts.gallery.buy_cb), self.bot)
            self.assertEqual(self.db.candies[(1, 5)], 10)

        self.arun(go())

    def test_broken_file_id_falls_back_to_another_video(self):
        async def go():
            self.db.pool = [(1, "V1"), (2, "V2")]
            self.bot.video_errors = [BadRequest("wrong file identifier")]
            c = self.call(self.texts.gallery.buy_cb)
            await self.h["on_gallery_button"](c, self.bot)
            self.assertEqual(len(self.bot.videos), 1)
            self.assertEqual(c.answers, [(None, False)])
            self.assertEqual(self.db.candies[(1, 5)], 7)             # списано один раз

        self.arun(go())


if __name__ == "__main__":
    unittest.main()
