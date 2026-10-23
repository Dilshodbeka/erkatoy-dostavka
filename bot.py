import os
import json
import asyncio
import itertools
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,
                      ReplyKeyboardMarkup, KeyboardButton, WebAppInfo)
from telegram.ext import (Application, CommandHandler, CallbackQueryHandler,
                          MessageHandler, filters, ContextTypes)

logging.basicConfig(level=logging.INFO)

# ===== НАСТРОЙКИ =====
TOKEN = os.getenv("BOT_TOKEN", "8672474046:AAGfIA0b7lVdDZfkDlERqPBXRHSNNE870X8")

# Telegram ID курьеров (узнать: каждый пишет боту /id)
COURIERS = {7829091241}

# Получатель: имя -> Telegram ID (должен нажать /start у бота)
RECEIVERS = {"Магазин": 7135485514}

# Адрес страницы сканера (https!). Пока пусто, работает сканирование камерой телефона.
WEBAPP_URL = "https://cheerful-creponne-c8d912.netlify.app/"
# =====================

carts = {}
shipments = {}
counter = itertools.count(1)


def cart_keyboard(cart):
    rows = [[InlineKeyboardButton(f"📦 Отправить ({len(cart)}) → {name}", callback_data=f"send:{name}")]
            for name in RECEIVERS]
    rows.append([InlineKeyboardButton("🗑 Очистить", callback_data="clear")])
    return InlineKeyboardMarkup(rows)


def courier_menu():
    if WEBAPP_URL.startswith("https://"):
        return ReplyKeyboardMarkup(
            [[KeyboardButton("📷 Сканировать", web_app=WebAppInfo(url=WEBAPP_URL))]],
            resize_keyboard=True)
    return None


def shipment_text(sid, s):
    lines = [f"{'✅' if ok else '⏳'} {code}" for code, ok in s["items"].items()]
    done = all(s["items"].values())
    head = f"📦 Отправка №{sid}" + (" — ВСЁ ПРИНЯТО ✅" if done else " — подтвердите получение")
    return head + "\n\n" + "\n".join(lines)


def shipment_keyboard(sid, s):
    pending = [c for c, ok in s["items"].items() if not ok]
    rows = [[InlineKeyboardButton(f"✅ Принять {c}", callback_data=f"acc:{sid}:{c}")] for c in pending]
    if len(pending) > 1:
        rows.append([InlineKeyboardButton("✅✅ Принять все", callback_data=f"all:{sid}")])
    return InlineKeyboardMarkup(rows) if rows else None


async def create_shipment(context, courier_id, items, name):
    """Создаёт отправку и шлёт уведомление получателю. Возвращает номер или None."""
    sid = next(counter)
    s = {"courier": courier_id, "receiver": RECEIVERS[name], "items": {c: False for c in items}}
    try:
        await context.bot.send_message(s["receiver"], shipment_text(sid, s),
                                       reply_markup=shipment_keyboard(sid, s))
    except Exception:
        return None
    shipments[sid] = s
    return sid


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    args = context.args

    # Курьер открыл ссылку из QR
    if args and args[0].startswith("item_"):
        if uid not in COURIERS:
            await update.message.reply_text("⛔ Сканировать могут только курьеры.")
            return
        code = args[0]
        cart = carts.setdefault(uid, [])
        if code in cart:
            await update.message.reply_text(f"⚠️ {code} уже добавлен.", reply_markup=cart_keyboard(cart))
            return
        cart.append(code)
        text = f"✅ Добавлен: {code}\n\nВ списке ({len(cart)}):\n" + "\n".join(f"• {c}" for c in cart)
        await update.message.reply_text(text, reply_markup=cart_keyboard(cart))
        return

    if uid in COURIERS:
        text = ("🚚 Вы курьер.\n\nНажмите «📷 Сканировать» внизу и наведите камеру на QR товара."
                if courier_menu() else
                "🚚 Вы курьер.\n\nСканируйте QR на товарах камерой телефона.\nДля проверки: /add item_001")
        await update.message.reply_text(text, reply_markup=courier_menu())
    elif uid in RECEIVERS.values():
        await update.message.reply_text("🏪 Вы магазин.\n\nСюда придут уведомления об отправках. "
                                        "Нажимайте «Принять» у товара или «Принять все».")
    else:
        await update.message.reply_text(
            f"Ваш ID: {uid}\n\nЭтот ID не добавлен в настройки бота (COURIERS или RECEIVERS в bot.py).")


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Напишите так: /add item_001")
        return
    code = context.args[0].lower()
    if not code.startswith("item_"):
        code = "item_" + code
    context.args[0] = code
    await start(update, context)


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Ваш ID: {update.effective_user.id}")


async def webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Данные из мини-страницы сканера: список товаров."""
    uid = update.effective_user.id
    if uid not in COURIERS:
        return
    try:
        items = list(dict.fromkeys(json.loads(update.message.web_app_data.data)["items"]))
    except Exception:
        await update.message.reply_text("⚠️ Не удалось прочитать данные сканера.")
        return
    if not items:
        return
    name = next(iter(RECEIVERS))
    sid = await create_shipment(context, uid, items, name)
    if sid is None:
        await update.message.reply_text("⚠️ Не удалось отправить. Магазин должен нажать /start у бота.")
    else:
        await update.message.reply_text(f"🚚 Отправка №{sid} → {name}\nТоваров: {len(items)}\nЖдём подтверждения.")


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data

    if data == "clear":
        carts[uid] = []
        await q.answer("Список очищен")
        await q.edit_message_text("Список пуст. Сканируйте QR-коды.")
        return

    if data.startswith("send:"):
        name = data.split(":", 1)[1]
        cart = carts.get(uid, [])
        if not cart or name not in RECEIVERS:
            await q.answer("Список пуст", show_alert=True)
            return
        sid = await create_shipment(context, uid, cart, name)
        if sid is None:
            await q.answer("Не удалось отправить. Магазин должен нажать /start у бота.", show_alert=True)
            return
        count = len(cart)
        carts[uid] = []
        await q.answer("Отправлено")
        await q.edit_message_text(f"🚚 Отправка №{sid} → {name}\nТоваров: {count}\nЖдём подтверждения.")
        return

    if data.startswith("acc:") or data.startswith("all:"):
        parts = data.split(":")
        sid = int(parts[1])
        s = shipments.get(sid)
        if not s or uid != s["receiver"]:
            await q.answer("Нет доступа", show_alert=True)
            return
        if parts[0] == "acc":
            code = parts[2]
            accepted = []
            if code in s["items"] and not s["items"][code]:
                s["items"][code] = True
                accepted = [code]
        else:
            accepted = [c for c, ok in s["items"].items() if not ok]
            for c in accepted:
                s["items"][c] = True

        await q.answer("Принято ✅")
        await q.edit_message_text(shipment_text(sid, s), reply_markup=shipment_keyboard(sid, s))
        if accepted:
            msg = f"✅ {q.from_user.full_name} принял(а) по отправке №{sid}: " + ", ".join(accepted)
            if all(s["items"].values()):
                msg += "\n🎉 Все товары приняты."
            await context.bot.send_message(s["courier"], msg)


class Ping(BaseHTTPRequestHandler):
    """Мини-сервер для Render: отвечает «ok», чтобы сервис не засыпал."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    do_HEAD = do_GET

    def log_message(self, *args):
        pass


def keep_alive():
    HTTPServer(("0.0.0.0", int(os.getenv("PORT"))), Ping).serve_forever()


def main():
    if os.getenv("PORT"):  # на Render есть PORT, на вашем компьютере нет
        threading.Thread(target=keep_alive, daemon=True).start()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", my_id))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data))
    app.add_handler(CallbackQueryHandler(buttons))
    asyncio.set_event_loop(asyncio.new_event_loop())  # нужно для Python 3.14
    app.run_polling()


if __name__ == "__main__":
    main()
