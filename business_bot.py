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
from aiogram.exceptions import TelegramRetryAfter
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "")
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TON_WALLET = os.getenv("TON_WALLET", "UQ_ТВОЙ_АДРЕС_TON")
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")

# Резервный получатель для подарков, если у покупателя нет @username
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

# ===== Технический cooldown =====
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

@dataclass
class Offer:
    id: str
    connection_id: str
    chat_id: int
    message_id: int
    buyer_id: int
    buyer_name: str
    buyer_username: str
    price: str
    currency: str
    lang: str = "ru"
    gift_link: str = "подарок"

offers: dict[str, Offer] = {}

# ===== Безопасная отправка сообщений =====
async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
) -> Message:
    await send_cooldown()
    try:
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, 
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )
    except TelegramRetryAfter as e:
        if is_peer_flood(e): raise
        wait_time = getattr(e, "retry_after", 30)
        await asyncio.sleep(wait_time)
        return await bot.send_message(
            chat_id=chat_id, text=text,
            business_connection_id=business_connection_id,
            reply_markup=reply_markup, 
            link_preview_options=LinkPreviewOptions(is_disabled=True)
        )

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

# ===== Тексты =====
def offer_text(offer: Offer) -> str:
    nbsp = "\u00A0"
    name = html.escape(offer.buyer_name)
    gift = offer.gift_link if offer.gift_link != "подарок" else "подарок"
    
    if offer.lang == "en":
        return f"User <b>{name}</b> offers you\n<b>{offer.price} {offer.currency}</b> for {gift}\n\n<b>The offer is valid for another {TTL_HOURS}h{nbsp}59m.</b>"
    if offer.lang == "zh":
        return f"用户 <b>{name}</b> 向您出价\n<b>{offer.price} {offer.currency}</b> 购买{gift}\n\n<b>报价有效期还剩 {TTL_HOURS}小时59分钟。</b>"
    if offer.lang == "ar":
        return f"المستخدم <b>{name}</b> يعرض عليك\n<b>{offer.price} {offer.currency}</b> مقابل {gift}\n\n<b>العرض صالح لمدة {TTL_HOURS}{nbsp}س{nbsp}و{nbsp}59{nbsp}د أخرى.</b>"
    
    return (
        f"Пользователь <b>{name}</b> предлагает\n"
        f"Вам <b>{offer.price} {offer.currency}</b> за {gift}\n\n"
        f"Предложение действует ещё <b>{TTL_HOURS}{nbsp}ч.{nbsp}59{nbsp}м.</b>"
    )

def decline_text(offer: Offer) -> str:
    return {"en": "❌ Offer declined", "zh": "❌ 报价已被拒绝", "ar": "❌ تم رفض العرض"}.get(offer.lang, "❌ Предложение отклонено")

def sold_text(offer: Offer) -> str:
    if offer.lang == "en": return f"{GIFT_EMOJI_HTML} The gift has been sold!\n\n• Transfer the gift to the buyer to credit {offer.currency}."
    if offer.lang == "zh": return f"{GIFT_EMOJI_HTML} 礼物已成功售出！\n\n• 请将礼物转给买家以入账 {offer.currency}。"
    if offer.lang == "ar": return f"{GIFT_EMOJI_HTML} تم بيع الهدية بنجاح!\n\n• قم بتحويل الهدية للمشتري ليتم إضافة {offer.currency}."
    return f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n• Передайте подарок покупателю для зачисления {offer.currency}."

def accept_alert_text(offer: Offer) -> str:
    buyer = offer.buyer_name
    if len(buyer) > 30: buyer = buyer[:30] + "..."
    
    cur_text_ru = "звезд" if offer.currency == "Stars" else offer.currency
    gift = "подарка"
    
    if offer.gift_link and "t.me/" in offer.gift_link:
        parts = offer.gift_link.split("/")
        if parts:
            last = parts[-1].replace("-", " #")
            gift = re.sub(r'(?<!^)(?=[A-Z])', ' ', last).strip()
    elif offer.gift_link != "подарок":
        gift = offer.gift_link

    if offer.lang == "en": return f"Offer to sell {gift} successfully accepted.\n\nTransfer the gift to {buyer} to receive {offer.price} {offer.currency}."
    if offer.lang == "zh": return f"出售 {gift} 的报价已成功接受。\n\n请将礼物转给 {buyer} 以将 {offer.price} {offer.currency} 存入余额。"
    if offer.lang == "ar": return f"تم قبول عرض بيع {gift} بنجاح.\n\nقم بتحويل الهدية إلى {buyer} لإضافة {offer.price} {offer.currency}."
    
    return (
        f"Предложение о продаже {gift} успешно принято.\n\n"
        f"Передайте подарок {buyer} для зачисления {offer.price} {cur_text_ru} на ваш баланс."
    )

# ===== Клавиатуры =====
def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    accept_label, decline_label = {"en": ("✓ Accept", "✕ Decline"), "zh": ("✓ 接受", "✕ 拒绝"), "ar": ("✓ قبول", "✕ رفض")}.get(offer.lang, ("✓ Принять", "✕ Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=decline_label, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=accept_label, callback_data=f"accept:{offer.id}"),
    ]])

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    transfer_label = {"en": "Transfer", "zh": "转赠", "ar": "إرسال"}.get(offer.lang, "Передать")
    # Главная защита: если username нет, ставим резервный.
    recipient = offer.buyer_username if (offer.buyer_username and not offer.buyer_username.isdigit()) else GIFT_RECIPIENT
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=transfer_label, url=f"tg://send_gift?to={recipient}")],
    ])

# ===== Команды /start и Вход =====
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
        await callback.answer("У вас уже есть доступ ✅"); return
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
        await callback.answer("У вас уже есть доступ ✅"); return
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
        await bot.send_message(ADMIN_ID, f"🚀 <b>Заявка /ledgerteam</b>\n\n{user_info_line(user)}", reply_markup=admin_decision_kb(user.id))
        await message.answer("Заявка отправлена ✅")
    except Exception: pass

# ===== Админка =====
@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID: return
    try: _, action, raw_id = callback.data.split(":"); user_id = int(raw_id)
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
    allowed_users.add(user_id); save_allowed_users()
    await message.answer(f"✅ Доступ выдан.")

@router.message(Command("revoke"), F.chat.type == "private")
async def cmd_revoke(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID: return
    try: user_id = int(message.text.split()[1])
    except Exception: return
    allowed_users.discard(user_id); save_allowed_users()
    await message.answer(f"🚫 Доступ удалён.")

@router.message(Command("users"), F.chat.type == "private")
async def cmd_users(message: Message, bot: Bot) -> None:
    if message.from_user.id != ADMIN_ID: return
    await message.answer("👥 <b>Список:</b>\n" + "\n".join(f"• <code>{u}</code>" for u in sorted(allowed_users)))

# ===== Создание оффера =====
@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id: return
    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    
    payload = match.group(1).strip()
    lang = detect_lang(payload)
    price = parse_price(payload)
    if price is None:
        await message.answer("Укажите цену! Пример: купить 100", quote=False); return

    gift_link = "подарок"
    for word in payload.split():
        if word.startswith(("http://", "https://", "t.me/")):
            gift_link = word; break

    currency = "TON" if any(w in payload.lower() for w in ("gram", "грам", "тон", "тн", "ton", "toncoin")) else "Stars"

    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id

        if buyer_id != ADMIN_ID and buyer_id not in allowed_users:
            await message.answer("⛔ Нет доступа. Нажмите /start в личке бота.", quote=False); return

        buyer_name = f"@{buyer.username}" if getattr(buyer, "username", None) else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        buyer_username = buyer.username if buyer.username else str(buyer.id)

        offer_id = secrets.token_urlsafe(8)
        temp_offer = Offer(
            id=offer_id, connection_id=message.business_connection_id,
            chat_id=message.chat.id, message_id=message.message_id,
            buyer_id=buyer_id, buyer_name=buyer_name, buyer_username=buyer_username,
            price=price, currency=currency, lang=lang, gift_link=gift_link
        )

        msg = await safe_send_message_text(bot, offer_text(temp_offer), message.chat.id, message.business_connection_id, proposal_keyboard(temp_offer))
        temp_offer.message_id = msg.message_id
        offers[offer_id] = temp_offer

        await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
    except Exception as e:
        logging.exception(f"Error: {e}")

# ===== Обработка кнопок "Принять" и "Отклонить" =====
@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True); return

    if action == "decline":
        try:
            await bot.edit_message_text(
                text=decline_text(offer),
                chat_id=offer.chat_id,
                message_id=offer.message_id,
                business_connection_id=offer.connection_id,
                reply_markup=None
            )
        except Exception as e:
            logging.error(f"Ошибка при отклонении: {e}")
        offers.pop(offer_id, None)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        kb = sold_keyboard(offer)
        
        try:
            # ВАЖНО: используем bot.edit_message_text с business_connection_id для обновления у продавца
            await bot.edit_message_text(
                text=text,
                chat_id=offer.chat_id,
                message_id=offer.message_id,
                business_connection_id=offer.connection_id,
                reply_markup=kb
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании (принятие): {e}")
            # Если редактирование не удалось, отправляем новое сообщение
            try:
                await safe_send_message_text(
                    bot, text, offer.chat_id, offer.connection_id, kb
                )
            except Exception as send_e:
                logging.error(f"Ошибка при отправке нового сообщения: {send_e}")
        
        offers.pop(offer_id, None)
        
        # Показываем окно
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
