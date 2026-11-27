
#!/usr/bin/env python3
"""Switch the project's dense embedding model to IBM Granite.

Usage:
    python apply_granite.py --dry-run
    python apply_granite.py
    python apply_granite.py --model small
    python apply_granite.py --skip-db-patch

Changes:
  1. settings.py: DENSE_MODEL and DENSE_DIM
  2. embeddings.py: removes BGE-only query prefix
  3. README.md: updates model name
  4. database.py: supports ORCH_DB_PATH

Safe to run multiple times.
Does not modify RETRIEVAL_MIN_SIMILARITY.
"""

import argparse
import difflib
import py_compile
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent

MODELS = {
    "english": (
        "ibm-granite/granite-embedding-english-r2",
        768,
    ),
    "small": (
        "ibm-granite/granite-embedding-small-english-r2",
        384,
    ),
    "multilingual": (
        "ibm-granite/granite-embedding-311m-multilingual-r2",
        768,
    ),
    "multilingual-small": (
        "ibm-granite/granite-embedding-97m-multilingual-r2",
        384,
    ),
}

SETTINGS = "backend/shared_common/common/settings.py"
EMBEDDINGS = "backend/shared_common/common/embeddings.py"
README = "README.md"
ORCH_DB = "backend/orchestrator/database.py"

OK = "changed"
SKIPPED = "already done"
FAILED = "FAILED"


# ------------------------------------------------------------
# File utilities


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write(path: Path, content: str) -> None:
    path.write_bytes(content.encode("utf-8"))


def newline_of(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


# ------------------------------------------------------------
# Settings patch


def patch_settings(text, name, dim):
    new = text
    problems = []

    replacements = [
        (
            "DENSE_MODEL",
            r'^DENSE_MODEL = "[^"\r\n]*"',
            f'DENSE_MODEL = "{name}"',
        ),
        (
            "DENSE_DIM",
            r"^DENSE_DIM = \d+",
            f"DENSE_DIM = {dim}",
        ),
    ]

    for label, pattern, value in replacements:

        if not re.search(pattern, new, flags=re.M):
            problems.append(f"{label} line not found")
            continue

        new = re.sub(
            pattern,
            value,
            new,
            count=1,
            flags=re.M,
        )

    if problems:
        return text, FAILED, "; ".join(problems)

    if new == text:
        return text, SKIPPED, f"already {name} ({dim})"

    return new, OK, f"{name}, {dim} dimensions"


# ------------------------------------------------------------
# Embeddings patch


QUERY_PREFIX = re.compile(
    r'query\s*=\s*\(\s*'
    r'"Represent this sentence for searching relevant passages: "\s*'
    r'\+\s*text\s*'
    r'\)'
)


def patch_embeddings(text):

    new = text
    notes = []

    if QUERY_PREFIX.search(new):

        new = QUERY_PREFIX.sub(
            "query = text",
            new,
            count=1,
        )

        notes.append("removed the BGE query prefix")

    elif "Represent this sentence" in new:

        return (
            text,
            FAILED,
            "BGE prefix found in an unexpected format; edit manually",
        )

    old_doc = "because bge-base reads at most 512."

    if old_doc in new:

        new = new.replace(
            old_doc,
            "because the dense model reads a limited number of tokens.",
        )

        notes.append("updated a docstring")

    if new == text:
        return text, SKIPPED, "prefix already removed"

    return new, OK, ", ".join(notes)


# ------------------------------------------------------------
# README patch


def patch_readme(text, name):

    old = "BAAI/bge-base-en-v1.5"

    if old not in text:
        return text, SKIPPED, "no mention of the old model"

    return (
        text.replace(old, name),
        OK,
        f"model name -> {name}",
    )


# ------------------------------------------------------------
# Orchestrator database patch


def patch_orch_db(text):

    if "ORCH_DB_PATH" in text:
        return text, SKIPPED, "ORCH_DB_PATH already supported"

    nl = newline_of(text)

    # Match:
    # DATABASE_URL = f"sqlite:///{BASE_DIR / 'chat.db'}"

    url_line = re.compile(
        r"""DATABASE_URL\s*=\s*f"sqlite:///\{BASE_DIR\s*/\s*'chat\.db'\}" """
    .rstrip())

    if not url_line.search(text):

        return (
            text,
            FAILED,
            "DATABASE_URL line not in expected format; edit manually",
        )

    replacement = (
        'DATABASE_PATH = Path(os.getenv("ORCH_DB_PATH", BASE_DIR / "chat.db"))'
        + nl
        + 'DATABASE_URL = f"sqlite:///{DATABASE_PATH}"'
    )

    new = url_line.sub(
        replacement,
        text,
        count=1,
    )

    # Add import os if missing.
    if not re.search(r"^import os\b", new, flags=re.M):

        new, count = re.subn(
            r"^(\ufeff?)(from pathlib import Path)",
            lambda m: m.group(1) + "import os" + nl + m.group(2),
            new,
            count=1,
            flags=re.M,
        )

        if not count:
            return (
                text,
                FAILED,
                "could not add import os; add it manually",
            )

    return (
        new,
        OK,
        "database path now comes from ORCH_DB_PATH",
    )


# ------------------------------------------------------------
# Diff display


def show_diff(rel, before, after):

    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=1,
    )

    for line in lines:
        print("    " + line.rstrip("\r\n"))


# ------------------------------------------------------------
# Python syntax validation
# Windows-safe: never use cfile="NUL" or "/dev/null".


def check_python_syntax(path: Path):

    with tempfile.TemporaryDirectory() as temp_dir:

        temp_pyc = Path(temp_dir) / "check.pyc"

        py_compile.compile(
            str(path),
            doraise=True,
            cfile=str(temp_pyc),
        )


# ------------------------------------------------------------
# Main runner


def main():

    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[0]
    )

    parser.add_argument(
        "--model",
        choices=MODELS,
        default="english",
        help="Granite model (default: english)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show changes without writing files",
    )

    parser.add_argument(
        "--skip-db-patch",
        action="store_true",
        help="skip database.py modification",
    )

    args = parser.parse_args()

    name, dim = MODELS[args.model]

    if not (ROOT / SETTINGS).exists():

        print(f"Cannot find {SETTINGS} under {ROOT}.")
        print("Place this script in the repository root.")
        return 2

    jobs = [
        (
            SETTINGS,
            lambda text: patch_settings(text, name, dim),
        ),
        (
            EMBEDDINGS,
            patch_embeddings,
        ),
        (
            README,
            lambda text: patch_readme(text, name),
        ),
    ]

    if not args.skip_db_patch:
        jobs.append(
            (
                ORCH_DB,
                patch_orch_db,
            )
        )

    print(f"Model: {name} ({dim} dimensions)")

    if args.dry_run:
        print("DRY RUN: nothing will be written.\n")

    results = []

    for rel, patch in jobs:

        path = ROOT / rel

        if not path.exists():

            if rel == README:
                continue

            results.append(
                (rel, FAILED, "file not found")
            )

            continue

        before = read(path)

        after, status, message = patch(before)

        results.append(
            (rel, status, message)
        )

        if status != OK:
            continue

        show_diff(rel, before, after)

        if args.dry_run:
            continue

        # Write the modified file.
        write(path, after)

        # Validate modified Python files.
        if rel.endswith(".py"):

            try:

                check_python_syntax(path)

            except Exception as error:

                results[-1] = (
                    rel,
                    FAILED,
                    f"compilation failed: {error}",
                )

                # Restore original file.
                write(path, before)

                print(f"\nRestored original file: {rel}")

    # --------------------------------------------------------
    # Summary

    print("\nSummary")

    width = max(
        (len(rel) for rel, _, _ in results),
        default=0,
    )

    for rel, status, message in results:

        print(
            f"  {rel:<{width}}  {status:<12}  {message}"
        )

    failed = any(
        status == FAILED
        for _, status, _ in results
    )

    if not failed:

        print("\nNext steps")

        print(
            "  1. Re-embed existing vectors because embeddings "
            "from different models cannot be compared."
        )

        print(
            "     cd backend/retriever && "
            "python reembed_collections.py"
        )

        print(
            "     Skip migration if this is a fresh Qdrant collection."
        )

        print(
            "\n  2. Retune RETRIEVAL_MIN_SIMILARITY "
            "using an environment variable."
        )

        print(
            "     RETRIEVAL_MIN_SIMILARITY=<new value>"
        )

        print(
            "     The previous 0.79 default was tuned for BGE."
        )

        print(
            "\n  3. Rebuild Docker images to download the new model."
        )

        if not args.skip_db_patch:

            print(
                "\n  4. Check render.yaml for "
                "ORCH_DB_PATH=/data/orchestrator.db."
            )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
