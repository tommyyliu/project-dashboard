# Project dashboard

A local page showing every project in `~/PycharmProjects`: when you last touched
it, what exists only on this machine, and how much disk it uses.

```bash
cd ~/PycharmProjects/project-dashboard
python3 -m dashboard        # http://127.0.0.1:8765
```

It needs only Python, with no packages to install. Tests: `uv run pytest`.

## What the numbers mean

- **Last touched** is the latest of your own commit, an edit to a source file, or
  a Claude Code session. In a repo that belongs to someone else, upstream commits
  don't count. Ignored files, run output and tooling folders don't count either.
- **Stages** by days idle: Active up to 30, Cooling up to 90, Dormant up to 180,
  then Cold.
- **Only on this machine**: a red triangle means no copy of the history exists
  anywhere else. An orange dot means a backup exists but is behind.
- **Unpushed** compares against the last known state of the remote. The scan never
  fetches, so run `git fetch` in a repo if this looks wrong.
- **Worktrees**: a folder that is a git worktree of another project here shows only
  its own uncommitted files. Unpushed commits and stashes are shared, so the main
  repo's row reports them.
- **Disk** comes from `du`, which reports sizes before btrfs compression. `/home` uses
  zstd, so real use is lower and deleting data frees less than the number shown. Rebuildable means `.venv`, `target`, `node_modules` and
  `__pycache__`. uv hardlinks packages across projects, so deleting them all would
  free less than the total shown.

## Project dashboards

A project can serve its own dashboard. List its URL under `dashboards` in `config.json`
and its row links there. `vgo` serves one from `vgo/dashboard` on port 8766.

## Read-only snapshot

`python3 -m dashboard --snapshot snapshot.html` runs a fresh scan and writes the page
with that scan and your notes embedded. The committed `snapshot.html` is the copy that
gets published, and this repo is public, so everything in it is public too. It needs no server, so it can be published or
shared. It can't rescan, edit notes, open PyCharm, or link to project dashboards.

## Files

- `config.json`: the folder to scan, plus the emails and GitHub owners that count as you.
- `annotations.json`: your statuses, themes, next steps and notes. The page writes
  this file, and you can edit it by hand while the server is stopped. The themes and
  descriptions already in it are first guesses from reading each project.
- `.cache/scan.json`: the last scan.
- `previous_names` in `config.json`: renamed folders, so Claude history still matches.

The scan is read-only. The only thing the page does outside this folder is open a
project in PyCharm when you press the button. The server listens on localhost only.
