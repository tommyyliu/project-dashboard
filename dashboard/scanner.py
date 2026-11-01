"""Read-only scan of every project folder.

Nothing here writes to a project. Git runs with GIT_OPTIONAL_LOCKS=0 so that
`git status` cannot refresh a repo's index, and it never fetches, so "unpushed"
means "not on the remote as of the last time this clone talked to it".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

DAY = 86400
ACTIVE_DAYS = 30
COOLING_DAYS = 90
DORMANT_DAYS = 180

MONTHS_SHOWN = 12

# Tooling and caches: never a sign that a person worked on the project.
SKIP_DIRS = {
    ".git", ".idea", ".vscode", ".claude", ".agents", ".worktrees", ".venv", "venv",
    "node_modules", "target", "dist", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".ipynb_checkpoints", ".wrangler", "test-results",
}
# Output a running job writes, which only matters outside git (.gitignore
# normally covers it in a repo).
OUTPUT_DIRS = {"runs", "logs", "data", "out", "wandb", "checkpoints", "results"}
REBUILDABLE_DIRS = {".venv", "venv", "node_modules", "target", "__pycache__"}

PLACEHOLDER_DESCRIPTIONS = {"add your description here", ""}
RESUME_DOC = re.compile(
    r"^(handoff|roadmap|todo|notes|plan|ideas|research|experiments|mission"
    r"|optimization_plan|kernel_agent_handoffs|phase2_priorities)\b.*\.md$",
    re.IGNORECASE,
)
STACK_MARKERS = [
    ("Cargo.toml", "Rust"),
    ("tsconfig.json", "TypeScript"),
    ("package.json", "JavaScript"),
    ("CMakeLists.txt", "C++"),
    ("pyproject.toml", "Python"),
    ("requirements.txt", "Python"),
]
OWNER_PATTERN = re.compile(r"github\.com[:/]+([^/]+)/")


@dataclass
class Config:
    root: Path
    my_emails: set[str] = field(default_factory=set)
    my_owners: set[str] = field(default_factory=set)
    claude_projects: Path = Path.home() / ".claude" / "projects"
    # Renamed folders: {current name: old name}. Claude's history keeps the old path.
    previous_names: dict[str, str] = field(default_factory=dict)
    # Projects with their own dashboard: {name: URL}.
    dashboards: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        raw = json.loads(path.read_text())
        return cls(
            root=Path(raw["root"]).expanduser(),
            my_emails={e.lower() for e in raw.get("my_emails", [])},
            my_owners={o.lower() for o in raw.get("my_github_owners", [])},
            previous_names=raw.get("previous_names", {}),
            dashboards=raw.get("dashboards", {}),
        )


def git(repo: Path, *args: str, timeout: int = 60) -> str | None:
    """Run git in `repo`; None means it failed or timed out."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, errors="replace",
            timeout=timeout, env=env, stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return done.stdout if done.returncode == 0 else None


def is_repo_root(path: Path) -> bool:
    return (path / ".git").exists()


def newest_of(path: Path, relpaths) -> tuple[float, str | None]:
    newest, newest_path = 0.0, None
    for rel in relpaths:
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        try:
            mtime = (path / rel).stat().st_mtime
        except OSError:
            continue
        if mtime > newest:
            newest, newest_path = mtime, rel
    return newest, newest_path


def walk_files(path: Path):
    """Source-looking files in a folder that isn't under git."""
    skip = SKIP_DIRS | OUTPUT_DIRS
    for current, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            yield os.path.relpath(os.path.join(current, name), path)


def has_content(path: Path) -> bool:
    return any(path.iterdir())


def parse_status(porcelain: str) -> dict:
    modified, untracked, files = 0, 0, []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        if line.startswith("??"):
            untracked += 1
        else:
            modified += 1
        files.append(line[3:])
    return {"modified": modified, "untracked": untracked, "files": files[:15]}


def month_key(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m")


def month_range(now: float, count: int = MONTHS_SHOWN) -> list[str]:
    day = datetime.fromtimestamp(now)
    year, month = day.year, day.month
    keys = []
    for _ in range(count):
        keys.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return keys[::-1]


def classify_ownership(remote: str | None, in_git: bool, my_commits: int, cfg: Config) -> str:
    """`mine`, `local` (no remote), `fork` (their repo, my commits), `clone` (their repo)."""
    if not in_git or not remote:
        return "local"
    owner = OWNER_PATTERN.search(remote)
    if owner and owner.group(1).lower() in cfg.my_owners:
        return "mine"
    return "fork" if my_commits else "clone"


def stage_for(days_idle: float | None) -> str:
    if days_idle is None:
        return "empty"
    if days_idle <= ACTIVE_DAYS:
        return "active"
    if days_idle <= COOLING_DAYS:
        return "cooling"
    if days_idle <= DORMANT_DAYS:
        return "dormant"
    return "cold"


def scan_git(path: Path, cfg: Config, now: float) -> dict:
    remote_out = git(path, "remote", "get-url", "origin")
    remote = remote_out.strip() if remote_out else None
    if remote is None:
        other = (git(path, "remote") or "").split()
        if other:
            remote = (git(path, "remote", "get-url", other[0]) or "").strip() or None

    # In someone else's repo, only my commits say anything about me.
    upstream_owned = bool(remote) and not (
        (m := OWNER_PATTERN.search(remote)) and m.group(1).lower() in cfg.my_owners
    )
    months: dict[str, int] = {}
    last_ts, last_subject, my_commits, total_commits = 0.0, None, 0, 0
    for line in (git(path, "log", "--all", "--format=%ct%x09%ae%x09%s") or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[0].isdigit():
            continue
        total_commits += 1
        if upstream_owned and parts[1].lower() not in cfg.my_emails:
            continue
        ts = float(parts[0])
        my_commits += 1
        months[month_key(ts)] = months.get(month_key(ts), 0) + 1
        if ts > last_ts:
            last_ts, last_subject = ts, parts[2]

    # --branches rather than --all: a stash is not an unpushed commit.
    unpushed = int((git(path, "rev-list", "--count", "--branches", "--not", "--remotes") or "0").strip() or 0)
    status = parse_status(git(path, "status", "--porcelain") or "")

    worktrees, worktree_dirty = 0, 0
    for line in (git(path, "worktree", "list", "--porcelain") or "").splitlines():
        if not line.startswith("worktree "):
            continue
        tree = Path(line.removeprefix("worktree "))
        if tree == path or not tree.exists():
            continue
        worktrees += 1
        worktree_dirty += len((git(tree, "status", "--porcelain") or "").splitlines())

    listed = (git(path, "ls-files", "-co", "--exclude-standard", "-z") or "").split("\0")
    file_ts, file_path = newest_of(path, [f for f in listed if f])

    return {
        "remote": remote,
        "branch": (git(path, "branch", "--show-current") or "").strip() or None,
        "branches": len((git(path, "for-each-ref", "refs/heads") or "").splitlines()),
        "stashes": len((git(path, "stash", "list") or "").splitlines()),
        "my_commits": my_commits,
        "total_commits": total_commits,
        "months": months,
        "last_commit_ts": last_ts or None,
        "last_commit_subject": last_subject,
        "unpushed": unpushed,
        "worktrees": worktrees,
        "worktree_dirty": worktree_dirty,
        **status,
        "file_ts": file_ts or None,
        "file_path": file_path,
    }


def claude_sessions(name: str, cfg: Config) -> tuple[int, float | None]:
    sessions = []
    for folder in filter(None, [name, cfg.previous_names.get(name)]):
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(cfg.root / folder))
        sessions += (cfg.claude_projects / slug).glob("*.jsonl")
    times = []
    for session in sessions:
        try:
            times.append(session.stat().st_mtime)
        except OSError:
            pass
    return len(times), max(times, default=None)


def disk_usage(path: Path) -> dict:
    """Disk use by this folder. Hardlinks shared with other projects count in each."""
    try:
        done = subprocess.run(
            ["du", "-B1", "-d", "2", str(path)], capture_output=True, text=True,
            errors="replace", timeout=300, stdin=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"size": None, "rebuildable": None, "largest": []}
    total, rebuildable, children = None, 0, []
    for line in done.stdout.splitlines():
        size_text, _, entry = line.partition("\t")
        if not size_text.isdigit():
            continue
        size, rel = int(size_text), Path(entry).relative_to(path).parts
        if not rel:
            total = size
        elif len(rel) == 1:
            children.append((size, rel[0]))
            if rel[0] in REBUILDABLE_DIRS:
                rebuildable += size
        elif len(rel) == 2 and rel[1] in REBUILDABLE_DIRS and rel[0] not in REBUILDABLE_DIRS:
            rebuildable += size
    children.sort(reverse=True)
    return {
        "size": total,
        "rebuildable": rebuildable,
        "largest": [{"name": n, "size": s} for s, n in children[:4]],
    }


def detect_stack(path: Path) -> str | None:
    subdirs = [
        c for c in sorted(path.iterdir())
        if c.is_dir() and c.name not in SKIP_DIRS and not c.name.startswith(".")
    ]
    for base in [path, *subdirs]:
        for marker, stack in STACK_MARKERS:
            if (base / marker).exists():
                return stack
    return "Python" if any(path.glob("*.py")) else None


def read_description(path: Path) -> str | None:
    pyproject = path / "pyproject.toml"
    if pyproject.exists():
        match = re.search(r'^description\s*=\s*"(.*)"', pyproject.read_text(errors="replace"), re.M)
        if match and match.group(1).strip().lower() not in PLACEHOLDER_DESCRIPTIONS:
            return match.group(1).strip()
    readme = path / "README.md"
    if not readme.exists():
        return None
    in_frontmatter = False
    for number, line in enumerate(readme.read_text(errors="replace").splitlines()):
        line = line.strip()
        if line == "---" and (number == 0 or in_frontmatter):
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter or len(line) < 20 or line[0] in "#![<|`*-_>":
            continue
        return line[:200]
    return None


def resume_docs(path: Path) -> list[str]:
    candidates = [p.name for p in path.iterdir() if p.is_file()]
    docs = path / "docs"
    if docs.is_dir():
        candidates += [f"docs/{p.name}" for p in docs.iterdir() if p.is_file()]
    return sorted(c for c in candidates if RESUME_DOC.match(Path(c).name))


def risks_for(project: dict) -> list[dict]:
    """Work that exists only on this machine, worst first.

    A risk's `kind` is what the UI filters on; `lost` means the whole history
    goes with the disk, as opposed to just the latest changes.
    """
    risks = []
    if project["empty"]:
        return risks
    if not project["in_git"]:
        return [{"kind": "not-in-git", "label": "Not in git", "lost": True}]
    commits = project["total_commits"]
    if not commits:
        risks.append({"kind": "no-commits", "label": "Never committed", "lost": True})
    elif not project["remote"]:
        risks.append({"kind": "no-remote", "label": "No remote", "lost": True})
    unpushed = project["unpushed"]
    if unpushed and project["remote"]:
        risks.append({"kind": "unpushed", "label": f"{unpushed} unpushed", "lost": False})
    dirty = project["modified"] + project["untracked"] + project["worktree_dirty"]
    if dirty and commits:
        risks.append({"kind": "uncommitted", "label": f"{dirty} uncommitted", "lost": False})
    if project["stashes"]:
        count = project["stashes"]
        risks.append({"kind": "stash", "label": f"{count} stash{'es' if count > 1 else ''}", "lost": False})
    return risks


def scan_project(path: Path, cfg: Config, now: float) -> dict:
    empty = not has_content(path)
    project: dict = {
        "name": path.name,
        "path": str(path),
        "empty": empty,
        "in_git": is_repo_root(path),
        "description": None,
        "stack": None,
        "docs": [],
        "remote": None, "branch": None, "branches": 0, "stashes": 0,
        "my_commits": 0, "total_commits": 0, "months": {}, "last_commit_ts": None, "last_commit_subject": None,
        "unpushed": 0, "worktrees": 0, "worktree_dirty": 0,
        "modified": 0, "untracked": 0, "files": [],
        "file_ts": None, "file_path": None,
    }
    if project["in_git"]:
        project.update(scan_git(path, cfg, now))
    elif not empty:
        project["file_ts"], project["file_path"] = newest_of(path, walk_files(path))
        project["file_ts"] = project["file_ts"] or None
    if not empty:
        project["description"] = read_description(path)
        project["stack"] = detect_stack(path)
        project["docs"] = resume_docs(path)

    project["claude_sessions"], project["claude_ts"] = claude_sessions(path.name, cfg)

    touches = [
        (project["last_commit_ts"], "commit"),
        (project["file_ts"], "file edit"),
        (project["claude_ts"], "Claude session"),
    ]
    touches = [t for t in touches if t[0]]
    last, via = max(touches, default=(None, None))
    project["last_touched"] = last
    project["last_touched_via"] = via
    project["days_idle"] = max(0, int((now - last) // DAY)) if last else None
    project["stage"] = "empty" if empty else stage_for(project["days_idle"])
    project["ownership"] = classify_ownership(
        project["remote"], project["in_git"], project["my_commits"], cfg
    )
    project.update(disk_usage(path))
    project["risks"] = risks_for(project)
    return project


def project_dirs(root: Path) -> list[Path]:
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name.lower(),
    )


def scan_all(cfg: Config, now: float | None = None) -> dict:
    now = now or time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        projects = list(pool.map(lambda p: scan_project(p, cfg, now), project_dirs(cfg.root)))
    return {
        "scanned_at": now,
        "months": month_range(now),
        "projects": projects,
        "thresholds": {"active": ACTIVE_DAYS, "cooling": COOLING_DAYS, "dormant": DORMANT_DAYS},
    }
