import asyncio
import hmac
import json
import logging
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
)
import uvicorn

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK")
PAID_CHAT_ID_RAW = os.getenv("PAID_CHAT_ID", "").strip()
PAID_CHAT_ID = int(PAID_CHAT_ID_RAW) if PAID_CHAT_ID_RAW else None
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
ACCESS_CHECK_INTERVAL_SECONDS = int(os.getenv("ACCESS_CHECK_INTERVAL_SECONDS", "300"))
SUPPORT_USERNAME = "@volot543"
PAYMENTS_FILE = Path("payments.json")
BASE_DIR = Path(__file__).resolve().parent
PRIVACY_POLICY_FILE = BASE_DIR / "legal" / "privacy_policy.txt"
USER_AGREEMENT_FILE = BASE_DIR / "legal" / "user_agreement.txt"
TELEGRAM_TEXT_LIMIT = 4000
PRIVACY_POLICY_URL = "https://telegra.ph/Politika-konfidencialnosti-08-18-100"
USER_AGREEMENT_URL = "https://telegra.ph/polzovatelskoe-soglashenie-08-18-42"
try:
    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    MOSCOW_TZ = timezone(timedelta(hours=3), name="MSK")
ACCESS_LOOP_TASK_KEY = "access_loop_task"


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
    "Всем приветик\n\n"
    "Это бот для покупки доступа в приватный канал (18+).\n\n"
    "Выбирай удобный тариф, оплачивай и после подтверждения оплаты бот выдаст ссылку на канал."
)

DOCUMENTS_TEXT = (
    "Политика конфиденциальности:\n"
    f"{PRIVACY_POLICY_URL}\n\n"
    "Пользовательское соглашение:\n"
    f"{USER_AGREEMENT_URL}"
)

TARIFFS = {
    "tariff_day": {
        "label": "1 день",
        "amount_rub": "350",
        "description": "Подписка на 1 день",
        "success_title": "Подписка на 1 день активирована.",
        "interval_days": 1,
        "recurring": True,
    },
    "tariff_week": {
        "label": "1 неделя",
        "amount_rub": "700",
        "description": "Подписка на 7 дней",
        "success_title": "Подписка на 7 дней активирована.",
        "interval_days": 7,
        "recurring": True,
    },
    "tariff_month": {
        "label": "Месяц",
        "amount_rub": "1200",
        "description": "Подписка на 1 месяц",
        "success_title": "Подписка на месяц активирована.",
        "interval_days": 30,
        "recurring": True,
    },
    "tariff_forever": {
        "label": "Навсегда",
        "amount_rub": "2700",
        "description": "Подписка навсегда",
        "success_title": "Оплата тарифа навсегда подтверждена.",
        "recurring": False,
    },
}

TARIFFS_TEXT = (
    "Выбери удобную подписку:\n\n"
    "1 день - 350 RUB \n\n"
    "1 неделя - 700 RUB \n\n"
    "Месяц - 1200 RUB \n\n"
    "Навсегда - 2700 RUB"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None

    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def format_datetime_local(value: datetime | None) -> str:
    if value is None:
        return "навсегда"
    return value.astimezone(MOSCOW_TZ).strftime("%d.%m.%Y %H:%M МСК")


def load_storage() -> dict[str, dict[str, dict]]:
    if not PAYMENTS_FILE.exists():
        return {"payments": {}, "access": {}}

    try:
        raw_data = json.loads(PAYMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("payments.json поврежден, создаю новое хранилище.")
        return {"payments": {}, "access": {}}

    if not isinstance(raw_data, dict):
        return {"payments": {}, "access": {}}

    payments = raw_data.get("payments")
    access = raw_data.get("access")

    if isinstance(payments, dict) or isinstance(access, dict):
        return {
            "payments": payments if isinstance(payments, dict) else {},
            "access": access if isinstance(access, dict) else {},
        }

    return {"payments": raw_data, "access": {}}


def save_storage(storage: dict[str, dict[str, dict]]) -> None:
    PAYMENTS_FILE.write_text(
        json.dumps(storage, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_payments() -> dict[str, dict]:
    return load_storage()["payments"]


def save_payments(payments: dict[str, dict]) -> None:
    storage = load_storage()
    storage["payments"] = payments
    save_storage(storage)


def load_access_records() -> dict[str, dict]:
    return load_storage()["access"]


def save_access_records(access_records: dict[str, dict]) -> None:
    storage = load_storage()
    storage["access"] = access_records
    save_storage(storage)


def get_payment(invoice_id: str) -> dict | None:
    return load_payments().get(str(invoice_id))


def upsert_payment(invoice_id: str, **changes: object) -> dict:
    payments = load_payments()
    payment = payments.get(str(invoice_id), {})
    payment.update(changes)
    payments[str(invoice_id)] = payment
    save_payments(payments)
    return payment


def get_access_record(user_id: int) -> dict | None:
    return load_access_records().get(str(user_id))


def upsert_access_record(user_id: int, **changes: object) -> dict:
    access_records = load_access_records()
    access_record = access_records.get(str(user_id), {})
    access_record.update(changes)
    access_records[str(user_id)] = access_record
    save_access_records(access_records)
    return access_record


def get_access_expires_at(access_record: dict | None) -> datetime | None:
    if not access_record:
        return None
    return parse_datetime(access_record.get("expires_at"))


def has_active_access(access_record: dict | None, *, now: datetime | None = None) -> bool:
    if not access_record or not access_record.get("active"):
        return False

    expires_at = get_access_expires_at(access_record)
    if expires_at is None:
        return True

    return expires_at > (now or utc_now())


def access_status_text(access_record: dict | None) -> str:
    now = utc_now()
    if not access_record:
        return "У тебя пока нет активной подписки."

    if has_active_access(access_record, now=now):
        expires_at = get_access_expires_at(access_record)
        membership_text = "Ты уже в приватке." if access_record.get("is_member") else "Ожидается твоя заявка на вступление."
        return (
            "Подписка активна.\n"
            f"Доступ действует до: {format_datetime_local(expires_at)}\n"
            f"{membership_text}"
        )

    expires_at = get_access_expires_at(access_record)
    if expires_at is None:
        return "Подписка оформлена, но статус доступа не удалось определить."

    return (
        "Подписка неактивна.\n"
        f"Срок доступа закончился: {format_datetime_local(expires_at)}"
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("Купить доступ", callback_data="buy")],
        [InlineKeyboardButton("Мой доступ", callback_data="access_status")],
        [InlineKeyboardButton("Документы", callback_data="documents")],
        [InlineKeyboardButton("Поддержка", callback_data="support")],
    ]
    return InlineKeyboardMarkup(keyboard)


def tariff_menu_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("1 день", callback_data="tariff_day")],
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


def paid_keyboard(access_record: dict | None = None) -> InlineKeyboardMarkup:
    keyboard = []

    invite_link = access_record.get("invite_link") if access_record else None
    if invite_link:
        keyboard.append([InlineKeyboardButton("Подать заявку в канал", url=invite_link)])
    elif PAID_CHANNEL_LINK:
        keyboard.append([InlineKeyboardButton("Открыть канал", url=PAID_CHANNEL_LINK)])

    keyboard.append([InlineKeyboardButton("Мой доступ", callback_data="access_status")])
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


def build_paid_text(tariff: dict, access_record: dict | None = None) -> str:
    lines = [tariff["success_title"], "", access_status_text(access_record)]

    invite_link = access_record.get("invite_link") if access_record else None
    if invite_link:
        lines.extend(
            [
                "",
                "Нажми на кнопку ниже и отправь заявку на вступление. Бот примет ее автоматически.",
                invite_link,
            ]
        )
    elif access_record and access_record.get("is_member"):
        lines.extend(["", "Ты уже находишься в приватке."])
    elif PAID_CHANNEL_LINK:
        lines.extend(["", "Если нужно открыть канал вручную, используй эту ссылку:", PAID_CHANNEL_LINK])
    else:
        lines.extend(
            [
                "",
                "Оплата прошла успешно, но ссылка для входа не настроена. Напиши в поддержку, чтобы получить доступ.",
            ]
        )

    return "\n".join(lines)


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


def build_platega_payload(tariff: dict, user_id: int, user_name: str, tariff_key: str) -> dict:
    payment_details: dict[str, object] = {
        "amount": float(tariff["amount_rub"]),
        "currency": "RUB",
    }
    payload: dict[str, object] = {
        "paymentDetails": payment_details,
        "description": f"Доступ в канал: {tariff['label']}",
        "return": get_return_url(),
        "failedUrl": get_failed_url(),
        "payload": json.dumps({"user_id": user_id, "tariff_key": tariff_key}, ensure_ascii=False),
        "metadata": {"userId": str(user_id), "userName": user_name},
    }

    if tariff.get("recurring"):
        payload["paymentMethod"] = 6
        payment_details["interval"] = tariff["interval_days"]

    return payload


def parse_callback_json(callback: dict) -> dict:
    payload = callback.get("payload") or callback.get("Payload") or ""
    if not isinstance(payload, str) or not payload.strip():
        return {}

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def find_payment_by_payload(callback_payload: dict) -> tuple[str | None, dict | None]:
    user_id = callback_payload.get("user_id")
    tariff_key = callback_payload.get("tariff_key")
    if user_id is None or tariff_key is None:
        return None, None

    user_id = int(user_id)
    payments = load_payments()
    for payment_id, payment in payments.items():
        if payment.get("user_id") == user_id and payment.get("tariff_key") == tariff_key:
            return payment_id, payment

    return None, None


def get_callback_value(callback: dict, *keys: str) -> object:
    for key in keys:
        if key in callback:
            return callback[key]
    return None


def normalize_callback_status(status: object) -> str:
    if not isinstance(status, str):
        return ""
    return status.strip().upper()


def resolve_callback_payment(callback: dict) -> tuple[str | None, dict | None]:
    callback_id = get_callback_value(callback, "id", "Id")
    if isinstance(callback_id, str) and callback_id:
        payment = get_payment(callback_id)
        if payment:
            return callback_id, payment

    callback_payload = parse_callback_json(callback)
    payment_id, payment = find_payment_by_payload(callback_payload)
    if payment_id and payment:
        return payment_id, payment

    subscription_id = get_callback_value(callback, "subscriptionId", "SubscriptionId")
    if isinstance(subscription_id, str) and subscription_id:
        payment = get_payment(subscription_id)
        if payment:
            return subscription_id, payment

    return None, None


def mirror_payment_alias(
    source_id: str,
    alias_id: str,
    payment: dict,
    *,
    reset_runtime_flags: bool = False,
) -> None:
    if source_id == alias_id:
        return

    alias_payment = dict(payment)
    alias_payment["subscription_id"] = alias_id
    alias_payment["origin_payment_id"] = source_id
    if reset_runtime_flags:
        alias_payment["access_applied"] = False
        alias_payment["notification_sent"] = False
        alias_payment["delivered"] = False
    upsert_payment(alias_id, **alias_payment)


def get_access_base_time(access_record: dict | None, now: datetime) -> datetime:
    if not access_record:
        return now

    expires_at = get_access_expires_at(access_record)
    if expires_at and expires_at > now:
        return expires_at

    return now


async def create_join_request_link(bot, user_id: int, expires_at: datetime | None) -> dict | None:
    if not PAID_CHAT_ID:
        return None

    invite = await bot.create_chat_invite_link(
        chat_id=PAID_CHAT_ID,
        expire_date=expires_at,
        creates_join_request=True,
        name=f"user-{user_id}",
    )
    return {
        "invite_link": invite.invite_link,
        "invite_link_expires_at": serialize_datetime(expires_at),
        "invite_link_name": invite.name,
    }


async def ensure_join_request_link(bot, user_id: int, access_record: dict) -> dict:
    if not PAID_CHAT_ID or access_record.get("is_member"):
        return access_record

    expires_at = get_access_expires_at(access_record)
    current_link = access_record.get("invite_link")
    current_link_expires_at = parse_datetime(access_record.get("invite_link_expires_at"))
    now = utc_now()

    if current_link and (current_link_expires_at is None or current_link_expires_at > now):
        return access_record

    invite_data = await create_join_request_link(bot, user_id, expires_at)
    if not invite_data:
        return access_record

    return upsert_access_record(
        user_id,
        **invite_data,
        last_invite_created_at=serialize_datetime(now),
    )


async def sync_paid_access(bot, payment_id: str) -> tuple[dict | None, dict | None]:
    payment = get_payment(payment_id)
    if not payment:
        return None, None

    tariff_key = payment.get("tariff_key")
    user_id = payment.get("user_id")
    if not isinstance(tariff_key, str) or tariff_key not in TARIFFS or not isinstance(user_id, int):
        return None, None

    tariff = TARIFFS[tariff_key]
    access_record = get_access_record(user_id)
    should_apply_access = not payment.get("access_applied") or not access_record

    if should_apply_access:
        now = utc_now()
        base_time = get_access_base_time(access_record, now)

        if tariff.get("recurring"):
            expires_at = base_time + timedelta(days=int(tariff["interval_days"]))
        else:
            expires_at = None

        access_record = upsert_access_record(
            user_id,
            active=True,
            tariff_key=tariff_key,
            source_payment_id=payment_id,
            activated_at=serialize_datetime(now),
            expires_at=serialize_datetime(expires_at),
            is_member=bool(access_record.get("is_member")) if access_record else False,
            removed_at=None,
            revoked_at=None,
        )
        upsert_payment(
            payment_id,
            access_applied=True,
            access_expires_at=serialize_datetime(expires_at),
        )

    refreshed_access_record = await ensure_join_request_link(bot, user_id, access_record or {})
    delivery_link = refreshed_access_record.get("invite_link") or PAID_CHANNEL_LINK or ""
    payment = upsert_payment(payment_id, delivered=bool(delivery_link), delivery_link=delivery_link)
    return payment, refreshed_access_record


async def revoke_expired_access(bot, user_id: int, access_record: dict) -> None:
    now = utc_now()

    if PAID_CHAT_ID:
        try:
            member = await bot.get_chat_member(PAID_CHAT_ID, user_id)
            if member.status not in {"left", "kicked"}:
                await bot.ban_chat_member(PAID_CHAT_ID, user_id)
                await bot.unban_chat_member(PAID_CHAT_ID, user_id, only_if_banned=True)
        except Exception:
            logging.exception("Не удалось исключить пользователя %s из приватки", user_id)

    upsert_access_record(
        user_id,
        active=False,
        is_member=False,
        revoked_at=serialize_datetime(now),
        invite_link=None,
        invite_link_expires_at=None,
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "Срок подписки закончился, доступ в приватку отключен.\n\n"
                "Если хочешь вернуться, оформи новую подписку в боте."
            ),
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление об окончании подписки")


async def process_expired_accesses(bot) -> None:
    now = utc_now()
    access_records = load_access_records()

    for raw_user_id, access_record in access_records.items():
        try:
            user_id = int(raw_user_id)
        except ValueError:
            continue

        expires_at = get_access_expires_at(access_record)
        if not access_record.get("active") or expires_at is None or expires_at > now:
            continue

        await revoke_expired_access(bot, user_id, access_record)


async def access_expiry_loop(bot) -> None:
    logging.info(
        "Проверка подписок включена: каждые %s секунд",
        ACCESS_CHECK_INTERVAL_SECONDS,
    )

    while True:
        try:
            await process_expired_accesses(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка при проверке истекших подписок")

        await asyncio.sleep(ACCESS_CHECK_INTERVAL_SECONDS)


async def start_access_loop(application: Application) -> None:
    if application.bot_data.get(ACCESS_LOOP_TASK_KEY):
        return

    application.bot_data[ACCESS_LOOP_TASK_KEY] = asyncio.create_task(
        access_expiry_loop(application.bot)
    )


async def stop_access_loop(application: Application) -> None:
    loop_task = application.bot_data.pop(ACCESS_LOOP_TASK_KEY, None)
    if not loop_task:
        return

    loop_task.cancel()
    try:
        await loop_task
    except asyncio.CancelledError:
        pass


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
    payload = build_platega_payload(tariff, user_id, user_name, tariff_key)
    return await platega_api_request("POST", "/v2/transaction/process", payload)


def extract_payment_url(transaction: dict) -> str:
    pay_url = transaction.get("url") or transaction.get("redirect")
    if not isinstance(pay_url, str) or not pay_url.strip():
        raise ValueError("Platega не вернула ссылку на оплату")
    return pay_url.strip()


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


async def show_documents(query) -> None:
    await query.edit_message_text(
        DOCUMENTS_TEXT,
        reply_markup=back_to_main_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def show_access_status(query) -> None:
    access_record = get_access_record(query.from_user.id)
    if access_record and has_active_access(access_record):
        access_record = await ensure_join_request_link(query.bot, query.from_user.id, access_record)

    await query.edit_message_text(
        access_status_text(access_record),
        reply_markup=paid_keyboard(access_record),
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
    pay_url = extract_payment_url(transaction)

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
        _, access_record = await sync_paid_access(query.bot, invoice_id)
        await query.edit_message_text(
            build_paid_text(tariff, access_record),
            reply_markup=paid_keyboard(access_record),
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


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    access_record = get_access_record(update.effective_user.id)
    if access_record and has_active_access(access_record):
        access_record = await ensure_join_request_link(context.bot, update.effective_user.id, access_record)

    await update.message.reply_text(
        access_status_text(access_record),
        reply_markup=paid_keyboard(access_record),
        disable_web_page_preview=True,
    )


async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request or not PAID_CHAT_ID or request.chat.id != PAID_CHAT_ID:
        return

    user_id = request.from_user.id
    access_record = get_access_record(user_id)

    if has_active_access(access_record):
        await context.bot.approve_chat_join_request(request.chat.id, user_id)
        upsert_access_record(
            user_id,
            is_member=True,
            joined_at=serialize_datetime(utc_now()),
            invite_link=None,
            invite_link_expires_at=None,
        )
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text="Заявка одобрена. Добро пожаловать в приватку.",
            )
        except Exception:
            logging.exception("Не удалось отправить подтверждение о вступлении")
        return

    await context.bot.decline_chat_join_request(request.chat.id, user_id)
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=(
                "Сейчас у тебя нет активной подписки, поэтому заявку отклонена.\n\n"
                "Оформи доступ в боте и подай заявку еще раз."
            ),
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logging.exception("Не удалось отправить сообщение об отклонении заявки")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    already_answered = False

    try:
        if data == "main_menu":
            await show_main_menu(query)
        elif data == "buy":
            await show_tariffs(query)
        elif data == "access_status":
            await show_access_status(query)
        elif data in {"privacy_policy", "user_agreement", "documents"}:
            await show_documents(query)
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
    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if manual_webhook:
        builder = builder.updater(None)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(ChatJoinRequestHandler(handle_chat_join_request))
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

        callback_id = str(get_callback_value(callback, "id", "Id") or "")
        subscription_id = str(get_callback_value(callback, "subscriptionId", "SubscriptionId") or "")
        status = normalize_callback_status(get_callback_value(callback, "status", "Status"))
        payment_id, payment = resolve_callback_payment(callback)

        if not payment:
            return JSONResponse({"ok": True})

        update_fields = {
            "status": status,
            "paid_amount": get_callback_value(callback, "amount", "Amount"),
            "paid_currency": get_callback_value(callback, "currency", "Currency"),
            "subscription_id": subscription_id or payment.get("subscription_id"),
        }
        target_ids = {payment_id, subscription_id, callback_id}
        for target_id in target_ids:
            if target_id:
                upsert_payment(target_id, **update_fields)

        if subscription_id and payment_id and subscription_id != payment_id:
            mirror_payment_alias(payment_id, subscription_id, payment)

        if status in {"CONFIRMED", "SUBSCRIPTION_ACTIVATED"}:
            tariff = TARIFFS.get(payment.get("tariff_key", ""))
            chat_id = payment.get("user_id")

            if tariff and chat_id:
                if callback_id and payment_id and callback_id != payment_id:
                    mirror_payment_alias(payment_id, callback_id, payment, reset_runtime_flags=True)
                if subscription_id and payment_id and subscription_id != payment_id:
                    mirror_payment_alias(payment_id, subscription_id, payment)

                event_payment_id = callback_id or payment_id
                event_payment, access_record = await sync_paid_access(app.bot, event_payment_id)
                try:
                    if event_payment and not event_payment.get("notification_sent"):
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=build_paid_text(tariff, access_record),
                            reply_markup=paid_keyboard(access_record),
                            disable_web_page_preview=True,
                        )
                        upsert_payment(event_payment_id, notification_sent=True)
                except Exception:
                    logging.exception("Не удалось отправить сообщение об оплате")
                    if event_payment_id:
                        upsert_payment(event_payment_id, delivered=False, notification_sent=False)

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
        await start_access_loop(app)
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
            await stop_access_loop(app)
            await app.stop()


async def post_init(application: Application) -> None:
    await cache_bot_username(application.bot)
    await start_access_loop(application)


async def post_shutdown(application: Application) -> None:
    await stop_access_loop(application)


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
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
