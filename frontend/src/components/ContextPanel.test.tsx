import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RetrievedMessage } from "../types";
import { ContextPanel } from "./ContextPanel";

const render = (items: RetrievedMessage[]) =>
  renderToStaticMarkup(<ContextPanel items={items} />);

describe("ContextPanel", () => {
  it("renders nothing when there are no sources", () => {
    expect(render([])).toBe("");
  });

  it("is called Sources", () => {
    const html = render([{ id: "1", payload: { role: "user", content: "hi" } }]);

    expect(html).toContain("Sources");
    expect(html).not.toContain("Related past messages");
  });

  it("shows 'filename · part N' (counted from 1) for a document excerpt", () => {
    const html = render([
      {
        id: "d1:2",
        payload: {
          document_id: "d1",
          filename: "report.pdf",
          chunk_index: 2,
          content: "Revenue grew.",
        },
      },
    ]);

    expect(html).toContain("report.pdf · part 3");
    expect(html).toContain("Revenue grew.");
  });

  it("shows the section title and page when the chunk has them", () => {
    const html = render([
      {
        id: "d1:0",
        payload: {
          document_id: "d1",
          filename: "plan.docx",
          chunk_index: 0,
          section_title: "2. Results",
          page: 4,
          content: "text",
        },
      },
    ]);

    expect(html).toContain("2. Results");
    expect(html).toContain("p. 4");
  });

  it("keeps the old look for a past chat message", () => {
    const html = render([
      { id: "m1", payload: { role: "assistant", content: "Earlier answer", timestamp: "2026-11-01T10:00:00Z" } },
    ]);

    expect(html).toContain("Assistant");
    expect(html).toContain("Earlier answer");
    expect(html).not.toContain("part ");
  });

  it("shows a document excerpt and a chat message side by side", () => {
    const html = render([
      { id: "d1:0", payload: { filename: "a.txt", chunk_index: 0, content: "from the file" } },
      { id: "m1", payload: { role: "user", content: "from the chat" } },
    ]);

    expect(html).toContain("a.txt · part 1");
    expect(html).toContain("You");
  });
});
