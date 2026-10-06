"""يحول شيت الأسعار إلى نص عرض بقالب ثابت — بدون ذكاء اصطناعي.

الفكرة: كل شيت يتقرأ صف صف.
- صف العناوين (البرنامج | السنكل | المبيع | اسم الفندق ...) يحدد دور كل عمود.
- الصف اللي بيه سعر = سطر سعر. اسم الفندق ينكتب مرة وحدة وتحته مدده.
- الخلية العريضة المدموجة = عنوان قسم أو ملاحظة.
- أي صف ما ينفهم ينطبع مثل ما هو، حتى ما يضيع رقم.
الأرقام تنتقل من الشيت مثل ما هي: البوت ما يحسب وما يخمن.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from pricing import Cell, Sheet, clean, remove_commission_lines

# ---------------------------------------------------------------- أدوار الأعمدة

ROLES: list[tuple[str, tuple[str, ...]]] = [  # الترتيب مهم: أول تطابق يفوز
    ("skip", ("صور",)),
    ("single", ("سنكل",)),
    ("infant", ("رضيع",)),
    ("child", ("طفل",)),
    ("location", ("موقع", "منطقه", "عنوان", "مدينه")),
    ("rating", ("تقيم", "تقييم", "تصنيف")),
    ("days", ("ايام", "ليالي")),
    ("date", ("تاريخ",)),
    ("airport", ("مطار",)),
    ("price", ("مبيع", "بيع", "تسديد", "دبل", "تربل", "بالغ", "سعر", "غرفه")),
    ("hotel", ("فندق", "فنادق")),
    ("program", ("برنامج", "برانامج", "برنمج", "تحميل")),
    ("notes", ("ملاحظ",)),
]
HEADER_WORDS = (
    "الوصف", "العدد", "المقعد", "المركبه", "الوجهه", "الانطلاق", "مده", "المتطلبات", "صلاحيه", "النوع", "الدول", "الجنسيه",
)
CONTEXT = {"program", "notes", "date", "airport"}
HOTEL = {"hotel", "location", "rating"}
AMOUNT = {"price", "single", "infant", "child"}
ICON = {"notes": "📌", "date": "🗓", "airport": "🛫", "days": "⏱"}
MAX_HEADER_CELL = 45
MAX_TITLE = 110
_PLAIN_PRICE = re.compile(r"^(ال)?(مبيع|بيع|تسديد|سعر)( النفر| الفرد| الشخص)?$")
_LABEL_ONLY = re.compile(r"^(تحميل )?(ال)?(برنامج|برانامج|برنمج|ملاحظات)( و(ال)?(برنامج|ملاحظات))?$")
LINK_WORDS = {"البرنامج", "تحميل البرنامج", "صور", "صور الفندق", "الصور"}  # نص روابط بالشيت ماله معنى بالنص
_DIACRITICS = re.compile("[ً-ٰٟ]")


def normalize(text: str) -> str:
    """توحيد الكتابة حتى تتطابق الكلمات: أ/إ/آ = ا ، ة = ه ، ى = ي."""
    text = _DIACRITICS.sub("", clean(text)).lower()
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ة", "ه"), ("ى", "ي"), ("ؤ", "و"), ("ئ", "ي"), ("ی", "ي"), ("ک", "ك")):
        text = text.replace(src, dst)
    return text


def role_of(label: str) -> str:
    text = normalize(label)
    for role, words in ROLES:
        if any(word in text for word in words):
            return role
    return "other"


def flat(text: str, sep: str = " / ") -> str:
    return sep.join(part.strip(' "') for part in text.split("\n") if part.strip(' "'))


@dataclass
class Column:
    start: int
    end: int
    role: str
    label: str


@dataclass
class Header:
    columns: list[Column]
    pseudo: bool = False  # جدول جنب جدول: كل عمود ينطبع كقائمة لوحده

    def find(self, col: int) -> Column | None:
        for column in self.columns:
            if column.start <= col <= column.end:
                return column
        return None

    def of(self, *roles: str) -> list[Column]:
        """الأعمدة بترتيب القراءة (من اليمين): العمود الأخير أولاً."""
        return sorted((c for c in self.columns if c.role in roles), key=lambda c: -c.start)


@dataclass
class Record:
    cells: dict[int, Cell]
    header: Header
    extra_hotels: list[str] = field(default_factory=list)


class Renderer:
    def __init__(self, sheet: Sheet, audience: str, brief: bool = False):
        self.sheet = sheet
        self.agency = audience == "agency"
        self.brief = brief

    # -------------------------------------------------------- نص الخلية

    def body(self, cell: Cell) -> str:
        return cell.text if self.agency else cell.customer_text

    def amount(self, cell: Cell) -> str:
        if cell.number is None:
            return flat(self.body(cell), " ")
        unit = cell.unit or "$"  # رقم صغير بدون رمز تحت عمود سعر = دولار
        return f"{cell.text}$" if unit == "$" else f"{cell.text} {unit}"

    def stars(self, cell: Cell) -> str:
        if cell.number is not None and cell.number == int(cell.number) and 1 <= cell.number <= 7:
            return f"{int(cell.number)}⭐"
        return flat(self.body(cell), " ")

    def value(self, cell: Cell, role: str) -> str:
        if role in AMOUNT:
            return self.amount(cell)
        if cell.number is not None and cell.unit:
            return self.amount(cell)
        return flat(self.body(cell), " ")

    # -------------------------------------------------------- تصنيف الصفوف

    def _headerish(self, cell: Cell) -> bool:
        text = flat(self.body(cell), " ")
        if len(text) > MAX_HEADER_CELL:
            return False
        return role_of(text) != "other" or any(word in normalize(text) for word in HEADER_WORDS)

    def _is_header(self, orig: dict[int, Cell]) -> bool:
        if len(orig) < 2 or any(cell.number is not None for cell in orig.values()):
            return False
        hits = sum(1 for cell in orig.values() if self._headerish(cell))
        return hits >= 2 and hits >= 0.6 * len(orig)

    def _make_header(self, orig: dict[int, Cell]) -> tuple[Header, list[str]]:
        columns, notes = [], []
        for col, cell in orig.items():
            label = flat(self.body(cell), " ")
            if len(label) > MAX_HEADER_CELL:  # ملاحظة مكتوبة بمكان عنوان العمود
                notes.append(self.body(cell))
                columns.append(Column(col, cell.span_to or col, "notes", ""))
            else:
                columns.append(Column(col, cell.span_to or col, role_of(label), label))
        return Header(columns), notes

    @staticmethod
    def _wide(col: int, cell: Cell, header: Header | None) -> bool:
        end = cell.span_to or col
        if header is None:
            return end - col >= 2
        return sum(1 for c in header.columns if col <= c.start <= end) >= 2

    # -------------------------------------------------------- القراءة

    def items(self) -> list[tuple]:
        """يحول الصفوف إلى عناصر: عنوان، ملاحظة، سياق، سطر سعر، سطر خام."""
        out: list[tuple] = []
        header: Header | None = None
        rows = [
            (cells, {c: x for c, x in cells.items() if x.original and self.body(x).strip()}) for _, cells in self.sheet.rows
        ]
        rows = [(cells, orig) for cells, orig in rows if orig]
        for index, (cells, orig) in enumerate(rows):
            if len(orig) == 1 and len(self.body(next(iter(orig.values()))).strip()) <= 2:
                continue  # حرف يتيم بالشيت
            numeric = any(cell.number is not None for cell in orig.values())
            if self._is_header(orig):
                following = rows[index + 1][1] if index + 1 < len(rows) else {}
                if not (self._is_header(following) and len(following) > len(orig)):  # مو "الملاحظات | عنوان القسم"
                    header, notes = self._make_header(orig)
                    out.append(("header", header))
                    out.extend(("note", note) for note in notes)
                    continue
            if not numeric:
                rest = {c: x for c, x in orig.items() if not self._label_only(x)}
                wide = [c for c, x in rest.items() if self._wide(c, x, header)]
                if not rest:
                    continue
                if len(rest) == 1 and (wide or header is None):
                    text = self.body(next(iter(rest.values())))
                    short = len(text) <= MAX_TITLE and text.count("\n") <= 2
                    out.append(("title" if short else "note", text))
                    continue
                if len(rest) >= 2 and all(x.span_to > c for c, x in rest.items()) and (wide or header is None):
                    header = Header(
                        [Column(c, x.span_to or c, "other", flat(self.body(x))) for c, x in rest.items()], pseudo=True
                    )
                    out.append(("header", header))
                    continue
            if header is None:
                out.append(("raw", self._raw(orig)))
                continue
            if header.pseudo:
                out.append(("pseudo", cells))
                continue
            self._by_roles(cells, orig, header, out)
        return out

    def _label_only(self, cell: Cell) -> bool:
        """خلية بيها كلمة عنوان فقط مثل «الملاحظات» أو «تحميل البرنامج» جنب عنوان القسم."""
        return bool(_LABEL_ONLY.match(normalize(flat(self.body(cell), " "))))

    def _raw(self, cells: dict[int, Cell]) -> str:
        parts = [self.value(cell, "other") for _, cell in sorted(cells.items(), reverse=True)]
        parts = [part for part in parts if part]
        return " | ".join(parts) if any(len(part) > 2 for part in parts) else ""

    def _by_roles(self, cells: dict[int, Cell], orig: dict[int, Cell], header: Header, out: list[tuple]) -> None:
        roles = {col: header.find(col) for col in orig}
        for wanted in ("date", "airport", "notes", "program"):
            for col in sorted(orig, reverse=True):
                column = roles[col]
                if column and column.role == wanted:
                    out.append(("ctx", wanted, column.label, self.body(orig[col]), header))
        price_cols = header.of("price")
        has = lambda *group: any(roles[c] and roles[c].role in group for c in orig)  # noqa: E731
        if (price_cols and has("price")) or (not price_cols and has("other", "days", "single", "infant", "child")):
            out.append(("rec", Record(cells, header)))
        elif has(*HOTEL):
            line = self._hotel_line({c: x for c, x in orig.items() if roles[c] and roles[c].role in HOTEL}, header)
            last = next((item for item in reversed(out) if item[0] in ("rec", "header", "title")), None)
            if last and last[0] == "rec":
                last[1].extra_hotels.append(line)
            elif line:
                out.append(("line", f"🔹 {line}"))
        else:
            rest = {c: x for c, x in orig.items() if roles[c] and roles[c].role not in CONTEXT | {"skip"}}
            if rest:
                out.append(("raw", " | ".join(self._field(roles[c], x) for c, x in sorted(rest.items(), reverse=True))))
        unknown = {c: orig[c] for c in orig if roles[c] is None}
        if unknown:
            out.append(("raw", self._raw(unknown)))

    # -------------------------------------------------------- الصياغة

    def _field(self, column: Column, cell: Cell, with_label: bool = True) -> str:
        text = self.value(cell, column.role)
        return f"{column.label}: {text}" if with_label and column.label else text

    def _hotel_line(self, cells: dict[int, Cell], header: Header) -> str:
        hotel_cols = header.of("hotel")
        labelled = len({c.label for c in hotel_cols}) >= 2
        names = []
        for column in hotel_cols:
            for col in sorted((c for c in cells if column.start <= c <= column.end), reverse=True):
                name = flat(self.body(cells[col])).lstrip("- ")
                if name:
                    names.append(f"{column.label}: {name}" if labelled else name)
        location = "، ".join(
            flat(self.body(cells[col]), " ")
            for column in header.of("location")
            for col in sorted(c for c in cells if column.start <= c <= column.end)
            if self.body(cells[col]).strip()
        )
        rating = " ".join(
            self.stars(cells[col])
            for column in header.of("rating")
            for col in sorted(c for c in cells if column.start <= c <= column.end)
        )
        if "⭐" in location and "⭐" not in rating:  # العمودين معكوسين بالشيت
            location, rating = rating, location
        line = ("\n➕ " if labelled else " + ").join(names)
        if location:
            line = f"{line} — {location}" if line else location
        if rating:
            line = f"{line} — {rating}" if len(rating) > 25 else f"{line} ({rating})" if line else rating
        return line

    def _hotel_text(self, record: Record) -> str:
        header = record.header
        cells = {c: x for c, x in record.cells.items() if header.find(c) and header.find(c).role in HOTEL}
        return "\n".join(filter(None, [self._hotel_line(cells, header)] + [f"➕ {h}" for h in record.extra_hotels if h]))

    def _price_label(self, column: Column, header: Header) -> str:
        if len(header.of("price")) == 1 and _PLAIN_PRICE.match(normalize(column.label)):
            return ""
        return column.label

    def render(self) -> str:
        lines: list[str] = [f"⭕️ {self.sheet.title}"]
        blocks: list[list[tuple]] = [[]]  # كل عنوان أعمدة أو عنوان قسم يبدي قسم جديد
        for item in self.items():
            if item[0] in ("header", "title"):
                blocks.append([])
            blocks[-1].append(item)
        for block in blocks:
            self._render_block(block, lines)
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return text if self.agency else remove_commission_lines(text)

    def _context_line(self, role: str, text: str, header: Header) -> str:
        parts = [part.strip(' "') for part in text.split("\n")]
        parts = [part for part in parts if part and normalize(part) not in LINK_WORDS]
        if not parts:
            return ""
        if role == "program":
            if not header.of("hotel") and len(parts) <= 3 and sum(map(len, parts)) <= 90:
                return "🔹 " + " / ".join(parts)  # جدول بدون عمود فنادق: اسم البرنامج هو عنوان السطر
            icon = "✅" if "يشمل" in normalize(parts[0]) else "📋"
        else:
            icon = ICON[role]
        return f"{icon} " + "\n".join(parts)

    def _render_block(self, block: list[tuple], lines: list[str]) -> None:
        records = [item[1] for item in block if item[0] == "rec"]
        hotels = [self._hotel_text(record) for record in records]
        child_cols = records[0].header.of("infant", "child") if records else []
        child_cols.sort(key=lambda c: c.role != "infant")
        text_of = lambda record, column: self.amount(record.cells[column.start]) if column.start in record.cells else None  # noqa: E731
        hidden: set[tuple[int, int]] = set()  # (رقم السطر، العمود): قيمة الرضيع/الطفل تنكتب مرة وحدة بدل كل سطر
        after: dict[int, str] = {}  # سطر ينضاف بعد آخر مدة للفندق
        footer: list[str] = []

        def tag(column: Column, value: str) -> str:
            return f"{'👶' if column.role == 'infant' else '🧒'} {column.label}: {value}"

        def collapse(span: range, target: list[str] | None) -> None:
            parts = []
            for column in child_cols:
                if any((i, column.start) in hidden for i in span):
                    continue
                values = {text_of(records[i], column) for i in span}
                if len(values) == 1 and None not in values:
                    hidden.update((i, column.start) for i in span)
                    parts.append(tag(column, values.pop()))
            if parts and target is not None:
                target.extend(parts)
            elif parts:
                after[span[-1]] = " | ".join(parts)

        if len(records) >= 2:
            collapse(range(len(records)), footer)
            start = 0
            for i in range(1, len(records) + 1):
                if i == len(records) or hotels[i] != hotels[start]:
                    if i - start >= 2:
                        collapse(range(start, i), None)
                    start = i

        last_hotel = None
        last_ctx: dict[tuple[str, str], str] = {}
        pseudo_rows: list[dict[int, Cell]] = []
        pseudo_header: Header | None = None
        index = -1
        for item in block:
            kind = item[0]
            if kind == "header":
                pseudo_header = item[1] if item[1].pseudo else None
            elif kind == "title":
                text = flat(item[1], " — ")
                if normalize(text) != normalize(self.sheet.title):
                    icon = "💼" if "عمول" in text else "📌" if normalize(text).startswith(("ملاحظ", "تنويه")) else "📍"
                    lines += ["", f"{icon} {text}"]
            elif kind == "note":
                if not self.brief:
                    lines += ["", f"📌 {item[1]}"]
            elif kind == "ctx":
                _, role, label, text, header = item
                if last_ctx.get((role, label)) != text:
                    last_ctx[(role, label)] = text
                    lines += ["", self._context_line(role, text, header)]
            elif kind == "line":
                lines += ["", item[1]]
            elif kind == "raw":
                if item[1]:
                    lines.append(f"▫️ {item[1]}")
            elif kind == "pseudo":
                pseudo_rows.append(item[1])
            elif kind == "rec":
                index += 1
                last_hotel = self._render_record(records, index, hotels[index], hidden, last_hotel, lines)
                if index in after:
                    lines.append(after[index])
        if pseudo_header and pseudo_rows:
            for column in sorted(pseudo_header.columns, key=lambda c: -c.start):
                lines += ["", f"📍 {column.label}"]
                for cells in pseudo_rows:
                    parts = [self.value(x, "other") for c, x in sorted(cells.items()) if column.start <= c <= column.end]
                    parts = [part for part in parts if part]
                    if parts:
                        lines.append("▫️ " + " — ".join(parts))
        if footer:
            lines += [""] + footer

    def _shared(self, records: list[Record], index: int, col: int) -> bool:
        """الخلية مدموجة على أكثر من سطر سعر (قيمة مشتركة) لو خاصة بهذا السطر؟"""
        if not records[index].cells[col].original:
            return True
        following = records[index + 1].cells.get(col) if index + 1 < len(records) else None
        return following is not None and not following.original

    def _render_record(self, records: list[Record], index: int, hotel: str, hidden: set, last_hotel, lines: list[str]):
        record = records[index]
        header, cells = record.header, record.cells
        inline: list[str] = []  # أجزاء سطر السعر
        amounts: list[str] = []
        fields: list[tuple[Column, Cell]] = []
        shared: list[str] = []
        # 1) المدة وأي عمود غير معروف: المشترك بين أكثر من سطر ينكتب فوق مرة وحدة، والخاص ينكتب بسطر السعر
        for column in header.of("days", "other"):
            for col in sorted((c for c in cells if column.start <= c <= column.end), reverse=True):
                cell = cells[col]
                if not self.body(cell).strip():
                    continue
                if self._shared(records, index, col):
                    if cell.original:
                        text = self._field(column, cell, with_label=column.role == "other")
                        shared.append(f"{ICON['days'] if column.role == 'days' else '▫️'} {text}")
                elif column.role == "days":
                    inline.append(flat(self.body(cell), " – "))
                elif cell.number is not None and cell.unit:  # عمود سعر عنوانه مو معروف
                    amounts.append(f"{column.label}: {self.amount(cell)}" if column.label else self.amount(cell))
                else:
                    fields.append((column, cell))
        if shared:
            lines += [""] + shared
        # 2) سطر الفندق: ينكتب فقط إذا تغير عن السطر اللي قبله
        if hotel != last_hotel:
            if hotel:
                lines += ["", f"🔹 {hotel}"]
            elif last_hotel:
                lines.append("")
        # 3) سطر السعر
        if len(fields) == 1:
            inline.append(self._field(fields[0][0], fields[0][1], with_label=False))
        elif fields:
            lines += ["", "▪️ " + " | ".join(self._field(column, cell) for column, cell in fields)]
        for role in ("price", "single", "infant", "child"):
            for column in header.of(role):
                for col in sorted((c for c in cells if column.start <= c <= column.end), reverse=True):
                    if (index, col) in hidden or not self.body(cells[col]).strip():
                        continue
                    label = self._price_label(column, header) if role == "price" else column.label
                    text = self.amount(cells[col])
                    inline.append(f"{label}: {text}" if label else text)
        inline += amounts
        if inline:
            lines.append("⬅️ " + " | ".join(inline))
        return hotel


def render(sheet: Sheet, audience: str = "customer", brief: bool = False) -> str:
    """audience: customer (بدون عمولة) أو agency (ويا العمولة). brief: بدون الملاحظات الطويلة."""
    return Renderer(sheet, audience, brief).render()


def split_message(text: str, limit: int = 3900) -> list[str]:
    """يقسم النص الطويل على حدود الأسطر حتى ما يتجاوز حد تليكرام."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current.strip():
        chunks.append(current)
    return [chunk.strip("\n") for chunk in chunks if chunk.strip()]
