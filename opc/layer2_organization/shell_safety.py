"""Flag-audited shell-command safety classification.

Single source of truth for shell-command handling in the approval pipeline:
detecting active shell syntax, splitting commands for approval display,
analysing flags, deriving grant prefixes, and deciding whether a command is a
standalone read-only command (auto-approvable without an approval card).

Design rules (mirroring the codex / Claude Code permission engines):
- Fail closed: anything unparseable, too dynamic, or unknown is NOT safe.
- Compound commands and active shell syntax always require explicit review.
  A safe first command must never confer trust on a pipeline, redirection,
  substitution, subshell, background job, or subsequent command.
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
from pathlib import Path
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
    "NODE_ENV",
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
    "cat", "head", "tail", "wc", "cut", "tr", "stat",
    "basename", "dirname", "realpath", "readlink", "du", "nproc",
    "whoami", "uname", "pwd", "ls", "id", "groups", "echo",
    "printf", "true", "false", "test", "[", "expr", "seq", "sleep",
    "cmp", "comm", "nl", "column", "expand", "unexpand", "paste", "join",
    "strings", "hexdump", "od", "md5sum", "sha1sum", "sha256sum", "sha512sum",
    "cksum", "b2sum", "which", "type", "grep", "egrep", "fgrep", "jq", "ps",
    "free", "uptime", "lscpu", "lsblk", "whereis", "cd", "wait", "pgrep",
    "getent", "locale", "tty", "arch", "printenv",
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

# Git has options which turn an otherwise observational command into a file
# writer or an arbitrary-program launcher.  Keep these as a second, explicit
# approval boundary instead of relying on the subcommand name.  The short
# ``-O`` spelling is especially context-sensitive: for ``git grep`` it opens
# matching files in a pager, while for diff commands it reads an order file.
# Rejecting it everywhere is intentionally conservative.
_GIT_FORCED_REVIEW_LONG_OPTIONS = {
    "--config-env",
    "--exec-path",
    "--ext-diff",
    "--ext-grep",
    "--filters",
    "--open-files-in-pager",
    "--output",
    "--paginate",
    "--show-signature",
    "--textconv",
}

_GIT_GLOBAL_FLAGS = {
    "--bare",
    "--glob-pathspecs",
    "--icase-pathspecs",
    "--literal-pathspecs",
    "--no-lazy-fetch",
    "--no-optional-locks",
    "--no-pager",
    "--no-replace-objects",
    "--noglob-pathspecs",
    "-P",
}
_GIT_GLOBAL_VALUE_OPTIONS = {"-C", "--git-dir", "--namespace", "--work-tree"}

_GIT_STATUS_FLAGS = {
    "--ahead-behind", "--branch", "--ignored", "--long", "--no-ahead-behind",
    "--no-column", "--no-renames", "--null", "--porcelain", "--renames",
    "--short", "--show-stash", "--untracked-files", "--verbose",
    "-b", "-M", "-s", "-u", "-v", "-z",
}
_GIT_STATUS_OPTIONAL_VALUES = {
    "--column", "--find-renames", "--ignored", "--ignore-submodules",
    "--porcelain", "--untracked-files",
}

_GIT_DIFF_FLAGS = {
    "--binary", "--cached", "--check", "--color", "--color-moved",
    "--color-moved-ws", "--color-words", "--compact-summary", "--default-prefix",
    "--dirstat", "--dirstat-by-file", "--exit-code", "--find-copies-harder",
    "--full-index", "--histogram", "--ignore-all-space", "--ignore-blank-lines",
    "--ignore-cr-at-eol", "--ignore-space-at-eol", "--ignore-space-change",
    "--indent-heuristic", "--irreversible-delete", "--ita-invisible-in-index",
    "--ita-visible-in-index", "--merge-base", "--minimal", "--name-only",
    "--name-status", "--no-color", "--no-ext-diff", "--no-index", "--no-prefix",
    "--no-renames", "--no-textconv", "--numstat", "--patch", "--patch-with-raw",
    "--patch-with-stat", "--patience", "--pickaxe-all", "--pickaxe-regex",
    "--quiet", "--raw", "--relative", "--shortstat", "--staged", "--stat",
    "--submodule", "--summary", "--text", "--word-diff", "-B", "-C", "-M",
    "-R", "-a", "-p", "-s", "-u", "-z",
}
_GIT_DIFF_VALUE_OPTIONS = {
    "--abbrev", "--anchored", "--diff-algorithm", "--diff-filter", "--dst-prefix",
    "--find-copies", "--find-object", "--find-renames", "--ignore-matching-lines",
    "--ignore-submodules", "--inter-hunk-context", "--line-prefix", "--src-prefix",
    "--stat-graph-width", "--stat-name-width", "--stat-width", "--submodule",
    "--unified", "--word-diff", "--word-diff-regex", "-G", "-S", "-U", "-l",
}

_GIT_LOG_FLAGS = {
    "--abbrev-commit", "--all", "--all-match", "--ancestry-path", "--author-date-order",
    "--boundary", "--branches", "--children", "--cherry", "--cherry-mark",
    "--cherry-pick", "--date-order", "--decorate", "--do-walk", "--extended-regexp",
    "--first-parent", "--fixed-strings", "--full-history", "--graph", "--invert-grep",
    "--left-right", "--mailmap", "--merges", "--no-abbrev-commit", "--no-decorate",
    "--no-ext-diff", "--no-mailmap", "--no-merges", "--no-notes", "--no-patch",
    "--no-textconv", "--no-walk", "--notes", "--objects", "--objects-edge",
    "--oneline", "--parents", "--patch", "--perl-regexp", "--raw", "--reflog",
    "--regexp-ignore-case", "--relative-date", "--remotes", "--reverse", "--simplify-merges",
    "--source", "--tags", "--topo-order", "--use-mailmap", "--walk-reflogs",
    "-E", "-F", "-P", "-g", "-i", "-p", "-q",
} | _GIT_DIFF_FLAGS
_GIT_LOG_VALUE_OPTIONS = {
    "--abbrev", "--after", "--author", "--before", "--committer", "--date",
    "--decorate", "--decorate-refs", "--decorate-refs-exclude", "--encoding",
    "--exclude", "--format", "--glob", "--grep", "--max-count", "--max-parents",
    "--min-parents", "--notes", "--pretty", "--since", "--skip", "--until", "-L", "-n",
} | _GIT_DIFF_VALUE_OPTIONS

_GIT_REV_PARSE_FLAGS = {
    "--absolute-git-dir", "--all", "--branches", "--flags", "--git-common-dir",
    "--git-dir", "--is-bare-repository", "--is-inside-git-dir", "--is-inside-work-tree",
    "--is-shallow-repository", "--local-env-vars", "--no-flags", "--no-revs",
    "--parseopt", "--quiet", "--remotes", "--revs-only", "--show-cdup",
    "--show-object-format", "--show-prefix", "--show-superproject-working-tree",
    "--show-toplevel", "--sq", "--sq-quote", "--symbolic", "--symbolic-full-name",
    "--tags", "--verify", "-q",
}
_GIT_REV_PARSE_VALUE_OPTIONS = {
    "--abbrev-ref", "--default", "--disambiguate", "--exclude", "--exclude-hidden",
    "--glob", "--path-format", "--short",
}

_GIT_LS_FILES_FLAGS = {
    "--cached", "--debug", "--deduplicate", "--deleted", "--directory", "--empty-directory",
    "--eol", "--error-unmatch", "--exclude-standard", "--full-name", "--ignored", "--killed",
    "--modified", "--others", "--recurse-submodules", "--resolve-undo", "--stage", "--unmerged",
    "-c", "-d", "-f", "-i", "-k", "-m", "-o", "-s", "-t", "-u", "-v", "-z",
}
_GIT_LS_FILES_VALUE_OPTIONS = {
    "--abbrev", "--exclude", "--exclude-from", "--exclude-per-directory", "--with-tree", "-X", "-x",
}

_GIT_LS_TREE_FLAGS = {
    "--full-name", "--full-tree", "--long", "--name-only", "--name-status",
    "-d", "-l", "-r", "-t", "-z",
}
_GIT_LS_TREE_VALUE_OPTIONS = {"--abbrev", "--format"}

_GIT_DESCRIBE_FLAGS = {
    "--all", "--always", "--contains", "--debug", "--exact-match", "--first-parent",
    "--long", "--tags",
}
_GIT_DESCRIBE_VALUE_OPTIONS = {"--abbrev", "--broken", "--candidates", "--dirty", "--exclude", "--match"}

_GIT_SHORTLOG_FLAGS = {"--committer", "--email", "--numbered", "--summary", "-c", "-e", "-n", "-s"}
_GIT_SHORTLOG_VALUE_OPTIONS = {"--group"}

_GIT_CAT_FILE_FLAGS = {
    "--allow-unknown-type", "--batch", "--batch-all-objects", "--batch-check",
    "--batch-command", "--buffer", "--follow-symlinks", "--unordered", "-e", "-p", "-s", "-t",
}
_GIT_CAT_FILE_VALUE_OPTIONS = {"--batch", "--batch-check", "--batch-command", "--path"}

_GIT_GREP_FLAGS = {
    "--all-match", "--and", "--basic-regexp", "--break", "--cached", "--column",
    "--count", "--exclude-standard", "--extended-regexp", "--files-with-matches",
    "--files-without-match", "--fixed-strings", "--full-name", "--function-context",
    "--heading", "--ignore-case", "--invert-match", "--line-number", "--name-only",
    "--no-index", "--not", "--null", "--only-matching", "--or", "--perl-regexp",
    "--quiet", "--recurse-submodules", "--recursive", "--show-function", "--text",
    "--untracked", "--word-regexp", "-A", "-B", "-C", "-E", "-F", "-G", "-H", "-I",
    "-L", "-P", "-W", "-a", "-c", "-e", "-f", "-h", "-i", "-l", "-n", "-o", "-p",
    "-q", "-r", "-v", "-w", "-z",
}
_GIT_GREP_VALUE_OPTIONS = {"--after-context", "--before-context", "--color", "--context", "--max-depth", "--threads", "-A", "-B", "-C", "-e", "-f"}

_GIT_CHECK_IGNORE_FLAGS = {"--no-index", "--non-matching", "--quiet", "--stdin", "--verbose", "-n", "-q", "-v", "-z"}
_GIT_SHOW_REF_FLAGS = {
    "--dereference", "--exclude-existing", "--hash", "--head", "--heads", "--quiet", "--tags", "--verify",
    "-d", "-q", "-s",
}
_GIT_SHOW_REF_VALUE_OPTIONS = {"--abbrev", "--exclude-existing", "--hash"}

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

_SHELL_STRUCTURE_REASON_ORDER = (
    "command substitution",
    "shell control operator",
    "shell newline",
    "shell redirection",
    "subshell or grouping",
    "unbalanced shell quoting",
)


def _active_shell_syntax(command: str) -> set[str]:
    """Return active shell constructs, ignoring quoted/escaped literals.

    ``shlex`` correctly keeps quoted punctuation inside an argument but drops
    the quote type, which makes it unsuitable for distinguishing active
    ``$(...)`` in double quotes from the same bytes in single quotes.  This
    small scanner only recognizes syntax that affects command structure; the
    normal ``shlex`` parser still performs tokenization afterwards.
    """
    text = str(command or "")
    found: set[str] = set()
    quote = ""
    escaped = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "single":
            if char == "'":
                quote = ""
            index += 1
            continue
        if quote == "double":
            if escaped:
                escaped = False
                index += 1
                continue
            if char == "\\":
                escaped = True
                index += 1
                continue
            if char == '"':
                quote = ""
                index += 1
                continue
            # Command substitutions remain active inside double quotes.
            if char == "`" or (char == "$" and text.startswith("$(", index)):
                found.add("command substitution")
            index += 1
            continue

        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "'":
            quote = "single"
            index += 1
            continue
        if char == '"':
            quote = "double"
            index += 1
            continue
        if char == "`" or (char == "$" and text.startswith("$(", index)):
            found.add("command substitution")
        if char in ";&|":
            found.add("shell control operator")
        elif char in "\r\n":
            found.add("shell newline")
        elif char in "<>":
            found.add("shell redirection")
        elif char == "(" and (
            index == 0
            or text[index - 1].isspace()
            or text[index - 1] in ";&|()"
        ):
            # An opening parenthesis embedded in an ordinary argument (for
            # example yt-dlp's ``%(title)s`` template) is data for policy
            # purposes.  At a command boundary it starts a shell group.
            found.add("subshell or grouping")
        index += 1

    if quote or escaped:
        found.add("unbalanced shell quoting")
    return found


def shell_structure_requires_review(command: str) -> tuple[bool, str]:
    """Whether a command contains active shell syntax requiring a checkpoint.

    This is intentionally stricter than a per-segment read-only audit.  The
    approval boundary authorizes one standalone command at a time; compound
    execution and shell evaluation are never inferred safe from a prefix.
    """
    found = _active_shell_syntax(command)
    if not found:
        return False, "standalone command contains no active shell structure"
    ordered = [reason for reason in _SHELL_STRUCTURE_REASON_ORDER if reason in found]
    return True, "manual review required for " + ", ".join(ordered)


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
    """True when the command contains an active command substitution.

    All substitutions now require explicit approval, including substitutions
    whose inner command is read-only.  Single-quoted/escaped text is literal.
    """
    return "command substitution" in _active_shell_syntax(command)


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


def _git_forced_review_option(token: str) -> bool:
    if token == "-O" or token.startswith("-O"):
        return True
    head = token.split("=", 1)[0]
    return head in _GIT_FORCED_REVIEW_LONG_OPTIONS


def _git_parse_global_options(tokens: list[str]) -> tuple[str, list[str]] | None:
    """Return ``(subcommand, args)`` after a strict Git global-option parse."""

    rest = list(tokens[1:])
    if rest == ["--version"] or rest == ["version"]:
        return "version", []
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        token = rest[index]
        if _git_forced_review_option(token):
            return None
        # ``-c`` / ``--config-env`` can inject core.pager, diff.external,
        # core.fsmonitor, aliases, and other arbitrary-program helpers.  They
        # are deliberately absent from the allowlist.
        if token in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        head = token.split("=", 1)[0]
        if head in _GIT_GLOBAL_VALUE_OPTIONS:
            if "=" in token:
                if not token.split("=", 1)[1]:
                    return None
                index += 1
                continue
            if index + 1 >= len(rest):
                return None
            index += 2
            continue
        return None
    if index >= len(rest):
        return None
    return rest[index], rest[index + 1:]


def _git_options_are_safe(
    args: list[str],
    *,
    flags: set[str] | frozenset[str] = frozenset(),
    value_options: set[str] | frozenset[str] = frozenset(),
    optional_value_options: set[str] | frozenset[str] = frozenset(),
    attached_short_patterns: tuple[str, ...] = (),
    allow_positionals: bool = True,
) -> bool:
    """Validate subcommand options from explicit allowlists.

    Unknown options fail closed. Values are inert argv tokens because active
    shell syntax was rejected before this parser is reached.
    """

    after_separator = False
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            after_separator = True
            index += 1
            continue
        if after_separator or not token.startswith("-") or token == "-":
            if not allow_positionals:
                return False
            index += 1
            continue
        if _git_forced_review_option(token):
            return False
        if token in flags:
            index += 1
            continue
        head = token.split("=", 1)[0]
        if head in optional_value_options:
            index += 1
            continue
        if head in value_options:
            if "=" in token:
                if not token.split("=", 1)[1]:
                    return False
                index += 1
                continue
            if index + 1 >= len(args):
                return False
            index += 2
            continue
        if any(re.fullmatch(pattern, token) for pattern in attached_short_patterns):
            index += 1
            continue
        return False
    return True


def _git_config_read_only(args: list[str]) -> bool:
    read_actions = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
    flags = {
        "--fixed-value", "--global", "--includes", "--local", "--name-only",
        "--no-includes", "--null", "--show-names", "--show-origin", "--show-scope",
        "--system", "--worktree", "-z",
    } | read_actions
    value_options = {"--blob", "--default", "--file", "--type"}
    if not any(token.split("=", 1)[0] in read_actions for token in args):
        return False
    return _git_options_are_safe(
        args,
        flags=flags,
        value_options=value_options,
        allow_positionals=True,
    )


def _git_list_only_read_only(sub: str, args: list[str]) -> bool:
    if sub == "branch":
        flags = {
            "--all", "--color", "--format", "--ignore-case", "--list", "--no-color",
            "--no-column", "--no-contains", "--no-merged", "--points-at", "--remotes",
            "--show-current", "--sort", "--verbose", "-a", "-l", "-r", "-v", "-vv",
        }
        values = {"--color", "--contains", "--format", "--merged", "--no-contains", "--no-merged", "--points-at", "--sort"}
        positionals = [item for item in args if not item.startswith("-")]
        permits_patterns = any(item in {"--list", "-l"} for item in args)
        return (permits_patterns or not positionals) and _git_options_are_safe(
            args,
            flags=flags,
            value_options=values,
            optional_value_options={"--color", "--contains", "--merged", "--no-contains", "--no-merged"},
        )
    if sub == "tag":
        flags = {"--color", "--column", "--ignore-case", "--list", "--no-column", "-l"}
        values = {"--contains", "--format", "--merged", "--no-contains", "--no-merged", "--points-at", "--sort"}
        positionals = [item for item in args if not item.startswith("-")]
        permits_patterns = any(item in {"--list", "-l"} for item in args)
        return (permits_patterns or not positionals) and _git_options_are_safe(
            args,
            flags=flags,
            value_options=values,
            optional_value_options={"--color", "--column", "--contains", "--merged", "--no-contains", "--no-merged"},
            attached_short_patterns=(r"-n\d*",),
        )
    if sub == "remote":
        return not args or args in (["-v"], ["--verbose"])
    if sub == "stash":
        return bool(args[:1] == ["list"]) and _git_options_are_safe(
            args[1:],
            flags=_GIT_LOG_FLAGS,
            value_options=_GIT_LOG_VALUE_OPTIONS,
            optional_value_options={"--decorate", "--notes"},
            attached_short_patterns=(r"-\d+", r"-n\d+"),
        )
    if sub == "worktree":
        return bool(args[:1] == ["list"]) and _git_options_are_safe(
            args[1:],
            flags={"--porcelain", "--verbose", "-v", "-z"},
            value_options={"--expire"},
            allow_positionals=False,
        )
    if sub == "config":
        return _git_config_read_only(args)
    return False


def _git_segment_read_only(tokens: list[str]) -> bool:
    parsed = _git_parse_global_options(tokens)
    if parsed is None:
        return False
    sub, args = parsed
    if sub == "version":
        return not args
    if sub == "status":
        return _git_options_are_safe(
            args,
            flags=_GIT_STATUS_FLAGS,
            optional_value_options=_GIT_STATUS_OPTIONAL_VALUES,
            attached_short_patterns=(r"-[sbuvzM]+", r"-M\d+"),
        )
    if sub in {"diff", "diff-tree"}:
        return _git_options_are_safe(
            args,
            flags=_GIT_DIFF_FLAGS | ({"--stdin", "--root", "--cc", "-c", "-m", "-r", "-t", "-v"} if sub == "diff-tree" else set()),
            value_options=_GIT_DIFF_VALUE_OPTIONS,
            optional_value_options={"--color", "--color-moved", "--color-moved-ws", "--dirstat", "--relative", "--stat", "--submodule", "--word-diff"},
            attached_short_patterns=(r"-U\d+", r"-[BMC]\d*%?", r"-l\d+", r"-[SG].+"),
        )
    if sub in {"log", "show", "whatchanged"}:
        return _git_options_are_safe(
            args,
            flags=_GIT_LOG_FLAGS,
            value_options=_GIT_LOG_VALUE_OPTIONS,
            optional_value_options={"--decorate", "--notes"},
            attached_short_patterns=(r"-\d+", r"-n\d+", r"-U\d+", r"-[BMC]\d*%?", r"-l\d+", r"-[SG].+"),
        )
    if sub == "blame":
        return _git_options_are_safe(
            args,
            flags={
                "--color-by-age", "--color-lines", "--incremental", "--line-porcelain",
                "--minimal", "--porcelain", "--progress", "--root", "--score-debug",
                "--show-email", "--show-name", "--show-number", "--show-stats",
                "-b", "-c", "-e", "-f", "-l", "-n", "-p", "-s", "-t", "-w",
            },
            value_options={"--abbrev", "--contents", "--ignore-rev", "--ignore-revs-file", "-L", "-S"},
            attached_short_patterns=(r"-[CM]\d*",),
        )
    if sub == "rev-parse":
        return _git_options_are_safe(
            args,
            flags=_GIT_REV_PARSE_FLAGS,
            value_options=_GIT_REV_PARSE_VALUE_OPTIONS,
            optional_value_options={"--abbrev-ref", "--short"},
        )
    if sub == "ls-files":
        return _git_options_are_safe(args, flags=_GIT_LS_FILES_FLAGS, value_options=_GIT_LS_FILES_VALUE_OPTIONS, optional_value_options={"--abbrev"})
    if sub == "ls-tree":
        return _git_options_are_safe(args, flags=_GIT_LS_TREE_FLAGS, value_options=_GIT_LS_TREE_VALUE_OPTIONS, optional_value_options={"--abbrev"})
    if sub == "describe":
        return _git_options_are_safe(args, flags=_GIT_DESCRIBE_FLAGS, value_options=_GIT_DESCRIBE_VALUE_OPTIONS, optional_value_options={"--abbrev", "--broken", "--dirty"})
    if sub == "shortlog":
        return _git_options_are_safe(args, flags=_GIT_SHORTLOG_FLAGS, value_options=_GIT_SHORTLOG_VALUE_OPTIONS, attached_short_patterns=(r"-w(?:\d+(?:,\d+(?:,\d+)?)?)?",))
    if sub == "cat-file":
        return _git_options_are_safe(args, flags=_GIT_CAT_FILE_FLAGS, value_options=_GIT_CAT_FILE_VALUE_OPTIONS, optional_value_options={"--batch", "--batch-check", "--batch-command"})
    if sub == "grep":
        return _git_options_are_safe(args, flags=_GIT_GREP_FLAGS, value_options=_GIT_GREP_VALUE_OPTIONS, optional_value_options={"--color"}, attached_short_patterns=(r"-\d+",))
    if sub == "reflog":
        if not args:
            return True
        if args[0] == "exists":
            return _git_options_are_safe(args[1:], allow_positionals=True)
        show_args = args[1:] if args[0] == "show" else args
        return _git_options_are_safe(show_args, flags=_GIT_LOG_FLAGS, value_options=_GIT_LOG_VALUE_OPTIONS, optional_value_options={"--decorate", "--notes"}, attached_short_patterns=(r"-\d+", r"-n\d+"))
    if sub == "count-objects":
        return _git_options_are_safe(args, flags={"--human-readable", "--verbose", "-H", "-v"}, allow_positionals=False)
    if sub == "rev-list":
        return _git_options_are_safe(args, flags=_GIT_LOG_FLAGS | {"--bisect-all", "--bisect-vars", "--count", "--header", "--no-object-names", "--object-names", "--unpacked"}, value_options=_GIT_LOG_VALUE_OPTIONS, optional_value_options={"--branches", "--remotes", "--tags"}, attached_short_patterns=(r"-\d+", r"-n\d+"))
    if sub == "merge-base":
        return _git_options_are_safe(args, flags={"--all", "--fork-point", "--independent", "--is-ancestor", "--octopus", "-a"})
    if sub == "name-rev":
        return _git_options_are_safe(args, flags={"--all", "--always", "--name-only", "--stdin", "--tags", "--undefined"}, value_options={"--exclude", "--refs"})
    if sub == "var":
        return _git_options_are_safe(args, flags={"-l"}) and bool(args)
    if sub == "check-ignore":
        return _git_options_are_safe(args, flags=_GIT_CHECK_IGNORE_FLAGS)
    if sub == "show-ref":
        return _git_options_are_safe(args, flags=_GIT_SHOW_REF_FLAGS, value_options=_GIT_SHOW_REF_VALUE_OPTIONS, optional_value_options={"--abbrev", "--exclude-existing", "--hash"})
    if sub == "cherry":
        return _git_options_are_safe(args, flags={"--verbose", "-v"}, value_options={"--abbrev"}, optional_value_options={"--abbrev"})
    if sub in _GIT_LIST_ONLY_SUBCOMMANDS:
        return _git_list_only_read_only(sub, args)
    return False


_GIT_REVIEWABLE_FAMILIES = (
    _GIT_READ_ONLY_SUBCOMMANDS
    | _GIT_LIST_ONLY_SUBCOMMANDS
    | {"help", "version"}
)


def _git_candidate_subcommand(tokens: list[str]) -> str:
    """Best-effort subcommand extraction for a rejected global option list."""

    args = list(tokens[1:])
    index = 0
    value_options = _GIT_GLOBAL_VALUE_OPTIONS | {"-c", "--config-env"}
    while index < len(args):
        token = args[index]
        if not token.startswith("-"):
            return token
        head = token.split("=", 1)[0]
        if head in value_options and "=" not in token:
            index += 2
        else:
            index += 1
    return ""


def _git_global_options_require_review(tokens: list[str]) -> bool:
    """Whether argv before the Git subcommand contains an unsafe global."""

    rest = list(tokens[1:])
    if not rest or rest == ["--version"]:
        return False
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        token = rest[index]
        if token in _GIT_GLOBAL_FLAGS:
            index += 1
            continue
        head = token.split("=", 1)[0]
        if head in _GIT_GLOBAL_VALUE_OPTIONS:
            if "=" in token:
                if not token.split("=", 1)[1]:
                    return True
                index += 1
                continue
            if index + 1 >= len(rest):
                return True
            index += 2
            continue
        return True
    return False


def _git_launch_tokens(tokens: list[str]) -> tuple[list[str], bool]:
    """Peel inert wrappers/environment prefixes while retaining env safety."""

    remaining = list(tokens)
    environment_safe = True
    while remaining:
        stripped, safe = _strip_env_assignments(remaining)
        environment_safe = environment_safe and safe
        remaining = stripped
        if not remaining:
            break
        wrapped = _strip_safe_wrappers(list(remaining))
        if wrapped != remaining:
            remaining = wrapped
            continue
        if remaining[0] == "env":
            env_args = remaining[1:]
            # Only the two inert environment-display modifiers are consumed.
            # Other ``env`` options fail closed but we still locate a following
            # Git command so the durable allowlist cannot bypass review.
            while env_args and env_args[0] in {"-i", "--ignore-environment"}:
                env_args = env_args[1:]
            stripped, safe = _strip_env_assignments(env_args)
            environment_safe = environment_safe and safe
            remaining = stripped
            continue
        break
    return remaining, environment_safe


def git_read_only_family_requires_review(command: str) -> tuple[bool, str]:
    """Force a one-shot human checkpoint for unsafe Git read-family forms.

    This guard runs before reusable permission rules.  Otherwise a historical
    ``git diff`` grant could authorize ``git diff --output=...`` even though
    the flag audit correctly stopped considering it read-only.
    """

    segments = split_shell_segments(str(command or ""))
    if not segments or len(segments) != 1:
        return False, ""
    tokens, env_safe = _git_launch_tokens(list(segments[0]))
    if not tokens:
        return False, ""
    executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1]
    if executable != "git":
        return False, ""
    if not env_safe or _git_global_options_require_review(tokens):
        return (
            True,
            "manual review required for Git options or environment that are not proven read-only",
        )
    parsed = _git_parse_global_options(tokens)
    subcommand = parsed[0] if parsed is not None else _git_candidate_subcommand(tokens)
    if subcommand not in _GIT_REVIEWABLE_FAMILIES:
        return False, ""
    if tokens[0] == "git" and _git_segment_read_only(tokens):
        return False, ""
    return (
        True,
        "manual review required for Git options or environment that are not proven read-only",
    )


def _sed_segment_read_only(tokens: list[str]) -> bool:
    args = tokens[1:]
    quiet = False
    scripts: list[str] = []
    positionals: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-n", "--quiet", "--silent"}:
            quiet = True
            index += 1
            continue
        if token in {"-e", "--expression"}:
            if index + 1 >= len(args):
                return False
            scripts.append(args[index + 1])
            index += 2
            continue
        if token.startswith("-"):
            return False
        positionals.append(token)
        index += 1
    if not scripts and positionals:
        scripts.append(positionals.pop(0))
    if not quiet or not scripts:
        return False
    return all(re.fullmatch(r"[0-9,$; ]*p", script.strip()) for script in scripts)


def _df_segment_read_only(tokens: list[str]) -> bool:
    """Only bare/path-only df invocations are proven side-effect free."""

    return all(token and not token.startswith("-") for token in tokens[1:])


def _diff_segment_read_only(tokens: list[str]) -> bool:
    """Only the two-operand, option-free diff form is proven read-only."""

    args = tokens[1:]
    return len(args) == 2 and all(not token.startswith("-") for token in args)


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
    | {
        "git",
        "find",
        "sed",
        "df",
        "diff",
        "awk",
        "gawk",
        "mawk",
        "nawk",
        "rg",
        "xxd",
        "npm",
        "pip",
        "pip3",
    }
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
    if head == "df":
        return _df_segment_read_only(tokens)
    if head == "diff":
        return _diff_segment_read_only(tokens)
    if head in {"awk", "gawk", "mawk", "nawk"}:
        return False
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
        return True

    # Unknown command: honor operator-configured safe prefixes.
    return _matches_config_prefix(" ".join(tokens), config_prefixes)


def is_read_only_shell_command(
    command: str,
    config_prefixes: Sequence[str] = (),
) -> tuple[bool, str]:
    """Classify one standalone shell command as read-only-safe.

    Returns ``(safe, reason)``. Active shell structure and unparseable input
    fail closed before the flag audit, even when each apparent segment would
    be read-only in isolation.
    """
    cleaned = str(command or "").strip()
    if not cleaned:
        return False, "empty command"
    requires_review, reason = shell_structure_requires_review(cleaned)
    if requires_review:
        return False, reason
    segments = split_shell_segments(cleaned)
    if segments is None:
        return False, "command could not be parsed"
    if not segments:
        return False, "no executable segments"
    for tokens in segments:
        if not _segment_read_only(tokens, config_prefixes):
            return False, f"segment `{ ' '.join(tokens[:6]) }` is not proven read-only"
    return True, "all segments are flag-audited read-only commands"


_COMPANY_WORKSPACE_INSPECTION_HEADS = frozenset({
    "basename", "b2sum", "cat", "cksum", "cmp", "comm", "cut", "diff",
    "dirname", "du", "egrep", "fgrep", "find", "git", "grep", "head",
    "hexdump", "jq", "ls", "md5sum", "nl", "od", "pwd", "readlink",
    "realpath", "rg", "sed", "sha1sum", "sha256sum", "sha512sum", "stat",
    "strings", "tail", "wc", "xxd",
})


def is_workspace_scoped_read_only_shell_command(
    command: str,
    *,
    working_directory: str,
    workspace_root: str,
) -> tuple[bool, str]:
    """Prove one shell inspection is read-only and workspace-confined.

    This is deliberately narrower than :func:`is_read_only_shell_command`.
    Company mode may bypass a human checkpoint only for a single built-in,
    audited inspection whose cwd and every possible path operand stay under
    the durable workspace root. Dynamic expansion, wrappers, configured
    prefixes, and Git directory overrides fail closed.
    """

    safe, reason = is_read_only_shell_command(command)
    if not safe:
        return False, reason
    segments = split_shell_segments(command)
    if not segments or len(segments) != 1:
        return False, "company read-only execution requires one standalone command"
    tokens = list(segments[0])
    if not tokens or tokens[0] not in _COMPANY_WORKSPACE_INSPECTION_HEADS:
        return False, "command is not a company workspace inspection command"

    # The POSIX shell expands these tokens after policy evaluation. Quoted
    # literals intentionally stay conservative: exact approval remains
    # available for unusual filenames without weakening the automatic path
    # boundary.
    expansion_markers = ("$", "`", "*", "?", "{", "}", "[", "]")
    if any(
        any(marker in token for marker in expansion_markers)
        for token in tokens
    ):
        return False, "dynamic shell expansion is not workspace-confined"

    if not str(workspace_root or "").strip() or not str(
        working_directory or ""
    ).strip():
        return False, "company workspace boundary is unavailable"
    try:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        cwd = Path(working_directory).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False, "company workspace or working directory is unavailable"
    if (
        not root.is_dir()
        or not cwd.is_dir()
        or (cwd != root and root not in cwd.parents)
    ):
        return False, "working directory is outside the company workspace"

    if tokens[0] == "git":
        parsed = _git_parse_global_options(tokens)
        if parsed is None:
            return False, "Git invocation is not proven read-only"
        subcommand, _ = parsed
        if subcommand in {"config", "worktree"}:
            return False, (
                "Git command can inspect state outside the company workspace"
            )
        for token in tokens[1:]:
            if token == subcommand:
                break
            if token.split("=", 1)[0] in _GIT_GLOBAL_VALUE_OPTIONS:
                return False, "Git directory overrides are not workspace-confined"

    after_separator = False
    for token in tokens[1:]:
        if token == "--" and not after_separator:
            after_separator = True
            continue
        candidate = token
        if not after_separator and token.startswith("-") and token != "-":
            if "=" in token:
                candidate = token.split("=", 1)[1]
            else:
                # Attached file options such as ``grep -f../rules`` cannot be
                # separated safely without a command-specific argv parser.
                if "/" in token or "~" in token or ".." in token:
                    return False, (
                        "attached option path is not workspace-auditable"
                    )
                continue
        if candidate in {"", "-"}:
            continue
        if candidate.startswith("@") and len(candidate) > 1:
            candidate = candidate[1:]
        try:
            raw_path = Path(candidate).expanduser()
            resolved = (
                raw_path if raw_path.is_absolute() else cwd / raw_path
            ).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return False, "command path operand cannot be resolved safely"
        if resolved != root and root not in resolved.parents:
            return False, (
                f"path operand is outside the company workspace: {candidate}"
            )

    return True, (
        "flag-audited read-only command is confined to the company workspace"
    )
