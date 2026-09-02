import argparse
import hashlib
import os
import time
from pathlib import Path

from griot import common

SUPPORTED_EXTENSIONS = {
    ".py", ".md", ".js", ".ts", ".java", ".cs", ".php", ".cpp",
    ".go", ".rb", ".rs", ".scala", ".html", ".css", ".sol", ".sh"
}
IGNORE_DIRS = {
    "node_modules", "dist", "build", ".git", ".vscode", "__pycache__",
    ".venv", "venv", "vendor", ".next", "target", "coverage",
}


def discover_files(repo_path: Path) -> list[Path]:
    """Walks the tree pruning IGNORE_DIRS before descending into them — rglob+match
    would scan (and open) entire trees like node_modules before filtering."""
    found = []
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
        for filename in filenames:
            if Path(filename).suffix in SUPPORTED_EXTENSIONS:
                found.append(Path(dirpath) / filename)
    return found


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


def process_repository(repo_path: Path, repo_key: str | None = None) -> list[dict]:
    key = repo_key or repo_path.name
    print(f"\nProcessing repository: {repo_path.name}")
    documents = []
    filtered_files = discover_files(repo_path)

    if not filtered_files:
        print(f"No supported file found in {repo_path.name}.")
        return []

    from tqdm import tqdm
    for file_path in tqdm(filtered_files, desc=f"Reading {repo_path.name}"):
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            if not content.strip():
                continue
            rel_path = str(file_path.relative_to(repo_path))
            chunks = common.chunk_text(content)
            for i, chunk in enumerate(chunks):
                documents.append({
                    "id": f"{key}:code:{rel_path}:{i}",
                    "content": chunk,
                    "metadata": {
                        "source_type": "code",
                        "repo": repo_path.name,
                        "file_path": rel_path,
                        "chunk_index": i,
                    },
                })
        except Exception as e:
            tqdm.write(f"Error reading {file_path}: {e}")
    return documents


def main(argv=None):
    parser = argparse.ArgumentParser(description="Indexes source code from the repositories into the local RAG vector store.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--repo", help="Name of a single repository (dirname) from repos.json to index, instead of all of them.")
    group.add_argument("--path", help="Directory of an arbitrary repository to index directly, without going through repos.json.")
    parser.add_argument("--dry-run", action="store_true", help="Only counts how many chunks would need to be (re)embedded, without spending anything (no local CPU, no API cost).")
    args = parser.parse_args(argv)

    start_time = time.time()

    if args.path:
        # --path skips repos.json entirely: the rest of the flow already
        # operates on Path objects identical to the ones from repos.json, no change needed.
        repo_paths_str = [args.path]
    else:
        try:
            repo_paths_str = common.load_repos()
        except FileNotFoundError:
            print(f"Error: {common.REPOS_JSON_PATH} not found.")
            return

        if args.repo:
            repo_paths_str = [p for p in repo_paths_str if Path(p).name == args.repo]
            if not repo_paths_str:
                print(f"Error: no repo named '{args.repo}' in repos.json.")
                return

    all_documents = []
    for path_str in repo_paths_str:
        repo_path = Path(path_str)
        if repo_path.is_dir():
            repo_key = _repo_key_for_path(repo_path) if args.path else None
            all_documents.extend(process_repository(repo_path, repo_key=repo_key))
        else:
            print(f"WARNING: '{repo_path}' is not a valid directory.")

    if not all_documents:
        print("\nNo documents to index.")
        return

    if args.dry_run:
        pending, up_to_date = common.count_pending(all_documents)
        print(f"\n[dry-run] {pending} chunks would need to be (re)embedded, {up_to_date} are already up to date.")
        return

    indexed, skipped, failed = common.index_documents(all_documents)

    elapsed = time.time() - start_time
    print(f"\nIndexing completed in {elapsed:.2f}s.")
    print(f"Total: {indexed} chunks indexed, {skipped} unchanged (skipped), {failed} failed. Collection: {common.COLLECTION_NAME} ({common.QDRANT_PATH})")
    common.log_run_summary(
        script="index_code.py", repo=args.repo or args.path or "all",
        indexed=indexed, skipped=skipped, failed=failed,
        duration_seconds=round(elapsed, 2),
        spend_today_usd=common.get_spend_today(),
        # [user-requested] WHICH documents failed, not just how many —
        # the count alone forced a grep through griot.log to diagnose.
        failures=common.last_run_failures(),
    )


if __name__ == "__main__":
    main()
