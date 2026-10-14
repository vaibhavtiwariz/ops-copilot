"""Graph assembly.

Hub-and-spoke: every worker reports back to the supervisor, which owns the
plan and the transitions. Destinations come from Command(goto=...) returns,
annotated with Literal types so the graph validates them at compile time.
"""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from .gate import approval_gate, executor
from .state import AgentState
from .supervisor import finalize, planner, supervisor
from .workers import analyst, devops, triage


def build_graph(checkpointer: BaseCheckpointSaver | None = None):
    builder = StateGraph(AgentState)
    builder.add_node("supervisor", supervisor)
    builder.add_node("planner", planner)
    builder.add_node("analyst", analyst)
    builder.add_node("devops", devops)
    builder.add_node("triage", triage)
    builder.add_node("approval_gate", approval_gate)
    builder.add_node("executor", executor)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "supervisor")
    # Workers, gate and executor all return Commands; finalize funnels to END.
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


def runnable_config(thread_id: str):
    from ops_copilot.settings import get_settings

    return {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": get_settings().graph_recursion_limit,
    }
