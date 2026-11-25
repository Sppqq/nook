"""Identity checks shared by Telegram messages and callbacks."""
import hmac


def private_identity(chat, sender):
    return bool(chat and sender and chat.type == 'private' and chat.id == sender.id)


def private_sender(message):
    return bool(message and private_identity(message.chat, message.from_user))


def owner_message(message, owner_id):
    return owner_id is not None and private_sender(message) and message.from_user.id == owner_id


def private_callback(callback):
    return bool(callback.message and private_identity(callback.message.chat, callback.from_user))


def valid_pair_code(value, configured):
    return bool(isinstance(configured, str) and configured
                and hmac.compare_digest((value or '').strip().encode(), configured.encode()))
