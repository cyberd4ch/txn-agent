from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AuditLog:
    """Append-only record of every decision. Optionally mirrored to a JSONL file."""

    def __init__(self, path: str | Path | None = None):
        self.events: list[dict[str, Any]] = []
        self.path = Path(path) if path else None

    def record(self, intent_id: str, event: str, **data: Any) -> None:
        entry = {"ts": datetime.now(timezone.utc).isoformat(),
                 "intent_id": intent_id, "event": event, **data}
        self.events.append(entry)
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
