"""Runtime settings read from the Lambda environment."""

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    table_name: str
    allowed_hosts: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "Settings":
        hosts = os.environ.get("ALLOWED_HOSTS", "")
        return cls(
            table_name=os.environ["TABLE_NAME"],
            allowed_hosts=[h.strip() for h in hosts.split(",") if h.strip()],
        )
