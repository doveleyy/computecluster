from dataclasses import dataclass
from enum import StrEnum
from html import escape
from pathlib import Path

from fastapi import Request

from app.accounts import AccountStore
from app.dashboard_auth import session_identity


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    MEMBER = "member"


EVERYONE = frozenset(Role)
ADMINISTRATOR_ONLY = frozenset({Role.ADMINISTRATOR})
MEMBER_ONLY = frozenset({Role.MEMBER})


@dataclass(frozen=True)
class PlatformApp:
    key: str
    label: str
    href: str
    roles: frozenset[Role]
    accent: str
    kicker: str = ""
    description: str = ""


BUILTIN_APPS: tuple[tuple[int, PlatformApp], ...] = (
    (0, PlatformApp("home", "Home", "/", EVERYONE, "#f3f3f3")),
    (
        40,
        PlatformApp(
            "jobs",
            "Job Desk",
            "/jobs-ui",
            MEMBER_ONLY,
            "#ff9f6e",
            "Compute and files",
            "Submit jobs, watch runs, and browse your files.",
        ),
    ),
    (
        50,
        PlatformApp(
            "dashboard",
            "Dashboard",
            "/dashboard",
            ADMINISTRATOR_ONLY,
            "#c9c9c9",
            "Control plane",
            "Host health, workers, every job, and storage.",
        ),
    ),
)

BUILTIN_KEYS = frozenset(app.key for _, app in BUILTIN_APPS)


def viewer_role(
    request: Request, account_store: AccountStore, session_cookie: str | None
) -> Role:
    """Administrator only for a valid admin dashboard session; everyone else is
    a member, including a visitor with no session at all."""
    identity = session_identity(request, account_store, session_cookie)
    if identity is not None and identity.is_admin:
        return Role.ADMINISTRATOR
    return Role.MEMBER


BAR_STYLE = Path(__file__).with_name("platform_bar.css").read_text().strip()


def render_bar(apps: tuple[PlatformApp, ...], current: str) -> str:
    """The script-free document every app frames at the top of its page."""
    items: list[str] = []
    shared_so_far = True
    # Shared apps first and one divider before the role-specific tail; registry
    # positions alone do not keep that grouping. The stable sort keeps position
    # order within each group.
    for app in sorted(apps, key=lambda app: app.roles != EVERYONE):
        current_attribute = ' aria-current="page"' if app.key == current else ""
        href = escape(app.href, quote=True)
        if app.key == "home":
            items.append(
                f'<li><a class="home" href="{href}"{current_attribute}>'
                'Home<span class="long">&nbsp;Platform</span></a></li>'
                '<li class="sep" aria-hidden="true"></li>'
            )
            continue
        if shared_so_far and app.roles != EVERYONE:
            items.append('<li class="sep" aria-hidden="true"></li>')
            shared_so_far = False
        items.append(
            f'<li><a href="{href}"{current_attribute} style="--c:{app.accent}">'
            f'<i aria-hidden="true"></i>{escape(app.label)}</a></li>'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<base target="_top"><title>Home Platform apps</title>'
        f"<style>{BAR_STYLE}</style></head><body>"
        '<nav aria-label="Home Platform apps"><div class="inner"><ul>'
        + "".join(items)
        + "</ul></div></nav></body></html>"
    )


def render_launcher_tiles(apps: tuple[PlatformApp, ...], pinned: frozenset[str]) -> str:
    """One link per app the role can open; the launcher itself is not a tile.
    Each registered app also gets a toggle for whether the viewer's bar shows
    it. The toggle sits beside the link, since a button cannot nest in one."""
    tiles: list[str] = []
    for index, app in enumerate((app for app in apps if app.key != "home"), 1):
        tile = (
            f'<a class="tile" href="{escape(app.href, quote=True)}"'
            f' style="--c:{app.accent}"><i aria-hidden="true"></i>'
            f'<div class="tile-copy"><div class="tile-kicker">{escape(app.kicker)}'
            f'</div><div class="tile-name">{escape(app.label)}</div>'
            f'<p class="tile-desc">{escape(app.description)}</p></div>'
            '<span class="tile-go" aria-hidden="true">→</span>'
            f'<span class="tile-key" aria-hidden="true">{index:02d}</span></a>'
        )
        if app.key not in BUILTIN_KEYS:
            pressed = "true" if app.key in pinned else "false"
            tile = (
                f'<div class="slot">{tile}<button class="pin" type="button"'
                f' data-app="{escape(app.key, quote=True)}" aria-pressed="{pressed}"'
                f' aria-label="Pin {escape(app.label, quote=True)} to the bar">'
                "Pin</button></div>"
            )
        tiles.append(tile)
    return "".join(tiles)
