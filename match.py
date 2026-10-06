"""يفهم طلب الموظف بدون ذكاء اصطناعي: يطابق كلمات الطلب ويا أسماء الشيتات ومحتواها."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from pricing import Sheet
from render import normalize

# كلمات ما تدل على عرض معين
_STOPWORDS = (
    """دز دزلي دزولي دزه ارسل ارسلي ارسلولي انطيني انطني اريد ابي بدي جيب جيبلي شوفلي نص نصوص النص عرض عروض العرض
    العروض كروب كروبات الكروب الكروبات برنامج برامج البرنامج سعر اسعار السعر الاسعار شكد شگد بكم بيش شنو شنهي هو هي هذا
    هاي على عن من الى في مال مالت مالات لو سمحت رجاء رجاءا ممكن يا هلا كم او مع لي الي ويا بدون حق خاص خاصه تبع بوست
    منشور اعلان رحله رحلات سفره زبون الزبون للزبون زبائن عادي كامل مختصر مختصره ملاحظات شركات الشركات للشركات شركه
    وكالات الوكالات للوكالات وكاله وكيل وكلاء عموله العموله بالعموله b2b الله يخليك عفيه حبيبي عيني اخي استاذ بوت
    صباح مساء الخير النور شباب جماعه شكرا تمام اوكي هلو مرحبا السلام عليكم وعليكم اليوم باجر هسه بعد اكو ماكو
    فندق الفندق فنادق ايام يوم ليالي ليله نجوم شخص اشخاص نفر بالغ طفل رضيع سنكل دبل"""
)
AGENCY_WORDS = {"شركات", "وكالات", "وكيل", "وكلاء", "وكاله", "عموله", "b2b"}
BRIEF_WORDS = {"مختصر", "مختصره", "اختصار"}
TRIGGER_WORDS = {"نص", "نصوص", "عرض", "عروض", "سعر", "اسعار", "دز", "دزلي", "ارسل", "ارسلي", "كروب", "كروبات", "برنامج", "شكد", "بكم", "اريد"}
MAX_CONTENT_MATCHES = 8
LIST_PHRASES = ("العروض", "القائمه", "قائمه", "الشيتات", "شنو عدكم", "شنو عدنا", "شنو متوفر", "شنو موجود")

# كلمة بالطلب ← كلمة موجودة باسم شيت
ALIASES = {
    "تركيا": ["اسطنبول", "طرابزون", "انطاليا"],
    "استنبول": ["اسطنبول"],
    "اسطمبول": ["اسطنبول"],
    "لبنان": ["بيروت"],
    "تايلند": ["تايلاند"],
    "سريلانكا": ["سيرلانكا"],
    "سيريلانكا": ["سيرلانكا"],
    "سيلان": ["سيرلانكا"],
    "ايران": ["ايران", "مشهد"],
    "فيزه": ["فيزا"],
    "فيز": ["فيزا"],
    "تاشيره": ["فيزا"],
    "تاشيرات": ["فيزا"],
    "موافقه": ["فيزا"],
    "سياره": ["بري"],
    "سيارات": ["بري"],
    "باص": ["بري"],
    "تكسي": ["بري"],
    "برا": ["بري"],
    "سوبر": ["سوبر"],
    "كلاسيكو": ["سوبر"],
    "مباراه": ["سوبر"],
    "عمان": ["مسقط", "بري"],
    "اوزنكول": ["اوزنجول"],
    "طرابزن": ["طرابزون"],
    "ماليزيه": ["ماليزيا"],
    "جورجيه": ["جورجيا"],
    "مصريه": ["مصر"],
}
_WORD = re.compile(r"[0-9a-zء-ي]+")
STOPWORDS = set(_WORD.findall(normalize(_STOPWORDS)))


def stem(word: str) -> str:
    """يشيل أل التعريف وحروف الجر الملتصقة: للشركات ← شركات ، بالبيروت ← بيروت."""
    for prefix in ("وال", "بال", "لل", "ال"):
        if word.startswith(prefix) and len(word) - len(prefix) >= 2:
            return word[len(prefix) :]
    return word


def words(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def sheet_id(sheet: Sheet) -> str:
    return hashlib.md5(sheet.key.encode("utf-8")).hexdigest()[:8]


@dataclass
class Entry:
    sheet: Sheet
    id: str
    title_words: set[str]
    content: set[str]  # كل كلمات الشيت، للبحث عن فندق أو منطقة


@dataclass
class Result:
    sheets: list[Sheet] = field(default_factory=list)
    audience: str = "customer"
    brief: bool = False
    by_title: bool = False  # التطابق من اسم الشيت (أقوى) لو من المحتوى فقط
    wants_list: bool = False
    triggered: bool = False  # الرسالة بيها كلمة طلب (نص، عرض، سعر...)


def build_index(sheets: list[Sheet]) -> list[Entry]:
    return [
        Entry(sheet, sheet_id(sheet), {stem(w) for w in words(sheet.title)}, {stem(w) for w in words(sheet.search_text("agency"))})
        for sheet in sheets
    ]


def find(request: str, index: list[Entry]) -> Result:
    text = normalize(request)
    raw = words(request)
    stems = {stem(w) for w in raw}
    result = Result()
    result.audience = "agency" if stems & AGENCY_WORDS and "بدون عموله" not in text and "بلا عموله" not in text else "customer"
    result.brief = bool(stems & BRIEF_WORDS) or "بدون ملاحظات" in text
    result.triggered = bool(set(raw) & TRIGGER_WORDS or stems & TRIGGER_WORDS)

    tokens: list[str] = []
    for word in raw:
        token = stem(word)
        if word in STOPWORDS or token in STOPWORDS or len(token) < 2 or token.isdigit() or token in tokens:
            continue
        tokens.append(token)
    if not tokens:
        result.wants_list = any(phrase in text for phrase in LIST_PHRASES)
        return result

    scored: list[tuple[int, int, Entry]] = []
    for entry in index:
        title_hits = content_hits = 0
        for token in tokens:
            targets = {token, *ALIASES.get(token, [])}
            if len(token) >= 4 and token[0] in "وبل":  # وبيروت / لبيروت
                targets.add(token[1:])
            if targets & entry.title_words:
                title_hits += 1
            elif len(token) >= 3 and targets & entry.content:
                content_hits += 1
        if title_hits or content_hits:
            scored.append((title_hits, content_hits, entry))
    if not scored:
        result.wants_list = any(phrase in text for phrase in LIST_PHRASES)
        return result
    best = max((t, c) for t, c, _ in scored)
    chosen = [entry.sheet for t, c, entry in scored if (t, c) == best]
    if best[0] == 0 and len(chosen) > MAX_CONTENT_MATCHES:
        return result  # كلمة عامة موجودة بأغلب الشيتات: ما تدل على عرض معين
    result.by_title = best[0] > 0
    result.sheets = chosen
    return result
