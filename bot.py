"""بوت الموظفين — بركات الكوثر (نسخة مجانية بدون ذكاء اصطناعي).

الموظف يكتب بكروب الموظفين اسم العرض ("دزلي نص كروبات بيروت")،
والبوت يقرأ شيت الأسعار من Google Sheets ويرجع النص جاهز بقالب ثابت.
ماكو أي خدمة مدفوعة: الأرقام تنتقل من الشيت مثل ما هي.

المتغيرات المطلوبة (Environment Variables):
  TELEGRAM_BOT_TOKEN   توكن البوت من BotFather
  SHEET_ID             معرف ملف Google Sheets
  ALLOWED_CHAT_IDS     معرف كروب الموظفين (أو أكثر من واحد بينهم فارزة)
اختيارية:
  GOOGLE_SERVICE_ACCOUNT_JSON  إذا تريد الشيت يبقى خاص وما ينفتح بالرابط
  CACHE_MINUTES                كل كم دقيقة يعيد قراءة الشيت (الافتراضي 10)
  POST_FOOTER                  الخاتمة الثابتة لكل نص
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import match
import pricing
import render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("staff-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")


def parse_chat_ids(raw: str) -> set[int]:
    """يقرأ المعرفات حتى لو الناقص انكتب بعد الرقم (5492479479-) مثل ما يطلع بالنص العربي."""
    ids = set()
    for lead, digits, trail in re.findall(r"(-?)(\d+)(-?)", raw):
        ids.add(-int(digits) if (lead or trail) else int(digits))
    return ids


ALLOWED = parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))
CACHE_SECONDS = int(os.environ.get("CACHE_MINUTES", "10")) * 60
MAX_BUTTONS = 12
MAX_ALL = 4  # زر "الكل" يطلع إذا الخيارات لحد هذا العدد
FOOTER = os.environ.get(
    "POST_FOOTER",
    "🔺الأسعار حسب التوفر وقت الحجز، ممكن تتغير\n\n"
    "#شركة_بركات_الكوثر_للسفر_والسياحة\n\n"
    "📞 للحجز والاستفسار:\n"
    "✈️ فرع كربلاء : 07810105600\n"
    "✈️ فرع بغداد : 07802620002",
)

# ---------------------------------------------------------------- الشيت

_cache: dict = {"at": 0.0, "sheets": [], "index": [], "texts": {}}
_lock = asyncio.Lock()


def _google_headers() -> dict:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        return {}
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(Request())
    return {"Authorization": f"Bearer {creds.token}"}


def set_sheets(sheets: list[pricing.Sheet]) -> None:
    _cache.update(at=time.time(), sheets=sheets, index=match.build_index(sheets), texts={})


async def get_index(force: bool = False) -> list[match.Entry]:
    async with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if _cache["sheets"] and fresh and not force:
            return _cache["index"]
        headers = await asyncio.to_thread(_google_headers)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
            response = await http.get(pricing.export_url(SHEET_ID), headers=headers)
        if response.status_code != 200 or "html" in response.headers.get("content-type", ""):
            raise RuntimeError(f"Google رجع {response.status_code}: الشيت مو مشارك أو المعرف غلط")
        sheets = await asyncio.to_thread(pricing.parse_workbook, response.content)
        if not sheets:
            raise RuntimeError("ماكو أي شيت ظاهر بيه بيانات")
        set_sheets(sheets)
        log.info("loaded %d sheets", len(sheets))
        return _cache["index"]


def sheet_text(sheet: pricing.Sheet, audience: str, brief: bool) -> str:
    key = (sheet.key, audience, brief)
    if key not in _cache["texts"]:
        _cache["texts"][key] = render.render(sheet, audience, brief)
    return _cache["texts"][key]


# ---------------------------------------------------------------- الأزرار

HELP = (
    "اكتب اسم العرض وأنا أرسل نصه جاهز من الشيت، مثلاً:\n"
    "• دزلي نص كروبات بيروت\n"
    "• اسطنبول اور\n"
    "• نص شمال ايران للشركات\n"
    "• فيزا الامارات\n"
    "• الروشة  (اسم فندق أو منطقة، وأطلعلك العروض اللي بيها)\n\n"
    "النص يطلع للزبون بدون عمولة. للشركات اكتب كلمة \"شركات\" أو اضغط الزر تحت النص.\n\n"
    "/sheets كل العروض الفعالة كأزرار\n"
    "/refresh إعادة قراءة الشيت هسه\n"
    "/id معرف هذا الكروب"
)


def flags(audience: str, brief: bool) -> str:
    return ("a" if audience == "agency" else "c") + ("b" if brief else "")


def unflag(code: str) -> tuple[str, bool]:
    return ("agency" if code.startswith("a") else "customer"), code.endswith("b")


def label(audience: str, brief: bool) -> str:
    text = "نسخة الشركات (ويا العمولة)" if audience == "agency" else "نسخة الزبون (بدون عمولة)"
    return f"{text} · مختصر" if brief else text


def choices(sheets: list[pricing.Sheet], audience: str, brief: bool, with_all: bool = True) -> InlineKeyboardMarkup:
    code = flags(audience, brief)
    buttons = [InlineKeyboardButton(s.title, callback_data=f"s|{code}|{match.sheet_id(s)}") for s in sheets]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    if with_all and 2 <= len(sheets) <= MAX_ALL:
        ids = ",".join(match.sheet_id(s) for s in sheets)
        rows.append([InlineKeyboardButton("📚 الكل", callback_data=f"m|{code}|{ids}")])
    return InlineKeyboardMarkup(rows)


def switches(sheet: pricing.Sheet, audience: str, brief: bool) -> InlineKeyboardMarkup:
    """أزرار تحت عنوان النص: التبديل بين نسخة الزبون والشركات، والكامل والمختصر."""
    sid = match.sheet_id(sheet)
    other = "customer" if audience == "agency" else "agency"
    row = [
        InlineKeyboardButton(
            "👤 نسخة الزبون" if other == "customer" else "💼 نسخة الشركات", callback_data=f"s|{flags(other, brief)}|{sid}"
        )
    ]
    if sheet_text(sheet, audience, True) != sheet_text(sheet, audience, False):
        row.append(
            InlineKeyboardButton(
                "📄 ويا الملاحظات" if brief else "✂️ بدون الملاحظات", callback_data=f"s|{flags(audience, not brief)}|{sid}"
            )
        )
    return InlineKeyboardMarkup([row])


# ---------------------------------------------------------------- تليكرام


def chat_allowed(chat) -> bool:
    if chat is None:
        return False
    if chat.id in ALLOWED:
        return True
    # معرف الكروب دائماً سالب: إذا انكتب بدون الناقص نقبله للكروبات فقط
    return getattr(chat, "type", "") in ("group", "supergroup") and abs(chat.id) in {abs(i) for i in ALLOWED}


def allowed(update: Update) -> bool:
    chat = update.effective_chat
    ok = chat_allowed(chat)
    log.info("update from chat %s (%s) allowed=%s", getattr(chat, "id", None), getattr(chat, "type", None), ok)
    return ok


async def not_allowed_hint(update: Update) -> None:
    """رد على الأوامر فقط بمحادثة غير مضافة، حتى يبين السبب بدل السكوت."""
    await update.effective_message.reply_text("هذه المحادثة غير مضافة للبوت. اكتب /id وحط الرقم بمتغير ALLOWED_CHAT_IDS.")


async def send_sheet(message, sheet: pricing.Sheet, audience: str, brief: bool) -> None:
    """رسالة عنوان بيها الأزرار، وبعدها النص نظيف بدون أزرار حتى ينسخه الموظف أو يحوله للزبون."""
    text = f"{sheet_text(sheet, audience, brief)}\n\n{FOOTER}"
    if audience != "agency":
        text = pricing.remove_commission_lines(text)  # حماية أخيرة
    await message.reply_text(f"📄 {sheet.title} · {label(audience, brief)}", reply_markup=switches(sheet, audience, brief))
    for chunk in render.split_message(text):
        await message.reply_text(chunk)


async def cmd_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    state = "مسموح ✅" if chat_allowed(chat) else "غير مضاف بعد"
    await update.effective_message.reply_text(
        f"معرف هذه المحادثة (اضغط عليه للنسخ):\n<code>{chat.id}</code>\nالحالة: {state}", parse_mode="HTML"
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await not_allowed_hint(update)
        return
    await update.effective_message.reply_text(HELP)


async def show_list(message, index: list[match.Entry]) -> None:
    sheets = [entry.sheet for entry in index]
    await message.reply_text(
        f"العروض الفعالة بالشيت ({len(sheets)}). اضغط على العرض حتى أرسل نصه:",
        reply_markup=choices(sheets, "customer", False, with_all=False),
    )


async def cmd_sheets(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await not_allowed_hint(update)
        return
    try:
        await show_list(update.effective_message, await get_index())
    except Exception as error:  # noqa: BLE001
        log.exception("sheets failed")
        await update.effective_message.reply_text(f"ما كدرت أقرأ الشيت: {error}")


async def cmd_refresh(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        await not_allowed_hint(update)
        return
    try:
        index = await get_index(force=True)
    except Exception as error:  # noqa: BLE001
        log.exception("refresh failed")
        await update.effective_message.reply_text(f"ما كدرت أقرأ الشيت: {error}")
        return
    await update.effective_message.reply_text(f"تم تحديث الأسعار ✅ ({len(index)} عرض فعال)")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not allowed(update) or not message or not message.text:
        return
    request = message.text.strip()
    if not request:
        return
    private = getattr(update.effective_chat, "type", "") == "private"
    try:
        index = await get_index()
        found = match.find(request, index)
        if not found.sheets:
            if found.wants_list:
                await show_list(message, index)
            elif found.triggered or private:
                await message.reply_text(
                    "ما لكيت عرض بهذا الاسم بالشيت. اكتب اسم الوجهة مثل ما مكتوب بالشيت، أو اختار من القائمة:",
                    reply_markup=choices([e.sheet for e in index], found.audience, found.brief, with_all=False),
                )
            return  # كلام عادي بالكروب: البوت يسكت
        casual = not found.triggered and not private  # رسالة بالكروب بدون كلمة طلب (نص، عرض، سعر...)
        if casual and len(request.split()) > (4 if found.by_title else 2):
            return  # سوالف بين الموظفين وبيها اسم وجهة بالصدفة: البوت يسكت
        if len(found.sheets) == 1:
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            await send_sheet(message, found.sheets[0], found.audience, found.brief)
            return
        shown = found.sheets[:MAX_BUTTONS]
        await message.reply_text(
            f"لكيت {len(found.sheets)} عروض. أي واحد تريد؟ ({label(found.audience, found.brief)})",
            reply_markup=choices(shown, found.audience, found.brief),
        )
    except Exception as error:  # noqa: BLE001
        log.exception("request failed")
        await message.reply_text(f"صار خطأ: {error}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not allowed(update):
        await query.answer("هذه المحادثة غير مضافة للبوت", show_alert=True)
        return
    try:
        kind, code, ids = (query.data or "").split("|", 2)
        audience, brief = unflag(code)
        index = await get_index()
        by_id = {entry.id: entry.sheet for entry in index}
        sheets = [by_id[i] for i in ids.split(",") if i in by_id]
        if not sheets or kind not in ("s", "m"):
            await query.answer("هذا العرض ما موجود بعد بالشيت. اكتب /sheets", show_alert=True)
            return
        await query.answer()
        for sheet in sheets:
            await send_sheet(query.message, sheet, audience, brief)
    except Exception as error:  # noqa: BLE001
        log.exception("button failed")
        await query.answer(f"صار خطأ: {error}"[:190], show_alert=True)


def main() -> None:
    missing = [name for name in ("TELEGRAM_BOT_TOKEN", "SHEET_ID") if not os.environ.get(name)]
    if missing:
        raise SystemExit("ناقص بالمتغيرات: " + ", ".join(missing))
    if not ALLOWED:
        log.warning("ALLOWED_CHAT_IDS فارغ: البوت يرد على /id فقط لحد ما تضيف معرف الكروب")

    log.info("allowed chats: %s", sorted(ALLOWED))
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("sheets", cmd_sheets))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    public_url = os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL")
    if public_url:  # على Render: Web Service
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", "10000")),
            url_path=TOKEN,
            webhook_url=f"{public_url.rstrip('/')}/{TOKEN}",
            allowed_updates=Update.ALL_TYPES,
        )
    else:  # تشغيل محلي للتجربة
        app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
