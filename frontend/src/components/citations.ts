// Inline citations: "[C2]" in an answer points at the 2nd document excerpt the
// answer was written with (backend/retriever/llm.py numbers them that way).
import type { RetrievedMessage } from "../types";

// [C1], [C1, C2], and the 【C1†L1-L3】 style some models write
const MARKER_SOURCE = "(?:\\[|【)\\s*C\\d+(?:\\s*[,;]\\s*C\\d+)*\\s*(?:†[^\\]】]*)?(?:\\]|】)";

// The excerpts that can be cited, in order. Past chat messages are not counted.
export function documentSources(context: RetrievedMessage[] = []): RetrievedMessage[] {
  return context.filter((item) => item.payload?.document_id || item.payload?.filename);
}

// Answers saved before inline citations existed
export function hasOldStyleCitations(content: string): boolean {
  return /\[Source:/i.test(content);
}

// ---------- Markdown: marker text -> <cite> elements ----------

interface MarkdownNode {
  type: string;
  value?: string;
  children?: MarkdownNode[];
  data?: Record<string, unknown>;
}

// A claim never reaches past one of these
const BLOCKS = new Set(["paragraph", "heading", "tableCell"]);

function citeNode(number: number, claim: string): MarkdownNode {
  return {
    type: "citation",
    data: { hName: "cite", hProperties: { dataCite: String(number), dataClaim: claim } },
    children: [{ type: "text", value: "[C" + number + "]" }],
  };
}

function lastSentence(text: string): string {
  const parts = splitSentences(text);
  const last = parts[parts.length - 1];
  return last ? text.slice(last.start, last.end) : "";
}

function splitMarkers(text: string, count: number, block: { text: string }): MarkdownNode[] {
  const nodes: MarkdownNode[] = [];
  const marker = new RegExp(MARKER_SOURCE, "g");
  let copied = 0;

  const addText = (value: string) => {
    if (value) nodes.push({ type: "text", value });
  };

  for (const match of text.matchAll(marker)) {
    const before = text.slice(copied, match.index);
    addText(before);
    block.text += before;

    const claim = lastSentence(block.text);
    const numbers = [...match[0].split("†")[0].matchAll(/C(\d+)/g)].map((m) => Number(m[1]));

    for (const number of new Set(numbers)) {
      // A number with no excerpt behind it stays as the text the model wrote
      if (number >= 1 && number <= count) nodes.push(citeNode(number, claim));
      else addText("[C" + number + "]");
    }

    copied = match.index + match[0].length;
  }

  const rest = text.slice(copied);
  addText(rest);
  block.text += rest;

  return nodes;
}

// A remark plugin. Code blocks and `inline code` are not text nodes, so a
// marker written inside code is left alone.
export function remarkCitations(count: number) {
  const walk = (node: MarkdownNode, block: { text: string }) => {
    if (!node.children) return;

    const own = BLOCKS.has(node.type) ? { text: "" } : block;

    node.children = node.children.flatMap((child) => {
      if (child.type === "text") return splitMarkers(child.value ?? "", count, own);
      if (child.type === "inlineCode") own.text += child.value ?? "";
      walk(child, own);
      return [child];
    });
  };

  return (tree: MarkdownNode) => {
    if (count > 0) walk(tree, { text: "" });
  };
}

// ---------- Finding the supporting line inside an excerpt ----------

export interface TextRange {
  start: number;
  end: number;
}

// Sentences and lines, as positions in the original text (surrounding spaces
// left out), so what is shown is always an unchanged slice of it
function splitSentences(text: string): TextRange[] {
  const ranges: TextRange[] = [];

  for (const match of text.matchAll(/[^\n]+?(?:[.!?।]+(?=\s|$)|(?=\n)|$)/g)) {
    const start = match.index + (match[0].length - match[0].trimStart().length);
    const end = match.index + match[0].trimEnd().length;
    if (end > start) ranges.push({ start, end });
  }

  return ranges;
}

const COMMON_WORDS = new Set(
  ("a an and are as at be been but by can for from had has have he her his i in is it its " +
    "me my not of on or our she so that the their them there they this to was we were " +
    "which who will with you your").split(" "),
);

const WORD = /[\p{L}\p{M}\p{N}]+/gu;

export function keyWords(text: string): Set<string> {
  const words = text.toLowerCase().match(WORD) ?? [];
  return new Set(
    words.filter((word) => !COMMON_WORDS.has(word) && (word.length > 1 || /\p{N}/u.test(word))),
  );
}

// The sentence or line of `content` that shares the most words with the claim.
// This is a word-overlap guess; null when too little matches to point anywhere.
export function findSupportingText(content: string, claim: string): TextRange | null {
  const wanted = keyWords(claim);
  const needed = Math.min(2, wanted.size);
  let best: TextRange | null = null;
  let bestScore = 0;

  for (const range of splitSentences(content)) {
    const words = keyWords(content.slice(range.start, range.end));
    const shared = [...wanted].filter((word) => words.has(word)).length;

    if (shared === 0 || shared < needed) continue;

    // Of two sentences sharing as many words, prefer the shorter one
    const score = shared + shared / (words.size + 1);

    if (score > bestScore) {
      best = range;
      bestScore = score;
    }
  }

  return best;
}
