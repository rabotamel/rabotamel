"""Валюты бота: 🍬, рубли, доллары и Telegram Stars ⭐.

Курс: 1🍬 = 0,8 ₽ = 0,01 $ = 1⭐ (тот же курс звёзд, что в «Купить конфеты», см. topup.to_stars). Все расчёты на Decimal, чтобы не было ошибок float (1,2 / 0,8 и т. п.).
Выдача в рублях/долларах переводится в целое число конфет с округлением «половина вверх».
"""
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CANDY_RUB = Decimal("0.8")    # сколько рублей стоит 1🍬
CANDY_USD = Decimal("0.01")   # сколько долларов стоит 1🍬
CANDY_STARS = Decimal("1")    # сколько звёзд Telegram стоит 1🍬 (курс звёзд бота)

CANDY, RUB, USD, STAR = "candy", "rub", "usd", "star"
CURRENCIES = (CANDY, RUB, USD, STAR)
SYMBOLS = {RUB: "₽", USD: "$", STAR: "⭐"}

MAX_CANDIES = 1_000_000_000_000


def parse_amount(raw: str, currency: str) -> Decimal:
    """Разбирает введённую сумму. Конфеты и звёзды — только целые; рубли и доллары — до двух знаков после запятой.
    Бросает ValueError с понятным текстом."""
    if currency not in CURRENCIES:
        raise ValueError("Неизвестная валюта")
    text = (raw or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("Введите сумму числом") from None
    if not value.is_finite() or value <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    if currency in (CANDY, STAR):
        if value != value.to_integral_value():
            raise ValueError("Конфеты — только целым числом" if currency == CANDY else "Звёзды — только целым числом")
    elif value != value.quantize(Decimal("0.01")):
        raise ValueError("Не больше двух знаков после запятой")
    return value


def to_candies(amount: Decimal, currency: str) -> int:
    """Сколько конфет соответствует сумме в указанной валюте (округление половина вверх)."""
    if currency == CANDY:
        exact = amount
    elif currency == RUB:
        exact = amount / CANDY_RUB
    elif currency == USD:
        exact = amount / CANDY_USD
    elif currency == STAR:
        exact = amount / CANDY_STARS
    else:
        raise ValueError("Неизвестная валюта")
    return int(exact.to_integral_value(rounding=ROUND_HALF_UP))


def convert_all(amount: Decimal, currency: str) -> dict[str, Decimal]:
    """Точные значения суммы во всех валютах бота (без округления до целых конфет)."""
    if currency == CANDY:
        candies = amount
    elif currency == RUB:
        candies = amount / CANDY_RUB
    elif currency == STAR:
        candies = amount / CANDY_STARS
    else:
        candies = amount / CANDY_USD
    return {CANDY: candies, RUB: candies * CANDY_RUB, USD: candies * CANDY_USD, STAR: candies * CANDY_STARS}


def format_money(value: Decimal) -> str:
    """Сумма для текста пользователю: «1234,5» (без разделителей разрядов, запятая в дроби)."""
    q = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    whole, _, frac = f"{q:f}".partition(".")
    frac = frac.rstrip("0")
    return f"{whole},{frac}" if frac else whole
