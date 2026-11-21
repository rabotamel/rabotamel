"""Экспорт/импорт ВСЕХ текстов вкладки «Кнопки в боте» (товары магазина и задания) в .txt (UTF-8).

Файл v2 (`TG-BOT-ROTATOR BUTTONS v2`): `products: N`, `tasks: M`, затем блоки `=== PRODUCT ===` (как ниже) и блоки заданий:

    === TASK ===
    id: 3
    button: Подписаться на канал
    reward: 10
    screenshot: yes
    one-time: yes
    visible: yes
    about-lines: 2
    comment-lines: 1
    <2 строки описания>
    <1 строка комментария для админа>
    === END TASK ===

В заголовке v2 после `tasks:` может идти строка `support: @username` (или ID; пусто — контакт не задан): контакт поддержки с сайта.
Старые файлы без неё при импорте контакт не меняют.

Описание и комментарий записываются построчно (число строк указано), поэтому любые символы разбор не ломают. Старые файлы
`TG-BOT-ROTATOR SHOP v1` (только товары) по-прежнему принимаются: тогда задания не затрагиваются.

Ниже — описание формата товаров (общее для v1 и v2).

Экспорт каталога магазина в текстовый файл (.txt, UTF-8) и разбор такого файла для восстановления.

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

from . import shop, tasks
from . import support as support_mod

HEADER = "TG-BOT-ROTATOR SHOP v1"            # только товары (прежний формат, принимается при импорте)
HEADER_V2 = "TG-BOT-ROTATOR BUTTONS v2"      # товары + задания
BEGIN, END = "=== PRODUCT ===", "=== END PRODUCT ==="
TASK_BEGIN, TASK_END = "=== TASK ===", "=== END TASK ==="
MAX_FILE_CHARS = 2_000_000
MAX_ITEMS = 500
MAX_TASK_ITEMS = 200
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


@dataclass(frozen=True)
class TaskItem:
    """Задание из файла: поля уже проверены и приведены к виду БД."""
    id: int | None
    button_text: str
    about: str
    reward: int
    screenshot: bool
    one_time: bool
    visible: bool
    comment: str


@dataclass(frozen=True)
class Parsed:
    """Разобранный файл. tasks=None — файл старого формата (только товары): задания при импорте не трогаются.
    support=None — в файле нет строки `support:` (контакт поддержки не трогаем); "" — в файле он пустой (очистить)."""
    products: list
    tasks: list | None
    support: str | None = None


def _lines(value) -> list[str]:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    return text.split("\n") if text else []


def _product_block(r) -> list[str]:
    lines = _lines(r.about)
    color = str(r.color or "")
    return [
        BEGIN, f"id: {int(r.id)}", f"name: {r.name}", f"price: {int(r.price)}",
        f"color: {_COLOR_OUT.get(color, 'default')}", f"visible: {'yes' if r.is_visible else 'no'}",
        f"about-lines: {len(lines)}", *lines, END, "",
    ]


def export_all(products, task_rows, support: str | None = None) -> str:
    """Все тексты вкладки «Кнопки в боте»: товары (в порядке показа, включая скрытые), задания и контакт поддержки -> текст файла v2.
    task_rows — объекты с полями id, button_text, about, reward, require_screenshot, one_time, is_visible, comment.
    support — «@username» / ID / "" (не задан); None — строка `support:` в файл не пишется."""
    products, task_rows = list(products), list(task_rows)
    out = [HEADER_V2, f"products: {len(products)}", f"tasks: {len(task_rows)}"]
    if support is not None:
        out.append(f"support: {support}")
    out.append("")
    for r in products:
        out += _product_block(r)
    for t in task_rows:
        about, comment = _lines(t.about), _lines(getattr(t, "comment", ""))
        out += [
            TASK_BEGIN, f"id: {int(t.id)}", f"button: {t.button_text}", f"reward: {int(t.reward)}",
            f"screenshot: {'yes' if t.require_screenshot else 'no'}", f"one-time: {'yes' if getattr(t, 'one_time', True) else 'no'}",
            f"visible: {'yes' if t.is_visible else 'no'}", f"about-lines: {len(about)}", f"comment-lines: {len(comment)}",
            *about, *comment, TASK_END, "",
        ]
    return "\n".join(out)


def export_products(rows) -> str:
    """Весь каталог (в порядке показа, включая скрытые) -> текст файла. rows — объекты с полями id, name, price, about, color, is_visible."""
    rows = list(rows)
    out = [HEADER, f"products: {len(rows)}", ""]
    for r in rows:
        out += _product_block(r)
    return "\n".join(out)


def _bool(value: str, line_no: int) -> bool:
    v = value.strip().lower()
    if v in ("yes", "true", "1", "да"):
        return True
    if v in ("no", "false", "0", "нет"):
        return False
    raise ValueError(f"Строка {line_no}: visible — yes или no")


def _parse(text: str, v2: bool) -> Parsed:
    """Текст файла (v1: только товары; v2: товары и задания) -> Parsed, порядок как в файле. ValueError с понятным сообщением (с номером строки), если файл
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
    head = HEADER_V2 if v2 else HEADER
    if i >= n or lines[i].strip() != head:
        raise ValueError("Это не файл экспорта: нет заголовка " + head)
    i += 1
    declared: int | None = None
    declared_tasks: int | None = None
    support: str | None = None
    for _ in range(3 if v2 else 1):
        skip_blank()
        for key in ("products", "tasks", "support") if v2 else ("products",):
            if i < n and lines[i].lower().startswith(key + ":"):
                if key == "support":
                    try:
                        support = support_mod.normalize(lines[i].partition(":")[2])
                    except ValueError as e:
                        raise ValueError(f"Строка {i + 1}: support — {e}") from None
                    i += 1
                    break
                try:
                    value = int(lines[i].partition(":")[2].strip())
                except ValueError:
                    raise ValueError(f"Строка {i + 1}: {key} — число") from None
                if key == "products":
                    declared = value
                else:
                    declared_tasks = value
                i += 1
                break

    items: list[Item] = []
    seen_ids: set[int] = set()
    task_items: list[TaskItem] = []
    seen_task_ids: set[int] = set()
    while True:
        skip_blank()
        if i >= n:
            break
        if v2 and lines[i].strip() == TASK_BEGIN:
            i = _parse_task(lines, i, task_items, seen_task_ids)
            if len(task_items) > MAX_TASK_ITEMS:
                raise ValueError(f"Слишком много заданий (максимум {MAX_TASK_ITEMS})")
            continue
        if lines[i].strip() != BEGIN:
            raise ValueError(f"Строка {i + 1}: ожидалось «{BEGIN}»" + (f" или «{TASK_BEGIN}»" if v2 else ""))
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
    if declared_tasks is not None and declared_tasks != len(task_items):
        raise ValueError(f"В заголовке указано заданий: {declared_tasks}, а в файле найдено: {len(task_items)} — файл повреждён")
    if v2 and declared_tasks is None and not task_items:
        raise ValueError("В заголовке нет строки tasks — файл повреждён")
    return Parsed(items, task_items if v2 else None, support)


def _parse_task(lines: list[str], i: int, out: list, seen_ids: set) -> int:
    """Разбирает блок задания, начиная со строки TASK_BEGIN; добавляет TaskItem в out, возвращает индекс следующей строки."""
    n = len(lines)
    start_line = i + 1
    i += 1
    fields: dict[str, str] = {}
    keys = ("id", "button", "reward", "screenshot", "one-time", "visible", "about-lines", "comment-lines")
    counts: dict[str, int] = {}
    while True:
        if i >= n:
            raise ValueError(f"Задание со строки {start_line}: нет строки comment-lines")
        line = lines[i]
        key, sep, value = line.partition(":")
        key = key.strip().lower()
        if not sep or key not in keys:
            raise ValueError(f"Строка {i + 1}: неизвестное поле «{line.strip()[:40]}»")
        if key in fields:
            raise ValueError(f"Строка {i + 1}: поле {key} повторяется")
        fields[key] = value.strip()
        i += 1
        if key.endswith("-lines"):
            try:
                counts[key] = int(fields[key])
                if counts[key] < 0:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Строка {i}: {key} — целое число от 0") from None
        if "about-lines" in counts and "comment-lines" in counts:
            break
    about_n, comment_n = counts["about-lines"], counts["comment-lines"]
    body = lines[i:i + about_n + comment_n]
    if len(body) < about_n + comment_n:
        raise ValueError(f"Задание со строки {start_line}: текст короче заявленного — файл обрезан")
    i += about_n + comment_n
    if i >= n or lines[i].strip() != TASK_END:
        raise ValueError(f"Строка {i + 1}: после текста задания ожидалось «{TASK_END}» (число строк не совпадает)")
    i += 1
    try:
        if "button" not in fields or "reward" not in fields:
            raise ValueError("нужны поля button и reward")
        tid = None
        if fields.get("id"):
            if not fields["id"].isascii() or not fields["id"].isdigit() or not 1 <= int(fields["id"]) <= MAX_ID:
                raise ValueError("id — целое число")
            tid = int(fields["id"])
            if tid in seen_ids:
                raise ValueError(f"id {tid} повторяется")
            seen_ids.add(tid)
        out.append(TaskItem(
            id=tid, button_text=tasks.normalize_button_text(fields["button"]), reward=tasks.normalize_reward(fields["reward"]),
            about=tasks.normalize_description("\n".join(body[:about_n])), comment=tasks.normalize_comment("\n".join(body[about_n:])),
            screenshot=_bool(fields["screenshot"], start_line) if fields.get("screenshot") else False,
            one_time=_bool(fields["one-time"], start_line) if fields.get("one-time") else True,
            visible=_bool(fields["visible"], start_line) if fields.get("visible") else True,
        ))
    except ValueError as e:
        raise ValueError(f"Задание со строки {start_line}: {e}") from None
    return i


def parse_products(text: str) -> list[Item]:
    """Файл старого формата (только товары) -> список товаров."""
    return _parse(text, False).products


def parse_all(text: str) -> Parsed:
    """Файл экспорта (v2: товары + задания, либо старый v1: только товары) -> Parsed. ValueError с понятным сообщением,
    если файл повреждён: ничего не применяется, пока разбор не прошёл целиком."""
    first = next((ln.strip() for ln in str(text or "").lstrip("\ufeff").splitlines() if ln.strip()), "")
    return _parse(text, first == HEADER_V2)
