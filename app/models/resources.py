from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from uuid import uuid4


class ResourceType(StrEnum):
    FILE = "file"
    TOOL_RESULT = "tool_result"
    RETRIEVED_DOCUMENT = "retrieved_result"
    ARTIFACT = "artifact"
    MEMORY = "memory"

@dataclass(frozen = True, slots = True)
class ResourceRef:
    resource_id: str
    ref: str
    resource_type: ResourceType
    loaded: bool
    summary: str | None
    selected_context: str | None
    source: str | None
    created_at: datetime
    updated_at: datetime

class Resources:
    def __init__(self) -> None:
        self._refs: dict[str, ResourceRef] = {}

    def register(
        self, ref: str, resource_type: ResourceType, source: str | None = None
    ) -> str:
        if not isinstance(ref, str) or not ref.strip():
            raise ValueError("ref must be a non-empty string")

        ref = ref.strip()

        if not isinstance(resource_type, ResourceType):
            raise TypeError("resource_type must be a ResourceType")

        if source is not None:
            if not isinstance(source, str) or not source.strip():
                raise ValueError("source must be a non-empty string or None")

            source = source.strip()

        for resource in self._refs.values():
            if resource.ref == ref:
                raise ValueError(
                    f"Resource ref '{ref}' has already been registered"
                )

        now = datetime.now(timezone.utc)

        resource = ResourceRef(
            resource_id = str(uuid4()),
            ref = ref,
            resource_type = resource_type,
            loaded = False,
            summary = None,
            selected_context = None,
            source = source,
            created_at = now,
            updated_at = now,
        )

        self._refs[resource.resource_id] = resource

        return resource.resource_id

    def get(self, resource_id: str) -> ResourceRef:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")

        resource_id = resource_id.strip()

        if resource_id not in self._refs:
            raise KeyError(
                f"Resource {resource_id} does not exist"
            )
        return self._refs[resource_id]

    def remove(self, resource_id: str) -> ResourceRef:
        resource = self.get(resource_id)
        return self._refs.pop(resource.resource_id)

    def mark_loaded(self, resource_id: str, summary: str | None = None) -> None:
        """Mark a resource loaded; repeated loading raises RuntimeError."""
        target_source = self.get(resource_id)

        if target_source.loaded:
            raise RuntimeError(
                f"Resource {resource_id} is already loaded"
            )
        if summary is not None:
            if not isinstance(summary, str) or not summary.strip():
                raise ValueError(
                    "summary must be a non-empty string or None"
                )

            summary = summary.strip()
        now = datetime.now(timezone.utc)

        loaded_source = replace(
            target_source,
            loaded = True,
            summary = summary,
            updated_at = now
        )
        self._refs[target_source.resource_id] = loaded_source

    def select_context(self, resource_id: str, context: str) -> None:
        """Select context for a registered resource, even if not yet loaded."""
        resource = self.get(resource_id)

        if not isinstance(context, str) or not context.strip():
            raise ValueError("context must be a non-empty string")

        self._refs[resource.resource_id] = replace(
            resource,
            selected_context = context.strip(),
            updated_at = datetime.now(timezone.utc),
        )

    def clear_selected_context(self, resource_id: str) -> None:
        """Clear a resource's selected context, if present."""
        resource = self.get(resource_id)
        if resource.selected_context is None:
            return

        self._refs[resource.resource_id] = replace(
            resource,
            selected_context = None,
            updated_at = datetime.now(timezone.utc),
        )

    def clear_all_selected_contexts(self) -> int:
        """Clear selected contexts and return the number of changed resources."""
        selected = self.list_selected()
        now = datetime.now(timezone.utc)
        for resource in selected:
            self._refs[resource.resource_id] = replace(
                resource,
                selected_context = None,
                updated_at = now,
            )
        return len(selected)

    def list_all(self) -> list[ResourceRef]:
        """Return all resources in registration order."""
        return list(self._refs.values())

    def list_loaded(self) -> list[ResourceRef]:
        """Return loaded resources in registration order."""
        return [resource for resource in self._refs.values() if resource.loaded]

    def list_selected(self) -> list[ResourceRef]:
        """Return resources with selected context in registration order."""
        return [
            resource for resource in self._refs.values()
            if resource.selected_context is not None
        ]
