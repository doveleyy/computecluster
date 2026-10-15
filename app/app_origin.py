from dataclasses import dataclass
from uuid import UUID


class AppReplayConflict(ValueError):
    pass


@dataclass(frozen=True)
class AppOrigin:
    app_id: str
    owner_user_id: UUID
    installation_id: UUID
    key: str
    payload_sha256: str
    inputs_json: str
    reservation_id: UUID
