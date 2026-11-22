"""Vesper Code: a small coding agent that works inside one folder, driven by a local model.

The model gets tools to look around (list, read, search), change files (write, edit) and run
PowerShell commands. Every change and command is shown to the user and, unless they said
"allow all", has to be approved first. File tools can never leave the chosen folder; commands
that point outside it or could do lasting damage are refused outright (command_risk). Every
file change is checkpointed per request so the user can undo it.

Small local models have small context windows, so old tool output is trimmed as the
conversation grows (see CodeAgent._fit).
"""
import difflib
import re
import subprocess
from datetime import datetime
from pathlib import Path

from src import search
from src.llm import NUM_CTX

MAX_STEPS = 30              # model turns per request before we stop it
READ_MAX_LINES = 220        # per read_file call
READ_MAX_CHARS = 8000
OUTPUT_MAX_CHARS = 3500     # command output kept for the model (the end matters most)
LIST_MAX = 200
SEARCH_MAX = 60
SEARCH_FILE_MAX_BYTES = 1_000_000
CMD_TIMEOUT = 120
DIFF_MAX_LINES = 300
NUM_PREDICT = 4096          # room for a whole file inside a write_file call
THINK_NUM_PREDICT = 6144
# Roughly what the history may use: the context minus the system prompt, tool definitions and
# room for the reply. Code is about 3 characters per token.
HISTORY_BUDGET_CHARS = max(6000, (NUM_CTX - NUM_PREDICT // 2 - 1800) * 3)

# Qwen's recommended sampling, minus the presence penalty: code repeats names on purpose.
SAMPLING = {"temperature": 0.6, "top_p": 0.95, "top_k": 20}

SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode",
    "dist", "build", ".gradle", ".next", ".nuxt", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".cache", "coverage", ".turbo", "bin", "obj",
}
ELIDED = "[Old output removed to save space. Run the tool again if you need it.]"


def _tool(name, description, props, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props, "required": required},
    }}


_S = {"type": "string"}
_I = {"type": "integer"}
TOOLS = [
    _tool("list_files", "List the files and folders in a folder of the project (two levels deep).",
          {"path": {**_S, "description": "Folder path relative to the project, e.g. \".\" or \"src\""}}, []),
    _tool("read_file", "Read a text file, with line numbers. Long files come in chunks: pass start_line to read further.",
          {"path": {**_S, "description": "File path relative to the project"},
           "start_line": {**_I, "description": "First line to read (default 1)"},
           "end_line": {**_I, "description": "Last line to read (optional)"}}, ["path"]),
    _tool("search_files", "Search the project's files for text or a regular expression. Returns matching lines.",
          {"pattern": {**_S, "description": "Text or regular expression to look for"},
           "path": {**_S, "description": "Folder to search in (default: the whole project)"}}, ["pattern"]),
    _tool("write_file", "Create a new file, or replace a file's entire contents. Use edit_file for small changes to existing files.",
          {"path": {**_S, "description": "File path relative to the project"},
           "content": {**_S, "description": "The complete new contents of the file"}}, ["path", "content"]),
    _tool("edit_file", "Change part of an existing file: replaces old_text with new_text. old_text must be copied exactly "
          "from the file and appear only once, so include a few whole lines.",
          {"path": {**_S, "description": "File path relative to the project"},
           "old_text": {**_S, "description": "The exact existing text to replace"},
           "new_text": {**_S, "description": "The text to put in its place"}}, ["path", "old_text", "new_text"]),
    _tool("run_command", "Run a PowerShell command in the project folder (Windows) and get its output, e.g. to run "
          "a script, tests or an install. Don't start servers or anything that keeps running.",
          {"command": {**_S, "description": "The PowerShell command"}}, ["command"]),
]

PROMPT = """You are {name} Code, a coding assistant working in a project folder on the user's Windows PC. You read and change files in this folder and run commands using your tools. The user can see every tool call you make.

# The most important rule
You know nothing about this project except what your tools show you. Before you answer any question about the project or change anything, look first: search_files for the relevant names or words, then read_file the files that matter. Never answer from memory or guess what a file contains or how the project works.

# How to work
- To change an existing file, use edit_file: copy old_text exactly from the file (a few whole lines, enough to be unique) and give the new_text to put in its place. One focused change per call.
- To create a new file, or rewrite a small file completely, use write_file with the full contents.
- Always make changes with the tools. Never paste code in your reply and ask the user to copy it in.
- Use run_command for running scripts, tests or installs. It's PowerShell, in the project folder. Don't start servers or programs that keep running.
- Paths are relative to the project folder.
- After writing or changing code, check it: run it if it finishes on its own, or at least check it compiles (for Python: python -m py_compile file.py). Programs that wait for keyboard input will hang, so only compile-check those. Fix anything that fails.
- If a tool fails, read the error, fix the cause and try again.
- Keep going until the task is done, then reply with a short summary of what you did. Don't repeat file contents in the summary.
- If a request is unclear, ask one short question instead of guessing.
- Write complete, working code. No placeholders like "rest of the code here".
- For questions about the code, look at the code, then answer in a few sentences, naming the files involved. You don't have to change anything.

# Context
- Today: {today}
- Project folder: {root}
- Top level of the folder:
{listing}"""

WEB_LINE = "\n- You can also search the web with web_search, e.g. for library docs or error messages."


class ToolError(Exception):
    pass


class CodeAgent:
    """One coding conversation in one folder. `run` handles one user request."""

    def __init__(self, ollama, root, assistant_name="Vesper"):
        self.ollama = ollama
        self.root = Path(root).resolve()
        self.name = assistant_name or "Vesper"
        self.history = []
        self.allow_edits = False      # "Allow all edits" for this conversation
        self.allow_commands = False   # "Always allow commands" for this conversation
        # Undo: for each request, every file's contents from before that request first changed it
        # (None = the file didn't exist). Only edits made through the file tools are recorded.
        self.checkpoints = []         # [{"turn": id, "files": {Path: bytes | None}}]
        self._checkpoint = None
        self._undo_note = ""          # told to the model with the next request

    # ------------------------------------------------------------------ the loop
    def run(self, model, text, cancel, ui, think=False, web=False, turn=None):
        """ui: token(t), thought(t), tool_start(tid, name, label), tool_end(tid, ok, summary, output, diff),
        ask(kind, label, detail) -> "allow" | "always" | "deny", note(text).
        `turn` names this request's checkpoint, for undo()."""
        self._checkpoint = {"turn": turn, "files": {}}
        self.checkpoints.append(self._checkpoint)
        try:
            self._run(model, text, cancel, ui, think, web)
        finally:
            if not self._checkpoint["files"] and self._checkpoint in self.checkpoints:
                self.checkpoints.remove(self._checkpoint)
            self._checkpoint = None

    def changed_files(self, turn):
        """Paths (relative) that request `turn` changed, or [] if none."""
        for cp in self.checkpoints:
            if cp["turn"] == turn:
                return sorted(self._rel(p) for p in cp["files"])
        return []

    def undo(self, turn):
        """Put files back to how they were before request `turn`. Later requests are undone too,
        since they built on it. Returns (undone turn ids, restored relative paths)."""
        idx = next((i for i, cp in enumerate(self.checkpoints) if cp["turn"] == turn), None)
        if idx is None:
            raise ToolError("Those changes were already undone, or there's nothing to undo.")
        undone = self.checkpoints[idx:]
        restored = set()
        for cp in reversed(undone):  # newest first, so the oldest saved copy is what's left
            for path, data in cp["files"].items():
                if data is None:
                    if path.is_file():
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                restored.add(self._rel(path))
        del self.checkpoints[idx:]
        files = sorted(restored)
        self._undo_note = (
            "[Note: the user undid your earlier changes to " + ", ".join(files) + ". Those files are back to how they "
            "were before. Read them again before changing them.]\n\n"
        )
        return [cp["turn"] for cp in undone], files

    def _snapshot(self, full):
        """Remember a file's contents before its first change in this request."""
        cp = self._checkpoint
        if cp is not None and full not in cp["files"]:
            cp["files"][full] = full.read_bytes() if full.is_file() else None

    def _run(self, model, text, cancel, ui, think, web):
        if self._undo_note:
            text, self._undo_note = self._undo_note + text, ""
        self.history.append({"role": "user", "content": text})
        tools = TOOLS + ([search.TOOL] if web else [])
        step_id = 0
        for _ in range(MAX_STEPS):
            messages = [{"role": "system", "content": self._prompt(web)}] + self._fit()
            meta, said = {}, ""
            for token in self.ollama.chat_step(
                model, messages, cancel, meta, tools=tools, think=think, on_thinking=ui.thought,
                num_predict=THINK_NUM_PREDICT if think else NUM_PREDICT, sampling=SAMPLING,
            ):
                said += token
                ui.token(token)
            calls = meta.get("tool_calls") or []
            msg = {"role": "assistant", "content": said}
            if calls:
                msg["tool_calls"] = calls
            self.history.append(msg)
            if cancel.is_set() or not calls:
                return
            for call in calls:
                fn = call.get("function") or {}
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                step_id += 1
                result = self._call(name, args, cancel, ui, step_id)
                self.history.append({"role": "tool", "tool_name": name, "content": result})
                if cancel.is_set():
                    return
        ui.note(f"Stopped after {MAX_STEPS} steps. Say \"keep going\" to let it continue.")

    def clear(self):
        self.history.clear()
        self.checkpoints.clear()
        self._undo_note = ""
        self.allow_edits = self.allow_commands = False

    # ------------------------------------------------------------------ tools
    def _call(self, name, args, cancel, ui, step_id):
        if isinstance(args, str):
            args = {}
        tid = f"t{id(self)}_{len(self.history)}_{step_id}"
        ui.tool_start(tid, name, self._label(name, args))
        try:
            handler = getattr(self, f"_t_{name}", None)
            if handler is None:
                raise ToolError(f"There is no tool called {name}.")
            out = handler(args, cancel, ui)
            summary, output, diff = out if isinstance(out, tuple) else ("", out, "")
            ui.tool_end(tid, True, summary, output, diff)
            return output
        except ToolError as e:
            ui.tool_end(tid, False, str(e), "", "")
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - never let one tool kill the turn
            ui.tool_end(tid, False, f"{e.__class__.__name__}: {e}", "", "")
            return f"Error: {e.__class__.__name__}: {e}"

    def _label(self, name, args):
        path = args.get("path") or "."
        return {
            "list_files": f"List {path}",
            "read_file": f"Read {path}",
            "search_files": f"Search for {args.get('pattern', '')!r}" + (f" in {path}" if path != "." else ""),
            "write_file": f"Write {path}",
            "edit_file": f"Edit {path}",
            "run_command": f"Run {args.get('command', '')}",
            "web_search": f"Search the web for {args.get('query', '')!r}",
        }.get(name, name)

    def _path(self, p, must_exist=True):
        p = str(p or ".").strip().strip("\"'") or "."
        full = (self.root / p).resolve()
        if full != self.root and not full.is_relative_to(self.root):
            raise ToolError(f"{p} is outside the project folder. Use paths inside it.")
        if must_exist and not full.exists():
            raise ToolError(f"{p} doesn't exist. Use list_files or search_files to find the right path.")
        return full

    def _rel(self, full):
        rel = full.relative_to(self.root).as_posix()
        return rel or "."

    def _t_list_files(self, args, cancel, ui):
        base = self._path(args.get("path"))
        if not base.is_dir():
            raise ToolError(f"{self._rel(base)} is a file, not a folder.")
        lines = _listing(base, depth=2, limit=LIST_MAX)
        return f"{len(lines)} entries", f"{self._rel(base)}/\n" + ("\n".join(lines) or "(empty folder)"), ""

    def _t_read_file(self, args, cancel, ui):
        full = self._path(args.get("path"))
        if full.is_dir():
            raise ToolError(f"{self._rel(full)} is a folder. Use list_files for folders.")
        text, _nl, _bom = _read_text(full)
        lines = text.split("\n")
        start = max(1, _int(args.get("start_line"), 1))
        end = _int(args.get("end_line"), 0) or start + READ_MAX_LINES - 1
        end = min(end, len(lines), start + READ_MAX_LINES - 1)
        if start > len(lines):
            raise ToolError(f"{self._rel(full)} only has {len(lines)} lines.")
        out, size = [], 0
        for n in range(start, end + 1):
            row = f"{n:>5}| {lines[n - 1]}"
            if size + len(row) > READ_MAX_CHARS and out:
                end = n - 1
                break
            out.append(row)
            size += len(row) + 1
        head = f"{self._rel(full)} (lines {start}-{end} of {len(lines)})"
        more = f"\n[{len(lines) - end} more lines: read_file with start_line={end + 1} to continue]" if end < len(lines) else ""
        return f"lines {start}-{end} of {len(lines)}", head + "\n" + "\n".join(out) + more, ""

    def _t_search_files(self, args, cancel, ui):
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("Give a pattern to search for.")
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            rx = re.compile(re.escape(pattern), re.IGNORECASE)
        base = self._path(args.get("path"))
        hits, files = [], [base] if base.is_file() else _walk(base)
        for f in files:
            if cancel.is_set() or len(hits) >= SEARCH_MAX:
                break
            try:
                if f.stat().st_size > SEARCH_FILE_MAX_BYTES:
                    continue
                text, _nl, _bom = _read_text(f)
            except (ToolError, OSError):
                continue
            for n, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    hits.append(f"{self._rel(f)}:{n}: {line.strip()[:200]}")
                    if len(hits) >= SEARCH_MAX:
                        break
        if not hits:
            return "no matches", f"No matches for {pattern!r}.", ""
        more = f"\n[Stopped at {SEARCH_MAX} matches; search more specifically.]" if len(hits) >= SEARCH_MAX else ""
        return f"{len(hits)} matches", "\n".join(hits) + more, ""

    def _t_write_file(self, args, cancel, ui):
        full = self._path(args.get("path"), must_exist=False)
        if full.is_dir():
            raise ToolError(f"{self._rel(full)} is a folder.")
        content = _unescape(str(args.get("content") or ""))
        old, nl, bom = _read_text(full) if full.exists() else ("", "\n", False)
        if full.exists() and old == content:
            return "no change", f"{self._rel(full)} already has exactly this content.", ""
        diff = _diff(self._rel(full), old, content)
        self._approve("edit", ("Overwrite " if full.exists() else "Create ") + self._rel(full), diff, cancel, ui)
        self._snapshot(full)
        full.parent.mkdir(parents=True, exist_ok=True)
        _write_text(full, content, nl, bom)
        n = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = "Updated" if old else "Created"
        return _diff_stats(diff), f"{verb} {self._rel(full)} ({n} lines).", diff

    def _t_edit_file(self, args, cancel, ui):
        full = self._path(args.get("path"))
        if full.is_dir():
            raise ToolError(f"{self._rel(full)} is a folder.")
        old_text = _unescape(str(args.get("old_text") or "")).replace("\r\n", "\n")
        new_text = _unescape(str(args.get("new_text") or "")).replace("\r\n", "\n")
        if not old_text:
            raise ToolError("old_text is empty. To create or replace a whole file, use write_file.")
        text, nl, bom = _read_text(full)
        count = text.count(old_text)
        if count == 0:
            new = _loose_replace(text, old_text, new_text)
            if new is None:
                raise ToolError(
                    f"old_text wasn't found in {self._rel(full)}. read_file it again and copy the exact lines, "
                    "including indentation."
                )
        elif count > 1:
            raise ToolError(f"old_text appears {count} times in {self._rel(full)}. Include more surrounding lines so it's unique.")
        else:
            new = text.replace(old_text, new_text, 1)
        if new == text:
            return "no change", "old_text and new_text are the same; nothing changed.", ""
        diff = _diff(self._rel(full), text, new)
        self._approve("edit", f"Edit {self._rel(full)}", diff, cancel, ui)
        self._snapshot(full)
        _write_text(full, new, nl, bom)
        return _diff_stats(diff), f"Edited {self._rel(full)}.", diff

    def _t_run_command(self, args, cancel, ui):
        command = str(args.get("command") or "").strip()
        if not command:
            raise ToolError("Give a command to run.")
        risk = command_risk(command, self.root)
        if risk:  # never even offered to the user: these can't be undone or leave the folder
            raise ToolError(
                f"Blocked: {risk}. Vesper Code only runs commands that stay inside the project folder and can't do "
                "lasting damage. Find another way, or tell the user the command so they can run it themselves."
            )
        self._approve("command", command, "", cancel, ui)
        code, output = _run_powershell(command, self.root, cancel)
        if len(output) > OUTPUT_MAX_CHARS:
            output = f"[...{len(output) - OUTPUT_MAX_CHARS} characters cut...]\n" + output[-OUTPUT_MAX_CHARS:]
        status = "timed out" if code is None else f"exit code {code}"
        return status, f"$ {command}\n{output.rstrip() or '(no output)'}\n[{status}]", ""

    def _t_web_search(self, args, cancel, ui):
        query = str(args.get("query") or "")
        return "", search.search(query), ""

    def _approve(self, kind, label, detail, cancel, ui):
        if (kind == "edit" and self.allow_edits) or (kind == "command" and self.allow_commands):
            return
        answer = ui.ask(kind, label, detail)
        if cancel.is_set():
            raise ToolError("Stopped by the user.")
        if answer == "always":
            if kind == "edit":
                self.allow_edits = True
            else:
                self.allow_commands = True
        elif answer != "allow":
            raise ToolError("The user said no to this. Don't retry it; ask what they'd like instead.")

    # ------------------------------------------------------------------ context
    def _prompt(self, web):
        listing = "\n".join("  " + line for line in _listing(self.root, depth=1, limit=40)) or "  (empty folder)"
        return PROMPT.format(
            name=self.name, root=self.root, listing=listing,
            today=datetime.now().strftime("%A, %B %d, %Y").replace(" 0", " "),
        ) + (WEB_LINE if web else "")

    def _fit(self):
        """The history, trimmed to fit the context: old tool output goes first, then whole old turns."""
        msgs = [dict(m) for m in self.history]
        size = lambda: sum(_msg_chars(m) for m in msgs)  # noqa: E731
        # Never trim the newest exchange: the model needs what it just read.
        keep_from = max(0, len(msgs) - 3)
        for i, m in enumerate(msgs[:keep_from]):
            if size() <= HISTORY_BUDGET_CHARS:
                break
            if m["role"] == "tool" and m["content"] != ELIDED and len(m["content"]) > 200:
                m["content"] = ELIDED
            elif m.get("tool_calls"):
                m["tool_calls"] = [_shrink_call(c) for c in m["tool_calls"]]
        # Still too big: drop the oldest turns (a turn starts at a user message).
        while size() > HISTORY_BUDGET_CHARS:
            starts = [i for i, m in enumerate(msgs) if m["role"] == "user"]
            if len(starts) < 2:
                break
            msgs = msgs[starts[1]:]
        # Last resort, one huge exchange: cut the oldest tool outputs in what's left.
        for m in msgs:
            if size() <= HISTORY_BUDGET_CHARS:
                break
            if m["role"] == "tool" and len(m["content"]) > 1500 and m is not msgs[-1]:
                m["content"] = m["content"][:1500] + "\n[...cut to save space...]"
        return msgs


# ------------------------------------------------------------------ command guard
# Commands run as the user, so a command could reach anywhere. These rules refuse the ones that
# point outside the project folder or can do damage that Undo can't reverse. It's a safety net,
# not a sandbox: the user still approves every command that gets through.
_SEGMENT = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
_DELETE_VERBS = {"remove-item", "rm", "ri", "del", "erase", "rmdir", "rd", "rimraf", "rm.exe"}
_DANGER = [
    (re.compile(r"\bformat(\.com)?\s+[a-z]:", re.I), "it formats a drive"),
    (re.compile(r"\b(diskpart|bcdedit|vssadmin|cipher(\.exe)?\s+/w|takeown|icacls|"
                r"shutdown|restart-computer|stop-computer|clear-disk|format-volume|initialize-disk|"
                r"remove-partition|set-executionpolicy|disable-computerrestore|mkfs|dd\s+if=)\b", re.I),
     "it changes system or disk settings"),
    (re.compile(r"\breg(\.exe)?\s+(delete|add|import|load)\b|\bHK(LM|CU|CR|U|CC):|\bRegistry::|"
                r"\b(set|new|remove)-itemproperty\b", re.I),
     "it changes the Windows registry"),
    (re.compile(r"\b(iex|invoke-expression)\b.*\b(iwr|irm|curl|wget|invoke-webrequest|invoke-restmethod|"
                r"downloadstring|net\.webclient)\b|\b(iwr|irm|curl|wget|invoke-webrequest|invoke-restmethod|"
                r"downloadstring)\b.*\|\s*(iex|invoke-expression|sh|bash|python|cmd)\b", re.I),
     "it downloads code and runs it"),
    (re.compile(r"\s-(e|ec|enc|encodedcommand)\s+[A-Za-z0-9+/=]{16,}", re.I), "it hides what it does (encoded command)"),
    (re.compile(r"-verb\s+runas\b|\brunas(\.exe)?\s|\bsudo\b|\bgsudo\b", re.I), "it asks for administrator rights"),
    (re.compile(r"\bgit\s+(clean\b|reset\s+--hard|checkout\s+--?\s*\.|restore\s+(--source\S*\s+)?\.|"
                r"push\b|stash\s+(drop|clear))", re.I),
     "it throws away work in git or publishes it"),
    (re.compile(r"\b(net\s+user|net\s+localgroup|new-localuser|set-mppreference|netsh\s+advfirewall|"
                r"schtasks(\.exe)?\s+/create|register-scheduledtask|new-service|sc(\.exe)?\s+(create|delete|config))\b",
                re.I),
     "it changes accounts, security settings or background tasks"),
]
_ABS_PATH = re.compile(r"(?<![\w:/\\.])([A-Za-z]:[\\/][^\s'\"|;&<>(),]*|\\\\[^\s'\"|;&<>(),]+)")
_UP_PATH = re.compile(r"(?<![\w.])\.\.(?:[\\/][^\s'\"|;&<>(),]*)?(?=$|[\s'\"|;&<>(),])")
_HOME_PATH = re.compile(
    r"(?<![\w$])~(?=$|[\\/\s'\"])|\$(env:)?(userprofile|home|homepath|homedrive|appdata|localappdata|temp|tmp|"
    r"programfiles|programdata|systemroot|windir|public|onedrive|allusersprofile)\b|"
    r"%(userprofile|homepath|homedrive|appdata|localappdata|temp|tmp|programfiles|programdata|systemroot|windir|"
    r"public|onedrive|allusersprofile)%|\[environment\]::getfolderpath|getfolderpath\(",
    re.I,
)


def command_risk(command, root):
    """Why a command is too risky to run, or "" if it may be offered to the user."""
    root = Path(root).resolve()
    for rx, why in _DANGER:
        if rx.search(command):
            return why
    for seg in _SEGMENT.split(command):
        words = seg.strip().lstrip("&. ").split()
        if not words:
            continue
        verb = words[0].lower()
        rest = " ".join(words[1:])
        if verb in _DELETE_VERBS and (re.search(r"(^|\s)(-r|-rf|-fr|-recurse|/s|-force)\b", rest, re.I) or "*" in rest):
            return "it deletes files in bulk (recursive, forced or wildcard), which Undo can't bring back"
    if _HOME_PATH.search(command):
        return "it points outside the project folder (to your user, system or temp folders)"
    for m in _ABS_PATH.finditer(command):
        if not _inside(m.group(1), root):
            return f"it points outside the project folder ({m.group(1)})"
    for m in _UP_PATH.finditer(command):
        if not _inside(root / m.group(0), root):
            return f"it points outside the project folder ({m.group(0)})"
    return ""


def _inside(path, root):
    try:
        p = Path(str(path).strip("\"'").rstrip("\\/") or "\\").resolve()
    except (OSError, ValueError):
        return False
    return p == root or p.is_relative_to(root)


# ---------------------------------------------------------------------- helpers
def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _unescape(s):
    """Some small models double-escape newlines inside tool arguments ("line1\\nline2")."""
    if "\n" not in s and "\\n" in s:
        return s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"')
    return s


def _msg_chars(m):
    n = len(m.get("content") or "")
    for c in m.get("tool_calls") or []:
        n += len(str((c.get("function") or {}).get("arguments") or "")) + 30
    return n + 20


def _shrink_call(call):
    fn = dict(call.get("function") or {})
    args = fn.get("arguments")
    if isinstance(args, dict) and any(len(str(v)) > 300 for v in args.values()):
        fn["arguments"] = {k: (v if len(str(v)) <= 300 else "[long text, already applied]") for k, v in args.items()}
    return {**call, "function": fn}


def _read_text(path):
    """(text with \\n newlines, the file's newline style, whether it had a UTF-8 BOM)."""
    raw = path.read_bytes()
    if b"\0" in raw[:8000]:
        raise ToolError(f"{path.name} is a binary file, not text.")
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig" if bom else "utf-8", errors="replace")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), nl, bom


def _write_text(path, text, nl="\n", bom=False):
    data = text.replace("\r\n", "\n").replace("\n", nl).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)


def _loose_replace(text, old, new):
    """Find old_text ignoring trailing spaces and indentation differences, if that's unique."""
    norm = lambda s: [line.strip() for line in s.strip("\n").split("\n")]  # noqa: E731
    want = norm(old)
    lines = text.split("\n")
    hits = [i for i in range(len(lines) - len(want) + 1) if [x.strip() for x in lines[i:i + len(want)]] == want]
    if len(hits) != 1:
        return None
    i = hits[0]
    # Keep the file's indentation: shift new_text by the difference on the first line.
    have = lines[i][: len(lines[i]) - len(lines[i].lstrip())]
    first_old = old.strip("\n").split("\n")[0]
    gave = first_old[: len(first_old) - len(first_old.lstrip())]
    new_lines = new.strip("\n").split("\n") if new.strip("\n") else []
    if have != gave:
        new_lines = [have + ln[len(gave):] if ln.startswith(gave) else ln for ln in new_lines]
    return "\n".join(lines[:i] + new_lines + lines[i + len(want):])


def _diff(rel, old, new):
    lines = list(difflib.unified_diff(old.split("\n") if old else [], new.split("\n"),
                                      f"a/{rel}", f"b/{rel}", lineterm="", n=2))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... {len(lines) - DIFF_MAX_LINES} more lines"]
    return "\n".join(lines)


def _diff_stats(diff):
    add = sum(1 for ln in diff.split("\n") if ln.startswith("+") and not ln.startswith("+++"))
    rem = sum(1 for ln in diff.split("\n") if ln.startswith("-") and not ln.startswith("---"))
    return f"+{add} -{rem}"


def _walk(base):
    for p in sorted(base.iterdir(), key=lambda x: (x.is_file(), x.name.lower())):
        if p.is_dir():
            if p.name not in SKIP_DIRS and not p.is_symlink():
                yield from _walk(p)
        elif p.is_file():
            yield p


def _listing(base, depth, limit):
    out = []

    def visit(d, level, prefix):
        try:
            entries = sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except OSError:
            return
        for p in entries:
            if len(out) >= limit:
                return
            if p.is_dir():
                if p.name in SKIP_DIRS:
                    continue
                out.append(f"{prefix}{p.name}/")
                if level < depth:
                    visit(p, level + 1, prefix + "  ")
            else:
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                out.append(f"{prefix}{p.name} ({_size(size)})")

    visit(base, 1, "")
    if len(out) >= limit:
        out.append(f"... (stopped at {limit} entries; list a subfolder to see more)")
    return out


def _size(n):
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB" if n < 1024 ** 2 else f"{n / 1024 ** 2:.1f} MB"


def _run_powershell(command, cwd, cancel):
    """(exit code or None on timeout/stop, combined output)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    script = "[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'; " + command
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=flags,
    )
    waited, out = 0.0, b""
    while True:
        try:
            out, _ = proc.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            waited += 0.25
            if cancel.is_set() or waited >= CMD_TIMEOUT:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=flags)
                out, _ = proc.communicate()
                note = "\n[Stopped by the user]" if cancel.is_set() else f"\n[Timed out after {CMD_TIMEOUT} s]"
                return None, out.decode("utf-8", errors="replace") + note
    return proc.returncode, out.decode("utf-8", errors="replace")
