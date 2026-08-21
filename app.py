import asyncio
import hmac
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
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK")
PLATEGA_MERCHANT_ID = os.getenv("PLATEGA_MERCHANT_ID")
PLATEGA_SECRET = os.getenv("PLATEGA_SECRET")
PLATEGA_API_URL = os.getenv("PLATEGA_API_URL", "https://app.platega.io").rstrip("/")
PLATEGA_CALLBACK_PATH = os.getenv("PLATEGA_CALLBACK_PATH", "/platega/callback")
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
BASE_DIR = Path(__file__).resolve().parent
PRIVACY_POLICY_FILE = BASE_DIR / "legal" / "privacy_policy.txt"
USER_AGREEMENT_FILE = BASE_DIR / "legal" / "user_agreement.txt"
TELEGRAM_TEXT_LIMIT = 4000
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-08-18-100"
USER_AGREEMENT_URL = "https://telegra.ph/polzovatelskoe-soglashenie-08-18-42"


def load_static_text(path: Path, fallback_text: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logging.exception("Не удалось прочитать файл %s", path)
        return fallback_text


def split_text_for_telegram(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    pages: list[str] = []
    current_parts: list[str] = []
    current_length = 0

    for paragraph in text.splitlines():
        paragraph = paragraph.rstrip()

        if not paragraph:
            candidate = "\n"
            if current_length + len(candidate) > limit and current_parts:
                pages.append("".join(current_parts).strip())
                current_parts = []
                current_length = 0
            current_parts.append(candidate)
            current_length += len(candidate)
            continue

        candidate = f"{paragraph}\n"
        if len(candidate) > limit:
            if current_parts:
                pages.append("".join(current_parts).strip())
                current_parts = []
                current_length = 0

            for start in range(0, len(paragraph), limit):
                pages.append(paragraph[start : start + limit].strip())
            continue

        if current_length + len(candidate) > limit and current_parts:
            pages.append("".join(current_parts).strip())
            current_parts = []
            current_length = 0

        current_parts.append(candidate)
        current_length += len(candidate)

    if current_parts:
        pages.append("".join(current_parts).strip())

    return pages or [text[:limit]]

SUPPORT_TEXT = (
    "Если у вас возникли проблемы, можете написать сюда:\n"
    f"{SUPPORT_USERNAME}\n\n"
    "Обычно отвечают в течение 24 часов."
)

WELCOME_TEXT = (
    "PLAT\n\n"
    "Всем приветик\n\n"
    "Это бот для покупки доступа в приватный канал (18+).\n\n"
    "Выбирай удобный тариф, оплачивай и после подтверждения оплаты бот выдаст ссылку на канал."
)

PRIVACY_POLICY_TEXT = (
    "Политика конфиденциальности:\n"
    f"{PRIVACY_POLICY_URL}"
)

USER_AGREEMENT_TEXT = (
    "Пользовательское соглашение:\n"
    f"{USER_AGREEMENT_URL}"
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
        [InlineKeyboardButton("Политика конфиденциальности", callback_data="privacy_policy")],
        [InlineKeyboardButton("Пользовательское соглашение", callback_data="user_agreement")],
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


def build_invoice_text(tariff: dict, invoice_id: str) -> str:
    return (
        f"Тариф: {tariff['label']}\n"
        f"Сумма: {tariff['amount_rub']} RUB\n"
        "Статус: ожидает оплату\n\n"
        "1. Нажми кнопку «Оплатить».\n"
        "2. После оплаты вернись в бота.\n"
        "3. Нажми «Проверить оплату», чтобы получить доступ.\n\n"
        f"Счет №{invoice_id[:8]} действует примерно 15 минут."
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


BOT_USERNAME: str | None = None


async def cache_bot_username(bot) -> None:
    global BOT_USERNAME
    try:
        me = await bot.get_me()
        BOT_USERNAME = me.username
    except Exception:
        logging.exception("Не удалось получить username бота")


def get_return_url() -> str:
    configured = os.getenv("PLATEGA_RETURN_URL")
    if configured:
        return configured
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}"
    return "https://t.me"


def get_failed_url() -> str:
    return os.getenv("PLATEGA_FAILED_URL") or get_return_url()


async def platega_api_request(http_method: str, path: str, payload: dict | None = None) -> dict:
    if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
        raise RuntimeError("PLATEGA_MERCHANT_ID / PLATEGA_SECRET не найдены в .env")

    headers = {"X-MerchantId": PLATEGA_MERCHANT_ID, "X-Secret": PLATEGA_SECRET}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(
            http_method,
            f"{PLATEGA_API_URL}{path}",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()

    return response.json()


async def create_transaction(tariff_key: str, user_id: int, user_name: str) -> dict:
    tariff = TARIFFS[tariff_key]
    payload = {
        "paymentDetails": {
            "amount": float(tariff["amount_rub"]),
            "currency": "RUB",
        },
        "description": f"Доступ в канал: {tariff['label']}",
        "return": get_return_url(),
        "failedUrl": get_failed_url(),
        "payload": json.dumps({"user_id": user_id, "tariff_key": tariff_key}, ensure_ascii=False),
        "metadata": {"userId": str(user_id), "userName": user_name},
    }

    return await platega_api_request("POST", "/v2/transaction/process", payload)


async def get_transaction(transaction_id: str) -> dict | None:
    try:
        return await platega_api_request("GET", f"/transaction/{transaction_id}")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return None
        raise


async def show_main_menu(query) -> None:
    await query.edit_message_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def show_tariffs(query) -> None:
    await query.edit_message_text(TARIFFS_TEXT, reply_markup=tariff_menu_keyboard())


async def show_support(query) -> None:
    await query.edit_message_text(SUPPORT_TEXT, reply_markup=back_to_main_menu_keyboard())


async def show_privacy_policy(query) -> None:
    await query.edit_message_text(
        PRIVACY_POLICY_TEXT,
        reply_markup=back_to_main_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def show_user_agreement(query) -> None:
    await query.edit_message_text(
        USER_AGREEMENT_TEXT,
        reply_markup=back_to_main_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def handle_tariff_selection(query, tariff_key: str) -> None:
    tariff = TARIFFS[tariff_key]
    user = query.from_user
    user_name = f"@{user.username}" if user.username else user.full_name

    try:
        transaction = await create_transaction(tariff_key, user.id, user_name)
    except Exception:
        logging.exception("Не удалось создать транзакцию Platega")
        await query.edit_message_text(
            "Не удалось создать счет. Попробуй еще раз чуть позже или напиши в поддержку.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    invoice_id = str(transaction["transactionId"])
    pay_url = transaction["url"]

    upsert_payment(
        invoice_id,
        user_id=user.id,
        tariff_key=tariff_key,
        pay_url=pay_url,
        status=transaction.get("status", "PENDING"),
        expires_in=transaction.get("expiresIn"),
        delivered=False,
    )

    await query.edit_message_text(
        build_invoice_text(tariff, invoice_id),
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
        transaction = await get_transaction(invoice_id)
    except Exception:
        logging.exception("Не удалось проверить транзакцию Platega")
        await query.answer("Не удалось проверить оплату. Попробуй еще раз.", show_alert=True)
        return True

    if not transaction:
        await query.answer("Счет не найден в Platega.", show_alert=True)
        return True

    tariff = TARIFFS[payment["tariff_key"]]
    status = transaction.get("status", "PENDING")

    upsert_payment(invoice_id, status=status)

    if status == "CONFIRMED":
        if not payment.get("delivered"):
            upsert_payment(invoice_id, delivered=True, delivery_link=PAID_CHANNEL_LINK or "")
        await query.edit_message_text(
            build_paid_text(tariff),
            reply_markup=paid_keyboard(),
            disable_web_page_preview=True,
        )
        return False

    if status in {"CANCELED", "CHARGEBACKED"}:
        await query.edit_message_text(
            build_expired_text(tariff),
            reply_markup=expired_invoice_keyboard(payment["tariff_key"]),
        )
        return False

    await query.edit_message_text(
        build_invoice_text(tariff, invoice_id),
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
        elif data == "privacy_policy":
            await show_privacy_policy(query)
        elif data == "user_agreement":
            await show_user_agreement(query)
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
    platega_callback_path = normalize_path(PLATEGA_CALLBACK_PATH, "platega/callback")
    public_webhook_url = f"{WEBHOOK_URL.rstrip('/')}{webhook_path}"
    platega_callback_url = f"{WEBHOOK_URL.rstrip('/')}{platega_callback_path}"
    healthcheck_url = f"{WEBHOOK_URL.rstrip('/')}{healthcheck_path}"
    app = build_application(manual_webhook=True)

    async def telegram_webhook(request: Request) -> Response:
        update = Update.de_json(data=await request.json(), bot=app.bot)
        await app.update_queue.put(update)
        return Response(status_code=200)

    async def platega_callback(request: Request) -> JSONResponse:
        merchant_id = request.headers.get("X-MerchantId") or ""
        secret = request.headers.get("X-Secret") or ""
        expected_id = PLATEGA_MERCHANT_ID or ""
        expected_secret = PLATEGA_SECRET or ""
        if not (
            merchant_id
            and secret
            and hmac.compare_digest(merchant_id, expected_id)
            and hmac.compare_digest(secret, expected_secret)
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=403)

        try:
            callback = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        invoice_id = str(callback.get("id", ""))
        status = callback.get("status")
        payment = get_payment(invoice_id)

        if not payment:
            return JSONResponse({"ok": True})

        upsert_payment(
            invoice_id,
            status=status,
            paid_amount=callback.get("amount"),
            paid_currency=callback.get("currency"),
        )

        if status == "CONFIRMED" and not payment.get("delivered"):
            tariff = TARIFFS.get(payment.get("tariff_key", ""))
            chat_id = payment.get("user_id")

            if tariff and chat_id:
                upsert_payment(invoice_id, delivered=True, delivery_link=PAID_CHANNEL_LINK or "")
                try:
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=build_paid_text(tariff),
                        reply_markup=paid_keyboard(),
                    )
                except Exception:
                    logging.exception("Не удалось отправить сообщение об оплате")
                    upsert_payment(invoice_id, delivered=False)

        return JSONResponse({"ok": True})

    async def healthcheck(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    web_app = Starlette(
        routes=[
            Route(webhook_path, telegram_webhook, methods=["POST"]),
            Route(platega_callback_path, platega_callback, methods=["POST"]),
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
        await cache_bot_username(app.bot)
        await app.bot.set_webhook(
            url=public_webhook_url,
            allowed_updates=Update.ALL_TYPES,
        )
        logging.info("Bot is running in webhook mode on port %s", PORT)
        logging.info("Webhook URL: %s", public_webhook_url)
        logging.info("Platega callback URL: %s", platega_callback_url)
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


async def post_init(application: Application) -> None:
    await cache_bot_username(application.bot)


def main() -> None:
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN не найден в .env")
        return

    if not PLATEGA_MERCHANT_ID or not PLATEGA_SECRET:
        logging.error("PLATEGA_MERCHANT_ID / PLATEGA_SECRET не найдены в .env")
        return

    logging.basicConfig(level=logging.INFO)

    if WEBHOOK_URL:
        asyncio.run(run_manual_webhook())
        return

    logging.info("WEBHOOK_URL не задан. Запускаю бота в polling mode.")
    logging.info("Callback от Platega недоступен без WEBHOOK_URL, оплата подтверждается кнопкой «Проверить оплату».")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES, post_init=post_init)


if __name__ == "__main__":
    main()
