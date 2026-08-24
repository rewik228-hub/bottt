import asyncio
import hmac
import json
import logging
import os
import re
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
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import uvicorn

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK")
PAID_CHAT_ID_RAW = os.getenv("PAID_CHAT_ID", "").strip()
PAID_CHAT_ID = int(PAID_CHAT_ID_RAW) if PAID_CHAT_ID_RAW else None
ADMIN_USER_IDS_RAW = os.getenv("ADMIN_USER_IDS", "").strip()
ADMIN_USER_IDS = {
    int(chunk.strip())
    for chunk in ADMIN_USER_IDS_RAW.split(",")
    if chunk.strip().isdigit()
}
ADMIN_PANEL_COMMAND = (os.getenv("ADMIN_PANEL_COMMAND") or "roomcontrol").strip().lstrip("/") or "roomcontrol"
ADMIN_PAGE_SIZE = max(1, int(os.getenv("ADMIN_PAGE_SIZE", "8")))
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
ADMIN_STATE_KEY = "admin_state"
ADMIN_PANEL_MESSAGE_ID_KEY = "admin_panel_message_id"
ADMIN_PANEL_CHAT_ID_KEY = "admin_panel_chat_id"


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

PLATEGA_SUCCESS_STATUSES = {"CONFIRMED", "SUBSCRIPTION_ACTIVATED"}


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
        return {"payments": {}, "access": {}, "users": {}}

    try:
        raw_data = json.loads(PAYMENTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logging.warning("payments.json поврежден, создаю новое хранилище.")
        return {"payments": {}, "access": {}, "users": {}}

    if not isinstance(raw_data, dict):
        return {"payments": {}, "access": {}, "users": {}}

    payments = raw_data.get("payments")
    access = raw_data.get("access")
    users = raw_data.get("users")

    if isinstance(payments, dict) or isinstance(access, dict) or isinstance(users, dict):
        return {
            "payments": payments if isinstance(payments, dict) else {},
            "access": access if isinstance(access, dict) else {},
            "users": users if isinstance(users, dict) else {},
        }

    return {"payments": raw_data, "access": {}, "users": {}}


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


def load_user_records() -> dict[str, dict]:
    return load_storage()["users"]


def save_user_records(user_records: dict[str, dict]) -> None:
    storage = load_storage()
    storage["users"] = user_records
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


def get_user_record(user_id: int) -> dict | None:
    return load_user_records().get(str(user_id))


def upsert_access_record(user_id: int, **changes: object) -> dict:
    access_records = load_access_records()
    access_record = access_records.get(str(user_id), {})
    access_record.update(changes)
    access_records[str(user_id)] = access_record
    save_access_records(access_records)
    return access_record


def upsert_user_record(user_id: int, **changes: object) -> dict:
    user_records = load_user_records()
    user_record = user_records.get(str(user_id), {})
    user_record.update(changes)
    user_records[str(user_id)] = user_record
    save_user_records(user_records)
    return user_record


def remember_user(user, *, last_action: str | None = None) -> None:
    if not user:
        return

    now = serialize_datetime(utc_now())
    existing_user = get_user_record(user.id) or {}
    upsert_user_record(
        user.id,
        username=user.username or existing_user.get("username"),
        first_name=user.first_name or existing_user.get("first_name"),
        last_name=user.last_name or existing_user.get("last_name"),
        full_name=user.full_name or existing_user.get("full_name"),
        language_code=user.language_code or existing_user.get("language_code"),
        is_bot=bool(user.is_bot),
        first_seen_at=existing_user.get("first_seen_at") or now,
        last_seen_at=now,
        last_action=last_action or existing_user.get("last_action"),
    )


def collect_known_user_ids() -> list[int]:
    user_ids: set[int] = set()

    for raw_user_id in load_user_records():
        if raw_user_id.lstrip("-").isdigit():
            user_ids.add(int(raw_user_id))

    for raw_user_id in load_access_records():
        if raw_user_id.lstrip("-").isdigit():
            user_ids.add(int(raw_user_id))

    for payment in load_payments().values():
        user_id = payment.get("user_id")
        if isinstance(user_id, int):
            user_ids.add(user_id)

    return sorted(user_ids)


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
        if expires_at is None:
            expiry_text = "Доступ действует бессрочно."
        else:
            expiry_text = f"Доступ действует до: {format_datetime_local(expires_at)}"
        return (
            "Подписка активна.\n"
            f"{expiry_text}\n"
            f"{membership_text}"
        )

    expires_at = get_access_expires_at(access_record)
    if expires_at is None:
        return "Подписка оформлена, но статус доступа не удалось определить."

    return (
        "Подписка неактивна.\n"
        f"Срок доступа закончился: {format_datetime_local(expires_at)}"
    )


def is_admin_user(user_id: int | None) -> bool:
    return user_id is not None and user_id in ADMIN_USER_IDS


def build_user_display_name(user_id: int, user_record: dict | None = None) -> str:
    user_record = user_record or {}
    full_name = user_record.get("full_name") or user_record.get("first_name")
    username = user_record.get("username")

    if full_name and username:
        return f"{full_name} (@{username})"
    if full_name:
        return str(full_name)
    if username:
        return f"@{username}"
    return f"Пользователь {user_id}"


def get_access_entry_link(access_record: dict | None = None) -> str:
    if isinstance(PAID_CHANNEL_LINK, str) and PAID_CHANNEL_LINK.strip():
        return PAID_CHANNEL_LINK.strip()

    invite_link = access_record.get("invite_link") if access_record else None
    if isinstance(invite_link, str):
        return invite_link.strip()

    return ""


def access_status_brief(access_record: dict | None) -> str:
    if not access_record:
        return "нет доступа"

    if has_active_access(access_record):
        expires_at = get_access_expires_at(access_record)
        if expires_at is None:
            return "активен навсегда"
        return f"активен до {format_datetime_local(expires_at)}"

    expires_at = get_access_expires_at(access_record)
    if expires_at is None:
        return "неактивен"
    return f"истек {format_datetime_local(expires_at)}"


def membership_status_brief(access_record: dict | None) -> str:
    if not access_record:
        return "не в приватке"
    if access_record.get("is_member"):
        return "в канале"
    if has_active_access(access_record):
        return "доступ активен, вход не завершен"
    return "доступ снят"


def get_known_user_snapshots() -> list[dict]:
    snapshots: list[dict] = []

    for user_id in collect_known_user_ids():
        snapshots.append(
            {
                "user_id": user_id,
                "user_record": get_user_record(user_id) or {},
                "access_record": get_access_record(user_id) or {},
            }
        )

    return snapshots


def build_admin_home_text() -> str:
    snapshots = get_known_user_snapshots()
    active_count = sum(1 for snapshot in snapshots if has_active_access(snapshot["access_record"]))
    member_count = sum(1 for snapshot in snapshots if snapshot["access_record"].get("is_member"))
    expired_count = sum(
        1
        for snapshot in snapshots
        if snapshot["access_record"] and not has_active_access(snapshot["access_record"])
    )

    lines = [
        "Скрытая админ-панель",
        "",
        f"Пользователей в базе: {len(snapshots)}",
        f"Активных доступов: {active_count}",
        f"Сейчас в приватке: {member_count}",
        f"Истекших или снятых доступов: {expired_count}",
        "",
        "Панель открывается только по твоей кастомной команде и приходит отдельным сообщением.",
    ]
    return "\n".join(lines)


def admin_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Активные доступы", callback_data="admin:list:active:0"),
                InlineKeyboardButton("Все пользователи", callback_data="admin:list:all:0"),
            ],
            [
                InlineKeyboardButton("Выдать доступ", callback_data="admin:grant"),
                InlineKeyboardButton("Снять доступ", callback_data="admin:revoke"),
            ],
            [
                InlineKeyboardButton("Восстановить", callback_data="admin:restore"),
                InlineKeyboardButton("Рассылка", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton("Обновить", callback_data="admin:home"),
                InlineKeyboardButton("Закрыть", callback_data="admin:close"),
            ],
        ]
    )


def admin_input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("◀ Назад", callback_data="admin:home")],
            [InlineKeyboardButton("Закрыть", callback_data="admin:close")],
        ]
    )


def build_admin_user_list(mode: str, page: int) -> tuple[str, InlineKeyboardMarkup]:
    snapshots = get_known_user_snapshots()

    if mode == "active":
        title = "Активные доступы"
        snapshots = [
            snapshot for snapshot in snapshots if has_active_access(snapshot["access_record"])
        ]
        snapshots.sort(
            key=lambda snapshot: (
                get_access_expires_at(snapshot["access_record"]) is not None,
                get_access_expires_at(snapshot["access_record"]) or datetime.max.replace(tzinfo=UTC),
                snapshot["user_id"],
            )
        )
    else:
        title = "Все пользователи"
        snapshots.sort(
            key=lambda snapshot: (
                parse_datetime(snapshot["user_record"].get("last_seen_at")) or datetime.min.replace(tzinfo=UTC),
                snapshot["user_id"],
            ),
            reverse=True,
        )

    total_items = len(snapshots)
    total_pages = max(1, (total_items + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start_index = page * ADMIN_PAGE_SIZE
    end_index = start_index + ADMIN_PAGE_SIZE
    page_items = snapshots[start_index:end_index]

    lines = [f"{title} ({total_items})", ""]

    if not page_items:
        lines.append("Список пока пуст.")
    else:
        for index, snapshot in enumerate(page_items, start=start_index + 1):
            user_id = snapshot["user_id"]
            user_record = snapshot["user_record"]
            access_record = snapshot["access_record"]
            last_seen_at = parse_datetime(user_record.get("last_seen_at"))
            lines.extend(
                [
                    f"{index}. {build_user_display_name(user_id, user_record)}",
                    f"ID: {user_id}",
                    f"Доступ: {access_status_brief(access_record)}",
                    f"Статус: {membership_status_brief(access_record)}",
                    (
                        f"Последняя активность: {format_datetime_local(last_seen_at)}"
                        if last_seen_at
                        else "Последняя активность: неизвестно"
                    ),
                    "",
                ]
            )

    lines.append(f"Страница {page + 1}/{total_pages}")

    navigation_row: list[InlineKeyboardButton] = []
    if page > 0:
        navigation_row.append(
            InlineKeyboardButton("◀", callback_data=f"admin:list:{mode}:{page - 1}")
        )
    if page < total_pages - 1:
        navigation_row.append(
            InlineKeyboardButton("▶", callback_data=f"admin:list:{mode}:{page + 1}")
        )

    keyboard_rows: list[list[InlineKeyboardButton]] = []
    if navigation_row:
        keyboard_rows.append(navigation_row)
    keyboard_rows.append([InlineKeyboardButton("◀ Назад", callback_data="admin:home")])
    keyboard_rows.append(
        [
            InlineKeyboardButton("Выдать доступ", callback_data="admin:grant"),
            InlineKeyboardButton("Снять доступ", callback_data="admin:revoke"),
        ]
    )
    keyboard_rows.append([InlineKeyboardButton("Закрыть", callback_data="admin:close")])

    return "\n".join(lines).strip(), InlineKeyboardMarkup(keyboard_rows)


def build_admin_grant_help_text() -> str:
    return (
        "Выдача доступа\n\n"
        "Отправь следующим сообщением:\n"
        "`user_id срок`\n\n"
        "Примеры:\n"
        "`123456789 tariff_week`\n"
        "`@username tariff_month`\n"
        "`123456789 1m`\n"
        "`123456789 1h`\n"
        "`123456789 3d`\n"
        "`123456789 2w`\n"
        "`123456789 1mo`\n"
        "`123456789 forever`\n\n"
        "Поддерживаются значения:\n"
        "`tariff_day`, `tariff_week`, `tariff_month`, `tariff_forever`, `forever`\n"
        "и произвольные сроки:\n"
        "`m` = минуты, `h` = часы, `d` = дни, `w` = недели, `mo` = месяцы по 30 дней"
    )


def build_admin_revoke_help_text() -> str:
    return (
        "Снятие доступа\n\n"
        "Отправь следующим сообщением:\n"
        "`user_id`\n"
        "или\n"
        "`@username`"
    )


def build_admin_broadcast_help_text() -> str:
    return (
        "Массовая рассылка\n\n"
        "Отправь следующим сообщением текст, который нужно разослать всем пользователям из базы бота.\n\n"
        "Сообщение уйдет как обычный текст с сохранением переносов строк."
    )


def build_admin_restore_help_text() -> str:
    return (
        "Восстановление подписки\n\n"
        "Отправь одной строкой:\n"
        "`user_id tariff_key payment_id [subscription_id] [purchase_at_or_expires_at]`\n\n"
        "Если после `payment_id` сразу идет дата, бот считает ее моментом покупки и сам прибавляет длительность тарифа.\n"
        "Можно вставить и JSON с полями `user_id`, `tariff_key`, `payment_id`, `subscription_id`, `purchased_at`, `expires_at`.\n\n"
        "Примеры:\n"
        "`672352889 tariff_day 90333fa2-390c-48cd-8dc9-f769d5372dd0`\n"
        "`672352889 tariff_day 90333fa2-390c-48cd-8dc9-f769d5372dd0 24.08.2026-02:10:21`\n"
        "`672352889 tariff_day 90333fa2-390c-48cd-8dc9-f769d5372dd0 sub-123 25.08.2026-02:10:21`\n\n"
        "Если `subscription_id` не указан, бот подставит `payment_id`."
    )


def remember_admin_panel_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    context.user_data[ADMIN_PANEL_CHAT_ID_KEY] = chat_id
    context.user_data[ADMIN_PANEL_MESSAGE_ID_KEY] = message_id


async def upsert_admin_panel_message(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    stored_chat_id = context.user_data.get(ADMIN_PANEL_CHAT_ID_KEY)
    stored_message_id = context.user_data.get(ADMIN_PANEL_MESSAGE_ID_KEY)

    if stored_chat_id == chat_id and isinstance(stored_message_id, int):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=stored_message_id,
                text=text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return
        except Exception:
            logging.exception("Не удалось обновить сообщение админ-панели")

    sent_message = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )
    remember_admin_panel_message(context, chat_id, sent_message.message_id)


async def delete_message_safely(message) -> None:
    if not message:
        return

    try:
        await message.delete()
    except Exception:
        logging.exception("Не удалось удалить сообщение %s", getattr(message, "message_id", "?"))


async def safe_edit_query_message(query, text: str, reply_markup=None, **kwargs) -> bool:
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, **kwargs)
        return True
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return False
        raise


def normalize_admin_tariff_key(raw_value: str) -> str | None:
    normalized = raw_value.strip().lower()
    alias_map = {
        "tariff_day": "tariff_day",
        "day": "tariff_day",
        "1d": "tariff_day",
        "tariff_week": "tariff_week",
        "week": "tariff_week",
        "7d": "tariff_week",
        "tariff_month": "tariff_month",
        "month": "tariff_month",
        "30d": "tariff_month",
        "tariff_forever": "tariff_forever",
        "forever": "tariff_forever",
        "life": "tariff_forever",
        "navsegda": "tariff_forever",
    }
    return alias_map.get(normalized)


def parse_admin_duration(raw_value: str) -> dict | None:
    normalized = raw_value.strip().lower()
    tariff_key = normalize_admin_tariff_key(normalized)
    if tariff_key:
        tariff = TARIFFS[tariff_key]
        if tariff.get("recurring"):
            duration = timedelta(days=int(tariff["interval_days"]))
        else:
            duration = None
        return {
            "tariff_key": tariff_key,
            "duration": duration,
            "label": tariff["label"],
            "is_forever": duration is None,
        }

    match = re.fullmatch(
        r"(?P<amount>\d+)\s*(?P<unit>m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks|mo|mon|month|months)",
        normalized,
    )
    if not match:
        return None

    amount = int(match.group("amount"))
    unit = match.group("unit")
    if amount <= 0:
        return None

    if unit in {"m", "min", "mins", "minute", "minutes"}:
        duration = timedelta(minutes=amount)
        label = f"{amount} мин."
    elif unit in {"h", "hr", "hrs", "hour", "hours"}:
        duration = timedelta(hours=amount)
        label = f"{amount} ч."
    elif unit in {"d", "day", "days"}:
        duration = timedelta(days=amount)
        label = f"{amount} дн."
    elif unit in {"w", "week", "weeks"}:
        duration = timedelta(weeks=amount)
        label = f"{amount} нед."
    else:
        duration = timedelta(days=amount * 30)
        label = f"{amount} мес."

    return {
        "tariff_key": "manual_custom",
        "duration": duration,
        "label": label,
        "is_forever": False,
    }


def parse_admin_restore_datetime(raw_value: str | None) -> datetime | None:
    if raw_value is None:
        return None

    normalized = raw_value.strip()
    if not normalized or normalized.lower() == "forever":
        return None

    parsed = parse_datetime(normalized)
    if parsed is not None:
        return parsed

    match = re.fullmatch(
        r"(?P<day>\d{2})\.(?P<month>\d{2})\.(?P<year>\d{4})[- ](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})",
        normalized,
    )
    if not match:
        return None

    try:
        return datetime(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second")),
            tzinfo=MOSCOW_TZ,
        ).astimezone(UTC)
    except ValueError:
        return None


def parse_admin_restore_entry(raw_text: str) -> tuple[dict | None, str | None]:
    text = raw_text.strip()
    if not text:
        return None, "Пустая строка."

    source: dict[str, object]
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None, "Не удалось разобрать JSON."
        if not isinstance(parsed, dict):
            return None, "JSON должен быть объектом."
        source = parsed
    else:
        parts = text.split()
        if len(parts) < 3:
            return None, "Нужен формат: user_id tariff_key payment_id [subscription_id] [expires_at]"

        source = {
            "user_id": parts[0],
            "tariff_key": parts[1],
            "payment_id": parts[2],
        }
        if len(parts) == 4 and parse_admin_restore_datetime(parts[3]) is not None:
            source["subscription_id"] = parts[2]
            source["purchased_at"] = parts[3]
        elif len(parts) >= 4:
            source["subscription_id"] = parts[3]
            if len(parts) >= 5:
                source["expires_at"] = " ".join(parts[4:])

    payload_source = source.get("payload")
    payload_data: dict[str, object] = {}
    if isinstance(payload_source, str):
        try:
            parsed_payload = json.loads(payload_source)
        except json.JSONDecodeError:
            parsed_payload = None
        if isinstance(parsed_payload, dict):
            payload_data = parsed_payload
    elif isinstance(payload_source, dict):
        payload_data = payload_source

    metadata_source = source.get("metadata")
    metadata_data = metadata_source if isinstance(metadata_source, dict) else {}

    user_ref = (
        source.get("user_id")
        or source.get("userId")
        or payload_data.get("user_id")
        or payload_data.get("userId")
        or metadata_data.get("userId")
    )
    tariff_value = (
        source.get("tariff_key")
        or source.get("tariffKey")
        or payload_data.get("tariff_key")
        or payload_data.get("tariffKey")
    )
    payment_id = (
        source.get("payment_id")
        or source.get("paymentId")
        or source.get("transaction_id")
        or source.get("transactionId")
        or source.get("id")
    )
    subscription_id = source.get("subscription_id") or source.get("subscriptionId") or payment_id
    expires_raw = (
        source.get("expires_at")
        or source.get("expiresAt")
        or source.get("next_charge_at")
        or source.get("nextChargeAt")
    )
    purchased_raw = source.get("purchased_at") or source.get("purchasedAt")

    user_id = resolve_user_reference(str(user_ref or ""))
    tariff_key = normalize_admin_tariff_key(str(tariff_value or ""))
    payment_id_str = str(payment_id or "").strip()
    subscription_id_str = str(subscription_id or "").strip() or payment_id_str
    expires_at = parse_admin_restore_datetime(str(expires_raw)) if expires_raw is not None else None
    purchased_at = parse_admin_restore_datetime(str(purchased_raw)) if purchased_raw is not None else None

    if user_id is None:
        return None, "Не удалось определить `user_id`."
    if not tariff_key:
        return None, "Не удалось определить `tariff_key`."
    if not payment_id_str:
        return None, "Не удалось определить `payment_id`."

    return (
        {
            "user_id": user_id,
            "tariff_key": tariff_key,
            "payment_id": payment_id_str,
            "subscription_id": subscription_id_str,
            "purchased_at": purchased_at,
            "expires_at": expires_at,
        },
        None,
    )


def resolve_user_reference(raw_value: str) -> int | None:
    candidate = raw_value.strip()
    if not candidate:
        return None

    if candidate.lstrip("-").isdigit():
        return int(candidate)

    if not candidate.startswith("@"):
        return None

    username = candidate[1:].strip().lower()
    if not username:
        return None

    for raw_user_id, user_record in load_user_records().items():
        if str(user_record.get("username", "")).strip().lower() == username and raw_user_id.isdigit():
            return int(raw_user_id)

    return None


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
    access_is_active = has_active_access(access_record)
    entry_link = get_access_entry_link(access_record)
    if access_is_active and entry_link:
        keyboard.append([InlineKeyboardButton("Подать заявку в канал", url=entry_link)])

    if access_is_active:
        keyboard.append([InlineKeyboardButton("Мой доступ", callback_data="access_status")])
    else:
        keyboard.append([InlineKeyboardButton("Оплатить тариф", callback_data="buy")])
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
    entry_link = get_access_entry_link(access_record)
    if entry_link:
        lines.extend(
            [
                "",
                "Нажми на кнопку ниже и отправь заявку на вступление. Бот примет ее автоматически.",
                entry_link,
            ]
        )
    elif access_record and access_record.get("is_member"):
        lines.extend(["", "Ты уже находишься в приватке."])
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


async def allow_user_to_rejoin_paid_chat(bot, user_id: int) -> None:
    if not PAID_CHAT_ID:
        return

    try:
        member = await bot.get_chat_member(PAID_CHAT_ID, user_id)
    except Exception:
        logging.exception("Не удалось проверить статус пользователя %s в приватке", user_id)
        return

    if member.status != "kicked":
        return

    try:
        await bot.unban_chat_member(PAID_CHAT_ID, user_id, only_if_banned=True)
    except Exception:
        logging.exception("Не удалось снять бан с пользователя %s перед повторным входом", user_id)


async def ensure_join_request_link(bot, user_id: int, access_record: dict) -> dict:
    if not PAID_CHAT_ID or access_record.get("is_member"):
        return access_record

    await allow_user_to_rejoin_paid_chat(bot, user_id)
    if get_access_entry_link(access_record):
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


async def get_access_record_for_display(bot, user_id: int) -> dict | None:
    access_record = get_access_record(user_id)
    if not access_record or not has_active_access(access_record):
        return access_record

    try:
        return await ensure_join_request_link(bot, user_id, access_record)
    except Exception:
        logging.exception("Не удалось подготовить данные доступа для пользователя %s", user_id)
        return get_access_record(user_id) or access_record


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
    delivery_link = get_access_entry_link(refreshed_access_record)
    payment = upsert_payment(payment_id, delivered=bool(delivery_link), delivery_link=delivery_link)
    return payment, refreshed_access_record


async def grant_manual_access(
    bot,
    *,
    user_id: int,
    tariff_key: str,
    duration: timedelta | None,
    label: str,
    granted_by: int,
) -> dict:
    now = utc_now()
    existing_access = get_access_record(user_id)
    base_time = get_access_base_time(existing_access, now)
    expires_at = base_time + duration if duration is not None else None

    access_record = upsert_access_record(
        user_id,
        active=True,
        tariff_key=tariff_key,
        grant_label=label,
        grant_duration_seconds=int(duration.total_seconds()) if duration is not None else None,
        source_payment_id=f"manual:{granted_by}:{int(now.timestamp())}",
        activated_at=serialize_datetime(now),
        expires_at=serialize_datetime(expires_at),
        is_member=bool(existing_access.get("is_member")) if existing_access else False,
        granted_manually=True,
        granted_by=granted_by,
        revoked_at=None,
        removed_at=None,
    )
    access_record = await ensure_join_request_link(bot, user_id, access_record)
    return access_record


async def deactivate_user_access(
    bot,
    *,
    user_id: int,
    access_record: dict,
    notify_text: str,
    revoked_by: int | None = None,
) -> None:
    now = utc_now()

    if PAID_CHAT_ID:
        try:
            member = await bot.get_chat_member(PAID_CHAT_ID, user_id)
        except Exception:
            logging.exception("Не удалось проверить статус пользователя %s перед исключением", user_id)
        else:
            if member.status not in {"left", "kicked"}:
                try:
                    await bot.ban_chat_member(PAID_CHAT_ID, user_id)
                except Exception:
                    logging.exception("Не удалось исключить пользователя %s из приватки", user_id)
                else:
                    try:
                        await bot.unban_chat_member(PAID_CHAT_ID, user_id, only_if_banned=True)
                    except Exception:
                        logging.exception(
                            "Пользователь %s исключен, но снять бан не удалось. Повторно сниму бан при новой выдаче доступа.",
                            user_id,
                        )

    upsert_access_record(
        user_id,
        active=False,
        is_member=False,
        revoked_at=serialize_datetime(now),
        revoked_by=revoked_by,
        invite_link=None,
        invite_link_expires_at=None,
    )

    try:
        await bot.send_message(
            chat_id=user_id,
            text=notify_text,
            reply_markup=main_menu_keyboard(),
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление пользователю %s", user_id)


async def revoke_expired_access(bot, user_id: int, access_record: dict) -> None:
    await deactivate_user_access(
        bot,
        user_id=user_id,
        access_record=access_record,
        notify_text=(
            "Срок подписки закончился, доступ в приватку отключен.\n\n"
            "Если хочешь вернуться, оформи новую подписку в боте."
        ),
    )


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
    await safe_edit_query_message(query, WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def show_tariffs(query) -> None:
    await safe_edit_query_message(query, TARIFFS_TEXT, reply_markup=tariff_menu_keyboard())


async def show_support(query) -> None:
    await safe_edit_query_message(query, SUPPORT_TEXT, reply_markup=back_to_main_menu_keyboard())


async def show_documents(query) -> None:
    await safe_edit_query_message(
        query,
        DOCUMENTS_TEXT,
        reply_markup=back_to_main_menu_keyboard(),
        disable_web_page_preview=True,
    )


async def show_access_status(query) -> None:
    access_record = await get_access_record_for_display(query.bot, query.from_user.id)

    await safe_edit_query_message(
        query,
        access_status_text(access_record),
        reply_markup=paid_keyboard(access_record),
        disable_web_page_preview=True,
    )


async def handle_tariff_selection(query, tariff_key: str) -> None:
    tariff = TARIFFS[tariff_key]
    user = query.from_user
    user_name = f"@{user.username}" if user.username else user.full_name
    remember_user(user, last_action=f"buy:{tariff_key}")

    try:
        transaction = await create_transaction(tariff_key, user.id, user_name)
    except Exception:
        logging.exception("Не удалось создать транзакцию Platega")
        await safe_edit_query_message(
            query,
            "Не удалось создать счет. Попробуй еще раз чуть позже или напиши в поддержку.",
            reply_markup=back_to_main_menu_keyboard(),
        )
        return

    invoice_id = str(transaction["transactionId"])
    pay_url = extract_payment_url(transaction)

    upsert_payment(
        invoice_id,
        user_id=user.id,
        username=user.username or "",
        full_name=user.full_name,
        tariff_key=tariff_key,
        pay_url=pay_url,
        status=transaction.get("status", "PENDING"),
        expires_in=transaction.get("expiresIn"),
        delivered=False,
        created_at=serialize_datetime(utc_now()),
    )

    await safe_edit_query_message(
        query,
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

    if status in PLATEGA_SUCCESS_STATUSES:
        _, access_record = await sync_paid_access(query.bot, invoice_id)
        await safe_edit_query_message(
            query,
            build_paid_text(tariff, access_record),
            reply_markup=paid_keyboard(access_record),
            disable_web_page_preview=True,
        )
        return False

    if status in {"CANCELED", "CHARGEBACKED"}:
        await safe_edit_query_message(
            query,
            build_expired_text(tariff),
            reply_markup=expired_invoice_keyboard(payment["tariff_key"]),
        )
        return False

    await safe_edit_query_message(
        query,
        build_invoice_text(tariff, invoice_id),
        reply_markup=invoice_keyboard(payment["pay_url"], invoice_id),
        disable_web_page_preview=True,
    )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update.effective_user, last_action="start")
    await update.message.reply_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    remember_user(update.effective_user, last_action="status")
    access_record = await get_access_record_for_display(context.bot, update.effective_user.id)

    await update.message.reply_text(
        access_status_text(access_record),
        reply_markup=paid_keyboard(access_record),
        disable_web_page_preview=True,
    )


async def admin_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" or not is_admin_user(update.effective_user.id):
        return

    remember_user(update.effective_user, last_action=f"admin_command:{ADMIN_PANEL_COMMAND}")
    context.user_data.pop(ADMIN_STATE_KEY, None)
    await delete_message_safely(update.message)
    await upsert_admin_panel_message(
        context,
        chat_id=update.effective_chat.id,
        text=build_admin_home_text(),
        reply_markup=admin_home_keyboard(),
    )


async def process_admin_grant_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
) -> tuple[bool, str]:
    parts = raw_text.split()
    if len(parts) != 2:
        return False, "Нужен формат: user_id тариф"

    target_user_id = resolve_user_reference(parts[0])
    if target_user_id is None:
        return False, "Не удалось определить пользователя. Используй user_id или @username из базы бота."

    grant_spec = parse_admin_duration(parts[1])
    if grant_spec is None:
        return False, "Неизвестный срок. Используй tariff_week, forever или формат вроде 1m, 1h, 3d, 2w, 1mo."

    access_record = await grant_manual_access(
        context.bot,
        user_id=target_user_id,
        tariff_key=grant_spec["tariff_key"],
        duration=grant_spec["duration"],
        label=grant_spec["label"],
        granted_by=update.effective_user.id,
    )
    user_record = get_user_record(target_user_id)
    notification_sent = True

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=(
                "Администратор выдал тебе доступ в приватку.\n\n"
                f"{access_status_text(access_record)}"
            ),
            reply_markup=paid_keyboard(access_record),
            disable_web_page_preview=True,
        )
    except Exception:
        notification_sent = False
        logging.exception("Не удалось уведомить пользователя %s о ручной выдаче доступа", target_user_id)

    notice = (
        f"Доступ выдан: {build_user_display_name(target_user_id, user_record)}\n"
        f"ID: {target_user_id}\n"
        f"Выдано на: {grant_spec['label']}\n"
        f"Новый статус: {access_status_brief(access_record)}"
    )
    if not notification_sent:
        notice += "\nПользователю не удалось отправить уведомление в личку."
    return True, notice


async def process_admin_revoke_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
) -> tuple[bool, str]:
    parts = raw_text.split()
    if len(parts) != 1:
        return False, "Для снятия доступа отправь только user_id или @username."

    target_user_id = resolve_user_reference(parts[0])
    if target_user_id is None:
        return False, "Не удалось определить пользователя. Используй user_id или @username из базы бота."

    access_record = get_access_record(target_user_id)
    if not access_record or (not has_active_access(access_record) and not access_record.get("is_member")):
        return False, "У этого пользователя сейчас нет активного доступа."

    await deactivate_user_access(
        context.bot,
        user_id=target_user_id,
        access_record=access_record,
        notify_text=(
            "Доступ в приватку отключен администратором.\n\n"
            "Если это выглядит как ошибка, напиши в поддержку."
        ),
        revoked_by=update.effective_user.id,
    )

    user_record = get_user_record(target_user_id)
    return True, (
        f"Доступ снят: {build_user_display_name(target_user_id, user_record)}\n"
        f"ID: {target_user_id}"
    )


async def process_admin_restore_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
) -> tuple[bool, str]:
    restored_entry, error_text = parse_admin_restore_entry(raw_text)
    if restored_entry is None:
        return False, error_text or "Не удалось разобрать данные для восстановления."

    user_id = int(restored_entry["user_id"])
    tariff_key = str(restored_entry["tariff_key"])
    payment_id = str(restored_entry["payment_id"])
    subscription_id = str(restored_entry["subscription_id"])
    purchased_at = restored_entry["purchased_at"]
    expires_at = restored_entry["expires_at"]

    tariff = TARIFFS[tariff_key]
    existing_access = get_access_record(user_id)
    now = utc_now()

    if not isinstance(expires_at, datetime):
        if tariff.get("recurring"):
            if isinstance(purchased_at, datetime):
                expires_at = purchased_at + timedelta(days=int(tariff["interval_days"]))
            else:
                base_time = get_access_base_time(existing_access, now)
                expires_at = base_time + timedelta(days=int(tariff["interval_days"]))
        else:
            expires_at = None

    status = "SUBSCRIPTION_ACTIVATED" if tariff.get("recurring") else "CONFIRMED"
    created_at = purchased_at if isinstance(purchased_at, datetime) else now
    payment = upsert_payment(
        payment_id,
        user_id=user_id,
        tariff_key=tariff_key,
        status=status,
        subscription_id=subscription_id,
        created_at=serialize_datetime(created_at),
        access_applied=True,
        access_expires_at=serialize_datetime(expires_at),
        restored_by=update.effective_user.id,
        restored_at=serialize_datetime(now),
    )

    if subscription_id and subscription_id != payment_id:
        mirror_payment_alias(payment_id, subscription_id, payment)

    access_record = upsert_access_record(
        user_id,
        active=True,
        tariff_key=tariff_key,
        source_payment_id=payment_id,
        activated_at=serialize_datetime(created_at),
        expires_at=serialize_datetime(expires_at),
        is_member=bool(existing_access.get("is_member")) if existing_access else False,
        removed_at=None,
        revoked_at=None,
    )
    access_record = await ensure_join_request_link(context.bot, user_id, access_record)
    delivery_link = get_access_entry_link(access_record)
    payment = upsert_payment(
        payment_id,
        delivered=bool(delivery_link),
        delivery_link=delivery_link,
        subscription_id=subscription_id,
    )
    if subscription_id and subscription_id != payment_id:
        mirror_payment_alias(payment_id, subscription_id, payment)

    user_record = get_user_record(user_id)
    expiry_text = format_datetime_local(expires_at) if isinstance(expires_at, datetime) else "навсегда"
    return True, (
        f"Подписка восстановлена: {build_user_display_name(user_id, user_record)}\n"
        f"ID: {user_id}\n"
        f"Тариф: {tariff['label']}\n"
        f"Payment ID: {payment_id}\n"
        f"Subscription ID: {subscription_id}\n"
        f"Доступ активен до: {expiry_text}"
    )


async def process_admin_broadcast_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    raw_text: str,
) -> tuple[bool, str]:
    message_text = raw_text.strip()
    if not message_text:
        return False, "Текст рассылки пустой. Отправь обычное сообщение с текстом для всех пользователей."

    user_ids = collect_known_user_ids()
    if not user_ids:
        return False, "В базе пока нет пользователей для рассылки."

    sent_count = 0
    failed_ids: list[int] = []

    for user_id in user_ids:
        try:
            for chunk in split_text_for_telegram(message_text):
                await context.bot.send_message(
                    chat_id=user_id,
                    text=chunk,
                    disable_web_page_preview=True,
                )
            sent_count += 1
        except Exception:
            failed_ids.append(user_id)
            logging.exception("Не удалось отправить рассылку пользователю %s", user_id)

    failed_preview = ", ".join(str(user_id) for user_id in failed_ids[:10])
    lines = [
        "Рассылка завершена.",
        f"Всего пользователей в базе: {len(user_ids)}",
        f"Успешно отправлено: {sent_count}",
        f"Не удалось отправить: {len(failed_ids)}",
    ]
    if failed_preview:
        suffix = "..." if len(failed_ids) > 10 else ""
        lines.append(f"Проблемные ID: {failed_preview}{suffix}")
    return True, "\n".join(lines)


async def admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private" or not is_admin_user(update.effective_user.id):
        return

    admin_state = context.user_data.get(ADMIN_STATE_KEY)
    if not admin_state:
        return

    remember_user(update.effective_user, last_action=f"admin_input:{admin_state.get('action', 'unknown')}")
    raw_text = (update.message.text or "").strip()
    await delete_message_safely(update.message)

    if admin_state.get("action") == "grant":
        success, notice = await process_admin_grant_message(update, context, raw_text)
        help_text = build_admin_grant_help_text()
    elif admin_state.get("action") == "revoke":
        success, notice = await process_admin_revoke_message(update, context, raw_text)
        help_text = build_admin_revoke_help_text()
    elif admin_state.get("action") == "restore":
        success, notice = await process_admin_restore_message(update, context, raw_text)
        help_text = build_admin_restore_help_text()
    elif admin_state.get("action") == "broadcast":
        success, notice = await process_admin_broadcast_message(update, context, raw_text)
        help_text = build_admin_broadcast_help_text()
    else:
        context.user_data.pop(ADMIN_STATE_KEY, None)
        await upsert_admin_panel_message(
            context,
            chat_id=update.effective_chat.id,
            text=build_admin_home_text(),
            reply_markup=admin_home_keyboard(),
        )
        return

    if success:
        context.user_data.pop(ADMIN_STATE_KEY, None)
        await upsert_admin_panel_message(
            context,
            chat_id=update.effective_chat.id,
            text=f"{notice}\n\n{build_admin_home_text()}",
            reply_markup=admin_home_keyboard(),
        )
        return

    await upsert_admin_panel_message(
        context,
        chat_id=update.effective_chat.id,
        text=f"{notice}\n\n{help_text}",
        reply_markup=admin_input_keyboard(),
    )


async def handle_admin_callback(query, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_admin_user(query.from_user.id):
        await query.answer()
        return True

    remember_user(query.from_user, last_action=query.data)
    remember_admin_panel_message(context, query.message.chat_id, query.message.message_id)

    data = query.data
    context.user_data.setdefault(ADMIN_PANEL_CHAT_ID_KEY, query.message.chat_id)
    context.user_data.setdefault(ADMIN_PANEL_MESSAGE_ID_KEY, query.message.message_id)

    if data == "admin:home":
        context.user_data.pop(ADMIN_STATE_KEY, None)
        await safe_edit_query_message(
            query,
            build_admin_home_text(),
            reply_markup=admin_home_keyboard(),
            disable_web_page_preview=True,
        )
    elif data == "admin:grant":
        context.user_data[ADMIN_STATE_KEY] = {"action": "grant"}
        await safe_edit_query_message(
            query,
            build_admin_grant_help_text(),
            reply_markup=admin_input_keyboard(),
            disable_web_page_preview=True,
        )
    elif data == "admin:revoke":
        context.user_data[ADMIN_STATE_KEY] = {"action": "revoke"}
        await safe_edit_query_message(
            query,
            build_admin_revoke_help_text(),
            reply_markup=admin_input_keyboard(),
            disable_web_page_preview=True,
        )
    elif data == "admin:restore":
        context.user_data[ADMIN_STATE_KEY] = {"action": "restore"}
        await safe_edit_query_message(
            query,
            build_admin_restore_help_text(),
            reply_markup=admin_input_keyboard(),
            disable_web_page_preview=True,
        )
    elif data == "admin:broadcast":
        context.user_data[ADMIN_STATE_KEY] = {"action": "broadcast"}
        await safe_edit_query_message(
            query,
            build_admin_broadcast_help_text(),
            reply_markup=admin_input_keyboard(),
            disable_web_page_preview=True,
        )
    elif data.startswith("admin:list:"):
        context.user_data.pop(ADMIN_STATE_KEY, None)
        _, _, mode, page_raw = data.split(":", maxsplit=3)
        try:
            page = int(page_raw)
        except ValueError:
            page = 0
        list_text, list_keyboard = build_admin_user_list(mode, page)
        await safe_edit_query_message(
            query,
            list_text,
            reply_markup=list_keyboard,
            disable_web_page_preview=True,
        )
    elif data == "admin:close":
        context.user_data.pop(ADMIN_STATE_KEY, None)
        await query.answer()
        await delete_message_safely(query.message)
        return True
    else:
        await query.answer("Неизвестное действие.", show_alert=True)
        return True

    await query.answer()
    return True


async def handle_chat_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    request = update.chat_join_request
    if not request or not PAID_CHAT_ID or request.chat.id != PAID_CHAT_ID:
        return

    remember_user(request.from_user, last_action="chat_join_request")
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
    remember_user(query.from_user, last_action=data)

    try:
        if data.startswith("admin:"):
            already_answered = await handle_admin_callback(query, context)
        elif data == "main_menu":
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
    app.add_handler(CommandHandler(ADMIN_PANEL_COMMAND, admin_panel_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_text_input))
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

        if status in PLATEGA_SUCCESS_STATUSES:
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

    if not ADMIN_USER_IDS:
        logging.warning("ADMIN_USER_IDS не задан. Скрытая админ-панель будет недоступна.")

    if WEBHOOK_URL:
        asyncio.run(run_manual_webhook())
        return

    logging.info("WEBHOOK_URL не задан. Запускаю бота в polling mode.")
    logging.info("Callback от Platega недоступен без WEBHOOK_URL, оплата подтверждается кнопкой «Проверить оплату».")
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
