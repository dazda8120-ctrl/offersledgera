"""Telegram Business assistant for non-official gift proposals.
FINAL FIX: Two-step edit to avoid PEER_FLOOD.
1. First edit: Text WITH link, but Link Preview DISABLED (safe from flood).
2. Second edit (after 0.5s delay): Link Preview ENABLED (restores image/card).
Emoji removed from proposal text. Delay reduced to 0.5s for smooth look.
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
COMMAND = re.compile(r"^купить\s+(https?://t\.me/nft/[^\s?#/]+)\s+(\S+)(?:\s+(.*))?$", re.IGNORECASE)
GIFT_EMOJI_HTML = '<tg-emoji emoji-id="6032937473162614352">🎁</tg-emoji>'

# Кулдаун на отправку (запасной путь)
SEND_COOLDOWN_SECONDS = 3.0
_last_send_time: float = 0.0
_send_lock = asyncio.Lock()


def is_peer_flood(e: Exception) -> bool:
    """Определяет PeerFlood в любом виде исключения."""
    return "PEER_FLOOD" in str(e).upper()


async def send_cooldown() -> None:
    """Кулдаун между отправками новых сообщений."""
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
    slug: str
    gift_title: str
    price: str
    currency: str
    expires_at: datetime
    status: str = "active"
    last_sent_text: str = ""


offers: dict[str, Offer] = {}


async def safe_edit_message_text(
    bot: Bot, text: str, chat_id: int, message_id: int,
    business_connection_id: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
    link_preview_options: LinkPreviewOptions | None = None
) -> None:
    """Редактирование с защитой от Flood."""
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
            if attempt == max_retries - 1:
                logging.warning(f"Flood Limit Reached (Edit). Wait {wait_time}s.")
                raise e
            logging.info(f"Edit Flood Wait ({wait_time}s). Retrying...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            logging.debug(f"Edit failed: {e}")
            if attempt == max_retries - 1: raise e
            await asyncio.sleep(1)


async def safe_send_then_delete(
    bot: Bot, text: str, chat_id: int, command_message_id: int,
    business_connection_id: str, reply_markup: InlineKeyboardMarkup,
) -> Message:
    """ЗАПАСНОЙ ПУТЬ: отправка оффера отдельным сообщением + удаление команды."""
    await send_cooldown()
    preview = LinkPreviewOptions(is_disabled=True)

    last_error: Exception | None = None
    sent_msg: Message | None = None

    for attempt in range(3):
        try:
            sent_msg = await bot.send_message(
                chat_id=chat_id, text=text,
                business_connection_id=business_connection_id,
                reply_markup=reply_markup, link_preview_options=preview
            )
            break
        except TelegramRetryAfter as e:
            wait_time = e.retry_after if hasattr(e, 'retry_after') else 30
            logging.info(f"Send Flood Wait ({wait_time}s). Retrying...")
            await asyncio.sleep(wait_time)
        except Exception as e:
            last_error = e
            if is_peer_flood(e):
                logging.error(f"PEER_FLOOD при отправке (попытка #{attempt + 1}) в чат {chat_id}: {e}")
                await asyncio.sleep(30)
                continue
            raise

    if sent_msg is None:
        raise last_error or RuntimeError("Не удалось отправить оффер")

    # Удаление команды
    for attempt in range(2):
        try:
            await bot.delete_business_messages(
                business_connection_id=business_connection_id,
                message_ids=[command_message_id]
            )
            break
        except Exception:
            break

    return sent_msg


def gift_slug_from_link(link: str) -> str:
    match = re.search(r"/nft/([^/?#]+)", link)
    return match.group(1) if match else ""

def gift_title_from_slug(slug: str) -> str:
    name, _, num = slug.rpartition("-")
    if name and num.isdigit():
        return f"{name} #{int(num):,}"
    return slug or "подарок"

def parse_command(text: str) -> tuple[str, str, str] | None:
    match = COMMAND.fullmatch(text.strip())
    if not match: return None
    link, raw_price, flags = match.groups()
    try:
        price = Decimal(raw_price.replace(",", "."))
        if price <= 0: return None
    except InvalidOperation: return None
    currency = "TON" if "gram" in set((flags or "").lower().split()) else "Stars"
    return link, format(price.normalize(), "f"), currency

def remaining(offer: Offer) -> str:
    seconds = max(0, int((offer.expires_at - datetime.now(UTC)).total_seconds()))
    hours, seconds = divmod(seconds, 3600)
    minutes, _ = divmod(seconds, 60)
    return f"{hours}ч {minutes:02d}м"

def proposal_text(offer: Offer) -> str:
    # Эмодзи удалено. Ссылка кликабельна.
    return (
        f"Пользователь <b>{html.escape(offer.buyer_name)}</b> предлагает Вам\n"
        f"<b>{offer.price} {offer.currency}</b> за подарок\n"
        f'<b><a href="{offer.link}">{html.escape(offer.gift_title)}</a></b>.\n\n'
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


async def enable_offer_preview_later(offer: Offer, bot: Bot):
    """
    Шаг 2 (через 0.5 сек): Включаем превью, чтобы появилась картинка/описание ссылки.
    Если PEER_FLOOD — оставляем как есть (кнопка и ссылка работают).
    """
    # Уменьшили задержку до 0.5 сек, чтобы переход был почти незаметен
    await asyncio.sleep(0.5)
    if offer.status == "active":
        try:
            await bot.edit_message_text(
                text=proposal_text(offer),
                chat_id=offer.chat_id,
                message_id=offer.message_id,
                business_connection_id=offer.connection_id,
                reply_markup=proposal_keyboard(offer.id),
                link_preview_options=LinkPreviewOptions(is_disabled=False)
            )
            logging.info(f"Оффер {offer.id}: превью успешно включено.")
        except Exception as e:
            if is_peer_flood(e):
                logging.warning(f"Оффер {offer.id}: не удалось включить превью (PEER_FLOOD). Оставляем без картинки, ссылка активна.")
            else:
                logging.warning(f"Оффер {offer.id}: ошибка при включении превью: {e}")


@router.business_message(F.text)
async def create_offer(message: Message, bot: Bot) -> None:
    if not message.text or not message.business_connection_id:
        return
    parsed = parse_command(message.text)
    if not parsed: return None
    link, price, currency = parsed
    try:
        connection = await bot.get_business_connection(message.business_connection_id)
        buyer = connection.user
        buyer_id = buyer.id
        buyer_name = (
            f"@{buyer.username}" if getattr(buyer, "username", None)
            else (buyer.full_name if hasattr(buyer, "full_name") else f"id{buyer.id}")
        )

        slug = gift_slug_from_link(link)
        temporary = Offer(
            id=secrets.token_urlsafe(8), connection_id=message.business_connection_id,
            chat_id=message.chat.id, message_id=message.message_id,
            buyer_id=buyer_id, buyer_name=buyer_name, link=link, slug=slug,
            gift_title=gift_title_from_slug(slug), price=price, currency=currency,
            expires_at=datetime.now(UTC) + timedelta(hours=TTL_HOURS, minutes=59),
            last_sent_text=""
        )

        keyboard = proposal_keyboard(temporary.id)
        text = proposal_text(temporary)

        #============ ШАГ 1 (БЕЗОПАСНЫЙ): РЕДАКТИРОВАНИЕ БЕЗ ПРЕВЬЮ ============
        # Ссылка в тексте ЕСТЬ (синяя кликабельная), но картинка-превью выключена.
        # Это спасает от PEER_FLOOD при первой отправке.
        edit_ok = False
        try:
            await bot.edit_message_text(
                text=text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                business_connection_id=message.business_connection_id,
                reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True)  # Выключено сейчас
            )
            edit_ok = True
            logging.info(f"Оффер {temporary.id}: создан (Шаг 1: без превью).")
        except Exception as edit_error:
            logging.warning(f"Не удалось оформить оффер редактированием (chat_id={message.chat.id}): {edit_error!r}")

        if edit_ok:
            temporary.message_id = message.message_id
            # Запускаем Шаг 2 асинхронно (не блокируя бота)
            asyncio.create_task(enable_offer_preview_later(temporary, bot))
        else:
            # Если Шаг 1 не сработал — запасной путь с отправкой сообщения
            sent_msg = await safe_send_then_delete(
                bot=bot,
                text=text,
                chat_id=message.chat.id,
                command_message_id=message.message_id,
                business_connection_id=message.business_connection_id,
                reply_markup=keyboard,
            )
            temporary.message_id = sent_msg.message_id

        temporary.last_sent_text = proposal_text(temporary)
        offers[temporary.id] = temporary

    except Exception as e:
        if is_peer_flood(e):
            logging.error(f"PeerFlood для chat_id={message.chat.id}: {e!r}")
            try:
                await bot.edit_message_text(
                    text="⚠️ Не удалось отправить предложение (ограничение Telegram). Попробуйте позже.",
                    chat_id=message.chat.id, message_id=message.message_id,
                    business_connection_id=message.business_connection_id,
                    reply_markup=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
            except Exception:
                pass
        else:
            logging.exception("Could not create business proposal: %s", e)


@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer or offer.status != "active":
        await callback.answer("Предложение уже закрыто.", show_alert=True)
        return

    offer.status = "accepted" if action == "accept" else "declined"

    try:
        if action == "decline":
            text = "❌ <b>Предложение отклонено</b>\nСредства не резервировались."
            await safe_edit_message_text(
                bot, text=text, chat_id=offer.chat_id, message_id=offer.message_id,
                business_connection_id=offer.connection_id, reply_markup=None
            )
            await callback.answer("Предложение отклонено.")
        else:
            text = sold_text(offer)
            await safe_edit_message_text(
                bot, text=text, chat_id=offer.chat_id, message_id=offer.message_id,
                business_connection_id=offer.connection_id, reply_markup=sold_keyboard(offer)
            )
            await callback.answer("Оффер принят.")
    except Exception as e:
        logging.exception(f"Resolve error: {e}")


async def refresh_loop(bot: Bot) -> None:
    while True:
        await asyncio.sleep(60)
        now = datetime.now(UTC)
        for offer in list(offers.values()):
            if offer.status != "active":
                continue

            if now >= offer.expires_at:
                offer.status = "expired"
                try:
                    await safe_edit_message_text(
                        bot, text="⚠️ <b>Предложение истекло</b>",
                        chat_id=offer.chat_id, message_id=offer.message_id,
                        business_connection_id=offer.connection_id,
                    )
                except Exception: pass
                continue

            new_text = proposal_text(offer)
            if new_text != offer.last_sent_text:
                try:
                    # Refresh loop также пытается держать превью включённым
                    await safe_edit_message_text(
                        bot, text=new_text,
                        chat_id=offer.chat_id, message_id=offer.message_id,
                        business_connection_id=offer.connection_id,
                        reply_markup=proposal_keyboard(offer.id),
                        link_preview_options=LinkPreviewOptions(is_disabled=False)
                    )
                    offer.last_sent_text = new_text
                except Exception:
                    logging.debug("Could not refresh timer for %s", offer.id)


async def main() -> None:
    if not TOKEN: raise RuntimeError("Заполните BOT_TOKEN в .env")
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
