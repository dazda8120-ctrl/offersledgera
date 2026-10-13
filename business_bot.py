"""Telegram Business assistant for non-official gift proposals.
FIX PEER_FLOOD: Two-step creation support.
Mode A (Standard): 'buy link price' -> Instant offer.
Mode B (Split if needed): 'buy price' -> Pending offer (no link). Then 'buy link' -> Updates offer.
Emoji removed.
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
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

router = Router()
# Регулярка для разбора команды
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Кулдаун
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
    status: str = "active" # active, pending
    last_sent_text: str = ""
    pending_until: datetime | None = None

# Хранилищa
offers: dict[str, Offer] = {} # active & pending
pending_by_connection: dict[str, Offer] = {} # быстрый поиск по подключению покупателя


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
    for attempt in range(3):
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
                logging.error(f"Send PEER_FLOOD retry: {e}")
                await asyncio.sleep(30)
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
    return slug or "подарок"

def parse_command_payload(payload: str) -> tuple[str | None, str | None, str | None]:
    """
    Парсит содержимое после 'купить'.
    Возвращает (link, price, currency).
    """
    parts = payload.split()
    if not parts: return None, None, None
    
    link = None
    price = None
    currency = "Stars" # или TON

    # Ищем ссылку
    for part in parts:
        if "t.me/nft" in part:
            link = part
            break
    
    # Ищем цену
    if link:
        # Если ссылка есть, ищем цену в остальных частях
        for part in parts:
            if part == link: continue
            try:
                # Пытаемся распознать число с запятой или точкой
                raw = part.replace(",", ".").lower()
                if raw.replace(".", "").isdigit():
                    # Проверка что это не год и т.д.
                    if 0 < float(raw) < 10000000:
                        price = float(raw)
                        currency = "TON" if "gram" in payload.lower() else "Stars"
                        break
            except ValueError: pass

        # Если цена не найдена в строке, пробую инвертировать логику (ссылка может быть второй)
        # Для простоты: цена всегда число без тире/ссылок
    else:
        # Если ссылки нет, то первое число - это цена
        for part in parts:
            try:
                raw = part.replace(",", ".").lower()
                if raw.replace(".", "").isdigit():
                    if 0 < float(raw) < 10000000:
                        price = float(raw)
                        currency = "TON" if "gram" in payload.lower() else "Stars"
                        break
            except ValueError: pass

    if price: price = f"{int(price) if int(price) == price else price:f}".rstrip('0').rstrip('.')
    return link, price, currency


def remaining(offer: Offer) -> str:
    seconds = max(0, int((offer.expires_at - datetime.now(UTC)).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    return f"{hours}ч {minutes:02d}м"

# Текст для шага 1 (без ссылки)
def pending_text(offer: Offer, is_expired: bool = False) -> str:
    if is_expired:
        return "⚠️ Ожидание ссылки истекло. Отмена предложения."
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n"
        f"<i>Ожидание ссылки на NFT...</i>\n\n"
        f'<b>Введите: купить ССЫЛКА</b>'
    )

# Текст для финального оффера (с ссылкой)
def final_text(offer: Offer) -> str:
    title = offer.gift_title if offer.link else "NFT Gift"
    # Ссылка кликабельна
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n"
        f'<b><a href="{offer.link}">{html.escape(title)}</a></b>.\n\n'
        f'<b>Предложение действует ещё {remaining(offer)}.</b>'
    )

def sold_text(offer: Offer) -> str:
    return (
        f"{GIFT_EMOJI_HTML} Подарок: {html.escape(offer.gift_title)} успешно продан!\n\n"
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


def cleanup_pending():
    """Чистит устаревшие pending офферы"""
    now = datetime.now(UTC)
    for conn_id, off in list(pending_by_connection.items()):
        if off.status == "pending" and off.pending_until and now > off.pending_until:
            offers.pop(off.id)
            if conn_id in pending_by_connection: del pending_by_connection[conn_id]


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return
        
    cleanup_pending()

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    if not match: return
    payload = match.group(1)
    
    link, price, currency = parse_command_payload(payload)
    
    # Валидация
    if price is None and link is None:
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

        offer_id = secrets.token_urlsafe(8)
        temp_offer = Offer(
            id=offer_id,
            connection_id=message.business_connection_id,
            chat_id=message.chat.id,
            message_id=message.message_id, # ID команды
            buyer_id=buyer_id,
            buyer_name=buyer_name,
            link=link or "",
            gift_title="", # заполнится при добавлении ссылки
            price=price or "",
            currency=currency or "Stars",
            expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS),
            pending_until=datetime.now(UTC) + timedelta(minutes=5),
            status="active" if link else "pending"
        )
        
        keyboard = proposal_keyboard(temp_offer.id)

        # --- ВАРИАНТ 1: ПОЛНАЯ КОМАНДА (сразу и цена, и ссылка) ---
        if link and price:
            temp_offer.gift_title = gift_title_from_slug(gift_slug_from_link(link))
            temp_offer.status = "active"
            
            # Создаем сразу как активный оффер
            text = final_text(temp_offer)
            msg = await safe_send_message_text(
                bot, text, message.chat.id, message.business_connection_id, keyboard, LinkPreviewOptions(is_disabled=True)
            )
            temp_offer.message_id = msg.message_id
            
            offers[offer_id] = temp_offer
            await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
            logging.info(f"Оффер {offer_id}: создан полностью.")
            
        # --- ВАРИАНТ 2: ТОЛЬКО ССЫЛКА (досылаем ссылку к pending офферу) ---
        elif link and not price and message.business_connection_id in pending_by_connection:
            pending_offer = pending_by_connection[message.business_connection_id]
            pending_offer.link = link
            pending_offer.gift_title = gift_title_from_slug(gift_slug_from_link(link))
            pending_offer.status = "active"
            pending_offer.expires_at = datetime.now(UTC) + timedelta(hours=TTL_HOURS)
            
            # Редактируем старое сообщение
            text = final_text(pending_offer)
            await safe_edit_message_text(
                bot, text, pending_offer.chat_id, pending_offer.message_id,
                pending_offer.connection_id, keyboard, LinkPreviewOptions(is_disabled=True)
            )
            
            # Удаляем из pending и добавляем в main (update)
            del pending_by_connection[message.business_connection_id]
            offers[pending_offer.id] = pending_offer
            
            # (Опционально) Удаляем команду со ссылкой
            await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
            logging.info(f"Оффер {pending_offer.id}: добавлена ссылка.")

        # --- ВАРИАНТ 3: ТОЛЬКО ЦЕНА (начинаем создание) ---
        elif price and not link:
            pending_offer = temp_offer
            pending_offer.status = "pending"
            
            # Создаем сообщение в ожидании
            text = pending_text(pending_offer)
            msg = await safe_send_message_text(
                bot, text, message.chat.id, message.business_connection_id, keyboard, LinkPreviewOptions(is_disabled=True)
            )
            pending_offer.message_id = msg.message_id
            
            # Сохраняем
            offers[offer_id] = pending_offer
            pending_by_connection[pending_offer.connection_id] = pending_offer
            
            await bot.delete_business_messages(message.business_connection_id, message_ids=[message.message_id])
            logging.info(f"Оффер {offer_id}: создан (ожидание ссылки).")

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"PeerFlood: {e!r}")
        else:
            logging.exception(f"Error: {e}")


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer: 
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    if action == "decline":
        text = "❌ Предложение отклонено"
        if offer.status == "pending":
             # Если pending — удаляем
             try: 
                 await bot.delete_business_messages(offer.connection_id, [offer.message_id])
                 offers.pop(offer_id)
                 con_id = offer.connection_id
                 if con_id in pending_by_connection: del pending_by_connection[con_id]
             except: pass
        else:
            await safe_edit_message_text(bot, text, offer.chat_id, offer.message_id, offer.connection_id, None)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        # Если вдруг pending еще не стал active (редкий кейс)
        if offer.status == "pending":
            offer.status = "active" # принятие финализирует, если пользователь кликнул (хотя без ссылки странно)
            # Но для простоты:
            if not offer.link: text += " (Ссылка не указана!)"
        
        await safe_edit_message_text(
            bot, text, offer.chat_id, offer.message_id,
            offer.connection_id, sold_keyboard(offer)
        )
        await callback.answer("Принято.")
        # Удаляем из pending если там был
        con_id = offer.connection_id
        if con_id in pending_by_connection: del pending_by_connection[con_id]


async def refresh_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(60)
        cleanup_pending()
        now = datetime.now(UTC)
        for off in list(offers.values()):
            if off.status == "pending":
                # Обновить таймер ожидания ссылки
                text = pending_text(off, is_expired=False)
                try:
                    await safe_edit_message_text(
                        bot, text, off.chat_id, off.message_id,
                        off.connection_id, proposal_keyboard(off.id), LinkPreviewOptions(is_disabled=True)
                    )
                except: pass
            elif now >= off.expires_at:
                off.status = "expired"
                try:
                    await safe_edit_message_text(
                        bot, text="⚠️ Предложение истекло",
                        chat_id=off.chat_id, message_id=off.message_id,
                        business_connection_id=off.connection_id,
                    )
                except: pass
            else:
                new_text = final_text(off)
                if new_text != off.last_sent_text:
                    try:
                        await safe_edit_message_text(
                            bot, new_text, off.chat_id, off.message_id,
                            off.connection_id, proposal_keyboard(off.id), LinkPreviewOptions(is_disabled=True)
                        )
                        off.last_sent_text = new_text
                    except: pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"), timeout=15)
    dp = Dispatcher()
    dp.include_router(router)
    task = asyncio.create_task(refresh_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
