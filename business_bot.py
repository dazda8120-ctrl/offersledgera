"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ЦЕНА [eng|kit|arb]' -> Бот отправляет оффер.
"""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

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

try:
    from aiogram.exceptions import TelegramBadRequest
except ImportError:
    class TelegramBadRequest(Exception):
        pass

from dotenv import load_dotenv

# .env ищем сначала рядом со скриптом, потом от текущей папки
load_dotenv(Path(__file__).with_name(".env"))
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
TTL_HOURS = int(os.getenv("OFFER_TTL_HOURS", "5"))

ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))
TON_WALLET = os.getenv("TON_WALLET", "UQ_ТВОЙ_АДРЕС_TON")
ENTRY_PRICE = os.getenv("ENTRY_PRICE", "5.5")
USERS_FILE = os.getenv("USERS_FILE", "allowed_users.json")

# 1 = после принятия оффер не редактируется, а удаляется и отправляется заново.
# Нужно, если Telegram не доставляет правку business-сообщения второй стороне.
ACCEPT_RECREATE_MESSAGE = os.getenv("ACCEPT_RECREATE_MESSAGE", "0") == "1"

# ===== Глобальные переменные =====
router = Router()
# Команда: купить (без слова ру, язык подбирается автоматически)
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'
GIFT_EMOJI_PLAIN = "🎁"

# Маркеры языка
LANG_TOKENS = {
    "eng": "en",   # английский
    "kit": "zh",   # китайский
    "arb": "ar",   # арабский
}

def detect_lang(payload: str) -> str:
    for token in payload.lower().split():
        if token in LANG_TOKENS:
            return LANG_TOKENS[token]
    return "ru"  # Если нет маркера, по умолчанию русский

SEND_COOLDOWN_SECONDS = 2.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()

# ===== Хранилище доступа =====
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
    status: str = "pending"          # pending | accepted | declined
    created_at: float = field(default_factory=time.time)

offers: dict[str, Offer] = {}

def prune_offers() -> None:
    deadline = time.time() - (TTL_HOURS * 3600 + 3600)
    for offer_id, offer in list(offers.items()):
        if offer.created_at < deadline:
            offers.pop(offer_id, None)

def empty_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])

async def business_notice(
    bot: Bot, message: Message, text: str,
    reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    # В business-чате отвечать можно только через business_connection_id,
    # иначе Telegram вернёт "chat not found".
    await bot.send_message(
        chat_id=message.chat.id,
        text=text,
        business_connection_id=message.business_connection_id,
        reply_markup=reply_markup,
        link_preview_options=LinkPreviewOptions(is_disabled=True),
    )

async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    last_error: Exception | None = None
    MAX_RETRIES = 6
    for attempt in range(MAX_RETRIES):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
        except TelegramRetryAfter as e:
            wait_time = getattr(e, "retry_after", 30)
            logging.info(f"Send Wait: {wait_time}s")
            await asyncio.sleep(wait_time)
        except Exception as e:
            last_error = e
            if is_peer_flood(e):
                wait = 60 * (attempt + 1)
                logging.error(f"Send Flood, wait {wait}s")
                await asyncio.sleep(wait)
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("Не удалось отправить сообщение")

async def safe_edit_message_text(
    bot: Bot,
    *,
    business_connection_id: str | None,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None,
    attempts: int = 2,
) -> bool:
    """True — правка применена, False — Telegram её не принял (нужен фолбэк)."""
    if link_preview_options is None:
        link_preview_options = LinkPreviewOptions(is_disabled=True)

    for attempt in range(attempts):
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup,
                link_preview_options=link_preview_options,
                parse_mode="HTML",
            )
            return True
        except TelegramRetryAfter as e:
            wait_time = min(int(getattr(e, "retry_after", 3)), 5)
            if attempt == attempts - 1:
                return False
            logging.info(f"Edit Wait: {wait_time}s")
            await asyncio.sleep(wait_time)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return True
            logging.warning(f"Edit failed: {e}")
            return False
        except Exception as e:
            logging.warning(f"Edit failed: {e}")
            return False
    return False

async def safe_answer(
    callback: CallbackQuery, text: str | None = None, show_alert: bool = False
) -> None:
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except Exception as e:
        logging.warning(f"Callback answer failed: {e}")

def alert_text(text: str) -> str:
    text = text.strip()
    return text if len(text) <= 200 else text[:197] + "..."

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

def gift_display(offer: Offer) -> str:
    gift = offer.gift_link
    if "t.me/" in gift or "http" in gift:
        parts = gift.split("/")
        if len(parts) > 0:
            last_part = parts[-1].replace("-", " #")
            gift = re.sub(r'(?<!^)(?=[A-Z])', ' ', last_part).strip()
    return gift

# ===== Тексты офферов =====
def offer_text(offer: Offer) -> str:
    name = html.escape(offer.buyer_name)
    price, cur, ttl = offer.price, offer.currency, TTL_HOURS
    gift = gift_display(offer)

    # В самом оффере используем "Звезд" для русского языка
    cur_disp = "Звезд" if offer.lang == "ru" and cur == "Stars" else cur

    if offer.lang == "en":
        return f"User <b>{name}</b> offers you\n<b>{price} {cur_disp}</b> for <b>{gift}</b>\n\n<b>Valid for {ttl}h 59m.</b>"
    if offer.lang == "zh":
        return f"用户 <b>{name}</b> 向您出价\n<b>{price} {cur_disp}</b> 购买 <b>{gift}</b>\n\n<b>有效期 {ttl}小时59分。</b>"
    if offer.lang == "ar":
        return f"المستخدم <b>{name}</b> يعرض عليك\n<b>{price} {cur_disp}</b> مقابل <b>{gift}</b>\n\n<b>العرض صالح {ttl} س و 59 د.</b>"

    # Русский
    return f"Пользователь <b>{name}</b> предлагает\n<b>{price} {cur_disp}</b> за <b>{gift}</b>\n\n<b>Действует {ttl}ч 59м.</b>"

def decline_text(offer: Offer) -> str:
    return {
        "en": "❌ Declined",
        "zh": "❌ 已拒绝",
        "ar": "❌ مرفوض",
    }.get(offer.lang, "❌ Отклонено")

# === ТЕКСТ СООБЩЕНИЯ "ПРОДАНО" (как на скриншоте) ===
def sold_text(offer: Offer, emoji: str = GIFT_EMOJI_HTML) -> str:
    cur = offer.currency

    if offer.lang == "en":
        return f"{emoji} Sold!\n\n• Transfer to buyer for {cur}."
    if offer.lang == "zh":
        return f"{emoji} 已售！\n\n• 转给买家入帐 {cur}。"
    if offer.lang == "ar":
        return f"{emoji} تم البيع!\n\n• إرسال للمشتري {cur}."

    # Русский (точно как на скриншоте)
    return f"{emoji} Подарок успешно продан!\n\n• Передайте подарок покупателю для зачисления {cur}."

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    t_label = {"en": "Transfer", "zh": "Trans", "ar": "Send"}.get(offer.lang, "Передать")
    recipient = (offer.buyer_username or str(offer.buyer_id)).lstrip("@")
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t_label, url=f"tg://send_gift?to={recipient}")
    ]])

def proposal_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    acc, dec = {
        "en": ("Accept", "Decline"),
        "zh": ("接受", "拒绝"),
        "ar": ("قبول", "رفض"),
    }.get(offer.lang, ("Принять", "Отклонить"))
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=dec, callback_data=f"decline:{offer.id}"),
        InlineKeyboardButton(text=acc, callback_data=f"accept:{offer.id}"),
    ]])

# === ИСПРАВЛЕННОЕ ПРЕДУПРЕЖДЕНИЕ (Alert) (как в запросе) ===
def accept_alert_text(offer: Offer) -> str:
    buyer = offer.buyer_name
    cur = offer.currency
    # Перевод валюты для RU (Stars -> звезд)
    cur_ru = "звезд" if cur == "Stars" else cur

    gift = gift_display(offer)

    # Склонение русского
    disp_gift = "подарка" if not gift or gift == "подарок" else gift

    if offer.lang == "ru":
        # Текст точно как просили
        return (
            f"Предложение о продаже {disp_gift}\n"
            f"успешно принято.\n\n"
            f"Передайте подарок\n"
            f"{buyer} для зачисления\n"
            f"{offer.price} {cur_ru} на ваш баланс."
        )

    # Довес для других языков (оставил логичный)
    if offer.lang == "en":
        return f"Offer to sell {disp_gift} successfully accepted.\n\nTransfer gift to {buyer} to receive {offer.price} {cur}."
    return "Accepted."

# ===== Смена состояния сообщения оффера =====
async def apply_offer_state(
    bot: Bot, offer: Offer, text: str, keyboard: InlineKeyboardMarkup
) -> bool:
    """Правит сообщение оффера на месте, а если Telegram правку не принял —
    удаляет оффер и отправляет новое сообщение (новые сообщения доходят до обеих сторон)."""
    if not ACCEPT_RECREATE_MESSAGE:
        if await safe_edit_message_text(
            bot,
            business_connection_id=offer.connection_id,
            chat_id=offer.chat_id,
            message_id=offer.message_id,
            text=text,
            reply_markup=keyboard,
        ):
            return True

    await send_cooldown()
    try:
        await bot.delete_business_messages(
            business_connection_id=offer.connection_id,
            message_ids=[offer.message_id],
        )
    except Exception as e:
        logging.warning(f"Delete offer failed: {e}")

    try:
        msg = await bot.send_message(
            chat_id=offer.chat_id,
            text=text,
            business_connection_id=offer.connection_id,
            reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        offer.message_id = msg.message_id
        return True
    except Exception as e:
        logging.exception(f"Recreate offer failed: {e}")
        return False

async def show_sold_state(bot: Bot, offer: Offer) -> bool:
    kb = sold_keyboard(offer)
    if await apply_offer_state(bot, offer, sold_text(offer), kb):
        return True
    # Кастомный эмодзи (<tg-emoji>) Telegram принимает не у всех ботов —
    # если дело в нём, повторяем с обычным 🎁.
    return await apply_offer_state(bot, offer, sold_text(offer, GIFT_EMOJI_PLAIN), kb)

async def show_declined_state(bot: Bot, offer: Offer) -> bool:
    return await apply_offer_state(bot, offer, decline_text(offer), empty_kb())

# ===== /start =====
@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    if not message.from_user: return
    if message.from_user.id == ADMIN_ID or message.from_user.id in allowed_users:
        await message.answer("👋 Доступ есть ✅\nПишите: <code>купить ЦЕНА</code>")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start"),
    ]])
    await message.answer(f"🎁 Вход стоит <b>{ENTRY_PRICE} TON</b>.\nПишите: <code>купить ЦЕНА</code>", reply_markup=kb)

@router.callback_query(F.data == "entry:start")
async def entry_start(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id in allowed_users:
        await safe_answer(callback, "Есть доступ")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплатил", callback_data="entry:paid")],
        [InlineKeyboardButton(text="Назад", callback_data="entry:back")],
    ])
    await callback.message.edit_text(f"💳 {ENTRY_PRICE} TON на \n<code>{html.escape(TON_WALLET)}</code>\nПрисылайте скрин", reply_markup=kb)
    await safe_answer(callback)

@router.callback_query(F.data == "entry:back")
async def entry_back(callback: CallbackQuery, bot: Bot) -> None:
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Купить вход", callback_data="entry:start")]])
    await callback.message.edit_text(f"🎁 Вход {ENTRY_PRICE} TON", reply_markup=kb)
    await safe_answer(callback)

@router.callback_query(F.data == "entry:paid")
async def entry_paid(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id in allowed_users:
        await safe_answer(callback, "Есть доступ")
        return
    entry_pending.add(callback.from_user.id)
    await callback.message.edit_text("📸 Скиньте скриншот")
    try:
        await bot.send_message(ADMIN_ID, f"🔔 Входный\n{user_info_line(callback.from_user)}", reply_markup=admin_decision_kb(callback.from_user.id))
    except Exception: pass
    await safe_answer(callback, "✅ Заявка отправлена")

@router.message((F.photo | F.document), F.chat.type == "private")
async def on_screenshot(message: Message, bot: Bot) -> None:
    if message.from_user.id not in entry_pending: return
    entry_pending.discard(message.from_user.id)
    await message.copy_to(ADMIN_ID, caption=f"📸 Оплата от {user_info_line(message.from_user)}", reply_markup=admin_decision_kb(message.from_user.id))
    await message.answer("✅ Ожидайте")

# ===== ВОЗВРАЩЕН КОМАНДА /ledgerteam =====
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

@router.callback_query(F.data.startswith(("admin:allow:", "admin:deny:")))
async def admin_decision(callback: CallbackQuery, bot: Bot) -> None:
    if callback.from_user.id != ADMIN_ID: return
    if not callback.data or callback.data.count(":") != 2: return
    _, action, raw_id = callback.data.split(":")
    user_id = int(raw_id)
    if action == "allow":
        allowed_users.add(user_id); save_allowed_users(); entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "✅ Доступ выдан: <code>купить ЦЕНА</code>")
        except Exception: pass
        await safe_answer(callback, "✅ Добавлен")
    else:
        entry_pending.discard(user_id)
        try: await bot.send_message(user_id, "❌ Отклонено")
        except Exception: pass
        await safe_answer(callback, "❌ Отклонено")

# ===== Создание оффера =====
@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text: return
    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    payload = match.group(1).strip()

    lang = detect_lang(payload)

    price = parse_price(payload)
    if not price:
        await business_notice(bot, message, "Формат: <code>купить 100</code>")
        return

    currency = "TON" if any(w in payload.lower() for w in ("gram", "грам", "тон", "тн", "ton", "toncoin")) else "Stars"

    gift_link = "подарок"
    for w in payload.split():
        if w.startswith(("http://", "https://", "t.me/")):
            gift_link = w; break

    try:
        conn = await bot.get_business_connection(message.business_connection_id)
        buyer = conn.user
        if not buyer: return
        buyer_id = buyer.id
        if buyer.id not in allowed_users and buyer.id != ADMIN_ID:
            await business_notice(bot, message, "⛔ Нет доступа (/start)")
            return

        buyer_username = buyer.username or str(buyer.id)
        buyer_name = f"@{buyer.username}" if buyer.username else f"ID{buyer_id}"

        prune_offers()
        offer = Offer(
            id=secrets.token_urlsafe(8),
            connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id,
            buyer_id=buyer_id,
            buyer_name=buyer_name,
            price=price,
            currency=currency,
            lang=lang,
            buyer_username=buyer_username,
            gift_link=gift_link,
        )

        msg = await safe_send_message_text(
            bot, offer_text(offer), message.chat.id,
            message.business_connection_id, proposal_keyboard(offer),
            LinkPreviewOptions(is_disabled=True),
        )
        offer.message_id = msg.message_id
        offers[offer.id] = offer

        try:
            await bot.delete_business_messages(
                business_connection_id=message.business_connection_id,
                message_ids=[message.message_id],
            )
        except Exception as e:
            logging.warning(f"Не удалось удалить команду: {e}")

        logging.info(f"Оффер {offer.id}: {price} {currency} ({lang})")
    except Exception as e:
        logging.exception(f"Error create: {e}")

# ===== Принятие / Отклонение =====
@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    if not callback.data or ":" not in callback.data: return
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await safe_answer(callback, "Оффер не найден или истёк", show_alert=True)
        return
    if offer.status != "pending":
        await safe_answer(callback, "Этот оффер уже обработан", show_alert=True)
        return

    offer.status = "accepted" if action == "accept" else "declined"

    try:
        if action == "decline":
            if not await show_declined_state(bot, offer):
                offer.status = "pending"
                await safe_answer(callback, "Не удалось обновить сообщение, попробуйте ещё раз", show_alert=True)
                return
            logging.info(f"Оффер {offer_id}: ОТКЛОНЁН")
            await safe_answer(callback, "Отклонено")
            return

        if not await show_sold_state(bot, offer):
            offer.status = "pending"
            await safe_answer(callback, "Не удалось обновить сообщение, попробуйте ещё раз", show_alert=True)
            return

        logging.info(f"Оффер {offer_id}: ПРИНЯТ")
        await safe_answer(callback, alert_text(accept_alert_text(offer)), show_alert=True)

    except Exception as e:
        logging.exception(f"Resolve error: {e}")
        offer.status = "pending"
        await safe_answer(callback, alert_text(f"Ошибка: {str(e)[:60]}"), show_alert=True)

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")

    if not TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан.\n"
            f"Положите .env рядом с {Path(__file__).name} (BOT_TOKEN=123456:ABC...)\n"
            "или задайте переменную окружения BOT_TOKEN и запустите снова."
        )

    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    try:
        me = await bot.get_me()
    except Exception as e:
        raise SystemExit(f"Telegram не принял BOT_TOKEN: {e}")

    logging.info(f"Запуск как @{me.username} (id {me.id})")

    # если на токене остался webhook или второй инстанс бота — поллинг молча не работает
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logging.warning(f"delete_webhook: {e}")

    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
