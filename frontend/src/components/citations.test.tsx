// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ChatMessage, RetrievedMessage } from "../types";
import { MessageBubble } from "./MessageBubble";
import { findSupportingText } from "./citations";

afterEach(cleanup);

const doc = (
  id: string,
  filename: string,
  content: string,
  extra: Record<string, unknown> = {},
): RetrievedMessage => ({
  id,
  payload: { document_id: id, filename, chunk_index: 0, content, ...extra },
});

const pastChat: RetrievedMessage = {
  id: "m0",
  payload: { role: "user", content: "an older chat message" },
};

const RESUME = doc(
  "d1",
  "resume.pdf",
  "Curriculum vitae. My name is Suvekshya Jha. I live in Kathmandu.",
);
const REPORT = doc(
  "d2",
  "report.pdf",
  "Overview\nRevenue grew by 12 percent in 2025.\nCosts were flat.",
  { section_title: "2. Results", page: 4 },
);

function show(content: string, context: RetrievedMessage[] = [RESUME, REPORT]) {
  const message: ChatMessage = {
    id: "a1",
    role: "assistant",
    content,
    createdAt: "2026-11-26T10:00:00Z",
    context,
  };

  return render(
    <MessageBubble
      message={message}
      onRetry={() => {}}
      onEdit={() => true}
      onRegenerate={() => {}}
      actionsDisabled={false}
      canRegenerate={false}
    />,
  );
}

const cite = (label: string) => screen.getByRole("button", { name: "Source " + label });
const popover = () => screen.queryByRole("dialog");

describe("inline citations", () => {
  it("turns a marker into a button inside the sentence", () => {
    const { container } = show("My name is Suvekshya [C1].");

    expect(cite("C1").textContent).toBe("[C1]");
    expect(cite("C1").closest("p")?.textContent).toBe("My name is Suvekshya [C1].");
    expect(container.querySelector(".context-panel")).toBeNull();
    expect(popover()).toBeNull();
  });

  it("opens a popover with the file name and the exact source sentence", () => {
    show("My name is Suvekshya [C1].");

    fireEvent.click(cite("C1"));

    const dialog = popover()!;
    expect(dialog.querySelector(".citation-title")?.textContent).toBe("resume.pdf");
    expect(dialog.querySelector("blockquote")?.textContent).toBe("My name is Suvekshya Jha.");
    expect([...dialog.querySelectorAll("mark")].map((m) => m.textContent)).toContain("Suvekshya");
  });

  it("counts only document excerpts, so past chat messages do not shift the numbers", () => {
    show("Revenue grew by 12 percent [C2].", [pastChat, RESUME, REPORT]);

    fireEvent.click(cite("C2"));

    expect(popover()!.querySelector(".citation-title")?.textContent).toBe("report.pdf");
    expect(popover()!.querySelector("blockquote")?.textContent).toBe(
      "Revenue grew by 12 percent in 2025.",
    );
    expect(popover()!.textContent).toContain("2. Results");
    expect(popover()!.textContent).toContain("p. 4");
  });

  it("shows only one popover: another citation replaces the open one", () => {
    show("My name is Suvekshya [C1]. Revenue grew by 12 percent [C2].");

    fireEvent.click(cite("C1"));
    fireEvent.click(cite("C2"));

    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    expect(popover()!.textContent).toContain("report.pdf");
  });

  it("closes with the close button", () => {
    show("My name is Suvekshya [C1].");
    fireEvent.click(cite("C1"));

    fireEvent.click(screen.getByRole("button", { name: "Close source" }));

    expect(popover()).toBeNull();
  });

  it("closes on a click outside, but not on a click inside", () => {
    show("My name is Suvekshya [C1].");
    fireEvent.click(cite("C1"));

    fireEvent.click(popover()!.querySelector("blockquote")!);
    expect(popover()).not.toBeNull();

    fireEvent.click(document.body);
    expect(popover()).toBeNull();
  });

  it("closes on Escape and when its own marker is clicked again", () => {
    show("My name is Suvekshya [C1].");

    fireEvent.click(cite("C1"));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(popover()).toBeNull();

    fireEvent.click(cite("C1"));
    fireEvent.click(cite("C1"));
    expect(popover()).toBeNull();
  });

  it("splits grouped markers and accepts the model's other bracket style", () => {
    show("Both say so [C1, C2]. Again【C2†L1-L3】.");

    expect(screen.getAllByRole("button", { name: "Source C1" })).toHaveLength(1);
    expect(screen.getAllByRole("button", { name: "Source C2" })).toHaveLength(2);
  });

  it("leaves a marker with no matching source as plain text", () => {
    const { container } = show("Made up [C7].");

    expect(screen.queryByRole("button", { name: "Source C7" })).toBeNull();
    expect(container.textContent).toContain("[C7]");
  });

  it("leaves markers inside code alone", () => {
    show("Type `[C1]` here.");

    expect(screen.queryByRole("button", { name: "Source C1" })).toBeNull();
  });

  it("shows the whole retrieved passage when no sentence matches", () => {
    show("Completely unrelated words [C1].");

    fireEvent.click(cite("C1"));

    expect(popover()!.querySelector("blockquote")?.textContent).toBe(RESUME.payload!.content);
    expect(popover()!.querySelector("mark")).toBeNull();
  });

  it("never shows the Sources list for a new answer", () => {
    const { container } = show("An answer without markers.", [pastChat, RESUME]);

    expect(container.querySelector(".context-panel")).toBeNull();
  });

  it("keeps the Sources list for an old answer written with [Source: file]", () => {
    const { container } = show("Old answer [Source: resume.pdf].");

    expect(container.querySelector(".context-panel")).not.toBeNull();
  });
});

describe("findSupportingText", () => {
  it("returns a slice of the original text, unchanged", () => {
    const content = "First line.\n  Nepal's  capital is Kathmandu.  \nLast.";
    const range = findSupportingText(content, "The capital of Nepal is Kathmandu")!;

    expect(content.slice(range.start, range.end)).toBe("Nepal's  capital is Kathmandu.");
  });

  it("returns null when too little matches", () => {
    expect(findSupportingText("Costs were flat.", "Bananas ripen quickly in summer")).toBeNull();
  });
});
