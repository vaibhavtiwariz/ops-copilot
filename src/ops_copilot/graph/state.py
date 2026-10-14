from typing import Annotated, Any, Literal

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel
from typing_extensions import TypedDict

TaskStatus = Literal["pending", "in_progress", "done", "failed", "rejected"]

WORKER_NAMES = ("analyst", "devops", "triage")


class Task(BaseModel):
    id: str
    description: str
    assignee: str
    status: TaskStatus = "pending"
    summary: str = ""


def merge_tasks(left: list[Task], right: list[Task]) -> list[Task]:
    """Right-hand updates win, matched by task id.

    A Task with id "__clear__" is a reset sentinel: the result is exactly the
    incoming list minus the sentinel. The supervisor uses this at the start of
    a new user turn so plans never leak across turns within one thread.
    """
    if any(task.id == "__clear__" for task in right):
        return [task for task in right if task.id != "__clear__"]
    by_id = {task.id: task for task in left}
    for task in right:
        by_id[task.id] = task
    return list(by_id.values())


CLEAR_TASKS = "__clear__"


class ApprovalDecision(BaseModel):
    approved: bool
    note: str = ""
    decided_by: str = "human"


class AgentState(TypedDict):
    # Full conversation transcript. Workers read a trimmed tail but the
    # checkpointer keeps everything.
    messages: Annotated[list[AnyMessage], add_messages]
    tasks: Annotated[list[Task], merge_tasks]
    # Set when a worker proposed a sensitive action; the gate turns this into
    # an interrupt() payload and clears it once resolved.
    awaiting: dict[str, Any] | None
    # Populated on fatal-but-contained failures so the UI can explain itself.
    error: str | None
