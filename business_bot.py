@router.callback_query(F.data.startswith(("accept:", "decline:")))
async def resolve_offer(callback: CallbackQuery, bot: Bot) -> None:
    action, offer_id = callback.data.split(":", 1)
    offer = offers.get(offer_id)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

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
            # САМОЕ ВАЖНОЕ: используем bot.edit_message_text с business_connection_id
            await bot.edit_message_text(
                text=text,
                chat_id=offer.chat_id,
                message_id=offer.message_id,
                business_connection_id=offer.connection_id,
                reply_markup=kb
            )
        except Exception as e:
            logging.error(f"Ошибка при редактировании (принятие): {e}")
            # Если вдруг не сработало, отправляем новое сообщение
            try:
                await safe_send_message_text(
                    bot, text, offer.chat_id, offer.connection_id, kb
                )
            except Exception as send_e:
                logging.error(f"Ошибка при отправке нового сообщения: {send_e}")
        
        offers.pop(offer_id, None)
        
        # Показываем окно (Alert)
        alert_text = accept_alert_text(offer)
        if len(alert_text) > 200:
            alert_text = alert_text[:197] + "..."
        await callback.answer(alert_text, show_alert=True)
