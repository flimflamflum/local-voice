"""Helpers for turning streamed model output into speakable sentences."""
import re

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
        self.first = True

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

    # Long run-on sentence? Break at a clause (", " "; " ": " " - ") so speech can start sooner.
    _CLAUSE = re.compile(r"[,;:]\s+|\s+[—–-]\s+")
    CLAUSE_SPLIT_AT = 110  # chars without a sentence end before we split at a clause
    MIN_CLAUSE = 35

    def _clause_split(self):
        # The very first chunk splits eagerly: getting the voice started fast matters most.
        split_at, min_clause = (45, 20) if self.first else (self.CLAUSE_SPLIT_AT, self.MIN_CLAUSE)
        if len(self.buf) < split_at or self.buf.count("```") % 2 == 1:
            return None
        found = None
        for m in self._CLAUSE.finditer(self.buf):
            if m.start() >= min_clause:
                found = m
                if self.first:
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
                    self.first = False
                    continue
                break
            end = m.end()
            candidate = self.buf[:end]
            if len(candidate.strip()) < self.min_len and "\n" not in m.group(0):
                # Too short ("Ok." / "Mr.") - merge with the next sentence.
                nxt = self._next_boundary(end)
                if not nxt:
                    break
                end = nxt.end()
                candidate = self.buf[:end]
            self.buf = self.buf[end:]
            if candidate.strip():
                out.append(candidate)  # raw (keeps newlines) - strip/clean before speaking
                self.first = False
        return out

    def flush(self):
        rest, self.buf = self.buf, ""
        return [rest] if rest.strip() else []


class ThinkFilter:
    """Removes <think>...</think> sections from a token stream (reasoning models)."""

    def __init__(self):
        self.in_think = False
        self.pending = ""

    def feed(self, token: str) -> str:
        self.pending += token
        out = ""
        while self.pending:
            if self.in_think:
                i = self.pending.find("</think>")
                if i == -1:
                    self.pending = self.pending[-8:]
                    return out
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
