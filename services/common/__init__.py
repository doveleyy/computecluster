"""Mechanism shared by the application services: identity, web, SQLite.

Each service image copies this package at build time, so services share code
but never a process, a database, or a deploy.
"""
