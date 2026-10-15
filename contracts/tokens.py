import os
from pathlib import Path


def load_api_token(
    default_file: Path | None = None,
    *,
    required: bool = False,
    explicit_file: Path | None = None,
) -> str | None:
    token = (
        None
        if explicit_file is not None
        else os.environ.get("HOME_PLATFORM_API_TOKEN") or None
    )
    configured_file = os.environ.get("HOME_PLATFORM_API_TOKEN_FILE")
    token_file = (
        explicit_file
        if explicit_file is not None
        else Path(configured_file)
        if configured_file
        else default_file
    )
    if token is None and token_file is not None:
        try:
            token = token_file.read_text().strip() or None
        except FileNotFoundError:
            if required:
                raise RuntimeError(
                    f"API token file does not exist: {token_file}"
                ) from None
    if required and token is None:
        raise RuntimeError("API token is required but empty")
    return token
