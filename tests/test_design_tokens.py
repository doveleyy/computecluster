"""The control plane's pages take every value from docs/design-system.md.

`app/tokens.css` is the registry. Each surface below (a page's one style
block and script, or the platform bar's stylesheet) fails here when a rule
sets spacing, type size, radius or letter spacing with a raw value, writes a
colour literal, queries a width that is not a breakpoint, reads a token nobody
declared, or redeclares a token. A custom property counts as defined locally
when any rule in the same surface declares it.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app import dashboard_apps, dashboard_pages, platform_apps
from app.page_assets import TOKENS

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
SURFACES: list[str] = ["app/platform_bar.css", "app/apps.html"]
BREAKPOINTS = {840, 600, 380}
ACCENTS = {f"var(--{name})" for name in ("cyan", "green", "violet", "yellow", "red")}
LENGTH = r"-?(?:\d+\.?\d*|\.\d+)(?:px|r?em|vw|vh|vmin|vmax|[ds]?vh|[ds]?vw|ch|pt)\b"
SPACING = re.compile(
    r"^(?:padding|margin)(?:-(?:top|right|bottom|left|block|inline)"
    r"(?:-(?:start|end))?)?$|^(?:gap|row-gap|column-gap|inset"
    r"(?:-(?:block|inline)(?:-(?:start|end))?)?|top|right|bottom|left)$"
)
RADIUS = re.compile(
    r"^border(?:-(?:top|bottom|start|end)-(?:left|right|start|end))?-radius$"
)
COLOUR_LITERAL = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\b(?:rgba?|hsla?|hwb|oklch|oklab|lab|lch)\("
)
SCRIPT_COLOUR = re.compile(
    r"""(["'`])#[0-9a-fA-F]{3,8}\1|\b(?:rgba?|hsla?|hwb|oklch|oklab|lab|lch)\("""
)
SIZE_STEP = re.compile(r"var\(--text-(?:[1-9]|10)\)")
TRACKING = re.compile(r"var\(--track-(?:caps|nav|display)\)")
RADIUS_VALUE = re.compile(r"var\(--radius-(?:[123]|round)\)")


@dataclass
class Rule:
    prelude: str
    media: list[str]
    declarations: list[tuple[str, str]] = field(default_factory=list)


def rules(css: str) -> Iterator[Rule]:
    """Style rules in source order, each with the media queries around it."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    stack: list[Rule] = []
    found: list[Rule] = []
    buffer, parens, quote = "", 0, ""

    def declaration() -> None:
        name, colon, value = buffer.partition(":")
        if stack and colon:
            value = re.sub(r"\s*!important\s*$", "", value.strip())
            stack[-1].declarations.append((name.strip().lower(), value))

    for char in css:
        if quote:
            buffer += char
            quote = "" if char == quote else quote
        elif char in "\"'":
            buffer += char
            quote = char
        elif char in "(":
            parens += 1
            buffer += char
        elif char == ")":
            parens -= 1
            buffer += char
        elif parens == 0 and char == "{":
            media = [
                item.prelude for item in stack if item.prelude.startswith("@media")
            ]
            stack.append(Rule(" ".join(buffer.split()), media))
            found.append(stack[-1])
            buffer = ""
        elif parens == 0 and char == ";":
            declaration()
            buffer = ""
        elif parens == 0 and char == "}":
            declaration()
            buffer = ""
            stack.pop()
        else:
            buffer += char
    yield from found


def top_level(value: str, separators: str = " ") -> list[str]:
    """Split a CSS value on separators that sit outside parentheses."""
    parts, current, depth = [], "", 0
    for char in value:
        depth += char == "("
        depth -= char == ")"
        if depth == 0 and char in separators:
            if current:
                parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current] if current else parts


def declared_tokens() -> dict[str, str]:
    """Every `--name: value` in tokens.css."""
    return dict(
        (name, value)
        for rule in rules(TOKENS)
        for name, value in rule.declarations
        if name.startswith("--")
    )


def font_problems(prop: str, value: str) -> Iterator[str]:
    parts = top_level(value)
    if prop == "font-size":
        sizes = [value]
    elif prop == "font":
        if value in {"inherit", "initial"}:
            return
        sizes = [
            part.split("/")[0]
            for part in parts
            if not re.fullmatch(r"[1-9]00|normal|italic|oblique|small-caps", part)
        ][:1]
        for part in parts:
            if re.fullmatch(r"[1-9]00", part) and part not in {"400", "500", "600"}:
                yield f"font weight {part} is outside 400, 500 and 600"
    else:
        sizes = []
    for size in sizes:
        if size not in {"inherit"} and not SIZE_STEP.fullmatch(size):
            yield f"{prop} {size} is not a --text step"
    if prop == "font-weight" and value not in {
        "400",
        "500",
        "600",
        "normal",
        "inherit",
    }:
        yield f"font weight {value} is outside 400, 500 and 600"


def problems(rule: Rule, defined: set[str], declared: dict[str, str]) -> Iterator[str]:
    for query in rule.media:
        for width in re.findall(r"(?:min|max)-width\s*:\s*([\d.]+)px", query):
            if float(width) not in BREAKPOINTS:
                yield f"media query {width}px is not a breakpoint (840, 600, 380)"
        for width in re.findall(r"\bwidth\s*[<>]=?\s*([\d.]+)px", query):
            if float(width) not in BREAKPOINTS:
                yield f"media query {width}px is not a breakpoint (840, 600, 380)"
    for prop, value in rule.declarations:
        bare = re.sub(r"(?:var|env)\([^()]*(?:\([^()]*\)[^()]*)*\)", "", value)
        if prop.startswith("--"):
            if prop in declared and prop != "--accent":
                yield f"{prop} redeclares a token"
            if prop == "--accent" and value not in ACCENTS:
                yield f"--accent is {value}, not an accent token"
        if SPACING.match(prop):
            for length in re.findall(LENGTH, bare):
                yield f"{prop}: {length} is a raw length; use a --space token"
        if RADIUS.match(prop):
            leftover = RADIUS_VALUE.sub("", value)
            if (
                leftover.replace("0", "")
                .replace("/", "")
                .replace("inherit", "")
                .strip()
            ):
                yield f"{prop}: {value} is not a --radius token"
        if prop == "letter-spacing" and not (
            TRACKING.fullmatch(value) or value in {"0", "normal", "inherit"}
        ):
            yield f"letter-spacing: {value} is not a --track token"
        if prop in {"box-shadow", "text-shadow"} and value != "none":
            yield f"{prop}: {value} casts a shadow or glow"
        if prop in {"backdrop-filter", "-webkit-backdrop-filter"}:
            yield f"{prop} is glass"
        yield from font_problems(prop, value)
        if COLOUR_LITERAL.search(value):
            yield f"{prop}: {value} writes a colour literal"
        for name in re.findall(r"var\(\s*(--[\w-]+)", value):
            if name not in declared and name not in defined:
                yield f"{prop} reads {name}, which nothing declares"


def surface_text(path: str) -> tuple[list[str], list[str]]:
    """The stylesheets and the scripts a surface carries."""
    text = (ROOT / path).read_text()
    if path.endswith(".css"):
        return [text], []
    styles = re.findall(r"<style[^>]*>(.*?)</style>", text, flags=re.S)
    scripts = re.findall(
        r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", text, flags=re.S
    )
    return styles, scripts


def check(styles: list[str], scripts: list[str]) -> list[str]:
    declared = declared_tokens()
    parsed = [rule for css in styles for rule in rules(css)]
    defined = {
        name
        for rule in parsed
        for name, _ in rule.declarations
        if name.startswith("--")
    }
    found = []
    for rule in parsed:
        found += [
            f"{rule.prelude}: {issue}" for issue in problems(rule, defined, declared)
        ]
    for script in scripts:
        for match in SCRIPT_COLOUR.finditer(script):
            line = script[: match.start()].count("\n") + 1
            found.append(f"script line {line}: colour literal {match.group(0)}")
        for name in re.findall(r"""getPropertyValue\(\s*["'`](--[\w-]+)""", script):
            if name not in declared and name not in defined:
                found.append(f"script reads {name}, which nothing declares")
    return found


@pytest.mark.parametrize("path", SURFACES)
def test_surface_takes_every_value_from_the_tokens(path: str) -> None:
    styles, scripts = surface_text(path)
    if path.endswith(".html"):
        assert len(styles) == 1, f"{len(styles)} style blocks; a page carries one"
    assert check(styles, scripts) == []


def test_checker_names_each_kind_of_violation() -> None:
    sample = (
        ".a { padding: 13px; margin: 0 var(--space-2);"
        " gap: calc(var(--space-1) + 3px) }"
        ".b { font-size: 13px; border-radius: 50%; letter-spacing: .1em; color: #abc }"
        ".c { font: 700 var(--text-4)/1.5 var(--sans); background: rgba(0, 0, 0, .5) }"
        ".d { color: var(--nothing); box-shadow: 0 0 8px var(--cyan) }"
        ".e { --bg: #000; --accent: var(--text) }"
        "@media (max-width: 640px) { .f { top: 5px; font: var(--text-3) var(--mono) } }"
    )
    script = "const c = '#dfeee4'; ctx.fillStyle = `rgba(1,2,3,${a})`;"
    found = check([sample], [script])
    assert [item.split(": ", 1)[1] for item in found] == [
        "padding: 13px is a raw length; use a --space token",
        "gap: 3px is a raw length; use a --space token",
        "font-size 13px is not a --text step",
        "border-radius: 50% is not a --radius token",
        "letter-spacing: .1em is not a --track token",
        "color: #abc writes a colour literal",
        "font weight 700 is outside 400, 500 and 600",
        "background: rgba(0, 0, 0, .5) writes a colour literal",
        "color reads --nothing, which nothing declares",
        "box-shadow: 0 0 8px var(--cyan) casts a shadow or glow",
        "--bg redeclares a token",
        "--bg: #000 writes a colour literal",
        "--accent is var(--text), not an accent token",
        "media query 640px is not a breakpoint (840, 600, 380)",
        "top: 5px is a raw length; use a --space token",
        "colour literal '#dfeee4'",
        "colour literal rgba(",
    ]


def doc_tables() -> dict[str, str]:
    """Each `--token` and its value from the tables in the design document."""
    rows = re.findall(
        r"^\|[^|]*\|\s*`(--[\w-]+)`\s*\|\s*`([^`]+)`\s*\|",
        (ROOT / "docs/design-system.md").read_text(),
        flags=re.M,
    )
    return dict(rows)


def test_tokens_css_declares_exactly_the_documented_tokens() -> None:
    documented = doc_tables()
    declared = declared_tokens()
    declared.pop("color-scheme", None)
    assert len(documented) > 50
    assert declared == documented


def test_every_served_page_carries_the_tokens_once() -> None:
    served = {
        "dashboard": dashboard_pages.DASHBOARD_HTML,
        "jobs": dashboard_pages.JOBS_HTML,
        "home": dashboard_pages.HOME_HTML,
        "apps": dashboard_apps.APPS_HTML,
        "bar": platform_apps.render_bar((), ""),
    }
    for name, page in served.items():
        assert page.count(TOKENS) == 1, name
        assert page.index(TOKENS) < page.index("</style>"), name
