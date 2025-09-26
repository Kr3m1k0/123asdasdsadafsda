import discord
from discord import app_commands
from discord.ext import commands
import sqlite3
import random
import string
import asyncio
from flask import Flask, request, jsonify
from flask_cors import CORS
import threading
import logging
import httpx
import os

# Настройки
GUILD_ID = int(os.getenv('GUILD_ID', '680473306440269852'))
MEMBER_ROLE_ID = int(os.getenv('MEMBER_ROLE_ID', '1418321489576333345'))
VIEWER_ROLE_ID = int(os.getenv('VIEWER_ROLE_ID', '1418321452028919944'))
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', 'ABOBAROFLINT228ZXC')

# URL основного API
MAIN_API_URL = os.getenv('MAIN_API_URL', 'http://localhost:8000')

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class KeyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix='!', intents=intents)
        self.db_path = 'keys_database.db'
        self.init_database()

    def init_database(self):
        """Инициализация базы данных"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute('''
            CREATE TABLE IF NOT EXISTS keys (
                key TEXT PRIMARY KEY,
                user_id INTEGER,
                used BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP
            )
        ''')

        c.execute('''
            CREATE TABLE IF NOT EXISTS verification_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                key TEXT,
                role_given TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        conn.close()

    def generate_keys(self, count=15000):
        """Генерация уникальных ключей"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("SELECT COUNT(*) FROM keys")
        existing_count = c.fetchone()[0]

        if existing_count >= count:
            logger.info(f"В базе уже есть {existing_count} ключей")
            conn.close()
            return

        keys_to_generate = count - existing_count
        generated = 0

        logger.info(f"Генерация {keys_to_generate} новых ключей...")

        while generated < keys_to_generate:
            key = ''.join(random.choices(string.ascii_letters + string.digits, k=16))

            try:
                c.execute("INSERT INTO keys (key) VALUES (?)", (key,))
                generated += 1

                if generated % 1000 == 0:
                    conn.commit()
                    logger.info(f"Сгенерировано {generated}/{keys_to_generate} ключей")

            except sqlite3.IntegrityError:
                continue

        conn.commit()
        conn.close()
        logger.info(f"Генерация завершена. Всего ключей в базе: {count}")


bot = KeyBot()


@bot.event
async def on_ready():
    logger.info(f'Бот {bot.user} запущен!')
    bot.generate_keys()

    try:
        synced = await bot.tree.sync()
        logger.info(f'Глобально синхронизировано {len(synced)} команд')
    except Exception as e:
        logger.error(f'Ошибка синхронизации: {e}')


@bot.tree.command(name='key', description='Получить уникальный ключ для верификации')
async def get_key(interaction: discord.Interaction):
    """Команда для получения ключа"""
    user_id = interaction.user.id

    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()

    c.execute("SELECT key FROM keys WHERE user_id = ?", (user_id,))
    existing_key = c.fetchone()

    if existing_key:
        await interaction.response.send_message(
            f"🔑 Ваш ключ: `{existing_key[0]}`\n"
            f"⚠️ Вы уже получали ключ ранее. Используйте его на сайте для верификации.",
            ephemeral=True
        )
        conn.close()
        return

    c.execute("SELECT key FROM keys WHERE user_id IS NULL LIMIT 1")
    free_key = c.fetchone()

    if not free_key:
        await interaction.response.send_message(
            "❌ Извините, все ключи уже распределены. Обратитесь к администратору.",
            ephemeral=True
        )
        conn.close()
        return

    key = free_key[0]

    c.execute("UPDATE keys SET user_id = ? WHERE key = ?", (user_id, key))
    conn.commit()
    conn.close()

    await interaction.response.send_message(
        f"🔑 **Ваш уникальный ключ:** `{key}`\n\n"
        f"📝 **Инструкция:**\n"
        f"1. Скопируйте этот ключ\n"
        f"2. Перейдите на сайт {MAIN_API_URL} для верификации\n"
        f"3. Зарегистрируйтесь или войдите в свой аккаунт\n"
        f"4. Привяжите Discord аккаунт, используя ключ\n"
        f"5. После подтверждения вы автоматически получите роль на сервере\n\n"
        f"⚠️ **Важно:** Этот ключ уникален и может быть использован только один раз!",
        ephemeral=True
    )

    logger.info(f"Пользователь {interaction.user.name} (ID: {user_id}) получил ключ: {key}")


@bot.tree.command(name='verify', description='Проверить статус верификации')
async def check_verification(interaction: discord.Interaction):
    """Проверить статус верификации пользователя"""
    user_id = interaction.user.id

    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()

    c.execute("SELECT key, used FROM keys WHERE user_id = ?", (user_id,))
    result = c.fetchone()

    if not result:
        await interaction.response.send_message(
            "❌ У вас нет выданного ключа. Используйте команду `/key` для получения.",
            ephemeral=True
        )
    else:
        key, used = result
        if used:
            await interaction.response.send_message(
                "✅ Ваш аккаунт верифицирован!",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⏳ Ваш ключ `{key}` еще не использован.\n"
                f"Перейдите на сайт {MAIN_API_URL} для завершения верификации.",
                ephemeral=True
            )

    conn.close()


@bot.tree.command(name='stats', description='[Админ] Статистика использования ключей')
@app_commands.checks.has_permissions(administrator=True)
async def stats(interaction: discord.Interaction):
    """Команда для просмотра статистики (только для админов)"""
    conn = sqlite3.connect(bot.db_path)
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM keys")
    total_keys = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM keys WHERE user_id IS NOT NULL")
    issued_keys = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM keys WHERE used = 1")
    used_keys = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM keys WHERE user_id IS NULL")
    available_keys = c.fetchone()[0]

    conn.close()

    embed = discord.Embed(
        title="📊 Статистика ключей",
        color=discord.Color.blue()
    )
    embed.add_field(name="Всего ключей", value=f"{total_keys:,}", inline=True)
    embed.add_field(name="Выдано", value=f"{issued_keys:,}", inline=True)
    embed.add_field(name="Использовано", value=f"{used_keys:,}", inline=True)
    embed.add_field(name="Доступно", value=f"{available_keys:,}", inline=True)

    await interaction.response.send_message(embed=embed, ephemeral=True)


# Flask веб-сервер для приема webhook запросов
app = Flask(__name__)
CORS(app)


@app.route('/webhook/verify', methods=['POST', 'OPTIONS'])
def verify_webhook():
    """Endpoint для приема уведомлений с сайта"""

    if request.method == 'OPTIONS':
        response = jsonify({'status': 'ok'})
        response.headers.add('Access-Control-Allow-Origin', '*')
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type')
        response.headers.add('Access-Control-Allow-Methods', 'POST, OPTIONS')
        return response

    try:
        data = request.json

        if data.get('secret') != WEBHOOK_SECRET:
            return jsonify({'error': 'Unauthorized'}), 401

        discord_id = data.get('discord_id')
        key = data.get('key')
        role_type = data.get('role_type', 'member')

        if not discord_id or not key:
            return jsonify({'error': 'Missing required fields'}), 400

        conn = sqlite3.connect(bot.db_path)
        c = conn.cursor()

        c.execute("SELECT user_id, used FROM keys WHERE key = ?", (key,))
        key_data = c.fetchone()

        if not key_data:
            conn.close()
            return jsonify({'error': 'Invalid key'}), 404

        db_user_id, used = key_data

        # Проверяем, что ключ принадлежит этому пользователю
        if db_user_id != int(discord_id):
            conn.close()
            return jsonify({'error': 'Key does not belong to this user'}), 403

        if used:
            conn.close()
            return jsonify({'error': 'Key already used'}), 400

        # Отмечаем ключ как использованный
        c.execute("UPDATE keys SET used = 1, used_at = CURRENT_TIMESTAMP WHERE key = ?", (key,))

        # Определяем какую роль выдавать
        role_id = MEMBER_ROLE_ID if role_type == 'member' else VIEWER_ROLE_ID
        role_name = 'Участник' if role_type == 'member' else 'Зритель'

        # Логируем верификацию
        c.execute(
            "INSERT INTO verification_logs (user_id, key, role_given) VALUES (?, ?, ?)",
            (discord_id, key, role_name)
        )

        conn.commit()
        conn.close()

        # Выдаем роль пользователю (асинхронно)
        asyncio.run_coroutine_threadsafe(
            assign_role(int(discord_id), role_id, role_name),
            bot.loop
        )

        # Уведомляем основной API о верификации
        asyncio.run_coroutine_threadsafe(
            notify_main_api(discord_id),
            bot.loop
        )

        return jsonify({'success': True, 'message': f'Role {role_name} assigned'}), 200

    except Exception as e:
        logger.error(f"Ошибка в webhook: {e}")
        return jsonify({'error': 'Internal server error'}), 500


async def assign_role(user_id, role_id, role_name):
    """Асинхронная функция для выдачи роли"""
    try:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            logger.error(f"Сервер с ID {GUILD_ID} не найден")
            return

        member = guild.get_member(user_id)
        if not member:
            logger.error(f"Пользователь с ID {user_id} не найден на сервере")
            return

        role = guild.get_role(role_id)
        if not role:
            logger.error(f"Роль с ID {role_id} не найдена")
            return

        await member.add_roles(role)
        logger.info(f"Пользователю {member.name} (ID: {user_id}) выдана роль {role_name}")

        try:
            await member.send(
                f"✅ **Верификация успешна!**\n"
                f"Вам была выдана роль **{role_name}** на сервере {guild.name}.\n"
                f"Теперь вы можете участвовать в ставках на сайте {MAIN_API_URL}"
            )
        except discord.Forbidden:
            pass

    except Exception as e:
        logger.error(f"Ошибка при выдаче роли: {e}")


async def notify_main_api(discord_id):
    """Уведомить основной API о верификации"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{MAIN_API_URL}/webhook/discord-verified",
                json={
                    "discord_id": str(discord_id),
                    "key": "",  # Ключ уже проверен
                    "role_type": "member",
                    "secret": WEBHOOK_SECRET
                }
            )
            if response.status_code == 200:
                logger.info(f"Основной API уведомлен о верификации пользователя {discord_id}")
    except Exception as e:
        logger.error(f"Не удалось уведомить основной API: {e}")


def run_flask():
    """Запуск Flask сервера в отдельном потоке"""
    app.run(host='0.0.0.0', port=5001, debug=False)


if __name__ == '__main__':
    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Запускаем Discord бота
    bot.run(BOT_TOKEN)