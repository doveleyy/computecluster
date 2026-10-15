"""Stable identities used while multi-user authorization is introduced."""

# Existing records and all submissions through the current single-owner token
# belong to this account.  It is deliberately stable across installations and
# migrations; human-readable account names may change later.
ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
ADMIN_USERNAME = "administrator"
