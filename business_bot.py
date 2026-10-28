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
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
