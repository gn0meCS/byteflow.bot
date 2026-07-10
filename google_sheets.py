"""
ByteFlow — запис замовлень у Google Sheets (вкладка Orders).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger("byteflow.sheets")

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

HEADERS: tuple[str, ...] = (
    "Дата і час",
    "ID замовлення",
    "Ім'я та прізвище",
    "Телефон",
    "Telegram",
    "Telegram ID",
    "Бізнес",
    "Що автоматизувати",
    "Бюджет",
    "Статус оплати",
)


class GoogleSheetsOrders:
    """Асинхронна обгортка над gspread (синхронний API через to_thread)."""

    def __init__(
        self,
        credentials_path: Path,
        spreadsheet_id: str,
        worksheet_name: str = "Orders",
    ) -> None:
        self._credentials_path = credentials_path
        self._spreadsheet_id = spreadsheet_id
        self._worksheet_name = worksheet_name
        self._worksheet: gspread.Worksheet | None = None

    def _get_worksheet(self) -> gspread.Worksheet:
        if self._worksheet is not None:
            return self._worksheet

        if not self._credentials_path.is_file():
            raise FileNotFoundError(
                f"JSON-ключ Google не знайдено: {self._credentials_path}"
            )

        creds = Credentials.from_service_account_file(
            str(self._credentials_path),
            scopes=list(SCOPES),
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(self._spreadsheet_id)

        try:
            worksheet = spreadsheet.worksheet(self._worksheet_name)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=self._worksheet_name,
                rows=1000,
                cols=len(HEADERS),
            )
            logger.info("Створено вкладку Google Sheets: %s", self._worksheet_name)

        self._ensure_headers(worksheet)
        self._worksheet = worksheet
        return worksheet

    @staticmethod
    def _ensure_headers(worksheet: gspread.Worksheet) -> None:
        try:
            first_row = worksheet.row_values(1)
        except Exception:
            first_row = []

        if first_row[: len(HEADERS)] != list(HEADERS):
            worksheet.update(
                range_name=f"A1:{chr(ord('A') + len(HEADERS) - 1)}1",
                values=[list(HEADERS)],
            )
            logger.info("Заголовки таблиці Orders оновлено.")

    def _append_order_sync(self, row: list[Any]) -> int:
        worksheet = self._get_worksheet()
        worksheet.append_row(row, value_input_option="USER_ENTERED")
        return len(worksheet.get_all_values())

    def _update_payment_sync(self, order_id: str, status: str) -> bool:
        worksheet = self._get_worksheet()
        records = worksheet.get_all_records()
        for index, record in enumerate(records, start=2):
            if str(record.get("ID замовлення", "")).strip() == order_id:
                status_col = len(HEADERS)
                worksheet.update_cell(index, status_col, status)
                return True
        return False

    async def connect(self) -> None:
        await asyncio.to_thread(self._get_worksheet)

    async def append_order(
        self,
        *,
        order_id: str,
        full_name: str,
        phone: str,
        telegram_username: str | None,
        telegram_id: int,
        business_name: str,
        automation: str,
        budget: str,
        payment_status: str,
    ) -> int:
        username_cell = f"@{telegram_username}" if telegram_username else "—"
        row = [
            datetime.now().strftime("%d.%m.%Y %H:%M"),
            order_id,
            full_name,
            phone,
            username_cell,
            str(telegram_id),
            business_name,
            automation,
            budget,
            payment_status,
        ]
        row_number = await asyncio.to_thread(self._append_order_sync, row)
        logger.info(
            "Замовлення %s записано в Google Sheets (рядок ~%s).",
            order_id,
            row_number,
        )
        return row_number

    async def update_payment_status(self, order_id: str, status: str) -> bool:
        updated = await asyncio.to_thread(
            self._update_payment_sync,
            order_id,
            status,
        )
        if updated:
            logger.info("Статус оплати %s оновлено в Sheets: %s", order_id, status)
        else:
            logger.warning("Замовлення %s не знайдено в Google Sheets.", order_id)
        return updated
