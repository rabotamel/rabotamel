"""«Предложка»: меню в боте и проверка присланного видео.

Пока у пользователя открыто меню предложки (handlers: suggest_menu), всё, что он присылает, проходит `check`.
Ограничений по частоте отправки нет: видео можно присылать подряд. Тексты меню и кнопки рандомизируются (suggest_ui.py); сообщения-ответы фиксированные.
"""
REWARD = 2                 # 🍬 автору за каждое принятое видео
MIN_SECONDS = 30
MAX_SECONDS = 3600         # 1 час
CANDY = "🍬"

MSG_NOT_VIDEO = "❌ В предложку можно отправлять только видео"
MSG_TOO_SHORT = f"❌ Видео должно быть не короче {MIN_SECONDS} секунд"
MSG_TOO_LONG = "❌ Видео не должно превышать 1 час"
MSG_ACCEPTED = "✅ Ваше видео принято на рассмотрение."


def check(message) -> str | None:
    """Текст ошибки для пользователя или None, если видео подходит.
    Принимается только обычное видео (message.video); GIF, кружки, файлы и текст — «только видео»."""
    video = getattr(message, "video", None)
    if video is None:
        return MSG_NOT_VIDEO
    duration = int(getattr(video, "duration", 0) or 0)
    if duration < MIN_SECONDS:
        return MSG_TOO_SHORT
    if duration > MAX_SECONDS:
        return MSG_TOO_LONG
    return None
