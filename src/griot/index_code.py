import argparse
import hashlib
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

from griot import common
from griot.cli import PROFILE_FLAG, show_flags_read_by_griot

SUPPORTED_EXTENSIONS = {
    ".py", ".md", ".js", ".ts", ".java", ".cs", ".php", ".cpp",
    ".go", ".rb", ".rs", ".scala", ".html", ".css", ".sol", ".sh",
    ".tsx", ".jsx", ".mjs", ".cjs", ".mdx", ".sql", ".yml", ".yaml", ".tf", ".toml",
}
IGNORE_DIRS = {
    "node_modules", "dist", "build", ".git", ".vscode", "__pycache__",
    ".venv", "venv", "vendor", ".next", "target", "coverage",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".turbo", ".cache",
}
# Generated files that carry a supported extension: hundreds of chunks of noise
# each, and on a paid profile every one of them is embedded.
IGNORE_FILES = {"pnpm-lock.yaml"}
# A source file is far below this; what is above it is a dump, a bundle or a
# data file. Skipped with a printed note rather than read whole into memory
# and, on a paid profile, sent whole to the embedding API.
MAX_FILE_BYTES = 1_000_000


def _tracked_files(repo_path: Path) -> list[Path] | None:
    """What the repository does not ignore under repo_path: the files git
    tracks plus the new ones it would track (`--others --exclude-standard`
    honours .gitignore, .git/info/exclude and the user's global excludes).
    None when repo_path is not inside a git work tree.

    New files are included on purpose: in real repositories work that has
    not been added yet is a large share of the files (decision records,
    models, migrations), and "not added yet" says nothing about whether a
    file is private. Ignoring it does. `-z` keeps names with spaces, quotes
    or line breaks intact; a name can be listed twice (an intent-to-add
    entry, a merge conflict), hence the set."""
    try:
        listing = common.run_git(repo_path, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                                 timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if listing.returncode != 0:
        return None
    return [repo_path / name for name in sorted({name for name in listing.stdout.split("\0") if name})]


def _walked_files(repo_path: Path) -> list[Path]:
    """Every file under repo_path, pruning IGNORE_DIRS before descending into
    them: rglob+match would scan (and open) entire trees like node_modules
    before filtering. os.walk does not descend into symlinked directories."""
    found = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        found.extend(Path(dirpath) / filename for filename in filenames)
    return found


def _has_git_marker(repo_path: Path) -> bool:
    """A `.git` in repo_path or above it: the directory belongs to a work
    tree, whether or not git can currently read it."""
    resolved = repo_path.resolve()
    return any((directory / ".git").exists() for directory in (resolved, *resolved.parents))


def discover_files(repo_path: Path) -> list[Path]:
    """The files `griot index code` reads, sorted.

    In a git work tree: what the repository does not ignore (tracked files
    and new ones). A file the repository ignores is ignored here too, and
    that is where local settings and notes with secrets usually live; on a
    paid profile they would be sent to the embedding API. Outside git the
    tree is walked.

    In both cases, only regular files count. A symlinked file is never
    followed: its target can be anywhere on the machine, and a repository
    that ships `notes.md -> ~/.aws/credentials` would have it indexed.
    The directory on the way to a file is checked as well: git lists a file
    by name, and a directory that has since become a link would make that
    name reach outside the repository.
    Generated directories and files and anything over MAX_FILE_BYTES stay out."""
    candidates = _tracked_files(repo_path)
    if candidates is None:
        if _has_git_marker(repo_path):
            # Fail closed. Walking here would read exactly what the rule
            # keeps out: a broken HEAD, a timeout or an ownership refusal
            # must not turn "not the ignored files" into "every file".
            print(f"git could not list the files of {repo_path}, so nothing is read from it: walking it instead "
                  f"would index files its .gitignore keeps out. `git -C {repo_path} status` shows what is wrong.")
            return []
        candidates = _walked_files(repo_path)
    elif not candidates:
        # Not an error, but silence here reads as "indexed nothing for no
        # reason": a directory inside some other work tree (a home directory
        # kept in git, say) lands here with everything untracked.
        print(f"git lists no files under {repo_path}, so nothing is read from it: "
              f"in a git work tree, files the repository ignores are not indexed.")

    root = repo_path.resolve()
    inside = {}  # directory -> whether it really is inside the repository
    found, too_large = [], []
    for path in candidates:
        if any(part in IGNORE_DIRS for part in path.relative_to(repo_path).parts[:-1]):
            continue
        if path.parent not in inside:
            inside[path.parent] = _is_inside(path.parent, root)
        if not inside[path.parent]:
            continue
        if path.suffix not in SUPPORTED_EXTENSIONS or path.name in IGNORE_FILES or ".min." in path.name.lower():
            continue
        try:
            info = os.lstat(path)
        except OSError:
            continue  # tracked but deleted from disk, or gone since it was listed
        if not stat.S_ISREG(info.st_mode):
            continue  # a symlink (never followed), or a submodule's directory
        if info.st_size > MAX_FILE_BYTES:
            too_large.append(path)
            continue
        found.append(path)

    if too_large:
        shown = ", ".join(common.shown(str(p.relative_to(repo_path))) for p in sorted(too_large)[:5])
        more = f" and {len(too_large) - 5} more" if len(too_large) > 5 else ""
        print(f"Skipped {len(too_large)} file(s) larger than {MAX_FILE_BYTES // 1_000_000} MB in {repo_path.name}: {shown}{more}")
    return sorted(found)


def _is_inside(directory: Path, root: Path) -> bool:
    try:
        return directory.resolve().is_relative_to(root)
    except OSError:
        return False


def read_source(path: Path, root: Path) -> str | None:
    """The text of a discovered file, or None when it is no longer what
    discovery saw. Listing and reading are two moments: the open itself
    refuses a symlink (O_NOFOLLOW), and what was opened is checked again for
    kind and size, so a file swapped in between is not read."""
    if not _is_inside(path.parent, root):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8", errors="ignore") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        return f.read()


def _repo_key_for_path(repo_path: Path) -> str:
    """Only used when the repo comes from --path (outside repos.json
    curation): appends a short hash of the resolved absolute path to the
    basename, so two directories with the same final name in different
    locations don't collide in the natural id —
    stable_id() in common.py hashes this string into the Qdrant point id, so
    a collision here would make one repo's upsert silently overwrite the
    other's. Only affects the id: metadata['repo'] stays the raw basename
    (readable name for search/filtering), and the --repo/repos.json path
    never calls this, preserving the usual id so as not to break idempotency
    of already-indexed data."""
    digest = hashlib.md5(str(repo_path.resolve()).encode()).hexdigest()[:8]
    return f"{repo_path.name}-{digest}"


def process_repository(repo_path: Path, repo_key: str | None = None, problems: list | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    print(f"\nProcessing repository: {repo_path.name}")
    documents = []
    filtered_files = discover_files(repo_path)

    if not filtered_files:
        print(f"No supported file found in {repo_path.name}.")
        return []

    from tqdm import tqdm
    root = repo_path.resolve()
    for file_path in tqdm(filtered_files, desc=f"Reading {repo_path.name}"):
        try:
            content = read_source(file_path, root)
            if content is None:
                tqdm.write(f"Skipped {file_path}: it changed after it was listed.")
                if problems is not None:
                    problems.append(str(file_path))
                continue
            if not content.strip():
                continue
            rel_path = str(file_path.relative_to(repo_path))
            chunks = common.chunk_text(content, where=f"{repo_path.name}/{rel_path}")
            for i, chunk in enumerate(chunks):
                documents.append({
                    "id": f"{key}:code:{rel_path}:{i}",
                    "content": chunk,
                    "metadata": common.result_metadata({
                        "source_type": "code",
                        "repo": repo_path.name,
                        "file_path": rel_path,
                        "chunk_index": i,
                    }),
                })
        except Exception as e:
            tqdm.write(f"Error reading {file_path}: {e}")
            if problems is not None:
                problems.append(str(file_path))
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="griot index code",
        description="Indexes source code from the repositories into the local RAG vector store.")
    show_flags_read_by_griot(parser, PROFILE_FLAG)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many chunks would need to be (re)embedded, without spending anything (no local CPU, no API cost).")
    parser.add_argument("--prune", action="store_true", help="Remove stale points (their source is no longer there) even when they are more than half of what is indexed for a repository.")
    args = parser.parse_args(argv)

    start_time = time.time()

    if args.path:
        # --path skips repos.json entirely: the rest of the flow already
        # operates on Path objects identical to the ones from repos.json, no change needed.
        # Resolved: `--path .` must still give the repository its directory
        # name (Path(".").name is empty), in its points and in its ids.
        repo_paths_str = [str(Path(args.path).resolve())]
    else:
        try:
            repo_paths_str = common.load_repos()
        except FileNotFoundError:
            print(f"Error: {common.REPOS_JSON_PATH} not found.", file=sys.stderr)
            return 1

        if args.repo:
            repo_paths_str = [p for p in repo_paths_str if Path(p).name == args.repo]
            if not repo_paths_str:
                print(f"Error: no repo named '{args.repo}' in repos.json.", file=sys.stderr)
                return 1

    # One missing path among several is a warning (below). None of them
    # existing is the same failure as a name that matches nothing: the run
    # could not start, and saying "nothing to index" with exit status 0 would
    # let a script believe the index is up to date.
    if not repo_paths_str:
        print("Error: no repository is registered. Register one with `griot repos add <path>`.", file=sys.stderr)
        return 1
    if not any(Path(p).is_dir() for p in repo_paths_str):
        print(f"Error: none of the {len(repo_paths_str)} path(s) to index is a directory: "
              f"{', '.join(repo_paths_str[:3])}{' ...' if len(repo_paths_str) > 3 else ''}", file=sys.stderr)
        return 1

    all_documents = []
    # Repositories a file of which could not be read: their stale points are
    # left alone, or an unreadable file would be removed as if it were gone.
    incomplete = set()
    for path_str in repo_paths_str:
        repo_path = Path(path_str)
        if repo_path.is_dir():
            repo_key = _repo_key_for_path(repo_path) if args.path else None
            problems = []
            all_documents.extend(process_repository(repo_path, repo_key=repo_key, problems=problems))
            if problems:
                incomplete.add(repo_path.name)
        else:
            print(f"WARNING: '{repo_path}' is not a valid directory.")

    # What stale-point removal may act on (see common.prune_orphans for the fences).
    prune_scope = dict(source_type="code", repo_paths=repo_paths_str, used_path=bool(args.path),
                       incomplete=incomplete, force=args.prune)

    if not all_documents:
        print("\nNo documents to index.")
        return

    if args.dry_run:
        common.dry_run(all_documents, source="code", unit="chunks", prune_scope=prune_scope)
        return

    indexed, skipped, failed = common.index_documents(all_documents)
    redacted = common.report_redactions()
    pruned = common.prune_orphans(all_documents, failed=failed, **prune_scope)

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} chunks indexed, {skipped} unchanged (skipped), {failed} failed. Collection: {common.COLLECTION_NAME} ({common.QDRANT_PATH})")
    common.log_run_summary(
        script="index_code.py", repo=args.repo or args.path or "all", repo_paths=repo_paths_str,
        indexed=indexed, skipped=skipped, failed=failed, redacted=redacted, pruned=pruned,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    raise SystemExit(main())
