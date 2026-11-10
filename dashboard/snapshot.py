"""A read-only copy of the page with one scan and your notes embedded.

The result is a single HTML fragment with no server behind it: no rescan, no
note editing, no opening projects. It is meant to be published as an Artifact.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .scanner import Config, scan_all
from .server import ANNOTATIONS, PAGE

TITLE = "Project Dashboard Snapshot"


def embed(value) -> str:
    """JSON that is safe inside a <script> element."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def build(cfg: Config, annotations: Path = ANNOTATIONS, page: Path = PAGE) -> str:
    data = {
        "scan": scan_all(cfg),
        "annotations": json.loads(annotations.read_text()) if annotations.exists() else {},
        "dashboards": {},  # project dashboards are on localhost, so unreachable from a snapshot
        "scanning": False,
        "error": None,
    }
    html = page.read_text()
    style = re.search(r"<style>.*?</style>", html, re.S).group(0)
    body = re.search(r"<body>(.*)</body>", html, re.S).group(1)
    return (
        f"<title>{TITLE}</title>\n"
        '<meta name="claude:viewport-fit-sides" content="cover">\n'
        f"{style}\n"
        f"<script>window.SNAPSHOT = {embed(data)};</script>\n"
        f"{body.strip()}\n"
    )
