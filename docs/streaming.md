# Streaming: how an answer reaches the browser word by word

The answer is streamed **token by token** from Groq to the browser. Each token must reach the gRPC client while the model is still writing, not all together at the end. This page describes the path a token takes, the rules that keep it streaming, and what to do before changing anything on that path.

The tests that guard it are in `backend/retriever/tests/test_streaming.py`.

## The path a token takes

```
Groq  ──►  llm.stream_response        async generator, AsyncGroq(stream=True), yields chunk.choices[0].delta.content
              │
              ▼
        turn.node_generate            async for text in tokens: sender.add(text)   (LangGraph custom stream writer)
              │
              ▼
        turn_graph.get_graph()        compiled StateGraph, no checkpointer
              │  astream(state, stream_mode="custom")
              ▼
        turn.run_turn                 async with aclosing(...astream(...)) as events: async for event in events: yield event
              │
              ▼
        main.ChatServicer.Chat        Token -> chat_pb2.ChatEvent(token=chat_pb2.Token(text=...))   (async generator = gRPC server stream)
              │  gRPC: rpc Chat(ChatRequest) returns (stream ChatEvent)
              ▼
        orchestrator chat_client.chat   yields ("token", text); closing it cancels the gRPC call
              │
              ▼
        orchestrator websocket        send_json({"type": "token", ...}); "cancel" only applies while tokens are flowing
              │
              ▼
        browser
```

| Piece | Why it matters for streaming |
|---|---|
| `node_generate` (`turn.py`) | The only place tokens are written. The save and the `Done` event are in the same node, so a stop still keeps the partial answer. |
| `run_turn` (`turn.py`) | Re-yields graph events one by one. Holds the claim for the question and releases it in its own `finally`. |
| `ChatServicer.Chat` (`main.py`) | Maps events 1:1 to gRPC messages and maps exceptions to status codes. |

## The contract

1. **Incremental:** token N is received by the gRPC client before the model has produced token N+1.
2. **Context first:** the `Context` event arrives before the model has produced any token.
3. **Complete and ordered:** `Context`, then every `Token` in the order the model wrote them, then exactly one `Done` last. The tokens joined equal `Done.answer`.
4. **Stoppable:** cancelling the call mid-stream stops the model, saves the partial answer once, releases the claim and leaves no orphan task.
5. **Honest errors:** if Groq fails after some tokens were sent, the client gets those tokens, then a gRPC error status (never a `Done`), and no half answer is saved.

Each point has at least one test in `tests/test_streaming.py`. The incremental tests use a gated fake model: it writes token N+1 only after the consumer has received token N. If anything buffers, the consumer waits for a token that was never written and the test fails with a `TimeoutError` instead of hanging.

## Why a custom stream and not `stream_mode="messages"`

Tokens are sent with LangGraph's custom stream: `get_stream_writer()` in `node_generate` and `stream_mode="custom"` in `run_turn`. The token source stays `llm.stream_response`, the raw `AsyncGroq` stream.

`stream_mode="messages"` is not used because it needs a LangChain chat model (for example `langchain-groq`). That adds a dependency and brings tracing risk. Do not enable LangSmith or LangChain tracing: leave `LANGCHAIN_TRACING_V2` and `LANGSMITH_TRACING` unset.

## Known ways streaming silently breaks

- **Tokens collected and sent at the end.** A `list(...)` around the stream, tokens put in the state, or a node placed after `generate` that "sends the answer".
- **`stream_mode="values"` or `"updates"` used for the answer.** LangGraph then emits once per finished node, so the whole answer arrives at the end.
- **The writer called from a worker thread or a sync node** (for example `asyncio.to_thread`). Ordering and flushing are not guaranteed. Keep the streaming loop in an async node, on the event loop.
- **Python older than 3.11.** `get_stream_writer()` does not work inside async nodes there. CI uses 3.12. If Python is ever downgraded, `test_python_can_stream_from_async_graph_nodes` fails loudly.
- **A proxy or server between the orchestrator and the browser that buffers.** This is outside the retriever, so unit tests cannot catch it. Use the manual check below.
- **A slow client lets the model run ahead.** This is not a bug. LangGraph's stream queue can hold tokens in memory while a slow client reads, bounded by the length of one answer. `test_a_slow_consumer_still_gets_every_token_in_order` and its gRPC twin check that nothing is lost or reordered.

## Optional settings and logs

- **`first_token ms=N` log line.** `node_generate` logs it once per turn, when the first token arrives, measured from the start of the node. It contains only a number, no query, answer or document text. It is the one number that tells you in production whether streaming feels instant.
- **`TOKEN_COALESCE_MS` (default `0`, off).** With `0`, every token goes out the moment the model writes it. With a value such as `25`, tokens that arrive faster than one per 25 ms are sent together, which lowers the message rate. The first token, and any token after a quiet moment, is never delayed. A timer, the end of the answer, a stop or a model failure sends any held text. Turn it on only if the message rate is a measured problem, and run the tests with it off and on.

## Upgrade checklist

After any `langgraph` version bump (or any change to `turn.py`, `turn_graph.py` or `main.py`):

1. Run the streaming tests first, from `backend/retriever`:

   ```
   pip install -r requirements-dev.txt
   python -m grpc_tools.protoc -I ../proto --python_out=. --grpc_python_out=. ../proto/chat.proto   # only if chat_pb2*.py are missing
   GROQ_API_KEY=dummy-key-for-tests python -m pytest tests/test_streaming.py -q                    # PowerShell: $env:GROQ_API_KEY="dummy-key-for-tests"
   ```

2. If `test_each_token_arrives_before_the_next_one_is_written` (or its gRPC version `test_over_grpc_each_token_arrives_before_the_next_one_is_written`) times out, streaming is buffered. **Do not ship.**
3. Run the rest of the suite:

   ```
   GROQ_API_KEY=dummy-key-for-tests python -m pytest tests -q
   ```

4. Do the manual check on the real stack, because no unit test crosses the orchestrator, the websocket and the browser:
   1. Start the stack, open a chat and ask a long question ("write 300 words about ...").
   2. The answer must appear word by word, not all at once after a pause.
   3. Compare the time to the first word with the `first_token ms=` log line. They should be close.
   4. Press **Stop** mid-answer. It stops, and after a reload the partial answer is still there.
   5. In the retriever log, check for one `node=` line per node and the same request id on all of them.

If the browser shows the answer all at once while every test is green, the buffering is outside the retriever (the orchestrator, the websocket or a proxy). That is a separate fix.
