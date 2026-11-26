import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { RetrievedPayload } from "../types";
import { findSupportingText, keyWords } from "./citations";

const GAP = 6; // between the marker and the popover
const EDGE = 8; // smallest distance to the window edge

// The excerpt's own words. The words it shares with the answer are marked.
function Marked({ text, claim }: { text: string; claim: string }) {
  const wanted = keyWords(claim);

  return (
    <>
      {text.split(/([\p{L}\p{M}\p{N}]+)/u).map((piece, index) =>
        // odd positions are the words
        index % 2 === 1 && wanted.has(piece.toLowerCase()) ? <mark key={index}>{piece}</mark> : piece,
      )}
    </>
  );
}

export function Citation({
  number,
  claim,
  source,
}: {
  number: number;
  claim: string;
  source: RetrievedPayload;
}) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();

  const closeAndReturnFocus = useCallback(() => {
    setOpen(false);
    buttonRef.current?.focus();
  }, []);

  // Next to the marker, kept inside the window. It is attached to <body>, so
  // the scrolling message list cannot cut it off.
  const place = useCallback(() => {
    const button = buttonRef.current;
    const popover = popoverRef.current;
    if (!button || !popover) return;

    const anchor = button.getBoundingClientRect();
    const { offsetWidth: width, offsetHeight: height } = popover;
    const below = anchor.bottom + GAP;
    const top = below + height > window.innerHeight - EDGE ? anchor.top - GAP - height : below;
    const left = Math.min(anchor.left, window.innerWidth - width - EDGE);

    popover.style.top = Math.max(EDGE, top) + "px";
    popover.style.left = Math.max(EDGE, left) + "px";
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    closeRef.current?.focus({ preventScroll: true });
  }, [open, place]);

  useEffect(() => {
    if (!open) return;

    // A click anywhere else closes it, which includes another citation: that
    // one opens, so only one popover is ever open
    const onClick = (event: MouseEvent) => {
      const target = event.target as Node;
      if (buttonRef.current?.contains(target) || popoverRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") closeAndReturnFocus();
    };

    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKeyDown);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);

    return () => {
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, place, closeAndReturnFocus]);

  const content = source.content ?? "";
  const range = open ? findSupportingText(content, claim) : null;
  const where = [
    source.section_title ?? "",
    source.page ? "p. " + source.page : "",
    typeof source.chunk_index === "number" ? "part " + (source.chunk_index + 1) : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      <button
        type="button"
        ref={buttonRef}
        className="citation-marker"
        aria-label={"Source C" + number}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        [C{number}]
      </button>
      {open &&
        createPortal(
          <div className="citation-popover" role="dialog" aria-labelledby={titleId} ref={popoverRef}>
            <div className="citation-head">
              <strong className="citation-title" id={titleId}>{source.filename || "Document"}</strong>
              <button
                type="button"
                ref={closeRef}
                className="citation-close"
                aria-label="Close source"
                onClick={closeAndReturnFocus}
              >
                ×
              </button>
            </div>
            {where && <div className="citation-place">{where}</div>}
            {range ? (
              <blockquote>
                <Marked text={content.slice(range.start, range.end)} claim={claim} />
              </blockquote>
            ) : content ? (
              <>
                <div className="citation-note">Retrieved passage (exact line not identified)</div>
                <blockquote>{content}</blockquote>
              </>
            ) : (
              <div className="citation-note">Content unavailable.</div>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
