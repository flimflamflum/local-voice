"""Helpers for turning streamed model output into speakable sentences."""
import re
import time

_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_URL = re.compile(r"https?://\S+")
_MD_MARKS = re.compile(r"[*_#>|~`]+")
_LIST_BULLET = re.compile(r"^\s*(?:[-•]|\d+[.)])\s+", re.M)
# Emoji / pictographs and other symbols the voice would read as gibberish.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️‍]+"
)


def clean_for_speech(text: str) -> str:
    """Strip markdown/code/emoji so the TTS reads natural text."""
    text = _CODE_BLOCK.sub(" I've put the code on screen. ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("the link on screen", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LIST_BULLET.sub("", text)
    text = _MD_MARKS.sub(" ", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class SentenceChunker:
    """Feed streamed tokens in, get complete sentences out.

    Code blocks are held back until they close so they get replaced by a single
    "code on screen" remark instead of being read character by character.
    """

    _BOUNDARY = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")

    def __init__(self, min_len: int = 12):
        self.buf = ""
        self.min_len = min_len
        self.n = 0  # chunks emitted so far

    def _next_boundary(self, start=0):
        """First sentence boundary that isn't inside a ``` code block."""
        pos = start
        while True:
            m = self._BOUNDARY.search(self.buf, pos)
            if not m:
                return None
            if self.buf[: m.start()].count("```") % 2 == 0:
                return m
            pos = m.end()

    # Break at a clause (", " "; " ": " " - ") instead of waiting for the sentence to end.
    # TTS time grows with chunk length (~150 ms + 15 ms/char on CPU Kokoro) while audio
    # lasts ~60 ms/char, so: a tiny first chunk gets the voice going fast, a short second
    # chunk is ready before the first finishes playing, and after that chunks can be long.
    _CLAUSE = re.compile(r"[,;:]\s+|\s+[—–-]\s+")
    #            (split once buffer reaches, min clause length, take earliest clause?)
    _CLAUSE_RULES = [(24, 8, True), (70, 20, True), (110, 35, False)]

    def _clause_split(self):
        split_at, min_clause, earliest = self._CLAUSE_RULES[min(self.n, 2)]
        if len(self.buf) < split_at or self.buf.count("```") % 2 == 1:
            return None
        found = None
        for m in self._CLAUSE.finditer(self.buf):
            if m.start() >= min_clause:
                found = m
                if earliest:
                    break
        return found.end() if found else None

    def feed(self, token: str):
        self.buf += token
        out = []
        while True:
            m = self._next_boundary()
            if not m:
                cut = self._clause_split()
                if cut:
                    out.append(self.buf[:cut])
                    self.buf = self.buf[cut:]
                    self.n += 1
                    continue
                break
            end = m.end()
            candidate = self.buf[:end]
            min_len = 6 if self.n == 0 else self.min_len  # "Sure!" can go out on its own first
            if len(candidate.strip()) < min_len and "\n" not in m.group(0):
                # Too short ("Ok." / "Mr.") - merge with the next sentence.
                nxt = self._next_boundary(end)
                if not nxt:
                    break
                end = nxt.end()
                candidate = self.buf[:end]
            self.buf = self.buf[end:]
            if candidate.strip():
                out.append(candidate)  # raw (keeps newlines) - strip/clean before speaking
                self.n += 1
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return [rest] if rest.strip() else []


_DETAIL_ASKED = re.compile(
    r"\b(detail|detailed|details|elaborate|in depth|in-depth|step by step|steps|story|long|longer|"
    r"tell me more|explain|everything|list|recipe|instructions|write me|write a)\b",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*\s*$")


class ReplyLimiter:
    """Backstop for chatty models: ends a reply at a sentence boundary once it has said enough.

    The prompt already asks for short answers; this catches models that ignore it (and the
    Normal personality, which has no prompt). Counted in spoken characters (~16 per second of
    speech), so code shown on screen doesn't count.
    """

    SHORT = 320  # ~20 s of speech
    DETAILED = 1100  # ~70 s, when the user asked for detail / a story / steps

    def __init__(self, user_text: str):
        self.limit = self.DETAILED if _DETAIL_ASKED.search(user_text or "") else self.SHORT
        self.spoken = 0

    def done(self, raw: str, clean: str) -> bool:
        """Feed each chunk as it's spoken; True = stop the reply after this one."""
        self.spoken += len(clean)
        return self.spoken >= self.limit and (raw.endswith("\n") or bool(_SENTENCE_END.search(raw)))


class ThinkFilter:
    """Removes <think>...</think> sections from a token stream (reasoning models).

    Some models write their reasoning inline in tags instead of Ollama's separate field;
    the removed text goes to on_think(text) so it can still be shown.
    """

    def __init__(self, on_think=None):
        self.in_think = False
        self.pending = ""
        self.on_think = on_think or (lambda _t: None)

    def feed(self, token: str) -> str:
        self.pending += token
        out = ""
        while self.pending:
            if self.in_think:
                i = self.pending.find("</think>")
                if i == -1:
                    # pass on everything but a possible partial "</think" tag at the end
                    keep = next((k for k in range(min(8, len(self.pending)), 0, -1)
                                 if "</think>".startswith(self.pending[-k:])), 0)
                    self.on_think(self.pending[:len(self.pending) - keep])
                    self.pending = self.pending[len(self.pending) - keep:]
                    return out
                self.on_think(self.pending[:i])
                self.pending = self.pending[i + 8:]
                self.in_think = False
            else:
                i = self.pending.find("<think>")
                if i == -1:
                    # keep a possible partial "<think" tag at the end
                    lt = self.pending.rfind("<")
                    if lt != -1 and "<think>".startswith(self.pending[lt:]):
                        out += self.pending[:lt]
                        self.pending = self.pending[lt:]
                        return out
                    out += self.pending
                    self.pending = ""
                    return out
                out += self.pending[:i]
                self.pending = self.pending[i + 7:]
                self.in_think = True
        return out


class ThoughtStream:
    """Collects a reply's reasoning and hands it to `emit` in batches.

    Thinking streams fast (~40 tokens/s); batching keeps the UI from being flooded
    with one update per token.
    """

    def __init__(self, emit, interval=0.15):
        self.emit = emit
        self.interval = interval
        self.buf = ""
        self.started = None  # time.monotonic() of the first thought
        self.finished = False
        self._last = 0.0

    def add(self, text: str):
        if not text or self.finished:
            return
        now = time.monotonic()
        if self.started is None:
            self.started = now
            text = text.lstrip()
            if not text:
                return
        self.buf += text
        if now - self._last >= self.interval:
            self._flush(now)

    def _flush(self, now):
        if self.buf:
            self.emit(self.buf, None)
            self.buf = ""
        self._last = now

    def finish(self):
        """Call when the reply starts (or the turn ends). Sends the rest + how long it took."""
        if self.finished or self.started is None:
            self.finished = True
            return
        self.finished = True
        now = time.monotonic()
        rest, self.buf = self.buf.rstrip(), ""
        self.emit(rest, round(now - self.started, 1))


_SENT_END = re.compile(r"(?<=[.!?])\s+|\n+")


class RepeatWatch:
    """Spots a model going round in circles: the same sentence turning up again and again.

    Small models sometimes get stuck repeating the same couple of sentences while they
    think. feed() returns True once any real sentence (not "Okay." / "Wait.") has appeared
    `limit` times. deduped() is the text with every repeat after the first removed.
    """

    MIN_CHARS = 30  # shorter sentences repeat innocently ("So the answer is 391.")

    def __init__(self, limit=4):
        self.limit = limit
        self.buf = ""
        self.counts = {}
        self.kept = []  # first copy of each sentence, with its separator

    def feed(self, text: str) -> bool:
        self.buf += text
        looping = False
        while True:
            m = _SENT_END.search(self.buf)
            if not m:
                return looping
            sentence, sep = self.buf[:m.start()], m.group()
            self.buf = self.buf[m.end():]
            looping = self._add(sentence, sep) or looping

    def _add(self, sentence, sep):
        key = re.sub(r"\W+", " ", sentence.lower()).strip()
        if len(key) < self.MIN_CHARS:
            self.kept.append(sentence + sep)
            return False
        n = self.counts[key] = self.counts.get(key, 0) + 1
        if n == 1:
            self.kept.append(sentence + sep)
        return n >= self.limit

    def deduped(self) -> str:
        return "".join(self.kept) + self.buf
