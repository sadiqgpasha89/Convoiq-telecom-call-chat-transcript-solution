"""PII / PCI detectors (deterministic, validator-backed, dependency-free).

Design goals (spec sections 5.2 & 12.1):
* **High recall on regulated identifiers** (PAN, CVV, SSN, PIN, DOB, phone, e-mail, address, IMEI).
  Precision is protected with validators (Luhn, SSN area rules, context cues) because over-redaction
  of benign numbers (order ids, prices, tower ids) destroys downstream analytics.
* **Speech-aware**: ASR output spells numbers ("four one one one ...").  Spoken digit runs are
  converted, validated and redacted.
* **Conversation-aware**: customers answer "05/04/1985" or "4819" without repeating the question.
  ``Cue`` flags derived from the previous agent turn promote bare digit strings to PIN/DOB/PHONE.
* **Pluggable**: ``Detector`` objects can be extended with Presidio/spaCy/Vertex DLP adapters; the
  default set has no heavy dependencies so it can run in a constrained sovereign edge zone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, Protocol

DIGIT_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9"}
_DW_ALT = "|".join(sorted(DIGIT_WORDS, key=len, reverse=True))

# priority for overlap resolution: higher wins
PRIORITY = {"CARD": 100, "SSN": 95, "CVV": 90, "PIN": 88, "IMEI": 85, "EMAIL": 84, "IP": 70, "ADDRESS": 82,
            "DOB": 80, "EXPIRY": 78, "PHONE": 75, "ACCOUNT": 72, "ICCID": 71, "NAME": 60}


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    type: str
    value: str
    score: float = 1.0
    source: str = "regex"

    def overlaps(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


@dataclass
class Cue:
    """What the *previous* agent turn asked for - promotes bare answers to PII."""
    pin: bool = False
    dob: bool = False
    phone: bool = False
    card: bool = False
    ssn: bool = False
    account: bool = False
    name: bool = False

    @classmethod
    def from_agent_text(cls, text: str) -> "Cue":
        t = text.lower()
        return cls(
            pin=bool(re.search(r"\b(pin|passcode|pass code|security code|password)\b", t)),
            dob=bool(re.search(r"date of birth|birth ?date|\bdob\b|birthday|when were you born", t)),
            phone=bool(re.search(r"phone number|number on the account|which number|mobile number|number are you calling|contact number", t)),
            card=bool(re.search(r"card number|credit card|debit card|payment card", t)),
            ssn=bool(re.search(r"social security|\bssn\b|last four of your social", t)),
            account=bool(re.search(r"account number|account no", t)),
            name=bool(re.search(r"your (full )?name|who am i speaking|may i have your name", t)),
        )


class Detector(Protocol):
    name: str

    def detect(self, text: str, cue: Cue) -> list[Span]: ...


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit() or len(digits) < 12:
        return False
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _card_prefix_ok(d: str) -> bool:
    return (d[0] in "3456" and len(d) in (13, 14, 15, 16, 17, 18, 19)) or d.startswith("62")


def spoken_runs(text: str, min_len: int = 3) -> list[tuple[int, int, str]]:
    """Find runs of spoken digit words; returns (start, end, digits)."""
    out = []
    for m in re.finditer(rf"\b(?:(?:{_DW_ALT})(?:[\s,.-]+|\b)){{{min_len},}}", text, flags=re.I):
        raw = m.group(0)
        end = m.end()
        while end > m.start() and text[end - 1] in " ,.-":
            end -= 1
        words = re.findall(rf"\b(?:{_DW_ALT})\b", raw, flags=re.I)
        if len(words) < min_len:
            continue
        # a run made only of "oh" is not a number
        if all(w.lower() == "oh" for w in words):
            continue
        out.append((m.start(), end, "".join(DIGIT_WORDS[w.lower()] for w in words)))
    return out


# ---------------------------------------------------------------------------------------
class CardDetector:
    name = "card"
    _rx = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d])")

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = []
        for m in self._rx.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if 13 <= len(digits) <= 19 and luhn_ok(digits) and _card_prefix_ok(digits):
                spans.append(Span(m.start(), m.end(), "CARD", m.group(0)))
        for s, e, digits in spoken_runs(text, 13):
            if 13 <= len(digits) <= 19 and luhn_ok(digits):
                spans.append(Span(s, e, "CARD", text[s:e], 0.9, "spoken"))
        return spans


class CvvDetector:
    name = "cvv"
    _rx = re.compile(r"\b(?:cvv2?|cvc|csc|security code|card verification(?: code| value)?)\b[^\d\w]{0,12}(?:is|:)?[\s]*(\d{3,4})\b", re.I)
    _rx_sp = re.compile(rf"\b(?:cvv2?|cvc|security code)\b[\s,:]*(?:is\s+)?((?:(?:{_DW_ALT})[\s,]*){{3,4}})", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = [Span(m.start(1), m.end(1), "CVV", m.group(1)) for m in self._rx.finditer(text)]
        for m in self._rx_sp.finditer(text):
            val = m.group(1).rstrip(" ,")
            spans.append(Span(m.start(1), m.start(1) + len(val), "CVV", val, 0.9, "spoken"))
        return spans


class ExpiryDetector:
    name = "expiry"
    _rx = re.compile(r"\b(?:exp(?:iry|ires|iration)?(?: date)?|valid thru|good thru)\b[^\d]{0,12}((?:0?[1-9]|1[0-2])\s*[/-]\s*(?:\d{2}|\d{4}))\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        return [Span(m.start(1), m.end(1), "EXPIRY", m.group(1)) for m in self._rx.finditer(text)]


class SsnDetector:
    name = "ssn"
    _rx = re.compile(r"(?<![\d-])(\d{3})-(\d{2})-(\d{4})(?![\d-])")
    _rx_ctx = re.compile(r"\b(?:ssn|social security(?: number)?)\b[^\d]{0,15}(\d{9}|\d{4})\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = []
        for m in self._rx.finditer(text):
            a, g, s = m.groups()
            if a in ("000", "666") or a.startswith("9") or g == "00" or s == "0000":
                continue
            spans.append(Span(m.start(), m.end(), "SSN", m.group(0)))
        for m in self._rx_ctx.finditer(text):
            spans.append(Span(m.start(1), m.end(1), "SSN", m.group(1), 0.9, "context"))
        return spans


class EmailDetector:
    name = "email"
    _rx = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
    _rx_spoken = re.compile(r"\b[\w.]+\s+at\s+[\w.]+\s+dot\s+(?:com|org|net|edu|io)\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = [Span(m.start(), m.end(), "EMAIL", m.group(0)) for m in self._rx.finditer(text)]
        spans += [Span(m.start(), m.end(), "EMAIL", m.group(0), 0.7, "spoken") for m in self._rx_spoken.finditer(text)]
        return spans


class IpDetector:
    name = "ip"
    _rx = re.compile(r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])")

    @staticmethod
    def _private(ip: str) -> bool:
        """RFC1918 / loopback / link-local addresses identify a LAN device (e.g. a router admin page), not a subscriber."""
        a, b = (int(x) for x in ip.split(".")[:2])
        return a in (10, 127) or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or (a == 169 and b == 254)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        return [Span(m.start(), m.end(), "IP", m.group(0)) for m in self._rx.finditer(text) if not self._private(m.group(0))]


class PhoneDetector:
    name = "phone"
    _rx = re.compile(
        r"(?<![\w.])(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?![\d])"
    )
    _intl = re.compile(r"(?<![\w.])\+\d{1,3}[\s.-]?\d{2,4}[\s.-]?\d{3,4}[\s.-]?\d{3,4}(?![\d])")

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = []
        for rx in (self._rx, self._intl):
            for m in rx.finditer(text):
                digits = re.sub(r"\D", "", m.group(0))
                if not 10 <= len(digits) <= 13:
                    continue
                if luhn_ok(digits) and len(digits) >= 15:      # card/imei handled elsewhere
                    continue
                spans.append(Span(m.start(), m.end(), "PHONE", m.group(0)))
        for s, e, digits in spoken_runs(text, 7):
            if 7 <= len(digits) <= 11 and not luhn_ok(digits):
                spans.append(Span(s, e, "PHONE", text[s:e], 0.85, "spoken"))
        return spans


class PinDetector:
    name = "pin"
    _rx = re.compile(r"\b(?:pin|passcode|pass code|passphrase|security pin|voicemail pin|transfer pin)\b(?:\s+(?:number|code))?[^\d\w]{0,15}(?:is|:)?\s*(\d{4,8})\b", re.I)
    _rx_sp = re.compile(rf"\b(?:pin|passcode)\b[\s,:]*(?:is\s+)?((?:(?:{_DW_ALT})[\s,]*){{4,8}})", re.I)
    # "PIN as 4821", "PIN on file is 4821", "PIN currently set to 4821": a bounded allow-list of connector words (never free text)
    _rx_conn = re.compile(r"\b(?:pin|passcode|pass code)\b(?:\s*(?:is|as|was|on file|on (?:the|your|my) account|currently|set to|should be|"
                          r"number|code|of|the|your|my|that|reads?|has been|been|reset to|changed to|updated to|now|:)){1,5}[\s:,]*(\d{4,8})\b", re.I)
    # "4821 is your pin"
    _rx_rev = re.compile(r"(?<![\d$.,/-])\b(\d{4,8})\b(?=\s*(?:is|was|as)\s+(?:your|my|the|their)\s+(?:pin|passcode|pass code)\b)", re.I)
    _bare = re.compile(r"(?<![\d$.,/-])\b(\d{4,8})\b(?!\d)(?![.,]\d)(?![/%-])")
    # "card ending in 1234" / "last four digits are 1234" reference a masked suffix, not a secret
    _suffix_ref = re.compile(r"(?:ending|ends|ended)\s+(?:in|with)\s+$|last\s+(?:four|4)(?:\s+digits)?(?:\s+(?:is|are|of\s+\w+))?[\s:]*$", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = [Span(m.start(1), m.end(1), "PIN", m.group(1)) for m in self._rx.finditer(text)]
        spans += [Span(m.start(1), m.end(1), "PIN", m.group(1), 0.95, "connector") for m in self._rx_conn.finditer(text)]
        spans += [Span(m.start(1), m.end(1), "PIN", m.group(1), 0.95, "reverse") for m in self._rx_rev.finditer(text)]
        for m in self._rx_sp.finditer(text):
            val = m.group(1).rstrip(" ,")
            spans.append(Span(m.start(1), m.start(1) + len(val), "PIN", val, 0.9, "spoken"))
        if cue.pin:
            for m in self._bare.finditer(text):
                if len(m.group(1)) == 4 and self._suffix_ref.search(text[max(0, m.start(1) - 40): m.start(1)]):
                    continue
                spans.append(Span(m.start(1), m.end(1), "PIN", m.group(1), 0.8, "cue"))
            for s, e, d in spoken_runs(text, 4):
                if 4 <= len(d) <= 8:
                    spans.append(Span(s, e, "PIN", text[s:e], 0.8, "cue-spoken"))
        return spans


class DobDetector:
    name = "dob"
    _months = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    _date = re.compile(
        rf"\b(?:\d{{1,2}}[/-]\d{{1,2}}[/-](?:19|20)\d{{2}}|(?:19|20)\d{{2}}-\d{{1,2}}-\d{{1,2}}|{_months}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+(?:19|20)\d{{2}}|\d{{1,2}}(?:st|nd|rd|th)?\s+{_months}\.?,?\s+(?:19|20)\d{{2}})\b",
        re.I)
    _ctx = re.compile(r"\b(?:born|dob|date of birth|birthday|birth ?date)\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = []
        has_ctx = bool(self._ctx.search(text))
        for m in self._date.finditer(text):
            yr = int(re.search(r"(?:19|20)\d{2}", m.group(0)).group(0))
            # conservative: DOB if cue/context, or year makes it a plausible birth year (<= 2010)
            if cue.dob or has_ctx or yr <= 2010:
                spans.append(Span(m.start(), m.end(), "DOB", m.group(0), 0.9))
        return spans


class AddressDetector:
    name = "address"
    _suffix = r"(?i:street|st|avenue|ave|road|rd|lane|ln|drive|dr|boulevard|blvd|way|court|ct|place|pl|terrace|highway|hwy)"
    _rx = re.compile(
        rf"\b\d{{1,5}}\s+(?:[A-Z][\w'-]*\s+){{1,3}}{_suffix}\b\.?(?:,?\s+(?:Apt|Unit|Suite|#)\s*\w+)?(?:,\s*[A-Z][a-zA-Z .]+)?(?:,?\s*[A-Z]{{2}}\b)?(?:\s+\d{{5}}(?:-\d{{4}})?)?")

    def detect(self, text: str, cue: Cue) -> list[Span]:
        return [Span(m.start(), m.end(), "ADDRESS", m.group(0).rstrip(", ."), 0.9) for m in self._rx.finditer(text)]


class ImeiDetector:
    name = "imei"
    _rx = re.compile(r"(?<![\d])\d{15}(?![\d])")
    _ctx = re.compile(r"\b(?:imei|imsi|iccid|sim (?:serial|number))\b[^\d]{0,12}(\d{15,20})\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = [Span(m.start(1), m.end(1), "IMEI", m.group(1), 0.95, "context") for m in self._ctx.finditer(text)]
        for m in self._rx.finditer(text):
            if luhn_ok(m.group(0)) and m.group(0).startswith(("35", "86", "01", "99", "45")):
                spans.append(Span(m.start(), m.end(), "IMEI", m.group(0), 0.8))
        return spans


class AccountDetector:
    name = "account"
    _rx = re.compile(r"\b(?:account|acct)\s*(?:number|no\.?|#|num)?[^\d\w]{0,10}(?:is|:)?\s*(\d{6,12})\b", re.I)

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans = [Span(m.start(1), m.end(1), "ACCOUNT", m.group(1)) for m in self._rx.finditer(text)]
        if cue.account:
            for m in re.finditer(r"(?<![\d$.])\b(\d{6,12})\b(?!\d)", text):
                spans.append(Span(m.start(1), m.end(1), "ACCOUNT", m.group(1), 0.7, "cue"))
        return spans


# --- names ---------------------------------------------------------------------------------
COMMON_FIRST = set("""john james robert michael william david richard joseph thomas charles mary patricia jennifer linda elizabeth
barbara susan jessica sarah karen nancy lisa maria carlos priya wei aisha emma liam olivia noah sofia sophia arjun fatima daniel
chloe mohamed muhammad hannah ethan isabella ravi grace lucas mei omar anna david jose juan luis miguel ahmed ali amit anil ravi raj
sunita sanjay kavita deepak neha pooja rohit rahul vikram yuki hiroshi kenji li zhang wang chen lee kim park nguyen tran pham ivan
olga natasha dmitri sergei fatma leila hassan ibrahim yusuf noor sara alex alexander andrew anthony christopher jason kevin brian
matthew mark paul steven kenneth george edward ryan jacob nicholas tyler samuel benjamin henry jack amy angela ashley emily
megan nicole samantha stephanie rachel rebecca laura helen amanda kelly julie""".split())
NAME_STOP = set("""the a an not your you this that it hello hi thanks thank please sorry calling having trying looking just very
still also really now here there going getting afraid happy glad ready unable able sure fine okay ok good great bad angry upset
frustrated tired done back new old customer account holder security team""".split())
_CAP = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?"


class NameDetector:
    name = "name"
    _intro = re.compile(rf"\b(?i:my name is|my name's|this is|i am|i'm|name is|speaking with|it's|its|call me|name:)\s+((?:{_CAP})(?:\s+{_CAP}){{0,2}})")
    _honor = re.compile(rf"\b(?:Mr|Mrs|Ms|Miss|Dr|Mx)\.?\s+({_CAP})")
    _full = re.compile(rf"\b({_CAP})\s+({_CAP})\b")

    def __init__(self, known_names: Iterable[str] = ()):
        self.known = [n for n in known_names if n]

    def detect(self, text: str, cue: Cue) -> list[Span]:
        spans: list[Span] = []
        for m in self._intro.finditer(text):
            val = m.group(1)
            toks = val.split()
            # strip trailing stop-words ("John Smith and ...")
            while toks and toks[-1].lower() in NAME_STOP:
                toks.pop()
            if toks and toks[0].lower() in NAME_STOP:
                continue
            first_ok = toks and (toks[0].lower() in COMMON_FIRST or len(toks) >= 2)
            if first_ok:
                val = " ".join(toks)
                start = m.start(1)
                spans.append(Span(start, start + len(val), "NAME", val, 0.85, "intro"))
        for m in self._honor.finditer(text):
            spans.append(Span(m.start(1), m.end(1), "NAME", m.group(1), 0.8, "honorific"))
        for m in self._full.finditer(text):
            f, l = m.group(1), m.group(2)
            if f.lower() in COMMON_FIRST and f.lower() not in NAME_STOP and l.lower() not in NAME_STOP:
                spans.append(Span(m.start(), m.end(), "NAME", m.group(0), 0.7, "gazetteer"))
        low = text.lower()
        for n in self.known:
            i = low.find(n.lower())
            while i != -1:
                spans.append(Span(i, i + len(n), "NAME", text[i:i + len(n)], 0.95, "known"))
                i = low.find(n.lower(), i + 1)
        if cue.name:
            m = re.match(rf"^\W*({_CAP})(?:\s+({_CAP}))?\W*$", text)
            if m:
                spans.append(Span(m.start(1), m.end(2) if m.group(2) else m.end(1), "NAME", m.group(0).strip(" .,!"), 0.7, "cue"))
        return spans


def default_detectors(known_names: Iterable[str] = ()) -> list[Detector]:
    return [CardDetector(), CvvDetector(), ExpiryDetector(), SsnDetector(), EmailDetector(), IpDetector(),
            AddressDetector(), PhoneDetector(), PinDetector(), DobDetector(), ImeiDetector(), AccountDetector(),
            NameDetector(known_names)]


def resolve_overlaps(spans: list[Span]) -> list[Span]:
    """Keep the highest priority (then longest, then highest score) span among overlaps."""
    ordered = sorted(spans, key=lambda s: (-PRIORITY.get(s.type, 0), -(s.end - s.start), -s.score, s.start))
    kept: list[Span] = []
    for s in ordered:
        if not any(s.overlaps(k) for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s.start)


def detect_all(text: str, detectors: list[Detector], cue: Cue | None = None) -> list[Span]:
    cue = cue or Cue()
    spans: list[Span] = []
    for d in detectors:
        spans.extend(d.detect(text, cue))
    return resolve_overlaps(spans)
