"""Runtime settings read from the Lambda environment."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    table_name: str
    allowed_hosts: list[str] = field(default_factory=list)
    vector_bucket: str = ""
    text_index: str = ""
    photo_index: str = ""
    photo_bucket: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        hosts = os.environ.get("ALLOWED_HOSTS", "")
        return cls(
            table_name=os.environ["TABLE_NAME"],
            allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
            vector_bucket=os.environ.get("VECTOR_BUCKET", ""),
            text_index=os.environ.get("TEXT_INDEX", ""),
            photo_index=os.environ.get("PHOTO_INDEX", ""),
            photo_bucket=os.environ.get("PHOTO_BUCKET", ""),
        )
