"""Telegram Business assistant for non-official gift proposals.
Команда: 'купить [ссылка на NFT] цена'
Пример: 'купить t.me/nft/xxx 100'
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time
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
COMMAND_REGEX = re.compile(r"^купить\s+(.*)$", re.IGNORECASE | re.DOTALL)
URL_REGEX = re.compile(r'(?:https?://|tg://|t\.me/)\S+', re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

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
    price: str
    currency: str
    owner_id: int
    nft_url: str | None = None


offers: dict[str, Offer] = {}


async def safe_send_message_text(
    bot: Bot, text: str, chat_id: int,
    business_connection_id: str,
    reply_markup: InlineKeyboardMarkup,
    link_preview_options: LinkPreviewOptions
) -> Message:
    await send_cooldown()
    last_error: Exception = None
    MAX_PEER_FLOOD_RETRIES = 6
    for attempt in range(MAX_PEER_FLOOD_RETRIES):
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
                logging.error(f"PEER_FLOOD, ждём {wait}с (попытка {attempt+1}/{MAX_PEER_FLOOD_RETRIES})")
                await asyncio.sleep(wait)
                continue
            raise
    if last_error: raise last_error


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


def parse_payload(payload: str) -> tuple[str | None, str | None]:
    url_match = URL_REGEX.search(payload)
    nft_url = url_match.group(0) if url_match else None
    remaining = URL_REGEX.sub('', payload)
    price = parse_price(remaining)
    return price, nft_url


def offer_text(offer: Offer) -> str:
    text = (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n\n"
    )
    if offer.nft_url:
        text += f'🔗 <a href="{html.escape(offer.nft_url)}">Ссылка на NFT</a>\n\n'
    text += f"<b>Предложение действует ещё {TTL_HOURS}ч 59м.</b>"
    return text


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
    logging.info(
        f"[business_message] text={message.text!r} "
        f"from={(message.from_user.id if message.from_user else None)} "
        f"conn_id={message.business_connection_id}"
    )

    if not message.text or not message.business_connection_id:
        logging.info("  -> нет text или conn_id, skip")
        return

    match = COMMAND_REGEX.fullmatch(message.text.strip())
    logging.info(f"  -> regex: {match}")
    if not match:
        return

    payload = match.group(1).strip()
    price, nft_url = parse_payload(payload)
    logging.info(f"  -> payload={payload!r} price={price!r} url={nft_url!r}")

    if price is None:
        try:
            await bot.send_message(
                chat_id=message.chat.id,
                text="Укажите цену! Пример: купить t.me/nft/xxx 100",
                business_connection_id=message.business_connection_id,
            )
        except Exception as e:
            logging.warning(f"Не смог ответить об ошибке: {e}")
        return

    currency = "TON" if "gram" in payload.lower() else "Stars"

    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        owner = connection.user
        owner_id = owner.id
        logging.info(f"  -> owner_id={owner_id}")

        buyer = message.from_user
        if not buyer:
            logging.warning("  -> нет from_user, skip")
            return

        if buyer.id == owner_id:
            logging.warning("  -> команда от владельца, создаю оффер (тестовый режим)")

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
            message_id=message.message_id,
            buyer_id=buyer_id,
            buyer_name=buyer_name,
            price=price,
            currency=currency,
            owner_id=owner_id,
            nft_url=nft_url,
        )

        text = offer_text(temp_offer)
        keyboard = proposal_keyboard(temp_offer.id)

        logging.info(f"  -> отправляю оффер в chat_id={message.chat.id}")
        msg = await safe_send_message_text(
            bot, text, message.chat.id, message.business_connection_id, keyboard,
            LinkPreviewOptions(is_disabled=True)
        )
        temp_offer.message_id = msg.message_id
        offers[offer_id] = temp_offer
        logging.info(f"  -> оффер {offer_id} отправлен, msg_id={msg.message_id}")

        try:
            await bot.delete_business_messages(
                message.business_connection_id, message_ids=[message.message_id]
            )
            logging.info("  -> сообщение покупателя удалено")
        except Exception as e:
            logging.warning(f"  -> не смог удалить: {e}")

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"  -> PeerFlood: {e!r}")
        else:
            logging.exception(f"  -> ОШИБКА: {e}")


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    logging.info(
        f"[callback] data={callback.data!r} from={callback.from_user.id}"
    )
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    logging.info(f"  -> offer.owner_id={offer.owner_id}, clicker={callback.from_user.id}")

    if callback.from_user.id != offer.owner_id:
        await callback.answer(
            "⛔ Только владелец может принять или отклонить предложение.",
            show_alert=True
        )
        return

    if action == "decline":
        await safe_edit_message_text(
            bot, "❌ Предложение отклонено",
            offer.chat_id, offer.message_id, offer.connection_id, None
        )
        offers.pop(offer_id, None)
        await callback.answer("Отклонено.")
    else:
        text = sold_text(offer)
        await safe_edit_message_text(
            bot, text, offer.chat_id, offer.message_id,
            offer.connection_id, sold_keyboard(offer)
        )
        offers.pop(offer_id, None)
        await callback.answer("Принято.")


@router.message()
async def debug_all(message: Message) -> None:
    logging.info(
        f"[обычное сообщение] text={message.text!r} "
        f"chat_type={message.chat.type} "
        f"conn_id={message.business_connection_id} "
        f"from={(message.from_user.id if message.from_user else None)}"
    )


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
