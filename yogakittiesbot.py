import datetime as dt
import locale
import logging
import os
import sys
import time
from threading import Thread

import pymongo
import schedule
from dotenv import load_dotenv
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup)
from telegram.ext import (CallbackQueryHandler, CommandHandler, Filters,
                          MessageHandler, Updater, ConversationHandler)
from pymongo.operations import UpdateOne

load_dotenv()

SECRET_TOKEN = os.getenv('TOKEN')

WEBHOOK_LISTEN = '0.0.0.0'
WEBHOOK_PORT = 8443

WEBHOOK_SSL_CERT = '/etc/ssl/certs/cert.pem'
WEBHOOK_SSL_PRIV = '/etc/ssl/private/private.key'

WEBHOOK_DOMAIN = '185.139.68.168'

# variables for conversation (see conv_handler)
F_NAME, L_NAME = range(2)

# set datetime in local language 'ru_RU' - Russia
locale.setlocale(locale.LC_TIME, 'ru_RU')

# setup db
client = pymongo.MongoClient()
db_name = 'YogaKitties'
collection_users = 'users'
collection_groups = 'groups'


# set collections
users = client[db_name][collection_users]
groups = client[db_name][collection_groups]

# groups.delete_many({})
# set groups
if not groups.count_documents({"group_name": "Йога 17:30"}, limit=1):
    groups.insert_one({"group_name": "Йога 17:30", "participants": {}})

if not groups.count_documents({"group_name": "Йога 18:40"}, limit=1):
    groups.insert_one({"group_name": "Йога 18:40", "participants": {}})

group17_id = groups.find_one({"group_name": "Йога 17:30"})["_id"]
group18_id = groups.find_one({"group_name": "Йога 18:40"})["_id"]


# logging
logger = logging.getLogger(__name__)
handler = logging.StreamHandler(sys.stdout)
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)


# methods for working with db
def create_user(id, first_name, last_name, user_id):
    """
    Создание профиля пользователя в базе данных, если его нет.
    """
    user = users.find_one({"id": id})
    if user is not None:
        return True
    try:
        users.insert_one(
            {
                "id": id,
                "first_name": first_name,
                "last_name": last_name,
                "user_id": user_id,
                "workouts": 0,
            }
        )
    except Exception as error:
        logger.error(f'Ошибка создания профиля в базе: {error}')


def subscribe_user(id, chatt):
    """Занесение пользоваться в список участников."""
    group = groups.find_one({"_id": id})
    if chatt in group['participants']:
        return f'Вы уже записаны в группу: {group["group_name"]}'
    data = {}
    user = users.find_one({"id": chatt})
    first_name = user['first_name']
    last_name = user['last_name']
    if not last_name:
        last_name = ''
    data[f'participants.{chatt}'] = first_name + last_name
    groups.update_one(
        {"_id": group["_id"]},
        {"$set": data},
        upsert=True
    )
    return f'Записал вас в группу: {group["group_name"]}'


def unsubscribe_user(id, chatt):
    """Удаление пользователя из списка участников."""
    group = groups.find_one({"_id": id})
    groups.update_one(
        {"_id": group["_id"]},
        {"$unset":
            {
                f"participants.{chatt}": ""
            }
        },
    )
    return f'Вы отписаны от занятия: {group["group_name"]}'


def count_workouts():
    """Подсчёт количества тренировок после каждого занятия."""
    participants17 = groups.find_one({"_id": group17_id})['participants']
    participants18 = groups.find_one({"_id": group18_id})['participants']
    people = [*participants17.keys()] + [*participants18.keys()]
    if not len(people):
        return False
    try:
        users.bulk_write([
            UpdateOne(filter={"id": chatid},
                      update={"$inc": {"workouts": 1}},)
            for chatid in people])
        return True
    except Exception as error:
        logger.error(f'Ошибка изменения данных в базе: {error}')


def clear_participants():
    """Очистка списка участников после подсчёта тренировок."""
    if not count_workouts():
        return False
    logger.info('Очистка списка участников...')
    try:
        groups.update_many(
            {},
            {"$set":
                {"participants": {}}
            },
        )
    except Exception as error:
        logger.error(f'Ошибка очистки участников: {error}')


def get_participants(id):
    """Получить список участников предстоящего занятия."""
    group = groups.find_one({"_id": id})
    participants = group['participants']
    participants_amount = len(participants)
    group_name = group["group_name"]
    if participants_amount == 0:
        return f'В группу "{group_name}" ещё никто не записался 😢'
    elif participants_amount != 0:
        message = ''
        # loop can be replaced with f'{"\n".join(map(name, participants.values()))}'
        for name in participants.values():
            message += f'\n- {name}'
        return f'Количество участников: {participants_amount}\n{message}'


def get_user_data(chatid):
    """Получить информацию о пользователе."""
    user = users.find_one({"id": chatid})
    first_name = user["first_name"]
    last_name = user["last_name"]
    if not last_name:
        last_name = ''
    workouts = user["workouts"]
    message = (f'Информация о пользователе:\n'
            f'Имя: {first_name} {last_name}\n'
            f'Количество тренировок: {workouts}')
    return message


def update_profile(chatid, value, status):
    """Изменение профиля пользователя в базе."""
    parameter = ''
    if status == F_NAME:
        parameter = "first_name"
    elif status == L_NAME:
        parameter = "last_name"
    try:
        users.update_one(
            {"id": chatid},
            {"$set":
                {parameter: value}
            },
        )
        return True
    except Exception as error:
        logger.error(f'Ошибка изменения профиля: {error}')
        return False


# bot methods
def wake_up(update, context):
    """Отправка приветственного сообщения при первом включении бота."""
    chat = update.effective_chat
    chatt = str(chat.id)
    first_name = update.message.chat.first_name
    last_name = update.message.chat.last_name
    user_id = update.effective_user.id
    buttons = [
        ['📋 Профиль', '✏️ Записаться']
    ]
    reply_markup = ReplyKeyboardMarkup(buttons, resize_keyboard=True)
    create_user(chatt, first_name, last_name, user_id)
    context.bot.send_message(
        chat_id=chat.id,
        text=f'Привет, йога-котик {first_name} 😻\n'
        'Я буду управлять твоей записью на занятия йоги 🙏',
        reply_markup=reply_markup
    )


def subscribe_menu():
    return [
        [InlineKeyboardButton('Йога 17:30', callback_data='group17'),
        InlineKeyboardButton('Йога 18:40', callback_data='group18')],
        [InlineKeyboardButton(
            'Участники 17:30', callback_data='participants17'),
        InlineKeyboardButton(
            'Участники 18:40', callback_data='participants18')]
    ]


def on_message(update, context):
    """Обработка сообщений от пользователя."""
    chat = update.effective_chat
    buttons = subscribe_menu()
    if update.message.text == '✏️ Записаться':
        context.bot.send_message(
            chat_id=chat.id,
            text=f'Предстоящее занятие состоится: {class_day()}\n'
            'Выберите группу, в которую хотите записаться:',
            reply_markup=InlineKeyboardMarkup(buttons, resize_keyboard=True)
        )
    elif update.message.text == '📋 Профиль':
        message = get_user_data(str(chat.id))
        context.bot.send_message(
            chat_id=chat.id,
            text=message,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton('Редактировать', callback_data='edit_profile')]],
                resize_keyboard=True
            )
        )
    # else:
    #     context.bot.send_message(
    #         chat_id=chat.id,
    #         text='Когда-нибудь я научусь поддерживать осмысленные разговоры '
    #         'о йоге, жизни, вселенной и всё такое.. 😌\nа пока воспользуйтесь '
    #         'функционалом записи на занятия 🙂'
    #     )


def subscribe_to_class(update, context):
    """Запись участника на занятие."""
    chat = update.effective_chat
    query = update.callback_query
    chatt = str(chat.id)
    buttons = [
        [InlineKeyboardButton('Отписаться', callback_data='unsubscribe'),
        InlineKeyboardButton('Назад', callback_data='back')]
    ]
    if query.data == 'group17':
        message = subscribe_user(group17_id, chatt)
    elif query.data == 'group18':
        message = subscribe_user(group18_id, chatt)
    update.callback_query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(buttons, resize_keyboard=True),
    )


def unsubscribe_from_class(update, context):
    """Отписка участника от занятия."""
    chat = update.effective_chat
    query = update.callback_query.message.text
    chatt = str(chat.id)
    if '17:30' in query:
        message = unsubscribe_user(group17_id, chatt)
    elif '18:40' in query:
        message = unsubscribe_user(group18_id, chatt)
    update.callback_query.edit_message_text(
            text=message,
        )


def show_participants(update, context):
    """Показать записавшихся участников."""
    query = update.callback_query
    button = [
        [InlineKeyboardButton('Назад', callback_data='back')]
    ]
    if query.data == 'participants17':
        message = get_participants(group17_id)
    elif query.data == 'participants18':
        message = get_participants(group18_id)
    query.edit_message_text(
        text=message,
        reply_markup=InlineKeyboardMarkup(button, resize_keyboard=True)
    )


def back_to_subscribe_menu(update, context):
    """Возврат в основное меню записи."""
    query = update.callback_query
    buttons = subscribe_menu()
    query.edit_message_text(
        text=f'Предстоящее занятие состоится: {class_day()}\n'
            'Выберите группу, в которую хотите записаться:',
        reply_markup=InlineKeyboardMarkup(buttons, resize_keyboard=True)
    )


def start_edit_profile(update, context):
    """Начало разговора для получения информации от пользователя."""
    chat = update.effective_chat
    context.bot.send_message(
            chat_id=chat.id,
            text='- Для перехода к изменению фамилии, отправьте /skip\n'
                 '- Для завершения разговора отправьте /cancel\n\n'
                 'Напишите пожалуйста имя:\n')
    return F_NAME


def get_f_name(update, context):
    """Изменение имени."""
    chatid = update.effective_chat.id
    first_name = update.message.text
    if update_profile(chatid, first_name, F_NAME):
        update.message.reply_text(
            'Имя успешно изменено. Напишите пожалуйста фамилию или /skip'
        )
        return L_NAME
    else:
        update.message.reply_text(
            'Произошла ошибка 😢. Попробуйте позднее.'
        )
        return ConversationHandler.END


def skip_f_name(update, context):
    update.message.reply_text(
        'Напишите пожалуйста фамилию или /skip'
    )
    return L_NAME


def get_l_name(update, context):
    chatid = update.effective_chat.id
    last_name = update.message.text
    if update_profile(chatid, last_name, L_NAME):
        update.message.reply_text(
            'Спасибо! Изменения сохранены.'
        )
        return ConversationHandler.END
    update.message.reply_text(
        'Произошла ошибка 😢. Попробуйте позднее.'
    )
    return ConversationHandler.END


def skip_l_name(update, context):
    update.message.reply_text(
        'Спасибо! Изменения сохранены.'
    )
    return ConversationHandler.END


def cancel(update, context):
    update.message.reply_text(
        'Может быть продолжим в следующий раз 🙂'
    )
    return ConversationHandler.END


def add_handlers(dispatcher):
    """Регистрация обработчиков команд ботом"""
    conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_edit_profile, pattern='edit_profile'),
        ],
        states={
            F_NAME: [
                MessageHandler(Filters.text & ~Filters.command, get_f_name),
                CommandHandler('skip', skip_f_name)
            ],
            L_NAME: [
                MessageHandler(Filters.text & ~Filters.command, get_l_name),
                CommandHandler('skip', skip_f_name)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    dispatcher.add_handler(CommandHandler('start', wake_up))
    dispatcher.add_handler(MessageHandler(Filters.text, on_message))
    dispatcher.add_handler(
        CallbackQueryHandler(subscribe_to_class, pattern='group17'))
    dispatcher.add_handler(
        CallbackQueryHandler(subscribe_to_class, pattern='group18'))
    dispatcher.add_handler(
        CallbackQueryHandler(unsubscribe_from_class, pattern='unsubscribe'))
    dispatcher.add_handler(
        CallbackQueryHandler(show_participants, pattern='participants17'))
    dispatcher.add_handler(
        CallbackQueryHandler(show_participants, pattern='participants18'))
    dispatcher.add_handler(
        CallbackQueryHandler(back_to_subscribe_menu, pattern='back'))
    dispatcher.add_handler(conv_handler)


# other methods
def check_tokens():
    """Проверка обязательных переменных окружения, где хранятся токены."""
    if SECRET_TOKEN is None:
        logger.critical(
            f'Отсутствует обязательная переменная окружения: {SECRET_TOKEN}'
        )
        return False
    return True


def class_day():
    """Определение даты предстоящего занятия."""
    classday = dt.datetime.now()
    weekday_now = int(classday.strftime('%w'))
    if (weekday_now in [1, 3, 5]):
        return classday.strftime('%A, %d %B %Y')
    elif (weekday_now in [0, 2, 4]):
        classday = classday + dt.timedelta(days=1)
        return classday.strftime('%A, %d %B %Y')
    elif weekday_now == 6:
        classday = classday + dt.timedelta(days=2)
        return classday.strftime('%A, %d %B %Y')


def setup_schedule():
    """Создание расписания по очистке списка участников"""
    schedule.every().tuesday.at('00:10').do(clear_participants)
    schedule.every().thursday.at('00:10').do(clear_participants)
    schedule.every().saturday.at('00:10').do(clear_participants)

    scheduler = Thread(target=schedule_checker, daemon=True)
    scheduler.start()
    logger.info('Расписание очистки списка участников установлено')


def schedule_checker():
    """Проверка расписания для запуска установленных работ."""
    while True:
        schedule.run_pending()
        time.sleep(1)


def main():
    """Основная логика работы бота."""
    if not check_tokens():
        raise Exception('Отсутствуют обязательные переменные окружения')

    # setup schedule and spin up a thread to clear participants list
    setup_schedule()

    updater = Updater(token=SECRET_TOKEN)
    dispatcher = updater.dispatcher
    add_handlers(dispatcher)

    # updater.start_polling()
    # set webhook
    # updater.bot.set_webhook(url='https://185.139.68.168/root/yogabot/yogakittiesbot.py',
    #			certificate=open(WEBHOOK_SSL_CERT, 'rb'),
    #			max_connections=100, ip_address='185.139.68.168')

    updater.start_webhook(listen=WEBHOOK_LISTEN,
                          port=WEBHOOK_PORT,
                          url_path=f'{SECRET_TOKEN}',
                          key=WEBHOOK_SSL_PRIV,
                          cert=WEBHOOK_SSL_CERT,
                          webhook_url=f'https://{WEBHOOK_DOMAIN}:{WEBHOOK_PORT}/{SECRET_TOKEN}/')
    updater.idle()


if __name__ == '__main__':
    main()
