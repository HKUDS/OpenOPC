"""Flag-audited shell-command safety classification.

Single source of truth for shell-command handling in the approval pipeline:
splitting compound commands, stripping harmless redirections, analysing
command substitution, deriving grant prefixes, and deciding whether a command
is read-only-safe (auto-approvable without an approval card).

Design rules (mirroring the codex / Claude Code permission engines):
- Fail closed: anything unparseable, too dynamic, or unknown is NOT safe.
- Audited commands are classified by their flags, not just their name —
  ``find .`` is read-only, ``find . -delete`` is not. For audited commands the
  built-in verdict is final; a bare config prefix cannot rescue a failing
  audit.
- Config prefixes only extend coverage to commands the audit table does not
  know (user-trusted tools like ``ffmpeg``); network fetchers stay
  config-gated even though their flags are audited here.
"""

from __future__ import annotations

import re
import shlex
from typing import Iterable, Sequence

SHELL_CONTROL_TOKENS = {"&&", "||", ";", ";;", "|", "|&", "&"}

# Redirections that cannot write to a real file: fd duplication (2>&1, >&2)
# and discarding output into /dev/null.
SAFE_REDIRECTION_RE = re.compile(r"(?:\d?>>?\s*/dev/null\b|\d?>&\d|&>>?\s*/dev/null\b)")

# Loop/branch headers execute nothing themselves (or only the guarded command,
# which survives the strip). ``for``-style headers are dropped whole because
# their tokens are data (loop variables / word lists), not commands.
_DROP_SEGMENT_KEYWORDS = {"for", "while", "until", "case", "select", "function"}
_STRIP_LEADING_KEYWORDS = {"if", "elif", "then", "else", "do", "done", "fi", "esac", "{", "}", "!"}

# Environment assignments that cannot change what a command does in a way
# that matters for safety. PATH / LD_PRELOAD / PYTHONPATH etc. are absent on
# purpose: an unlisted assignment makes the segment fail the read-only audit.
_SAFE_ENV_VARS = {
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "COLUMNS", "LINES",
    "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "PYTHONIOENCODING", "PYTHONUNBUFFERED",
    "NODE_ENV", "PAGER", "GIT_PAGER",
}

# Substitution results that may safely expand into another command's argument
# list. Deliberately excludes anything that can carry file/environment content
# (`cat`, `echo`, `ls`, ...): allowing `curl $(cat secrets)` would turn a
# read-only helper into an exfil channel.
_EXPANSION_SAFE_HEADS = {
    "pwd", "date", "whoami", "hostname", "uname", "nproc", "basename",
    "dirname", "realpath", "readlink", "which",
    "git rev-parse", "git branch --show-current", "git describe",
}
_EXPANSION_PLACEHOLDER = "__opc_subst__"

# Commands that are read-only with any arguments, minus per-command banned
# flags. They print to stdout and cannot write files or execute other
# programs through their own options.
_GENERIC_READ_ONLY = {
    "cat", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "stat", "file",
    "basename", "dirname", "realpath", "readlink", "du", "df", "tree", "nproc",
    "whoami", "hostname", "date", "uname", "pwd", "ls", "id", "groups", "echo",
    "printf", "true", "false", "test", "[", "expr", "seq", "sleep", "diff",
    "cmp", "comm", "nl", "column", "expand", "unexpand", "paste", "join",
    "strings", "hexdump", "od", "md5sum", "sha1sum", "sha256sum", "sha512sum",
    "cksum", "b2sum", "which", "type", "grep", "egrep", "fgrep", "jq", "ps",
    "free", "uptime", "lscpu", "lsblk", "whereis", "cd", "wait", "pgrep",
    "getent", "locale", "tty", "arch", "printenv",
}

# Flags that make an otherwise read-only command write somewhere.
_BANNED_FLAGS: dict[str, set[str]] = {
    "sort": {"-o", "--output"},
    "date": {"-s", "--set"},
    "tree": {"-o"},
    "jq": set(),  # jq cannot execute or write via flags
    "grep": set(),
    "ps": set(),
}

_GIT_READ_ONLY_SUBCOMMANDS = {
    "status", "diff", "log", "show", "blame", "rev-parse", "ls-files",
    "ls-tree", "describe", "shortlog", "cat-file", "grep", "reflog",
    "count-objects", "diff-tree", "rev-list", "merge-base", "name-rev", "var",
    "check-ignore", "show-ref", "version", "--version", "cherry", "whatchanged",
}
# Subcommands that only stay read-only in their bare/list form.
_GIT_LIST_ONLY_SUBCOMMANDS = {"branch", "tag", "remote", "stash", "worktree", "config"}
_GIT_LIST_ONLY_SAFE_FLAGS = {
    "branch": {"--list", "-l", "-a", "--all", "-r", "--remotes", "-v", "-vv",
               "--verbose", "--show-current", "--contains", "--merged", "--no-merged"},
    "tag": {"--list", "-l", "-n", "--contains", "--merged", "--no-merged", "--sort"},
    "remote": {"-v", "--verbose"},
    "stash": set(),      # only `git stash list`
    "worktree": set(),   # only `git worktree list`
    "config": {"--get", "--get-all", "--list", "-l", "--get-regexp", "--global", "--local", "--system"},
}

_FIND_BANNED_PREDICATES = {
    "-delete", "-exec", "-execdir", "-ok", "-okdir",
    "-fprint", "-fprint0", "-fprintf", "-fls",
}

_RG_BANNED_FLAGS = {"--pre", "--hostname-bin"}

# curl writes to stdout by default; these flags make it write files, upload
# data, or read attacker-controlled config. Single chars cover combined short
# flags like ``-sSfLo``.
_CURL_BANNED_LONG = {
    "--output", "--remote-name", "--remote-name-all", "--output-dir",
    "--upload-file", "--data", "--data-binary", "--data-raw", "--data-ascii",
    "--data-urlencode", "--form", "--form-string", "--config", "--dump-header",
    "--cookie-jar", "--trace", "--trace-ascii", "--remote-header-name",
}
_CURL_BANNED_SHORT_CHARS = set("oOTdFKDcJ")

# Network fetchers stay config-gated: flag audit alone never auto-allows them,
# the command must also appear in the operator's safe-prefix config.
_NETWORK_AUDITED = {"curl"}

_INTERPRETERS = {"python", "python3", "python2", "node", "bun", "deno", "ruby", "perl"}
_VERSION_ONLY_FLAGS = {"-v", "-V", "--version"}

# Heads that must never become broad grant prefixes ("always allow bash"
# would be a blank check). Grants for these degrade to the exact command.
UNGRANTABLE_PREFIX_HEADS = {
    "bash", "sh", "zsh", "dash", "ksh", "eval", "source", ".", "sudo", "doas",
    "env", "xargs", "command", "exec", "nohup", "setsid", "watch", "script",
}

_SAFE_WRAPPER_HEADS = {"time", "nohup"}


def strip_safe_redirections(command: str) -> str:
    """Remove fd-duplication / null-sink redirections that cannot write files."""
    return SAFE_REDIRECTION_RE.sub(" ", str(command or ""))


def sanitize_expansions(command: str) -> tuple[str, bool]:
    """Replace expansion-safe ``$(...)`` with a placeholder.

    Returns ``(sanitized_text, all_safe)``. ``all_safe`` is False when the
    command contains backticks, process substitution, nested substitution, or
    a ``$(...)`` whose inner command is not in the expansion-safe set.
    """
    text = str(command or "")
    if "`" in text or "<(" in text or ">(" in text:
        return text, False
    out: list[str] = []
    i = 0
    all_safe = True
    while i < len(text):
        start = text.find("$(", i)
        if start < 0:
            out.append(text[i:])
            break
        out.append(text[i:start])
        depth = 1
        j = start + 2
        while j < len(text) and depth > 0:
            if text.startswith("$(", j):
                # nested substitution: too dynamic to audit
                return text, False
            if text[j] == "(":
                depth += 1
            elif text[j] == ")":
                depth -= 1
            j += 1
        if depth != 0:
            return text, False
        inner = text[start + 2:j - 1].strip()
        inner_head = " ".join(inner.split())
        if not any(
            inner_head == safe or inner_head.startswith(safe + " ")
            for safe in _EXPANSION_SAFE_HEADS
        ):
            all_safe = False
        out.append(_EXPANSION_PLACEHOLDER)
        i = j
    return "".join(out), all_safe


def has_blocked_substitution(command: str) -> bool:
    """True when the command contains substitution we refuse to auto-allow."""
    _, all_safe = sanitize_expansions(command)
    return not all_safe


def split_shell_segments(command: str) -> list[list[str]] | None:
    """Split a compound command into per-command token lists.

    Loop/branch headers are dropped or stripped so the returned segments are
    the commands that actually execute. Returns ``None`` when the input cannot
    be tokenized (unbalanced quotes etc.) — callers must fail closed.
    """
    text = str(command or "").replace("\r\n", "\n").replace("\n", " ; ").strip()
    if not text:
        return []
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in SHELL_CONTROL_TOKENS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)

    cleaned: list[list[str]] = []
    for segment in segments:
        if segment[0] in _DROP_SEGMENT_KEYWORDS:
            continue
        index = 0
        while index < len(segment) and segment[index] in _STRIP_LEADING_KEYWORDS:
            index += 1
        remainder = segment[index:]
        if remainder:
            cleaned.append(remainder)
    return cleaned


def command_has_redirection(command: str) -> bool:
    """Detect real (file-writing or file-reading) redirection tokens."""
    text = str(command or "").replace("\r\n", "\n").replace("\n", " ; ").strip()
    if not text:
        return False
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return any(marker in text for marker in (">", "<"))
    return any(token in {">", ">>", "<", "<<", "<<<"} for token in tokens)


def _strip_env_assignments(tokens: list[str]) -> tuple[list[str], bool]:
    """Consume leading VAR=value assignments; unsafe vars fail the audit."""
    index = 0
    safe = True
    while index < len(tokens):
        token = tokens[index]
        eq = token.find("=")
        if eq <= 0 or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token[:eq]):
            break
        if token[:eq] not in _SAFE_ENV_VARS:
            safe = False
        index += 1
    return tokens[index:], safe


def _strip_safe_wrappers(tokens: list[str]) -> list[str]:
    while tokens:
        head = tokens[0]
        if head in _SAFE_WRAPPER_HEADS:
            tokens = tokens[1:]
            continue
        if head == "timeout" and len(tokens) >= 2:
            rest = tokens[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            tokens = rest[1:] if rest else []
            continue
        if head == "nice":
            rest = tokens[1:]
            if len(rest) >= 2 and rest[0] == "-n":
                rest = rest[2:]
            elif rest and rest[0].startswith("-"):
                rest = rest[1:]
            tokens = rest
            continue
        if head == "stdbuf":
            rest = tokens[1:]
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            tokens = rest
            continue
        break
    return tokens


def _flags_in(tokens: Iterable[str]) -> list[str]:
    return [token for token in tokens if token.startswith("-")]


def _git_segment_read_only(tokens: list[str]) -> bool:
    rest = tokens[1:]
    # consume global options that take a value
    while rest and rest[0].startswith("-"):
        if rest[0] in {"-C", "-c", "--git-dir", "--work-tree", "--namespace"} and len(rest) >= 2:
            rest = rest[2:]
            continue
        if rest[0] in {"--no-pager", "--paginate", "-P", "-p"}:
            rest = rest[1:]
            continue
        if rest[0] in {"--version", "--help"}:
            return True
        return False
    if not rest:
        return False
    sub = rest[0]
    if sub in _GIT_READ_ONLY_SUBCOMMANDS:
        return True
    if sub in _GIT_LIST_ONLY_SUBCOMMANDS:
        args = rest[1:]
        if sub == "stash":
            return args[:1] == ["list"]
        if sub == "worktree":
            return args[:1] == ["list"]
        safe_flags = _GIT_LIST_ONLY_SAFE_FLAGS.get(sub, set())
        positionals = [a for a in args if not a.startswith("-")]
        flags_ok = all(a.split("=", 1)[0] in safe_flags for a in args if a.startswith("-"))
        if sub == "config":
            # reads need --get/--list; positionals are the key names
            has_read_flag = any(a.split("=", 1)[0] in {"--get", "--get-all", "--get-regexp", "--list", "-l"} for a in args)
            return flags_ok and has_read_flag
        return flags_ok and not positionals
    return False


def _sed_segment_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    if not any(a == "-n" or (a.startswith("-") and not a.startswith("--") and "n" in a[1:]) for a in args):
        return False
    scripts: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            if token.split("=", 1)[0] in {"-i", "--in-place", "-f", "--file", "-s"} or token.startswith("-i"):
                return False
            if token in {"-e", "--expression"} and index + 1 < len(args):
                scripts.append(args[index + 1])
                index += 2
                continue
            index += 1
            continue
        if not scripts:
            scripts.append(token)
        index += 1
    if not scripts:
        return False
    numeric_addr = r"[0-9,$; ]*p"
    pattern_addr = r"/(?:\\.|[^/\\])+/"
    regex_addr = rf"(?:{pattern_addr},)?{pattern_addr}p"
    return all(
        re.fullmatch(numeric_addr, script.strip()) or re.fullmatch(regex_addr, script.strip())
        for script in scripts
    )


def _awk_segment_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    program = ""
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("-"):
            head = token.split("=", 1)[0]
            if head in {"-f", "--file", "-i", "--include", "-l", "--load"}:
                return False
            if head in {"-v", "--assign", "-F", "--field-separator"} and "=" not in token and index + 1 < len(args):
                index += 2
                continue
            if head in {"-e", "--source"} and index + 1 < len(args):
                program = program or args[index + 1]
                index += 2
                continue
            index += 1
            continue
        if not program:
            program = token
        index += 1
    if not program:
        return False
    banned = ("system", ">", "|", "getline", "close(", "fflush(", "print >", "printf >")
    return not any(marker in program for marker in banned)


def _xxd_segment_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    if any(a == "-r" or a == "-revert" for a in args):
        return False
    positionals = [a for a in args if not a.startswith("-")]
    return len(positionals) <= 1


def _find_segment_read_only(tokens: list[str]) -> bool:
    return not any(token in _FIND_BANNED_PREDICATES for token in tokens[1:])


def _rg_segment_read_only(tokens: list[str]) -> bool:
    return not any(token.split("=", 1)[0] in _RG_BANNED_FLAGS for token in tokens[1:])


def _curl_flags_clean(tokens: list[str]) -> bool:
    args = tokens[1:]
    index = 0
    while index < len(args):
        token = args[index]
        if token.startswith("--"):
            if token.split("=", 1)[0] in _CURL_BANNED_LONG:
                return False
            if token.split("=", 1)[0] == "--request":
                value = token.split("=", 1)[1] if "=" in token else (args[index + 1] if index + 1 < len(args) else "")
                if value.upper() not in {"GET", "HEAD"}:
                    return False
        elif token.startswith("-") and len(token) > 1:
            if token == "-X":
                value = args[index + 1] if index + 1 < len(args) else ""
                if value.upper() not in {"GET", "HEAD"}:
                    return False
                index += 2
                continue
            if any(ch in _CURL_BANNED_SHORT_CHARS for ch in token[1:]):
                return False
        index += 1
    return True


# Commands the audit table knows. For these the audit verdict is FINAL: a
# bare config prefix (e.g. "find" in safe_command_prefixes) cannot rescue a
# failing audit, closing the `find -delete` / `curl -o` holes.
AUDITED_COMMAND_HEADS = (
    _GENERIC_READ_ONLY
    | _NETWORK_AUDITED
    | _INTERPRETERS
    | {"git", "find", "sed", "awk", "gawk", "mawk", "nawk", "rg", "xxd", "npm", "pip", "pip3"}
)


def _matches_config_prefix(segment_text: str, config_prefixes: Sequence[str]) -> bool:
    normalized = segment_text.casefold()
    for raw in config_prefixes:
        prefix = " ".join(str(raw or "").split()).casefold()
        if not prefix:
            continue
        if normalized == prefix or normalized.startswith(prefix + " "):
            return True
    return False


def _segment_read_only(tokens: list[str], config_prefixes: Sequence[str]) -> bool:
    tokens, env_safe = _strip_env_assignments(list(tokens))
    if not env_safe or not tokens:
        return False
    tokens = _strip_safe_wrappers(tokens)
    if not tokens:
        return False
    head = tokens[0]
    if head == "env":
        rest, env_safe = _strip_env_assignments(tokens[1:])
        if not env_safe:
            return False
        if not rest:
            return True  # bare `env` prints the environment
        tokens = rest
        head = tokens[0]
    if "/" in head:
        # path-invoked binaries (./find, /tmp/cat) are never classified by name
        return False

    if head == "git":
        return _git_segment_read_only(tokens)
    if head == "find":
        return _find_segment_read_only(tokens)
    if head == "sed":
        return _sed_segment_read_only(tokens)
    if head in {"awk", "gawk", "mawk", "nawk"}:
        return _awk_segment_read_only(tokens)
    if head == "rg":
        return _rg_segment_read_only(tokens)
    if head == "xxd":
        return _xxd_segment_read_only(tokens)
    if head in _NETWORK_AUDITED:
        segment_text = " ".join(tokens)
        if not _matches_config_prefix(head, config_prefixes) and not _matches_config_prefix(segment_text, config_prefixes):
            return False
        return _curl_flags_clean(tokens)
    if head in _INTERPRETERS or head in {"npm", "pip", "pip3"}:
        return len(tokens) == 2 and tokens[1] in _VERSION_ONLY_FLAGS
    if head in _GENERIC_READ_ONLY:
        banned = _BANNED_FLAGS.get(head, set())
        if banned and any(token.split("=", 1)[0] in banned for token in _flags_in(tokens[1:])):
            return False
        return True

    # Unknown command: honor operator-configured safe prefixes.
    return _matches_config_prefix(" ".join(tokens), config_prefixes)


def is_read_only_shell_command(
    command: str,
    config_prefixes: Sequence[str] = (),
) -> tuple[bool, str]:
    """Classify a (possibly compound) shell command as read-only-safe.

    Returns ``(safe, reason)``. Every segment must independently pass; any
    substitution we cannot prove harmless, real redirection, or unparseable
    input fails closed.
    """
    cleaned = " ".join(str(command or "").split()).strip()
    if not cleaned:
        return False, "empty command"
    sanitized, expansions_safe = sanitize_expansions(cleaned)
    if not expansions_safe:
        return False, "command substitution cannot be audited"
    sanitized = strip_safe_redirections(sanitized)
    if command_has_redirection(sanitized):
        return False, "command performs file redirection"
    segments = split_shell_segments(sanitized)
    if segments is None:
        return False, "command could not be parsed"
    if not segments:
        return False, "no executable segments"
    for tokens in segments:
        if not _segment_read_only(tokens, config_prefixes):
            return False, f"segment `{ ' '.join(tokens[:6]) }` is not proven read-only"
    return True, "all segments are flag-audited read-only commands"
