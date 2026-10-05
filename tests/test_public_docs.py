"""Public documentation carries design, never deployment identifiers.

AGENTS.md keeps hardware, vendors, share and account names, addresses,
machine paths, and rollout or version narration out of `docs/` and the root
README. This gate turns that rule into a failing test. Fix a hit by
rewriting the sentence in generic terms ("the control-plane host", "the NAS",
"a storage share"); widen `ALLOWED` only for a value that is itself part of
the public configuration contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Rule:
    term: str
    pattern: re.Pattern[str]
    fix: str


@dataclass(frozen=True)
class Allowed:
    path: str
    text: str
    reason: str


def rule(term: str, pattern: str, fix: str) -> Rule:
    return Rule(term, re.compile(pattern), fix)


RULES: tuple[Rule, ...] = (
    rule("Synology", r"(?i)synology", "say 'the NAS'"),
    rule("DSM", r"\bDSM\b", "say 'the NAS' or 'file-server ACLs'"),
    rule("Raspberry Pi", r"(?i)raspberry\s*pi", "say 'the control-plane host'"),
    rule("Pi", r"\bPi\b", "say 'the control-plane host'"),
    rule("microSD", r"(?i)\bmicro-?sd\b", "say 'the host's system disk'"),
    rule("SSD", r"\bSSDs?\b", "say 'local disk' or drop the hardware note"),
    rule("Samba", r"\bSamba\b", "say 'SMB' or 'the file server'"),
    rule("share name", r"\bHomeStorage\b", "say 'NAS storage' or 'the share'"),
    rule("share name", r"\bHomePlatform\b", "say 'the platform share'"),
    rule(
        "share name",
        r"`(?:Personal|Media)`|\b(?:Personal|Media) share\b",
        "say 'a personal share' or 'a media share'",
    ),
    rule("folder name", r"\bdownloads_(?:dump|sorted)\b", "say 'a dump folder'"),
    rule("tailnet name", r"(?<!example)\.ts\.net\b", "say 'the host's private name'"),
    rule("mDNS hostname", r"\b[\w-]+\.local\b", "use a placeholder URL"),
    rule(
        "IP address",
        r"(?<![\d.])(?!127\.0\.0\.1(?![\d.]))(?!0\.0\.0\.0(?![\d.]))"
        r"\d{1,3}(?:\.\d{1,3}){3}(?![\d.])",
        "use a placeholder; only 127.0.0.1 and 0.0.0.0 are generic",
    ),
    rule(
        "IANA timezone",
        r"\b(?:Africa|America|Asia|Atlantic|Australia|Europe|Indian|Pacific)"
        r"/[A-Z][A-Za-z_]+",
        "a timezone reveals the household's location; omit it",
    ),
    rule("machine path", r"/home/[a-z]", "use a placeholder such as <user>"),
    rule("machine path", r"/Users/[A-Za-z]", "use a placeholder such as <user>"),
    rule("machine path", r"/mnt/|/volume\d|/srv/", "name the logical area"),
    rule("machine path", r"(?i)\b[a-z]:\\users\\", "use a placeholder"),
    rule("rollout narration", r"(?i)\bcut ?over\b", "describe the design"),
    rule("rollout narration", r"(?i)\bretired\b", "drop the history"),
    rule("rollout narration", r"(?i)\bbackfill", "drop the migration history"),
    rule("rollout narration", r"(?i)\btransitional\b", "drop the status note"),
    rule("rollout narration", r"(?i)\brollout\b", "describe what the flag does"),
    rule("rollout narration", r"(?i)\bpilot\b", "describe the allowlist"),
    rule("acceptance result", r"(?i)\blive-verified\b", "keep results local"),
    rule(
        "version number",
        r"(?<![\w.])v?\d+\.\d+\.\d+(?![\d.])",
        "keep version narration in local/docs",
    ),
)

# Names that would leak if this tracked file listed them (host and account
# names) live in an ignored file, one `term<TAB>regex` per line.
PRIVATE_TERMS = ROOT / "local" / "public-docs-denylist.tsv"
if PRIVATE_TERMS.exists():
    RULES += tuple(
        rule(term, pattern, "use a generic name")
        for term, pattern in (
            line.split("\t", 1)
            for line in PRIVATE_TERMS.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
    )

ALLOWED = (
    Allowed(
        "docs/configuration.md",
        "`HOME_PLATFORM_SYNOLOGY_HOST`",
        "environment variable read by app/dashboard.py; its name is the contract",
    ),
    Allowed(
        "docs/configuration.md",
        "| `HOME_PLATFORM_API_URL` | `http://raspberrypi.local:8000` |",
        "worker default in worker/main.py load_settings",
    ),
    Allowed(
        "docs/configuration.md",
        "| `HOME_PLATFORM_STORAGE_DIR` | `/srv/home-platform/storage/nas` |",
        "control-plane default in app/config.py load_settings",
    ),
    Allowed(
        "docs/configuration.md",
        "| `HABIT_TRACKER_TIMEZONE` | `Asia/Singapore` |",
        "default in services/habit_tracker/main.py load_settings",
    ),
    Allowed(
        "docs/configuration.md",
        "| `WISHLIST_TIMEZONE` | `Asia/Singapore` |",
        "default in services/wishlist/main.py load_settings",
    ),
    Allowed(
        "docs/configuration.md",
        "| `TRANSPORT_TIMEZONE` | `Asia/Singapore` |",
        "default in services/transport/main.py load_settings",
    ),
)


def public_docs() -> list[Path]:
    return [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md"))]


def violations(relative: str, text: str) -> list[str]:
    allowed = [entry.text for entry in ALLOWED if entry.path == relative]
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        for entry in allowed:
            line = line.replace(entry, "")
        for check in RULES:
            match = check.pattern.search(line)
            if match:
                found.append(
                    f"{relative}:{number}: {check.term} {match.group(0)!r}; {check.fix}"
                )
    return found


def test_public_docs_hold_no_deployment_identifiers() -> None:
    found = [
        problem
        for path in public_docs()
        for problem in violations(
            path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")
        )
    ]
    assert not found, (
        "Public docs must carry design only (AGENTS.md, Documentation rules):\n"
        + "\n".join(found)
    )


def test_every_allowance_still_matches_its_file() -> None:
    stale = [
        f"{entry.path}: {entry.text!r}"
        for entry in ALLOWED
        if entry.text not in (ROOT / entry.path).read_text(encoding="utf-8")
    ]
    assert not stale, "Remove allowances that no longer match:\n" + "\n".join(stale)


@pytest.mark.parametrize(
    ("line", "term"),
    [
        ("Files reach a job through the Synology share.", "Synology"),
        ("The coordinator runs on the Pi.", "Pi"),
        ("Inputs are not copied to microSD staging.", "microSD"),
        ("The retired Samba unit and local SSD are gone.", "SSD"),
        ("HomeStorage/users/<id>/notes.csv", "share name"),
        ("A separate `Personal` share holds the library.", "share name"),
        ("Open https://host.tail1234.ts.net/habits", "tailnet name"),
        ("Point it at 192.168.1.20 instead.", "IP address"),
        ("Mounted at /mnt/example-share.", "machine path"),
        ("Enable only with the NAS cutover.", "rollout narration"),
        ("Existing records were backfilled.", "rollout narration"),
        ("Control plane 0.40.8 is deployed.", "version number"),
    ],
)
def test_gate_detects_a_known_violation(line: str, term: str) -> None:
    assert any(f": {term} " in problem for problem in violations("x.md", line))


@pytest.mark.parametrize(
    "line",
    [
        "Each backend binds to `127.0.0.1`, never `0.0.0.0`.",
        "The worker image is `home-platform-ml:0.1`.",
        "Placeholder control plane: https://pi.example.ts.net",
        "Cache lives in `~/.local/share/home-platform-worker`.",
        "Members see `Home`, `Shared` and `Artifacts`.",
    ],
)
def test_gate_accepts_generic_text(line: str) -> None:
    assert violations("x.md", line) == []
