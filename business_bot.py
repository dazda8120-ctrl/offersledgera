"""Telegram Business assistant for non-official gift proposals.
Исправленная версия:
- Исправлен import time
- Исправлен класс Offer (добавлено ttl_str)
- Команда работает как /купить, так и купить
- Подробные логи для отладки
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time  # <--- ИСПРАВЛЕНО: импортируем time
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
# Хелперы ошибок
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
# Команда: купить (с / или без) + всё остальное
COMMAND_REGEX = re.compile(r"^/купить\s+(.+)|^купить\s+(.+)", re.IGNORECASE)

GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Кулдаун отправки (чтобы не спамить API)
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
    status: str
    ttl_str: str  # <--- ИСПРАВЛЕНО: добавлено поле


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
    for attempt in range(3):
        try:
            await bot.edit_message_text(
                text=text, chat_id=chat_id, message_id=message_id,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
            return
        except TelegramRetryAfter as e:
            wait = e.retry_after if hasattr(e, 'retry_after') else 60
            logging.info(f"EDIT FLOOD WAIT {wait}s")
            await asyncio.sleep(wait)
        except Exception as e:
            logging.warning(f"Edit error attempt {attempt}: {e}")
            if attempt == 2: raise e
            await asyncio.sleep(1)


async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    for attempt in range(6):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=link_preview_options
            )
        except TelegramRetryAfter as e:
            wait = e.retry_after if hasattr(e, 'retry_after') else 30
            logging.info(f"SEND FLOOD WAIT {wait}s")
            await asyncio.sleep(wait)
        except Exception as e:
            if is_peer_flood(e):
                wait = 60 * (attempt + 1)
                logging.error(f"PEER_FLOOD retry {attempt+1}, wait {wait}s")
                await asyncio.sleep(wait)
                continue
            raise e
    raise Exception("Too many retries")


def gift_slug_from_link(link: str) -> str:
    if not link: return ""
    # Гибкий поиск: /NFT/ или /nft/
    match = re.search(r"/nft/([^/?#]+)", link, re.IGNORECASE)
    return match.group(1) if match else ""

def gift_title_from_slug(slug: str) -> str:
    if not slug: return "NFT"
    # Формат: Имя-Номер -> Имя #Номер
    name, _, num = slug.rpartition("-")
    if name and num.isdigit():
        num_int = int(num)
        if num_int >= 1000:
            num_str = f"{num_int:,}"
        else:
            num_str = str(num_int)
        return f"{name} #{num_str}"
    return slug

def parse_command_payload(payload: str) -> tuple[str | None, str | None, str]:
    """Возвращает (ссылка, цена, валюта)"""
    parts = payload.split()
    if not parts: return None, None, "Stars"
    
    link = None
    price = None
    currency = "Stars"

    # 1. Ищем ссылку
    for part in parts:
        if "t.me/nft" in part.lower() and link is None:
            link = part
            
    # 2. Ищем цену (число)
    for part in parts:
        if part == link: continue
        try:
            raw = part.replace(",", ".").lower()
            if raw.replace(".", "").isdigit():
                val = float(raw)
                if 0 < val < 10_000_000:
                    price = val
                    if "gram" in payload.lower(): currency = "TON"
                    break
        except ValueError: pass

    if price: price = f"{int(price) if int(price) == price else price:f}".rstrip('0').rstrip('.')
    return link, price, currency


def final_text(offer: Offer) -> str:
    title = offer.gift_title if offer.gift_title else "NFT"
    if offer.link:
        # Красивая ссылка
        link_html = f'<b><a href="{offer.link}">{html.escape(title)}</a></b>'
    else:
        # Просто текст
        link_html = f'<b>{title}</b>'
        
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок {link_html}.\n\n"
        f"<b>Предложение действует ещё {offer.ttl_str}ч 59м.</b>"
    )

def pending_text(offer: Offer) -> str:
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n"
        f"<i>Ожидание ссылки на NFT...</i>\n\n"
        f'<b>Напишите: купить ССЫЛКА</b>'
    )

def sold_text(offer: Offer) -> str:
    return (
        f"{GIFT_EMOJI_HTML} Подарок успешно продан!\n\n"
        f"• Передайте подарок покупателю для зачисления {offer.currency}."
    )

def proposal_keyboard(offer_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✕ Отклонить", callback_data=f"decline:{offer_id}"),
        InlineKeyboardButton(text="✓ Принять", callback_data=f"accept:{offer_id}"),
    ]])

def sold_keyboard(offer: Offer) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Передать", url=f"tg://user?id={offer.buyer_id}")],
    ])


async def delete_command(message: Message, bot: Bot, conn_id: str):
    try:
        await bot.delete_business_messages(conn_id, message_ids=[message.message_id])
    except:
        pass


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text:
        return
        
    logging.info(f"COMMAND RECEIVED: {message.text[:50]}...")

    # Проверка Битнес-соединения
    conn_id = message.business_connection_id
    if not conn_id:
        logging.warning("No Business Connection ID found.")
        await message.answer("Это не бизнес-чат.", quote=False)
        return

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: 
        # Пробуем найти цену, если команда не полностью совпала (для удобства)
        payload = message.text.strip()
        if not payload.startswith("купить") and not payload.startswith("/купить"):
             return
        
        # Вырезаем если есть /
        if payload.startswith("/"):
             payload = payload[1:]
             
        # Снова пробуем
        parts = payload.split()
        if not parts: return
     
    # Парсим
    # Получаем группу 1 или 2 из регулярки
    payload = match.group(1) or match.group(2)
    
    link, price, currency = parse_command_payload(payload)
    
    logging.info(f"Parsed: Link={bool(link)}, Price={price}, Currency={currency}")

    if not link and not price:
        await message.answer("Непонятная команда. Пример: купить 100", quote=False)
        return

    try:
        connection = await bot.get_business_connection(conn_id)
        buyer = connection.user
        buyer_id = buyer.id
        buyer_name = (
            f"@{buyer.username}" if getattr(buyer, "username", None)
            else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        )

        # Сценарий 1: Есть и цена, и ссылка -> Отправляем сразу
        if link and price:
            offer_id = secrets.token_urlsafe(8)
            slug = gift_slug_from_link(link)
            title = gift_title_from_slug(slug)
            
            offer = Offer(
                id=offer_id, connection_id=conn_id,
                chat_id=message.chat.id, message_id=message.message_id,
                buyer_id=buyer_id, buyer_name=buyer_name,
                link=link, gift_title=title,
                price=price, currency=currency,
                expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS),
                status="active", ttl_str=str(TTL_HOURS)
            )
            
            await delete_command(message, bot, conn_id)
            
            text = final_text(offer)
            keyboard = proposal_keyboard(offer_id)
            
            try:
                await safe_send_message_text(
                    bot, text, message.chat.id, conn_id, keyboard, LinkPreviewOptions(is_disabled=True)
                )
                offers[offer_id] = offer
                logging.info(f"Active offer created: {price} {currency}")
            except:
                await message.answer("Ошибка при отправке (возможно PEER_FLOOD).", quote=False)
            
        # Сценарий 2: Только цена -> Ожидаем ссылку
        elif price and not link:
            offer_id = secrets.token_urlsafe(8)
            offer = Offer(
                id=offer_id, connection_id=conn_id,
                chat_id=message.chat.id, message_id=message.message_id,
                buyer_id=buyer_id, buyer_name=buyer_name,
                link="", gift_title="",
                price=price, currency=currency,
                expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS),
                status="pending", ttl_str=str(TTL_HOURS)
            )
            
            await delete_command(message, bot, conn_id)
            
            text = pending_text(offer)
            keyboard = proposal_keyboard(offer_id)
            
            try:
                msg = await safe_send_message_text(
                    bot, text, message.chat.id, conn_id, keyboard, LinkPreviewOptions(is_disabled=True)
                )
                offer.message_id = msg.message_id
                offers[offer_id] = offer
                pending_by_connection[conn_id] = offer
                logging.info(f"Pending offer created for {conn_id}")
            except:
                await message.answer("Ошибка при создании ожидания.", quote=False)
                
        # Сценарий 3: Только ссылка (завершаем pending)
        elif link and not price:
            if conn_id in pending_by_connection:
                pending = pending_by_connection[conn_id]
                pending.link = link
                pending.gift_title = gift_title_from_slug(gift_slug_from_link(link))
                pending.status = "active"
                
                text = final_text(pending)
                keyboard = proposal_keyboard(pending.id)
                
                await safe_edit_message_text(bot, text, pending.chat_id, pending.message_id, conn_id, keyboard, LinkPreviewOptions(is_disabled=True))
                
                del pending_by_connection[conn_id]
                logging.info(f"Pending offer {pending.id} completed.")
                await delete_command(message, bot, conn_id)
            else:
                await message.answer("Ссылка отправлена, но нет активного ожидания!", quote=False)

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"FLOOD ERROR: {e}")
            await message.answer("Временная задержка отправки (Flood).", quote=False)
        else:
            logging.exception(f"Unexpected error: {e}")
            await message.answer("Внутренняя ошибка бота.", quote=False)


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    
    if not offer: 
        await callback.answer("Оффер не найден.", show_alert=True)
        return
    
    # Удаляем из pending-списка если там был
    if offer.status == "pending" and offer.connection_id in pending_by_connection:
        if pending_by_connection[offer.connection_id].id == offer_id:
             del pending_by_connection[offer.connection_id]

    if action == "decline":
        offers.pop(offer_id, None)
        await safe_edit_message_text(bot, "❌ Предложение отклонено", offer.chat_id, offer.message_id, offer.connection_id, None)
        await callback.answer("Отклонено.")
    else:
        # Принятие
        text = sold_text(offer)
        await safe_edit_message_text(
            bot, text, offer.chat_id, offer.message_id,
            offer.connection_id, sold_keyboard(offer)
        )
        offers.pop(offer_id, None)
        await callback.answer("Принято.")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    if not TOKEN:
        logging.error("BOT_TOKEN is missing!")
        return
        
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"), timeout=15)
    dp = Dispatcher()
    dp.include_router(router)
    
    try:
        logging.info("Starting bot...")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
