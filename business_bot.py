"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить ссылка цена' -> Мгновенный оффер с красивой ссылкой.
         'купить цена' -> Ожидание (можно позже отправить 'купить ссылка').
Таймер в тексте статичный, фоновых правок нет.
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time  # <-- Вернут импорт time
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
# Попытка импортировать Flood ошибку
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

router = Router()
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Кулдаун и лок
SEND_COOLDOWN_SECONDS = 2.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()


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
    link: str
    gift_title: str
    price: str
    currency: str
    expires_at: datetime
    status: str  # "active" or "pending"


# Хранилища
offers: dict[str, Offer] = {}
pending_by_connection: dict[str, Offer] = {}


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
            wait_time = e.retry_after if hasattr(e, 'retry_after') else 60
            if attempt == max_retries - 1: raise e
            logging.info(f"Edit Flood Wait ({wait_time}s).")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logging.debug(f"Edit failed: {e}")
            if attempt == max_retries - 1: raise e
            await asyncio.sleep(1)


async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    last_error: Exception = None
    MAX_RETRIES = 6
    for attempt in range(MAX_RETRIES):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
        except TelegramRetryAfter as e:
            wait_time = e.retry_after if hasattr(e, 'retry_after') else 30
            logging.info(f"Send Flood Wait ({wait_time}s).")
            await asyncio.sleep(wait_time)
        except Exception as e:
            last_error = e
            if is_peer_flood(e):
                wait = 60 * (attempt + 1)
                logging.error(f"PEER_FLOOD retry {attempt+1}/{MAX_RETRIES} wait {wait}s")
                await asyncio.sleep(wait)
                continue
            raise
    if last_error: raise last_error


def gift_slug_from_link(link: str) -> str:
    match = re.search(r"/nft/([^/?#]+)", link)
    return match.group(1) if match else ""

def gift_title_from_slug(slug: str) -> str:
    name, _, num = slug.rpartition("-")
    if name and num.isdigit():
        return f"{name} #{int(num):,}"
    return slug or "NFT"

def parse_command_payload(payload: str) -> tuple[str | None, str | None, str]:
    """
    Разбор команды: (ссылка, цена, валюта)
    Работает и с 'купить цена ссылка', и с 'купить ссылка цена'.
    """
    parts = payload.split()
    if not parts: return None, None, "Stars"
    
    link = None
    price = None
    currency = "Stars"

    # 1. Ищем ссылку
    for part in parts:
        if "t.me/nft" in part and link is None:
            link = part
    
    # 2. Ищем цену (число)
    for part in parts:
        if part == link: continue
        try:
            raw = part.replace(",", ".").lower()
            if raw.replace(".", "").isdigit():
                price_val = float(raw)
                if 0 < price_val < 10000000:
                    price = price_val
                    if "gram" in payload.lower(): currency = "TON"
                    break
        except ValueError: pass

    # Форматирование цены
    if price: 
        price = f"{int(price) if int(price) == price else price:f}".rstrip('0').rstrip('.')
        
    return link, price, currency


def pending_text(offer: Offer) -> str:
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n"
        f"<i>(Ожидание ссылки...)</i>\n\n"
        f'<b>Напишите: купить ССЫЛКА</b>'
    )

def final_text(offer: Offer) -> str:
    # Если ссылка есть — кликабельный красивый текст
    if offer.link:
        title = offer.gift_title
        link_html = f'<b><a href="{offer.link}">{html.escape(title)}</a></b>'
    else:
        link_html = 'NFT'
        
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок {link_html}.\n\n"
        f"<b>Предложение действует ещё {offer.ttl_str}ч 59м.</b>"
    )

def sold_text(offer: Offer) -> str:
    return (
        f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {offer.currency}."
    )

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Передать", url=f"tg://user?id={offer.buyer_id}")],
    ])

def proposal_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Отклонить", callback_data=f"decline:{offer_id}"),
        InlineKeyboardButton(text="✓ Принять", callback_data=f"accept:{offer_id}"),
    ]])


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    payload = match.group(1)
    
    link, price, currency = parse_command_payload(payload)

    # Валидация: должно быть хоть что-то
    if not link and not price:
        await message.answer("Укажите цену или ссылку!", quote=False)
        return

    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id
        buyer_name = (
            f"@{buyer.username}" if getattr(buyer, "username", None)
            else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        )

        # --- Логика обработки ---
        
        # 1. Есть и ссылка, и цена -> Мгновенный оффер
        if link and price:
            offer_id = secrets.token_urlsafe(8)
            slug = gift_slug_from_link(link)
            
            offer = Offer(
                id=offer_id, connection_id=message.business_connection_id,
                chat_id=message.chat.id, message_id=message.message_id,
                buyer_id=buyer_id, buyer_name=buyer_name,
                link=link, gift_title=gift_title_from_slug(slug),
                price=price, currency=currency,
                expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS),
                status="active", ttl_str=str(TTL_HOURS) # Статичное время для текста
            )
            
            await delete_command(message, bot, message.business_connection_id)
            await send_final(offer_id, bot, message.chat.id, message.business_connection_id, offer)
            offers[offer_id] = offer
            return

        # 2. Есть только ссылка -> Обновляем pending оффер (если он есть)
        elif link and not price:
            pending = pending_by_connection.get(message.business_connection_id)
            if pending:
                pending.link = link
                pending.gift_title = gift_title_from_slug(gift_slug_from_link(link))
                pending.status = "active"
                pending.expires_at = datetime.now(UTC) + timedelta(hours=TTL_HOURS)
                
                # Обновляем текст и клавиатуру
                text = final_text(pending)
                await safe_edit_message_text(bot, text, pending.chat_id, pending.message_id,
                    pending.connection_id, proposal_keyboard(pending.id), LinkPreviewOptions(is_disabled=True))
                
                # Переносим в активные
                offers[pending.id] = pending
                del pending_by_connection[message.business_connection_id]
                
                await delete_command(message, bot, message.business_connection_id)
                return
            # Если pending нет, можно проигнорировать или создать (по логике выше лучше создать pending с ценой, но тут только ссылка)
            # Для простоты игнорируем если только ссылка без pending
            await message.answer("Ссылка не привязана ни к одному предложению.", quote=False)
            return

        # 3. Есть только цена -> Создаем Pending
        elif price and not link:
            offer_id = secrets.token_urlsafe(8)
            offer = Offer(
                id=offer_id, connection_id=message.business_connection_id,
                chat_id=message.chat.id, message_id=message.message_id,
                buyer_id=buyer_id, buyer_name=buyer_name,
                link="", gift_title="", price=price, currency=currency,
                expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS),
                status="pending", ttl_str=str(TTL_HOURS)
            )

            text = pending_text(offer)
            keyboard = proposal_keyboard(offer_id)
            
            await delete_command(message, bot, message.business_connection_id)
            
            msg = await safe_send_message_text(
                bot, text, message.chat.id, message.business_connection_id, keyboard,
                LinkPreviewOptions(is_disabled=True)
            )
            offer.message_id = msg.message_id
            
            offers[offer_id] = offer
            pending_by_connection[message.business_connection_id] = offer
            return

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"PeerFlood: {e!r}")
        else:
            logging.exception(f"Error: {e}")


async def send_final(offer_id: str, bot: Bot, chat_id: int, conn_id: str, offer: Offer):
    text = final_text(offer)
    keyboard = proposal_keyboard(offer_id)
    try:
        await safe_send_message_text(
            bot, text, chat_id, conn_id, keyboard, LinkPreviewOptions(is_disabled=True)
        )
    except Exception as e:
        logging.error(f"Failed to send final: {e}")
        # Если упало, очищаем, чтобы не висел невалидный оффер
        if offer_id in offers: offers.pop(offer_id)

async def delete_command(message: Message, bot: Bot, conn_id: str):
    try:
        await bot.delete_business_messages(conn_id, message_ids=[message.message_id])
    except: pass


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer: 
        await callback.answer("Оффер не найден.")
        return

    # Удаляем из pending если там был
    con_id = offer.connection_id
    if con_id in pending_by_connection and pending_by_connection[con_id].id == offer_id:
        del pending_by_connection[con_id]

    if action == "decline":
        await safe_edit_message_text(bot, "❌ Предложение отклонено", offer.chat_id, offer.message_id, offer.connection_id, None)
        offers.pop(offer_id)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        await safe_edit_message_text(
            bot, text, offer.chat_id, offer.message_id,
            offer.connection_id, sold_keyboard(offer)
        )
        offers.pop(offer_id)
        await callback.answer("Принято.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"), timeout=15)
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
