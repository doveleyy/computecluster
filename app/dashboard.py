"""The dashboard and Job Desk HTTP surface, composed from its route groups."""

from fastapi import APIRouter

from app import (
    dashboard_account,
    dashboard_artifacts,
    dashboard_files,
    dashboard_jobs,
    dashboard_operator,
    dashboard_pages,
    dashboard_storage,
    dashboard_uploads,
)

ROUTE_GROUPS = (
    dashboard_pages,
    dashboard_account,
    dashboard_operator,
    dashboard_jobs,
    dashboard_uploads,
    dashboard_storage,
    dashboard_files,
    dashboard_artifacts,
)


def create_dashboard_router() -> APIRouter:
    router = APIRouter()
    for group in ROUTE_GROUPS:
        router.include_router(group.router)
    return router
