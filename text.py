"""أدوات النص العربي المشتركة: توحيد الكتابة، تقطيع الكلمات، والمرادفات."""
from __future__ import annotations

import re

from pricing import clean

_DIACRITICS = re.compile("[ً-ٰٟ]")
_WORD = re.compile("[0-9a-zء-ي]+")
_LETTERS = (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ة", "ه"), ("ى", "ي"), ("ؤ", "و"), ("ئ", "ي"), ("ی", "ي"), ("ک", "ك"))

# كلمة يكتبها الموظف ← كلمة موجودة باسم شيت أو قسم
ALIASES = {
    "تركيا": ["اسطنبول", "طرابزون", "انطاليا"],
    "استنبول": ["اسطنبول"],
    "اسطمبول": ["اسطنبول"],
    "لبنان": ["بيروت"],
    "تايلند": ["تايلاند"],
    "سريلانكا": ["سيرلانكا"],
    "سيريلانكا": ["سيرلانكا"],
    "سيلان": ["سيرلانكا"],
    "ايران": ["مشهد"],
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
    "كلاسيكو": ["سوبر"],
    "مباراه": ["سوبر"],
    "عمان": ["مسقط"],
    "اوزنكول": ["اوزنجول"],
    "طرابزن": ["طرابزون"],
    "ماليزيه": ["ماليزيا"],
    "جورجيه": ["جورجيا"],
    "مصريه": ["مصر"],
    "اردن": ["اردن", "عمان"],
    "امارات": ["امارات", "دبي"],
    "اندونيسيا": ["اندنوسيا", "اندونيسيا"],
    "سنغافوره": ["سنغافورا"],
    "تايلاند": ["تايلند"],
    "بوكت": ["بوكيت"],
    "باتايا": ["بتايا"],
    "دمشق": ["سوريا"],
    "سوريه": ["سوريا"],
}


def normalize(text: str) -> str:
    """توحيد الكتابة حتى تتطابق الكلمات: أ/إ/آ = ا ، ة = ه ، ى = ي."""
    text = _DIACRITICS.sub("", clean(text)).lower()
    for src, dst in _LETTERS:
        text = text.replace(src, dst)
    return text


def stem(word: str) -> str:
    """يشيل أل التعريف وحروف الجر الملتصقة: للشركات ← شركات ، بالبيروت ← بيروت."""
    for prefix in ("وال", "بال", "لل", "ال"):
        if word.startswith(prefix) and len(word) - len(prefix) >= 2:
            return word[len(prefix) :]
    return word


def words(text: str) -> list[str]:
    return _WORD.findall(normalize(text))


def word_set(text: str) -> set[str]:
    return {stem(word) for word in words(text)}


def targets(token: str) -> set[str]:
    """كل الأشكال اللي تعتبر تطابق لكلمة الطلب: نفسها، بدون حرف الجر، ومرادفاتها."""
    found = {token, *ALIASES.get(token, [])}
    if len(token) >= 4 and token[0] in "وبل":  # وبيروت / لبيروت
        found.add(token[1:])
    return found


def pick_sections(sections: list[set[str]], title: set[str], tokens: list[str]) -> tuple[list[int], int]:
    """أقسام الشيت اللي يذكرها الطلب. الكلمات اللي تطابق اسم الشيت نفسه ما تنحسب (فيزا بشيت الفيزا)."""
    usable = [token for token in tokens if not targets(token) & title]
    scores = [sum(1 for token in usable if targets(token) & section) for section in sections]
    best = max(scores, default=0)
    return ([i for i, score in enumerate(scores) if score == best] if best else []), best
