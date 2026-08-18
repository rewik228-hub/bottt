import asyncio
import json
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
import uvicorn

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/telegram-webhook")
HEALTHCHECK_PATH = os.getenv("HEALTHCHECK_PATH", "/healthz")
PORT = int(os.getenv("PORT", "8000"))
SELF_PING_ENABLED = os.getenv("SELF_PING_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
SELF_PING_INTERVAL_SECONDS = int(os.getenv("SELF_PING_INTERVAL_SECONDS", "840"))
SUPPORT_USERNAME = "@volot543"
PAYMENTS_FILE = Path("payments.json")

SUPPORT_TEXT = (
    "Если у вас возникли проблемы, можете написать сюда:\n"
    f"{SUPPORT_USERNAME}\n\n"
    "Обычно отвечают в течение 24 часов."
)

WELCOME_TEXT = (
    "Всем приветик\n\n"
    "Это бот для покупки доступа в приватный канал (18+).\n\n"
    "Выбирай удобный тариф, оплачивай и после подтверждения оплаты бот выдаст ссылку на канал."
)

TARIFFS = {
    "tariff_week": {
        "label": "1 неделя",
        "amount_rub": "700",
        "description": "Попробовать и оценить",
        "success_title": "Оплата тарифа на 1 неделю подтверждена.",
    },
    "tariff_month": {
        "label": "Месяц",
        "amount_rub": "1200",
        "description": "Самый оптимальный вариант",
        "success_title": "Оплата тарифа на месяц подтверждена.",
    },
    "tariff_forever": {
        "label": "Навсегда",
        "amount_rub": "2700",
        "description": "Один раз оплатил - доступ навсегда",
        "success_title": "Оплата тарифа навсегда подтверждена.",
    },
}

TARIFFS_TEXT = (
    "Выбери удобную подписку:\n\n"
    "1 неделя - 700 RUB (попробовать и оценить)\n\n"
    "Месяц - 1200 RUB (самый оптимальный вариант)\n\n"
    "Навсегда - 2700 RUB (один раз оплатил - доступ навсегда)"
)


def load_payments() -> dict[str, dict]:
    if not PAYMENTS_FILE.exists():
        return {}

    try:
        return json.loads(PAYMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("payments.json поврежден, создаю новое хранилище.")
        return {}


def save_payments(payments: dict[str, dict]) -> None:
    PAYMENTS_FILE.write_text(
        json.dumps(payments, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_payment(invoice_id: str) -> dict | None:
    return load_payments().get(str(invoice_id))


def upsert_payment(invoice_id: str, **changes: object) -> dict:
    payments = load_payments()
    payment = payments.get(str(invoice_id), {})
    payment.update(changes)
    payments[str(invoice_id)] = payment
    save_payments(payments)
    return payment


def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Купить доступ", callback_data="buy")],
        [InlineKeyboardButton("Поддержка", callback_data="support")],
    ]
    return InlineKeyboardMarkup(keyboard)


def tariff_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("1 неделя", callback_data="tariff_week")],
        [InlineKeyboardButton("Месяц", callback_data="tariff_month")],
        [InlineKeyboardButton("Навсегда", callback_data="tariff_forever")],
        [InlineKeyboardButton("◀ Назад", callback_data="main_menu")],
    ]
    return InlineKeyboardMarkup(keyboard)


def back_to_main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("◀ Назад", callback_data="main_menu")]]
    )


def invoice_keyboard(pay_url: str, invoice_id: str) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Оплатить", url=pay_url)],
        [InlineKeyboardButton("Проверить оплату", callback_data=f"check_invoice:{invoice_id}")],
        [InlineKeyboardButton("◀ Назад", callback_data="buy")],
    ]
    return InlineKeyboardMarkup(keyboard)


def paid_keyboard() -> InlineKeyboardMarkup:
    keyboard = []

    if PAID_CHANNEL_LINK:
        keyboard.append([InlineKeyboardButton("Открыть канал", url=PAID_CHANNEL_LINK)])

    keyboard.append([InlineKeyboardButton("◀ Главное меню", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)


def expired_invoice_keyboard(tariff_key: str) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Создать новый счет", callback_data=tariff_key)],
        [InlineKeyboardButton("◀ Назад", callback_data="buy")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_invoice_text(tariff: dict, invoice: dict) -> str:
    return (
        f"Тариф: {tariff['label']}\n"
        f"Сумма: {tariff['amount_rub']} RUB\n"
        "Статус: ожидает оплату\n\n"
        "1. Нажми кнопку «Оплатить».\n"
        "2. После оплаты вернись в бота.\n"
        "3. Нажми «Проверить оплату», чтобы получить доступ.\n\n"
        f"Счет №{invoice['invoice_id']} действует примерно 1 час."
    )


def build_paid_text(tariff: dict) -> str:
    base_text = (
        f"{tariff['success_title']}\n\n"
        "Доступ выдан. Ссылка на канал:\n"
    )

    if PAID_CHANNEL_LINK:
        return (
            f"{base_text}{PAID_CHANNEL_LINK}\n\n"
            "Если со ссылкой возникнут проблемы, напиши в поддержку."
        )

    return (
        f"{tariff['success_title']}\n\n"
        "Оплата прошла успешно, но ссылка для выдачи не настроена. "
        "Напиши в поддержку, чтобы получить доступ."
    )


def build_expired_text(tariff: dict) -> str:
    return (
        f"Счет для тарифа «{tariff['label']}» истек.\n\n"
        "Нажми кнопку ниже, чтобы создать новый счет."
    )


async def crypto_api_request(method: str, payload: dict | None = None) -> object:
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("CRYPTO_PAY_TOKEN не найден в .env")

    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(
            f"{CRYPTO_PAY_API_URL}/{method}",
            headers=headers,
            json=payload or {},
        )
        response.raise_for_status()

    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "Неизвестная ошибка Crypto Pay API"))

    return data["result"]


async def create_invoice(tariff_key: str, user_id: int) -> dict:
    tariff = TARIFFS[tariff_key]
    payload = {
        "currency_type": "fiat",
        "fiat": "RUB",
        "accepted_assets": "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC",
        "amount": tariff["amount_rub"],
        "description": f"Доступ в канал: {tariff['label']}",
        "hidden_message": "Оплата получена. Вернитесь в бота и нажмите «Проверить оплату».",
        "payload": json.dumps({"user_id": user_id, "tariff_key": tariff_key}, ensure_ascii=False),
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    }

    result = await crypto_api_request("createInvoice", payload)
    return result


async def get_invoice(invoice_id: str) -> dict | None:
    result = await crypto_api_request("getInvoices", {"invoice_ids": str(invoice_id)})
    if not isinstance(result, list) or not result:
        return None
    return result[0]


async def show_main_menu(query) -> None:
    await query.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def show_tariffs(query) -> None:
    await query.edit_message_text(TARIFFS_TEXT, reply_markup=tariff_menu_keyboard())


async def show_support(query) -> None:
    await query.edit_message_text(SUPPORT_TEXT, reply_markup=back_to_main_menu_keyboard())


async def handle_tariff_selection(query, tariff_key: str) -> None:
    tariff = TARIFFS[tariff_key]

    try:
        invoice = await create_invoice(tariff_key, query.from_user.id)
    except Exception:
        logging.exception("Не удалось создать счет Crypto Bot")
        await query.edit_message_text(
            "Не удалось создать счет. Попробуй еще раз чуть позже или напиши в поддержку.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    invoice_id = str(invoice["invoice_id"])
    pay_url = invoice["bot_invoice_url"]

    upsert_payment(
        invoice_id,
        user_id=query.from_user.id,
        tariff_key=tariff_key,
        pay_url=pay_url,
        status=invoice.get("status", "active"),
        created_at=invoice.get("created_at"),
        expiration_date=invoice.get("expiration_date"),
        delivered=False,
    )

    await query.edit_message_text(
        build_invoice_text(tariff, invoice),
        reply_markup=invoice_keyboard(pay_url, invoice_id),
        disable_web_page_preview=True,
    )


async def handle_invoice_check(query) -> bool:
    invoice_id = query.data.split(":", maxsplit=1)[1]
    payment = get_payment(invoice_id)

    if not payment:
        await query.answer("Счет не найден. Создай новый счет.", show_alert=True)
        return True

    if payment.get("user_id") != query.from_user.id:
        await query.answer("Этот счет принадлежит другому пользователю.", show_alert=True)
        return True

    try:
        invoice = await get_invoice(invoice_id)
    except Exception:
        logging.exception("Не удалось проверить счет Crypto Bot")
        await query.answer("Не удалось проверить оплату. Попробуй еще раз.", show_alert=True)
        return True

    if not invoice:
        await query.answer("Счет не найден в Crypto Bot.", show_alert=True)
        return True

    tariff = TARIFFS[payment["tariff_key"]]
    status = invoice.get("status", "active")

    upsert_payment(
        invoice_id,
        status=status,
        paid_at=invoice.get("paid_at"),
        paid_asset=invoice.get("paid_asset"),
        paid_amount=invoice.get("paid_amount"),
    )

    if status == "paid":
        upsert_payment(invoice_id, delivered=True, delivery_link=PAID_CHANNEL_LINK or "")
        await query.edit_message_text(
            build_paid_text(tariff),
            reply_markup=paid_keyboard(),
            disable_web_page_preview=True,
        )
        return False

    if status == "expired":
        await query.edit_message_text(
            build_expired_text(tariff),
            reply_markup=expired_invoice_keyboard(payment["tariff_key"]),
        )
        return False

    await query.edit_message_text(
        build_invoice_text(tariff, invoice),
        reply_markup=invoice_keyboard(payment["pay_url"], invoice_id),
        disable_web_page_preview=True,
    )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    already_answered = False

    try:
        if data == "main_menu":
            await show_main_menu(query)
        elif data == "buy":
            await show_tariffs(query)
        elif data == "support":
            await show_support(query)
        elif data in TARIFFS:
            await handle_tariff_selection(query, data)
        elif data.startswith("check_invoice:"):
            already_answered = await handle_invoice_check(query)
        else:
            await query.answer("Неизвестная команда.", show_alert=True)
            return
    except Exception:
        logging.exception("Ошибка в обработчике кнопок")
        await query.answer("Что-то пошло не так. Попробуй еще раз.", show_alert=True)
        return

    if not already_answered:
        await query.answer()


def build_application(*, manual_webhook: bool = False) -> Application:
    builder = Application.builder().token(BOT_TOKEN)
    if manual_webhook:
        builder = builder.updater(None)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))
    return app


def normalize_path(path: str, default: str) -> str:
    cleaned = path.strip()
    if not cleaned:
        cleaned = default
    return "/" + cleaned.strip("/")


async def keep_service_awake(healthcheck_url: str) -> None:
    logging.info(
        "Self-ping включен: %s каждые %s секунд",
        healthcheck_url,
        SELF_PING_INTERVAL_SECONDS,
    )

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        while True:
            await asyncio.sleep(SELF_PING_INTERVAL_SECONDS)

            try:
                response = await client.get(healthcheck_url)
                logging.info("Self-ping выполнен, статус %s", response.status_code)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("Не удалось выполнить self-ping")


async def run_manual_webhook() -> None:
    webhook_path = normalize_path(WEBHOOK_PATH, "telegram-webhook")
    healthcheck_path = normalize_path(HEALTHCHECK_PATH, "healthz")
    public_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"
    healthcheck_url = f"{WEBHOOK_URL.rstrip('/')}{healthcheck_path}"
    app = build_application(manual_webhook=True)

    async def telegram_webhook(request: Request) -> Response:
        update = Update.de_json(data=await request.json(), bot=app.bot)
        await app.update_queue.put(update)
        return Response(status_code=200)

    async def healthcheck(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    web_app = Starlette(
        routes=[
            Route(webhook_path, telegram_webhook, methods=["POST"]),
            Route(healthcheck_path, healthcheck, methods=["GET"]),
        ]
    )
    server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host="0.0.0.0",
            port=PORT,
            log_level="info",
        )
    )

    async with app:
        await app.start()
        await app.bot.set_webhook(
            url=public_webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
        logging.info("Bot is running in webhook mode on port %s", PORT)
        logging.info("Webhook URL: %s", public_webhook_url)
        logging.info("Healthcheck URL: %s", healthcheck_url)

        keepalive_task = None
        if SELF_PING_ENABLED:
            keepalive_task = asyncio.create_task(keep_service_awake(healthcheck_url))

        try:
            await server.serve()
        finally:
            if keepalive_task:
                keepalive_task.cancel()
                try:
                    await keepalive_task
                except asyncio.CancelledError:
                    pass
            await app.stop()


def main() -> None:
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN не найден в .env")
        return

    if not CRYPTO_PAY_TOKEN:
        logging.error("CRYPTO_PAY_TOKEN не найден в .env")
        return

    logging.basicConfig(level=logging.INFO)

    if WEBHOOK_URL:
        asyncio.run(run_manual_webhook())
        return

    logging.info("WEBHOOK_URL не задан. Запускаю бота в polling mode.")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
