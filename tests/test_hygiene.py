"""Privacy hygiene regression test for public GitHub safety."""

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("src", "tests", "config", "examples", "web")
FORBIDDEN_PATTERNS = [
    re.compile(r"\b192\.168\.\d+\.\d+\b"),
    re.compile(r"\b100\.(66|102|112)\.\d+\.\d+\b"),
    re.compile(r"\.ts\.net\b", re.IGNORECASE),
    re.compile(r"\b(cambridge|saugus|belmont)\b", re.IGNORECASE),
    re.compile(r"\balfred@", re.IGNORECASE),
]


def test_no_private_ips_or_personal_strings_in_tracked_code():
    violations = []
    this_file = Path(__file__).resolve()
    for d in SCAN_DIRS:
        base = REPO_ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.resolve() == this_file:
                continue
            if path.suffix.lower() not in (".py", ".yaml", ".yml", ".json", ".txt", ".md", ".html", ".css", ".js"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pat in FORBIDDEN_PATTERNS:
                if pat.search(text):
                    violations.append(f"{path.relative_to(REPO_ROOT)} matched {pat.pattern}")
    assert not violations, f"Privacy hygiene violations found:\n" + "\n".join(violations)
