import asyncio
import os
import re
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from sqlalchemy import select

from .db import SessionLocal, Transaction, User, get_user, init_db, next_bgram_id
from .texts import (
    BALANCE_RU,
    HELP_RU,
    INSUFFICIENT_RU,
    INVALID_TRANSFER_RU,
    RATE_RU,
    START_RU,
    TRANSFER_SUCCESS_RU,
    USER_NOT_FOUND_RU,
)

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN не указан в .env")


bot = Bot(TOKEN)
dp = Dispatcher()


async def register_user(message: Message) -> User:
    async with SessionLocal() as session:
        user = await get_user(session, message.from_user.id)

        if user:
            user.username = message.from_user.username
            await session.commit()
            return user

        bgram_id = await next_bgram_id(session)

        user = User(
            id=message.from_user.id,
            bgram_id=bgram_id,
            username=message.from_user.username,
            balance=Decimal("0"),
        )

        session.add(user)
        await session.commit()

        return user


@dp.message(CommandStart())
async def start_handler(message: Message):
    user = await register_user(message)

    await message.answer(
        START_RU.format(
            bgram_id=user.bgram_id
        )
    )


@dp.message(lambda message: message.text and message.text.lower() in {
    ".б",
    ".баланс",
    ".b",
    ".balance",
})
async def balance_handler(message: Message):
    user = await register_user(message)

    await message.answer(
        BALANCE_RU.format(
            balance=user.balance
        )
    )


@dp.message(lambda message: message.text and message.text.lower() in {
    ".помощь",
    ".help",
})
async def help_handler(message: Message):
    await register_user(message)
    await message.answer(HELP_RU)


@dp.message(lambda message: message.text and message.text.lower() in {
    ".курс",
    ".r",
})
async def rate_handler(message: Message):
    await register_user(message)
    await message.answer(RATE_RU)


@dp.message(lambda message: message.text and message.text.lower() in {
    ".айди",
    ".id",
})
async def id_handler(message: Message):
    user = await register_user(message)

    if message.reply_to_message:
        replied = message.reply_to_message.from_user

        if replied:
            async with SessionLocal() as session:
                target = await get_user(session, replied.id)

                if not target:
                    await message.answer("Этот пользователь ещё не зарегистрирован.")
                    return

                await message.answer(
                    f"BGRAM ID: #{target.bgram_id}\n"
                    f"Telegram ID: {target.id}"
                )
                return

    await message.answer(
        f"BGRAM ID: #{user.bgram_id}\n"
        f"Telegram ID: {user.id}"
    )


@dp.message(lambda message: message.text and message.text.lower() in {
    ".профиль",
    ".profile",
})
async def profile_handler(message: Message):
    user = await register_user(message)

    username = (
        f"@{user.username}"
        if user.username
        else "без username"
    )

    await message.answer(
        f"BGRAM PROFILE\n\n"
        f"BGRAM ID: #{user.bgram_id}\n"
        f"Username: {username}\n"
        f"Balance: {user.balance} BGRAM"
    )


def parse_transfer(text: str):
    parts = text.strip().split()

    if not parts:
        return None

    command = parts[0].lower()

    if command not in {
        ".п",
        ".перевести",
        ".p",
        ".pay",
    }:
        return None

    if len(parts) < 2:
        return None

    target = None
    amount = None
    comment = ""

    if len(parts) >= 2:
        first = parts[1]

        if first.startswith("@"):
            target = ("username", first)

            if len(parts) < 3:
                return None

            amount_text = parts[2]
            comment = " ".join(parts[3:])

        elif first.isdigit() and len(parts) >= 3:
            target = ("id", int(first))

            amount_text = parts[2]
            comment = " ".join(parts[3:])

        else:
            amount_text = first
            comment = " ".join(parts[2:])

    try:
        amount = Decimal(amount_text)

        if amount <= 0:
            return None

    except (InvalidOperation, ValueError):
        return None

    return target, amount, comment


async def find_recipient(session, target):
    if target is None:
        return None

    target_type, value = target

    if target_type == "id":
        return await get_user(session, value)

    if target_type == "username":
        username = value.lstrip("@").lower()

        result = await session.execute(
            select(User).where(
                User.username.ilike(username)
            )
        )

        return result.scalar_one_or_none()

    return None


@dp.message(
    lambda message: message.text
    and message.text.lower().startswith(
        (".п ", ".перевести ", ".p ", ".pay ")
    )
)
async def transfer_handler(message: Message):
    sender = await register_user(message)

    parsed = parse_transfer(message.text)

    if not parsed:
        await message.answer(INVALID_TRANSFER_RU)
        return

    target, amount, comment = parsed

    async with SessionLocal() as session:
        sender_db = await get_user(session, sender.id)

        recipient = None

        if target is None:
            if not message.reply_to_message:
                await message.answer(INVALID_TRANSFER_RU)
                return

            replied_user = message.reply_to_message.from_user

            if not replied_user:
                await message.answer(USER_NOT_FOUND_RU)
                return

            recipient = await get_user(
                session,
                replied_user.id
            )

        else:
            recipient = await find_recipient(
                session,
                target
            )

        if not recipient:
            await message.answer(USER_NOT_FOUND_RU)
            return

        if recipient.id == sender_db.id:
            await message.answer("Нельзя переводить BGRAM самому себе.")
            return

        if Decimal(sender_db.balance) < amount:
            await message.answer(
                INSUFFICIENT_RU.format(
                    balance=sender_db.balance
                )
            )
            return

        sender_db.balance = (
            Decimal(sender_db.balance) - amount
        )

        recipient.balance = (
            Decimal(recipient.balance) + amount
        )

        transaction = Transaction(
            sender_id=sender_db.id,
            recipient_id=recipient.id,
            amount=amount,
            comment=comment or None,
        )

        session.add(transaction)

        await session.commit()

        recipient_name = (
            f"@{recipient.username}"
            if recipient.username
            else f"#{recipient.bgram_id}"
        )

    await message.answer(
        TRANSFER_SUCCESS_RU.format(
            recipient=recipient_name,
            amount=amount,
            comment=comment or "—",
        )
    )

    try:
        await bot.send_message(
            recipient.id,
            f"Вам поступил перевод.\n\n"
            f"Сумма: {amount} BGRAM\n"
            f"От: @{sender.username}"
            if sender.username
            else f"Вам поступил перевод.\n\n"
                 f"Сумма: {amount} BGRAM\n"
                 f"От: #{sender.bgram_id}"
        )
    except Exception:
        pass


async def main_async():
    await init_db()

    await dp.start_polling(bot)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
