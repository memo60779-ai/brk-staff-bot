"""فحص بدون إنترنت: شيت تجريبي + Claude وهمي + تليكرام وهمي.  التشغيل: python test_bot.py"""
import asyncio
import io
import json
import os
import time
from types import SimpleNamespace

import openpyxl

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST")
os.environ.setdefault("SHEET_ID", "TEST")
os.environ["ALLOWED_CHAT_IDS"] = "-100111"

import bot  # noqa: E402
import pricing  # noqa: E402


def sample_workbook() -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "بيروت بغـــداد"
    ws.append(["البرنامج", "الموقع", "السنكل", "الرضيع", "المبيع", "عدد الايام", "تقييم الفندق", "اسم الفندق"])
    ws.append(["كروب بغداد بيروت ( عمولــه الشركات 20.000 دينار )"])
    ws.append(["السعر يشمل تذكرة ذهاب وعودة", "الحمرا", 710000, 150000, 600000, "4 ايام / 3 ليالي", 3, "Villa Queens"])
    ws.append([None, None, 770000, 150000, 645000, "5 ايام / 4 ليالي"])
    for cell in ("C3", "D3", "E3", "C4", "D4", "E4"):
        ws[cell].number_format = "#,##0"
    ws2 = wb.create_sheet("اسطنبول")
    ws2.append(["عمولة الشركات 10$"])
    ws2.append(["البرنامج", "الدبل", "السنكل", "عدد الايام", "اسم الفندق", "التقييم"])
    ws2.append(["طيران + فندق + استقبال", 450, 600, "5 ايام", "Grand Hotel", 4])
    ws2.append([None, 520, 700, "7 ايام", "Grand Hotel", 4])
    for cell in ("B3", "C3", "B4", "C4"):
        ws2[cell].number_format = '"$"#,##0'
    old = wb.create_sheet("بيروت العيد قديم")
    for _ in range(4):
        old.append(["عرض قديم", 999999, "x", "y"])
    old.sheet_state = "hidden"
    wb.create_sheet("Sheet9")
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class FakeClaude:
    """يرجع قرار التوزيع أو نص وهمي، ويحفظ كل اللي انرسل له حتى نفحصه."""

    def __init__(self):
        self.calls = []
        self.next_route = {}
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["system"].startswith("أنت موزّع"):
            text = "هذا القرار: " + json.dumps(self.next_route, ensure_ascii=False)
        else:
            text = "⭕️عرض تجريبي\n⬅️ 4 أيام | 600,000 د.ع\n💼 عمولة الشركات 20,000"
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)])


def fake_update(text, chat_id=-100111, user_id=7):
    sent = []

    async def reply_text(message):
        sent.append(message)

    message = SimpleNamespace(text=text, reply_text=reply_text)
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type="group" if chat_id < 0 else "private"),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
    )
    return update, sent


async def run():
    sheets = pricing.parse_workbook(sample_workbook())
    titles = [s.title for s in sheets]
    assert titles == ["بيروت بغداد", "اسطنبول"], titles  # المخفي والفارغ انشالوا والمدّات تنظفت
    beirut, istanbul = sheets
    assert "عمول" not in beirut.customer_dump and "عمول" not in istanbul.customer_dump
    assert "كروب بغداد بيروت" in beirut.customer_dump  # باقي الخلية بقى
    assert "عموله الشركات 20.000" in beirut.agency_dump
    assert "600,000 د.ع" in beirut.customer_dump and "450$" in istanbul.customer_dump
    assert "G=3" in beirut.customer_dump  # تقييم الفندق يبقى رقم بدون عملة
    print("✓ قراءة الشيت: المخفي متجاهل، العملة صحيحة، العمولة منزوعة من نسخة الزبون")

    fake = FakeClaude()
    bot.claude = fake
    bot._cache.update(at=time.time(), sheets=sheets)
    context = SimpleNamespace(bot=SimpleNamespace(send_chat_action=lambda *a, **k: asyncio.sleep(0)))

    # 1) نص زبون
    fake.next_route = {"action": "write", "sheets": [1], "audience": "customer"}
    update, sent = fake_update("دزلي نص كروبات بيروت")
    await bot.on_message(update, context)
    writer_call = fake.calls[-1]
    assert "عمول" not in writer_call["messages"][0]["content"], "العمولة وصلت للكاتب بنسخة الزبون"
    assert "ممنوع تذكر عمولة" in writer_call["system"]
    assert sent[0] == "📄 بيروت بغداد · نسخة الزبون (بدون عمولة)"
    assert "عمول" not in sent[1] and "07810105600" in sent[1] and "600,000" in sent[1]
    print("✓ نسخة الزبون: العمولة ما وصلت للكاتب، وانشالت من الناتج، والخاتمة انضافت")

    # 2) نص شركات
    fake.next_route = {"action": "write", "sheets": [1, 2], "audience": "agency"}
    update, sent = fake_update("نص بيروت واسطنبول للشركات")
    await bot.on_message(update, context)
    assert len(sent) == 4 and "نسخة الشركات" in sent[0] and "عمولة الشركات" in sent[1]
    assert any("عموله الشركات 20.000" in c["messages"][0]["content"] for c in fake.calls[-2:])
    print("✓ نسخة الشركات: شيتين بطلب واحد والعمولة موجودة")

    # 3) توضيح ثم جواب
    fake.next_route = {"action": "clarify", "sheets": [], "reply": "تقصد بيروت بغداد لو اسطنبول؟"}
    update, sent = fake_update("دزلي العرض")
    await bot.on_message(update, context)
    assert sent == ["تقصد بيروت بغداد لو اسطنبول؟"]
    fake.next_route = {"action": "write", "sheets": [2], "audience": "customer"}
    update, sent = fake_update("اسطنبول")
    await bot.on_message(update, context)
    router_call = fake.calls[-2]
    assert "دزلي العرض" in router_call["messages"][0]["content"]  # السياق انتقل
    assert sent[0].startswith("📄 اسطنبول")
    print("✓ سؤال التوضيح: الجواب ينربط بالطلب الأول")

    # 4) كروب غير مسموح
    calls_before = len(fake.calls)
    update, sent = fake_update("دزلي نص بيروت", chat_id=999)
    await bot.on_message(update, context)
    assert sent == [] and len(fake.calls) == calls_before
    print("✓ محادثة خارج كروب الموظفين: البوت ما يرد وما يصرف من الرصيد")

    # 5) قرار غلط من الموزّع
    fake.next_route = {"action": "write", "sheets": [99], "audience": "customer"}
    update, sent = fake_update("نص المريخ")
    await bot.on_message(update, context)
    assert len(sent) == 1 and "/sheets" in sent[0]
    print("✓ شيت غير موجود: البوت يطلب التوضيح بدل ما يخترع")

    # 6) قراءة معرف الكروب بكل أشكاله
    assert bot.parse_chat_ids("-5492479479") == {-5492479479}
    assert bot.parse_chat_ids("5492479479-") == {-5492479479}  # الناقص بعد الرقم مثل ما يطلع بالعربي
    assert bot.parse_chat_ids(" -100123 , -100456 ") == {-100123, -100456}
    bot.ALLOWED = bot.parse_chat_ids("5492479479")  # انكتب بدون ناقص
    assert bot.chat_allowed(SimpleNamespace(id=-5492479479, type="group"))
    bot.ALLOWED = {-5492479479}
    assert not bot.chat_allowed(SimpleNamespace(id=5492479479, type="private"))  # محادثة خاصة بنفس الرقم ما تنقبل
    assert not bot.chat_allowed(SimpleNamespace(id=-777, type="group"))
    bot.ALLOWED = {-100111}
    print("✓ معرف الكروب ينقبل حتى لو الناقص بعد الرقم أو مفقود، والمحادثات الخاصة ما تنقبل")

    from telegram.ext import Application

    Application.builder().token(os.environ["TELEGRAM_BOT_TOKEN"]).concurrent_updates(True).build()
    print("✓ تطبيق تليكرام ينبني بدون أخطاء")


if __name__ == "__main__":
    asyncio.run(run())
    print("\nكل الفحوصات نجحت")
