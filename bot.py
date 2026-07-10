"""
ByteFlow — Telegram-бот на aiogram 3.x
Автоматизована система замовлень, оплати (симуляція), AI-аудит та маршрутизація для адміністратора.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    TelegramObject,
)
from dotenv import load_dotenv

from dashboard import DASHBOARD_UPDATE_INTERVAL, dashboard
from google_sheets import GoogleSheetsOrders
from user_logs import list_active_users_today, log_user_event, read_daily_log, read_user_log

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Конфігурація
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
# Контакти менеджера (за вимогою — фіксовані в коді)
MANAGER_USERNAME = "byteflowmanager"
MANAGER_PHONE = "+380689630697"

GOOGLE_SHEETS_ID = os.getenv(
    "GOOGLE_SHEETS_ID",
    "1FGfZE_wNwBhpVjBgnyR1bL3OSE3sqxnM66xJil_ZsUc",
).strip()
GOOGLE_SHEETS_TAB = os.getenv("GOOGLE_SHEETS_TAB", "Orders").strip()
GOOGLE_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_CREDENTIALS_FILE",
    "byteflow-498010-1233c24e8f31.json",
).strip()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
DEEPSEEK_API_BASE = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
DEEPSEEK_PROMPT_FILE = os.getenv(
    "DEEPSEEK_PROMPT_FILE",
    r"c:\Users\Admin\Downloads\byteflow_deepseek_system_prompt_1.md",
).strip()

sheets_orders: GoogleSheetsOrders | None = None
deepseek_system_prompt = ""
_active_ai_users: set[int] = set()
MAX_AI_HISTORY = 20
_dashboard_task: asyncio.Task[None] | None = None

async def answer_clean(message: Message, text: str, **kwargs: Any) -> Message:
    return await message.answer(text, **kwargs)


async def answer_photo_clean(message: Message, photo: FSInputFile, caption: str, **kwargs: Any) -> Message:
    return await message.answer_photo(photo, caption=caption, **kwargs)

if not BOT_TOKEN:
    print(
        "ПОМИЛКА: BOT_TOKEN не задано. Створіть файл .env на основі .env.example.",
        file=sys.stderr,
    )
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("byteflow.bot")

router = Router(name="byteflow")


# ---------------------------------------------------------------------------
# Доменні моделі
# ---------------------------------------------------------------------------


class ServiceType(str, Enum):
    TELEGRAM_BOT = "telegram_bot"
    AI_AGENTS = "ai_agents"
    WEBSITE_CRM = "website_crm"


class PaymentStatus(str, Enum):
    PENDING = "Очікує оплати"
    PAID = "💰 ОПЛАЧЕНО"


@dataclass(frozen=True)
class ServiceCatalogItem:
    key: ServiceType
    title: str
    short_title: str
    base_cost_uah: int
    base_days_min: int
    base_days_max: int
    description: str


SERVICE_CATALOG: dict[ServiceType, ServiceCatalogItem] = {
    ServiceType.TELEGRAM_BOT: ServiceCatalogItem(
        key=ServiceType.TELEGRAM_BOT,
        title="Telegram-бот на aiogram",
        short_title="Telegram-бот",
        base_cost_uah=12_000,
        base_days_min=1,
        base_days_max=10,
        description=(
            "Розумний бот: воронка продажів, підтримка 24/7, інтеграції з CRM та БД "
            "(PostgreSQL, Redis, Google Sheets)."
        ),
    ),
    ServiceType.AI_AGENTS: ServiceCatalogItem(
        key=ServiceType.AI_AGENTS,
        title="Інтеграція ШІ-агентів",
        short_title="ШІ-агенти",
        base_cost_uah=6_000,
        base_days_min=2,
        base_days_max=10,
        description=(
            "ChatGPT / Claude у ваших процесах: RAG, workflow-тригери, контроль токенів, "
            "ескалація на менеджера."
        ),
    ),
    ServiceType.WEBSITE_CRM: ServiceCatalogItem(
        key=ServiceType.WEBSITE_CRM,
        title="Створення сайту + CRM",
        short_title="Сайт + CRM",
        base_cost_uah=15_000,
        base_days_min=1,
        base_days_max=10,
        description=(
            "B2B-лендінг під ключ, CRM-воронки, дашборди, інтеграція з ботом та аналітикою."
        ),
    ),
}


@dataclass
class BudgetEstimate:
    base_cost_uah: int
    adjusted_cost_uah: int
    complexity_label: str
    days_min: int
    days_max: int
    timeline_text: str

    @property
    def cost_range_text(self) -> str:
        low = self.adjusted_cost_uah
        high = int(self.adjusted_cost_uah * 1.25)
        return f"{low:,} – {high:,} грн".replace(",", " ")


@dataclass
class Order:
    order_id: str
    user_id: int
    username: str | None
    client_full_name: str
    phone: str
    business_name: str
    automation_description: str
    budget: str
    payment_status: PaymentStatus = PaymentStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    paid_at: datetime | None = None
    invoice_message_id: int | None = None

    @property
    def full_name(self) -> str:
        return self.client_full_name

    def to_admin_payload(self, event: str) -> str:
        paid_line = (
            f"Оплачено: {self.paid_at.astimezone().strftime('%d.%m.%Y %H:%M')}"
            if self.paid_at
            else "Оплачено: —"
        )
        username_line = f"@{self.username}" if self.username else "(немає username)"
        return (
            "\n"
            "╔══════════════════════════════════════════════════════════╗\n"
            f"║  BYTEFLOW · СПОВІЩЕННЯ АДМІНІСТРАТОРА · {event:<18} ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            f"║  Замовлення:     {self.order_id:<40} ║\n"
            f"║  Клієнт:         {self.client_full_name[:40]:<40} ║\n"
            f"║  Телефон:        {self.phone[:40]:<40} ║\n"
            f"║  Username:       {username_line[:40]:<40} ║\n"
            f"║  Telegram ID:    {str(self.user_id):<40} ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            f"║  Бізнес:         {self.business_name[:40]:<40} ║\n"
            "╠══════════════════════════════════════════════════════════╣\n"
            "║  Що автоматизувати:                                       ║\n"
            + _wrap_multiline(self.automation_description, width=58, prefix="║  ")
            + "╠══════════════════════════════════════════════════════════╣\n"
            f"║  Бюджет:         {self.budget[:40]:<40} ║\n"
            f"║  Статус оплати:  {self.payment_status.value[:40]:<40} ║\n"
            f"║  {paid_line[:58]:<58} ║\n"
            + "╚══════════════════════════════════════════════════════════╝\n"
        )


# Реєстр активних замовлень (у production — PostgreSQL / Redis)
_orders: dict[str, Order] = {}


def _wrap_multiline(text: str, width: int = 58, prefix: str = "║  ") -> str:
    words = text.replace("\r", "").split()
    if not words:
        return f"{prefix}(порожньо)\n"
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word[:width]
    if current:
        lines.append(current)
    return "".join(f"{prefix}{line}\n" for line in lines)


def generate_order_id() -> str:
    date_part = datetime.now().strftime("%Y%m%d")
    suffix = secrets.token_hex(2).upper()
    return f"BF-{date_part}-{suffix}"


def estimate_budget(service: ServiceCatalogItem, tz_text: str) -> BudgetEstimate:
    length = len(tz_text.strip())
    if length < 80:
        multiplier = 1.0
        complexity = "Базова"
        extra_days = 0
    elif length < 250:
        multiplier = 1.15
        complexity = "Середня"
        extra_days = 5
    elif length < 500:
        multiplier = 1.35
        complexity = "Розширена"
        extra_days = 10
    else:
        multiplier = 1.55
        complexity = "Enterprise"
        extra_days = 15

    adjusted = int(service.base_cost_uah * multiplier)
    days_min = service.base_days_min + extra_days
    days_max = service.base_days_max + extra_days
    timeline = f"{days_min}–{days_max} робочих днів"

    return BudgetEstimate(
        base_cost_uah=service.base_cost_uah,
        adjusted_cost_uah=adjusted,
        complexity_label=complexity,
        days_min=days_min,
        days_max=days_max,
        timeline_text=timeline,
    )


async def notify_admin_console(order: Order, event: str) -> None:
    payload = order.to_admin_payload(event)
    logger.info("%s", payload)


def format_admin_telegram_order(order: Order) -> str:
    username_line = f"@{order.username}" if order.username else "—"
    tg_link = (
        f"<a href='tg://user?id={order.user_id}'>Написати клієнту</a>"
        if order.user_id
        else "—"
    )
    automation = order.automation_description
    if len(automation) > 800:
        automation = automation[:800] + "…"

    return (
        "<b>🆕 Нове замовлення ByteFlow</b>\n\n"
        f"<b>ID:</b> <code>{order.order_id}</code>\n"
        f"<b>Дата:</b> {order.created_at.astimezone().strftime('%d.%m.%Y %H:%M')}\n\n"
        f"<b>👤 Клієнт:</b> {order.client_full_name}\n"
        f"<b>📞 Телефон:</b> {order.phone}\n"
        f"<b>✈️ Telegram:</b> {username_line}\n"
        f"<b>🆔 Telegram ID:</b> <code>{order.user_id}</code>\n"
        f"{tg_link}\n\n"
        f"<b>🏢 Бізнес:</b> {order.business_name}\n\n"
        f"<b>⚙️ Що автоматизувати:</b>\n{automation}\n\n"
        f"<b>💰 Бюджет:</b> {order.budget}\n"
        f"<b>💳 Статус оплати:</b> {order.payment_status.value}"
    )


async def notify_admin_new_order(bot: Bot, order: Order) -> None:
    await notify_admin_console(order, "НОВЕ ЗАМОВЛЕННЯ")

    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задано — Telegram-сповіщення адміну пропущено.")
        return

    try:
        await bot.send_message(
            ADMIN_ID,
            format_admin_telegram_order(order),
            disable_web_page_preview=True,
        )
        logger.info("Сповіщення про замовлення %s надіслано адміну (ID %s).", order.order_id, ADMIN_ID)
    except TelegramAPIError:
        logger.exception(
            "Не вдалося надіслати Telegram-сповіщення адміну (ID %s). "
            "Переконайтесь, що ви натиснули /start у боті.",
            ADMIN_ID,
        )


def format_money(amount: int) -> str:
    return f"{amount:,} грн".replace(",", " ")


def format_invoice(order: Order) -> str:
    status_emoji = "⏳" if order.payment_status == PaymentStatus.PENDING else "✅"
    username_line = f"@{order.username}" if order.username else "—"
    return (
        f"<b>🧾 Цифровий рахунок ByteFlow</b>\n\n"
        f"<b>ID замовлення:</b> <code>{order.order_id}</code>\n"
        f"<b>Клієнт:</b> {order.client_full_name}\n"
        f"<b>Телефон:</b> {order.phone}\n"
        f"<b>Telegram:</b> {username_line}\n"
        f"<b>Бізнес:</b> {order.business_name}\n"
        f"<b>Що автоматизувати:</b>\n{order.automation_description[:400]}"
        f"{'…' if len(order.automation_description) > 400 else ''}\n\n"
        f"<b>Бюджет клієнта:</b> {order.budget}\n\n"
        f"<b>Статус оплати:</b> {status_emoji} <b>{order.payment_status.value}</b>\n"
        f"<i>Дата створення: {order.created_at.astimezone().strftime('%d.%m.%Y %H:%M')}</i>"
    )


def resolve_menu_photo() -> Path | None:
    for name in ("menu.jpg", "menu.png", "menu.jpeg", "menu.webp", "menu.JPG", "menu.PNG"):
        path = BASE_DIR / name
        if path.is_file():
            return path
    return None


def validate_full_name(value: str) -> bool:
    parts = value.strip().split()
    return len(value.strip()) >= 3 and len(parts) >= 2


def validate_phone(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    return 10 <= len(digits) <= 15


def telegram_username_label(user_id: int, username: str | None) -> str:
    return f"@{username}" if username else f"ID {user_id} (без @username)"


def load_deepseek_prompt() -> str:
    prompt_path = Path(DEEPSEEK_PROMPT_FILE)
    if not prompt_path.is_file():
        logger.warning("Файл системного промпту DeepSeek не знайдено: %s", prompt_path)
        return (
            "Ти AI-менеджер ByteFlow. Відповідай українською коротко та по суті, "
            "допомагай із консультацією щодо послуг ByteFlow."
        )
    try:
        text = prompt_path.read_text(encoding="utf-8").strip()
        return text or "Ти AI-менеджер ByteFlow. Відповідай українською."
    except OSError:
        logger.exception("Не вдалося прочитати файл промпту DeepSeek: %s", prompt_path)
        return "Ти AI-менеджер ByteFlow. Відповідай українською."


async def deepseek_chat(history: list[dict[str, str]]) -> str:
    if not DEEPSEEK_API_KEY:
        return "Ключ DeepSeek API не налаштований. Напишіть менеджеру, щоб активувати консультацію."

    trimmed = history[-MAX_AI_HISTORY:] if len(history) > MAX_AI_HISTORY else history
    messages = [{"role": "system", "content": deepseek_system_prompt}, *trimmed]

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{DEEPSEEK_API_BASE}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=45)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, headers=headers, json=payload) as response:
                data = await response.json(content_type=None)
                if response.status >= 400:
                    logger.error("DeepSeek API error %s: %s", response.status, data)
                    return "Сервіс консультації тимчасово недоступний. Спробуйте ще раз за хвилину."
    except asyncio.TimeoutError:
        return "Сервіс консультації не відповів вчасно. Спробуйте ще раз."
    except aiohttp.ClientError:
        logger.exception("Помилка з'єднання з DeepSeek API")
        return "Помилка підключення до сервісу консультації. Спробуйте трохи пізніше."

    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return "Не вдалося отримати відповідь від ШІ. Спробуйте поставити питання ще раз."
    content = (((choices[0] or {}).get("message") or {}).get("content") or "").strip()
    if not content:
        return "ШІ не повернув текст відповіді. Спробуйте ще раз."
    return content


def new_ai_session_id() -> str:
    return f"AI-{datetime.now().strftime('%Y%m%d')}-{secrets.token_hex(2).upper()}"


async def log_from_message(
    message: Message,
    event: str,
    details: str = "",
    **extra: Any,
) -> None:
    user = message.from_user
    if not user:
        return
    name = user_display_name(message)
    await log_user_event(
        user_id=user.id,
        username=user.username,
        full_name=name,
        event=event,
        details=details,
        extra=extra or None,
    )
    dashboard.touch_user(user.id)


def leave_ai_consult(user_id: int | None) -> None:
    if user_id is not None:
        _active_ai_users.discard(user_id)
    dashboard.set_active(ai_chats=len(_active_ai_users), fsm_users=0)


class ActivityMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[..., Any],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user and event.text:
            dashboard.inc("message", event.from_user.id)
        return await handler(event, data)


class AdminFilter:
    @staticmethod
    def check(message: Message) -> bool:
        return bool(message.from_user and message.from_user.id == ADMIN_ID and ADMIN_ID > 0)


# ---------------------------------------------------------------------------
# FSM — замовлення
# ---------------------------------------------------------------------------


class OrderStates(StatesGroup):
    entering_full_name = State()
    entering_phone = State()
    entering_business = State()
    entering_automation = State()
    entering_budget = State()
    confirming_order = State()


# ---------------------------------------------------------------------------
# FSM — безкоштовний AI-аудит (квіз)
# ---------------------------------------------------------------------------


class AuditStates(StatesGroup):
    q1_business_type = State()
    q2_main_pain = State()
    q3_team_size = State()
    q4_current_tools = State()
    q5_priority = State()


class ManagerStates(StatesGroup):
    waiting_message = State()


class AIConsultStates(StatesGroup):
    waiting_question = State()


# ---------------------------------------------------------------------------
# Клавіатури
# ---------------------------------------------------------------------------

BTN_ORDER = "🛒 Оформити замовлення"
BTN_AUDIT = "🤖 Безкоштовний AI-аудит"
BTN_SERVICES = "💼 Наші послуги та ціни"
BTN_MANAGER = "📞 Зв'язатися з менеджером"
BTN_AI_CONSULT = "🧠 Консультація з ШІ"
BTN_CANCEL = "❌ Скасувати"
BTN_MAIN_MENU = "🏠 Головне меню"
BTN_CONFIRM_ORDER = "✅ Підтвердити замовлення"
BTN_EDIT_ORDER = "✏️ Змінити дані"
BTN_BACK_SERVICES = "🔙 До послуг"

MAIN_MENU_BUTTONS = [BTN_ORDER, BTN_AUDIT, BTN_SERVICES, BTN_MANAGER, BTN_AI_CONSULT]


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_ORDER)],
            [KeyboardButton(text=BTN_AUDIT), KeyboardButton(text=BTN_SERVICES)],
            [KeyboardButton(text=BTN_AI_CONSULT)],
            [KeyboardButton(text=BTN_MANAGER)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Оберіть дію з меню…",
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)], [KeyboardButton(text=BTN_MAIN_MENU)]],
        resize_keyboard=True,
    )


def order_confirm_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_CONFIRM_ORDER)],
            [KeyboardButton(text=BTN_EDIT_ORDER)],
            [KeyboardButton(text=BTN_CANCEL), KeyboardButton(text=BTN_MAIN_MENU)],
        ],
        resize_keyboard=True,
    )


def payment_keyboard(order_id: str, paid: bool = False) -> InlineKeyboardMarkup | None:
    # За вимогою: кнопку «Оплатити» прибрано.
    # Рахунок залишається інформаційним, а оплату узгоджує менеджер.
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 Головне меню", callback_data="go_main")]]
    )


def services_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Telegram-боти",
                    callback_data="svc_info:telegram_bot",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✨ ШІ-агенти",
                    callback_data="svc_info:ai_agents",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🌐 Сайт + CRM",
                    callback_data="svc_info:website_crm",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛒 Оформити замовлення",
                    callback_data="svc_order",
                )
            ],
        ]
    )


def audit_q1_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="E-commerce / ритейл", callback_data="audit_q1:ecom")],
            [InlineKeyboardButton(text="B2B-послуги", callback_data="audit_q1:b2b")],
            [InlineKeyboardButton(text="Освіта / курси", callback_data="audit_q1:edu")],
            [InlineKeyboardButton(text="Медицина / клініка", callback_data="audit_q1:med")],
            [InlineKeyboardButton(text="Інше", callback_data="audit_q1:other")],
            [InlineKeyboardButton(text="❌ Скасувати аудит", callback_data="audit_cancel")],
        ]
    )


def audit_q2_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Втрата лідів", callback_data="audit_q2:leads")],
            [InlineKeyboardButton(text="Повільна підтримка", callback_data="audit_q2:support")],
            [InlineKeyboardButton(text="Хаос у даних / CRM", callback_data="audit_q2:data")],
            [InlineKeyboardButton(text="Рутина менеджерів", callback_data="audit_q2:routine")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="audit_cancel")],
        ]
    )


def audit_q3_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="1–5 осіб", callback_data="audit_q3:small")],
            [InlineKeyboardButton(text="6–20 осіб", callback_data="audit_q3:medium")],
            [InlineKeyboardButton(text="21+ осіб", callback_data="audit_q3:large")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="audit_cancel")],
        ]
    )


def audit_q4_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Excel / Google Sheets", callback_data="audit_q4:sheets")],
            [InlineKeyboardButton(text="CRM (HubSpot, Pipedrive…)", callback_data="audit_q4:crm")],
            [InlineKeyboardButton(text="Telegram / месенджери", callback_data="audit_q4:chat")],
            [InlineKeyboardButton(text="Майже нічого", callback_data="audit_q4:none")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="audit_cancel")],
        ]
    )


def audit_q5_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Telegram-бот", callback_data="audit_q5:bot")],
            [InlineKeyboardButton(text="ШІ-агент", callback_data="audit_q5:ai")],
            [InlineKeyboardButton(text="Сайт + CRM", callback_data="audit_q5:web")],
            [InlineKeyboardButton(text="Потрібна консультація", callback_data="audit_q5:consult")],
            [InlineKeyboardButton(text="❌ Скасувати", callback_data="audit_cancel")],
        ]
    )


# ---------------------------------------------------------------------------
# Тексти
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "<b>👋 Вітаємо в ByteFlow!</b>\n\n"
    "Ми — digital-агентство для B2B: <b>розумні Telegram-боти</b>, "
    "<b>інтеграція ШІ-агентів</b> та <b>сайти з CRM</b>.\n\n"
    "Оберіть дію в меню нижче:\n"
    f"• <b>{BTN_ORDER}</b> — автоматичне оформлення та оплата\n"
    f"• <b>{BTN_AUDIT}</b> — безкоштовний AI-аудит за 5 кроків\n"
    f"• <b>{BTN_SERVICES}</b> — послуги та орієнтовні ціни\n"
    f"• <b>{BTN_AI_CONSULT}</b> — швидкі відповіді від AI-менеджера\n"
    f"• <b>{BTN_MANAGER}</b> — прямий контакт з менеджером"
)

SERVICES_OVERVIEW = (
    "<b>💼 Послуги та орієнтовні ціни ByteFlow</b>\n\n"
    "<b>1. Telegram-бот на aiogram</b>\n"
    f"   від {format_money(SERVICE_CATALOG[ServiceType.TELEGRAM_BOT].base_cost_uah)} · "
    f"{SERVICE_CATALOG[ServiceType.TELEGRAM_BOT].base_days_min}–"
    f"{SERVICE_CATALOG[ServiceType.TELEGRAM_BOT].base_days_max} днів\n\n"
    "<b>2. Інтеграція ШІ-агентів</b>\n"
    f"   від {format_money(SERVICE_CATALOG[ServiceType.AI_AGENTS].base_cost_uah)} · "
    f"{SERVICE_CATALOG[ServiceType.AI_AGENTS].base_days_min}–"
    f"{SERVICE_CATALOG[ServiceType.AI_AGENTS].base_days_max} днів\n\n"
    "<b>3. Сайт + CRM</b>\n"
    f"   від {format_money(SERVICE_CATALOG[ServiceType.WEBSITE_CRM].base_cost_uah)} · "
    f"{SERVICE_CATALOG[ServiceType.WEBSITE_CRM].base_days_min}–"
    f"{SERVICE_CATALOG[ServiceType.WEBSITE_CRM].base_days_max} днів\n\n"
    "<i>Точна вартість залежить від ТЗ — натисніть кнопку послуги для деталей "
    "або оформіть замовлення.</i>"
)

MANAGER_TEXT = (
    f"<b>📞 Зв'язок з менеджером ByteFlow</b>\n\n"
    f"Telegram: <a href='https://t.me/{MANAGER_USERNAME}'>@{MANAGER_USERNAME}</a>\n"
    f"Телефон: <b>{MANAGER_PHONE}</b>\n\n"
    "Або залиште коротке повідомлення тут — ми передамо його менеджеру "
    "разом із вашим Telegram-профілем.\n\n"
    "<i>Напишіть текст повідомлення або натисніть «Головне меню».</i>"
)

AUDIT_LABELS: dict[str, dict[str, str]] = {
    "q1": {
        "ecom": "E-commerce / ритейл",
        "b2b": "B2B-послуги",
        "edu": "Освіта / курси",
        "med": "Медицина / клініка",
        "other": "Інше",
    },
    "q2": {
        "leads": "Втрата лідів",
        "support": "Повільна підтримка",
        "data": "Хаос у даних / CRM",
        "routine": "Рутина менеджерів",
    },
    "q3": {
        "small": "1–5 осіб",
        "medium": "6–20 осіб",
        "large": "21+ осіб",
    },
    "q4": {
        "sheets": "Excel / Google Sheets",
        "crm": "CRM (HubSpot, Pipedrive…)",
        "chat": "Telegram / месенджери",
        "none": "Майже нічого",
    },
    "q5": {
        "bot": "Telegram-бот",
        "ai": "ШІ-агент",
        "web": "Сайт + CRM",
        "consult": "Потрібна консультація",
    },
}


# ---------------------------------------------------------------------------
# Допоміжні функції UX
# ---------------------------------------------------------------------------


async def send_main_menu(
    target: Message,
    text: str = WELCOME_TEXT,
    *,
    edit: bool = False,
) -> None:
    if edit:
        try:
            await target.edit_text(text, reply_markup=None)
        except TelegramBadRequest:
            pass

    photo_path = resolve_menu_photo()
    if photo_path:
        await answer_photo_clean(
            target,
            FSInputFile(photo_path),
            text,
            reply_markup=main_menu_keyboard(),
        )
    else:
        await answer_clean(target, text, reply_markup=main_menu_keyboard())


async def clear_state_and_menu(message: Message, state: FSMContext, notice: str | None = None) -> None:
    if message.from_user:
        leave_ai_consult(message.from_user.id)
    await state.clear()
    text = notice if notice else "🏠 <b>Головне меню</b>"
    await send_main_menu(message, text)


def user_display_name(message: Message) -> str:
    user = message.from_user
    if not user:
        return "Невідомий клієнт"
    parts = [user.first_name or "", user.last_name or ""]
    name = " ".join(p for p in parts if p).strip()
    return name or (user.username or f"ID {user.id}")


def service_detail_text(item: ServiceCatalogItem) -> str:
    return (
        f"<b>{item.title}</b>\n\n"
        f"{item.description}\n\n"
        f"<b>Орієнтовна вартість:</b> від {format_money(item.base_cost_uah)}\n"
        f"<b>Термін:</b> {item.base_days_min}–{item.base_days_max} робочих днів\n\n"
        "<i>Натисніть «Оформити замовлення» для автоматичного розрахунку під ваше ТЗ.</i>"
    )


def build_audit_report(data: dict[str, str], user: Message) -> str:
    recommendation_map = {
        "bot": "🤖 <b>Рекомендація:</b> Telegram-бот на aiogram для воронки та підтримки 24/7.",
        "ai": "✨ <b>Рекомендація:</b> інтеграція ШІ-агента з RAG та ескалацією на менеджера.",
        "web": "🌐 <b>Рекомендація:</b> лендінг + CRM як єдине джерело правди про ліди.",
        "consult": "📞 <b>Рекомендація:</b> безкоштовна консультація 30 хв — узгодимо стек і KPI.",
    }
    priority = data.get("q5", "consult")
    rec = recommendation_map.get(priority, recommendation_map["consult"])

    score = 0
    if data.get("q2") in ("leads", "support"):
        score += 2
    if data.get("q3") in ("medium", "large"):
        score += 1
    if data.get("q4") in ("sheets", "none"):
        score += 2
    urgency = "🔴 Високий" if score >= 3 else "🟡 Середній" if score >= 1 else "🟢 Помірний"

    return (
        "<b>🤖 Результат безкоштовного AI-аудиту ByteFlow</b>\n\n"
        f"<b>Сфера:</b> {AUDIT_LABELS['q1'].get(data.get('q1', ''), '—')}\n"
        f"<b>Головний біль:</b> {AUDIT_LABELS['q2'].get(data.get('q2', ''), '—')}\n"
        f"<b>Команда:</b> {AUDIT_LABELS['q3'].get(data.get('q3', ''), '—')}\n"
        f"<b>Поточні інструменти:</b> {AUDIT_LABELS['q4'].get(data.get('q4', ''), '—')}\n"
        f"<b>Пріоритет:</b> {AUDIT_LABELS['q5'].get(data.get('q5', ''), '—')}\n\n"
        f"<b>Потенціал автоматизації:</b> {urgency}\n\n"
        f"{rec}\n\n"
        "Наступний крок: оформіть замовлення через меню або зв'яжіться з менеджером — "
        "ми підготуємо детальну пропозицію протягом 2 годин у робочий час."
    )


async def log_audit_to_admin(message: Message, data: dict[str, str]) -> None:
    username = message.from_user.username if message.from_user else None
    username_line = f"@{username}" if username else "(немає username)"
    payload = (
        "\n"
        "╔══════════════════════════════════════════════════════════╗\n"
        "║  BYTEFLOW · НОВИЙ AI-АУДИТ (КВІЗ)                       ║\n"
        "╠══════════════════════════════════════════════════════════╣\n"
        f"║  Клієнт:      {user_display_name(message)[:42]:<42} ║\n"
        f"║  Username:    {username_line[:42]:<42} ║\n"
        f"║  Telegram ID: {str(message.from_user.id if message.from_user else 0):<42} ║\n"
        "╠══════════════════════════════════════════════════════════╣\n"
        f"║  Сфера:       {AUDIT_LABELS['q1'].get(data.get('q1', ''), '—')[:42]:<42} ║\n"
        f"║  Біль:        {AUDIT_LABELS['q2'].get(data.get('q2', ''), '—')[:42]:<42} ║\n"
        f"║  Команда:     {AUDIT_LABELS['q3'].get(data.get('q3', ''), '—')[:42]:<42} ║\n"
        f"║  Інструменти: {AUDIT_LABELS['q4'].get(data.get('q4', ''), '—')[:42]:<42} ║\n"
        f"║  Пріоритет:   {AUDIT_LABELS['q5'].get(data.get('q5', ''), '—')[:42]:<42} ║\n"
        "╚══════════════════════════════════════════════════════════╝\n"
    )
    logger.info("%s", payload)


# ---------------------------------------------------------------------------
# Обробники: старт і головне меню
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    if message.from_user:
        leave_ai_consult(message.from_user.id)
    await state.clear()
    await log_from_message(message, "Старт /start")
    await send_main_menu(message, WELCOME_TEXT)


@router.message(Command("menu"))
@router.message(F.text == BTN_MAIN_MENU)
async def cmd_menu(message: Message, state: FSMContext) -> None:
    await clear_state_and_menu(message, state)


@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await answer_clean(message, "Немає активного процесу для скасування.", reply_markup=main_menu_keyboard())
        return
    await clear_state_and_menu(
        message,
        state,
        "❌ Дію скасовано. Повертаємось до головного меню.",
    )


@router.callback_query(F.data == "go_main")
async def cb_go_main(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await state.clear()
    if callback.message:
        await send_main_menu(callback.message, "🏠 <b>Головне меню</b>")


# ---------------------------------------------------------------------------
# Послуги та ціни
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_SERVICES)
async def show_services(message: Message, state: FSMContext) -> None:
    await state.clear()
    await answer_clean(
        message,
        SERVICES_OVERVIEW,
        reply_markup=services_inline_keyboard(),
    )


@router.callback_query(F.data.startswith("svc_info:"))
async def cb_service_info(callback: CallbackQuery) -> None:
    await callback.answer()
    if not callback.data or not callback.message:
        return
    key_str = callback.data.split(":", 1)[1]
    try:
        service_key = ServiceType(key_str)
    except ValueError:
        await callback.message.answer("⚠️ Невідома послуга.")
        return
    item = SERVICE_CATALOG[service_key]
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Оформити замовлення", callback_data="svc_order")],
            [InlineKeyboardButton(text="🔙 Назад до списку", callback_data="svc_back")],
        ]
    )
    await callback.message.answer(service_detail_text(item), reply_markup=kb)


@router.callback_query(F.data == "svc_back")
async def cb_services_back(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            SERVICES_OVERVIEW,
            reply_markup=services_inline_keyboard(),
        )


@router.callback_query(F.data == "svc_order")
async def cb_services_to_order(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if callback.message:
        await start_order_flow(callback.message, state)


# ---------------------------------------------------------------------------
# Зв'язок з менеджером
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_MANAGER)
async def contact_manager(message: Message, state: FSMContext) -> None:
    await state.set_state(ManagerStates.waiting_message)
    await answer_clean(message, MANAGER_TEXT, reply_markup=cancel_keyboard())


@router.message(F.text == BTN_AI_CONSULT)
async def start_ai_consult(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user:
        leave_ai_consult(user.id)

    session_id = new_ai_session_id()
    await state.set_state(AIConsultStates.waiting_question)
    await state.update_data(ai_history=[], ai_session_id=session_id)

    if user:
        _active_ai_users.add(user.id)
        dashboard.inc("ai_session", user.id)
        dashboard.set_active(ai_chats=len(_active_ai_users), fsm_users=0)
        await log_from_message(
            message,
            "AI консультація: новий чат",
            extra={"session": session_id},
        )

    await answer_clean(
        message,
        "<b>🧠 Консультація з ШІ ByteFlow</b>\n\n"
        f"Новий діалог: <code>{session_id}</code>\n"
        "Пам'ять увімкнена в межах цієї сесії.\n\n"
        "Напишіть питання про послуги, автоматизацію або запуск проєкту.\n"
        "Щоб завершити — натисніть «Головне меню» (почнеться новий чат).",
        reply_markup=cancel_keyboard(),
    )


@router.message(ManagerStates.waiting_message, F.text.in_({BTN_CANCEL, BTN_MAIN_MENU}))
async def manager_cancel(message: Message, state: FSMContext) -> None:
    await clear_state_and_menu(message, state)


@router.message(ManagerStates.waiting_message, F.text)
async def manager_forward(message: Message, state: FSMContext) -> None:
    if message.text in MAIN_MENU_BUTTONS:
        await state.clear()
        if message.text == BTN_ORDER:
            await start_order_flow(message, state)
        elif message.text == BTN_AUDIT:
            await start_audit(message, state)
        elif message.text == BTN_SERVICES:
            await show_services(message, state)
        elif message.text == BTN_AI_CONSULT:
            await start_ai_consult(message, state)
        elif message.text == BTN_MANAGER:
            await contact_manager(message, state)
        return

    username = message.from_user.username if message.from_user else None
    username_line = f"@{username}" if username else "(немає username)"
    payload = (
        "\n"
        "╔══════════════════════════════════════════════════════════╗\n"
        "║  BYTEFLOW · ЗАПИТ ДО МЕНЕДЖЕРА                          ║\n"
        "╠══════════════════════════════════════════════════════════╣\n"
        f"║  Клієнт:      {user_display_name(message)[:42]:<42} ║\n"
        f"║  Username:    {username_line[:42]:<42} ║\n"
        f"║  Telegram ID: {str(message.from_user.id if message.from_user else 0):<42} ║\n"
        "╠══════════════════════════════════════════════════════════╣\n"
        "║  Повідомлення:                                            ║\n"
        + _wrap_multiline(message.text or "", width=58, prefix="║  ")
        + "╚══════════════════════════════════════════════════════════╝\n"
    )
    logger.info("%s", payload)
    await log_from_message(message, "Повідомлення менеджеру", details=(message.text or "")[:300])

    await state.clear()
    await answer_clean(
        message,
        "✅ <b>Дякуємо!</b> Ваше повідомлення передано менеджеру ByteFlow.\n"
        f"Також можете написати напряму: <a href='https://t.me/{MANAGER_USERNAME}'>@{MANAGER_USERNAME}</a>\n\n"
        f"Телефон: <b>{MANAGER_PHONE}</b>\n\n"
        "Відповімо протягом <b>2 годин</b> у робочий час.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(AIConsultStates.waiting_question, F.text.in_({BTN_CANCEL, BTN_MAIN_MENU}))
async def ai_consult_cancel(message: Message, state: FSMContext) -> None:
    await log_from_message(message, "AI консультація: завершено")
    await clear_state_and_menu(message, state)


@router.message(AIConsultStates.waiting_question, F.text)
async def ai_consult_answer(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await answer_clean(
            message,
            "Будь ласка, напишіть питання текстом.",
            reply_markup=cancel_keyboard(),
        )
        return
    if text in MAIN_MENU_BUTTONS:
        return

    data = await state.get_data()
    session_id = str(data.get("ai_session_id", "—"))
    history: list[dict[str, str]] = list(data.get("ai_history", []))
    history.append({"role": "user", "content": text})

    await log_from_message(
        message,
        "AI запит",
        details=text[:300],
        session=session_id,
        turn=len(history),
    )

    wait_msg = await message.answer("⏳ Обробляю запит через DeepSeek...")
    response = await deepseek_chat(history)
    history.append({"role": "assistant", "content": response})
    await state.update_data(ai_history=history)

    dashboard.inc("ai_message", message.from_user.id if message.from_user else None)
    await log_from_message(
        message,
        "AI відповідь",
        details=response[:300],
        session=session_id,
        turn=len(history),
    )

    with suppress(TelegramAPIError):
        await wait_msg.delete()
    await message.answer(response, reply_markup=cancel_keyboard())


# ---------------------------------------------------------------------------
# AI-аудит (квіз)
# ---------------------------------------------------------------------------


@router.message(F.text == BTN_AUDIT)
async def start_audit(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AuditStates.q1_business_type)
    await state.update_data(audit={})
    await answer_clean(
        message,
        "<b>🤖 Безкоштовний AI-аудит ByteFlow</b>\n\n"
        "Відповідайте на 5 коротких питань — ми сформуємо персональну рекомендацію.\n\n"
        "<b>Питання 1/5.</b> Яка сфера вашого бізнесу?",
        reply_markup=ReplyKeyboardRemove(),
    )
    # Окреме повідомлення з кнопками не надсилаємо, щоб чат залишався чистим.
    await answer_clean(message, "Оберіть варіант:", reply_markup=audit_q1_keyboard())


@router.callback_query(F.data == "audit_cancel")
async def audit_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Аудит скасовано")
    await state.clear()
    if callback.message:
        await callback.message.answer(
            "Аудит скасовано.",
            reply_markup=main_menu_keyboard(),
        )


@router.callback_query(AuditStates.q1_business_type, F.data.startswith("audit_q1:"))
async def audit_q1(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    audit = data.get("audit", {})
    audit["q1"] = value
    await state.update_data(audit=audit)
    await state.set_state(AuditStates.q2_main_pain)
    if callback.message:
        await callback.message.edit_text(
            "<b>Питання 2/5.</b> Що болить найбільше зараз?",
            reply_markup=audit_q2_keyboard(),
        )


@router.callback_query(AuditStates.q2_main_pain, F.data.startswith("audit_q2:"))
async def audit_q2(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    audit = data.get("audit", {})
    audit["q2"] = value
    await state.update_data(audit=audit)
    await state.set_state(AuditStates.q3_team_size)
    if callback.message:
        await callback.message.edit_text(
            "<b>Питання 3/5.</b> Скільки людей у команді продажів / підтримки?",
            reply_markup=audit_q3_keyboard(),
        )


@router.callback_query(AuditStates.q3_team_size, F.data.startswith("audit_q3:"))
async def audit_q3(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    audit = data.get("audit", {})
    audit["q3"] = value
    await state.update_data(audit=audit)
    await state.set_state(AuditStates.q4_current_tools)
    if callback.message:
        await callback.message.edit_text(
            "<b>Питання 4/5.</b> Які інструменти ви вже використовуєте?",
            reply_markup=audit_q4_keyboard(),
        )


@router.callback_query(AuditStates.q4_current_tools, F.data.startswith("audit_q4:"))
async def audit_q4(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data:
        return
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    audit = data.get("audit", {})
    audit["q4"] = value
    await state.update_data(audit=audit)
    await state.set_state(AuditStates.q5_priority)
    if callback.message:
        await callback.message.edit_text(
            "<b>Питання 5/5.</b> Що хочете автоматизувати в першу чергу?",
            reply_markup=audit_q5_keyboard(),
        )


@router.callback_query(AuditStates.q5_priority, F.data.startswith("audit_q5:"))
async def audit_q5_finish(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    if not callback.data or not callback.message:
        return
    value = callback.data.split(":", 1)[1]
    data = await state.get_data()
    audit: dict[str, str] = data.get("audit", {})
    audit["q5"] = value
    await state.clear()

    report = build_audit_report(audit, callback.message)
    await log_audit_to_admin(callback.message, audit)

    await callback.message.edit_text("✅ Аудит завершено!")
    await send_main_menu(callback.message, report)


# ---------------------------------------------------------------------------
# Система замовлень
# ---------------------------------------------------------------------------


def _order_cancel_filter(text: str | None) -> bool:
    return text in {BTN_CANCEL, BTN_MAIN_MENU}


async def start_order_flow(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if not user:
        await answer_clean(message, "⚠️ Не вдалося визначити користувача.")
        return

    await state.clear()
    await state.update_data(
        telegram_id=user.id,
        telegram_username=user.username,
    )
    await state.set_state(OrderStates.entering_full_name)

    tg_label = telegram_username_label(user.id, user.username)
    await answer_clean(
        message,
        "<b>🛒 Оформлення замовлення ByteFlow</b>\n\n"
        "<b>Крок 1 з 5.</b> Введіть ваше <b>ім'я та прізвище</b>:\n"
        "<i>Наприклад: Олена Петренко</i>\n\n"
        f"📎 Ваш Telegram автоматично буде додано до заявки: <b>{tg_label}</b>",
        reply_markup=cancel_keyboard(),
    )


@router.message(F.text == BTN_ORDER)
async def order_start(message: Message, state: FSMContext) -> None:
    await start_order_flow(message, state)


@router.message(OrderStates.entering_full_name, F.text.func(_order_cancel_filter))
@router.message(OrderStates.entering_phone, F.text.func(_order_cancel_filter))
@router.message(OrderStates.entering_business, F.text.func(_order_cancel_filter))
@router.message(OrderStates.entering_automation, F.text.func(_order_cancel_filter))
@router.message(OrderStates.entering_budget, F.text.func(_order_cancel_filter))
@router.message(OrderStates.confirming_order, F.text.in_({BTN_CANCEL, BTN_MAIN_MENU}))
async def order_flow_cancel(message: Message, state: FSMContext) -> None:
    await clear_state_and_menu(message, state, "Оформлення замовлення скасовано.")


@router.message(OrderStates.entering_full_name, F.text)
async def order_full_name(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in MAIN_MENU_BUTTONS:
        return
    if not validate_full_name(value):
        await answer_clean(
            message,
            "⚠️ Вкажіть ім'я та прізвище повністю (мінімум два слова).\n"
            "<i>Наприклад: Іван Коваленко</i>",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(client_full_name=value)
    await state.set_state(OrderStates.entering_phone)
    await answer_clean(
        message,
        "<b>Крок 2 з 5.</b> Введіть ваш <b>номер телефону</b>:\n"
        "<i>Наприклад: +380501234567</i>",
        reply_markup=cancel_keyboard(),
    )


@router.message(OrderStates.entering_phone, F.text)
async def order_phone(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in MAIN_MENU_BUTTONS:
        return
    if not validate_phone(value):
        await answer_clean(
            message,
            "⚠️ Некоректний номер. Введіть 10–15 цифр (можна з +380…).",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(phone=value)
    await state.set_state(OrderStates.entering_business)
    await answer_clean(
        message,
        "<b>Крок 3 з 5.</b> Як називається ваш <b>бізнес</b>?\n"
        "<i>Наприклад: Студія краси Luxe, ТОВ «АгроПлюс»</i>",
        reply_markup=cancel_keyboard(),
    )


@router.message(OrderStates.entering_business, F.text)
async def order_business(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in MAIN_MENU_BUTTONS:
        return
    if len(value) < 2:
        await answer_clean(
            message,
            "⚠️ Назва бізнесу занадто коротка.",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(business_name=value)
    await state.set_state(OrderStates.entering_automation)
    await answer_clean(
        message,
        "<b>Крок 4 з 5.</b> Опишіть, <b>що потрібно автоматизувати</b>:\n"
        "• які процеси зараз робляться вручну;\n"
        "• які канали (Telegram, сайт, CRM);\n"
        "• бажаний результат.\n\n"
        "<i>Мінімум 10 символів.</i>",
        reply_markup=cancel_keyboard(),
    )


@router.message(OrderStates.entering_automation, F.text)
async def order_automation(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in MAIN_MENU_BUTTONS:
        return
    if len(value) < 10:
        await answer_clean(
            message,
            "⚠️ Опишіть детальніше (мінімум 10 символів).",
            reply_markup=cancel_keyboard(),
        )
        return
    if len(value) > 4000:
        await answer_clean(
            message,
            "⚠️ Занадто довгий текст (макс. 4000 символів).",
            reply_markup=cancel_keyboard(),
        )
        return

    await state.update_data(automation_description=value)
    await state.set_state(OrderStates.entering_budget)
    await answer_clean(
        message,
        "<b>Крок 5 з 5.</b> Який у вас <b>орієнтовний бюджет</b> на проєкт?\n"
        "<i>Наприклад: 25 000 грн, до 50 000 грн, потрібна оцінка</i>",
        reply_markup=cancel_keyboard(),
    )


@router.message(OrderStates.entering_budget, F.text)
async def order_budget(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if value in MAIN_MENU_BUTTONS:
        return
    if len(value) < 2:
        await answer_clean(
            message,
            "⚠️ Вкажіть бюджет або напишіть «потрібна оцінка».",
            reply_markup=cancel_keyboard(),
        )
        return

    data = await state.get_data()
    await state.update_data(budget=value)
    await state.set_state(OrderStates.confirming_order)

    tg_label = telegram_username_label(
        data.get("telegram_id", 0),
        data.get("telegram_username"),
    )
    summary = (
        "<b>📋 Перевірте дані замовлення</b>\n\n"
        f"<b>Ім'я:</b> {data.get('client_full_name', '—')}\n"
        f"<b>Телефон:</b> {data.get('phone', '—')}\n"
        f"<b>Telegram:</b> {tg_label}\n"
        f"<b>Бізнес:</b> {data.get('business_name', '—')}\n"
        f"<b>Що автоматизувати:</b>\n{data.get('automation_description', '—')[:500]}"
        f"{'…' if len(data.get('automation_description', '')) > 500 else ''}\n\n"
        f"<b>Бюджет:</b> {value}\n\n"
        "Якщо все вірно — натисніть «✅ Підтвердити замовлення»."
    )
    await answer_clean(message, summary, reply_markup=order_confirm_keyboard())


@router.message(OrderStates.confirming_order, F.text == BTN_EDIT_ORDER)
async def order_edit(message: Message, state: FSMContext) -> None:
    await state.set_state(OrderStates.entering_full_name)
    await answer_clean(
        message,
        "✏️ Почнемо спочатку. Введіть <b>ім'я та прізвище</b>:",
        reply_markup=cancel_keyboard(),
    )


@router.message(OrderStates.confirming_order, F.text == BTN_CONFIRM_ORDER)
async def order_confirm(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    required = (
        "client_full_name",
        "phone",
        "business_name",
        "automation_description",
        "budget",
        "telegram_id",
    )
    if not all(data.get(key) for key in required):
        await clear_state_and_menu(message, state, "⚠️ Дані замовлення втрачено. Почніть спочатку.")
        return

    user = message.from_user
    if not user:
        await answer_clean(message, "⚠️ Не вдалося визначити користувача.")
        return

    order_id = generate_order_id()
    order = Order(
        order_id=order_id,
        user_id=int(data["telegram_id"]),
        username=data.get("telegram_username"),
        client_full_name=str(data["client_full_name"]),
        phone=str(data["phone"]),
        business_name=str(data["business_name"]),
        automation_description=str(data["automation_description"]),
        budget=str(data["budget"]),
    )
    _orders[order_id] = order
    await state.clear()

    sheets_ok = False
    if sheets_orders:
        try:
            await sheets_orders.append_order(
                order_id=order.order_id,
                full_name=order.client_full_name,
                phone=order.phone,
                telegram_username=order.username,
                telegram_id=order.user_id,
                business_name=order.business_name,
                automation=order.automation_description,
                budget=order.budget,
                payment_status=order.payment_status.value,
            )
            sheets_ok = True
        except Exception:
            logger.exception("Помилка запису замовлення %s у Google Sheets", order_id)

    invoice_text = format_invoice(order)
    sent = await answer_clean(
        message,
        invoice_text,
        reply_markup=payment_keyboard(order_id),
    )
    order.invoice_message_id = sent.message_id

    await notify_admin_new_order(message.bot, order)

    dashboard.inc("order", user.id)
    await log_from_message(
        message,
        "Замовлення створено",
        details=f"ID {order_id}, бізнес: {order.business_name}",
        budget=order.budget,
    )

    sheets_note = (
        "📊 Дані збережено в Google Sheets (вкладка Orders)."
        if sheets_ok
        else "⚠️ Не вдалося записати в Google Sheets. Спробуємо ще раз пізніше."
    )
    await answer_clean(
        message,
        "✅ <b>Замовлення створено!</b>\n\n"
        f"Номер: <code>{order_id}</code>\n"
        f"{sheets_note}\n\n"
        "Рахунок надіслано вище. Далі з вами зв'яжеться менеджер для узгодження деталей.",
        reply_markup=main_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Симуляція оплати
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("pay:"))
async def process_payment(callback: CallbackQuery) -> None:
    # За вимогою: кнопку оплати прибрано, тому цей сценарій вимкнений.
    await callback.answer("Оплата через бота вимкнена. Зв'яжіться з менеджером.", show_alert=True)
    return


# ---------------------------------------------------------------------------
# Захист від сторонніх повідомлень під час FSM
# ---------------------------------------------------------------------------


@router.message(StateFilter(OrderStates))
async def order_fallback(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    prompts = {
        OrderStates.entering_full_name.state: (
            "Введіть ім'я та прізвище текстом (мінімум два слова).",
            cancel_keyboard(),
        ),
        OrderStates.entering_phone.state: (
            "Введіть номер телефону (наприклад +380501234567).",
            cancel_keyboard(),
        ),
        OrderStates.entering_business.state: (
            "Введіть назву вашого бізнесу.",
            cancel_keyboard(),
        ),
        OrderStates.entering_automation.state: (
            "Опишіть текстом, що потрібно автоматизувати (мін. 10 символів).",
            cancel_keyboard(),
        ),
        OrderStates.entering_budget.state: (
            "Вкажіть орієнтовний бюджет або «потрібна оцінка».",
            cancel_keyboard(),
        ),
        OrderStates.confirming_order.state: (
            f"Натисніть «{BTN_CONFIRM_ORDER}» або «{BTN_EDIT_ORDER}».",
            order_confirm_keyboard(),
        ),
    }
    text, markup = prompts.get(
        current or "",
        ("Продовжіть оформлення або скасуйте дію.", cancel_keyboard()),
    )
    await message.answer(text, reply_markup=markup)


@router.message(StateFilter(AuditStates))
async def audit_fallback(message: Message) -> None:
    await message.answer("Оберіть відповідь кнопкою в повідомленні квізу або скасуйте аудит.")


@router.message(StateFilter(ManagerStates))
async def manager_fallback(message: Message) -> None:
    await message.answer(
        "Надішліть текст повідомлення для менеджера або натисніть «Скасувати».",
        reply_markup=cancel_keyboard(),
    )


@router.message(StateFilter(AIConsultStates))
async def ai_consult_fallback(message: Message) -> None:
    await answer_clean(
        message,
        "Надішліть текст питання для консультації або натисніть «Скасувати».",
        reply_markup=cancel_keyboard(),
    )


# ---------------------------------------------------------------------------
# Адмін-команди (тільки ADMIN_ID)
# ---------------------------------------------------------------------------


async def _send_long_text(message: Message, text: str) -> None:
    chunk_size = 3800
    for i in range(0, len(text), chunk_size):
        await message.answer(text[i : i + chunk_size])


@router.message(Command("admin"), F.func(AdminFilter.check))
async def cmd_admin(message: Message) -> None:
    await message.answer(
        "<b>🔐 Адмін-панель ByteFlow</b>\n\n"
        "/admin_status — live-статистика бота\n"
        "/admin_logs — логи за сьогодні\n"
        "/admin_user &lt;telegram_id&gt; — логи користувача\n"
        "/admin_users — список активних користувачів за сьогодні\n\n"
        "<i>Логи зберігаються локально в папці logs/ і не потрапляють у git.</i>"
    )


@router.message(Command("admin_status"), F.func(AdminFilter.check))
async def cmd_admin_status(message: Message) -> None:
    d = dashboard.to_dict()
    today = d["today"]
    live = d["live"]
    sysm = d["system"]
    err_lines = "\n".join(
        f"• {e['time']} [{e['source']}] {e['message']}" for e in d.get("last_errors", [])
    ) or "—"
    text = (
        "<b>📊 Статус ByteFlow Bot</b>\n\n"
        f"Оновлено: {d['updated_at']}\n"
        f"Uptime: {d['uptime_sec'] // 3600} год {(d['uptime_sec'] % 3600) // 60} хв\n"
        f"Статус: <b>{d['status']}</b>\n\n"
        f"<b>Сьогодні ({today['date']}):</b>\n"
        f"Користувачів: {today['users']}\n"
        f"Повідомлень: {today['messages']}\n"
        f"Замовлень: {today['orders']}\n"
        f"AI-сесій: {today['ai_sessions']}\n"
        f"AI-повідомлень: {today['ai_messages']}\n"
        f"Помилок: {today['errors']}\n\n"
        f"<b>Зараз:</b>\n"
        f"Активних AI-чатів: {live['active_ai_chats']}\n\n"
        f"<b>Система:</b>\n"
        f"CPU: {sysm['cpu_percent']}%\n"
        f"RAM: {sysm['ram_percent']}%\n"
        f"Процес бота: {sysm['process_mb']} MB\n\n"
        f"<b>Останні помилки:</b>\n{err_lines}"
    )
    await _send_long_text(message, text)


@router.message(Command("admin_logs"), F.func(AdminFilter.check))
async def cmd_admin_logs(message: Message) -> None:
    await _send_long_text(message, read_daily_log(limit=50))


@router.message(Command("admin_users"), F.func(AdminFilter.check))
async def cmd_admin_users(message: Message) -> None:
    users = list_active_users_today()
    if not users:
        await message.answer("Сьогодні активних користувачів у логах немає.")
        return
    await message.answer(
        "<b>👥 Користувачі за сьогодні:</b>\n" + "\n".join(f"• <code>{uid}</code>" for uid in users)
    )


@router.message(Command("admin_user"), F.func(AdminFilter.check))
async def cmd_admin_user(message: Message) -> None:
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().isdigit():
        await message.answer("Формат: /admin_user 123456789")
        return
    user_id = int(parts[1].strip())
    await _send_long_text(message, read_user_log(user_id, limit=50))


# ---------------------------------------------------------------------------
# Глобальний роутер головного меню (після спеціалізованих хендлерів)
# ---------------------------------------------------------------------------


@router.message(F.text.in_(MAIN_MENU_BUTTONS))
async def main_menu_routing(message: Message, state: FSMContext) -> None:
    """Резервний маршрутизатор, якщо кнопка не перехоплена вище."""
    text = message.text
    if text == BTN_ORDER:
        await start_order_flow(message, state)
    elif text == BTN_AUDIT:
        await start_audit(message, state)
    elif text == BTN_SERVICES:
        await show_services(message, state)
    elif text == BTN_AI_CONSULT:
        await start_ai_consult(message, state)
    elif text == BTN_MANAGER:
        await contact_manager(message, state)


@router.message()
async def unknown_message(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        return
    await send_main_menu(
        message,
        "Не розумію команду. Оберіть дію з головного меню 👇",
    )


# ---------------------------------------------------------------------------
# Обробка помилок
# ---------------------------------------------------------------------------


@router.errors()
async def errors_handler(event: ErrorEvent) -> bool:
    err = event.exception
    logger.exception(
        "Необроблена помилка: update=%s exception=%s",
        event.update,
        err,
    )
    dashboard.add_error("bot", str(err))
    await dashboard.flush(force=True)

    update = event.update
    if update.message:
        try:
            await update.message.answer(
                "⚠️ Сталася технічна помилка. Спробуйте ще раз або оберіть «Головне меню».",
                reply_markup=main_menu_keyboard(),
            )
        except TelegramAPIError:
            pass
    elif update.callback_query and update.callback_query.message:
        try:
            await update.callback_query.message.answer(
                "⚠️ Сталася технічна помилка. Спробуйте ще раз.",
                reply_markup=main_menu_keyboard(),
            )
        except TelegramAPIError:
            pass
    return True


# ---------------------------------------------------------------------------
# Точка входу
# ---------------------------------------------------------------------------


async def dashboard_background() -> None:
    while True:
        try:
            dashboard.set_active(ai_chats=len(_active_ai_users), fsm_users=0)
            await dashboard.flush()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Помилка циклу dashboard")
        await asyncio.sleep(DASHBOARD_UPDATE_INTERVAL)


async def on_startup(bot: Bot) -> None:
    global sheets_orders, deepseek_system_prompt, _dashboard_task

    me = await bot.get_me()
    logger.info("ByteFlow бот запущено: @%s (id=%s)", me.username, me.id)

    deepseek_system_prompt = load_deepseek_prompt()
    if DEEPSEEK_API_KEY:
        logger.info("DeepSeek підключено: model=%s base=%s", DEEPSEEK_MODEL, DEEPSEEK_API_BASE)
    else:
        logger.warning("DEEPSEEK_API_KEY не задано — консультація з ШІ буде недоступна.")

    credentials_path = BASE_DIR / GOOGLE_CREDENTIALS_FILE
    try:
        sheets_orders = GoogleSheetsOrders(
            credentials_path=credentials_path,
            spreadsheet_id=GOOGLE_SHEETS_ID,
            worksheet_name=GOOGLE_SHEETS_TAB,
        )
        await sheets_orders.connect()
        logger.info(
            "Google Sheets підключено: таблиця %s, вкладка «%s»",
            GOOGLE_SHEETS_ID,
            GOOGLE_SHEETS_TAB,
        )
    except Exception:
        logger.exception(
            "Google Sheets недоступний. Перевірте JSON-ключ (%s) і доступ service account до таблиці.",
            credentials_path,
        )
        sheets_orders = None

    menu_photo = resolve_menu_photo()
    if menu_photo:
        logger.info("Фото меню: %s", menu_photo.name)
    else:
        logger.warning(
            "Файл menu.jpg / menu.png не знайдено в %s — меню без зображення.",
            BASE_DIR,
        )

    if ADMIN_ID:
        logger.info("ADMIN_ID налаштовано: %s", ADMIN_ID)
    else:
        logger.warning("ADMIN_ID не задано — сповіщення лише в консоль")

    await dashboard.flush(force=True)
    _dashboard_task = asyncio.create_task(dashboard_background())


async def on_shutdown(bot: Bot) -> None:
    global _dashboard_task
    if _dashboard_task:
        _dashboard_task.cancel()
        with suppress(asyncio.CancelledError):
            await _dashboard_task
    await dashboard.flush(force=True)
    logger.info("ByteFlow бот зупинено.")


async def main() -> None:
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)
    dp.message.middleware(ActivityMiddleware())
    dp.callback_query.middleware(ActivityMiddleware())

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.include_router(router)

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Зупинка за запитом користувача.")
