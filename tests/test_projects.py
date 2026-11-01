import re

from dashboard.scanner import Config


def test_renamed_project_keeps_its_claude_history(tmp_path):
    from dashboard.scanner import claude_sessions

    claude = tmp_path / "claude"
    root = tmp_path / "root"
    old = claude / re.sub(r"[^A-Za-z0-9]", "-", str(root / "old"))
    old.mkdir(parents=True)
    (old / "s.jsonl").write_text("{}")
    cfg = Config(root=root, claude_projects=claude, previous_names={"new": "old"})
    assert claude_sessions("new", cfg)[0] == 1
