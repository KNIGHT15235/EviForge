from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

from mewcode.tools.base import ToolCategory

if TYPE_CHECKING:
    from mewcode.tools.base import Tool


SideEffect = Literal[
    "none",
    "local_read",
    "local_write",
    "process",
    "network",
    "external_write",
    "unknown",
]
Idempotency = Literal["idempotent", "conditional", "non_idempotent", "unknown"]


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    """Policy metadata for a tool, independent from its implementation.

    Existing :class:`mewcode.tools.base.Tool` implementations do not need to be
    changed.  ``from_tool`` derives conservative defaults and also honours
    optional metadata that newer tools may expose as class attributes.
    """

    name: str
    description: str
    params_model: type[BaseModel]
    category: ToolCategory
    side_effect: SideEffect
    idempotency: Idempotency
    risk_tags: frozenset[str] = field(default_factory=frozenset)
    timeout_seconds: float | None = None
    path_fields: frozenset[str] = field(default_factory=frozenset)
    transport_kind: str | None = None
    transport_command: tuple[str, ...] = ()
    destination_hosts: tuple[str, ...] = ()
    supports_exact_argv: bool = False

    @classmethod
    def from_tool(cls, tool: Tool) -> ToolDescriptor:
        category = tool.category
        default_side_effect: SideEffect
        default_idempotency: Idempotency
        if category == "read":
            default_side_effect = "local_read"
            default_idempotency = "idempotent"
        elif category == "write":
            default_side_effect = "local_write"
            default_idempotency = "conditional"
        else:
            default_side_effect = "process"
            default_idempotency = "unknown"

        model_fields = tool.params_model.model_fields
        inferred_path_fields = {
            name
            for name in model_fields
            if name in _COMMON_PATH_FIELDS
            or name.endswith("_path")
            or name.endswith("_dir")
            or name.endswith("_directory")
        }
        declared_path_fields = getattr(tool, "path_fields", ())
        inferred_path_fields.update(str(name) for name in declared_path_fields)

        side_effect = getattr(tool, "side_effect", default_side_effect)
        idempotency = getattr(tool, "idempotency", default_idempotency)
        risk_tags = frozenset(str(tag).lower() for tag in getattr(tool, "risk_tags", ()))
        timeout = getattr(tool, "timeout_seconds", None)
        transport_kind = getattr(tool, "transport_kind", None)
        declared_command = getattr(tool, "transport_command", ())
        if isinstance(declared_command, (list, tuple)) and all(
            isinstance(part, str) for part in declared_command
        ):
            transport_command = tuple(declared_command)
        else:
            transport_command = ()
        destination_hosts = tuple(
            sorted(
                {
                    str(host).casefold().rstrip(".")
                    for host in getattr(tool, "destination_hosts", ())
                    if str(host).strip()
                }
            )
        )

        return cls(
            name=tool.name,
            description=tool.description,
            params_model=tool.params_model,
            category=category,
            side_effect=side_effect,
            idempotency=idempotency,
            risk_tags=risk_tags,
            timeout_seconds=float(timeout) if timeout is not None else None,
            path_fields=frozenset(inferred_path_fields),
            transport_kind=str(transport_kind) if transport_kind is not None else None,
            transport_command=transport_command,
            destination_hosts=destination_hosts,
            supports_exact_argv=bool(getattr(tool, "supports_exact_argv", False)),
        )


_COMMON_PATH_FIELDS = frozenset(
    {
        "path",
        "file",
        "file_path",
        "target",
        "target_path",
        "source",
        "source_path",
        "destination",
        "destination_path",
        "directory",
        "cwd",
        "work_dir",
        "workspace",
        "workspace_root",
        "output",
        "output_path",
    }
)
