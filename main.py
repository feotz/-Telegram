import asyncio
import json
import logging
import os
from contextlib import suppress
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (Message, InlineKeyboardButton, InlineKeyboardMarkup,
                           CallbackQuery, ChatMemberUpdated)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

bot = Bot(token=os.getenv("BOT_TOKEN"), default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATA_FILE = "bot_data.json"


def load_data():
    try:
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}

    data.setdefault('pending_reviews', {})
    data.setdefault('groups', [])
    data.setdefault('main_group_id', None)
    data.setdefault('settings', {
        'reviews_locked': False,
        'review_timeout_seconds': 0
    })
    data.setdefault('user_last_review_time', {})

    data['pending_reviews'] = {int(k): v for k, v in data['pending_reviews'].items()}
    return data


def save_data(data):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


bot_data = load_data()


class ReviewState(StatesGroup):
    waiting_for_review = State()


class AdminState(StatesGroup):
    waiting_for_rejection_reason = State()


def humanize_time(seconds: int) -> str:
    if seconds == 0: return "Отключен"
    if seconds == 86400: return "1 день"
    if seconds == 172800: return "2 дня"
    if seconds == 604800: return "1 неделя"
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    if days > 0: return f"{days} дн."
    if hours > 0: return f"{hours} ч."
    return f"{minutes} мин."


def get_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Оставить отзыв", callback_data="leave_review")]
    ])


def get_admin_panel_keyboard():
    reviews_count = len(bot_data['pending_reviews'])
    reviews_text = f"📋 Модерация ({reviews_count})" if reviews_count > 0 else "📋 Модерация"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=reviews_text, callback_data="admin_moderate_reviews")],
        [InlineKeyboardButton(text="👥 Мои группы", callback_data="admin_my_groups")],
        [InlineKeyboardButton(text="⚙️ Ограничения", callback_data="admin_restrictions")]
    ])


def get_back_keyboard(back_to: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=back_to)]
    ])


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! 👋\nЗдесь ты можешь оставить свой отзыв о (Ваше название).",
                         reply_markup=get_main_menu_keyboard())


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return await message.answer("⛔ У вас нет доступа к этой команде.")
    await state.clear()
    await message.answer("⚙️ Админ-панель", reply_markup=get_admin_panel_keyboard())


@dp.callback_query(F.data == "main_menu")
async def back_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Привет! 👋\nЗдесь ты можешь оставить свой отзыв о Т Е М К А.",
                                     reply_markup=get_main_menu_keyboard())


@dp.callback_query(F.data == "admin_panel")
async def back_to_admin_panel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Админ-панель", reply_markup=get_admin_panel_keyboard())



@dp.callback_query(F.data == "leave_review")
async def start_review(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id

    if bot_data['settings']['reviews_locked']:
        return await callback.answer("⛔ Прием отзывов временно приостановлен администратором.", show_alert=True)

    timeout_seconds = bot_data['settings']['review_timeout_seconds']
    if user_id != ADMIN_ID and timeout_seconds > 0:
        last_review_timestamp = bot_data['user_last_review_time'].get(str(user_id))
        if last_review_timestamp:
            elapsed = datetime.now().timestamp() - last_review_timestamp
            if elapsed < timeout_seconds:
                remaining_seconds = int(timeout_seconds - elapsed)
                return await callback.answer(
                    f"Вы сможете оставить следующий отзыв через {humanize_time(remaining_seconds)}.", show_alert=True)

    await callback.message.edit_text(
        "Напишите ваш отзыв (от 10 до 50 символов).\n\n"
        "По желанию, прикрепите к сообщению <b>фотографию</b> (текст отзыва в этом случае пишите в подписи к фото).",
        reply_markup=get_back_keyboard("main_menu")
    )
    await state.set_state(ReviewState.waiting_for_review)


@dp.message(ReviewState.waiting_for_review, F.text | F.photo)
async def process_review(message: Message, state: FSMContext):
    text = message.caption if message.photo else message.text
    if not text: return await message.answer("❌ Ошибка: к фотографии нужно добавить подпись с текстом отзыва.")
    if not (10 <= len(text) <= 50): return await message.answer(
        "❌ Ошибка: текст отзыва (или подпись к фото) должен содержать от 10 до 50 символов.")

    review_id = message.message_id
    bot_data['pending_reviews'][review_id] = {
        'user_id': message.from_user.id,
        'username': message.from_user.username,
        'first_name': message.from_user.first_name,
        'text': text,
        'photo_file_id': message.photo[-1].file_id if message.photo else None
    }

    bot_data['user_last_review_time'][str(message.from_user.id)] = datetime.now().timestamp()
    save_data(bot_data)
    await state.clear()

    await bot.send_message(ADMIN_ID, f"🔔 Новый отзыв на модерацию от @{message.from_user.username}.")
    await message.answer("✅ Спасибо! Твой отзыв отправлен на модерацию.")
    await cmd_start(message, state)


@dp.callback_query(F.data == "admin_moderate_reviews")
async def show_pending_reviews(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    message_to_edit = callback.message
    if callback.message.photo:
        await callback.message.delete()
        message_to_edit = await callback.message.answer("👀 Отзывы на модерации:")

    if not bot_data['pending_reviews']:
        await callback.answer("✅ Нет отзывов для модерации.", show_alert=True)
        return await message_to_edit.edit_text("⚙️ Админ-панель", reply_markup=get_admin_panel_keyboard())

    buttons = [[InlineKeyboardButton(text=f"От {review['first_name']}{' 🖼️' if review.get('photo_file_id') else ''}",
                                     callback_data=f"review_{review_id}")] for review_id, review in
               bot_data['pending_reviews'].items()]
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await message_to_edit.edit_text("👀 Отзывы на модерации:",
                                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("review_"))
async def moderate_review(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    review_id = int(callback.data.split("_")[1])
    review = bot_data['pending_reviews'].get(review_id)
    if not review:
        await callback.answer("Этот отзыв уже был обработан.", show_alert=True)
        return await show_pending_reviews(callback, state)

    caption_text = f"<b>Отзыв от {review['first_name']}</b> (@{review['username'] or 'N/A'})\n\n<i>\"{review['text']}\"</i>"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{review_id}"),
         InlineKeyboardButton(text="❌ Отказать", callback_data=f"reject_{review_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_moderate_reviews")]
    ])

    if review.get('photo_file_id'):
        await callback.message.delete()
        await callback.message.answer_photo(photo=review['photo_file_id'], caption=caption_text, reply_markup=keyboard)
    else:
        await callback.message.edit_text(caption_text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("approve_"))
async def approve_review(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    review_id = int(callback.data.split("_")[1])
    review = bot_data['pending_reviews'].pop(review_id, None)
    if not review: return await callback.answer("Этот отзыв уже был обработан.", show_alert=True)

    if not bot_data['main_group_id']:
        await callback.answer("⚠️ Основная группа не выбрана!", show_alert=True)
        await bot.send_message(review['user_id'], "✅ Ваш отзыв одобрен!")
    else:
        try:
            await bot.forward_message(chat_id=bot_data['main_group_id'], from_chat_id=review['user_id'],
                                      message_id=review_id)
            await bot.send_message(review['user_id'], "✅ Ваш отзыв одобрен и опубликован!")
            await callback.answer("✅ Отзыв переслан в группу!", show_alert=True)
        except Exception as e:
            await callback.answer(f"❌ Ошибка пересылки: {e}", show_alert=True)
            bot_data['pending_reviews'][review_id] = review

    save_data(bot_data)
    await show_pending_reviews(callback, state)


@dp.callback_query(F.data.startswith("reject_") & F.data.split("_")[1].isdigit())
async def reject_review_confirm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    review_id = int(callback.data.split("_")[1])
    buttons = [
        [InlineKeyboardButton(text="Без причины", callback_data=f"reject_final_noreason_{review_id}")],
        [InlineKeyboardButton(text="Указать причину", callback_data=f"reject_final_reason_{review_id}")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"review_{review_id}")]
    ]
    message_to_use = callback.message
    if callback.message.photo:
        await callback.message.delete()
        message_to_use = await callback.message.answer("Как отклонить отзыв?",
                                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    else:
        await message_to_use.edit_text("Как отклонить отзыв?",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("reject_final_noreason_"))
async def reject_final_noreason(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    review_id = int(callback.data.split("_")[-1])
    review = bot_data['pending_reviews'].pop(review_id, None)
    if not review: return await callback.answer("Отзыв уже обработан.", show_alert=True)
    with suppress(TelegramBadRequest): await bot.send_message(review['user_id'],
                                                              "❌ К сожалению, ваш отзыв был отклонен.")
    save_data(bot_data)
    await callback.answer("Отзыв отклонен.", show_alert=True)
    await show_pending_reviews(callback, state)


@dp.callback_query(F.data.startswith("reject_final_reason_"))
async def reject_final_reason_prompt(callback: CallbackQuery, state: FSMContext):
    review_id = int(callback.data.split("_")[-1])
    await state.set_state(AdminState.waiting_for_rejection_reason)
    await state.update_data(review_id_to_reject=review_id)
    await callback.message.edit_text("Напишите причину отказа. Она будет отправлена пользователю.",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                         [InlineKeyboardButton(text="🔙 Отмена", callback_data=f"review_{review_id}")]]))


@dp.message(AdminState.waiting_for_rejection_reason, F.text)
async def process_rejection_reason(message: Message, state: FSMContext):
    data = await state.get_data();
    review_id = data.get('review_id_to_reject');
    reason = message.text
    review = bot_data['pending_reviews'].pop(review_id, None)
    await state.clear()
    if not review: await message.answer("Отзыв уже был обработан."); return
    with suppress(TelegramBadRequest): await bot.send_message(review['user_id'],
                                                              f"❌ К сожалению, ваш отзыв был отклонен.\n<b>Причина:</b> {reason}")
    save_data(bot_data)
    await message.answer("✅ Причина отправлена, отзыв отклонен.")
    await cmd_admin(message, state)


@dp.callback_query(F.data == "admin_my_groups")
async def show_my_groups(callback: CallbackQuery):
    if not bot_data['groups']: return await callback.message.edit_text(
        "Бот пока не состоит ни в одной группе.\nЧтобы добавить группу, просто сделайте его администратором в ней.",
        reply_markup=get_back_keyboard("admin_panel"))
    buttons = [[InlineKeyboardButton(text=f"{g['title']}{' ⭐' if g['id'] == bot_data.get('main_group_id') else ''}",
                                     callback_data=f"group_{g['id']}")] for g in bot_data['groups']]
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")])
    await callback.message.edit_text("👥 Мои группы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("group_"))
async def group_options(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    group = next((g for g in bot_data['groups'] if g['id'] == group_id), None)
    if not group: return await callback.answer("Группа не найдена.", show_alert=True)
    try:
        chat = await bot.get_chat(group_id);
        invite_link = chat.invite_link or (await bot.export_chat_invite_link(group_id))
    except Exception:
        invite_link = None
    main_button_text = "⭐ Основная" if group_id == bot_data.get('main_group_id') else "Сделать основной"
    buttons = [
        [InlineKeyboardButton(text="➡️ Открыть группу", url=invite_link)] if invite_link else [],
        [InlineKeyboardButton(text=main_button_text, callback_data=f"setmain_{group_id}")],
        # <<< ИЗМЕНЕНИЕ: Кнопка ведет на подтверждение >>>
        [InlineKeyboardButton(text="🗑️ Удалить и выйти", callback_data=f"confirm_delete_{group_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_my_groups")]
    ]
    await callback.message.edit_text(f"Группа: <b>{group['title']}</b>",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("confirm_delete_"))  # <<< Новая логика подтверждения
async def confirm_delete_group(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[-1])
    buttons = [[InlineKeyboardButton(text="✅ Да, выйти", callback_data=f"delete_final_{group_id}"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"group_{group_id}")]]
    await callback.message.edit_text("Вы уверены, что хотите, чтобы бот покинул эту группу?",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("delete_final_"))  # <<< Финальное удаление
async def delete_and_leave_group(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[-1])
    try:
        await bot.leave_chat(group_id);
        await callback.answer("Бот покинул группу.", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка при выходе: {e}", show_alert=True)
    await show_my_groups(callback)


@dp.callback_query(F.data.startswith("setmain_"))
async def set_main_group(callback: CallbackQuery):
    group_id = int(callback.data.split("_")[1])
    if bot_data.get('main_group_id') == group_id: return await callback.answer("Эта группа уже является основной.",
                                                                               show_alert=True)
    bot_data['main_group_id'] = group_id
    save_data(bot_data)
    await callback.answer("⭐ Основная группа успешно изменена!", show_alert=True)
    await show_my_groups(callback)


@dp.callback_query(F.data == "admin_restrictions")
async def admin_restrictions_menu(callback: CallbackQuery):
    settings = bot_data['settings']
    timeout_text = f"⏳ Тайм-аут: {humanize_time(settings['review_timeout_seconds'])}"
    lock_text = "✅ Разблокировать отправку" if settings['reviews_locked'] else "❌ Заблокировать отправку"
    lock_callback = "confirm_unlock" if settings['reviews_locked'] else "confirm_lock"

    buttons = [
        [InlineKeyboardButton(text=timeout_text, callback_data="restrictions_timeout")],
        [InlineKeyboardButton(text=lock_text, callback_data=lock_callback)],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_panel")]
    ]
    await callback.message.edit_text("⚙️ Настройка ограничений",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data == "restrictions_timeout")
async def restrictions_timeout_menu(callback: CallbackQuery):
    buttons = [
        [InlineKeyboardButton(text="1 день", callback_data="set_timeout_86400"),
         InlineKeyboardButton(text="2 дня", callback_data="set_timeout_172800")],
        [InlineKeyboardButton(text="1 неделя", callback_data="set_timeout_604800"),
         InlineKeyboardButton(text="Отключить", callback_data="set_timeout_0")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_restrictions")]
    ]
    await callback.message.edit_text("⏳ Выберите тайм-аут между отзывами для одного пользователя:",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.startswith("set_timeout_"))
async def set_timeout(callback: CallbackQuery):
    seconds = int(callback.data.split("_")[-1])
    bot_data['settings']['review_timeout_seconds'] = seconds
    save_data(bot_data)
    await callback.answer(f"Тайм-аут установлен: {humanize_time(seconds)}", show_alert=True)
    await admin_restrictions_menu(callback)


@dp.callback_query(F.data.in_({"confirm_lock", "confirm_unlock"}))
async def confirm_lock_unlock(callback: CallbackQuery):
    action = "заблокировать" if callback.data == "confirm_lock" else "разблокировать"
    buttons = [[InlineKeyboardButton(text=f"✅ Да, {action}", callback_data=f"final_{action}"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data="admin_restrictions")]]
    await callback.message.edit_text(f"Вы уверены, что хотите {action} прием отзывов для всех?",
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@dp.callback_query(F.data.in_({"final_заблокировать", "final_разблокировать"}))
async def final_lock_unlock(callback: CallbackQuery):
    is_locking = callback.data == "final_заблокировать"
    bot_data['settings']['reviews_locked'] = is_locking
    save_data(bot_data)
    await callback.answer(f"Прием отзывов {'заблокирован' if is_locking else 'разблокирован'}!", show_alert=True)
    await admin_restrictions_menu(callback)


@dp.my_chat_member()
async def on_chat_member_updated(update: ChatMemberUpdated):
    if update.chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]: return
    chat_id, new_status, title = update.chat.id, update.new_chat_member.status, update.chat.title
    is_in_list = any(g['id'] == chat_id for g in bot_data['groups'])

    if new_status in ("administrator", "member") and not is_in_list:
        bot_data['groups'].append({'id': chat_id, 'title': title})
        await bot.send_message(ADMIN_ID, f"ℹ️ Бот был добавлен в группу: <b>{title}</b>")
    elif new_status in ("left", "kicked") and is_in_list:
        bot_data['groups'] = [g for g in bot_data['groups'] if g['id'] != chat_id]
        if bot_data.get('main_group_id') == chat_id: bot_data['main_group_id'] = None
        await bot.send_message(ADMIN_ID, f"ℹ️ Бот был удален из группы: <b>{title}</b>")

    save_data(bot_data)


async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    asyncio.run(main())

