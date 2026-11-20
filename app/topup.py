"""«🍭 Купить конфеты 🍬»: пополнение баланса картой, через CryptoBot и Telegram Stars.

Переписанная версия отдельного модуля topup.py (данные для оплаты те же: номер карты, курс 1🍬 = 0,8₽ = 0,01$ = 1⭐,
токены CryptoBot / Stars-бота, чат админов для чеков). Отличия от прежнего кода:
  * заказы лежат в таблице `topup_orders` (SQLAlchemy, как всё остальное в проекте), без своего пула asyncpg и
    без самописных миграций; начисление — один условный UPDATE (`repo.credit_topup_order`), двойного начисления нет;
  * баланс общий с остальным ботом: `bot_users.candies` ЭТОГО бота (заказ помнит bot_id);
  * один экран на всё меню: сообщение обновляется на месте (как раздел «Магазин»), картинка — картинка бота;
  * фоновый опрос CryptoBot работает, только пока есть неоплаченные счета (иначе база не просыпалась бы зря);
  * кнопки «Выдать / Отказать» под чеком доступны только администраторам чата, в который чек отправлен.

Переменные окружения (те же имена): CRYPTO_PAY_TOKEN (или CRYPTO_BOT_TOKEN), STARS_BOT_TOKEN, TOPUP_CARD_NUMBER,
TOPUP_ADMIN_GROUP_ID (или GROUP_ID, или ADMIN_CHAT_ID), а также CRYPTO_PAY_BASE_URL, CRYPTO_PAY_FIAT,
CRYPTO_PAY_ACCEPTED_ASSETS, TOPUP_INVOICE_EXPIRES, TOPUP_POLL_INTERVAL, STARS_BOT_TITLE, STARS_BOT_DESCRIPTION.
Всё, что связано с aiogram / aiohttp, импортируется внутри функций: чистая логика (тексты, расчёты) проверяется без них.
"""
import asyncio
import hashlib
import html
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Awaitable, Callable

from . import messaging, repo
from .config import _secret
from .currency import format_money

log = logging.getLogger("bot.topup")

# ---------- настройки (значения по умолчанию — прежние данные для оплаты) ----------
CARD_NUMBER = os.environ.get("TOPUP_CARD_NUMBER", "5599002131738728").strip()
CRYPTO_PAY_TOKEN = (_secret("CRYPTO_PAY_TOKEN") or _secret("CRYPTO_BOT_TOKEN")).strip()
CRYPTO_PAY_BASE_URL = os.environ.get("CRYPTO_PAY_BASE_URL", "https://pay.crypt.bot/api").strip().rstrip("/")
CRYPTO_PAY_FIAT = os.environ.get("CRYPTO_PAY_FIAT", "USD").strip().upper()
CRYPTO_PAY_ACCEPTED_ASSETS = os.environ.get("CRYPTO_PAY_ACCEPTED_ASSETS", "USDT,TON,BTC,ETH,LTC,TRX,USDC").strip()
INVOICE_EXPIRES = int(os.environ.get("TOPUP_INVOICE_EXPIRES", "3600") or 3600)
POLL_INTERVAL = int(os.environ.get("TOPUP_POLL_INTERVAL", "10") or 10)
STARS_BOT_TOKEN = _secret("STARS_BOT_TOKEN").strip()
STARS_TITLE = os.environ.get("STARS_BOT_TITLE", "Покупка конфет").strip()
STARS_DESCRIPTION = os.environ.get("STARS_BOT_DESCRIPTION", "Оплата покупки конфет через Telegram Stars").strip()

CANDY = "🍬"
BUTTON_LABEL = "🍭 Купить конфеты 🍬"
BUTTON_STYLE = "primary"      # как у соседних кнопок под приветствием
MAX_CANDIES = 1_000_000       # больше за один заказ не берём (защита от опечаток)
RETRY_LIMIT = 30              # сколько раз пробуем начислить оплаченный заказ, если пользователя нет в базе

M_CARD, M_CRYPTO, M_STARS = "card", "cryptobot", "stars"
SYMBOLS = {"USD": "$", "RUB": "₽", "XTR": "⭐"}


def admin_chat_id() -> int | None:
    """Куда слать чеки по карте: TOPUP_ADMIN_GROUP_ID, затем GROUP_ID, затем ADMIN_CHAT_ID."""
    for name in ("TOPUP_ADMIN_GROUP_ID", "GROUP_ID", "ADMIN_CHAT_ID"):
        raw = os.environ.get(name, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return None


# ---------- расчёты ----------
def to_usd(candies: int) -> Decimal:
    return Decimal(candies) * Decimal("0.01")


def to_rub(candies: int) -> Decimal:
    return Decimal(candies) * Decimal("0.8")


def to_stars(candies: int) -> Decimal:
    return Decimal(candies)


def money(value: Decimal | int | str) -> str:
    return format_money(Decimal(str(value)))


def parse_amount(text: str | None) -> int | None:
    """Первое число из текста («1 000», «500 конфет»). None — числа нет или оно не от 1 до MAX_CANDIES."""
    m = re.search(r"\d+", (text or "").replace(" ", "").replace(" ", ""))
    if not m:
        return None
    value = int(m.group(0))
    return value if 0 < value <= MAX_CANDIES else None


# ---------- ключи callback_data (свои у каждого бота, как у остальных кнопок) ----------
@dataclass(frozen=True)
class Keys:
    p: str   # префикс; вид «tp<хэш>:действие[:id]»

    @staticmethod
    def for_seed(seed: str) -> "Keys":
        return Keys("tp" + hashlib.sha256(f"topup|{seed}".encode()).hexdigest()[:6])

    def cb(self, action: str, arg: int | None = None) -> str:
        return f"{self.p}:{action}" + (f":{arg}" if arg is not None else "")

    def parse(self, data: str | None) -> tuple[str, int | None] | None:
        parts = (data or "").split(":")
        if len(parts) not in (2, 3) or parts[0] != self.p:
            return None
        if len(parts) == 3:
            return (parts[1], int(parts[2])) if parts[2].isdigit() else None
        return parts[1], None


# ---------- тексты ----------
RATE_LINE = "1🍬 ➜ 0,8₽ ➜ 0,01$ ➜ 1⭐"


def methods_text(amount: int | None) -> str:
    head = (
        "<b>Курс конфет:</b>\n"
        f"{RATE_LINE}\n\n"
        "💳 Оплата проходит автоматически, данные карты мы не храним. Конфеты зачислятся сразу после оплаты. 🫰\n\n"
    )
    if amount is None:
        return head + "👇 Введите желаемое количество конфет, чтобы узнать их цену."
    return (
        head
        + f"🍭 За {amount}🍬 вы заплатите:\n"
        f"— <b>{money(to_rub(amount))}₽</b> или <b>{money(to_usd(amount))}$</b> или <b>{money(to_stars(amount))}⭐</b>\n\n"
        "👇 Выберите способ оплаты или введите другое количество конфет."
    )


def card_text(amount: int) -> str:
    rub = money(to_rub(amount))
    return (
        "<b>💳 Оплата по карте 🇷🇺</b>\n\n"
        f"<b>💵 К оплате:</b> {rub}₽\n"
        f"<b>🍭 Получаете:</b> {amount}🍬\n\n"
        "<b>💳 Оплатить переводом по номеру карты:</b>\n"
        f"<code>{html.escape(CARD_NUMBER)}</code>\n\n"
        f"⚠️ Переведите {rub}₽ по реквизитам выше, затем нажмите «Я оплатил» и отправьте в бота скрин или чек платежа. "
        f"После этого ожидайте проверки поступления указанной суммы — вам автоматически будет начислено {amount}🍬."
    )


CARD_CONFIRM_TEXT = (
    "<b>🧾 Подтвердите оплату 🧾</b>\n\n"
    "Отправьте скрин или чек об оплате, после чего мы проверим поступление и вы получите свои конфеты 📷💵👇"
)
CARD_PROCESSING_TEXT = (
    "<b>🔍 Проверяем ваш чек...</b>\n\n"
    "✅ Ваш чек принят на рассмотрение. Обычно это занимает меньше получаса. "
    "Пока что вы можете вернуться в главное меню."
)


def stars_text(amount: int) -> str:
    return (
        "<b>⭐ Оплата через Telegram Stars 💫</b>\n\n"
        f"<b>💵 К оплате:</b> {money(to_stars(amount))}⭐\n"
        f"<b>🍭 Получаете:</b> {amount}🍬\n\n"
        "✅ Счёт успешно создан. Нажмите «Перейти к оплате» и подтвердите платёж.\n\n"
        "🧾 Конфеты зачислятся автоматически после оплаты."
    )


def crypto_text(amount: int) -> str:
    return (
        "<b>🤖 Оплата через CryptoBot 💎</b>\n\n"
        f"<b>💵 К оплате:</b> {money(to_usd(amount))}$\n"
        f"<b>🍭 Получаете:</b> {amount}🍬\n\n"
        "✅ Счёт успешно создан. Нажмите «Перейти к оплате», выберите монету и нажмите «Оплатить».\n\n"
        "🧾 Конфеты зачислятся автоматически после оплаты."
    )


CRYPTO_INSTRUCTION_TEXT = (
    "Мы подготовили короткую инструкцию, как оплатить крипто через CryptoBot буквально в пару кликов! 🚀\n\n"
    "✦ Открываем @CryptoBot → переходим в раздел P2P 🤖\n"
    "✦ В разделе P2P жмём «Купить» 🛒\n"
    "✦ Выбираем монету, которую принимает бот к оплате (лучше всего USDT TRC20) 💰\n"
    "✦ Указываем удобный способ оплаты → ищем объявление с подходящим объёмом 🔍\n"
    "✦ Нашли? Жмём на него и выбираем «Купить» ✅\n"
    "✦ Вписываем сумму в рублях, которую хотим купить 📝\n"
    "✦ После подтверждения от продавца получаем реквизиты для перевода 💳\n"
    "✦ Переводим деньги и прикрепляем чек 🧾\n"
    "✦ Крипта у вас на счёте! 🎉\n\n"
    "Возвращаемся к нашему боту и оплачиваем счёт 🔥"
)


def success_text(method: str, order) -> str:
    sym = SYMBOLS.get(order.currency, "")
    title = "<b>⭐ Оплата через Telegram Stars 💫</b>" if method == M_STARS else "<b>🤖 Оплата через CryptoBot 💎</b>"
    return (
        f"{title}\n\n"
        f"✅ Счёт на {money(order.fiat_amount)}{sym} успешно оплачен.\n\n"
        f"Вы успешно купили {int(order.candies)}{CANDY}"
    )


def credited_notice(order) -> str:
    return f"🍭 Вы приобрели {int(order.candies)}{CANDY} за {money(order.fiat_amount)}{SYMBOLS.get(order.currency, '')}"


def receipt_caption(order, mentions: str = "") -> str:
    sender = html.escape(order.receipt_sender or str(order.user_id), quote=False)
    text = (
        "<b>🧾 Чек на проверку</b>\n\n"
        f"<b>Пользователь:</b> {sender} (<code>{order.user_id}</code>)\n"
        f"<b>🍭 Сумма:</b> {int(order.candies)}{CANDY}\n"
        f"<b>💵 К оплате:</b> {money(order.fiat_amount)}₽"
    )
    return text + (f"\n\n<b>👮 Пинг админов:</b>\n{mentions}" if mentions else "")


# ---------- кнопки (ряды без aiogram: (подпись, callback_data, url, стиль)) ----------
Row = list[tuple[str, str | None, str | None, str | None]]


def methods_rows(keys: Keys, ready: bool) -> list[Row]:
    pay = "success" if ready else None
    return [
        [("⭐ Telegram Stars 💫", keys.cb("stars"), None, pay)],
        [("🤖 CryptoBot 💎", keys.cb("crypto"), None, pay)],
        [("💳 По карте 💵", keys.cb("card"), None, pay)],
        [("⬅️ Назад", keys.cb("back"), None, "danger")],
    ]


def card_rows(keys: Keys) -> list[Row]:
    return [[("✅ Я оплатил ✅", keys.cb("paid"), None, "success")], [("⬅️ Назад", keys.cb("methods"), None, "danger")]]


def card_confirm_rows(keys: Keys) -> list[Row]:
    return [[("⬅️ Назад", keys.cb("card"), None, "danger")]]


def card_processing_rows(keys: Keys) -> list[Row]:
    return [[("⬅️ В главное меню", keys.cb("back"), None, "danger")]]


def invoice_rows(keys: Keys, url: str, *, crypto: bool) -> list[Row]:
    rows: list[Row] = [[("💸 Перейти к оплате 💸", None, url, "success")]]
    if crypto:
        rows.append([("💡 Инструкция по оплате CryptoBot 📋", keys.cb("info"), None, "primary")])
    rows.append([("⬅️ Назад", keys.cb("methods"), None, "danger")])
    return rows


def crypto_info_rows(keys: Keys) -> list[Row]:
    return [[("⬅️ Назад", keys.cb("crypto"), None, "danger")]]


def success_rows(keys: Keys) -> list[Row]:
    return [[("⬅️ Назад", keys.cb("methods"), None, "danger")]]


def receipt_rows(keys: Keys, order) -> list[Row]:
    return [
        [(f"✅ Выдать {int(order.candies)}🍬 за {money(order.fiat_amount)}₽", keys.cb("ok", int(order.id)), None, "success")],
        [("❌ Отказать", keys.cb("no", int(order.id)), None, "danger")],
    ]


def markup(rows: list[Row]):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    def btn(text, cb, url, style):
        kw: dict[str, Any] = {"text": text}
        if cb is not None:
            kw["callback_data"] = cb
        if url is not None:
            kw["url"] = url
        if style:
            kw["style"] = style
        return InlineKeyboardButton(**kw)

    return InlineKeyboardMarkup(inline_keyboard=[[btn(*b) for b in row] for row in rows])


def menu_row(keys: Keys) -> list[tuple[str, str, str]]:
    """Ряд для приветствия: (подпись, callback_data, стиль) — подставляется в shop_buttons.build_keyboard."""
    return [(BUTTON_LABEL, keys.cb("open"), BUTTON_STYLE)]


# ---------- CryptoBot ----------
async def _crypto_post(method: str, payload: dict[str, Any]) -> Any:
    import aiohttp

    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("CRYPTO_PAY_TOKEN is not set")
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{CRYPTO_PAY_BASE_URL}/{method}", json=payload,
            headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}, timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                raise RuntimeError(f"Non-JSON response: {(await resp.text())[:300]}") from None
    if not isinstance(data, dict) or not data.get("ok"):
        raise RuntimeError(str((data or {}).get("error") if isinstance(data, dict) else data))
    return data.get("result")


def _parse_dt(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except Exception:
        return None


# ---------- общее для всех ботов: опрос оплат, Stars-бот, начисление ----------
ACTIVE: dict[int, "TopupModule"] = {}      # bot_id -> модуль работающего бота (нужен для уведомлений)
_watch: dict[int, int] = {}                # order_id -> invoice_id (неоплаченные счета CryptoBot)
_retry: dict[int, int] = {}                # order_id -> число попыток начисления
_wake: asyncio.Event | None = None
_poll_task: asyncio.Task | None = None
_stars_task: asyncio.Task | None = None
_stars_bot: Any = None
_stars_dp: Any = None
_recovered = False


def watch(order_id: int, invoice_id: int) -> None:
    _watch[order_id] = invoice_id
    if _wake is not None:
        _wake.set()


def retry_credit(order_id: int) -> None:
    _retry.setdefault(order_id, 0)
    if _wake is not None:
        _wake.set()


async def finalize(order_id: int) -> None:
    """Оплата подтверждена (статус paid): начисляем конфеты и сообщаем пользователю. Безопасно вызывать повторно."""
    try:
        order = await repo.credit_topup_order(order_id)
    except Exception:
        log.exception("Не удалось начислить конфеты за заказ %s, повторю", order_id)
        retry_credit(order_id)
        return
    if order is None:
        current = await repo.get_topup_order(order_id)
        if current is not None and current.status == "paid":
            retry_credit(order_id)   # пользователя нет в базе или временный сбой — повторим несколько раз
        return
    _retry.pop(order_id, None)
    _watch.pop(order_id, None)
    mod = ACTIVE.get(int(order.bot_id))
    if mod is not None:
        try:
            await mod.notify_credited(order)
        except Exception:
            log.exception("Не удалось сообщить пользователю %s о начислении", order.user_id)


async def _check_invoices() -> None:
    if not _watch or not CRYPTO_PAY_TOKEN:
        return
    by_invoice = {inv: oid for oid, inv in _watch.items()}
    try:
        result = await _crypto_post("getInvoices", {"invoice_ids": ",".join(str(i) for i in by_invoice), "count": 100})
    except Exception as e:
        log.warning("CryptoBot getInvoices не удался: %s", e)
        return
    items = result.get("items", []) if isinstance(result, dict) else (result or [])
    seen: set[int] = set()
    for inv in items:
        if not isinstance(inv, dict):
            continue
        try:
            inv_id = int(inv.get("invoice_id", -1))
        except (TypeError, ValueError):
            continue
        order_id = by_invoice.get(inv_id)
        if order_id is None:
            continue
        seen.add(inv_id)
        status = inv.get("status")
        if status == "paid":
            if await repo.mark_topup_paid(order_id):
                await finalize(order_id)
            else:
                _watch.pop(order_id, None)
        elif status == "expired":
            await repo.finish_topup_order(order_id, "expired")
            _watch.pop(order_id, None)
    # счёт не нашли и срок давно вышел — перестаём следить
    now = datetime.now(timezone.utc)
    for inv_id, order_id in by_invoice.items():
        if inv_id in seen or order_id not in _watch:
            continue
        order = await repo.get_topup_order(order_id)
        exp = order.expires_at if order else None
        if order is None or order.status != "active" or (exp is not None and exp + timedelta(hours=1) < now):
            if order is not None and order.status == "active":
                await repo.finish_topup_order(order_id, "expired")
            _watch.pop(order_id, None)


async def _retry_credits() -> None:
    for order_id in list(_retry):
        _retry[order_id] += 1
        if _retry[order_id] > RETRY_LIMIT:
            log.warning("Заказ %s оплачен, но конфеты не начислены (нет пользователя в базе?) — оставляю как есть", order_id)
            _retry.pop(order_id, None)
            continue
        await finalize(order_id)


async def _recover() -> None:
    """После перезапуска приложения возвращаем в работу неоплаченные счета CryptoBot и неначисленные оплаты."""
    global _recovered
    if _recovered:
        return
    try:
        for o in await repo.list_topup_orders("active", M_CRYPTO):
            if o.invoice_id is not None:
                watch(int(o.id), int(o.invoice_id))
        for o in await repo.list_topup_orders("paid"):
            retry_credit(int(o.id))
        _recovered = True
    except Exception:
        log.exception("Не удалось восстановить заказы пополнения")


async def _poll_loop() -> None:
    global _wake
    _wake = asyncio.Event()
    await _recover()
    while True:
        try:
            if not _watch and not _retry:
                _wake.clear()
                await _wake.wait()   # заказов нет — спим, к базе и CryptoBot не ходим
            await asyncio.sleep(POLL_INTERVAL)
            await _retry_credits()
            await _check_invoices()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка цикла проверки оплат")
            await asyncio.sleep(POLL_INTERVAL)


def _ensure_stars_bot():
    global _stars_bot, _stars_dp
    if _stars_bot is not None:
        return _stars_bot
    if not STARS_BOT_TOKEN:
        raise RuntimeError("STARS_BOT_TOKEN is not set")
    from aiogram import Bot, Dispatcher, F
    from aiogram.types import Message, PreCheckoutQuery

    _stars_bot, _stars_dp = Bot(token=STARS_BOT_TOKEN), Dispatcher()

    @_stars_dp.pre_checkout_query()
    async def _pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        ok, error = False, "Неверный счёт."
        try:
            if query.invoice_payload.startswith("topup:") and query.currency == "XTR":
                order = await repo.get_topup_order(int(query.invoice_payload.split(":", 1)[1]))
                if order and order.method == M_STARS and order.status == "active" and int(order.candies) == query.total_amount:
                    ok, error = True, None
        except Exception:
            log.exception("Stars: не удалось проверить счёт перед оплатой")
            error = "Временная ошибка, попробуйте позже."
        try:
            await bot.answer_pre_checkout_query(query.id, ok=ok, error_message=error)
        except Exception:
            log.exception("Stars: не удалось ответить на pre-checkout")

    @_stars_dp.message(F.successful_payment)
    async def _success(message: Message) -> None:
        sp = message.successful_payment
        if sp is None or sp.currency != "XTR" or not sp.invoice_payload.startswith("topup:"):
            return
        order_id = None
        try:
            order_id = int(sp.invoice_payload.split(":", 1)[1])
            order = await repo.get_topup_order(order_id)
            if order is None or order.method != M_STARS:
                return
            await repo.mark_topup_paid(order_id, getattr(sp, "telegram_payment_charge_id", None))
            await finalize(order_id)
        except Exception:
            log.exception("Stars: не удалось обработать оплату")
            if order_id is not None:
                retry_credit(order_id)

    return _stars_bot


def ensure_started() -> None:
    """Запускает фоновые задачи (один раз на приложение): опрос оплат и, если задан токен, приём оплат Stars."""
    global _poll_task, _stars_task
    if _poll_task is None or _poll_task.done():
        _poll_task = asyncio.create_task(_poll_loop(), name="topup-poll")
    if STARS_BOT_TOKEN and (_stars_task is None or _stars_task.done()):
        try:
            bot = _ensure_stars_bot()
        except Exception:
            log.exception("Не удалось запустить Stars-бота")
            return

        async def run() -> None:
            try:
                await _stars_dp.start_polling(
                    bot, handle_signals=False, allowed_updates=["pre_checkout_query", "message"]
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Polling Stars-бота остановился")

        _stars_task = asyncio.create_task(run(), name="topup-stars")
    elif not STARS_BOT_TOKEN:
        log.info("STARS_BOT_TOKEN не задан — оплата Telegram Stars выключена")


async def shutdown() -> None:
    global _poll_task, _stars_task, _stars_bot, _stars_dp
    if _stars_dp is not None and _stars_task is not None and not _stars_task.done():
        try:
            await _stars_dp.stop_polling()
        except Exception:
            pass
    for task in (_poll_task, _stars_task):
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    if _stars_bot is not None:
        try:
            await _stars_bot.session.close()
        except Exception:
            pass
    _poll_task = _stars_task = _stars_bot = _stars_dp = None


# ---------- модуль одного бота ----------
@dataclass
class UserState:
    amount: int | None = None
    screen: str = "methods"        # methods | card | card_wait_receipt | card_review | invoice | info
    message_id: int | None = None
    chat_id: int | None = None
    method: str | None = None
    order_id: int | None = None
    invoice_url: str | None = None
    invoice_amount: int | None = None
    busy: bool = False
    extra: dict = field(default_factory=dict)


ShowScreen = Callable[..., Awaitable[None]]


class TopupModule:
    """Экраны и обработчики «Купить конфеты» для работающего бота. Создаётся в handlers.build_router."""

    def __init__(self, bot_id: int, bot: Any, profile: Any, keys: Keys, *, show_screen: ShowScreen,
                 safe_delete: Callable[..., Awaitable[None]], track: Callable[[int, int], None],
                 go_main: Callable[..., Awaitable[None]], is_user: Callable[[int], bool]) -> None:
        self.bot_id, self.bot, self.profile, self.keys = bot_id, bot, profile, keys
        self._show, self._delete, self._track, self._go_main, self._is_user = show_screen, safe_delete, track, go_main, is_user
        self.states: dict[int, UserState] = {}
        if bot is not None:
            ACTIVE[bot_id] = self

    def menu_rows(self) -> list[tuple[str, str, str]]:
        return menu_row(self.keys)

    # --- состояние ---
    def state(self, user_id: int) -> UserState:
        if user_id not in self.states and len(self.states) > 5000:
            self.states.clear()
        return self.states.setdefault(user_id, UserState())

    def reset(self, user_id: int) -> None:
        """Выход из раздела (приветствие, /start, «Меню»). Выставленные счета не отменяются: оплаченный счёт всё равно
        будет засчитан фоновой проверкой."""
        self.states.pop(user_id, None)

    def active(self, user_id: int) -> bool:
        return user_id in self.states

    @property
    def image(self) -> str | None:
        return getattr(self.profile, "message_image_url", None)

    # --- вывод ---
    async def _edit_any(self, bot, chat_id: int, message_id: int, text: str, rows: list[Row]) -> bool:
        """Обновляет сообщение раздела на месте (с картинкой или без: не знаем, дошла ли картинка)."""
        from aiogram.exceptions import TelegramAPIError

        for has_photo in (bool(self.image), not bool(self.image)):
            try:
                await messaging.edit_message(bot, chat_id, message_id, text, markup(rows), has_photo=has_photo)
                return True
            except TelegramAPIError:
                continue
        return False

    async def _send_new(self, bot, user_id: int, st: UserState, text: str, rows: list[Row]) -> None:
        """Новое сообщение раздела вместо старого (например, после ввода количества конфет)."""
        from aiogram.exceptions import TelegramAPIError

        old = st.message_id
        try:
            sent = await messaging.send_message(bot, user_id, text, self.image, reply_markup=markup(rows))
        except TelegramAPIError as e:
            log.warning("Экран пополнения пользователю %s не доставлен: %s", user_id, e)
            return
        st.message_id, st.chat_id = sent.message_id, sent.chat.id
        self._track(user_id, sent.message_id)
        if old is not None and old != sent.message_id:
            await self._delete(bot, user_id, old)

    async def _screen(self, call, bot, st: UserState, screen: str, text: str, rows: list[Row]) -> None:
        await self._show(call, bot, call.from_user.id, text, markup(rows))
        st.screen = screen
        st.chat_id = call.message.chat.id if call.message else call.from_user.id
        st.message_id = call.message.message_id if call.message else None

    async def _methods(self, call, bot, st: UserState) -> None:
        await self._screen(call, bot, st, "methods", methods_text(st.amount), methods_rows(self.keys, st.amount is not None))

    async def _alert(self, call, text: str) -> None:
        try:
            await call.answer(text, show_alert=True)
        except Exception:
            pass

    # --- нажатия ---
    async def on_callback(self, call, bot) -> None:
        parsed = self.keys.parse(call.data)
        if parsed is None:
            try:
                await call.answer()
            except Exception:
                pass
            return
        action, arg = parsed
        if action in ("ok", "no"):
            await self._admin_decision(call, bot, action, arg)
            return
        user_id = call.from_user.id
        if not self._is_user(user_id) or call.message is None:
            try:
                await call.answer()
            except Exception:
                pass
            return
        st = self.state(user_id)
        if action == "open":
            self.states[user_id] = st = UserState()
            await self._methods(call, bot, st)
        elif action == "methods":
            await self._methods(call, bot, st)
        elif action == "back":
            self.reset(user_id)
            await self._go_main(call, bot, user_id)
        elif action in ("card", "paid", "stars", "crypto", "info"):
            if st.amount is None:
                await self._alert(call, "❌ Сначала введите количество конфет.")
                return
            await self._pay_action(call, bot, st, action)
            return
        try:
            await call.answer()
        except Exception:
            pass

    async def _pay_action(self, call, bot, st: UserState, action: str) -> None:
        amount = int(st.amount or 0)
        if action == "card":
            if admin_chat_id() is None:
                await self._alert(call, "❌ Оплата картой сейчас недоступна.")
                return
            await self._screen(call, bot, st, "card", card_text(amount), card_rows(self.keys))
        elif action == "paid":
            await self._screen(call, bot, st, "card_wait_receipt", CARD_CONFIRM_TEXT, card_confirm_rows(self.keys))
        elif action == "info":
            await self._screen(call, bot, st, "info", CRYPTO_INSTRUCTION_TEXT, crypto_info_rows(self.keys))
        else:
            if st.busy:   # быстрые повторные нажатия не создают второй счёт
                await self._alert(call, "Секунду, счёт создаётся…")
                return
            st.busy = True
            try:
                await self._invoice(call, bot, st, M_STARS if action == "stars" else M_CRYPTO, amount)
            except Exception:
                log.exception("Не удалось создать счёт (%s) на %s🍬", action, amount)
                name = "Stars (возможно, сумма слишком большая)" if action == "stars" else "CryptoBot"
                await self._alert(call, f"❌ Не удалось создать счёт {name}.")
                return
            finally:
                st.busy = False
        try:
            await call.answer()
        except Exception:
            pass

    async def _invoice(self, call, bot, st: UserState, method: str, amount: int) -> None:
        user_id = call.from_user.id
        crypto = method == M_CRYPTO
        if st.method == method and st.invoice_url and st.invoice_amount == amount and st.order_id:
            url = st.invoice_url   # тот же счёт, если количество не менялось
        else:
            if crypto and not CRYPTO_PAY_TOKEN:
                raise RuntimeError("CRYPTO_PAY_TOKEN is not set")
            if not crypto:
                bot_stars = _ensure_stars_bot()
            currency = CRYPTO_PAY_FIAT if crypto else "XTR"
            fiat = to_usd(amount) if crypto else Decimal(amount)
            fiat = fiat.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            order_id = await repo.create_topup_order(
                self.bot_id, user_id, amount, method, currency, fiat,
                screen_chat_id=call.message.chat.id, screen_message_id=call.message.message_id,
            )
            try:
                if crypto:
                    inv = await _crypto_post("createInvoice", {
                        "currency_type": "fiat", "fiat": CRYPTO_PAY_FIAT, "amount": money(fiat).replace(",", "."),
                        "accepted_assets": CRYPTO_PAY_ACCEPTED_ASSETS,
                        "description": f"Пополнение баланса: {amount}🍬",
                        "hidden_message": f"Оплата получена. Вам начислено {amount}🍬.",
                        "payload": f"topup:{order_id}", "allow_comments": False, "allow_anonymous": True,
                        "expires_in": INVOICE_EXPIRES,
                    })
                    invoice_id, url = int(inv["invoice_id"]), inv["bot_invoice_url"]
                    expires = _parse_dt(inv.get("expiration_date")) or datetime.now(timezone.utc) + timedelta(seconds=INVOICE_EXPIRES)
                else:
                    from aiogram.types import LabeledPrice

                    url = await bot_stars.create_invoice_link(
                        title=STARS_TITLE, description=STARS_DESCRIPTION, payload=f"topup:{order_id}",
                        provider_token="", currency="XTR", prices=[LabeledPrice(label="🍬", amount=amount)],
                    )
                    invoice_id, expires = None, None
            except Exception:
                await repo.finish_topup_order(order_id, "failed")
                raise
            await repo.set_topup_invoice(order_id, invoice_id, url, expires)
            if crypto:
                watch(order_id, invoice_id)
            st.order_id, st.method, st.invoice_url, st.invoice_amount = order_id, method, url, amount
        text = crypto_text(amount) if crypto else stars_text(amount)
        await self._screen(call, bot, st, "invoice", text, invoice_rows(self.keys, url, crypto=crypto))

    # --- сообщения пользователя внутри раздела ---
    async def on_message(self, message, bot) -> bool:
        """True — сообщение обработано разделом (число = количество конфет, фото/файл = чек). Вызывать, только если
        для пользователя есть открытый раздел (`active`)."""
        user_id = message.from_user.id
        st = self.state(user_id)
        if st.screen == "card_wait_receipt":
            if message.photo or message.document:
                await self._receipt(message, bot, st)
            return True
        if st.screen == "card_review":
            return True
        amount = parse_amount(message.text)
        if amount is None:
            if message.text:
                try:
                    await messaging.send_message(bot, user_id, f"❌ Введите число конфет от 1 до {MAX_CANDIES}.")
                except Exception:
                    pass
            return True
        st.amount, st.screen = amount, "methods"
        st.method = st.order_id = st.invoice_url = st.invoice_amount = None
        await self._send_new(bot, user_id, st, methods_text(amount), methods_rows(self.keys, True))
        await self._delete(bot, user_id, message.message_id)   # число пользователя убираем, чтобы не копились в чате
        return True

    # --- чек по карте ---
    async def _receipt(self, message, bot, st: UserState) -> None:
        user_id, amount, target = message.from_user.id, int(st.amount or 0), admin_chat_id()
        if not amount or target is None:
            return
        user = message.from_user
        sender = f"@{user.username}" if user.username else ((user.full_name or "").strip() or str(user.id))
        file_id = message.photo[-1].file_id if message.photo else message.document.file_id
        rub = to_rub(amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        order_id = await repo.create_topup_order(
            self.bot_id, user_id, amount, M_CARD, "RUB", rub, status="review",
            receipt_file_id=file_id, receipt_sender=sender,
        )
        order = await repo.get_topup_order(order_id)
        if order is None:
            return
        caption = receipt_caption(order, await self._admin_mentions(bot, target))
        try:
            send = bot.send_photo if message.photo else bot.send_document
            kwargs = {"photo": file_id} if message.photo else {"document": file_id}
            await send(target, caption=caption, reply_markup=markup(receipt_rows(self.keys, order)), parse_mode="HTML", **kwargs)
        except Exception:
            log.exception("Не удалось отправить чек админам")
            await repo.finish_topup_order(order_id, "rejected")
            try:
                await messaging.send_message(bot, user_id, "❌ Не удалось отправить чек на проверку. Попробуйте позже.")
            except Exception:
                pass
            return
        st.screen, st.order_id, st.method = "card_review", order_id, M_CARD
        await self._send_new(bot, user_id, st, CARD_PROCESSING_TEXT, card_processing_rows(self.keys))
        if st.message_id is not None and st.chat_id is not None:
            try:
                await repo.set_topup_screen(order_id, st.chat_id, st.message_id)
            except Exception:
                pass

    @staticmethod
    async def _admin_mentions(bot, chat_id: int) -> str:
        if chat_id > 0:   # личный чат владельца — пинговать некого
            return ""
        try:
            admins = await bot.get_chat_administrators(chat_id)
        except Exception:
            return ""
        names = []
        for m in admins:
            u = getattr(m, "user", None)
            if u and not getattr(u, "is_bot", False) and getattr(u, "username", None):
                names.append(f"@{u.username}")
        return " ".join(dict.fromkeys(names))

    async def _is_admin(self, bot, chat_id: int, user_id: int) -> bool:
        if chat_id > 0:
            return user_id == chat_id
        try:
            member = await bot.get_chat_member(chat_id, user_id)
        except Exception:
            return False
        return getattr(member, "status", "") in ("creator", "administrator")

    async def _admin_decision(self, call, bot, action: str, order_id: int | None) -> None:
        msg = call.message
        if msg is None or order_id is None:
            return
        if not await self._is_admin(bot, msg.chat.id, call.from_user.id):
            await self._alert(call, "Подтверждать оплату могут только администраторы.")
            return
        order = await repo.get_topup_order(order_id)
        if order is None or order.method != M_CARD or int(order.bot_id) != self.bot_id:
            await self._alert(call, "Заказ не найден.")
            return
        who = f"@{call.from_user.username}" if call.from_user.username else html.escape(call.from_user.full_name or str(call.from_user.id))
        if action == "ok":
            if order.status != "review":
                await self._alert(call, "Этот чек уже обработан.")
                await self._strip_buttons(bot, msg)
                return
            if await repo.mark_topup_paid(order_id):
                await finalize(order_id)
            fresh = await repo.get_topup_order(order_id)
            done = fresh is not None and fresh.status == "credited"
            line = (f"✅ Выдано {int(order.candies)}{CANDY} за {money(order.fiat_amount)}₽ ({who})" if done
                    else "⚠️ Оплата отмечена, но конфеты пока не начислены (пользователя нет в базе?).")
        else:
            if order.status != "review":
                await self._alert(call, "Этот чек уже обработан.")
                await self._strip_buttons(bot, msg)
                return
            if await repo.finish_topup_order(order_id, "rejected"):
                await self._notify(bot, order, "❌ Чек не подтверждён. Если вы действительно оплатили, свяжитесь с поддержкой.")
            line = f"❌ Отказано ({who})"
        try:
            await bot.edit_message_caption(chat_id=msg.chat.id, message_id=msg.message_id, caption=line, reply_markup=None)
        except Exception:
            try:
                await bot.edit_message_reply_markup(chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None)
            except Exception:
                pass
        try:
            await call.answer()
        except Exception:
            pass

    @staticmethod
    async def _strip_buttons(bot, msg) -> None:
        try:
            await bot.edit_message_reply_markup(chat_id=msg.chat.id, message_id=msg.message_id, reply_markup=None)
        except Exception:
            pass

    # --- сообщения пользователю после оплаты ---
    async def _notify(self, bot, order, text: str) -> None:
        try:
            await messaging.send_message(bot, int(order.user_id), text)   # короткое уведомление — без картинки
        except Exception as e:
            log.warning("Уведомление пользователю %s не доставлено: %s", order.user_id, e)

    async def notify_credited(self, order) -> None:
        bot = self.bot
        if bot is None:
            return
        st = self.states.get(int(order.user_id))
        if order.method != M_CARD and order.screen_chat_id and order.screen_message_id:
            await self._edit_any(bot, int(order.screen_chat_id), int(order.screen_message_id),
                                 success_text(order.method, order), success_rows(self.keys))
        if st is not None:
            st.method = st.order_id = st.invoice_url = st.invoice_amount = None
            st.screen = "methods"
        await self._notify(bot, order, credited_notice(order))


def detach(bot_id: int, module: "TopupModule | None" = None) -> None:
    if module is None or ACTIVE.get(bot_id) is module:
        ACTIVE.pop(bot_id, None)
