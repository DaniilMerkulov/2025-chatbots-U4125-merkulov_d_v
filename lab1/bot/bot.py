"""
Telegram-бот «Отчет по Wildberries».

Что делает бот: каждое утро присылает продавцу сводку показателей
его магазина за прошедший день, чтобы не заходить в кабинет вручную.

Запуск: python bot.py
Перед запуском нужно создать файл .env и указать в нем BOT_TOKEN.
"""

import logging
import os
import sqlite3
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import wb_api

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Загружаем переменные из файла .env (там лежит токен бота)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_PATH = "bot_database.db"
MOSCOW = ZoneInfo("Europe/Moscow")
DEFAULT_NOTIFY_TIME = "09:00"

# Картинка, которую бот показывает при приветствии.
# Лежит рядом с bot.py в папке images.
WELCOME_IMAGE = Path(__file__).parent / "images" / "welcome.png"

# Настраиваем логирование, чтобы видеть, что происходит с ботом
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
# Убираем лишние технические сообщения библиотеки запросов
logging.getLogger("httpx").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Создает таблицу пользователей, если ее еще нет."""
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            telegram_id    INTEGER PRIMARY KEY,
            api_key        TEXT,
            notify_time    TEXT    DEFAULT '09:00',
            notify_enabled INTEGER DEFAULT 1
        )
        """
    )
    connection.commit()
    connection.close()
    logger.info("База данных готова к работе")


def get_user(telegram_id: int) -> dict:
    """
    Возвращает данные пользователя из базы.
    Если пользователя еще нет, возвращает None.
    """
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    row = connection.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    connection.close()
    return dict(row) if row else None


def create_user_if_needed(telegram_id: int) -> None:
    """Добавляет пользователя в базу при первом обращении к боту."""
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        "INSERT OR IGNORE INTO users (telegram_id, notify_time, notify_enabled) "
        "VALUES (?, ?, 1)",
        (telegram_id, DEFAULT_NOTIFY_TIME),
    )
    connection.commit()
    connection.close()


def save_api_key(telegram_id: int, api_key: str) -> None:
    """Сохраняет API-ключ пользователя."""
    create_user_if_needed(telegram_id)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        "UPDATE users SET api_key = ? WHERE telegram_id = ?", (api_key, telegram_id)
    )
    connection.commit()
    connection.close()
    # В лог записываем только факт сохранения, сам ключ не пишем
    logger.info("Пользователь %s сохранил API-ключ", telegram_id)


def set_notify_enabled(telegram_id: int, enabled: bool) -> None:
    """Включает или выключает ежедневную рассылку."""
    create_user_if_needed(telegram_id)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        "UPDATE users SET notify_enabled = ? WHERE telegram_id = ?",
        (1 if enabled else 0, telegram_id),
    )
    connection.commit()
    connection.close()


def set_notify_time(telegram_id: int, time_text: str) -> None:
    """Задает время ежедневной рассылки."""
    create_user_if_needed(telegram_id)
    connection = sqlite3.connect(DB_PATH)
    connection.execute(
        "UPDATE users SET notify_time = ? WHERE telegram_id = ?",
        (time_text, telegram_id),
    )
    connection.commit()
    connection.close()


def get_subscribed_users() -> list:
    """Возвращает всех пользователей с включенной рассылкой."""
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    rows = connection.execute(
        "SELECT * FROM users WHERE notify_enabled = 1 AND api_key IS NOT NULL"
    ).fetchall()
    connection.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def format_money(value: float) -> str:
    """Форматирует сумму: 125400 превращается в 125 400."""
    return f"{value:,.0f}".replace(",", " ")


def hide_key(api_key: str) -> str:
    """
    Прячет API-ключ, оставляя видимыми только последние 4 символа.
    Нужно, чтобы случайно не показать ключ целиком в переписке.
    """
    if not api_key:
        return "не задан"
    return "•" * 8 + api_key[-4:]


def build_report_text(stats: dict) -> str:
    """Собирает текст отчета из показателей."""
    report_date = stats["date"].strftime("%d.%m.%Y")

    return (
        f"📊 <b>Отчет за {report_date}</b>\n"
        f"\n"
        f"🛒 <b>Заказы</b>\n"
        f"{format_money(stats['orders_rub'])} ₽ · {stats['orders_count']} шт\n"
        f"\n"
        f"✅ <b>Выкупы</b>\n"
        f"{format_money(stats['buyouts_rub'])} ₽ · {stats['buyouts_count']} шт\n"
        f"\n"
        f"📣 <b>Реклама</b>\n"
        f"Затраты: {format_money(stats['ad_spend_rub'])} ₽\n"
        f"ДРР: {stats['drr_percent']:.1f}%\n"
        f"\n"
        f"🔄 <b>Конверсии</b>\n"
        f"Переход → корзина: {stats['conversion_to_cart_percent']:.1f}%\n"
        f"Корзина → заказ: {stats['conversion_cart_to_order_percent']:.1f}%\n"
        f"Переход → заказ: {stats['conversion_to_order_percent']:.1f}%"
    )


def main_keyboard() -> InlineKeyboardMarkup:
    """Кнопки главного меню, чтобы не вводить команды вручную."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Отчет за вчера", callback_data="report")],
            [InlineKeyboardButton("🔑 Указать API-ключ", callback_data="setkey")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="settings")],
        ]
    )


def settings_keyboard(user: dict) -> InlineKeyboardMarkup:
    """Кнопки раздела настроек."""
    is_enabled = user and user.get("notify_enabled")
    toggle_text = "🔕 Выключить рассылку" if is_enabled else "🔔 Включить рассылку"

    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(toggle_text, callback_data="toggle_notify")],
            [
                InlineKeyboardButton("08:00", callback_data="time_08:00"),
                InlineKeyboardButton("09:00", callback_data="time_09:00"),
                InlineKeyboardButton("10:00", callback_data="time_10:00"),
            ],
            [InlineKeyboardButton("🕐 Другое время", callback_data="custom_time")],
            [InlineKeyboardButton("⬅️ В меню", callback_data="menu")],
        ]
    )


def is_valid_time(text: str) -> bool:
    """Проверяет, что пользователь ввел время в формате ЧЧ:ММ."""
    parts = text.strip().split(":")
    if len(parts) != 2:
        return False
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return False
    hours, minutes = int(parts[0]), int(parts[1])
    return 0 <= hours <= 23 and 0 <= minutes <= 59


# ---------------------------------------------------------------------------
# Ежедневная рассылка
# ---------------------------------------------------------------------------

async def send_daily_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет пользователю ежедневный отчет. Вызывается по расписанию."""
    telegram_id = context.job.chat_id
    user = get_user(telegram_id)

    if not user or not user.get("api_key") or not user.get("notify_enabled"):
        return

    try:
        stats = wb_api.get_daily_stats(user["api_key"])
        await context.bot.send_message(
            chat_id=telegram_id,
            text="☀️ Доброе утро!\n\n" + build_report_text(stats),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
        logger.info("Отправлен ежедневный отчет пользователю %s", telegram_id)
    except Exception as error:
        logger.error("Ошибка при отправке отчета пользователю %s: %s", telegram_id, error)


def schedule_user_job(application: Application, telegram_id: int, time_text: str) -> None:
    """
    Ставит задачу на ежедневную отправку отчета конкретному пользователю.
    Если задача для него уже была, старую убираем, чтобы не было дублей.
    """
    job_name = f"daily_{telegram_id}"

    for old_job in application.job_queue.get_jobs_by_name(job_name):
        old_job.schedule_removal()

    hours, minutes = map(int, time_text.split(":"))
    application.job_queue.run_daily(
        send_daily_report,
        time=dtime(hour=hours, minute=minutes, tzinfo=MOSCOW),
        chat_id=telegram_id,
        name=job_name,
    )
    logger.info("Рассылка для %s запланирована на %s МСК", telegram_id, time_text)


def remove_user_job(application: Application, telegram_id: int) -> None:
    """Убирает задачу на рассылку, если пользователь ее выключил."""
    for job in application.job_queue.get_jobs_by_name(f"daily_{telegram_id}"):
        job.schedule_removal()


# ---------------------------------------------------------------------------
# Команды бота
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start: приветствие и главное меню."""
    create_user_if_needed(update.effective_user.id)

    text = (
        "👋 <b>Привет!</b>\n"
        "\n"
        "Я помогаю продавцам Wildberries следить за магазином. "
        "Каждое утро присылаю сводку за прошедший день, "
        "чтобы не заходить в кабинет вручную.\n"
        "\n"
        "<b>Что в сводке:</b>\n"
        "• Заказы в рублях и штуках\n"
        "• Выкупы в рублях и штуках\n"
        "• Затраты на рекламу и ДРР\n"
        "• Конверсии: переход в корзину, корзина в заказ, переход в заказ\n"
        "\n"
        "Чтобы начать, укажите API-ключ из кабинета продавца."
    )

    await send_welcome_photo(update, context, text)


async def send_welcome_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str
) -> None:
    """
    Отправляет приветственную картинку с текстом под ней.

    Телеграм при первой отправке сохраняет файл у себя и возвращает его
    идентификатор file_id. Мы его запоминаем и дальше отправляем картинку
    по этому идентификатору, чтобы не загружать файл заново каждый раз.

    Если картинки нет на месте, бот просто отправит текст без неё,
    чтобы приветствие в любом случае сработало.
    """
    file_id = context.bot_data.get("welcome_file_id")

    try:
        if file_id:
            await update.message.reply_photo(
                photo=file_id,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=main_keyboard(),
            )
            return

        if WELCOME_IMAGE.exists():
            with open(WELCOME_IMAGE, "rb") as image_file:
                message = await update.message.reply_photo(
                    photo=image_file,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_keyboard(),
                )
            # Запоминаем идентификатор самой большой версии картинки
            context.bot_data["welcome_file_id"] = message.photo[-1].file_id
            logger.info("Приветственная картинка загружена в Telegram")
            return

        logger.warning("Файл картинки не найден: %s", WELCOME_IMAGE)
    except Exception as error:
        logger.error("Не удалось отправить картинку: %s", error)

    # Запасной вариант: приветствие без картинки
    await update.message.reply_text(
        caption, parse_mode=ParseMode.HTML, reply_markup=main_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /help: список всех команд."""
    text = (
        "<b>Команды бота</b>\n"
        "\n"
        "/start — главное меню\n"
        "/help — эта справка\n"
        "/setkey — указать API-ключ кабинета Wildberries\n"
        "/report — получить отчет за вчерашний день\n"
        "/settings — включить или выключить рассылку и задать время\n"
        "\n"
        "Все суммы в рублях, время московское."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def setkey_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /setkey: бот просит прислать ключ следующим сообщением."""
    create_user_if_needed(update.effective_user.id)

    # Запоминаем, что следующее сообщение от этого пользователя это ключ
    context.user_data["waiting_for"] = "api_key"

    await update.message.reply_text(
        "🔑 Пришлите ваш API-ключ из кабинета продавца Wildberries "
        "следующим сообщением.\n\n"
        "Ключ сохранится только для вас. "
        "После сохранения рекомендую удалить сообщение с ключом из переписки."
    )


async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /report: отчет за вчерашний день по запросу."""
    await send_report_now(update.effective_user.id, update.message.reply_text)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /settings: настройки рассылки."""
    user = get_user(update.effective_user.id)
    await update.message.reply_text(
        build_settings_text(user),
        parse_mode=ParseMode.HTML,
        reply_markup=settings_keyboard(user),
    )


def build_settings_text(user: dict) -> str:
    """Собирает текст раздела настроек."""
    if not user:
        return "⚙️ <b>Настройки</b>\n\nСначала отправьте команду /start."

    status = "включена" if user.get("notify_enabled") else "выключена"
    return (
        "⚙️ <b>Настройки</b>\n"
        "\n"
        f"API-ключ: {hide_key(user.get('api_key'))}\n"
        f"Ежедневная рассылка: {status}\n"
        f"Время отправки: {user.get('notify_time')} МСК"
    )


async def send_report_now(telegram_id: int, reply_function) -> None:
    """
    Общая функция отправки отчета по запросу.
    Используется и командой /report, и кнопкой в меню.
    """
    user = get_user(telegram_id)

    if not user or not user.get("api_key"):
        await reply_function(
            "🔑 Сначала укажите API-ключ командой /setkey, "
            "иначе я не смогу получить данные вашего магазина."
        )
        return

    try:
        stats = wb_api.get_daily_stats(user["api_key"])
        await reply_function(
            build_report_text(stats),
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard(),
        )
    except Exception as error:
        logger.error("Ошибка при формировании отчета: %s", error)
        await reply_function(
            "😔 Не удалось получить данные. "
            "Проверьте, что API-ключ указан верно, и попробуйте еще раз."
        )


# ---------------------------------------------------------------------------
# Обработка нажатий на кнопки
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обрабатывает нажатия на кнопки под сообщениями."""
    query = update.callback_query
    await query.answer()

    telegram_id = query.from_user.id
    action = query.data

    if action == "report":
        await send_report_now(telegram_id, query.message.reply_text)

    elif action == "setkey":
        context.user_data["waiting_for"] = "api_key"
        await query.message.reply_text(
            "🔑 Пришлите ваш API-ключ из кабинета продавца Wildberries "
            "следующим сообщением."
        )

    elif action == "settings":
        user = get_user(telegram_id)
        await query.message.reply_text(
            build_settings_text(user),
            parse_mode=ParseMode.HTML,
            reply_markup=settings_keyboard(user),
        )

    elif action == "menu":
        await query.message.reply_text("Главное меню:", reply_markup=main_keyboard())

    elif action == "toggle_notify":
        user = get_user(telegram_id)
        new_state = not (user and user.get("notify_enabled"))
        set_notify_enabled(telegram_id, new_state)

        if new_state:
            updated_user = get_user(telegram_id)
            schedule_user_job(context.application, telegram_id, updated_user["notify_time"])
            await query.message.reply_text("🔔 Ежедневная рассылка включена.")
        else:
            remove_user_job(context.application, telegram_id)
            await query.message.reply_text("🔕 Ежедневная рассылка выключена.")

    elif action.startswith("time_"):
        new_time = action.replace("time_", "")
        set_notify_time(telegram_id, new_time)

        user = get_user(telegram_id)
        if user.get("notify_enabled"):
            schedule_user_job(context.application, telegram_id, new_time)

        await query.message.reply_text(f"🕐 Время отправки изменено на {new_time} МСК.")

    elif action == "custom_time":
        context.user_data["waiting_for"] = "time"
        await query.message.reply_text(
            "🕐 Напишите время в формате ЧЧ:ММ, например 07:30"
        )


# ---------------------------------------------------------------------------
# Обработка обычных текстовых сообщений
# ---------------------------------------------------------------------------

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Разбирает обычные сообщения пользователя.
    Бот понимает их, только если перед этим о чем-то спросил.
    """
    telegram_id = update.effective_user.id
    text = update.message.text.strip()
    waiting_for = context.user_data.get("waiting_for")

    if waiting_for == "api_key":
        save_api_key(telegram_id, text)
        context.user_data["waiting_for"] = None

        await update.message.reply_text(
            f"✅ Ключ сохранен: {hide_key(text)}\n\n"
            "Теперь можно запросить отчет кнопкой ниже.\n"
            "Не забудьте удалить сообщение с ключом из переписки.",
            reply_markup=main_keyboard(),
        )

    elif waiting_for == "time":
        if not is_valid_time(text):
            await update.message.reply_text(
                "Не похоже на время. Напишите в формате ЧЧ:ММ, например 07:30"
            )
            return

        set_notify_time(telegram_id, text)
        context.user_data["waiting_for"] = None

        user = get_user(telegram_id)
        if user.get("notify_enabled"):
            schedule_user_job(context.application, telegram_id, text)

        await update.message.reply_text(
            f"🕐 Время отправки изменено на {text} МСК.",
            reply_markup=main_keyboard(),
        )

    else:
        await update.message.reply_text(
            "Не понял команду. Посмотрите список команд через /help "
            "или воспользуйтесь кнопками.",
            reply_markup=main_keyboard(),
        )


# ---------------------------------------------------------------------------
# Обработка ошибок
# ---------------------------------------------------------------------------

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ловит любые необработанные ошибки, чтобы бот не падал."""
    logger.error("Произошла ошибка: %s", context.error)

    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(
            "😔 Что-то пошло не так. Попробуйте еще раз или напишите /start."
        )


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------

async def restore_jobs(application: Application) -> None:
    """
    После запуска бота заново ставит задачи на рассылку
    для всех, кто ее включил. Иначе после перезапуска рассылка пропала бы.
    """
    for user in get_subscribed_users():
        schedule_user_job(application, user["telegram_id"], user["notify_time"])


def main() -> None:
    """Точка входа: настраивает и запускает бота."""
    if not BOT_TOKEN:
        raise SystemExit(
            "Не найден BOT_TOKEN. Создайте файл .env по образцу .env.example "
            "и укажите в нем токен бота."
        )

    init_db()

    application = Application.builder().token(BOT_TOKEN).post_init(restore_jobs).build()

    # Команды
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setkey", setkey_command))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("settings", settings_command))

    # Кнопки и обычные сообщения
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    # Обработчик ошибок
    application.add_error_handler(error_handler)

    logger.info("Бот запущен. Для остановки нажмите Ctrl+C")
    application.run_polling()


if __name__ == "__main__":
    main()
