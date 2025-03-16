import os
import json
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
from psycopg_pool import AsyncConnectionPool
import random
import asyncio

# Получаем токен и URL базы данных из переменных окружения
TOKEN = os.getenv("TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

# Состояния для ConversationHandler
PORTUGUESE, RUSSIAN, TEST_ANSWER, BULK_ADD, EDIT_PORTUGUESE, EDIT_RUSSIAN = range(6)

# Инициализация бота
bot = Bot(TOKEN)
application = ApplicationBuilder().token(TOKEN).build()

async def init_db(pool):
    async with pool.connection() as conn:
        async with conn.cursor() as c:
            await c.execute("DROP TABLE IF EXISTS stats CASCADE")
            await c.execute('''CREATE TABLE IF NOT EXISTS thesaurus 
                             (id SERIAL PRIMARY KEY, portuguese TEXT, russian TEXT)''')
            await c.execute('''CREATE TABLE IF NOT EXISTS stats 
                             (id INTEGER PRIMARY KEY, correct INTEGER DEFAULT 0, incorrect INTEGER DEFAULT 0, 
                              FOREIGN KEY(id) REFERENCES thesaurus(id))''')
            await c.execute('''CREATE TABLE IF NOT EXISTS history 
                             (id SERIAL PRIMARY KEY, word_id INTEGER, correct INTEGER, 
                              timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                              FOREIGN KEY(word_id) REFERENCES thesaurus(id))''')
            await conn.commit()

# Обработчики команд
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        'Olá! 👋 Я бот для изучения португальского языка. Используй */help* для списка команд.',
        parse_mode='Markdown')

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        '📚 *Список команд:*\n'
        '*/start* - начать работу\n'
        '*/add* - добавить слово\n'
        '*/bulk_add* - добавить много слов (текст или файл)\n'
        '*/edit <id>* - редактировать слово по ID\n'
        '*/delete <id>* - удалить слово по ID\n'
        '*/test* - пройти тест\n'
        '*/thesaurus* - показать базу слов\n'
        '*/stats* - топ-10 ошибок\n'
        '*/memory* - степень запоминания',
        parse_mode='Markdown')

async def thesaurus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT id, portuguese, russian FROM thesaurus")
            words = await c.fetchall()

    if not words:
        await update.message.reply_text(
            'Тезаурус пуст. Добавь слова с */add*! 📝', parse_mode='Markdown')
        return

    response = "📖 *Тезаурус:*\n\n`ID | Португальский | Русский`\n" + "-" * 40 + "\n"
    for id, portuguese, russian in words:
        response += f"`{id}` | `{portuguese}` | `{russian}`\n"
    await update.message.reply_text(response, parse_mode='Markdown')

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('🇵🇹 Введи слово на португальском:')
    return PORTUGUESE

async def get_portuguese(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['portuguese'] = update.message.text
    await update.message.reply_text('🇷🇺 Теперь введи перевод на русский:')
    return RUSSIAN

async def get_russian(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return ConversationHandler.END

    russian = update.message.text
    portuguese = context.user_data['portuguese']

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "INSERT INTO thesaurus (portuguese, russian) VALUES (%s, %s)",
                (portuguese, russian))
            await conn.commit()

    await update.message.reply_text(
        f'✅ Слово *"{portuguese}"* с переводом *"{russian}"* добавлено!',
        parse_mode='Markdown')
    return ConversationHandler.END

async def bulk_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        '📋 Отправь текст или файл (.txt) в формате "португальское слово - перевод" (каждая пара на новой строке), например:\n'
        '`Sol - Солнце\nCasa - Дом`')
    return BULK_ADD

async def process_bulk_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return ConversationHandler.END

    added = 0
    errors = 0

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            try:
                if update.message.document:
                    file = await update.message.document.get_file()
                    file_name = update.message.document.file_name
                    await update.message.reply_text(
                        f'📥 Получен файл: {file_name}. Обрабатываю...')

                    if not file_name.endswith('.txt'):
                        await update.message.reply_text(
                            '❌ Поддерживаются только файлы .txt!')
                        return ConversationHandler.END

                    file_path = f"temp_{update.message.document.file_id}.txt"
                    await file.download_to_drive(custom_path=file_path)

                    lines = None
                    for encoding in ['utf-8', 'windows-1251']:
                        try:
                            with open(file_path, 'r', encoding=encoding) as f:
                                lines = f.readlines()
                            break
                        except UnicodeDecodeError:
                            continue

                    if lines is None:
                        with open(file_path, 'rb') as f:
                            raw_content = f.read().decode('utf-8', errors='replace')
                        os.remove(file_path)
                        await update.message.reply_text(
                            f'❌ Не удалось декодировать файл. Содержимое:\n```{raw_content}```',
                            parse_mode='Markdown')
                        return ConversationHandler.END

                    for line in lines:
                        line = line.strip()
                        if not line or '-' not in line:
                            errors += 1
                            continue
                        try:
                            portuguese, russian = [part.strip() for part in line.split('-', 1)]
                            if portuguese and russian:
                                await c.execute(
                                    "INSERT INTO thesaurus (portuguese, russian) VALUES (%s, %s)",
                                    (portuguese, russian))
                                added += 1
                            else:
                                errors += 1
                        except Exception:
                            errors += 1

                    os.remove(file_path)
                    response = f'✅ Из файла добавлено *{added}* слов!'
                    if errors > 0:
                        response += f'\n⚠️ Пропущено строк с ошибками: {errors}'
                    await update.message.reply_text(response, parse_mode='Markdown')

                else:
                    text = update.message.text
                    for line in text.split('\n'):
                        line = line.strip()
                        if not line or '-' not in line:
                            errors += 1
                            continue
                        try:
                            portuguese, russian = [part.strip() for part in line.split('-', 1)]
                            if portuguese and russian:
                                await c.execute(
                                    "INSERT INTO thesaurus (portuguese, russian) VALUES (%s, %s)",
                                    (portuguese, russian))
                                added += 1
                            else:
                                errors += 1
                        except Exception:
                            errors += 1

                    response = f'✅ Из текста добавлено *{added}* слов!'
                    if errors > 0:
                        response += f'\n⚠️ Пропущено строк с ошибками: {errors}'
                    await update.message.reply_text(response, parse_mode='Markdown')

                await conn.commit()

            except Exception as e:
                await update.message.reply_text(f'❌ Ошибка при обработке: {str(e)}')

    return ConversationHandler.END

async def edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return ConversationHandler.END

    if not context.args:
        await update.message.reply_text(
            '❌ Укажи ID слова! Например: `/edit 1`', parse_mode='Markdown')
        return ConversationHandler.END

    word_id = context.args[0]
    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT portuguese, russian FROM thesaurus WHERE id = %s", (word_id,))
            word = await c.fetchone()

    if not word:
        await update.message.reply_text(
            f'❌ Слово с ID {word_id} не найдено! Проверь */thesaurus*',
            parse_mode='Markdown')
        return ConversationHandler.END

    context.user_data['edit_id'] = word_id
    context.user_data['old_portuguese'], context.user_data['old_russian'] = word
    await update.message.reply_text(
        f'✏️ Редактируем: *{word[0]} - {word[1]}*\nВведи новое слово на португальском (или нажми Enter, чтобы оставить *{word[0]}*):',
        parse_mode='Markdown')
    return EDIT_PORTUGUESE

async def edit_portuguese(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data['new_portuguese'] = text if text else context.user_data['old_portuguese']
    await update.message.reply_text(
        f'🇷🇺 Введи новый перевод на русский (или нажми Enter, чтобы оставить *{context.user_data["old_russian"]}*):',
        parse_mode='Markdown')
    return EDIT_RUSSIAN

async def edit_russian(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    new_russian = text if text else context.user_data['old_russian']
    new_portuguese = context.user_data['new_portuguese']
    word_id = context.user_data['edit_id']

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "UPDATE thesaurus SET portuguese = %s, russian = %s WHERE id = %s",
                (new_portuguese, new_russian, word_id))
            await conn.commit()

    await update.message.reply_text(
        f'✅ Слово с ID {word_id} обновлено: *{new_portuguese} - {new_russian}*',
        parse_mode='Markdown')
    return ConversationHandler.END

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return

    if not context.args:
        await update.message.reply_text(
            '❌ Укажи ID слова! Например: `/delete 1`', parse_mode='Markdown')
        return

    word_id = context.args[0]
    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT portuguese, russian FROM thesaurus WHERE id = %s", (word_id,))
            word = await c.fetchone()

            if not word:
                await update.message.reply_text(
                    f'❌ Слово с ID {word_id} не найдено! Проверь */thesaurus*',
                    parse_mode='Markdown')
                return

            await c.execute("DELETE FROM thesaurus WHERE id = %s", (word_id,))
            await c.execute("DELETE FROM stats WHERE id = %s", (word_id,))
            await c.execute("DELETE FROM history WHERE word_id = %s", (word_id,))
            await conn.commit()

    await update.message.reply_text(
        f'🗑️ Слово *{word[0]} - {word[1]}* (ID {word_id}) удалено!',
        parse_mode='Markdown')

async def test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return ConversationHandler.END

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT t.id, t.portuguese, t.russian, COALESCE(s.incorrect, 0) as errors FROM thesaurus t "
                "LEFT JOIN stats s ON t.id = s.id")
            words = await c.fetchall()

    if not words:
        await update.message.reply_text(
            'Тезаурус пуст. Добавь слова с */add*! 📝', parse_mode='Markdown')
        return ConversationHandler.END

    sorted_words = sorted(words, key=lambda x: x[3], reverse=True)
    test_words = sorted_words[:min(25, len(words))]
    random.shuffle(test_words)
    context.user_data['test_words'] = test_words
    context.user_data['test_index'] = 0
    context.user_data['test_direction'] = [
        random.choice(['pt_to_ru', 'ru_to_pt']) for _ in test_words
    ]

    await ask_question(update, context)
    return TEST_ANSWER

async def ask_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    index = context.user_data['test_index']
    word_id, portuguese, russian, _ = context.user_data['test_words'][index]
    direction = context.user_data['test_direction'][index]
    total = len(context.user_data['test_words'])

    if direction == 'pt_to_ru':
        await update.message.reply_text(
            f'❓ *Вопрос {index + 1} из {total}:* Переведи *{portuguese}*',
            parse_mode='Markdown')
        context.user_data['correct_answer'] = russian
    else:
        await update.message.reply_text(
            f'❓ *Вопрос {index + 1} из {total}:* Переведи *{russian}*',
            parse_mode='Markdown')
        context.user_data['correct_answer'] = portuguese
    context.user_data['current_word_id'] = word_id

async def check_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_answer = update.message.text
    correct_answer = context.user_data['correct_answer']
    word_id = context.user_data['current_word_id']

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "INSERT INTO stats (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
                (word_id,))
            is_correct = user_answer.lower() == correct_answer.lower()

            if is_correct:
                await update.message.reply_text('✅ *Перевод правильный!*',
                                                parse_mode='Markdown')
                await c.execute(
                    "UPDATE stats SET correct = correct + 1 WHERE id = %s",
                    (word_id,))
                await c.execute(
                    "INSERT INTO history (word_id, correct) VALUES (%s, 1)",
                    (word_id,))
            else:
                await update.message.reply_text(
                    f'❌ *Ошибка!* Правильный ответ: *"{correct_answer}"*',
                    parse_mode='Markdown')
                await c.execute(
                    "UPDATE stats SET incorrect = incorrect + 1 WHERE id = %s",
                    (word_id,))
                await c.execute(
                    "INSERT INTO history (word_id, correct) VALUES (%s, 0)",
                    (word_id,))

            await conn.commit()

    context.user_data['test_index'] += 1
    if context.user_data['test_index'] < len(context.user_data['test_words']):
        await ask_question(update, context)
        return TEST_ANSWER
    else:
        await update.message.reply_text(
            '🎉 *Тест завершён!* Хочешь посмотреть статистику? Используй */stats* или */memory*!',
            parse_mode='Markdown')
        return ConversationHandler.END

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT t.portuguese, t.russian, COALESCE(s.incorrect, 0) as errors "
                "FROM thesaurus t LEFT JOIN stats s ON t.id = s.id "
                "ORDER BY errors DESC LIMIT 10")
            top_errors = await c.fetchall()

    if not top_errors or all(errors == 0 for _, _, errors in top_errors):
        await update.message.reply_text(
            '📊 *Статистика ошибок пуста!* Пройди тест с */test*!',
            parse_mode='Markdown')
        return

    response = "📊 *Топ-10 слов с ошибками:*\n\n`Слово | Перевод | Ошибки`\n" + "-" * 40 + "\n"
    for portuguese, russian, errors in top_errors:
        response += f"`{portuguese}` | `{russian}` | {errors}\n"
    await update.message.reply_text(response, parse_mode='Markdown')

async def memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.bot_data.get('db_pool'):
        await update.message.reply_text("❌ База данных не подключена!")
        return

    async with context.bot_data['db_pool'].connection() as conn:
        async with conn.cursor() as c:
            await c.execute("SELECT t.portuguese, t.russian, t.id FROM thesaurus t")
            words = await c.fetchall()

            response = "🧠 *Степень запоминания:*\n\n`Слово | Перевод | %`\n" + "-" * 40 + "\n"
            for portuguese, russian, word_id in words:
                await c.execute(
                    "SELECT correct FROM history WHERE word_id = %s ORDER BY timestamp DESC LIMIT 5",
                    (word_id,))
                last_5 = await c.fetchall()
                if not last_5:
                    percent = "N/A"
                else:
                    correct_count = sum(1 for (correct,) in last_5 if correct)
                    percent = {
                        5: 100,
                        4: 75,
                        3: 50,
                        2: 25,
                        1: 0,
                        0: 0
                    }[correct_count]
                response += f"`{portuguese}` | `{russian}` | {percent}%\n"

    await update.message.reply_text(response, parse_mode='Markdown')

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('✋ Операция отменена.')
    return ConversationHandler.END

async def setup_application(db_pool=None):
    # Создаём приложение
    application = ApplicationBuilder().token(TOKEN).build()

    # Сохраняем пул в bot_data, если он есть
    if db_pool:
        application.bot_data['db_pool'] = db_pool

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))

    if db_pool:
        application.add_handler(CommandHandler("thesaurus", thesaurus))
        application.add_handler(CommandHandler("stats", stats))
        application.add_handler(CommandHandler("memory", memory))
        application.add_handler(CommandHandler("delete", delete))

        add_handler = ConversationHandler(
            entry_points=[CommandHandler('add', add)],
            states={
                PORTUGUESE: [MessageHandler(filters.ALL & ~filters.COMMAND, get_portuguese)],
                RUSSIAN: [MessageHandler(filters.ALL & ~filters.COMMAND, get_russian)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
        application.add_handler(add_handler)

        bulk_handler = ConversationHandler(
            entry_points=[CommandHandler('bulk_add', bulk_add)],
            states={
                BULK_ADD: [MessageHandler(filters.ALL & ~filters.COMMAND | filters.Document.ALL, process_bulk_add)]
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
        application.add_handler(bulk_handler)

        edit_handler = ConversationHandler(
            entry_points=[CommandHandler('edit', edit)],
            states={
                EDIT_PORTUGUESE: [MessageHandler(filters.ALL & ~filters.COMMAND, edit_portuguese)],
                EDIT_RUSSIAN: [MessageHandler(filters.ALL & ~filters.COMMAND, edit_russian)],
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
        application.add_handler(edit_handler)

        test_handler = ConversationHandler(
            entry_points=[CommandHandler('test', test)],
            states={
                TEST_ANSWER: [MessageHandler(filters.ALL & ~filters.COMMAND, check_answer)]
            },
            fallbacks=[CommandHandler('cancel', cancel)]
        )
        application.add_handler(test_handler)

    return application

# Инициализация базы данных при старте
db_pool = None
if DATABASE_URL:
    try:
        db_pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=20)
        asyncio.get_event_loop().run_until_complete(init_db(db_pool))
        print("Database pool created successfully!")
    except Exception as e:
        print(f"Failed to create database pool: {e}")

# Настройка приложения
application = setup_application(db_pool)
asyncio.get_event_loop().run_until_complete(application.initialize())

# Vercel serverless function handler
async def handler(request):
    if request.method == "POST":
        try:
            # Получаем данные от Telegram
            body = await request.json()
            update = Update.de_json(body, bot)

            # Обрабатываем обновление
            await application.process_update(update)
            return {"statusCode": 200, "body": "OK"}
        except Exception as e:
            print(f"Error processing update: {e}")
            return {"statusCode": 500, "body": str(e)}
    return {"statusCode": 405, "body": "Method Not Allowed"}

# Vercel автоматически вызывает эту функцию
from aiohttp import web
app = web.Application()
app.router.add_post('/', handler)

if __name__ == "__main__":
    web.run_app(app)
