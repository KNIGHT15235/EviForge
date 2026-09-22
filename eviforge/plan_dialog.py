from __future__ import annotations

from enum import Enum
import json
import uuid

from rich.markup import escape

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Static


class PlanChoice(str, Enum):
    APPROVE = "approve"
    # Compatibility names no longer imply bypassing permission boundaries.
    YOLO = "approve"
    MANUAL = "approve"
    REJECT = "reject"
    FEEDBACK = "feedback"


_OPTIONS = [
    ("Approve these exact actions and scopes", PlanChoice.APPROVE),
    ("Reject this plan", PlanChoice.REJECT),
    ("Tell EviForge what to change", PlanChoice.FEEDBACK),
]


class InlinePlanWidget(Vertical, can_focus=True):
    """内联的计划审批组件，格式与 Go 版 TUI 保持一致。"""

    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "select", "Select", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("shift+tab", "approve_with_feedback", "Approve+Feedback", priority=True),
    ]

    class Responded(Message):


        def __init__(self, choice: PlanChoice, feedback: str = "", *, plan_id: str = "", content_hash: str = "", request_id: str = "") -> None:
            super().__init__()
            self.choice = choice
            self.feedback = feedback
            self.plan_id = plan_id
            self.content_hash = content_hash
            self.request_id = request_id

    def __init__(self, *, snapshot=None, ttl_seconds: float = 300, approval_details: str = "", **kwargs) -> None:
        super().__init__(id="plan-inline", **kwargs)
        self._cursor = 0
        self._input = ""
        self.snapshot = snapshot.as_dict() if hasattr(snapshot, "as_dict") else dict(snapshot or {})
        self.request_id = uuid.uuid4().hex
        self.ttl_seconds = ttl_seconds
        self.approval_details = approval_details


    def compose(self) -> ComposeResult:
        yield Static(self._build_content(), id="plan-content")

    def on_mount(self) -> None:
        self.focus()

    def _build_content(self) -> str:
        lines = [
            "\n [bold #875fff]EviForge has written up a plan and is ready to execute. "
            "Would you like to proceed?[/bold #875fff]\n"
        ]
        if self.snapshot:
            lines.extend([
                escape(self.snapshot.get("content", "")),
                "\nPlan: " + escape(self.snapshot.get("plan_id", "")),
                "SHA-256: " + escape(self.snapshot.get("content_hash", "")),
                f"Approval validity: {self.ttl_seconds:g} seconds from approval.",
                "Exact action manifest:\n" + escape(json.dumps(self.snapshot.get("actions", []), ensure_ascii=False, indent=2)),
            ])
            if any(action.get("opaque_process") or action.get("tool_name") == "Bash" for action in self.snapshot.get("actions", [])):
                lines.append("[bold yellow]argv approves process startup. Its internal file/network effects are not OS-isolated.[/bold yellow]")
            if not self.snapshot.get("actions"):
                lines.append("[yellow]No write, process, or network actions are authorized by this plan.[/yellow]")
        if self.approval_details:
            lines.append(escape(self.approval_details))
        for i, (label, _choice) in enumerate(_OPTIONS):
            if i == self._cursor:
                lines.append(f" [bold cyan]❯[/bold cyan] {i + 1}. [bold]{label}[/bold]")
            else:
                lines.append(f"   {i + 1}. [dim]{label}[/dim]")

        if self._cursor == 2:
            display = escape(self._input) if self._input else "[dim]Type feedback here...[/dim]"
            lines.append(f"      {display}█")
            lines.append("      [dim]shift+tab to request a revised plan[/dim]")

        return "\n".join(lines)

    def _refresh(self) -> None:
        self.query_one("#plan-content", Static).update(self._build_content())


    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._refresh()


    def action_cursor_down(self) -> None:
        if self._cursor < 2:
            self._cursor += 1
            self._refresh()

    def action_select(self) -> None:
        if self._cursor == 2 and self._input:
            self._respond(PlanChoice.FEEDBACK, self._input)
        elif self._cursor == 0:
            self._respond(PlanChoice.APPROVE)
        elif self._cursor == 1:
            self._respond(PlanChoice.REJECT)

    def _respond(self, choice: PlanChoice, feedback: str = "") -> None:
        self.post_message(self.Responded(choice, feedback, plan_id=self.snapshot.get("plan_id", ""),
            content_hash=self.snapshot.get("content_hash", ""), request_id=self.request_id))

    def action_cancel(self) -> None:
        self._respond(PlanChoice.REJECT)

    def action_approve_with_feedback(self) -> None:
        if self._cursor == 2 and self._input:
            self._respond(PlanChoice.FEEDBACK, self._input)


    def on_key(self, event) -> None:
        if self._cursor != 2:
            return
        key = event.key
        if key == "backspace":
            if self._input:
                self._input = self._input[:-1]
                self._refresh()
            event.stop()
        elif len(key) == 1 and key.isprintable():
            self._input += key
            self._refresh()
            event.stop()
