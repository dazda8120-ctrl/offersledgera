"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА' -> Бот отправляет оффер.
Плюс: система входа ({ENTRY_PRICE} TON), заявки админу, доступ только у одобренных.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart  # НОВОЕ
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
# Попытка импортировать Flood ошибку из разных версий
try:
    from aiogram.exceptions import TelegramRetryAfter
except ImportError:
    try:
        from aiogram.exceptions import TelegramFloodWait as TelegramRetryAfter
    except ImportError:
        class TelegramRetryAfter(Exception):
            def __init__(self, message): self.message = message
            @property
            def retry_after(self): return 60

from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))

# ===== НОВОЕ: настройки входа =====
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))          # твой Telegram ID
TON_WALLET = os.getenv("TON_WALLET", "UQ_ТВОЙ_АДРЕС_TON")  # куда отправлять деньги
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")               # цена входа в TON
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")  # куда сохранять доступы

router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

SEND_COOLDOWN_SECONDS = 2.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()

# ===== НОВОЕ: хранилище доступа и заявок =====
def load_allowed_users() -> set[int]:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            return {int(x) for x in json.load(f)}
    except Exception:
        return set()

def save_allowed_users() -> None:
    try:
        with open(USERS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(allowed_users), f)
    except Exception:
        logging.exception("Не удалось сохранить список пользователей")

allowed_users: set[int] = load_allowed_users()
entry_pending: set[int] = set()  # те, кто нажал "Я оплатил" и должен прислать скрин

def user_info_line(user) -> str:
    uname = f"@{user.username}" if getattr(user, "username", None) else "нет"
    return (
        f"ID: <code>{user.id}</code>\n"
        f"Имя: {html.escape(user.full_name)}\n"
        f"Username: {uname}"
    )

def admin_decision_kb(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Добавить", callback_data=f"admin:allow:{user_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin:deny:{user_id}"),
    ]])


def is_peer_flood(e: Exception) -> bool:
    return "PEER_FLOOD" in str(e).upper())


# ... (safe_send / safe_edit / parse_price / offer_text / sold_text / keyboards — без изменений)

# ===== НОВОЕ: /start =====
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if user.id == ADMIN_ID or user.id in allowed_users:
        await message.answer(
            f"Привет! 👋\n\nУ вас уже есть доступ к офферам ✅\n"
            f"Пишите в чат с бизнесом команду: <code>купить ЦЕНА</code>"
        )
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await message.answer(
        f"Привет! Это бот по офферам 🎁\n\n"
        f"Вход стоит <b>{ENTRY_PRICE} TON</b>.\n\n"
        f"После оплаты вы сможете отправлять офферы командой <code>купить ЦЕНА</code>.",
        reply_markup=kb,
    )


# ===== НОВОЕ: кнопка "Купить вход" -> адрес кошелька =====
@router.callback_query(F.data == "entry:start")
async def entry_start(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="entry:paid")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="entry:back")],
    ])
    await callback.message.edit_text(
        f"💳 Оплата входа <b>{ENTRY_PRICE} TON</b>\n\n"
        f"Адрес кошелька (сеть TON):\n<code>{html.escape(TON_WALLET)}</code>\n\n"
        f"Переведите <b>{ENTRY_PRICE} TON</b> на этот адрес, затем нажмите «Я оплатил» "
        f"и пришлите скриншот перевода.",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "entry:back")
async def entry_back(callback: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await callback.message.edit_text(
        f"Привет! Это бот по офферам 🎁\n\nВход стоит <b>{ENTRY_PRICE} TON</b>.",
        reply_markup=kb,
    )
    await callback.answer()


# ===== НОВОЕ: "Я оплатил" -> заявка админу + ожидание скрина =====
@router.callback_query(F.data == "entry:paid")
async def entry_paid(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    entry_pending.add(user.id)
    await callback.message.edit_text(
        "📸 Пришлите <b>скриншот перевода</b> одним сообщением (фото)."
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 <b>Заявка на вход ({ENTRY_PRICE} TON)</b>\n\n{user_info_line(user)}\n\n"
            f"Нажал «Я оплатил», ожидает скриншот.",
            reply_markup=admin_decision_kb(user.id),
        )
    except Exception:
        logging.exception("Не удалось отправить заявку админу")
    await callback.answer("Заявка отправлена ✅")


# ===== НОВОЕ: приём скриншота -> пересылка админу =====
@router.message((F.photo | F.document), F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or user.id != ADMIN_ID and user.id not in entry_pending:
        return
    entry_pending.discard(user.id)
    try:
        await message.copy_to(
            ADMIN_ID,
            caption=f"📸 <b>Скриншот оплаты (вход {ENTRY_PRICE} TON)</b>\n\n{user_info_line(user)}",
            reply_markup=admin_decision_kb(user.id),
        )
        await message.answer("Скриншот отправлен на проверку ✅ Ожидайте.")
    except Exception:
        logging.exception("Не удалось переслать скриншот админу")


# ===== НОВОЕ: /ledgerteam — мгновенная заявка админу =====
@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user:
        return
    if user.id == ADMIN_ID or user.id in allowed_users:
        await message.answer("У вас уже есть доступ ✅")
        return
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🚀 <b>Заявка /ledgerteam</b>\n\n{user_info_line(user)}",
            reply_markup=admin_decision_kb(user.id),
        )
        await message.answer("Заявка отправлена ✅ Ожидайте решения админа.")
    except Exception:
        logging.exception("Не удалось отправить заявку админу")


# ===== НОВОЕ: админ принимает/отклоняет =====
@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только для админа.", show_alert=True)
        return
    try:
        _, action, raw_id = callback.data.split(":")
        user_id = int(raw_id)
    except Exception:
        await callback.answer("Некорректные данные.")
        return

    if action == "allow":
        allowed_users.add(user_id)
        save_allowed_users()
        entry_pending.discard(user_id)
        try:
            await bot.send_message(
                user_id,
                "✅ <b>Вам выдан доступ к офферам!</b>\n\n"
                "Теперь пишите в чат с бизнесом команду: <code>купить ЦЕНА</code>",
            )
        except Exception:
            logging.exception(f"Не удалось уведомить {user_id}")
        if callback.message:
            await callback.message.edit_text(
                callback.message.html_text + "\n\n— ✅ <b>Добавлен админом</b>"
            )
        await callback.answer("Пользователь добавлен.")
    else:
        entry_pending.discard(user_id)
        try:
            await bot.send_message(
                user_id,
                "❌ Ваша заявка отклонена. Свяжитесь с админом."
            )
        except Exception:
            pass
        if callback.message:
            await callback.message.edit_text(
                callback.message.html_text + "\n\n— ❌ <b>Отклонено админом</b>"
            )
        await callback.answer("Заявка отклонена.")


# ===== НОВОЕ: админ-команды /grant /revoke /users =====
@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/grant ID</code>")
        return
    allowed_users.add(user_id)
    save_allowed_users()
    await message.answer(f"✅ Пользователь <code>{user_id}</code> получил доступ.")


@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    try:
        user_id = int(message.text.split()[1])
    except Exception:
        await message.answer("Формат: <code>/revoke ID</code>")
        return
    allowed_users.discard(user_id)
    save_allowed_users()
    await message.answer(f"🚫 Доступ у <code>{user_id}</code> удалён.")


@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID:
        return
    if not allowed_users:
        await message.answer("Список пуст.")
        return
    text = "👥 <b>Список с доступом:</b>\n" + "\n".join(f"• <code>{u}</code>" for u in sorted(allowed_users))
    await message.answer(text)


#IV. Гейт в create_offer — ВСТАВЬ ВНУТРЬ СУЩЕСТВУЮЩЕГО ХЕНДЛЕРА
@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    payload = match.group(1).strip()

    price = parse_price(payload)
    if price is None:
        await message.answer("Укажите цену! Пример: купить 100", quote=False)
        return

    currency = "TON" if "gram" in payload.lower() else "Stars"

    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id

        # ===== НОВОЕ: проверка доступа =====
        if buyer_id != ADMIN_ID and buyer_id not in allowed_users:
            await message.answer(
                "⛔ У вас нет доступа к офферам.\n"
                "Получите доступ: зайдите в личку с ботом и нажмите /start.",
                quote=False,
            )
            return

        # ... дальше всё как было (buyer_name, offer_id, temp_offer и т.д.)
