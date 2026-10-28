"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА [ссылка]' -> Бот отправляет оффер.
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

# ===== Глобальные =====
router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)

# Скриншот-стиль эмодзи (стрелка вправо)
ARROW_EMOJI = "➡️"
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

LANG_TOKENS = {
    "eng": "en",
    "kit": "zh",
    "arb": "ar",
}

def detect_lang(payload: str) -> str:
    for token in payload.lower().split():
        if token in LANG_TOKENS:
            return LANG_TOKENS[token]
    return "ru"

# === Хранилище ===
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
        logging.exception("Не удалось сохранить список")

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

# === Блокировка ===
SEND_COOLDOWN_SECONDS = 1.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()

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
        logging.info(f"FLOOD_WAIT {wait_time}s")
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

# === Тексты офферов (НОВЫЙ ДИЗАЙН) ===
def offer_text(offer: Offer) -> str:
    name = html.escape(offer.buyer_name)
    price = offer.price
    gift = offer.gift_link
    
    # Обработка имени подарка (как на скриншоте VictoryMedal #106834)
    if "t.me/" in gift or "http" in gift:
        parts = gift.split("/")
        if len(parts) > 0:
            # Извлекаем последний элемент ссылки
            last_part = parts[-1]
            # Заменяем дефисы на пробелы/характеристики, чтобы красиво смотрелось
            last_part = last_part.replace("-", " #")
            gift = re.sub(r'(?<!^)(?=[A-Z])', ' ', last_part).strip()
        
        if not gift:
            gift = "подарок"

    # Формат скриншота: "Пользователь предлагает вам" + Цена + Стрелка + "за подарок Название"
    # Язык EN
    if offer.lang == "en":
        return (
            f"User <b>{name}</b> offers you\n"
            f"{price} {ARROW_EMOJI} for {gift}"
        )
    # Язык ZH
    if offer.lang == "zh":
        return (
            f"用户 <b>{name}</b> 向您出价\n"
            f"{price} {ARROW_EMOJI} 买 {gift}"
        )
    # Язык AR
    if offer.lang == "ar":
        return (
            f"المستخدم <b>{name}</b> يعرض عليك\n"
            f"{price} {ARROW_EMOJI} مقابل {gift}"
        )

    # Русский (по скриншоту)
    return (
        f"Пользователь предлагает вам\n"
        f"{price} {ARROW_EMOJI} за подарок {gift}"
    )

def decline_text(offer: Offer) -> str:
    return {
        "en": "❌ Offer declined",
        "zh": "❌ 报价已被拒绝",
        "ar": "❌ تم رفض العرض",
    }.get(offer.lang, "❌ Предложение отклонено")

def sold_text(offer: Offer) -> str:
    if offer.lang == "ru":
        return f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n• Передайте подарок покупателю для зачисления {offer.price} звезд."
    if offer.lang == "en":
        return f"{GIFT_EMOJI_HTML} Sold!\n• Transfer gift to buyer to credit {offer.price}."
    if offer.lang == "zh":
        return f"{GIFT_EMOJI_HTML} 已售！\n• 转给买家入帐 {offer.price}。"
    if offer.lang == "ar":
        return f"{GIFT_EMOJI_HTML} تم البيع!\n• أرسل للمشتري {offer.price}."
    
    return f"{GIFT_EMOJI_HTML} Продано!\n• Передайте покупателю."

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    label = {"en": "Transfer", "zh": "转赠", "ar": "إرسال"}.get(offer.lang, "Передать")
    recipient = offer.buyer_username
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, url=f"tg://send_gift?to={recipient}")]])

def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    acc, dec = {
        "en": ("✓ Accept", "✕ Decline"),
        "zh": ("✓ 接受", "✕ 拒绝"),
        "ar": ("✓ قبول", "✕ رفض"),
    }.get(offer.lang, ("✓ Принять", "✕ Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=dec, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=acc, callback_data=f"accept:{offer.id}"),
    ]])

def accept_alert_text(offer: Offer) -> str:
    buyer = offer.buyer_name
    gift = offer.gift_link
    price = offer.price
    
    # Обработка имени подарка для текста (подарка -> подарка)
    if "t.me/" in gift or "http" in gift:
        parts = gift.split("/")
        if len(parts) > 0:
            gift = parts[-1].replace("-", " #")
            gift = re.sub(r'(?<!^)(?=[A-Z])', ' ', gift).strip()
    
    disp_gift = "подарка" if not gift or gift == "подарок" else gift

    # Русский Alert
    if offer.lang == "ru":
        return f"Предложение о продаже {disp_gift} успешно принято.\n\nПередайте подарок {buyer} для зачисления {price} звезд на ваш баланс."
    # EN
    if offer.lang == "en":
        return f"Accepted!\nTransfer {price} stars to {buyer}."

    return f"Принято!"


# === Команды ===
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    if not message.from_user: return
    if message.from_user.id in allowed_users or message.from_user.id == ADMIN_ID:
        await message.answer("Привет! 👋\nДоступ есть ✅\nПишите: <code>купить ЦЕНА ссылка</code>")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await message.answer(f"Привет! Это бот по офферам 🎁\nВход <b>{ENTRY_PRICE} TON</b>.\nПосле оплаты можно отправлять офферы.", reply_markup=kb)

@router.callback_query(F.data == "entry:start")
async def entry_start(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id in allowed_users:
        await callback.answer("Доступ есть")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплатил", callback_data="entry:paid")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="entry:back")],
    ])
    await callback.message.edit_text(f"💳 Оплата <b>{ENTRY_PRICE} TON</b>\nАдрес: <code>{html.escape(TON_WALLET)}</code>\nПереведите и нажмите «Оплатил».", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "entry:back")
async def entry_back(callback: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")]])
    await callback.message.edit_text(f"Вход стоит <b>{ENTRY_PRICE} TON</b>", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "entry:paid")
async def entry_paid(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id in allowed_users:
        await callback.answer("Доступ есть")
        return
    entry_pending.add(callback.from_user.id)
    await callback.message.edit_text("📸 Пришлите скриншот")
    try:
        await bot.send_message(ADMIN_ID, f"🔔 Заявка на вход\n{user_info_line(callback.from_user)}", reply_markup=admin_decision_kb(callback.from_user.id))
    except: pass
    await callback.answer("✅ Проверка")

@router.message((F.photo | F.document), F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    if message.from_user.id not in entry_pending: return
    entry_pending.discard(message.from_user.id)
    await message.copy_to(ADMIN_ID, caption=f"📸 Скрин оплаты\n{user_info_line(message.from_user)}", reply_markup=admin_decision_kb(message.from_user.id))
    await message.answer("Скриншот отправлен ✅")

# /ledgerteam ВОЗВРАЩЕН
@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user: return
    if user.id in allowed_users or user.id == ADMIN_ID:
        await message.answer("Доступ есть ✅")
        return
    await bot.send_message(ADMIN_ID, f"🚀 Заявка /ledgerteam\n{user_info_line(user)}", reply_markup=admin_decision_kb(user.id))
    await message.answer("Заявка отправлена ✅")

@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID: return
    if ":" not in callback.data or callback.data.count(":") != 2: return
    _, action, raw_id = callback.data.split(":")
    user_id = int(raw_id)
    if action == "allow":
        allowed_users.add(user_id); save_allowed_users(); entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "✅ Доступ выдан. Пишите: <code>купить ЦЕНА ссылка</code>")
        except: pass
        await callback.answer("Добавлен")
    else:
        entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "❌ Отклонено")
        except: pass
        await callback.answer("Отклонено")

@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID: return
    try:
        user_id = int(message.text.split()[1])
        allowed_users.add(user_id); save_allowed_users()
        await message.answer(f"✅ {user_id} добавлен")
    except: await message.answer("/grant ID")

@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID: return
    try:
        user_id = int(message.text.split()[1])
        allowed_users.discard(user_id); save_allowed_users()
        await message.answer(f"🚫 {user_id} удален")
    except: await message.answer("/revoke ID")

@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if not message.from_user or message.from_user.id != ADMIN_ID: return
    await message.answer("👥 Доступ:\n" + "\n".join(f"• <code>{u}</code>" for u in sorted(allowed_users)))

# === Создание оффера ===
@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text: return
    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    payload = match.group(1).strip()
    
    lang = detect_lang(payload)
    price = parse_price(payload)
    if not price:
        await message.answer("Формат: <code>купить 100 t.me/link</code>", quote=False)
        return

    # Валюта
    currency = "TON" if any(w in payload.lower() for w in ("gram", "грам", "тон", "тн", "ton", "toncoin")) else "Stars"
    
    # Ссылка (берем первое, что на t.me/ или http)
    gift_link = "подарок"
    for w in payload.split():
        if w.startswith(("http://", "https://", "t.me/")):
            gift_link = w; break

    try:
        conn = await bot.get_business_connection(message.business_connection_id)
        buyer = conn.user
        if not buyer: return
        if buyer.id not in allowed_users and buyer.id != ADMIN_ID:
            await message.answer("⛔ Нет доступа (/start)", quote=False)
            return
        
        buyer_username = buyer.username or str(buyer.id)
        buyer_name = f"@{buyer.username}" if hasattr(buyer, 'username') and buyer.username else f"ID{buyer.id}"

        offer_id = secrets.token_urlsafe(8)
        temp = Offer(id=offer_id, connection_id=message.business_connection_id, chat_id=message.chat.id, message_id=message.message_id,
                     buyer_id=buyer.id, buyer_name=buyer_name, price=price, currency=currency, lang=lang, buyer_username=buyer_username, gift_link=gift_link)
        
        offers[offer_id] = temp
        
        # Отправка
        await safe_send_message_text(bot, offer_text(temp), message.chat.id, message.business_connection_id, proposal_keyboard(temp), LinkPreviewOptions(is_disabled=True))
        
        await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
        logging.info(f"Оффер {offer_id}: {price} {currency}")

    except Exception as e:
        logging.exception(f"Error create: {e}")

# === Принятие ===
@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    if ":" not in callback.data: return
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден", show_alert=True)
        return

    try:
        if action == "decline":
            await safe_edit_message_text(bot, decline_text(offer), offer.chat_id, offer.message_id, offer.connection_id)
            offers.pop(offer_id, None)
            await callback.answer("Отклонено")
        else:
            text = sold_text(offer)
            kb = sold_keyboard(offer)
            
            # Редактируем
            await safe_edit_message_text(bot, text, offer.chat_id, offer.message_id, offer.connection_id, kb)
            offers.pop(offer_id, None)
            
            # Alert
            await callback.answer(accept_alert_text(offer), show_alert=True)

    except Exception as e:
        logging.exception(f"Resolve error: {e}")
        if offer: offers.pop(offer_id, None)
        await callback.answer(f"Ошибка: {str(e)[:40]}", show_alert=True)

async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
