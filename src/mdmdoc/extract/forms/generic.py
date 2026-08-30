"""Any document → the bank schema plus the company fields a vendor form also
carries (name / address / contacts / tax and registration identifiers).

Values are read LABEL-ANCHORED from every engine's transcript and voted the
same way as the bank reader's narrative fields: two engine families agreeing
confirm a value, one reading alone is handed over for review. Per-key
validators keep obvious mismatches (a label with prose after it) out of the
schema — a value that fails validation is simply not a candidate.
"""
from __future__ import annotations

import re

from . import bank as bank_reader
from .common import Field, absent, anchored, find_line, lines_of, looks_like_label, vote

# label → value on the same line, or on the next non-empty line.
LABELS = {
    "company_name": re.compile(
        r"(?i)^\W*(?:firma|firmenname|firmierung|name\s+des\s+unternehmens|unternehmensname|"
        r"company(?:\s*name)?|legal\s*name|raison\s*sociale|ragione\s*sociale|razón\s*social|"
        r"denominazione|empresa)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "street": re.compile(
        r"(?i)^\W*(?:stra(?:ß|ss)e(?:\s*/?\s*hausnummer|\s*und\s*hausnummer)?|str\.|anschrift|adresse|"
        r"address(?:\s*line\s*1)?|street(?:\s*address)?|calle|via|rue)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "postal_code": re.compile(
        r"(?i)^\W*(?:plz|postleitzahl|postal\s*code|post\s*code|zip(?:\s*code)?|code\s*postal|"
        r"c\.?p\.?|cap)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "city": re.compile(
        r"(?i)^\W*(?:ort|stadt|city|town|ville|ciudad|citt[àa]|localit[àa])\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "country": re.compile(
        r"(?i)^\W*(?:land|country|pays|pa[íi]s|paese|staat)\s*[:：\-–|]\s*(?P<v>.*)$"),
    "phone": re.compile(
        r"(?i)^\W*(?:telefon|tel\.?(?:efon)?(?:\s*nr\.?)?|phone(?:\s*number)?|telephone|t[ée]l[ée]phone|"
        r"tel[ée]fono|telefono)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "fax": re.compile(r"(?i)^\W*(?:telefax|fax(?:\s*nr\.?|\s*number)?)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "email": re.compile(
        r"(?i)^\W*(?:e-?mail(?:\s*address)?|mail|correo(?:\s*electr[óo]nico)?|courriel)"
        r"\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "website": re.compile(
        r"(?i)^\W*(?:internet|web\s*site|website|web|homepage|sitio\s*web|url)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "vat_id": re.compile(
        r"(?i)^\W*(?:umsatzsteuer-?\s*ident(?:ifikations)?-?\s*nummer|ust\.?[-\s]?id(?:\s*nr\.?)?|"
        r"uid(?:\s*nr\.?)?|vat\s*(?:reg(?:istration)?\s*)?(?:id|no\.?|number)?|"
        r"n[°o]?\.?\s*tva|partita\s*iva|p\.?\s*iva|btw(?:\s*nr\.?)?|mwst\.?[-\s]?nr\.?)"
        r"\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "tax_number": re.compile(
        r"(?i)^\W*(?:steuernummer|steuer-?nr\.?|tax\s*(?:number|no\.?)|n[°o]?\.?\s*fiscal|"
        r"codice\s*fiscale)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "registration": re.compile(
        r"(?i)^\W*(?:handelsregister(?:nummer|-?nr\.?)?|registergericht|hrb|hra|"
        r"commercial\s*register|company\s*reg(?:istration)?(?:\s*(?:no\.?|number))?|"
        r"registro\s*mercantil|rcs)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "duns": re.compile(r"(?i)^\W*(?:d-?u-?n-?s(?:[-\s]*nr\.?|[-\s]*no\.?|\s*number)?)\s*[:：\-–|]?\s*(?P<v>.*)$"),
    "management": re.compile(
        r"(?i)^\W*(?:gesch[äa]ftsf[üu]hr(?:er|ung)|unternehmensleitung|managing\s*director|"
        r"ceo|inhaber|gerente|amministratore)\s*[:：\-–|]?\s*(?P<v>.*)$"),
}

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"(?i)(?:https?://|www\.)\S+|\b[\w-]+\.(?:com|de|net|org|eu|fr|it|es|co\.uk)\b")
_EU_VAT = re.compile(r"^[A-Z]{2}\s?[0-9A-Z]{8,12}$")


def _some_digits(n: int):
    return lambda v: len(re.sub(r"\D", "", v)) >= n


def _short_text(v: str) -> bool:
    return 2 < len(v) <= 80 and bool(re.search(r"[^\W\d_]{2}", v))


# a candidate that fails its validator is dropped before the vote
_VALID = {
    "company_name": _short_text,
    "street": _short_text,
    "postal_code": lambda v: bool(re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z\s-]{2,9}", v)) and any(c.isdigit() for c in v),
    "city": _short_text,
    "country": lambda v: _short_text(v) and len(v) <= 40 and not any(c.isdigit() for c in v),
    "phone": _some_digits(6),
    "fax": _some_digits(6),
    "email": lambda v: bool(_EMAIL.search(v)),
    "website": lambda v: bool(_URL.search(v)),
    "vat_id": lambda v: bool(_EU_VAT.fullmatch(re.sub(r"[\s.]", "", v).upper())) or _some_digits(8)(v),
    "tax_number": _some_digits(5),
    "registration": _short_text,
    "duns": lambda v: len(re.sub(r"\D", "", v)) == 9,
    "management": _short_text,
}

_TIDY = {
    "email": lambda v: (_EMAIL.search(v).group(0) if _EMAIL.search(v) else v),
    "website": lambda v: (_URL.search(v).group(0).rstrip(".,;") if _URL.search(v) else v),
    "vat_id": lambda v: re.sub(r"[\s.]", "", v).upper() if _EU_VAT.fullmatch(re.sub(r"[\s.]", "", v).upper()) else v,
    "duns": lambda v: re.sub(r"\D", "", v),
}

_BARE_ID_LABEL = re.compile(r"(?i)^\W*(?:iban|bic|swift|blz|bankleitzahl|kontonummer|account\s*(?:no\.?|number)?|"
                            r"routing|aba|sort\s*code|bank\s*code)\s*[:：\-–|]?\s*(?P<v>.*)$")


def _is_label_line(s: str) -> bool:
    """A "value" that is itself a LABEL — the next line of a label-column
    layout, not a value. Knows the bank reader's labels too: after a bare
    "Firma" the next transcript line may well be "IBAN: DE…", and that is the
    bank's field, never the company's name (Codex review 2026-08-30)."""
    for rx in list(LABELS.values()) + list(bank_reader.LABELS.values()) + [_BARE_ID_LABEL]:
        m = rx.match(s)
        if m and (rx is _BARE_ID_LABEL or not (m.group("v") or "").strip(" :：-–|\t")):
            return True
    return False


def _row_anchored(page: dict, rx: re.Pattern) -> dict[str, str]:
    """Label and value as they sit ON THE PAGE: an inline "label: value" line
    wins; a bare label takes the nearest line on the same visual row to its
    right (two-column layouts, where the transcript's next line is just the
    next label). Falls back to the transcript when an engine has no line
    geometry. → engine_id → value."""
    out: dict[str, str] = {}
    for eid, lines in lines_of(page).items():
        for ln in lines:
            t = (ln.get("text") or "").strip()
            m = rx.match(t)
            if not m:
                continue
            val = (m.group("v") or "").strip(" :：-–|\t")
            if val and not looks_like_label(val) and not _is_label_line(val):
                out[eid] = val
                break
            bb = ln.get("bbox_pct")
            if not bb:
                continue
            y_mid, x_end = (bb[1] + bb[3]) / 2, bb[2]
            best = None
            for cand in lines:
                cb, ct = cand.get("bbox_pct"), (cand.get("text") or "").strip()
                if cand is ln or not cb or not ct:
                    continue
                c_mid = (cb[1] + cb[3]) / 2
                if abs(c_mid - y_mid) <= max(1.0, (bb[3] - bb[1])) and cb[0] >= x_end - 1:
                    if best is None or cb[0] < best[0]:
                        best = (cb[0], ct)
            if best and not looks_like_label(best[1]) and not _is_label_line(best[1]):
                out[eid] = best[1]
                break
    for eid, val in anchored({e: t for e, t in (page.get("readings") or {}).items() if e not in out}, rx).items():
        if not _is_label_line(val):
            out[eid] = val
    return out


_DE_POSTAL_CITY = re.compile(r"^(?P<zip>\d{4,5})\s+(?P<city>[^\d].*)$")
_STREET_TAIL = re.compile(r"^(?P<street>.+?),\s*(?P<zip>\d{4,5})\s+(?P<city>[^\d,]{2,}?)\s*$")
_ADDRESS_LABEL = re.compile(r"(?i)^\W*(?:anschrift|adresse|address)\s*[:：\-–|]?\s*(?P<v>.*)$")


def _address_block(readings: dict[str, str]) -> dict[str, dict[str, str]]:
    """"Anschrift" with the value spread over the following lines: up to three
    lines after the label; a "12345 Town" line is postal code + city, the rest
    is the street. → {key: {engine_id: value}}."""
    out: dict[str, dict[str, str]] = {"street": {}, "postal_code": {}, "city": {}}
    for eid, text in (readings or {}).items():
        lines = [ln.strip() for ln in (text or "").split("\n")]
        for i, ln in enumerate(lines):
            m = _ADDRESS_LABEL.match(ln)
            if not m:
                continue
            tail = [v for v in ([m.group("v").strip(" :：-–|\t")] if m.group("v").strip(" :：-–|\t") else [])]
            tail += [l for l in lines[i + 1:i + 4] if l.strip()][:3 - len(tail)]
            street: list[str] = []
            for part in tail:
                if _is_label_line(part):
                    break                      # a label column, not an address value
                pc = _DE_POSTAL_CITY.match(part)
                if pc and not out["postal_code"].get(eid):
                    out["postal_code"][eid] = pc.group("zip")
                    out["city"][eid] = pc.group("city").strip()
                elif len(street) < 2:
                    street.append(part)
            if street:
                out["street"][eid] = ", ".join(street)
            break
    return out


def _tax_id_tokens(pages: list[dict]) -> tuple[list[Field], list[Field]]:
    """Consensus tokens the extractor already labeled "tax id": an EU-format
    value (DE245809737) is a VAT id candidate, the rest are tax numbers."""
    vats, taxes = [], []
    for pg in pages:
        pno = int(pg.get("page", 0))
        for e in pg.get("fields") or []:
            if (e.get("kind") or "") != "tax id":
                continue
            f = bank_reader._token_field(e, pno)
            compact = re.sub(r"[\s.]", "", f.value).upper()
            label = (e.get("label") or "").lower()
            if _EU_VAT.fullmatch(compact) or "ust" in label or "vat" in label or "tva" in label:
                f.value = f.pretty = compact if _EU_VAT.fullmatch(compact) else f.value
                vats.append(f)
            else:
                taxes.append(f)
    return vats, taxes


def read(doc: dict, *, bank: tuple[dict, dict] | None = None) -> tuple[dict[str, dict], dict]:
    """→ (fields, extra): the bank reader's schema (keys the host already
    compares) plus the generic company keys above."""
    bank_fields, extra = bank if bank is not None else bank_reader.read(doc)
    pages = doc.get("pages_out") or []

    fields: dict[str, Field] = {}
    for key, rx in LABELS.items():
        for pg in pages:
            cands = _row_anchored(pg, rx)
            check = _VALID.get(key)
            cands = {e: v for e, v in cands.items() if not check or check(v)}
            if not cands:
                continue
            tidy = _TIDY.get(key)
            if tidy:
                cands = {e: tidy(v) for e, v in cands.items()}
            raw, status, voices = vote(cands)
            eid, bbox = find_line(pg, raw)
            fields[key] = Field(value=raw, pretty=raw, status=status, page=int(pg.get("page", 0)),
                                bbox_pct=bbox, evidence=raw, voices=voices)
            break
        fields.setdefault(key, absent())

    # "Waiblinger Straße 116, 70734 Fellbach" in one line: split the tail
    if fields["street"].value and not (fields["postal_code"].value or fields["city"].value):
        m = _STREET_TAIL.match(fields["street"].value)
        if m:
            src = fields["street"]
            fields["street"] = Field(value=m.group("street"), pretty=m.group("street"), status=src.status,
                                     page=src.page, bbox_pct=src.bbox_pct, evidence=src.evidence, voices=src.voices)
            for key, grp in (("postal_code", "zip"), ("city", "city")):
                fields[key] = Field(value=m.group(grp), pretty=m.group(grp), status=src.status, page=src.page,
                                    bbox_pct=src.bbox_pct, evidence=src.evidence, voices=src.voices)

    # "Anschrift" laid out label-above-value across several lines
    if not (fields["street"].value and fields["postal_code"].value and fields["city"].value):
        for pg in pages:
            block = _address_block(pg.get("readings") or {})
            done = False
            for key in ("street", "postal_code", "city"):
                if fields[key].value or not block[key]:
                    continue
                raw, status, voices = vote(block[key])
                eid, bbox = find_line(pg, raw)
                fields[key] = Field(value=raw, pretty=raw, status=status, page=int(pg.get("page", 0)),
                                    bbox_pct=bbox, evidence=raw, voices=voices)
                done = True
            if done:
                break

    # free-standing tokens when no label matched
    if not fields["email"].value:
        cands = {eid: m.group(0) for pg in pages for eid, text in (pg.get("readings") or {}).items()
                 if (m := _EMAIL.search(text or ""))}
        if cands:
            raw, status, voices = vote(cands, key=lambda s: s.lower())
            fields["email"] = Field(value=raw, pretty=raw, status=status, evidence="e-mail token", voices=voices)
    vats, taxes = _tax_id_tokens(pages)
    if not fields["vat_id"].value and vats:
        fields["vat_id"] = bank_reader._pick(vats)
    if not fields["tax_number"].value and taxes:
        fields["tax_number"] = bank_reader._pick(taxes)

    out = dict(bank_fields)
    for key, f in fields.items():
        out[key] = f.as_dict()
    return out, extra
