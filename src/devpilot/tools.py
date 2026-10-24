
import re
from pathlib import Path

# Directories that should never be searched or read by the agent.
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
}

# Avoid sending very large files into the model's context.
MAX_FILE_SIZE = 1_000_000
MAX_READ_SIZE = 200_000

# Common secret/configuration files that must not be exposed to the agent.
SENSITIVE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
}


def _resolve_repository_root(repo_root: str) -> Path | None:
    """Return a valid absolute repository root, or None if it is invalid."""
    root = Path(repo_root).resolve()
    return root if root.is_dir() else None


def _is_excluded(relative_path: Path) -> bool:
    """Check each path component, not just the final filename."""
    return any(part.lower() in EXCLUDED_DIRS for part in relative_path.parts)


def _is_sensitive_filename(filename: str) -> bool:
    """Identify known sensitive names and files derived from those names."""
    filename_lower = filename.lower()

    for sensitive_name in SENSITIVE_NAMES:
        sensitive_lower = sensitive_name.lower()

        # Block exact names, plus extensions appended to sensitive names,
        # such as .env.py and credentials.json.py.
        if (
            filename_lower == sensitive_lower
            or filename_lower.startswith(sensitive_lower + ".")
        ):
            return True

    return False


def search_code(query: str, repo_root: str = ".") -> str:
    """Search Python files and return matching lines with verified line numbers."""
    if not isinstance(query, str) or not query.strip():
        return "Error: search query cannot be empty."

    root = _resolve_repository_root(repo_root)
    if root is None:
        return "Error: repository directory does not exist."

    # Strip surrounding whitespace and unescape regex backslashes if present.
    raw_query = query.strip()
    clean_query = re.sub(r"\\([()[\]{}.*+?^$|\\])", r"\1", raw_query)

    # Prepare search candidates: exact, unescaped, and snake_case for natural phrasing.
    candidates = [raw_query.casefold()]
    if clean_query.casefold() not in candidates:
        candidates.append(clean_query.casefold())

    # Fallback to snake_case if multiple words were queried (e.g. "normalize username" -> "normalize_username").
    snake_candidate = None
    if " " in clean_query:
        snake = re.sub(r"\s+", "_", clean_query).casefold()
        if snake not in candidates:
            snake_candidate = snake

    tokens = [t.casefold() for t in clean_query.split() if len(t) > 1] if " " in clean_query else []

    results = []
    fallback_results = []

    # Search Python source recursively, skipping generated/vendor directories.
    for file_path in root.rglob("*.py"):
        try:
            relative_path = file_path.relative_to(root)

            if _is_excluded(relative_path):
                continue

            # Do not search files whose names indicate sensitive content.
            if _is_sensitive_filename(file_path.name):
                continue

            # Do not follow a file that resolves outside the selected repository.
            resolved_path = file_path.resolve()
            if not resolved_path.is_relative_to(root):
                continue

            if not resolved_path.is_file():
                continue

            if resolved_path.stat().st_size > MAX_FILE_SIZE:
                continue

            content = resolved_path.read_text(
                encoding="utf-8",
                errors="replace",
            )

            # enumerate(start=1) gives actual source-file line numbers.
            for line_number, line in enumerate(content.splitlines(), start=1):
                line_cf = line.casefold()

                # Primary search matches
                if any(candidate in line_cf for candidate in candidates):
                    results.append(
                        f"{relative_path.as_posix()}:{line_number}: {line.rstrip()}"
                    )
                    if len(results) >= 20:
                        break
                elif not results:
                    # Collect fallback match if primary hasn't found anything yet
                    if (snake_candidate and snake_candidate in line_cf) or (
                        tokens and all(tok in line_cf for tok in tokens)
                    ):
                        fallback_results.append(
                            f"{relative_path.as_posix()}:{line_number}: {line.rstrip()}"
                        )

            if len(results) >= 20:
                return (
                    f"Search query: {query}\n"
                    "Matches (maximum 20):\n"
                    + "\n".join(results)
                )

        # One unreadable file should not stop the entire search.
        except (OSError, ValueError):
            continue

    if not results and fallback_results:
        results = fallback_results[:20]

    if not results:
        return f"No matches found for: {query}"

    return (
        f"Search query: {query}\n"
        f"Matches ({len(results)}):\n"
        + "\n".join(results)
    )


def read_file(path: str, repo_root: str = ".") -> str:
    """Read a repository file and return source with explicit line numbers."""
    if not isinstance(path, str) or not path.strip():
        return "Error: file path cannot be empty."

    root = _resolve_repository_root(repo_root)
    if root is None:
        return "Error: repository directory does not exist."

    requested_path = Path(path)

    # Callers must provide repository-relative paths.
    if requested_path.is_absolute():
        return "Error: use a repository-relative path."

    try:
        target = (root / requested_path).resolve()

        # Prevent traversal such as ../../private_file.txt and symlinks escaping root.
        if not target.is_relative_to(root):
            return "Error: path is outside the repository."

        relative_path = target.relative_to(root)

        # Block access to excluded/generated directories.
        if _is_excluded(relative_path):
            return "Error: access to excluded directories is blocked."

        # Block sensitive filenames case-insensitively, including appended extensions.
        if _is_sensitive_filename(target.name):
            return "Error: access to sensitive files is blocked."

        if not target.is_file():
            return "Error: file does not exist."

        if target.stat().st_size > MAX_READ_SIZE:
            return "Error: file is too large to read."

        source = target.read_text(
            encoding="utf-8",
            errors="replace",
        )

        # Number every original source line so the model can cite real line numbers.
        numbered_lines = [
            f"{line_number:>4} | {line}"
            for line_number, line in enumerate(source.splitlines(), start=1)
        ]

        return (
            f"FILE: {relative_path.as_posix()}\n"
            f"LINE NUMBERS: 1-based, verified from this file\n"
            f"--- SOURCE START ---\n"
            + "\n".join(numbered_lines)
            + "\n--- SOURCE END ---"
        )

    except (OSError, ValueError) as exc:
        # Return a readable tool error rather than crashing the investigation loop.
        return f"Error reading file: {exc}"
