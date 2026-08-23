"""W-9 → fixed schema, read by LAYOUT, without a model.

The IRS form is fixed: every filled value sits in a known band between two
printed labels. The labels are big, printed text — every engine reads them,
even on a poor scan — so the reader anchors on them (fuzzy, OCR noise
tolerated) and takes the band's remaining lines as the value. One value per
engine, then the same vote as everywhere else: two engine families agreeing
confirm it, one reading alone goes to the operator for review.

The federal tax classification is a checkbox row. The box is found left of its
label; a text-layer glyph (✔ / X) inside decides, else the share of dark pixels
in the box on the 200-dpi render, measured against the other six boxes.
"""
from __future__ import annotations

import re
import statistics

from .common import Field, absent, family_of, norm_text, vote

# ── anchors: the printed labels, with the Rev. 10-2018 wording as alternates ──
ANCHORS = {
    "name": ("Name of entity/individual. An entry is required",
             "Name (as shown on your income tax return)"),
    "biz": ("Business name/disregarded entity name, if different from above",),
    "class": ("Check the appropriate box for federal tax classification",
              "Check appropriate box for federal tax classification"),
    "addr": ("Address (number, street, and apt. or suite no.)",),
    "city": ("City, state, and ZIP code",),
    "acct": ("List account number(s) here (optional)",),
    "ssn": ("Social security number",),
    "ein": ("Employer identification number",),
    "part2": ("Certification",),
    "req": ("Requester's name and address (optional)",),
}
# the seven boxes of line 3a, in print order; (key, label, max length factor)
BOXES = [
    ("individual_sole_prop", "Individual/sole proprietor", 2.4),   # 2018: "…or single-member LLC"
    ("corporation_c", "C corporation", 1.5),
    ("corporation_s", "S corporation", 1.5),
    ("partnership", "Partnership", 1.6),
    ("trust_estate", "Trust/estate", 1.6),
    ("llc", "LLC. Enter the tax classification", 4.0),             # 2018: "Limited liability company. Enter…"
    ("other", "Other (see instructions)", 1.6),
]
PRETTY = {"individual_sole_prop": "Individual/sole proprietor", "corporation_c": "C corporation",
          "corporation_s": "S corporation", "partnership": "Partnership", "trust_estate": "Trust/estate",
          "llc": "LLC", "other": "Other"}
_BOX_ALT = {"llc": ("Limited liability company. Enter the tax classification",)}
# printed text that may sit inside a value band and must never become a value
STOP = (
    "entity's name on line", "sole proprietor or disregarded entity", "owner's name on line",
    "See instructions", "See Specific Instructions", "Print or type", "Requester's name and address",
    "Give form to the", "requester. Do not", "send to the IRS", "only one of the following seven boxes",
    "Exemptions (codes apply only to", "certain entities, not individuals", "see instructions on page",
    "Name is required on this line", "do not leave this line blank", "Exempt payee code",
    "Before you begin", "Go to www.irs.gov", "Department of the Treasury", "Internal Revenue Service",
    "Request for Taxpayer", "Identification Number and Certification",
)
_MARK_LEAD = re.compile(r"^\s*[\[\(]?\s*[✔✓√vVxX■☒☑M]\s*[\]\)]?\s+\S")
_MUST = {"ein": ("EMPLOYER", "EMPLOY"), "ssn": ("SOCIAL", "SECURITY")}
_ENGINE_PREF = {"textlayer": 0, "rapidocr": 1, "tesseract": 2}
_MARKS = {"✔", "✓", "X", "x", "V", "v", "M", "Y", "[X]", "[x]", "☒", "■", "■"}
_LLC_CLASS = re.compile(r"^\W*([CSPcsp])\W*$")
_CSZ_RE = re.compile(r"^(?P<city>.+?)[,\s]+(?P<state>[A-Za-z]{2})[,\s]+(?P<zip>\d{5}(?:-\d{4})?)\W*$")
_REV_RE = re.compile(r"Rev\.?\s*((?:[A-Za-z]+\s+\d{4})|(?:\d{1,2}-\d{4}))")


def _ratio(a: str, b: str) -> float:
    from rapidfuzz import fuzz
    return fuzz.partial_ratio(a, b)


_LEAD = re.compile(r"^(?:[\W_]*\d{1,2}[ab]?\b[\W_]*|[\W_]+)")


def _match_label(text: str, label: str, max_len_factor: float, prefix: bool) -> bool:
    """Every printed label STARTS its line (after the line number), so the
    line's head is compared whole-string to the label — the instruction text
    that merely mentions "C corporation" or "disregarded entity" does not
    start with it and is not an anchor."""
    from rapidfuzz import fuzz
    t, l = norm_text(_LEAD.sub("", text.strip())), norm_text(label)
    if not t or not l:
        return False
    if prefix and len(t) > max_len_factor * len(l) + 6:
        return False
    lw, tw = l.split(" ", 1)[0], t.split(" ", 1)[0]
    if len(lw) == 1 and tw != lw:
        return False                     # "C corporation" is not "S corporation"
    head = t[: len(l) + 2]
    return fuzz.ratio(head, l) >= 78


def _lines(page: dict):
    for eid, lines in (page.get("lines") or {}).items():
        for ln in lines:
            if ln.get("bbox_pct") and (ln.get("text") or "").strip():
                yield eid, ln


def find_anchors(page: dict) -> dict[str, dict]:
    """key → {x0, y0, x1, y1} (page %), the median over the engines that read
    the label; `voices` lists them."""
    hits: dict[str, list] = {}
    for eid, ln in _lines(page):
        t = ln["text"]
        for key, labels in ANCHORS.items():
            if key in _MUST and not any(w in norm_text(t) for w in _MUST[key]):
                continue
            if any(_match_label(t, lab, 3.0, prefix=False) for lab in labels):
                hits.setdefault(key, []).append((eid, ln["bbox_pct"], t, labels[0], False))
        for key, label, factor in BOXES:
            labs = (label, *_BOX_ALT.get(key, ()))
            if any(_match_label(t, lab, factor, prefix=True) for lab in labs):
                # "√ C corporation": the engine read the tick INTO the label
                # line — the line's box then starts at the square, and the tick
                # itself is the best evidence there is
                marked = bool(_MARK_LEAD.match(t)) and norm_text(t).split(" ")[0] != norm_text(label).split(" ")[0]
                hits.setdefault("box:" + key, []).append((eid, ln["bbox_pct"], t, label, marked))
    out = {}
    for key, rows in hits.items():
        # one line per engine: the topmost candidate (the instruction text
        # repeats "Certification"/"Partnership" further down the page)
        per_engine: dict[str, tuple] = {}
        for eid, b, t, lab, marked in rows:
            if eid not in per_engine or b[1] < per_engine[eid][0][1]:
                per_engine[eid] = (b, t, lab, marked)
        # the box comes from the TIGHTEST reading: tesseract merges a whole
        # checkbox row into one line, whose x-span is no anchor for anything
        eid, (b, t, lab, marked) = min(per_engine.items(),
                                       key=lambda kv: (abs(len(norm_text(kv[1][1])) - len(norm_text(kv[1][2]))),
                                                       _ENGINE_PREF.get(family_of(kv[0]), 9)))
        out[key] = {"x0": b[0], "y0": b[1], "x1": b[2], "y1": b[3], "voices": sorted(per_engine), "engine": eid,
                    "marked_by": sorted(e for e, v in per_engine.items() if v[3]), "marked": marked}
    return out


def _is_stop(text: str) -> bool:
    t = norm_text(text)
    if len(t) < 2:
        return True
    return any(_ratio(t, norm_text(s)) >= 85 for s in STOP)


def _band_value(page: dict, y0: float, y1: float, x_max: float, *, skip_labels: bool = True) -> dict[str, str]:
    """engine_id → text of the lines whose centre lies in the band (y0, y1)
    and that start left of x_max, printed labels dropped."""
    out: dict[str, list] = {}
    for eid, ln in _lines(page):
        b = ln["bbox_pct"]
        cy = (b[1] + b[3]) / 2
        if not (y0 <= cy <= y1) or b[0] > x_max:
            continue
        t = ln["text"].strip()
        if skip_labels and _is_stop(t):
            continue
        if re.fullmatch(r"[\W_]+", t):
            continue
        out.setdefault(eid, []).append((b[1], b[0], t))
    return {eid: " ".join(t for _, _, t in sorted(v)) for eid, v in out.items()}


def _digits_in_band(page: dict, y0: float, y1: float, x_min: float) -> dict[str, str]:
    """The TIN boxes flatten to one digit per line: collect every digit-only
    line in the band, left to right, per engine."""
    out: dict[str, list] = {}
    for eid, ln in _lines(page):
        b = ln["bbox_pct"]
        cy = (b[1] + b[3]) / 2
        if not (y0 <= cy <= y1) or b[0] < x_min:
            continue
        t = ln["text"].strip()
        d = re.sub(r"\D", "", t)
        if d and (len(t) <= 3 or len(d) >= 9 or re.fullmatch(r"[\d\s\-–—|/\\.,;:)(\[\]]+", t)):
            out.setdefault(eid, []).append((b[0], d))
    res = {}
    for eid, items in out.items():
        digits = "".join(d for _, d in sorted(items))
        if len(digits) == 9:
            res[eid] = digits
        elif len(digits) > 9:
            # a box row read as one line plus a stray digit elsewhere: keep a
            # 9-digit run when the row itself holds one
            run = next((d for _, d in items if len(d) == 9), "")
            if run:
                res[eid] = run
    return res


def _field_from(page: dict, cands: dict[str, str], *, digits: bool = False) -> Field:
    if not cands:
        return absent()
    key = (lambda s: re.sub(r"\D", "", s)) if digits else norm_text
    raw, status, voices = vote(cands, key=key)
    bbox = _bbox_of(page, raw, digits=digits)
    return Field(value=raw, pretty=raw, status=status, page=int(page.get("page", 0)), bbox_pct=bbox,
                 evidence=raw, voices=voices)


def _bbox_of(page: dict, value: str, *, digits: bool = False) -> list | None:
    """Union box of the lines making up the value (a value may span lines)."""
    want = re.sub(r"\D", "", value) if digits else norm_text(value)
    if not want:
        return None
    best = None
    for eid, ln in _lines(page):
        t = ln["text"]
        have = re.sub(r"\D", "", t) if digits else norm_text(t)
        if have and (have in want if len(have) >= 3 else have == want):
            b = ln["bbox_pct"]
            best = [min(best[0], b[0]), min(best[1], b[1]), max(best[2], b[2]), max(best[3], b[3])] if best else list(b)
    return best


# ── the checkbox row ───────────────────────────────────────────────────────────

def _box_rect(anchor: dict) -> list[float]:
    """The square sits just left of its label, same height as the label line."""
    h = max(anchor["y1"] - anchor["y0"], 0.8)
    return [anchor["x0"] - 2.0 * h * 0.95, anchor["y0"] - 0.15 * h, anchor["x0"] - 0.25, anchor["y1"] + 0.15 * h]


def _search_window(anchor: dict) -> list[float]:
    """Where the square can be: left of the label, a little above/below it —
    or at the START of the label line when the engine read the tick into it."""
    h = max(anchor["y1"] - anchor["y0"], 0.8)
    # an OCR line box often begins AT the square (the detector saw tick and
    # text as one word), so the window always covers the line's first letters
    # too; the square's full-height borders outscore any letter stroke
    return [anchor["x0"] - 3.4 * h * 0.8, anchor["y0"] - 0.6 * h, anchor["x0"] + 1.4 * h, anchor["y1"] + 0.6 * h]


def _box_fill(img, window: list[float], side_pct: float) -> tuple[float, list[float] | None]:
    """Find the printed square inside the window and return the share of dark
    pixels in its centre (the middle 60 %) plus its rect in page %.

    The square is located as the best SQUARE whose four edges are dark: every
    column pair a box-width apart is tried against every row pair of about
    the same spacing, and the edges are scored only along the candidate's own
    sides — so the text line above the row or a letter stroke beside it
    cannot pose as a border. Without a square the window's right end is
    measured, which is where the box sits relative to its label."""
    w, h = img.size
    x0, y0, x1, y1 = (max(0, int(window[0] / 100 * w)), max(0, int(window[1] / 100 * h)),
                      min(w, int(window[2] / 100 * w)), min(h, int(window[3] / 100 * h)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return 0.0, None
    g = img.crop((x0, y0, x1, y1)).convert("L")
    iw, ih = g.size
    px = g.load()
    dark = [[1 if px[x, y] < 150 else 0 for x in range(iw)] for y in range(ih)]
    side = max(6, int(side_pct / 100 * h))          # the square is about a label line tall
    lo, hi = max(5, int(0.55 * side)), int(1.45 * side)

    def run(y, xa, xb):                              # dark share of a horizontal edge
        return sum(dark[y][x] for x in range(xa, xb)) / max(1, xb - xa)

    def col(x, ya, yb):                              # dark share of a vertical edge
        return sum(dark[yy][x] for yy in range(ya, yb)) / max(1, yb - ya)

    # candidate edges: columns/rows with SOME darkness (a 2-px border line is
    # at least `lo` px long; the profile filters the obvious blanks cheaply)
    cols = [x for x in range(iw) if sum(dark[y][x] for y in range(ih)) >= lo * 0.6]
    rows = [y for y in range(ih) if sum(dark[y][x] for x in range(iw)) >= lo * 0.6]
    best = None
    for i, c1 in enumerate(cols):
        for c2 in cols[i + 1:]:
            dx = c2 - c1
            if dx < lo:
                continue
            if dx > hi:
                break
            for j, r1 in enumerate(rows):
                for r2 in rows[j + 1:]:
                    dy = r2 - r1
                    if dy < lo or abs(dy - dx) > 0.3 * dx:
                        continue
                    if dy > hi:
                        break
                    sc = (run(r1, c1, c2 + 1) + run(r2, c1, c2 + 1) + col(c1, r1, r2 + 1) + col(c2, r1, r2 + 1)) / 4
                    if best is None or sc > best[0]:
                        best = (sc, c1, r1, c2, r2)
    if best and best[0] >= 0.55:
        _, bx0, by0, bx1, by1 = best
        rect = [(x0 + bx0) / w * 100, (y0 + by0) / h * 100, (x0 + bx1) / w * 100, (y0 + by1) / h * 100]
    else:
        bx1, bx0 = iw, max(0, iw - side)
        by0 = max(0, (ih - side) // 2)
        by1 = min(ih, by0 + side)
        rect = None
    mx = max(1, int((bx1 - bx0) * 0.2))
    my = max(1, int((by1 - by0) * 0.2))
    cx0, cx1, cy0, cy1 = bx0 + mx, bx1 - mx, by0 + my, by1 - my
    if cx1 - cx0 < 2 or cy1 - cy0 < 2:
        return 0.0, rect
    n = (cx1 - cx0) * (cy1 - cy0)
    d = sum(dark[y][x] for y in range(cy0, cy1) for x in range(cx0, cx1))
    return d / n, rect


def classification(page: dict, anchors: dict, page_image=None) -> tuple[Field, Field]:
    """→ (classification, llc_tax_class). Evidence order: a mark glyph read by
    an engine inside a box; else the darkest box on the render by a clear
    margin; else review with the best guess."""
    boxes = [(k, anchors.get("box:" + k)) for k, _, _ in BOXES]
    boxes = [(k, a) for k, a in boxes if a]
    if not boxes:
        return absent(), absent()
    rects = {k: _box_rect(a) for k, a in boxes}
    windows = {k: _search_window(a) for k, a in boxes}
    # 1. glyph evidence: a short mark line whose centre falls in a box rect (with slack)
    glyph: dict[str, set] = {}
    for eid, ln in _lines(page):
        t = ln["text"].strip()
        if t not in _MARKS and not re.fullmatch(r"[\[\(]?\s*[xX✔✓vVM]\s*[\]\)]?", t):
            continue
        b = ln["bbox_pct"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        for k, r in rects.items():
            if r[0] - 1.0 <= cx <= r[2] + 0.6 and r[1] - 0.4 <= cy <= r[3] + 0.4:
                glyph.setdefault(k, set()).add(eid)
    for k, a in boxes:
        for e in a.get("marked_by") or []:
            glyph.setdefault(k, set()).add(e)
    # 2. pixel evidence
    dark: dict[str, float] = {}
    if page_image is not None:
        try:
            from PIL import Image
            with Image.open(page_image) as im:
                for k, win in windows.items():
                    an = anchors["box:" + k]
                    dark[k], found = _box_fill(im, win, max(an["y1"] - an["y0"], 0.8))
                    if found:
                        rects[k] = found
        except Exception:
            dark = {}
    chosen, status, voices, evidence = "", "absent", [], ""
    ranked_dbg = ", ".join(f"{k}={v:.0%}" for k, v in sorted(dark.items(), key=lambda kv: -kv[1]))
    if glyph:
        chosen = max(glyph, key=lambda k: (len({family_of(e) for e in glyph[k]}), dark.get(k, 0)))
        fams = {family_of(e) for e in glyph[chosen]}
        voices = sorted(glyph[chosen])
        evidence = f"mark read in the {chosen} box by {', '.join(voices)}"
        status = "confirmed" if len(fams) >= 2 or (dark and chosen == max(dark, key=dark.get)
                                                   and dark[chosen] >= 0.06) else "review"
    if dark and not chosen:
        ranked = sorted(dark.items(), key=lambda kv: -kv[1])
        top, second = ranked[0], (ranked[1] if len(ranked) > 1 else (None, 0.0))
        if top[1] >= 0.05 and top[1] >= 1.8 * max(second[1], 0.01):
            chosen, voices = top[0], ["pixels"]
            evidence = f"{top[0]} box {top[1]:.0%} dark, next {second[0]} {second[1]:.0%}"
            status = "confirmed" if top[1] >= 2.5 * max(second[1], 0.01) and top[1] >= 0.10 else "review"
        elif top[1] >= 0.04:
            chosen, voices, status = top[0], ["pixels"], "review"
            evidence = f"{top[0]} box {top[1]:.0%} dark, next {second[0]} {second[1]:.0%} — not clear-cut"
    if not chosen:
        return Field(status="absent", evidence=f"no box marked ({ranked_dbg})"), absent()
    a = anchors["box:" + chosen]
    cls = Field(value=chosen, pretty=PRETTY[chosen], status=status,
                page=int(page.get("page", 0)), bbox_pct=[rects[chosen][0], rects[chosen][1], a["x1"], rects[chosen][3]],
                evidence=f"{evidence} [{ranked_dbg}]", voices=voices)
    llc = absent()
    if chosen == "llc":
        la = anchors["box:llc"]
        cands = {}
        for eid, ln in _lines(page):
            b = ln["bbox_pct"]
            cy = (b[1] + b[3]) / 2
            m = _LLC_CLASS.match(ln["text"])
            if m and la["y0"] - 0.6 <= cy <= la["y1"] + 0.6 and b[0] > la["x0"] + 20:
                cands[eid] = m.group(1).upper()
        if cands:
            raw, st, vs = vote(cands, key=lambda s: s.upper())
            llc = Field(value=raw, pretty={"C": "C corporation", "S": "S corporation", "P": "Partnership"}[raw],
                        status=st, page=int(page.get("page", 0)), bbox_pct=_bbox_of(page, raw) if False else None,
                        evidence=f"LLC tax class letter {raw}", voices=vs)
    return cls, llc


# ── the schema ────────────────────────────────────────────────────────────────

def revision(page: dict) -> str | None:
    for text in (page.get("readings") or {}).values():
        m = _REV_RE.search(text or "")
        if m:
            return m.group(1).replace("March", "3-").replace("October", "10-").replace("- ", "-").strip()
    return None


def read(doc: dict, page_image=None) -> tuple[dict[str, dict], dict]:
    pages = doc.get("pages_out") or []
    if not pages:
        return {}, {}
    page = pages[0]                                  # a W-9 is its first page
    a = find_anchors(page)
    fields: dict[str, Field] = {}

    def band(top: str, bottom: str, default_h: float, x_max: float) -> tuple[float, float] | None:
        if top not in a:
            return None
        y0 = a[top]["y1"]
        y1 = a[bottom]["y0"] if bottom in a and a[bottom]["y0"] > y0 else y0 + default_h
        return (y0 - 0.2, y1 + 0.2) if y1 > y0 else None

    # the name label wraps to two lines on Rev. 3-2024 — its value band starts
    # below the second line, which the STOP list removes anyway
    spec = {
        "line1_name": ("name", "biz", 4.5, 72.0),
        "line2_business_name": ("biz", "class", 3.5, 72.0),
        "address_street": ("addr", "city", 3.5, 62.0),
        "address_city_state_zip": ("city", "acct", 3.5, 62.0),
    }
    for key, (top, bottom, h, xmax) in spec.items():
        rng = band(top, bottom, h, xmax)
        if rng is None:
            fields[key] = absent()
            continue
        cands = _band_value(page, rng[0], rng[1], xmax)
        fields[key] = _field_from(page, cands)
    # city / state / zip split
    csz = fields["address_city_state_zip"]
    m = _CSZ_RE.match(csz.value.replace("  ", " ")) if csz.value else None
    for k in ("address_city", "address_state", "address_zip"):
        fields[k] = absent()
    if m:
        for k, g in (("address_city", "city"), ("address_state", "state"), ("address_zip", "zip")):
            fields[k] = Field(value=m.group(g).strip(" ,"), pretty=m.group(g).strip(" ,"), status=csz.status,
                              page=csz.page, bbox_pct=csz.bbox_pct, evidence=csz.value, voices=csz.voices)
        fields["address_state"].value = fields["address_state"].value.upper()

    # TIN: the digit boxes under the SSN / EIN labels
    tin, tin_type = absent(), absent()
    for kind in ("ein", "ssn"):
        if kind not in a:
            continue
        y0 = a[kind]["y1"] - 0.2
        cands = _digits_in_band(page, y0, y0 + 4.0, a[kind]["x0"] - 6)
        if cands:
            f = _field_from(page, cands, digits=True)
            if f.value:
                tin = f
                tin.pretty = (f"{f.value[:2]}-{f.value[2:]}" if kind == "ein" else f"{f.value[:3]}-{f.value[3:5]}-{f.value[5:]}")
                tin_type = Field(value=kind, pretty=kind.upper(), status=f.status, page=f.page, bbox_pct=f.bbox_pct,
                                 evidence=f"digits under the {kind.upper()} label", voices=f.voices)
                break
    if not tin.value:
        # a 9-digit run anywhere in the TIN rows (one engine read the boxes as one line)
        for kind in ("ein", "ssn"):
            if kind in a:
                y0 = a[kind]["y1"] - 0.2
                cands = {}
                for eid, ln in _lines(page):
                    b = ln["bbox_pct"]
                    cy = (b[1] + b[3]) / 2
                    d = re.sub(r"\D", "", ln["text"])
                    if y0 <= cy <= y0 + 4.0 and b[0] >= a[kind]["x0"] - 6 and len(d) == 9:
                        cands[eid] = d
                if cands:
                    tin = _field_from(page, cands, digits=True)
                    tin.status = "review"
                    tin_type = Field(value=kind, pretty=kind.upper(), status="review", page=tin.page,
                                     evidence="9 digits under the label, read as one line", voices=tin.voices)
                    break
    fields["tin"], fields["tin_type"] = tin, tin_type

    cls, llc = classification(page, a, page_image)
    fields["classification"], fields["llc_tax_class"] = cls, llc
    fields["sign_date"] = absent()
    extra = {"w9_revision": revision(page),
             "anchors": {k: v for k, v in a.items()},
             "anchors_found": sorted(a)}
    return {k: v.as_dict() for k, v in fields.items()}, extra
