import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from config import BOT_TOKEN, WEBAPP_URL

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


def main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура с кнопкой, открывающей Web App (каталог/корзина/заказы)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛒 Открыть каталог", web_app=WebAppInfo(url=WEBAPP_URL))],
        ],
        resize_keyboard=True,
    )


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Добро пожаловать в <b>Yaqin Food</b>! 🚚\n\n"
        "Доставка продукции для HoReCa: овощи, мясо, рыба, молочка и многое другое.\n\n"
        "Нажмите кнопку ниже, чтобы открыть каталог и оформить заявку.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


@dp.message(F.text == "🛒 Открыть каталог")
async def open_catalog(message: Message):
    await message.answer("Открываю каталог...", reply_markup=main_keyboard())


@dp.message()
async def fallback(message: Message):
    await message.answer(
        "Не понял команду. Нажмите кнопку «🛒 Открыть каталог» ниже.",
        reply_markup=main_keyboard(),
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
