"""step7_apply.py: Step 7 of the migration plan (frontend, scripts, docs).

Run from the PROJECT ROOT (the folder that holds backend\\ and frontend\\):

    python step7_apply.py              # apply
    python step7_apply.py --dry-run    # show what would change, write nothing
    python step7_apply.py --no-backup  # do not keep .step7.bak copies

The backend is NOT touched. What it changes:

FRONTEND (3 real faults found by reading it against the plan and the backend)
  1. Sources vanished when a chat was loaded again. The history route now sends
     `results` (the sources saved with each answer) but `fromServer` dropped them.
       types.ts, hooks/useChatMessages.ts (+ test)
  2. A document refresh that was already running could undo an upload or a
     delete (the new document disappeared, or a deleted one came back, and
     with nothing busy it stayed wrong until a reload).
       hooks/useDocuments.ts (+ 2 tests)
  3. A document stuck in "deleting" (the index was down, the server answers 503
     and keeps the row) had its delete button disabled forever.
       components/DocumentPanel.tsx (+ test)

SCRIPTS
  run-all.ps1: starts the ingestion API (waits for /health) and the ingestion
  worker (BelowNormal) BEFORE the orchestrator, and checks backend\\ingestion\\.env.

DOCS
  README.md: features, architecture, rules, REST and WebSocket tables, env vars,
  setup, running, tests, troubleshooting and limitations (plan sections 1, 13, 16.4).
  docs/MIGRATION_PLAN.md: a copy of the plan, if it is not there yet.

Every edit is checked on its own. An edit whose text is already there is
skipped ("already applied"), so the script can be run twice. An edit whose
anchor text is not found is reported as MISS and nothing is guessed. Each file
keeps its own line endings (CRLF or LF).
"""

import shutil
import sys
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv
BACKUP = "--no-backup" not in sys.argv

ROOT = Path.cwd()

APPLIED = 0
SKIPPED = 0
MISSED = 0


class Patcher:
    """Edits one text file in memory, then writes it once."""

    def __init__(self, relative: str):
        self.relative = relative
        self.path = ROOT / relative
        self.ok = self.path.is_file()
        self.changed = False

        if not self.ok:
            print(f"MISS   {relative}: file not found")
            global MISSED
            MISSED += 1
            return

        raw = self.path.read_bytes().decode("utf-8")
        self.eol = "\r\n" if "\r\n" in raw else "\n"
        self.text = raw.replace("\r\n", "\n")

    # ---- reporting ----

    def _applied(self, label: str):
        global APPLIED
        APPLIED += 1
        self.changed = True
        print(f"APPLY  {self.relative}: {label}")

    def _skipped(self, label: str):
        global SKIPPED
        SKIPPED += 1
        print(f"SKIP   {self.relative}: {label} (already applied)")

    def _missed(self, label: str, why: str):
        global MISSED
        MISSED += 1
        print(f"MISS   {self.relative}: {label} ({why})")

    # ---- helpers ----

    def _find_line(self, anchor: str):
        """Start and end of the one line that contains `anchor`."""
        count = self.text.count(anchor)
        if count != 1:
            return None, f"anchor found {count} times"
        position = self.text.index(anchor)
        start = self.text.rfind("\n", 0, position) + 1
        end = self.text.find("\n", position)
        if end == -1:
            end = len(self.text)
        return (start, end), ""

    # ---- edits ----

    def replace(self, label: str, old: str, new: str, marker: str | None = None):
        if not self.ok:
            return
        if (marker or new) in self.text:
            return self._skipped(label)
        count = self.text.count(old)
        if count != 1:
            return self._missed(label, f"text found {count} times")
        self.text = self.text.replace(old, new)
        self._applied(label)

    def replace_line(self, label: str, anchor: str, new_line: str, marker: str | None = None):
        if not self.ok:
            return
        if (marker or new_line) in self.text:
            return self._skipped(label)
        span, why = self._find_line(anchor)
        if span is None:
            return self._missed(label, why)
        start, end = span
        self.text = self.text[:start] + new_line + self.text[end:]
        self._applied(label)

    def insert_after(self, label: str, anchor: str, block: str, marker: str, gap: str = "\n\n"):
        if not self.ok:
            return
        if marker in self.text:
            return self._skipped(label)
        span, why = self._find_line(anchor)
        if span is None:
            return self._missed(label, why)
        _, end = span
        self.text = self.text[:end] + gap + block + self.text[end:]
        self._applied(label)

    def insert_before(self, label: str, anchor: str, block: str, marker: str, gap: str = "\n\n"):
        if not self.ok:
            return
        if marker in self.text:
            return self._skipped(label)
        span, why = self._find_line(anchor)
        if span is None:
            return self._missed(label, why)
        start, _ = span
        self.text = self.text[:start] + block + gap + self.text[start:]
        self._applied(label)

    def append(self, label: str, block: str, marker: str):
        if not self.ok:
            return
        if marker in self.text:
            return self._skipped(label)
        self.text = self.text.rstrip("\n") + "\n\n" + block.rstrip("\n") + "\n"
        self._applied(label)

    # ---- write ----

    def save(self):
        if not self.ok or not self.changed or DRY_RUN:
            return
        if BACKUP:
            backup = self.path.with_name(self.path.name + ".step7.bak")
            if not backup.exists():
                shutil.copyfile(self.path, backup)
        data = self.text.replace("\n", self.eol)
        self.path.write_bytes(data.encode("utf-8"))


# =====================================================================
# 1. FRONTEND
# =====================================================================

def frontend() -> None:
    # ---- 1. Sources must survive a reload -----------------------------------
    types = Patcher("frontend/src/types.ts")
    types.replace(
        "ServerMessage carries the saved sources",
        r'''export interface ServerMessage {
  id: string;
  role: MessageRole;
  content: string;
  created_at: string;
}''',
        r'''export interface ServerMessage {
  id: string;
  role: MessageRole;
  content: string;
  created_at: string;
  // The sources an answer was written with (document parts and older
  // messages). Saved with the answer, so they come back with the history.
  results?: RetrievedMessage[];
}''',
    )
    types.save()

    messages = Patcher("frontend/src/hooks/useChatMessages.ts")
    messages.replace(
        "fromServer keeps the saved sources",
        r'''    createdAt: item.created_at,
    status: item.role === "user" ? "sent" : undefined,
  };
}''',
        r'''    createdAt: item.created_at,
    status: item.role === "user" ? "sent" : undefined,
    // Without this the Sources panel would be empty after every reload
    context: item.results?.length ? item.results : undefined,
  };
}''',
    )
    messages.save()

    messages_test = Patcher("frontend/src/hooks/useChatMessages.test.ts")
    messages_test.append(
        "test: saved sources come back with the history",
        r'''describe("saved sources", () => {
  it("keeps the sources an answer was written with when a chat is loaded", async () => {
    const source = {
      id: "d1:0",
      payload: { document_id: "d1", filename: "report.pdf", chunk_index: 0, content: "Revenue grew." },
    };

    getMessagesMock.mockResolvedValue({
      items: [serverMessage("u1", "user"), { ...serverMessage("a1", "assistant"), results: [source] }],
      next_cursor: null,
    });

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(result.current.messages[1].context).toEqual([source]);
  });

  it("gives a message with no sources no context", async () => {
    getMessagesMock.mockResolvedValue({
      items: [{ ...serverMessage("a1", "assistant"), results: [] }, serverMessage("u1", "user")],
      next_cursor: null,
    });

    const { result } = renderHook(() => useChatMessages("chat-a", false));
    await waitFor(() => expect(result.current.historyStatus).toBe("ready"));

    expect(result.current.messages[0].context).toBeUndefined();
    expect(result.current.messages[1].context).toBeUndefined();
  });
});''',
        'describe("saved sources"',
    )
    messages_test.save()

    # ---- 2. A running refresh must not undo an upload or a delete -----------
    hook = Patcher("frontend/src/hooks/useDocuments.ts")
    hook.replace(
        "refs for change counting",
        r'''  const refreshing = useRef(false);
''',
        r'''  const refreshing = useRef(false);

  // Counts the changes made here (upload, delete). A refresh that started
  // before a change holds an older list, so its answer must not replace ours.
  const changes = useRef(0);
  const refreshRef = useRef<() => Promise<void>>(() => Promise.resolve());
''',
        marker="const changes = useRef(0);",
    )
    hook.replace(
        "refresh drops an out-of-date answer",
        r'''    refreshing.current = true;

    return getDocuments()
      .then((list) => {
        setDocuments(newestFirst(list));
        setStatus("ready");
      })''',
        r'''    refreshing.current = true;

    const startedAt = changes.current;
    let outdated = false;

    return getDocuments()
      .then((list) => {
        // We uploaded or deleted something while this was on its way, so its
        // list is older than ours: drop it and ask again.
        if (changes.current !== startedAt) {
          outdated = true;
          return;
        }

        setDocuments(newestFirst(list));
        setStatus("ready");
      })''',
        marker="let outdated = false;",
    )
    hook.replace(
        "refresh asks again after an out-of-date answer",
        r'''      .finally(() => {
        refreshing.current = false;
      });
  }, [handleFailure]);
''',
        r'''      .finally(() => {
        refreshing.current = false;
        if (outdated) void refreshRef.current();
      });
  }, [handleFailure]);

  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);
''',
        marker="if (outdated) void refreshRef.current();",
    )
    hook.replace(
        "upload counts as a change",
        r'''        const created = await uploadDocument(file);
''',
        r'''        const created = await uploadDocument(file);
        changes.current += 1; // a refresh that started earlier is now out of date
''',
        marker="changes.current += 1; // a refresh that started earlier is now out of date\n\n        // The same file",
    )
    hook.replace(
        "delete counts as a change",
        r'''      setDocuments((current) => current.filter((d) => d.id !== id));''',
        r'''      changes.current += 1; // a refresh that started earlier is now out of date
      setDocuments((current) => current.filter((d) => d.id !== id));''',
        marker="changes.current += 1; // a refresh that started earlier is now out of date\n      setDocuments",
    )
    hook.save()

    hook_test = Patcher("frontend/src/hooks/useDocuments.test.ts")
    hook_test.append(
        "tests: a running refresh cannot undo an upload or a delete",
        r'''describe("a refresh that was already running", () => {
  // A list request the test answers by hand
  function slowList() {
    let answer: (list: DocumentInfo[]) => void = () => {};
    const promise = new Promise<DocumentInfo[]>((resolve) => {
      answer = resolve;
    });
    return { promise, answer: (list: DocumentInfo[]) => answer(list) };
  }

  it("cannot bring back a document that was just deleted", async () => {
    const old = slowList();
    getDocumentsMock
      .mockResolvedValueOnce([doc("a"), doc("b")])
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue([doc("b")]);
    deleteMock.mockResolvedValue(undefined);

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    // A refresh is on its way, and it will still list "a" ...
    act(() => {
      void result.current.refresh();
    });

    // ... the user deletes "a" and the server confirms ...
    await act(async () => {
      await result.current.remove("a");
    });
    expect(ids(result.current.documents)).toEqual(["b"]);

    // ... and only then does the old answer arrive
    await act(async () => {
      old.answer([doc("a"), doc("b")]);
    });

    // It was dropped and the list was asked for again
    await waitFor(() => expect(getDocumentsMock).toHaveBeenCalledTimes(3));
    expect(ids(result.current.documents)).toEqual(["b"]);
  });

  it("cannot make a document that was just uploaded disappear", async () => {
    const old = slowList();
    getDocumentsMock
      .mockResolvedValueOnce([doc("b", "ready", 1)])
      .mockReturnValueOnce(old.promise)
      .mockResolvedValue([doc("n", "ready", 2), doc("b", "ready", 1)]);
    uploadMock.mockResolvedValue(doc("n", "ready", 2));

    const { result } = renderHook(() => useDocuments());
    await waitFor(() => expect(result.current.status).toBe("ready"));

    // A refresh is on its way, and it does not know about "n" yet ...
    act(() => {
      void result.current.refresh();
    });

    // ... the upload finishes ...
    await act(async () => {
      await result.current.upload(new File(["hi"], "n.txt"));
    });
    expect(ids(result.current.documents)).toEqual(["n", "b"]);

    // ... and only then does the old answer arrive
    await act(async () => {
      old.answer([doc("b", "ready", 1)]);
    });

    await waitFor(() => expect(getDocumentsMock).toHaveBeenCalledTimes(3));
    expect(ids(result.current.documents)).toEqual(["n", "b"]);
  });
});''',
        'describe("a refresh that was already running"',
    )
    hook_test.save()

    # ---- 3. A document stuck in "deleting" must be deletable again ----------
    panel = Patcher("frontend/src/components/DocumentPanel.tsx")
    panel.replace(
        "delete button stays enabled for a 'deleting' document",
        r'''                  title="Delete document"
                  disabled={document.status === "deleting"}
                  onClick={() => onRemove(document.id)}''',
        r'''                  title="Delete document"
                  onClick={() => onRemove(document.id)}''',
        marker='title="Delete document"\n                  onClick',
    )
    panel.save()

    panel_test = Patcher("frontend/src/components/DocumentPanel.test.tsx")
    panel_test.append(
        "test: a stuck 'deleting' document can be deleted again",
        r'''describe("a delete that did not finish", () => {
  it("can be tried again: the delete button stays enabled", () => {
    // The index was down, so the server kept the row as "deleting"
    const { onRemove } = setup([doc("a", "deleting")]);

    const button = screen.getByRole("button", { name: "Delete a.pdf" }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);

    fireEvent.click(button);

    expect(onRemove).toHaveBeenCalledWith("a");
  });

  it("cannot be ticked for questions", () => {
    setup([doc("a", "deleting")]);

    expect(box("a.pdf").disabled).toBe(true);
  });
});''',
        'describe("a delete that did not finish"',
    )
    panel_test.save()


# =====================================================================
# 2. run-all.ps1
# =====================================================================

def run_all() -> None:
    script = Patcher("run-all.ps1")

    script.replace(
        "header comment lists the new windows",
        r'''#     2. Retriever         (gRPC, port 50051)   - new window
#     3. Orchestrator      (FastAPI, port 8000) - new window
#     4. Frontend          (Vite, port 5173)    - new window
# Then it opens http://localhost:5173 in your browser.
#
# To stop: close the three windows (or press Ctrl+C in each), then run:''',
        r'''#     2. Retriever         (gRPC, port 50051)   - new window
#     3. Ingestion API     (FastAPI, port 8100) - new window
#     4. Ingestion worker  (no port)            - new window, BelowNormal priority
#     5. Orchestrator      (FastAPI, port 8000) - new window
#     6. Frontend          (Vite, port 5173)    - new window
# Then it opens http://localhost:5173 in your browser.
#
# To stop: close the five windows (or press Ctrl+C in each), then run:''',
    )

    script.insert_after(
        "$Ingestion folder variable",
        r'''$Orchestrator = Join-Path $Backend "orchestrator"''',
        r'''$Ingestion    = Join-Path $Backend "ingestion"''',
        marker='$Ingestion    = Join-Path $Backend "ingestion"',
        gap="\n",
    )

    script.replace(
        "Wait-ForApi names the window to look at",
        r'''function Wait-ForApi($url, $timeoutSeconds) {''',
        r'''function Wait-ForApi($url, $timeoutSeconds, $window = "orchestrator") {''',
    )
    script.replace(
        "Wait-ForApi error text uses that name",
        r'''Check the orchestrator window."''',
        r'''Check the $window window."''',
    )

    script.replace(
        "Start-InWindow can lower the priority",
        r'''function Start-InWindow($title, $folder, $command) {
    $inner = "`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$folder'; $command"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $inner | Out-Null
}''',
        r'''# -LowPriority: the window itself is set to BelowNormal BEFORE the command starts,
# and a process inherits the priority class of the process that starts it, so the
# command never runs at normal priority.
function Start-InWindow($title, $folder, $command, [switch]$LowPriority) {
    $priority = ""
    if ($LowPriority) { $priority = "(Get-Process -Id `$PID).PriorityClass = 'BelowNormal'; " }
    $inner = "$priority`$Host.UI.RawUI.WindowTitle = '$title'; Set-Location '$folder'; $command"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $inner | Out-Null
}''',
        marker="-LowPriority: the window itself",
    )

    script.replace(
        "check backend\\ingestion\\.env",
        r'''if (-not (Test-Path (Join-Path $Retriever ".env"))) {
    Fail "Missing backend\retriever\.env (it needs QDRANT_URL=http://localhost:6333)."
}''',
        r'''if (-not (Test-Path (Join-Path $Retriever ".env"))) {
    Fail "Missing backend\retriever\.env (it needs QDRANT_URL=http://localhost:6333)."
}

if (-not (Test-Path (Join-Path $Ingestion ".env"))) {
    Fail "Missing backend\ingestion\.env (copy .env.example. It needs INGEST_SERVICE_TOKEN, the same value as in the orchestrator, and the same CHAT_ENCRYPTION_KEYS as the retriever)."
}''',
        marker='Missing backend\\ingestion\\.env',
    )

    script.insert_after(
        "find the ingestion Python",
        r'''$OrchestratorPython = Get-VenvPython $Orchestrator''',
        r'''$IngestionPython    = Get-VenvPython $Ingestion''',
        marker="$IngestionPython    = Get-VenvPython $Ingestion",
        gap="\n",
    )

    script.insert_before(
        "start the ingestion API and the worker before the orchestrator",
        r'''# ---------- 3. Orchestrator ----------''',
        r'''# ---------- 3. Ingestion API ----------
Write-Step "Starting Ingestion API (FastAPI)"
Start-InWindow "Ingestion API (FastAPI :8100)" $Ingestion "& '$IngestionPython' main.py"

Wait-ForApi "http://localhost:8100/health" 120 "ingestion API"


# ---------- 4. Ingestion worker ----------
# The worker is what turns an uploaded document from pending into ready. It runs at
# BelowNormal priority so that reading a big PDF does not slow the live chat.
Write-Step "Starting Ingestion worker (low priority)"
Start-InWindow "Ingestion worker (BelowNormal)" $Ingestion "& '$IngestionPython' worker.py" -LowPriority


''',
        marker="# ---------- 3. Ingestion API ----------",
        gap="",
    )

    script.replace_line(
        "renumber the orchestrator section",
        r'''# ---------- 3. Orchestrator ----------''',
        r'''# ---------- 5. Orchestrator ----------''',
    )
    script.replace_line(
        "renumber the frontend section",
        r'''# ---------- 4. Frontend ----------''',
        r'''# ---------- 6. Frontend ----------''',
    )

    script.replace(
        "done message: window count",
        r'''To stop: close the 3 windows, then run: docker compose down''',
        r'''To stop: close the 5 windows, then run: docker compose down''',
    )
    script.insert_after(
        "done message: ingestion health",
        r'''Write-Host "    API health:   http://localhost:8000/api/health"''',
        r'''Write-Host "    Ingestion:    http://localhost:8100/health"''',
        marker='Write-Host "    Ingestion:    http://localhost:8100/health"',
        gap="\n",
    )

    script.save()


# =====================================================================
# 3. README.md
# =====================================================================

ARCH_DIAGRAM = r'''```text
Browser (React) ──REST + WebSocket (session cookie)──► Orchestrator
                    login, sessions, rate limits, user_id from SESSION only,
                    upload checks (size, type), no document storage

  ① chat + user_id + document_ids (gRPC, existing channel) ──► Retriever
  ② upload / list / status / delete + user_id (HTTP, token) ──► Ingestion API

Retriever  (chat service, owns chat.db)
  history · memory search · DOCUMENT search (read only) · LLM · answer + sources
  ③ HTTPS ──► Groq
  ④ READ, filtered by user_id (+ document_id, status=ready for answers) ──► Qdrant

Ingestion service (owns ingest.db: one `documents` table, job state = its status and stage)
  API process:    accepts the file, checks it, writes a job row, returns at once
  Worker process: parse ► chunk ► embed ► encrypt ► upsert ► set status=ready LAST
  ⑤ WRITE (upsert, set_payload, delete) ──► Qdrant

Qdrant
  chat_messages     retriever reads and writes
  document_chunks   ingestion writes, retriever reads only
```'''

ARCH_SECTION = (
    "### Documents and the ingestion service\n\n"
    "Uploaded documents are handled by a separate **ingestion service** (folder `backend/ingestion`). "
    "It has an **API** process (accepts the file, checks it, answers at once) and a **worker** process "
    "(reads, splits, embeds, encrypts and stores the document). The retriever only **reads** the stored "
    "document chunks, to answer questions. Qdrant is the only link between the two; the retriever and the "
    "ingestion service never call each other.\n\n"
    + ARCH_DIAGRAM
    + "\n\n"
    "- **Upload:** Browser ► Orchestrator (checks, user id) ► Ingestion API (`pending`) ► Worker (`processing`) ► Qdrant ► `ready`. The browser lists its documents every 2 seconds while any is `pending` or `processing`.\n\n"
    "- **Ask:** Browser ► Orchestrator (user id, `document_ids`) ► Retriever ► owner and ready check ► filtered Qdrant search plus chat memory ► Groq ► streamed answer with sources.\n\n"
    "- **Delete:** Browser ► Orchestrator ► Ingestion ► chunks removed from Qdrant, then the row from `ingest.db`.\n\n"
    "- **Status:** Browser ► Orchestrator ► Ingestion (`ingest.db`).\n\n"
    "New files for this feature: `orchestrator/documents.py` and `ingest_client.py`, `retriever/doc_retrieval.py`, "
    "`frontend/src/components/DocumentPanel.tsx` and `hooks/useDocuments.ts`. The shared code (encryption, service token, "
    "text cleaning, embeddings) lives in `backend/shared_common` (package `common`)."
)

TREE_BLOCK = "\n\n".join(
    [
        "│   ├── ingestion/            # the ingestion service: API (main.py) and worker (worker.py)",
        "│   │   ├── .env.example",
        "│   │   ├── routes.py         # /documents: upload, list, status, delete",
        "│   │   ├── jobs.py           # one document job: parse, analyse, chunk, embed, store",
        "│   │   ├── worker.py         # background worker (runs at low priority)",
        "│   │   ├── parsers.py, stats.py, analyzer.py, analysis_schema.py, chunking/   # reading and splitting",
        "│   │   ├── qdrant_docs.py    # writes the encrypted chunks to Qdrant",
        "│   │   ├── repository.py, models.py, database.py   # ingest.db: documents and job state",
        "│   │   └── tests/",
        "│   ├── shared_common/        # package `common`: crypto, service_auth, sanitize, embeddings, settings",
    ]
)

ENV_BLOCK = r'''Add these to `backend/orchestrator/.env` (the defaults are shown; the token is required):

```env
INGEST_SERVICE_URL=http://localhost:8100
INGEST_SERVICE_TOKEN=the_same_token_as_in_the_ingestion_env
INGEST_TLS_CA=
MAX_UPLOAD_MB=10
DOC_UPLOAD_RATE_LIMIT=5
```

`backend/ingestion/.env` (copy `.env.example`)

```env
QDRANT_URL=http://localhost:6333
CHAT_ENCRYPTION_KEYS=exactly_the_same_keys_as_the_retriever
INGEST_SERVICE_TOKEN=a_long_random_token
GROQ_API_KEY=your_key_here
```

Make a token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. The ingestion service will not start without `INGEST_SERVICE_TOKEN`, and a different `CHAT_ENCRYPTION_KEYS` than the retriever's means document text cannot be read back.

| Setting | What it does |
|---|---|
| `INGEST_SERVICE_TOKEN` | Required, and the same value in the ingestion service and the orchestrator. |
| `CHAT_ENCRYPTION_KEYS` (ingestion) | The same keys as the retriever. Document text and file names are encrypted with them. |
| `QDRANT_URL`, `QDRANT_API_KEY` | The same Qdrant as the retriever. |
| `INGEST_BIND_HOST`, `INGEST_PORT` | Where the ingestion API listens. Default `127.0.0.1` and `8100`. |
| `GROQ_API_KEY`, `INGEST_ANALYSIS_MODEL`, `INGEST_ANALYSIS_ENABLED` | Stage 1 of chunking: an AI model picks how a document is split. Without a key, or with `INGEST_ANALYSIS_ENABLED=false`, fixed rules choose and the upload still works. |
| `MAX_UPLOAD_MB`, `MAX_DOCS_PER_USER` | 10 MB and 20 documents. Keep `MAX_UPLOAD_MB` equal in the orchestrator, the ingestion service and (as `VITE_MAX_UPLOAD_MB`) the frontend. |
| `INGEST_SEMANTIC_MAX_CHARS`, `INGEST_SEMANTIC_PERCENTILE`, `INGEST_JOB_TIMEOUT_SECONDS`, `INGEST_TORCH_THREADS` | Tuning. Defaults 80000, 90, 300 and half the CPU cores. |
| `INGEST_DB_PATH`, `INGEST_UPLOAD_DIR` | Defaults `./ingest.db` and `./data/uploads`. |
| `INGEST_ALLOW_NO_AUTH` | Development only, localhost only: start without a token. |
| `INGEST_SERVICE_URL`, `INGEST_TLS_CA` (orchestrator) | Where and how the orchestrator reaches the ingestion API. |
| `DOC_UPLOAD_RATE_LIMIT` (orchestrator) | Uploads per user per 60 seconds. Default 5. |
| `DOC_BUDGET_CHARS` (retriever) | How much document text goes into one prompt. Default 12000. |

Ports: Qdrant 6333, retriever 50051, orchestrator 8000, frontend 5173, **ingestion 8100**.'''

TROUBLESHOOTING_BLOCK = r'''**A document is stuck on "Queued" or "Processing"**

- The ingestion **worker** is probably not running. The API only accepts the file; the worker reads and stores it. Start it with `python worker.py` in `backend\ingestion` (or use `run-all.ps1`). A job that was cut off in the middle starts again by itself.

**Uploading says "The document service is unavailable" (or "could not handle the request")**

- The ingestion API is not running at `INGEST_SERVICE_URL` (start it and check http://localhost:8100/health), or `INGEST_SERVICE_TOKEN` is not the same in `backend/orchestrator/.env` and `backend/ingestion/.env`.

**A document ends as "Failed"**

- The message under it says why: the file is damaged or not the type its name says, it has no readable text (scanned pages are not supported, there is no OCR), it is too large or too long, or reading it took too long. Fix or replace the file and upload it again.

**A document keeps showing "Deleting"**

- The search index (Qdrant) could not be reached when you deleted it. Start Qdrant and click the × again.

**The ingestion service fails to start**

- `INGEST_SERVICE_TOKEN` and `CHAT_ENCRYPTION_KEYS` are both required. Use the same `CHAT_ENCRYPTION_KEYS` as the retriever, or document text cannot be read when you ask about it.'''

LIMITS_BLOCK = "\n\n".join(
    [
        "- **The job queue is a database table** and the worker is a single process. For several workers, move claiming to Redis or a proper queue.",
        "- **Uploaded files sit on disk unencrypted** for the seconds a job runs, then they are deleted. The original files are not kept.",
        "- **Ingestion shares the encryption key with the retriever on purpose** (one trust zone). Keep both in the same secrets manager.",
        "- **The ingestion port must stay on an internal network** (or use TLS), because the ingestion service trusts the `X-User-Id` it receives, like the gRPC port.",
        "- **No OCR, no web pages, no sharing:** scanned PDFs fail with \"no readable text\", URLs cannot be ingested, and documents cannot be shared between users.",
    ]
)


def readme() -> None:
    doc = Patcher("README.md")

    # ---- Intro and features ----
    doc.replace(
        "status line mentions documents",
        "encryption at rest and automated tests are built.",
        "encryption at rest, document upload with questions about your documents, and automated tests are built.",
    )
    doc.insert_after(
        "features: documents",
        "- **Live chat screen:**",
        "- **Ask about your documents:** upload a PDF, DOCX, TXT or Markdown file (up to 10 MB), wait until it shows **Ready**, tick it, and ask questions about it. Up to 5 documents can be ticked per question, and the answer is written from the best matching parts of them.\n\n"
        "- **Sources under each answer:** the **Sources** panel lists the document parts (file name and part number) and the older chat messages the answer used. Sources are saved with the answer, so they are still there when the chat is loaded again.\n\n"
        "- **A separate ingestion service:** reading, splitting, embedding and storing a document is done by its own service and a background worker, so uploads do not slow down the chat. Document text and file names are encrypted, and every document belongs to one user.",
        marker="**Ask about your documents:**",
    )
    doc.replace(
        "features: one command start",
        "starts Qdrant, the retriever, the orchestrator and the frontend.",
        "starts Qdrant, the retriever, the ingestion API, the ingestion worker, the orchestrator and the frontend.",
    )
    doc.replace_line(
        "features: tests without stale counts",
        "- **Automated tests:** 46 orchestrator",
        "- **Automated tests:** orchestrator, retriever, ingestion, shared-package and frontend suites; each one prints its own test count (see [Tests](#tests)).",
        marker="each one prints its own test count (see [Tests](#tests))",
    )

    # ---- Architecture ----
    doc.insert_before(
        "architecture: documents and the ingestion service",
        "### What each part does",
        ARCH_SECTION,
        marker="### Documents and the ingestion service",
    )
    doc.replace(
        "parts table: Qdrant is no longer retriever-only",
        "is the **only** service that talks to Qdrant, Groq and the message database",
        "is the only service that talks to Groq and the message database (the ingestion service also writes document chunks to Qdrant; the retriever only reads them)",
    )
    doc.replace_line(
        "rules: retriever and Qdrant",
        "- Retriever ↔ Qdrant: database connection.",
        "- Retriever ↔ Qdrant: database connection. Chat messages are read and written; document chunks are **read only** (the retriever has no write path to them).",
        marker="document chunks are **read only**",
    )
    doc.insert_after(
        "rules: ingestion links",
        "document chunks are **read only**",
        "- Orchestrator ↔ Ingestion: HTTP with a shared service token (`INGEST_SERVICE_TOKEN`) and an `X-User-Id` header. The user id is the session's, never the browser's, and the ingestion service trusts it, so **keep port 8100 on an internal network** (or use TLS).\n\n"
        "- Ingestion ↔ Qdrant: writes the document chunks (`document_chunks`). The retriever and the ingestion service never call each other.\n\n"
        "- A document id that does not exist and one that belongs to another user give the same \"not found\" answer.",
        marker="- Orchestrator ↔ Ingestion: HTTP",
    )

    # ---- REST API ----
    doc.insert_after(
        "REST: document routes",
        "`GET /conversations/{id}/messages?limit=&cursor=`",
        "| `POST /documents` | Upload one document (multipart field `file`; `.pdf`, `.docx`, `.txt`, `.md`; up to `MAX_UPLOAD_MB`). `202` for a new document, `200` if the same file was uploaded before |\n\n"
        "| `GET /documents` | The user's documents with their status |\n\n"
        "| `GET /documents/{id}` | One document's status |\n\n"
        "| `DELETE /documents/{id}` | Delete the document and its chunks (`204`) |",
        marker="| `POST /documents` |",
    )
    doc.insert_after(
        "REST: sources in history and the document rules",
        "The two history routes are answered by the retriever;",
        "Each assistant message in the messages route also has `results`: the sources (document parts and older messages) that answer was written with, in the same shape as the live `done` message.\n\n"
        "**Documents.** The document routes are answered by the ingestion service. The orchestrator first checks the session, the size, the type and an upload rate limit (default 5 per 60 seconds, otherwise `429`). A document id that does not exist and one that belongs to someone else both return `404`. Other answers: `409` (too many documents), `413` (too large), `415` (wrong type or empty), `502` or `503` (the document service is down or refused the request). A document goes `pending`, then `processing`, then `ready`, or ends as `failed` with a short error code.",
        marker="**Documents.** The document routes",
    )

    # ---- WebSocket ----
    doc.insert_after(
        "WebSocket: document_ids message",
        "Ask a question |",
        "| Browser → server | `{\"query\": \"...\", \"conversation_id\": \"...\", \"document_ids\": [\"<uuid>\", \"...\"]}` | Ask a question and also search these documents (at most 5, all must be `ready`) |",
        marker='"document_ids": ["<uuid>"',
    )
    doc.insert_before(
        "WebSocket: document rules",
        "- One answer at a time **per chat**",
        "- **Documents:** `document_ids` is optional. A bad list gives `Invalid document id.`. A document that is still being processed gives `A selected document is still being processed. Try again in a moment.` and nothing is saved. A missing document, or one that belongs to someone else, gives `A selected document was not found.` In the `results` of an answer, a document part has `document_id`, `filename` and `chunk_index` (and sometimes `section_title` and `page`); an older chat message has `role` and `timestamp` instead.",
        marker="- **Documents:** `document_ids` is optional.",
    )

    # ---- Folder structure ----
    doc.replace(
        "tree: hooks",
        "# useChatSocket, useConversations, useChatMessages",
        "# useChatSocket, useConversations, useChatMessages, useDocuments (upload, list, delete, polling)",
    )
    doc.replace(
        "tree: components",
        "# Sidebar, MessageList, ...",
        "# Sidebar, MessageList, DocumentPanel, ContextPanel (Sources), ...",
    )
    doc.insert_before(
        "tree: ingestion and shared_common",
        "├── proto/",
        TREE_BLOCK,
        marker="│   ├── ingestion/            # the ingestion service",
    )

    # ---- Setup ----
    doc.insert_after(
        "setup: pip installs",
        "pip install -r retriever\\requirements.txt",
        "pip install -e shared_common                       # the shared package `common`\n\n"
        "pip install -r ingestion\\requirements.txt          # parsers, chunking, Qdrant, Groq",
        marker="pip install -e shared_common",
    )
    doc.insert_before(
        "setup: ingestion and new orchestrator settings",
        "`frontend/.env`",
        ENV_BLOCK,
        marker="`backend/ingestion/.env` (copy `.env.example`)",
    )
    doc.insert_after(
        "setup: frontend upload limit",
        "VITE_WS_URL=ws://localhost:8000/ws/chat",
        "# VITE_MAX_UPLOAD_MB=10   (optional; keep equal to MAX_UPLOAD_MB in the orchestrator)",
        marker="# VITE_MAX_UPLOAD_MB=10",
    )

    # ---- Running ----
    doc.replace(
        "running: option A",
        "opens separate windows for the retriever, orchestrator and frontend.",
        "opens separate windows for the retriever, the ingestion API, the ingestion worker (at low priority), the orchestrator and the frontend.",
    )
    doc.replace_line(
        "running: option B, ingestion before the orchestrator",
        "# 3. Orchestrator (in backend\\orchestrator, venv active)",
        "# 3. Ingestion API (in backend\\ingestion, venv active)\n\n"
        "python main.py\n\n"
        "# 4. Ingestion worker (in backend\\ingestion, venv active, its own window)\n\n"
        "python worker.py\n\n"
        "# 5. Orchestrator (in backend\\orchestrator, venv active)",
        marker="# 3. Ingestion API (in backend\\ingestion, venv active)",
    )
    doc.replace_line(
        "running: option B, frontend number",
        "# 4. Frontend (in frontend)",
        "# 6. Frontend (in frontend)",
    )
    doc.replace(
        "running: start order",
        "Start the retriever before the orchestrator.",
        "Start the retriever, the ingestion API and the ingestion worker before the orchestrator. The worker is what turns an uploaded document from `pending` into `ready`.",
    )
    doc.insert_after(
        "running: ingestion health check",
        "- Qdrant dashboard: http://localhost:6333/dashboard.",
        "- Ingestion health: http://localhost:8100/health should return `{\"status\": \"healthy\"}`.",
        marker="- Ingestion health:",
    )

    # ---- Tests ----
    doc.replace_line(
        "tests: orchestrator line",
        "# Orchestrator: 46 tests",
        "# Orchestrator: rate limiter, pagination, WebSocket relay, heartbeat, auth checks, documents (the runner prints the count)",
    )
    doc.replace_line(
        "tests: retriever line",
        "# Retriever: 52 tests",
        "# Retriever: turns, repository, gRPC, encrypted columns, document search (the runner prints the count)",
    )
    doc.replace_line(
        "tests: frontend line",
        "# Frontend: 77 tests",
        "# Frontend: API calls, heartbeat, rate-limit handling, history hooks, documents (upload, polling, selecting) and Sources (the runner prints the count)",
    )
    doc.insert_before(
        "tests: ingestion and shared package",
        "# Frontend: API calls, heartbeat",
        "# Ingestion: parsers, chunking, Qdrant storage, jobs, worker, routes (fake embeddings and a fake Qdrant)\n\n"
        "cd backend\\ingestion\n\n"
        "pytest\n\n"
        "# Shared package (crypto, service token, sanitizer)\n\n"
        "cd backend\\shared_common\n\n"
        "pytest",
        marker="# Ingestion: parsers, chunking",
    )

    # ---- Troubleshooting ----
    doc.insert_before(
        "troubleshooting: documents",
        "## Known limitations",
        TROUBLESHOOTING_BLOCK,
        marker='**A document is stuck on "Queued" or "Processing"**',
    )

    # ---- Known limitations ----
    doc.replace_line(
        "limitations: sources are saved now, search returns the top parts",
        "- **Retrieved context is not stored.**",
        "- **Search returns only the top few parts.** A request like \"summarize the whole document\" only sees the most relevant parts of it.",
        marker="- **Search returns only the top few parts.**",
    )
    doc.insert_after(
        "limitations: ingestion",
        "- **Search returns only the top few parts.**",
        LIMITS_BLOCK,
        marker="- **The job queue is a database table**",
    )

    doc.save()


# =====================================================================
# 4. docs/MIGRATION_PLAN.md
# =====================================================================

def export_plan() -> None:
    target = ROOT / "docs" / "MIGRATION_PLAN.md"
    source = ROOT / "docs" / "Info_Mate_Document_Ingestion_Migration_Plan.md"

    if target.exists():
        print("SKIP   docs/MIGRATION_PLAN.md (already there)")
        return
    if not source.is_file():
        print("MISS   docs/MIGRATION_PLAN.md: docs/Info_Mate_Document_Ingestion_Migration_Plan.md not found")
        global MISSED
        MISSED += 1
        return

    if DRY_RUN:
        print("APPLY  docs/MIGRATION_PLAN.md: copy of the plan (dry run)")
    else:
        shutil.copyfile(source, target)
        print("APPLY  docs/MIGRATION_PLAN.md: copy of the plan")

    global APPLIED
    APPLIED += 1


def main() -> int:
    if not (ROOT / "frontend" / "src").is_dir():
        print("Run this from the project root (frontend\\src was not found).")
        return 1

    if DRY_RUN:
        print("DRY RUN: nothing will be written.\n")

    frontend()
    run_all()
    readme()
    export_plan()

    print(f"\nDone: {APPLIED} applied, {SKIPPED} already applied, {MISSED} missed.")

    if MISSED:
        print("A MISS means that file looks different from what this script expects.")
        print("Nothing was guessed for it. Send me that file and the MISS line.")

    print("\nNext:")
    print("  cd frontend")
    print("  npm test")
    print("  npm run lint")
    print("  npm run build")
    print("Then try .\\run-all.ps1 and tick Step 7 in the tracker (section 18.1).")
    return 1 if MISSED else 0


if __name__ == "__main__":
    sys.exit(main())