import os
import asyncio
import logging
import json
from functools import wraps
import socket
import signal
import time

import database
from dotenv import load_dotenv
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

CONFIG_FILE = 'config.json'
config_lock = asyncio.Lock()
telegram_tag = ''

(
    AS_NAME, AS_HOST, AS_PORT,
    AC_ID,
    SI_VALUE
) = range(5)


async def load_json_async(lock, filename):
    async with lock:
        if not os.path.exists(filename) or os.path.getsize(filename) == 0:
            return {}
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)

async def save_json_async(lock, filename, data):
    async with lock:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

def escape_markdown(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>+-=|{}.!'
    return "".join(f"\\{char}" if char in escape_chars else char for char in text)

def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        config = await load_json_async(config_lock, CONFIG_FILE)
        admin_chat_ids = config.get('admin_chat_ids', [])

        if user_id not in admin_chat_ids:
            if update.callback_query:
                await update.callback_query.answer("⛔ У вас нет прав для этого действия.", show_alert=True)
            else:
                await update.message.reply_text("⛔ У вас нет прав для выполнения этой команды.")

            user_tag = update.effective_user.username or 'N/A'
            logger.warning(f"Несанкционированный доступ к {func.__name__} от {user_id} (@{user_tag})")
            text = f"Несанкционированный доступ к `{func.__name__}` от @{user_tag} \\| `{user_id}`"
            for chat_id in admin_chat_ids:
                try:
                    await context.bot.send_message(chat_id, text, parse_mode=ParseMode.MARKDOWN_V2)
                except TelegramError as e:
                    logger.error(f"Не удалось отправить уведомление о доступе в чат {chat_id}: {e}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

async def check_server(host, port, retries, delay, timeout):
    last_error = ""
    for attempt in range(retries):
        try:
            start_time = time.monotonic()
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            end_time = time.monotonic()
            latency_ms = int((end_time - start_time) * 1000)
            writer.close()
            await writer.wait_closed()
            return True, "✅ Онлайн", latency_ms
        except asyncio.TimeoutError:
            last_error = "❌ Таймаут"
        except ConnectionRefusedError:
            last_error = "❌ Соединение отклонено"
        except OSError as e:
            if isinstance(e, socket.gaierror):
                logger.warning(f"Ошибка DNS при проверке {host}:{port}: {e}")
                return False, "❌ Ошибка DNS", -1
            logger.warning(f"Ошибка сети при проверке {host}:{port} (попытка {attempt+1}/{retries}): {e.strerror}")
            last_error = f"❌ Ошибка сети"
        if attempt < retries - 1:
            await asyncio.sleep(delay)
    return False, last_error, -1

async def monitoring_job(context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot
    servers = database.get_servers()
    if not servers: return

    check_retries = context.bot_data.get('check_retries', 3)
    check_retry_delay = context.bot_data.get('check_retry_delay', 2)
    check_single_timeout = context.bot_data.get('check_single_timeout', 5)
    failure_threshold = context.bot_data.get('failure_threshold', 3)
    
    config = await load_json_async(config_lock, CONFIG_FILE)
    admin_chat_ids = config.get('admin_chat_ids', [])

    for server in servers:
        server_id = server['id']
        is_alive, _, latency_ms = await check_server(
            server['host'], server['port'],
            retries=check_retries, delay=check_retry_delay, timeout=check_single_timeout
        )

        database.update_server_status(server_id, is_alive, latency_ms)

        failure_counter_id = f"failure_count_{server_id}"
        alert_sent_id = f"alert_sent_{server_id}"
        
        current_failures = context.bot_data.get(failure_counter_id, 0)
        alert_was_sent = context.bot_data.get(alert_sent_id, False)

        if is_alive:
            if alert_was_sent:
                database.log_downtime_event(server_id, 'UP') 
                context.bot_data[alert_sent_id] = False 
                name = escape_markdown(server['name'])
                message = f"✅ *ВОССТАНОВЛЕНИЕ* ✅\n\nСервер *{name}* снова в строю\\!"
                for chat_id in admin_chat_ids:
                    try: await bot.send_message(chat_id, message, parse_mode=ParseMode.MARKDOWN_V2)
                    except TelegramError as e: logger.error(f"Не удалось отправить сообщение о восстановлении в чат {chat_id}: {e}")
            if current_failures > 0:
                context.bot_data[failure_counter_id] = 0
        else:
            current_failures += 1
            context.bot_data[failure_counter_id] = current_failures
            if current_failures >= failure_threshold and not alert_was_sent:
                database.log_downtime_event(server_id, 'DOWN') 
                context.bot_data[alert_sent_id] = True
                name = escape_markdown(server['name'])
                host_adress = escape_markdown(f"{server['host']}")
                message = (f"🚨 *ТРЕВОГА: СЕРВЕР НЕДОСТУПЕН* 🚨\n\n"
                           f"*Имя:* {name}\n*Адрес:* `{host_adress}`\n\n"
                           f"Сервер не отвечает после *{failure_threshold}* проверок подряд\\. Возможна блокировка или сбой\\.")
                for chat_id in admin_chat_ids:
                    try: await bot.send_message(chat_id, message, parse_mode=ParseMode.MARKDOWN_V2)
                    except TelegramError as e: logger.error(f"Не удалось отправить тревожное сообщение в чат {chat_id}: {e}")

async def build_main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("📊 Статус и Настройки", callback_data="menu:status")],
        [InlineKeyboardButton("⚡️ Проверить сейчас", callback_data="menu:check_now")],
        [
            InlineKeyboardButton("➕ Добавить сервер", callback_data="conv_add_server:start"),
            InlineKeyboardButton("➖ Удалить сервер", callback_data="menu:remove_server_list"),
        ],
        [
            InlineKeyboardButton("🌍 Опубликовать", callback_data="menu:publish_list"),
            InlineKeyboardButton("🔒 Скрыть", callback_data="menu:hide_list"),
        ],
        [
            InlineKeyboardButton("🔔 Добавить чат", callback_data="conv_add_chat:start"),
            InlineKeyboardButton("🔕 Удалить чат", callback_data="menu:remove_chat_list"),
        ],
        [InlineKeyboardButton("⏰ Изменить интервал", callback_data="conv_set_interval:start")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def get_status_text():
    config = await load_json_async(config_lock, CONFIG_FILE)
    all_servers = database.get_all_servers_with_status()
    chats_str = ", ".join(map(str, config.get('admin_chat_ids', [])))
    chats = escape_markdown(chats_str) if chats_str else "пусто"

    text_parts = [
        f"*Интервал проверки:* {escape_markdown(config.get('check_interval_seconds', 'N/A'))} секунд",
        f"*Чаты для оповещений:* `{chats}`",
        "\n*Отслеживаемые серверы:*"
    ]
    if not all_servers:
        text_parts.append("_Список пуст_")
    else:
        for s in all_servers:
            name = escape_markdown(s['name'])
            host = escape_markdown(s['host'])
            text_parts.append(f"\\- *{name}* \\- `{host}:{s['port']}`")
    
    text_parts.append("\n*Сервера в продакшене:*")
    public_servers = [s for s in all_servers if s['is_public']]
    if not public_servers:
        text_parts.append("_Нет серверов в продакшене_")
    else:
        for s in public_servers:
            name = escape_markdown(s['name'])
            host = escape_markdown(s['host'])
            text_parts.append(f"\\- *{name}* \\- `{host}:{s['port']}`")
            
    return "\n".join(text_parts)

async def perform_check_and_format(context: ContextTypes.DEFAULT_TYPE):
    servers = database.get_servers()
    if not servers: return "Список серверов пуст\\. Нечего проверять\\.", False

    check_retries = context.bot_data.get('check_retries', 3)
    check_retry_delay = context.bot_data.get('check_retry_delay', 2)
    check_single_timeout = context.bot_data.get('check_single_timeout', 5)

    tasks = []
    for server in servers:
        task = asyncio.create_task(
            check_server(server['host'], server['port'], check_retries, check_retry_delay, check_single_timeout)
        )
        tasks.append((server, task))

    results = []
    for server, task in tasks:
        is_alive, _, latency = await task
        database.update_server_status(server['id'], is_alive, latency)
        ping_str = f"\\({latency}ms\\)" if latency != -1 else ""
        status_icon = "✅" if is_alive else "❌"
        status_text = "Онлайн" if is_alive else "Офлайн"
        name = escape_markdown(server['name'])
        host_link = f"http://{server['host']}"
        results.append(f"*{name}* {ping_str} [адрес]({host_link}) \\- {status_icon} {status_text}")
    
    return "\n".join(results), True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = await load_json_async(config_lock, CONFIG_FILE)
    is_admin = user_id in config.get('admin_chat_ids', [])
    text = "👋 Привет\\! Я бот для мониторинга твоих серверов\\.\n\n"

    query = update.callback_query
    if is_admin:
        text += "Выберите действие из меню:"
        keyboard = await build_main_menu_keyboard()
        if query:
            await query.answer()
            await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
        return

    if not config.get('admin_chat_ids'):
        text += (f"Сейчас нет ни одного администратора\\. Чтобы начать, "
                 f"добавьте себя командой:\n`\\/add_chat {user_id}`")
    else:
        text += "По всем вопросам связанным с ботом, писать @"+telegram_tag
    
    if query:
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "*Доступные команды:*\n\n"
        "`/start` \\- Открыть главное меню\n"
        "`/check_now` \\- Принудительная проверка\n"
        "`/status` \\- Показать текущие настройки\n"
        "`/add_server <имя> <хост> <порт>` \\- Добавить сервер\n"
        "`/remove_server <имя>` \\- Удалить сервер\n"
        "`/publish <имя>` \\- Показать сервер на сайте\n"
        "`/hide <имя>` \\- Скрыть сервер с сайта\n"
        "`/add_chat <id>` \\- Добавить чат для оповещений\n"
        "`/remove_chat <id>` \\- Удалить чат\n"
        "`/set_interval <секунды>` \\- Изменить интервал проверки"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

async def post_init(application: Application):
    commands = [
        BotCommand("start", "🚀 Открыть главное меню"),
        BotCommand("status", "📊 Текущий статус"),
        BotCommand("check_now", "⚡️ Проверить все серверы"),
        BotCommand("help", "❓ Справка по текстовым командам"),
        BotCommand("cancel", "❌ Отменить текущее действие"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Команды бота успешно установлены.")



@admin_only
async def menu_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    text = await get_status_text()
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]])
    await query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def menu_check_now(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(text="🔍 Начинаю принудительную проверку\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    final_text, _ = await perform_check_and_format(context)
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]])
    await query.edit_message_text(final_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

@admin_only
async def check_now_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔍 Начинаю принудительную проверку...", parse_mode=ParseMode.MARKDOWN_V2)
    final_text, _ = await perform_check_and_format(context)
    await msg.edit_text(final_text, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = await get_status_text()
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def add_server_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("Неверный формат\\. Используйте:\n`/add_server <имя> <домен/ip> <порт>`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    port_str = args[-1]; host = args[-2]; name = " ".join(args[:-2])
    if not port_str.isdigit() or not 0 < int(port_str) < 65536:
        await update.message.reply_text("Порт должен быть числом от 1 до 65535.")
        return
    if database.add_server(name, host, int(port_str)):
        await update.message.reply_text(f"✅ Сервер `{escape_markdown(name)}` успешно добавлен\\!", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("Сервер с таким именем уже существует.")

@admin_only
async def remove_server_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = " ".join(context.args)
    if not name: await update.message.reply_text("Укажите имя сервера."); return
    if database.remove_server(name):
        await update.message.reply_text(f"🗑️ Сервер '{escape_markdown(name)}' удален\\.", parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text("Сервер не найден.")
        
async def generic_list_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, item_type: str):
    query = update.callback_query
    await query.answer()

    items = []
    action_prefix = ""
    title = ""
    name_key, id_key = 'name', 'id'

    if item_type == 'remove_server':
        items = database.get_servers()
        action_prefix = "action_remove_server"
        title = "Выберите сервер для *удаления*:"
    elif item_type == 'publish_server':
        items = [s for s in database.get_all_servers_with_status() if not s['is_public']]
        action_prefix = "action_publish"
        title = "Выберите сервер для *публикации* на сайте:"
    elif item_type == 'hide_server':
        items = [s for s in database.get_all_servers_with_status() if s['is_public']]
        action_prefix = "action_hide"
        title = "Выберите сервер, чтобы *скрыть* с сайта:"
    elif item_type == 'remove_chat':
        config = await load_json_async(config_lock, CONFIG_FILE)
        items = [{'id': chat_id, 'name': str(chat_id)} for chat_id in config.get('admin_chat_ids', [])]
        action_prefix = "action_remove_chat"
        title = "Выберите чат для *удаления* из оповещений:"
        id_key = name_key = 'id'

    if not items:
        title = "Список пуст, действие не требуется."
        keyboard = [[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]]
    else:
        keyboard_buttons = [[InlineKeyboardButton(str(item[name_key]), callback_data=f"{action_prefix}:{item[id_key]}")] for item in items]
        keyboard_buttons.append([InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")])
        keyboard = keyboard_buttons

    await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def generic_action_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    action, item_id = query.data.split(':')
    message = "Действие не выполнено"
    
    if action == "action_remove_server":
        server_to_remove = next((s for s in database.get_servers() if s['id'] == int(item_id)), None)
        if server_to_remove and database.remove_server(server_to_remove['name']):
            message = f"🗑️ Сервер *{escape_markdown(server_to_remove['name'])}* удален\\."
    elif action == "action_publish" or action == "action_hide":
        is_public = action == "action_publish"
        server_to_update = next((s for s in database.get_all_servers_with_status() if s['id'] == int(item_id)), None)
        if server_to_update and database.set_server_public(server_to_update['name'], is_public):
            status = "теперь *отображается* на сайте" if is_public else "больше *не отображается* на сайте"
            message = f"✅ Сервер *{escape_markdown(server_to_update['name'])}* {status}\\."
    elif action == "action_remove_chat":
        chat_id_to_remove = int(item_id)
        config = await load_json_async(config_lock, CONFIG_FILE)
        if chat_id_to_remove in config.get('admin_chat_ids', []):
            config['admin_chat_ids'].remove(chat_id_to_remove)
            await save_json_async(config_lock, CONFIG_FILE, config)
            message = f"🔕 Чат `{chat_id_to_remove}` удален из списка оповещений\\."

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]])
    await query.edit_message_text(message, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def publish_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    server_name = " ".join(context.args)
    if not server_name: await update.message.reply_text("Укажите имя сервера."); return
    if database.set_server_public(server_name, True):
        await update.message.reply_text(f"✅ Сервер '{escape_markdown(server_name)}' теперь отображается на сайте.")
    else: await update.message.reply_text("❌ Сервер не найден.")

@admin_only
async def hide_server(update: Update, context: ContextTypes.DEFAULT_TYPE):
    server_name = " ".join(context.args)
    if not server_name: await update.message.reply_text("Укажите имя сервера."); return
    if database.set_server_public(server_name, False):
        await update.message.reply_text(f"🔒 Сервер '{escape_markdown(server_name)}' больше не отображается на сайте.")
    else: await update.message.reply_text("❌ Сервер не найден.")


async def add_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config = await load_json_async(config_lock, CONFIG_FILE)
    if config.get('admin_chat_ids') and update.effective_user.id not in config.get('admin_chat_ids', []):
        await update.message.reply_text("⛔ Эту команду могут выполнять только уже добавленные администраторы.")
        return
    if not context.args or not context.args[0].lstrip('-').isdigit():
        await update.message.reply_text(f"Укажите ID чата\\. Ваш личный ID: `{update.effective_user.id}`", parse_mode=ParseMode.MARKDOWN_V2)
        return
    chat_id = int(context.args[0])
    if 'admin_chat_ids' not in config: config['admin_chat_ids'] = []
    if chat_id in config['admin_chat_ids']:
        await update.message.reply_text("Этот чат уже есть в списке."); return
    
    config['admin_chat_ids'].append(chat_id)
    await save_json_async(config_lock, CONFIG_FILE, config)
    await update.message.reply_text(f"✅ Чат `{chat_id}` успешно добавлен\\.", parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def remove_chat_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].lstrip('-').isdigit(): await update.message.reply_text("Укажите ID чата."); return
    chat_id = int(context.args[0])
    config = await load_json_async(config_lock, CONFIG_FILE)
    if chat_id not in config.get('admin_chat_ids', []):
        await update.message.reply_text("Этого чата нет в списке."); return
    config['admin_chat_ids'].remove(chat_id)
    await save_json_async(config_lock, CONFIG_FILE, config)
    await update.message.reply_text(f"🗑️ Чат `{chat_id}` удален.", parse_mode=ParseMode.MARKDOWN_V2)

@admin_only
async def set_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].isdigit(): await update.message.reply_text("Используйте: `\\/set_interval <секунды>`"); return
    new_interval = int(context.args[0])
    if new_interval < 30: await update.message.reply_text("Интервал < 30 секунд не рекомендуется."); return

    config = await load_json_async(config_lock, CONFIG_FILE)
    config['check_interval_seconds'] = new_interval
    await save_json_async(config_lock, CONFIG_FILE, config)
    
    current_jobs = context.job_queue.get_jobs_by_name("monitoring_job")
    for job in current_jobs: job.schedule_removal()
    context.job_queue.run_repeating(monitoring_job, interval=new_interval, name="monitoring_job", first=1)
    await update.message.reply_text(f"✅ Интервал проверки изменен на {new_interval} секунд.")


async def conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "Действие отменено\\."
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN_V2)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

@admin_only
async def add_server_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['conv_message'] = await query.edit_message_text(
        "Шаг 1/3: Введите *название* нового сервера:", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")]]), 
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return AS_NAME

@admin_only
async def add_server_get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['server_name'] = update.message.text
    await update.message.delete()
    conv_message = context.user_data['conv_message']
    await conv_message.edit_text(
        "Шаг 2/3: Отлично\\! Теперь введите *домен или IP-адрес*:", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")]]), 
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return AS_HOST

@admin_only
async def add_server_get_host(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['server_host'] = update.message.text
    await update.message.delete()
    conv_message = context.user_data['conv_message']
    await conv_message.edit_text(
        "Шаг 3/3: Принято\\! И последнее: введите *порт*:", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return AS_PORT

@admin_only
async def add_server_get_port(update: Update, context: ContextTypes.DEFAULT_TYPE):
    port_str = update.message.text
    await update.message.delete()
    conv_message = context.user_data['conv_message']
    
    text = ""
    if not port_str.isdigit() or not 0 < int(port_str) < 65536:
        text = "❌ *Ошибка:* Порт должен быть числом от 1 до 65535\\. Операция отменена\\."
    else:
        name = context.user_data['server_name']
        host = context.user_data['server_host']
        if database.add_server(name, host, int(port_str)):
            text = f"✅ Сервер *{escape_markdown(name)}* успешно добавлен\\!"
        else:
            text = f"❌ Сервер с именем *{escape_markdown(name)}* уже существует\\."

    await conv_message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]]), parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

@admin_only
async def add_chat_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    context.user_data['conv_message'] = await query.edit_message_text(
        f"Введите ID чата или пользователя, которого хотите добавить в администраторы\\.\n\nВаш ID: `{update.effective_user.id}`",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")]]), 
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return AC_ID

@admin_only
async def add_chat_get_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id_str = update.message.text
    await update.message.delete()
    conv_message = context.user_data['conv_message']
    text = ""
    
    if not chat_id_str.lstrip('-').isdigit():
        text = "❌ *Ошибка:* ID должен быть числом\\. Операция отменена\\."
    else:
        chat_id = int(chat_id_str)
        config = await load_json_async(config_lock, CONFIG_FILE)
        if 'admin_chat_ids' not in config: config['admin_chat_ids'] = []
        if chat_id in config['admin_chat_ids']:
            text = f"Информационно: Чат `{chat_id}` уже был в списке\\."
        else:
            config['admin_chat_ids'].append(chat_id)
            await save_json_async(config_lock, CONFIG_FILE, config)
            text = f"✅ Чат `{chat_id}` успешно добавлен в список оповещений\\."
            try: await context.bot.send_message(chat_id, "✅ Этот чат был успешно добавлен для получения оповещений от бота.")
            except Exception: pass
            
    await conv_message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]]), parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

@admin_only
async def set_interval_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    config = await load_json_async(config_lock, CONFIG_FILE)
    current_interval = config.get('check_interval_seconds', 'N/A')
    context.user_data['conv_message'] = await query.edit_message_text(
        f"Текущий интервал проверки: *{current_interval}* секунд\\.\n\nВведите новое значение в секундах \\(рекомендуется не менее 30\\):",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="conv:cancel")]]),
        parse_mode=ParseMode.MARKDOWN_V2
    )
    return SI_VALUE

@admin_only
async def set_interval_get_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    interval_str = update.message.text
    await update.message.delete()
    conv_message = context.user_data['conv_message']
    text = ""

    if not interval_str.isdigit():
        text = "❌ *Ошибка:* Интервал должен быть числом\\. Операция отменена\\."
    elif int(interval_str) < 30:
        text = "❌ *Ошибка:* Интервал не может быть меньше 30 секунд\\. Операция отменена\\."
    else:
        new_interval = int(interval_str)
        config = await load_json_async(config_lock, CONFIG_FILE)
        config['check_interval_seconds'] = new_interval
        await save_json_async(config_lock, CONFIG_FILE, config)
        
        current_jobs = context.job_queue.get_jobs_by_name("monitoring_job")
        for job in current_jobs: job.schedule_removal()
        context.job_queue.run_repeating(monitoring_job, interval=new_interval, name="monitoring_job", first=1)
        text = f"✅ Интервал проверки успешно изменен на *{new_interval}* секунд\\."
    
    await conv_message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад в меню", callback_data="menu:back_to_main")]]), parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.clear()
    return ConversationHandler.END

@admin_only
async def restart_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        logging.info("Перезапускаем бота...")
        await update.message.reply_text("Бот перезапускается\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)

        pid = os.getpid()
        os.kill(pid, signal.SIGTERM)
        logging.info(f"Отправлен сигнал SIGTERM процессу {pid}")

    except Exception as e:
        logging.exception("Произошла ошибка при попытке перезапуска бота.")
        await update.message.reply_text(f"Произошла ошибка: `{e}`", parse_mode=ParseMode.MARKDOWN_V2)

def main():
    database.migrate_db(); database.init_db()
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    global telegram_tag; telegram_tag = os.getenv("TELEGRAM_TAG", "your_telegram_tag")
    if not token: logger.critical("Не найден TELEGRAM_BOT_TOKEN в .env! Выход."); return

    if not database.get_servers() and os.path.exists('servers.json'):
        logger.info("База данных пуста. Импорт из servers.json...")
        try:
            with open('servers.json', 'r', encoding='utf-8') as f:
                servers_data = json.load(f)
                for s in servers_data:
                    database.add_server(s['name'], s['host'], s['port'])
            logger.info("Импорт завершен. servers.json больше не используется.")
        except Exception as e: logger.error(f"Не удалось импортировать серверы: {e}")

    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump({"admin_chat_ids": [], "check_interval_seconds": 300}, f, indent=2)

    try:
        with open(CONFIG_FILE, 'r') as f: check_interval = json.load(f).get('check_interval_seconds', 300)
    except (FileNotFoundError, json.JSONDecodeError): check_interval = 300

    application = Application.builder().token(token).post_init(post_init).build()
    
    try:
        application.bot_data['check_retries'] = int(os.getenv("CHECK_RETRIES", 3))
        application.bot_data['check_retry_delay'] = int(os.getenv("CHECK_RETRY_DELAY", 2))
        application.bot_data['check_single_timeout'] = int(os.getenv("CHECK_SINGLE_TIMEOUT", 5))
        application.bot_data['failure_threshold'] = int(os.getenv("FAILURE_THRESHOLD", 3))
    except (ValueError, TypeError):
        logger.error("Ошибка при чтении .env. Используются значения по умолчанию.")
        application.bot_data.update({'check_retries': 3, 'check_retry_delay': 2, 'check_single_timeout': 5, 'failure_threshold': 3})

    conv_add_server = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_server_start, pattern='^conv_add_server:start$')],
        states={
            AS_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_get_name)],
            AS_HOST: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_get_host)],
            AS_PORT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_server_get_port)],
        },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern='^conv:cancel$'), CommandHandler('cancel', conv_cancel)],
        conversation_timeout=120
    )
    conv_add_chat = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_chat_start, pattern='^conv_add_chat:start$')],
        states={ AC_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_chat_get_id)] },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern='^conv:cancel$'), CommandHandler('cancel', conv_cancel)],
        conversation_timeout=60
    )
    conv_set_interval = ConversationHandler(
        entry_points=[CallbackQueryHandler(set_interval_start, pattern='^conv_set_interval:start$')],
        states={ SI_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_interval_get_value)] },
        fallbacks=[CallbackQueryHandler(conv_cancel, pattern='^conv:cancel$'), CommandHandler('cancel', conv_cancel)],
        conversation_timeout=60
    )
    
    application.add_handler(conv_add_server)
    application.add_handler(conv_add_chat)
    application.add_handler(conv_set_interval)
    
    command_handlers = [
        CommandHandler("start", start), CommandHandler("help", help_command),
        CommandHandler("status", status_command), CommandHandler("check_now", check_now_command),
        CommandHandler("add_server", add_server_command), CommandHandler("remove_server", remove_server_command),
        CommandHandler("publish", publish_server), CommandHandler("hide", hide_server),
        CommandHandler("set_interval", set_interval_command), CommandHandler("add_chat", add_chat_command),
        CommandHandler("remove_chat", remove_chat_command),CommandHandler("restart", restart_bot)
    ]
    application.add_handlers(command_handlers)
    
    application.add_handler(CallbackQueryHandler(start, pattern=r"^menu:back_to_main$"))
    application.add_handler(CallbackQueryHandler(menu_status, pattern=r"^menu:status$"))
    application.add_handler(CallbackQueryHandler(menu_check_now, pattern=r"^menu:check_now$"))
    
    application.add_handler(CallbackQueryHandler(lambda u,c: generic_list_menu(u,c,'remove_server'), pattern=r"^menu:remove_server_list$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: generic_list_menu(u,c,'publish_server'), pattern=r"^menu:publish_list$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: generic_list_menu(u,c,'hide_server'), pattern=r"^menu:hide_list$"))
    application.add_handler(CallbackQueryHandler(lambda u,c: generic_list_menu(u,c,'remove_chat'), pattern=r"^menu:remove_chat_list$"))

    application.add_handler(CallbackQueryHandler(generic_action_handler, pattern=r"^action_"))

    if application.job_queue:
        application.job_queue.run_repeating(monitoring_job, interval=check_interval, name="monitoring_job", first=5)
    
    logger.info(f"Бот запускается. Интервал проверки: {check_interval} сек.")
    logger.info(f"Параметры проверки: Попыток={application.bot_data['check_retries']}, Порог сбоев={application.bot_data['failure_threshold']}.")
    
    application.run_polling()


if __name__ == "__main__":
    main()