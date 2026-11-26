"""The chat turn as a LangGraph graph.

Only the wiring lives here. Every node function is in turn.py on purpose: the
tests replace names inside the `turn` module (stream_response, retrieve_messages,
doc_retrieval.* ...), and a node must see the replacement when it runs.

turn.py imports this file inside run_turn (not at the top) and passes its OWN
node functions to get_graph(). This file never does `import turn` at load
time, so the nodes always belong to the module whose run_turn is running (the
tests load their own copy of `turn` with fakes, and that copy must use its own
nodes; `sys.modules["turn"]` can point somewhere else by then).

    START -> check_replay --replay--> replay -> END
                  |fresh
                  v
    cut_old -> load_history -> plan_search --+--> retrieve_chat --+
                                             |                    +--> emit_context
                                             +--> retrieve_docs -+          |
                                                                            v
                                                          save_question -> generate -> END

The two searches run at the same time (they write different state keys, so
no reducer is needed) and emit_context waits for both.
"""

from langgraph.graph import END, START, StateGraph

# The "fresh" path, in order. The two searches in the middle run in parallel.
BEFORE_SEARCH = ["cut_old", "load_history", "plan_search"]
SEARCHES = ["retrieve_chat", "retrieve_docs"]
AFTER_SEARCH = ["emit_context", "save_question", "generate"]


def build(turn=None):
    """Build and compile the graph. No checkpointer: SQLite and Qdrant stay
    the only places where anything is saved.

    `turn` is anything that has the node functions, `TurnState` and
    `route_after_check` as attributes (default: the `turn` module on the
    import path)."""

    if turn is None:
        import turn

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

    for first, second in zip(BEFORE_SEARCH, BEFORE_SEARCH[1:]):
        g.add_edge(first, second)

    # Fan out: both searches start as soon as plan_search is done...
    for search in SEARCHES:
        g.add_edge(BEFORE_SEARCH[-1], search)

    # ...and emit_context starts only when BOTH have finished (a list of
    # sources means "wait for all of them").
    g.add_edge(SEARCHES, AFTER_SEARCH[0])

    for first, second in zip(AFTER_SEARCH, AFTER_SEARCH[1:]):
        g.add_edge(first, second)

    g.add_edge("generate", END)

    return g.compile()


# One compiled graph per `turn` module. Normally there is exactly one module,
# so the graph is built once and reused for every question.
_graphs: dict = {}


def get_graph(turn=None):
    """The compiled graph, built once and reused for every question."""

    if turn is None:
        import turn

    # The nodes' own module dict says which copy of turn.py they belong to
    key = id(turn.node_generate.__globals__)

    if key not in _graphs:
        _graphs[key] = (turn, build(turn))  # keep `turn` alive so its id is not reused

    return _graphs[key][1]