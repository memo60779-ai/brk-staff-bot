"""قراءة شيت الأسعار وتحويله لنص يفهمه Claude.

القواعد:
- الشيت المخفي (Hide sheet) = عرض قديم، البوت يتجاهله.
- العملة تنقرأ من تنسيق الخلية: إذا بيه $ فهو دولار، وإذا الرقم 10,000 وفوق بدون رمز فهو دينار.
- نسخة الزبون تنشال منها أي خلية تذكر عمولة الشركات قبل ما توصل لـ Claude.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

import openpyxl

TATWEEL = "ـ"
MAX_CELL_CHARS = 700
MAX_SHEET_CHARS = 18000
MIN_CELLS = 10  # شيت بيه أقل من هذا يعتبر فارغ

_COMMISSION = re.compile(r"عمول")
_TRAILING_JUNK = re.compile(r"[\s(\[\-–—:،,]+$")


def clean(text) -> str:
    """يشيل المدّات (ـ) والفراغات الزايدة."""
    return " ".join(str(text).replace(TATWEEL, "").split())


def _format_number(value, number_format: str) -> str:
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    nf = number_format or ""
    if "$" in nf or "USD" in nf:
        return text + "$"
    if "IQD" in nf or "د.ع" in nf or "دينار" in nf:
        return text + " د.ع"
    if abs(value) >= 10000:
        return text + " د.ع"
    return text  # رقم صغير بدون رمز: تقييم فندق أو سعر بالدولار، Claude يحدده من عنوان العمود


def _cell_text(cell) -> str:
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return _format_number(value, getattr(cell, "number_format", "") or "")
    if isinstance(value, (datetime, date)):
        return value.strftime("%d-%m-%Y")
    return clean(value)[:MAX_CELL_CHARS]


def strip_commission(text: str) -> str:
    """يقص النص من أول ذكر للعمولة لآخر الخلية. يرجع نص فارغ إذا الخلية كلها عمولة."""
    match = _COMMISSION.search(text)
    if not match:
        return text
    return _TRAILING_JUNK.sub("", text[: match.start()])


@dataclass
class Sheet:
    key: str  # اسم الشيت الأصلي
    title: str  # الاسم بعد التنظيف
    customer_dump: str  # بدون أي ذكر للعمولة
    agency_dump: str  # كامل
    commission_notes: list[str] = field(default_factory=list)
    cells: int = 0

    @property
    def preview(self) -> str:
        flat = re.sub(r"صف \d+: ", "", self.customer_dump)
        flat = re.sub(r"\b[A-Z]{1,2}=", "", flat).replace("\n", " / ")
        return flat[:220]


def _cap(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) > MAX_SHEET_CHARS:
        text = text[:MAX_SHEET_CHARS] + "\n…(الشيت أطول من هذا، الباقي مقطوع)"
    return text


def parse_workbook(data: bytes) -> list[Sheet]:
    """يرجع الشيتات الظاهرة (غير المخفية) اللي بيها بيانات."""
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
    sheets: list[Sheet] = []
    for ws in workbook.worksheets:
        if getattr(ws, "sheet_state", "visible") != "visible":
            continue
        agency_lines: list[str] = []
        customer_lines: list[str] = []
        notes: list[str] = []
        count = 0
        for row in ws.iter_rows():
            agency_cells: list[str] = []
            customer_cells: list[str] = []
            row_number = None
            for cell in row:
                text = _cell_text(cell)
                if not text:
                    continue
                count += 1
                column = cell.coordinate.rstrip("0123456789")
                row_number = cell.row
                agency_cells.append(f"{column}={text}")
                if _COMMISSION.search(text):
                    notes.append(text)
                    text = strip_commission(text)
                    if not text:
                        continue
                customer_cells.append(f"{column}={text}")
            if agency_cells:
                agency_lines.append(f"صف {row_number}: " + " | ".join(agency_cells))
            if customer_cells:
                customer_lines.append(f"صف {row_number}: " + " | ".join(customer_cells))
        if count < MIN_CELLS:
            continue
        sheets.append(
            Sheet(
                key=ws.title,
                title=clean(ws.title),
                customer_dump=_cap(customer_lines),
                agency_dump=_cap(agency_lines),
                commission_notes=notes,
                cells=count,
            )
        )
    workbook.close()
    return sheets


def export_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"


def remove_commission_lines(text: str) -> str:
    """حماية أخيرة لنسخة الزبون: يشيل أي سطر يذكر العمولة من الناتج."""
    return "\n".join(line for line in text.split("\n") if not _COMMISSION.search(line.replace(TATWEEL, "")))


if __name__ == "__main__":  # فحص محلي: python pricing.py ملف.xlsx [اسم شيت]
    import sys

    with open(sys.argv[1], "rb") as handle:
        result = parse_workbook(handle.read())
    print(f"شيتات فعالة: {len(result)}")
    for index, sheet in enumerate(result, 1):
        leak = "عمول" in sheet.customer_dump
        print(f"[{index}] {sheet.title} | خلايا {sheet.cells} | عمولة بالشيت: {len(sheet.commission_notes)} | تسريب بنسخة الزبون: {leak}")
    if len(sys.argv) > 2:
        wanted = [s for s in result if sys.argv[2] in s.title]
        for sheet in wanted[:1]:
            print("\n--- نسخة الزبون ---\n" + sheet.customer_dump[:2500])
            print("\n--- ملاحظات العمولة (للشركات فقط) ---\n" + "\n".join(sheet.commission_notes))
