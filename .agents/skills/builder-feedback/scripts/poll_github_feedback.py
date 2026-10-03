#!/usr/bin/env python3
"""Read-only poller for issues and comments by a verified GitHub login."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


def run(*args: str) -> str:
    result = subprocess.run(args, text=True, capture_output=True)
    if result.returncode:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"command failed ({' '.join(args)}): {detail}")
    return result.stdout


def repo_from_git() -> str:
    remote = run("git", "remote", "get-url", "origin").strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?$", remote)
    if not match:
        raise RuntimeError(f"cannot determine GitHub owner/repo from origin: {remote}")
    return match.group(1)


def configured_author() -> str | None:
    env_login = os.environ.get("BUILDER_FEEDBACK_AUTHOR_LOGIN", "").strip()
    if env_login:
        return env_login
    result = subprocess.run(
        ["git", "config", "--get", "builder-feedback.author"],
        text=True,
        capture_output=True,
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def verified_author(explicit: str | None) -> str:
    requested = explicit or configured_author()
    if requested:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", requested):
            raise RuntimeError("author login must contain only letters, digits, or hyphens")
        # Resolve via GitHub so typos, deleted users, and noncanonical casing do not
        # silently become an unverified filter.
        data = json.loads(run("gh", "api", f"users/{requested}"))
        login = data.get("login")
        if not isinstance(login, str) or not login:
            raise RuntimeError(f"GitHub did not verify user login {requested!r}")
        return login
    raise RuntimeError(
        "no feedback author configured; set git config builder-feedback.author LOGIN "
        "or pass --author-login LOGIN"
    )


def api_json(endpoint: str) -> object:
    raw = run("gh", "api", "--paginate", "--slurp", endpoint)
    pages = json.loads(raw)
    if not isinstance(pages, list):
        raise RuntimeError(f"unexpected GitHub API response for {endpoint}")
    # With --slurp, each page is one array; accept an already flattened array too.
    if pages and all(isinstance(item, dict) for item in pages):
        return pages
    flattened: list[object] = []
    for page in pages:
        if isinstance(page, list):
            flattened.extend(page)
        else:
            flattened.append(page)
    return flattened


def default_state_path(issue: int) -> Path:
    git_path = Path(run("git", "rev-parse", "--git-path", f"builder-feedback/issue-{issue}.json").strip())
    if not git_path.is_absolute():
        git_path = Path.cwd() / git_path
    return git_path


def read_state(path: Path) -> dict[str, str]:
    try:
        state = json.loads(path.read_text())
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}


def write_state(path: Path, state: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temp.replace(path)


def poll(repo: str, issue: int, author: str, path: Path) -> None:
    prefix = f"repos/{repo}/issues/{issue}"
    issue_data = json.loads(run("gh", "api", prefix))
    creator = (issue_data.get("user") or {}).get("login", "")
    if issue_data.get("pull_request"):
        return
    trusted_issue = creator.casefold() == author.casefold()
    comments = api_json(prefix + "/comments?per_page=100")
    if not isinstance(comments, list):
        raise RuntimeError("GitHub returned an unexpected comments payload")

    state = read_state(path)
    changed: list[dict[str, object]] = []
    next_state = dict(state)
    issue_updated = str(issue_data.get("updated_at", ""))
    issue_fingerprint = hashlib.sha256(
        json.dumps([issue_data.get("title"), issue_data.get("body")], ensure_ascii=False).encode()
    ).hexdigest()
    issue_changed = trusted_issue and state.get("__issue__") != issue_fingerprint
    if trusted_issue:
        next_state["__issue__"] = issue_fingerprint
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        user = comment.get("user") or {}
        if user.get("login", "").casefold() != author.casefold():
            continue
        comment_id = str(comment.get("id", ""))
        updated = str(comment.get("updated_at", ""))
        if not comment_id or not updated:
            continue
        if comment_id not in state or state[comment_id] != updated:
            changed.append(comment)
            next_state[comment_id] = updated

    if issue_changed or changed:
        print(f"\n{repo} issue #{issue}: {issue_data.get('title', '(untitled)')}", flush=True)
        print(f"Issue: {issue_data.get('html_url', '')}", flush=True)
        if issue_changed:
            print(f"\n[Issue updated {issue_updated}]\n{issue_data.get('body') or '(empty issue body)'}", flush=True)
        for comment in changed:
            print(
                f"\n[{comment.get('updated_at')}] {author} — {comment.get('html_url', '')}\n"
                f"{comment.get('body') or '(empty comment)'}",
                flush=True,
            )
    elif not state:
        print(f"No comments by verified login {author} on {repo} issue #{issue}.", flush=True)
    write_state(path, next_state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("issue", type=int, nargs="?", help="GitHub issue number; omit to watch all issues")
    parser.add_argument("--repo", help="GitHub owner/repo (defaults to origin remote)")
    parser.add_argument("--author-login", help="Comment author login; verified through GitHub")
    parser.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--state-file", type=Path, help="override cursor file (default: under Git's private dir)")
    args = parser.parse_args()
    if args.issue is not None and args.issue <= 0:
        parser.error("issue must be a positive number")
    if args.interval < 1:
        parser.error("interval must be at least 1 second")
    try:
        repo = args.repo or repo_from_git()
        if not re.fullmatch(r"[^/]+/[^/]+", repo):
            parser.error("--repo must be OWNER/REPO")
        author = verified_author(args.author_login)
        if args.state_file and args.issue is None:
            parser.error("--state-file requires an issue number")
        scope = f"issue #{args.issue}" if args.issue else "all issues"
        print(f"Polling {repo} {scope} for feedback by verified login {author}.", flush=True)
        while True:
            if args.issue is not None:
                poll(repo, args.issue, author, args.state_file or default_state_path(args.issue))
            else:
                issues = api_json(f"repos/{repo}/issues?state=all&per_page=100")
                if not isinstance(issues, list):
                    raise RuntimeError("GitHub returned an unexpected issues payload")
                for item in issues:
                    if not isinstance(item, dict) or item.get("pull_request"):
                        continue
                    number = item.get("number")
                    if isinstance(number, int):
                        poll(repo, number, author, default_state_path(number))
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    except (OSError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"builder-feedback poller: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
