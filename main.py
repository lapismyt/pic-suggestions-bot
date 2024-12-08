import json
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
import aiosqlite
from dotenv import load_dotenv
import os
import random

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

class UserState(StatesGroup):
    registering = State()
    suggesting = State()

async def setup_database():
    async with aiosqlite.connect("db.sqlite3") as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            blocked INTEGER DEFAULT 0
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            image_id TEXT,
            caption TEXT,
            status TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
        """)
        await db.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (os.getenv("ADMIN_ID"),))
        await db.commit()

async def is_admin(user_id: int):
    async with aiosqlite.connect("db.sqlite3") as db:
        async with db.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,)) as cursor:
            return await cursor.fetchone() is not None

@dp.message(Command("start"))
async def start_command(message: types.Message, state: FSMContext):
    async with aiosqlite.connect("db.sqlite3") as db:
        async with db.execute("SELECT username FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            user = await cursor.fetchone()

    if user:
        await message.answer(f"Вы уже зарегистрированы как {user[0]}. Используйте /suggest чтобы предложить арт.")
    else:
        await message.answer("Привет! Пожалуйста, укажите свой ник:")
        await state.set_state(UserState.registering)


@dp.message(UserState.registering)
async def register_user(message: types.Message, state: FSMContext):
    username = message.text
    async with aiosqlite.connect("db.sqlite3") as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", 
                         (message.from_user.id, username))
        await db.commit()
    await state.clear()
    await message.answer(f"Регистрация завершена! Используйте /suggest чтобы предложить арт.")

@dp.message(Command("suggest"))
async def suggest_command(message: types.Message, state: FSMContext):
    async with aiosqlite.connect("db.sqlite3") as db:
        async with db.execute("SELECT blocked FROM users WHERE user_id = ?", (message.from_user.id,)) as cursor:
            result = await cursor.fetchone()
            if result and result[0] == 1:
                await message.answer("Вы заблокированы и не можете предлагать посты.")
                return
    await message.answer("Пожалуйста, отправьте арт, добавьте персонажей через хэштеги и укажите автора.")
    await state.set_state(UserState.suggesting)

@dp.message(UserState.suggesting, F.photo)
async def handle_suggestion(message: types.Message, state: FSMContext):
    if not message.caption or "#" not in message.caption:
        await message.answer("Пожалуйста, добавьте хотя бы одного персонажа через хэштеги в описании.")
        return

    caption = message.caption
    image_id = message.photo[-1].file_id

    async with aiosqlite.connect("db.sqlite3") as db:
        await db.execute("INSERT INTO suggestions (user_id, image_id, caption, status) VALUES (?, ?, ?, 'pending')", 
                         (message.from_user.id, image_id, caption))
        async with db.execute("SELECT last_insert_rowid()") as cursor:
            suggestion_id = (await cursor.fetchone())[0]
        await db.commit()

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Принять", callback_data=f"accept_{suggestion_id}"),
        InlineKeyboardButton(text="Отклонить", callback_data=f"reject_{suggestion_id}"),
        InlineKeyboardButton(text="Блокировать", callback_data=f"block_{message.from_user.id}")
    )
    await bot.send_photo(
        chat_id=os.getenv("ADMIN_ID"),
        photo=image_id,
        caption=f"Предложение от [{message.from_user.first_name}](tg://user?id={message.from_user.id}):\n\n{caption}",
        reply_markup=builder.as_markup(),
        parse_mode="markdown"
    )
    await message.answer(f"Ваше предложение #{suggestion_id} отправлено на рассмотрение.")
    await state.clear()

@dp.callback_query(F.data.startswith(("accept_", "reject_", "block_")))
async def handle_admin_actions(callback: types.CallbackQuery):
    if not await is_admin(callback.from_user.id):
        await callback.answer("У вас нет прав администратора.", show_alert=True)
        return
    
    action, target_id = callback.data.split("_", 1)

    async with aiosqlite.connect("db.sqlite3") as db:
        if action == "accept":
            async with db.execute("SELECT user_id, image_id, caption FROM suggestions WHERE id = ?", (target_id,)) as cursor:
                suggestion = await cursor.fetchone()
                if not suggestion:
                    await callback.answer("Предложение не найдено.", show_alert=True)
                    return
            async with db.execute("SELECT username FROM users WHERE user_id =?", (suggestion[0],)) as cursor:
                username = (await cursor.fetchone())[0]
            user_id, image_id, caption = suggestion
            caption = f"👤 *{username}*\n\n{caption}"
            with open("ads.json", "r") as f:
                ad_data = json.load(f)
            me = await bot.get_me()
            offer = f"[Предложить арт](https://t.me/{me.username})"
            caption += f"\n\n{offer}"
            ad_text = f"[{random.choice(ad_data['phrases'])}]({ad_data['url']})"
            caption += f"\n\n{ad_text}"
            await bot.send_photo(chat_id=os.getenv("CHANNEL_ID"), photo=image_id, caption=caption, parse_mode="Markdown")
            await db.execute("UPDATE suggestions SET status = 'accepted' WHERE id = ?", (target_id,))
            await bot.send_message(user_id, f"Ваше предложение #{target_id} принято, арт опубликован.")
        elif action == "reject":
            await db.execute("UPDATE suggestions SET status = 'rejected' WHERE id = ?", (target_id,))
            async with db.execute("SELECT user_id FROM suggestions WHERE id = ?", (target_id,)) as cursor:
                user_id = (await cursor.fetchone())[0]
            await bot.send_message(user_id, f"Ваше предложение #{target_id} отклонено.")
        elif action == "block":
            await db.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (target_id,))
            await bot.send_message(int(target_id), "Вы были заблокированы и больше не можете предлагать арты.")
        await db.commit()
    await callback.answer("Действие выполнено.")
    await callback.message.delete()

@dp.message(Command("broadcast"))
async def broadcast_command(message: types.Message):
    if not await is_admin(message.from_user.id):
        await message.answer("У вас нет прав администратора.")
        return
    await message.answer("Отправьте сообщение для рассылки.")

@dp.message(F.text.startswith('#mail'), StateFilter(None))
async def handle_broadcast(message: types.Message):
    async with aiosqlite.connect("db.sqlite3") as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
    for user in users:
        try:
            await bot.send_message(user[0], message.html_text, parse_mode="html")
        except Exception as e:
            print(f"Failed to send message to {user[0]}: {e}")
    await message.answer("Рассылка завершена.")

async def main():
    await setup_database()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
