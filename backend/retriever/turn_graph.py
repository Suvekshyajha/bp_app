"""The chat turn as a LangGraph graph.

Only the wiring lives here. Every node function is in turn.py on purpose: the
tests replace names inside the `turn` module (stream_response, retrieve_messages,
doc_retrieval.* ...), and a node must see the replacement when it runs.

turn.py imports this file inside run_turn (not at the top), so the two files
never import each other while loading.

    START -> check_replay --replay--> replay -> END
                  |fresh
                  v
    cut_old -> load_history -> plan_search -> retrieve_chat -> retrieve_docs
            -> emit_context -> save_question -> generate -> END
"""

from langgraph.graph import END, START, StateGraph

import turn

# Straight line of the "fresh" path, in order
FRESH_PATH = [
    "cut_old",
    "load_history",
    "plan_search",
    "retrieve_chat",
    "retrieve_docs",
    "emit_context",
    "save_question",
    "generate",
]


def build():
    """Build and compile the graph. No checkpointer: SQLite and Qdrant stay
    the only places where anything is saved."""

    g = StateGraph(turn.TurnState)

    g.add_node("check_replay", turn.node_check_replay)
    g.add_node("replay", turn.node_replay)
    g.add_node("cut_old", turn.node_cut_old)
    g.add_node("load_history", turn.node_load_history)
    g.add_node("plan_search", turn.node_plan_search)
    g.add_node("retrieve_chat", turn.node_retrieve_chat)
    g.add_node("retrieve_docs", turn.node_retrieve_docs)
    g.add_node("emit_context", turn.node_emit_context)
    g.add_node("save_question", turn.node_save_question)
    g.add_node("generate", turn.node_generate)

    g.add_edge(START, "check_replay")
    g.add_conditional_edges(
        "check_replay",
        turn.route_after_check,
        {"replay": "replay", "fresh": "cut_old"},
    )
    g.add_edge("replay", END)

    for first, second in zip(FRESH_PATH, FRESH_PATH[1:]):
        g.add_edge(first, second)

    g.add_edge("generate", END)

    return g.compile()


_graph = None


def get_graph():
    """The compiled graph, built once and reused for every question."""

    global _graph

    if _graph is None:
        _graph = build()

    return _graph