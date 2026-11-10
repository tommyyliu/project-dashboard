import os
import subprocess
import time
from pathlib import Path

import pytest

from dashboard.scanner import (
    Config, classify_ownership, month_range, read_description, risks_for,
    scan_all, stage_for,
)

NOW = time.time()
ME = "me@example.com"
THEM = "them@example.com"


def sh(repo: Path, *args: str, email: str = ME):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": email,
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def commit(repo: Path, name: str, email: str = ME):
    (repo / name).write_text("x")
    sh(repo, "add", ".")
    sh(repo, "commit", "-m", f"add {name}", email=email)


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    sh(path, "init", "-b", "main")
    return path


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    claude = tmp_path / "claude"
    claude.mkdir()
    return Config(root=root, my_emails={ME}, my_owners={"me"}, claude_projects=claude)


def scan(cfg) -> dict:
    return {p["name"]: p for p in scan_all(cfg, NOW)["projects"]}


def kinds(project) -> list[str]:
    return [r["kind"] for r in project["risks"]]


def test_empty_folder_has_no_stage_or_risk(cfg):
    (cfg.root / "blank").mkdir()
    project = scan(cfg)["blank"]
    assert project["stage"] == "empty"
    assert project["risks"] == []


def test_empty_folder_stays_empty_even_with_a_claude_session(cfg):
    (cfg.root / "blank").mkdir()
    slug = str(cfg.root / "blank").replace("/", "-").replace("_", "-")
    (cfg.claude_projects / slug).mkdir()
    (cfg.claude_projects / slug / "s.jsonl").write_text("{}")
    assert scan(cfg)["blank"]["stage"] == "empty"


def test_folder_outside_git_is_flagged(cfg):
    folder = cfg.root / "loose"
    folder.mkdir()
    (folder / "main.py").write_text("x")
    project = scan(cfg)["loose"]
    assert kinds(project) == ["not-in-git"]
    assert project["last_touched_via"] == "file edit"


def test_output_dirs_do_not_count_as_activity_outside_git(cfg):
    folder = cfg.root / "loose"
    (folder / "runs").mkdir(parents=True)
    source = folder / "main.py"
    source.write_text("x")
    os.utime(source, (NOW - 100 * 86400, NOW - 100 * 86400))
    (folder / "runs" / "metrics.csv").write_text("x")
    assert scan(cfg)["loose"]["days_idle"] == 100


def test_repo_with_no_commits_is_flagged(cfg):
    repo = make_repo(cfg.root / "fresh")
    (repo / "a.py").write_text("x")
    project = scan(cfg)["fresh"]
    assert kinds(project) == ["no-commits"]
    assert project["untracked"] == 1


def test_local_repo_counts_every_commit_as_unbacked(cfg):
    repo = make_repo(cfg.root / "solo")
    commit(repo, "a")
    (repo / "b").write_text("x")
    project = scan(cfg)["solo"]
    assert project["ownership"] == "local"
    assert kinds(project) == ["no-remote", "uncommitted"]


def test_unpushed_counts_only_commits_missing_from_the_remote(cfg, tmp_path):
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "--bare", "-b", "main", str(origin))
    repo = make_repo(cfg.root / "synced")
    commit(repo, "a")
    sh(repo, "remote", "add", "origin", f"git@github.com:me/{origin}")
    sh(repo, "config", "remote.origin.pushurl", str(origin))
    sh(repo, "push", "origin", "main")
    commit(repo, "b")
    commit(repo, "c")
    project = scan(cfg)["synced"]
    assert project["ownership"] == "mine"
    assert project["unpushed"] == 2
    assert kinds(project) == ["unpushed"]


def test_upstream_commits_do_not_make_a_clone_look_active(cfg):
    repo = make_repo(cfg.root / "theirs")
    commit(repo, "a", email=THEM)
    sh(repo, "remote", "add", "origin", "https://github.com/them/theirs.git")
    project = scan(cfg)["theirs"]
    assert project["my_commits"] == 0
    assert project["last_commit_ts"] is None
    assert project["ownership"] == "clone"


def test_my_commit_in_their_repo_is_a_fork(cfg):
    repo = make_repo(cfg.root / "theirs")
    commit(repo, "a", email=THEM)
    commit(repo, "b", email=ME)
    sh(repo, "remote", "add", "origin", "https://github.com/them/theirs.git")
    project = scan(cfg)["theirs"]
    assert project["my_commits"] == 1
    assert project["last_commit_subject"] == "add b"
    assert project["ownership"] == "fork"


def test_stash_is_reported_and_not_counted_as_unpushed(cfg):
    repo = make_repo(cfg.root / "stashy")
    commit(repo, "a")
    (repo / "a").write_text("changed")
    sh(repo, "stash")
    project = scan(cfg)["stashy"]
    assert project["stashes"] == 1
    assert project["unpushed"] == 1


def test_scan_does_not_change_the_repo(cfg):
    repo = make_repo(cfg.root / "solo")
    commit(repo, "a")
    (repo / "a").write_text("edit")
    index = repo / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    scan(cfg)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_claude_session_counts_as_activity(cfg):
    folder = cfg.root / "my_proj"
    folder.mkdir()
    source = folder / "a.py"
    source.write_text("x")
    os.utime(source, (NOW - 50 * 86400, NOW - 50 * 86400))
    slug = str(cfg.root / "my_proj").replace("/", "-").replace("_", "-")
    (cfg.claude_projects / slug).mkdir()
    (cfg.claude_projects / slug / "s.jsonl").write_text("{}")
    project = scan(cfg)["my_proj"]
    assert project["last_touched_via"] == "Claude session"
    assert project["claude_sessions"] == 1


def test_rebuildable_includes_nested_dependency_folders(cfg):
    nested = cfg.root / "web" / "app" / "node_modules"
    nested.mkdir(parents=True)
    (nested / "lib.js").write_bytes(b"x" * 100_000)
    project = scan(cfg)["web"]
    assert project["rebuildable"] >= 100_000


@pytest.mark.parametrize("days,stage", [
    (None, "empty"), (0, "active"), (30, "active"), (31, "cooling"),
    (90, "cooling"), (91, "dormant"), (180, "dormant"), (181, "cold"),
])
def test_stage_boundaries(days, stage):
    assert stage_for(days) == stage


def test_month_range_crosses_a_year():
    now = time.mktime((2026, 2, 15, 12, 0, 0, 0, 0, -1))
    months = month_range(now, 4)
    assert months == ["2025-11", "2025-12", "2026-01", "2026-02"]


def test_description_skips_placeholder_badges_and_frontmatter(tmp_path):
    (tmp_path / "pyproject.toml").write_text('description = "Add your description here"\n')
    (tmp_path / "README.md").write_text(
        "---\ntitle: long enough to look like prose\n---\n# Name\n"
        "![badge](http://example.com/badge)\nA real sentence about the project.\n"
    )
    assert read_description(tmp_path) == "A real sentence about the project."


def test_upstream_clone_with_edits_is_uncommitted_not_never_committed():
    project = {
        "empty": False, "in_git": True, "my_commits": 0, "total_commits": 500, "remote": "x",
        "unpushed": 0, "modified": 2, "untracked": 0, "worktree_dirty": 0, "stashes": 0,
    }
    assert [r["kind"] for r in risks_for(project)] == ["uncommitted"]


def test_worktree_with_its_own_row_is_not_counted_twice(cfg):
    main = make_repo(cfg.root / "main")
    commit(main, "a")
    sh(main, "remote", "add", "origin", "git@github.com:me/main.git")
    sh(main, "worktree", "add", "-b", "side", str(cfg.root / "side"))
    commit(cfg.root / "side", "b")
    (cfg.root / "side" / "dirty").write_text("x")
    (main / "dirty").write_text("x")
    projects = scan(cfg)
    assert projects["side"]["worktree_of"] == "main"
    assert projects["side"]["unpushed"] == 0
    assert projects["side"]["untracked"] == 1
    assert projects["side"]["worktree_dirty"] == 0
    assert kinds(projects["side"]) == ["uncommitted"]
    assert projects["main"]["unpushed"] == 2
    assert projects["main"]["worktree_dirty"] == 0
    assert projects["main"]["worktrees"] == 1
