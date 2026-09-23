from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.accounts import (
    AccountStore,
    ExternalIdentityConflictError,
    InvalidCurrentPasswordError,
    SessionIdentity,
    UserCreate,
    UserRole,
    decode_session,
    encode_session,
    hash_password,
    verify_password,
)
from app.database import Database


def test_password_hash_is_salted_and_verifiable() -> None:
    first = hash_password("a sufficiently long password")
    second = hash_password("a sufficiently long password")

    assert first != second
    assert "a sufficiently long password" not in first
    assert verify_password("a sufficiently long password", first)
    assert not verify_password("wrong password", first)


def test_signed_session_rejects_tampering_and_expiry() -> None:
    identity = SessionIdentity(
        id=UUID("00000000-0000-0000-0000-000000000123"),
        username="member",
        role=UserRole.MEMBER,
    )
    session = encode_session(identity, "secret", 200)

    assert decode_session(session, "secret", 100) == (identity.id, 1)
    assert decode_session(session + "x", "secret", 100) is None
    assert decode_session(session, "different", 100) is None
    assert decode_session(session, "secret", 201) is None


def test_member_can_be_authenticated_and_revoked(tmp_path) -> None:
    database = Database(tmp_path / "accounts.db")
    database.initialize()
    accounts = AccountStore(database)
    created = accounts.create(
        UserCreate(username="alice", password="correct horse battery staple")
    )

    authenticated = accounts.authenticate("alice", "correct horse battery staple")
    assert authenticated is not None
    assert authenticated.id == created.id
    assert accounts.authenticate("alice", "incorrect password") is None

    accounts.set_disabled(created.id, True)
    assert accounts.authenticate("alice", "correct horse battery staple") is None
    assert accounts.get_identity(created.id) is None
    assert datetime.now(UTC) >= created.created_at


def test_password_change_and_reset_revoke_existing_session(tmp_path) -> None:
    database = Database(tmp_path / "accounts.db")
    database.initialize()
    accounts = AccountStore(database)
    created = accounts.create(
        UserCreate(username="alice", password="correct horse battery staple")
    )
    original_version = accounts.session_version(created.id)

    with pytest.raises(InvalidCurrentPasswordError):
        accounts.change_password(created.id, "wrong password", "a new long password")

    accounts.change_password(
        created.id,
        "correct horse battery staple",
        "a new long password",
    )
    assert accounts.authenticate("alice", "correct horse battery staple") is None
    assert accounts.authenticate("alice", "a new long password") is not None
    assert accounts.get_identity(created.id, original_version) is None

    changed_version = accounts.session_version(created.id)
    accounts.reset_password(created.id, "administrator reset password")
    assert accounts.authenticate("alice", "a new long password") is None
    assert accounts.authenticate("alice", "administrator reset password") is not None
    assert accounts.get_identity(created.id, changed_version) is None


def test_external_identity_link_is_unique_and_resolves_existing_user(
    tmp_path,
) -> None:
    database = Database(tmp_path / "accounts.db")
    database.initialize()
    accounts = AccountStore(database)
    alice = accounts.create(
        UserCreate(username="alice", password="correct horse battery staple")
    )
    bob = accounts.create(
        UserCreate(username="bob", password="another correct long password")
    )

    linked = accounts.link_external_identity(
        alice.id, "tailscale", " Alice@Example.Test ", "Alice"
    )
    resolved = accounts.resolve_external_identity("tailscale", "alice@example.test")

    assert linked.subject == "alice@example.test"
    assert accounts.external_identity(alice.id, "tailscale") == linked
    assert resolved is not None
    assert resolved.id == alice.id

    with pytest.raises(ExternalIdentityConflictError):
        accounts.link_external_identity(
            bob.id, "tailscale", "alice@example.test", "Alice"
        )
    with pytest.raises(ExternalIdentityConflictError):
        accounts.link_external_identity(
            alice.id, "tailscale", "different@example.test", "Different"
        )

    accounts.set_disabled(alice.id, True)
    assert accounts.resolve_external_identity("tailscale", linked.subject) is None
    assert accounts.unlink_external_identity(alice.id, "tailscale")
    assert not accounts.unlink_external_identity(alice.id, "tailscale")
