"""Проверка скриншотов заданий в боте для чеков (app/task_review.py): доставка альбомом + кнопки админа.
БД и Telegram подменены, логика решения (кто выиграл, начисление, уведомление, блокировка) — настоящая."""
import asyncio
import types
import unittest
from unittest import mock

from app import repo, task_review, topup


def review(cid, **kw):
    base = dict(id=cid, bot_id=7, user_id=555, username="vasya", task_id=3, task_name="Отзыв", reward=25, status="pending",
                file_id=None, file_kind=None, moderator_id=None, moderator_username=None, moderator_name=None)
    base.update(kw)
    return repo.TaskReview(**base)


class FakeBot:
    """Любой метод Bot API: записывает вызов и возвращает «сообщение» с новым id."""

    def __init__(self):
        self.calls, self._n = [], 1000

    def __getattr__(self, name):
        async def method(*a, **k):
            self._n += 1
            self.calls.append((name, a, k))
            return types.SimpleNamespace(message_id=self._n)
        return method

    def names(self):
        return [c[0] for c in self.calls]


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.rows = {1: review(1)}
        self.files = {1: [("f1", "photo"), ("f2", "photo"), ("f3", "document")]}
        self.msgs, self.blocked, self.balance, self.notes, self.answers = {}, set(), {555: 100}, [], []
        self.check_bot, self.shop_bot = FakeBot(), FakeBot()

        async def get_task_reviews(ids):
            return [repo.TaskReview(**vars(self.rows[i])) for i in ids if i in self.rows]

        async def get_task_files(cid):
            return self.files.get(cid, [])

        async def decide_task(cid, bot_id, approve, mid, mu, mn):
            r = self.rows.get(cid)
            if r is None or r.status != "pending" or (bot_id is not None and r.bot_id != bot_id):
                return None
            r.status = "approved" if approve else "rejected"
            r.moderator_id, r.moderator_username, r.moderator_name = mid, mu, mn
            out = repo.TaskReview(**vars(r))
            if approve and r.user_id in self.balance:
                self.balance[r.user_id] += r.reward
                out.balance = self.balance[r.user_id]
            return out

        async def set_receipt_block(uid, blocked, by=None):
            self.blocked.add(uid)
            return True

        async def save_msg(cid, chat, mid):
            self.msgs.setdefault(cid, {})[chat] = mid

        async def msgs_of(cid):
            return list(self.msgs.get(cid, {}).items())

        async def pending(limit=50):
            return [r for r in self.rows.values() if r.status == "pending"]

        async def targets():
            return [11, 22]

        async def fetch(bot, file_id):
            return b"x", f"{file_id}.jpg", True

        async def is_admin(bot, chat_id, user_id):
            return user_id in (11, 22)

        async def answer(call, text=None, alert=False):
            self.answers.append((text, alert))

        async def notify(uid, key, name, reward):
            self.notes.append((uid, key, name, reward))

        patches = [
            mock.patch.object(repo, "get_task_reviews", get_task_reviews), mock.patch.object(repo, "get_task_files", get_task_files),
            mock.patch.object(repo, "decide_task", decide_task), mock.patch.object(repo, "set_receipt_block", set_receipt_block),
            mock.patch.object(repo, "save_task_review_message", save_msg), mock.patch.object(repo, "task_review_messages", msgs_of),
            mock.patch.object(repo, "list_pending_task_reviews", pending),
            mock.patch.object(topup, "CHECK_BOT_TOKEN", "token"), mock.patch.object(topup, "receipt_targets", targets),
            mock.patch.object(topup, "_ensure_check_bot", lambda: self.check_bot), mock.patch.object(topup, "_fetch_receipt", fetch),
            mock.patch.object(topup, "_is_admin", is_admin), mock.patch.object(topup, "_answer", answer),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        hook = task_review.register(7, self.shop_bot, notify)
        self.addCleanup(task_review.detach, 7, hook)

    def press(self, data, uid=11, chat=11):
        call = types.SimpleNamespace(
            data=data, from_user=types.SimpleNamespace(id=uid, username="adm", full_name="Adm"),
            message=types.SimpleNamespace(chat=types.SimpleNamespace(id=chat), message_id=self.msgs.get(1, {}).get(chat, 1)))
        asyncio.run(task_review.on_callback(call, self.check_bot))

    def deliver(self, cid=1, targets=None):
        return asyncio.run(task_review.deliver(cid, targets))

    def test_delivers_album_and_marked_message_with_buttons_to_every_admin(self):
        self.assertEqual(self.deliver(), 2)
        names = self.check_bot.names()
        self.assertEqual(names.count("send_media_group"), 2)   # 2 фото -> альбом (у каждого админа)
        self.assertEqual(names.count("send_document"), 2)      # 1 файл-картинка -> обычное сообщение
        sent = [c for c in self.check_bot.calls if c[0] == "send_message"]
        self.assertEqual(len(sent), 2)
        text = sent[0][1][1]
        for part in ("Выполнение задания #1", "Отзыв", "@vasya", "25🍬", "Скриншотов:</b> 3"):
            self.assertIn(part, text)
        kb = sent[0][2]["reply_markup"].inline_keyboard
        self.assertEqual([b.callback_data for row in kb for b in row], ["tkchk:ok:1", "tkchk:no:1", "tkchk:bl:1"])
        self.assertEqual(set(self.msgs[1]), {11, 22})

    def test_more_than_ten_screens_split_into_albums(self):
        self.files[1] = [(f"p{i}", "photo") for i in range(12)]
        self.deliver(targets=[11])
        groups = [c for c in self.check_bot.calls if c[0] == "send_media_group"]
        self.assertEqual([len(c[2]["media"]) for c in groups], [10, 2])

    def test_approve_credits_notifies_and_updates_all_admins(self):
        self.deliver()
        self.check_bot.calls.clear()
        self.press("tkchk:ok:1")
        self.assertEqual(self.balance[555], 125)
        self.assertEqual(self.notes, [(555, "approved", "Отзыв", 25)])
        edited = [c[2]["chat_id"] for c in self.check_bot.calls if c[0] == "edit_message_text"]
        self.assertEqual(sorted(edited), [11, 22])
        # второй админ жмёт «Отклонить» -> уже обработана, повторного начисления/уведомления нет
        self.answers.clear()
        self.press("tkchk:no:1", uid=22, chat=22)
        self.assertEqual(self.answers, [("Заявка уже обработана.", True)])
        self.assertEqual((self.balance[555], len(self.notes)), (125, 1))

    def test_reject_notifies_without_credit(self):
        self.deliver()
        self.press("tkchk:no:1")
        self.assertEqual((self.rows[1].status, self.balance[555]), ("rejected", 100))
        self.assertEqual(self.notes, [(555, "rejected", "Отзыв", 25)])

    def test_block_rejects_silently_and_blocks_user(self):
        self.deliver()
        self.press("tkchk:bl:1")
        self.assertEqual(self.rows[1].status, "rejected")
        self.assertIn(555, self.blocked)
        self.assertEqual((self.notes, self.balance[555]), ([], 100))
        # уже разобранную заявку блокировка тоже касается
        self.blocked.clear()
        self.press("tkchk:bl:1")
        self.assertIn(555, self.blocked)

    def test_only_admins_can_decide(self):
        self.deliver()
        self.press("tkchk:ok:1", uid=99)
        self.assertEqual(self.rows[1].status, "pending")
        self.assertEqual(self.answers[-1][1], True)
        self.assertEqual(self.notes, [])

    def test_approve_without_user_in_db_is_flagged_and_not_notified(self):
        self.balance.clear()
        self.deliver()
        self.check_bot.calls.clear()
        self.press("tkchk:ok:1")
        self.assertEqual(self.notes, [])
        text = [c[1][0] for c in self.check_bot.calls if c[0] == "edit_message_text"][0]
        self.assertIn("не начислены", text)

    def test_pending_are_resent_by_command(self):
        self.rows[2] = review(2)
        self.files[2] = [("g1", "photo")]
        self.assertEqual(asyncio.run(task_review.count_pending()), 2)
        self.assertEqual(asyncio.run(task_review.send_pending(self.check_bot, 11)), 2)

    def test_without_check_bot_token_task_stays_pending(self):
        with mock.patch.object(topup, "CHECK_BOT_TOKEN", ""):
            self.assertEqual(self.deliver(), 0)
        self.assertEqual(self.rows[1].status, "pending")


if __name__ == "__main__":
    unittest.main()
