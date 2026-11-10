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


def test_snapshot_embeds_data_without_breaking_the_script(tmp_path):
    from dashboard.snapshot import build

    root = tmp_path / "root"
    (root / "evil</script>").mkdir(parents=True)
    (root / "evil</script>" / "a.py").write_text("x")
    notes = tmp_path / "notes.json"
    notes.write_text('{"evil</script>": {"note": "</script><b>"}}')
    html = build(Config(root=root, claude_projects=tmp_path), annotations=notes)
    assert html.count("</script>") == 2  # the data and the app, nothing from the notes
    assert "<!doctype" not in html.lower() and "<body>" not in html
    assert "window.SNAPSHOT" in html
