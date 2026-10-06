"""بوت الموظفين — بركات الكوثر.

الموظف يكتب بكروب الموظفين طلبه بالعراقي ("دزلي نص كروبات بيروت")،
والبوت يقرأ شيت الأسعار من Google Sheets ويرجع النص جاهز.

المتغيرات المطلوبة (Environment Variables):
  TELEGRAM_BOT_TOKEN   توكن البوت من BotFather
  ANTHROPIC_API_KEY    مفتاح Claude API
  SHEET_ID             معرف ملف Google Sheets
  ALLOWED_CHAT_IDS     معرف كروب الموظفين (أو أكثر من واحد بينهم فارزة)
اختيارية:
  GOOGLE_SERVICE_ACCOUNT_JSON  إذا تريد الشيت يبقى خاص وما ينفتح بالرابط
  WRITER_MODEL / ROUTER_MODEL  موديلات Claude
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
from anthropic import AsyncAnthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import pricing

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("staff-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
ALLOWED = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ALLOWED_CHAT_IDS", ""))}
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-sonnet-5")
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-4-5-20251001")
CACHE_SECONDS = int(os.environ.get("CACHE_MINUTES", "10")) * 60
MAX_SHEETS_PER_REQUEST = 4
FOOTER = os.environ.get(
    "POST_FOOTER",
    "🔺الأسعار حسب التوفر وقت الحجز، ممكن تتغير\n\n"
    "#شركة_بركات_الكوثر_للسفر_والسياحة\n\n"
    "📞 للحجز والاستفسار:\n"
    "✈️ فرع كربلاء : 07810105600\n"
    "✈️ فرع بغداد : 07802620002",
)

claude: AsyncAnthropic | None = None  # ينشأ بـ main() ويقرأ ANTHROPIC_API_KEY من المتغيرات

# ---------------------------------------------------------------- الشيت

_cache: dict = {"at": 0.0, "sheets": []}
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


async def get_sheets(force: bool = False) -> list[pricing.Sheet]:
    async with _lock:
        fresh = time.time() - _cache["at"] < CACHE_SECONDS
        if _cache["sheets"] and fresh and not force:
            return _cache["sheets"]
        headers = await asyncio.to_thread(_google_headers)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as http:
            response = await http.get(pricing.export_url(SHEET_ID), headers=headers)
        if response.status_code != 200 or "html" in response.headers.get("content-type", ""):
            raise RuntimeError(f"Google رجع {response.status_code}: الشيت مو مشارك أو المعرف غلط")
        sheets = await asyncio.to_thread(pricing.parse_workbook, response.content)
        if not sheets:
            raise RuntimeError("ماكو أي شيت ظاهر بيه بيانات")
        _cache.update(at=time.time(), sheets=sheets)
        log.info("loaded %d sheets", len(sheets))
        return sheets


# ---------------------------------------------------------------- Claude

ROUTER_SYSTEM = """أنت موزّع طلبات داخل بوت موظفي شركة سفر عراقية. الموظف يكتب بالعراقي.
عندك قائمة شيتات الأسعار الفعالة، كل شيت برقمه واسمه ولمحة من محتواه.
حدد شنو يريد الموظف ورجّع JSON فقط بدون أي كلام ثاني، بهذا الشكل:
{"action":"write|clarify|list|other","sheets":[أرقام],"audience":"customer|agency","reply":"نص"}

القواعد:
- write: الموظف يريد نص عرض أو يسأل عن سعر/فندق/تاريخ موجود بشيت. حط أرقام الشيتات المطلوبة.
  إذا طلب وجهة عامة (مثل "كروبات بيروت") وأكو أكثر من شيت لنفس الوجهة، رجّعها كلها لحد 4.
  إذا الشيتات أكثر من 4 أو الطلب مو واضح أي وحدة، استخدم clarify.
- clarify: اكتب بـ reply سؤال قصير بالعراقي يذكر الخيارات المتوفرة بأسمائها.
- list: الموظف يسأل شنو العروض أو الشيتات المتوفرة.
- other: تحية أو شي ما يخص الأسعار. اكتب بـ reply رد قصير يوضح شلون يطلب.
- audience = agency فقط إذا ذكر الموظف: شركات، وكالات، وكيل، عمولة، B2B. غير هذا دائماً customer.
- لا تخترع شيت مو موجود بالقائمة."""

WRITER_SYSTEM = """أنت كاتب نصوص عروض لشركة بركات الكوثر للسفر والسياحة. تكتب لموظف راح ينسخ النص ويرسله.
تستلم بيانات شيت أسعار واحد (كل صف بأحرف الأعمدة) وطلب الموظف.

قواعد الأرقام، وهي الأهم:
- استخدم فقط الأرقام والفنادق والتواريخ الموجودة بالبيانات. ممنوع تخمن أو تكمل من عندك.
- العملة مثل ما مكتوبة: $ يعني دولار، د.ع يعني دينار. رقم صغير بدون رمز تحت عمود سعر يعني دولار، وتحت عمود تقييم يعني عدد نجوم.
- عمود "المبيع" أو "التسديد" أو "الدبل" هو سعر الشخص بالغرفة الثنائية. "السنكل" سعر الغرفة المفردة، و"فرق السنكل" مبلغ يضاف على سعر الشخص.
- الخلية الفارغة تحت اسم فندق أو منطقة تعني نفس القيمة اللي فوقها (خلايا مدموجة).
- إذا معلومة مطلوبة مو موجودة بالشيت، كول للموظف بسطر واحد إنها مو موجودة.

نوع الرد:
- إذا الموظف طلب "نص" أو "عرض" أو ما حدد: اكتب نص العرض الكامل بالنمط تحت.
- إذا سأل سؤال محدد (سعر فندق، مدة، تاريخ): جاوب بسطرين أو ثلاثة فقط بدون نمط.
- إذا حدد شرط (مثلاً بس 4 نجوم، أو بس 5 أيام): التزم بيه.

نمط نص العرض (نص عادي بدون Markdown وبدون نجمات):
⭕️<عنوان العرض والوجهة ومدينة الانطلاق>

<سطر الطيران وتواريخ السفر إذا موجودة>

✅ السعر يشمل: <مثل ما مكتوب بالشيت باختصار>

🔹<المنطقة — اسم الفندق (عدد النجوم)>
⬅️ <المدة> | <سعر الشخص>
(كرر لكل فندق)

👶 الرضيع: … | 🧒 الطفل بدون سرير: … | 🛏 السنكل: …
📌 <ملاحظات مهمة من الشيت، مختصرة>

- اكتب بعراقي بسيط وواضح بدون مبالغة تسويقية.
- لا تكتب أرقام هواتف ولا هاشتاكات ولا جملة "الأسعار ممكن تتغير": تنضاف تلقائياً.
- النص لازم ما يتجاوز 3200 حرف. إذا الفنادق هواية، اختصر الصياغة وخلّي كل الفنادق.
- رجّع النص فقط بدون مقدمة ولا شرح."""

CUSTOMER_RULE = "\n\nهذا النص للزبون: ممنوع تذكر عمولة أو سعر شركات أو أي شي يخص الوكالات."
AGENCY_RULE = (
    "\n\nهذا النص للشركات والوكالات: بعد الأسعار أضف سطر يبدأ بـ 💼 يذكر عمولة الشركات "
    "مثل ما مكتوبة بالشيت بالضبط. إذا الشيت ما بيه عمولة، لا تذكرها."
)


def _text(message) -> str:
    return "".join(block.text for block in message.content if getattr(block, "type", "") == "text").strip()


async def route(request: str, sheets: list[pricing.Sheet], context: str = "") -> dict:
    index = "\n".join(f"[{i}] {s.title} — {s.preview}" for i, s in enumerate(sheets, 1))
    user = f"الشيتات الفعالة:\n{index}\n\n"
    if context:
        user += f"سياق سابق بنفس المحادثة:\n{context}\n\n"
    user += f"طلب الموظف:\n{request}"
    message = await claude.messages.create(
        model=ROUTER_MODEL, max_tokens=500, system=ROUTER_SYSTEM, messages=[{"role": "user", "content": user}]
    )
    raw = _text(message)
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        decision = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        decision = {}
    ids = [i for i in decision.get("sheets") or [] if isinstance(i, int) and 1 <= i <= len(sheets)]
    return {
        "action": decision.get("action") if decision.get("action") in {"write", "clarify", "list", "other"} else "other",
        "sheets": list(dict.fromkeys(ids)),
        "audience": "agency" if decision.get("audience") == "agency" else "customer",
        "reply": str(decision.get("reply") or "").strip(),
    }


async def write(request: str, sheet: pricing.Sheet, audience: str) -> str:
    agency = audience == "agency"
    data = sheet.agency_dump if agency else sheet.customer_dump
    message = await claude.messages.create(
        model=WRITER_MODEL,
        max_tokens=2500,
        system=WRITER_SYSTEM + (AGENCY_RULE if agency else CUSTOMER_RULE),
        messages=[{"role": "user", "content": f"اسم الشيت: {sheet.title}\n\nالبيانات:\n{data}\n\nطلب الموظف:\n{request}"}],
    )
    text = _text(message)
    if not agency:
        text = pricing.remove_commission_lines(text)
    return text


# ---------------------------------------------------------------- تليكرام

HELP = (
    "اكتب طلبك عادي، مثلاً:\n"
    "• دزلي نص كروبات بيروت\n"
    "• نص اسطنبول للشركات\n"
    "• شكد سعر الروشة 5 أيام من بغداد؟\n"
    "• فيزا الإمارات شنو متطلباتها؟\n\n"
    "النص يطلع للزبون بدون عمولة. إذا تريده للشركات اكتب كلمة \"شركات\" بطلبك.\n\n"
    "/sheets قائمة العروض الفعالة\n"
    "/refresh إعادة قراءة الشيت هسه\n"
    "/id معرف هذا الكروب"
)
_pending: dict[tuple[int, int], tuple[float, str]] = {}  # سؤال توضيح ينتظر جواب


def allowed(update: Update) -> bool:
    return bool(update.effective_chat) and update.effective_chat.id in ALLOWED


async def send(update: Update, text: str) -> None:
    for start in range(0, len(text), 4000):
        await update.effective_message.reply_text(text[start : start + 4000])


async def cmd_id(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    state = "مسموح ✅" if chat.id in ALLOWED else "غير مضاف بعد"
    await update.effective_message.reply_text(f"معرف هذه المحادثة: {chat.id}\nالحالة: {state}")


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if allowed(update):
        await send(update, HELP)


async def cmd_sheets(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    try:
        sheets = await get_sheets()
    except Exception as error:  # noqa: BLE001
        await send(update, f"ما كدرت أقرأ الشيت: {error}")
        return
    await send(update, "العروض الفعالة بالشيت:\n" + "\n".join(f"• {s.title}" for s in sheets))


async def cmd_refresh(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    try:
        sheets = await get_sheets(force=True)
    except Exception as error:  # noqa: BLE001
        await send(update, f"ما كدرت أقرأ الشيت: {error}")
        return
    await send(update, f"تم تحديث الأسعار ✅ ({len(sheets)} شيت فعال)")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.effective_message or not update.effective_message.text:
        return
    request = update.effective_message.text.strip()
    if not request:
        return
    chat_id = update.effective_chat.id
    key = (chat_id, update.effective_user.id if update.effective_user else 0)
    await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        sheets = await get_sheets()
        previous = _pending.pop(key, None)
        history = previous[1] if previous and time.time() - previous[0] < 300 else ""
        decision = await route(request, sheets, history)

        if decision["action"] == "list":
            await send(update, "العروض الفعالة بالشيت:\n" + "\n".join(f"• {s.title}" for s in sheets))
            return
        if decision["action"] == "write" and len(decision["sheets"]) > MAX_SHEETS_PER_REQUEST:
            decision = {**decision, "action": "clarify", "reply": ""}
        if decision["action"] != "write" or not decision["sheets"]:
            reply = decision["reply"] or "ما فهمت أي عرض تقصد. اكتب /sheets وشوف الأسماء المتوفرة."
            if decision["action"] == "clarify":
                _pending[key] = (time.time(), f"الموظف طلب: {history or request}\nالبوت سأل: {reply}")
            await send(update, reply)
            return

        chosen = [sheets[i - 1] for i in decision["sheets"]]
        full_request = f"{history}\n{request}".strip() if history else request
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        results = await asyncio.gather(
            *(write(full_request, sheet, decision["audience"]) for sheet in chosen), return_exceptions=True
        )
        label = "نسخة الشركات (ويا العمولة)" if decision["audience"] == "agency" else "نسخة الزبون (بدون عمولة)"
        for sheet, result in zip(chosen, results):
            if isinstance(result, Exception):
                log.exception("write failed for %s", sheet.title, exc_info=result)
                await send(update, f"صار خطأ بكتابة نص «{sheet.title}». جرب مرة ثانية.")
                continue
            await send(update, f"📄 {sheet.title} · {label}")
            is_post = result.lstrip().startswith("⭕")
            await send(update, f"{result}\n\n{FOOTER}" if is_post else result)
    except Exception as error:  # noqa: BLE001
        log.exception("request failed")
        await send(update, f"صار خطأ: {error}")


def main() -> None:
    missing = [name for name in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY", "SHEET_ID") if not os.environ.get(name)]
    if missing:
        raise SystemExit("ناقص بالمتغيرات: " + ", ".join(missing))
    if not ALLOWED:
        log.warning("ALLOWED_CHAT_IDS فارغ: البوت يرد على /id فقط لحد ما تضيف معرف الكروب")

    global claude
    claude = AsyncAnthropic()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("sheets", cmd_sheets))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
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
