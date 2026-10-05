"""HTTP plumbing shared by the application services."""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response


def cache_versioned_assets(app: FastAPI, version: str) -> None:
    """Let browsers keep ``/static/*?v=<version>`` for a year.

    Pages link assets with the running version, so a versioned URL never
    changes content. Unversioned or stale-versioned requests keep the
    default revalidation.
    """

    @app.middleware("http")
    async def cache_versioned_assets(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        if (
            request.url.path.startswith("/static/")
            and request.query_params.get("v") == version
            and response.status_code == 200
        ):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
