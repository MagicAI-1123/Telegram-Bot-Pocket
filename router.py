from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message

router = Router()

@router.message(Command("start"))  # Make sure you're using Command from aiogram.filters
async def start_handler(message: Message):
    print("here")  # This isn't showing up
    await message.answer("Bot started!") 