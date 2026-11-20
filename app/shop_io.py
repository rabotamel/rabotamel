"""Экспорт каталога магазина в текстовый файл (.txt, UTF-8) и разбор такого файла для восстановления.

Формат (построчный; описание записывается ПОСТРОЧНО с указанием числа строк, поэтому любые символы в нём — в том числе
строки, похожие на служебные маркеры, пустые строки, эмодзи, `**...**`, `[вариант1/вариант2]` — разбор не ломают):

    TG-BOT-ROTATOR SHOP v1
    products: 2

    === PRODUCT ===
    id: 7
    name: Абсолют | MAXIMUM POWER 🚀
    price: 6789
    color: red
    visible: yes
    about-lines: 3
    **Мощный** набор

    Привет [товарищ/друг]!
    === END PRODUCT ===

    === PRODUCT ===
    ...

Товары идут в порядке показа. color: default / red / green / blue; visible: yes / no. `id` сохраняется, потому что от id товара
зависит выбор вариантов [а/б] у бота и callback_data кнопок: с тем же id бот показывает товар в точности как раньше. Файл можно править
вручную: `id` необязателен (тогда выдаётся новый), `products:` (если есть) должен совпадать с числом блоков. Описание — ровно столько
строк, сколько указано в `about-lines` (0 — без описания).
"""
from dataclasses import dataclass

from . import shop

HEADER = "TG-BOT-ROTATOR SHOP v1"
BEGIN, END = "=== PRODUCT ===", "=== END PRODUCT ==="
MAX_FILE_CHARS = 2_000_000
MAX_ITEMS = 500
MAX_ID = 2_147_483_647

_COLOR_OUT = {"": "default", "danger": "red", "success": "green", "primary": "blue"}


@dataclass(frozen=True)
class Item:
    """Товар из файла: поля уже проверены и приведены к виду БД."""
    id: int | None
    name: str
    price: int
    about: str
    color: str
    visible: bool


def export_products(rows) -> str:
    """Весь каталог (в порядке показа, включая скрытые) -> текст файла. rows — объекты с полями id, name, price, about, color, is_visible."""
    rows = list(rows)
    out = [HEADER, f"products: {len(rows)}", ""]
    for r in rows:
        about = str(r.about or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = about.split("\n") if about else []
        color = str(r.color or "")
        out += [
            BEGIN, f"id: {int(r.id)}", f"name: {r.name}", f"price: {int(r.price)}",
            f"color: {_COLOR_OUT.get(color, 'default')}", f"visible: {'yes' if r.is_visible else 'no'}",
            f"about-lines: {len(lines)}", *lines, END, "",
        ]
    return "\n".join(out)


def _bool(value: str, line_no: int) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "1", "да"):
        return True
    if v in ("no", "false", "0", "нет"):
        return False
    raise ValueError(f"Строка {line_no}: visible — yes или no")


def parse_products(text: str) -> list[Item]:
    """Текст файла -> список товаров в порядке файла. ValueError с понятным сообщением (с номером строки), если файл
    повреждён: ничего не применяется, пока разбор не прошёл целиком."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("Файл пустой")
    if len(text) > MAX_FILE_CHARS:
        raise ValueError("Файл слишком большой")
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    n, i = len(lines), 0

    def skip_blank() -> None:
        nonlocal i
        while i < n and not lines[i].strip():
            i += 1

    skip_blank()
    if i >= n or lines[i].strip() != HEADER:
        raise ValueError("Это не файл экспорта товаров: нет заголовка " + HEADER)
    i += 1
    declared: int | None = None
    skip_blank()
    if i < n and lines[i].lower().startswith("products:"):
        try:
            declared = int(lines[i].partition(":")[2].strip())
        except ValueError:
            raise ValueError(f"Строка {i + 1}: products — число") from None
        i += 1

    items: list[Item] = []
    seen_ids: set[int] = set()
    while True:
        skip_blank()
        if i >= n:
            break
        if lines[i].strip() != BEGIN:
            raise ValueError(f"Строка {i + 1}: ожидалось «{BEGIN}»")
        start_line = i + 1
        i += 1
        fields: dict[str, str] = {}
        about_count: int | None = None
        while True:
            if i >= n:
                raise ValueError(f"Товар со строки {start_line}: нет строки about-lines")
            line = lines[i]
            key, sep, value = line.partition(":")
            key = key.strip().lower()
            if not sep or key not in ("id", "name", "price", "color", "visible", "about-lines"):
                raise ValueError(f"Строка {i + 1}: неизвестное поле «{line.strip()[:40]}»")
            if key in fields:
                raise ValueError(f"Строка {i + 1}: поле {key} повторяется")
            fields[key] = value.strip()
            i += 1
            if key == "about-lines":
                try:
                    about_count = int(fields[key])
                    if about_count < 0:
                        raise ValueError
                except ValueError:
                    raise ValueError(f"Строка {i}: about-lines — целое число от 0") from None
                break
        about_lines = lines[i:i + about_count]
        if len(about_lines) < about_count:
            raise ValueError(f"Товар со строки {start_line}: описание короче заявленных {about_count} строк — файл обрезан")
        i += about_count
        if i >= n or lines[i].strip() != END:
            raise ValueError(f"Строка {i + 1}: после описания ожидалось «{END}» (число строк about-lines не совпадает)")
        i += 1
        try:
            if "name" not in fields or "price" not in fields:
                raise ValueError("нужны поля name и price")
            pid = None
            if fields.get("id"):
                if not fields["id"].isascii() or not fields["id"].isdigit() or not 1 <= int(fields["id"]) <= MAX_ID:
                    raise ValueError("id — целое число")
                pid = int(fields["id"])
                if pid in seen_ids:
                    raise ValueError(f"id {pid} повторяется")
                seen_ids.add(pid)
            item = Item(
                id=pid, name=shop.normalize_name(fields["name"]), price=shop.normalize_price(fields["price"]),
                about=shop.normalize_about("\n".join(about_lines)), color=shop.normalize_color(fields.get("color", "")),
                visible=_bool(fields["visible"], start_line) if fields.get("visible") else True,
            )
        except ValueError as e:
            raise ValueError(f"Товар со строки {start_line}: {e}") from None
        items.append(item)
        if len(items) > MAX_ITEMS:
            raise ValueError(f"Слишком много товаров (максимум {MAX_ITEMS})")
    if declared is not None and declared != len(items):
        raise ValueError(f"В заголовке указано товаров: {declared}, а в файле найдено: {len(items)} — файл повреждён")
    return items
