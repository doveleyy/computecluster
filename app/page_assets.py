"""Design tokens for the control plane's pages and the platform bar.

`tokens.css` is the one registry of colours, type, space and shape. Each page
and the bar carry a copy at the top of their single style block, so no page
declares a value of its own.
"""

from pathlib import Path

ASSETS = Path(__file__).parent
TOKENS = (ASSETS / "tokens.css").read_text().strip()


def read_page(name: str) -> str:
    """An HTML page from app/ with the tokens at the top of its style block."""
    return (ASSETS / name).read_text().replace("<style>", f"<style>\n{TOKENS}\n", 1)


def read_stylesheet(name: str) -> str:
    """A stylesheet from app/ with the tokens in front of it."""
    return f"{TOKENS}\n{(ASSETS / name).read_text().strip()}"
