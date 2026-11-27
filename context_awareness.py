"""Active-window context awareness for voice dictation.

Inspects the focused window (<1 ms, local-only) to infer:
- App category (`code`, `terminal`, `email`, `chat`, `notes`, `general`) so the
  English fixer can match the expected style (technical vs email vs chat).
- Dynamic session hotwords extracted from the window title (filenames,
  CamelCase/snake_case identifiers, acronyms like `ComfyUI`, `GB10`, `AIGC`)
  so technical terms on screen are spelled correctly without editing
  `hotwords.txt`.
- Privacy guardrails: skips context extraction on password managers, OS
  credential prompts, and banking windows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

import platform_support

# Process stems (lowercase, without .exe) mapped to app categories.
_CODE_APPS = frozenset({
    "code", "code - insiders", "cursor", "windsurf", "antigravity",
    "pycharm64", "pycharm", "idea64", "idea", "webstorm64", "clion64",
    "goland64", "rider64", "datagrip64", "devenv", "sublime_text",
    "notepad++", "zed", "xcode", "nvim", "vim", "gvim", "emacs", "fleet",
})
_TERMINAL_APPS = frozenset({
    "windowsterminal", "wt", "powershell", "pwsh", "cmd", "conemu64",
    "mintty", "alacritty", "wezterm-gui", "wezterm", "warp", "iterm2",
    "terminal", "ghostty", "kitty", "hyper", "tabby",
})
_EMAIL_APPS = frozenset({
    "outlook", "olk", "hxoutlook", "thunderbird", "mail", "mailspring",
    "spark", "superhuman", "postbox", "emclient",
})
_CHAT_APPS = frozenset({
    "slack", "discord", "whatsapp", "telegram", "ms-teams", "teams",
    "wechat", "weixin", "signal", "messages", "line", "lark", "feishu",
    "zoom", "element",
})
_NOTES_APPS = frozenset({
    "obsidian", "notion", "winword", "onenote", "typora", "logseq",
    "bear", "craft", "evernote", "joplin", "notepad", "textedit",
})
_BROWSER_APPS = frozenset({
    "chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc",
    "safari", "zen", "chromium",
})

# Privacy: never scrape terms or inject title context from sensitive windows.
_SENSITIVE_APPS = frozenset({
    "1password", "keepass", "keepassxc", "bitwarden", "dashlane", "lastpass",
    "enpass", "nordpass", "protonpass", "credentialuibroker", "lockapp",
    "logonui", "keychain access",
})
_SENSITIVE_TITLE_KEYWORDS = (
    "password", "passcode", "sign in", "log in", "login", "2fa",
    "authenticator", "vault", "private key", "seed phrase", "recovery phrase",
    "internet banking", "ibanking", "online banking", "dbs digibank",
    "ocbc", "uob", "maybank", "cimb", "citibank", "hsbc", "chase bank",
    "bank of america", "wells fargo", "credit card", "checkout",
)

# Browser title keywords that identify web apps inside Chrome/Edge/Firefox.
_TITLE_EMAIL_HINTS = ("gmail", "outlook", "proton mail", "yahoo mail", "fastmail", "webmail", "inbox - ")
_TITLE_CHAT_HINTS = ("slack", "discord", "whatsapp", "telegram", "microsoft teams", "messenger")
_TITLE_CODE_HINTS = ("github", "gitlab", "bitbucket", "pull request", "vscode", "codespaces", "stack overflow", "huggingface", "hugging face", "jupyter", "colab", "comfyui")
_TITLE_NOTES_HINTS = ("google docs", "notion", "obsidian", "confluence", "linear", "jira", "figma")

# Generic window-chrome words that should never be injected as dynamic hotwords.
_STOP_TERMS = frozenset({
    "visual", "studio", "code", "google", "chrome", "microsoft", "edge",
    "mozilla", "firefox", "brave", "safari", "windows", "terminal",
    "powershell", "administrator", "untitled", "document", "window",
    "new", "tab", "home", "page", "inbox", "mail", "sent", "drafts",
    "search", "settings", "preferences", "profile", "personal", "work",
    "file", "edit", "view", "help", "default", "local", "http", "https",
    "www", "com", "org", "net", "html", "index", "main", "master",
    "the", "and", "for", "with", "from", "that", "this", "your", "our",
})

# Matches filenames (dictate.py), acronyms/model IDs (GB10, AIGC, Qwen3-ASR),
# CamelCase (ComfyUI, PyTorch), snake_case, or kebab-case.
_FILENAME_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._-]{1,40}\.(?:py|js|ts|tsx|jsx|json|md|yml|yaml|toml|sh|bat|ps1|rs|go|cpp|hpp|c|h|java|kt|swift|sql|html|css|txt|csv|env|cfg|ini|wav|mp3|gguf|onnx)\b")
_CAMEL_RE = re.compile(r"\b(?:[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*|[a-z]+[A-Z][A-Za-z0-9]*)\b")
_ACRONYM_OR_ALNUM_RE = re.compile(r"\b(?:[A-Z]{2,}[0-9A-Za-z-]*|[A-Z][a-z]+[0-9]+[A-Za-z0-9-]*)\b")
_SNAKE_OR_KEBAB_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[_-][A-Za-z0-9]+)+\b")


@dataclass(frozen=True)
class AppContext:
    """Snapshot of the focused window captured when dictation starts."""

    process_name: str = ""
    window_title: str = ""
    category: str = "general"
    sensitive: bool = False
    dynamic_terms: tuple[str, ...] = field(default_factory=tuple)

    @property
    def active(self) -> bool:
        return bool(self.process_name or self.window_title)


def _process_stem(process_name: str) -> str:
    name = (process_name or "").strip().lower()
    if name.endswith(".exe") or name.endswith(".app"):
        name = name[:-4]
    return name


def is_sensitive_context(process_name: str, window_title: str) -> bool:
    """Return True for password managers, auth dialogs and banking windows."""
    stem = _process_stem(process_name)
    if stem in _SENSITIVE_APPS:
        return True
    low_title = (window_title or "").lower()
    return any(keyword in low_title for keyword in _SENSITIVE_TITLE_KEYWORDS)


def classify_app(process_name: str, window_title: str = "") -> str:
    """Map a focused app and window title to a dictation style category."""
    stem = _process_stem(process_name)
    if stem in _CODE_APPS:
        return "code"
    if stem in _TERMINAL_APPS:
        return "terminal"
    if stem in _EMAIL_APPS:
        return "email"
    if stem in _CHAT_APPS:
        return "chat"
    if stem in _NOTES_APPS:
        return "notes"

    low_title = (window_title or "").lower()
    if stem in _BROWSER_APPS or not stem:
        if any(hint in low_title for hint in _TITLE_CODE_HINTS):
            return "code"
        if any(hint in low_title for hint in _TITLE_EMAIL_HINTS):
            return "email"
        if any(hint in low_title for hint in _TITLE_CHAT_HINTS):
            return "chat"
        if any(hint in low_title for hint in _TITLE_NOTES_HINTS):
            return "notes"
    return "general"


def extract_terms_from_title(window_title: str, max_terms: int = 12) -> list[str]:
    """Pull filenames, identifiers and technical acronyms out of a window title.

    Conservative by design: only extracts tokens that look like filenames,
    CamelCase, snake_case/kebab-case or technical acronyms/model IDs, avoiding
    ordinary English prose so ASR context stays short and clean.
    """
    title = (window_title or "").strip()
    if not title:
        return []

    candidates: list[str] = []
    for pattern in (_FILENAME_RE, _CAMEL_RE, _SNAKE_OR_KEBAB_RE, _ACRONYM_OR_ALNUM_RE):
        for match in pattern.finditer(title):
            token = match.group(0).strip(".-_")
            if len(token) < 2 or len(token) > 40:
                continue
            if token.isdigit():
                continue
            if token.lower() in _STOP_TERMS:
                continue
            # Avoid pure version numbers like "v2" or single-letter prefixes.
            if re.fullmatch(r"v\d+(?:\.\d+)*", token, re.IGNORECASE):
                continue
            candidates.append(token)

    terms: list[str] = []
    seen: set[str] = set()
    for token in candidates:
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        terms.append(token)
        if len(terms) >= max_terms:
            break
    return terms


def merge_hotwords(
    base_terms: list[str],
    dynamic_terms: list[str] | tuple[str, ...],
    max_extra: int = 12,
) -> list[str]:
    """Append dynamic session terms after static hotwords without duplicates."""
    merged: list[str] = []
    seen: set[str] = set()
    for term in base_terms:
        clean = (term or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            merged.append(clean)

    added = 0
    for term in dynamic_terms:
        if added >= max_extra:
            break
        clean = (term or "").strip()
        if not clean:
            continue
        key = clean.lower()
        if key not in seen:
            seen.add(key)
            merged.append(clean)
            added += 1
    return merged


def style_hint_for_category(category: str) -> str:
    """Short, focused style guidance for the English fixer based on app type."""
    return {
        "code": (
            "The user is typing in a code editor or developer tool: keep file names, "
            "code identifiers, technical terms, and acronyms exact, and keep phrasing direct."
        ),
        "terminal": (
            "The user is typing in a terminal or AI coding prompt: keep commands, paths, "
            "flags, model names, and technical terms exact, and keep phrasing concise."
        ),
        "email": (
            "The user is writing an email: use clear, polite, well-punctuated standard English."
        ),
        "chat": (
            "The user is writing a chat message: keep the English clean and natural, "
            "not overly formal."
        ),
        "notes": (
            "The user is writing notes or a document: use clear, structured standard English."
        ),
    }.get((category or "").strip().lower(), "")


def capture_active_context(
    window_provider=None,
    max_terms: int = 12,
) -> AppContext:
    """Capture the current foreground app context safely."""
    provider = window_provider or platform_support.active_window_info
    try:
        proc, title = provider()
    except Exception:
        return AppContext()

    proc = (proc or "").strip()
    title = (title or "").strip()
    if not proc and not title:
        return AppContext()

    if is_sensitive_context(proc, title):
        return AppContext(process_name=proc, window_title="", category="general", sensitive=True)

    category = classify_app(proc, title)
    terms = tuple(extract_terms_from_title(title, max_terms=max_terms))
    return AppContext(
        process_name=proc,
        window_title=title,
        category=category,
        sensitive=False,
        dynamic_terms=terms,
    )
