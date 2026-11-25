import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { DocumentInfo, DocumentStatus } from "../types";
import { DocumentPanel } from "./DocumentPanel";

afterEach(cleanup);

function doc(id: string, status: DocumentStatus, error?: string): DocumentInfo {
  return { id, filename: id + ".pdf", status, error, createdAt: "2026-11-01T10:00:00Z" };
}

function setup(
  documents: DocumentInfo[],
  extra: Partial<Parameters<typeof DocumentPanel>[0]> = {},
) {
  const handlers = {
    onToggle: vi.fn(),
    onUpload: vi.fn(),
    onRemove: vi.fn(),
    onDismissError: vi.fn(),
    onReload: vi.fn(),
  };

  render(
    <DocumentPanel
      documents={documents}
      status="ready"
      selectedIds={[]}
      uploading={false}
      error={null}
      {...handlers}
      {...extra}
    />,
  );

  return handlers;
}

const box = (name: string) =>
  screen.getByRole("checkbox", { name: "Ask about " + name }) as HTMLInputElement;

describe("selecting", () => {
  it("only a ready document can be ticked", () => {
    setup([
      doc("a", "ready"),
      doc("b", "pending"),
      doc("c", "processing"),
      doc("d", "failed", "no_text"),
    ]);

    expect(box("a.pdf").disabled).toBe(false);
    expect(box("b.pdf").disabled).toBe(true);
    expect(box("c.pdf").disabled).toBe(true);
    expect(box("d.pdf").disabled).toBe(true);
  });

  it("ticking calls onToggle with the id", () => {
    const { onToggle } = setup([doc("a", "ready")]);

    fireEvent.click(box("a.pdf"));

    expect(onToggle).toHaveBeenCalledWith("a");
  });

  it("stops offering new ticks after 5, but a ticked one can be unticked", () => {
    const docs = ["a", "b", "c", "d", "e", "f"].map((id) => doc(id, "ready"));
    setup(docs, { selectedIds: ["a", "b", "c", "d", "e"] });

    expect(box("f.pdf").disabled).toBe(true);
    expect(box("a.pdf").disabled).toBe(false);
    expect(box("a.pdf").checked).toBe(true);
  });
});

describe("status", () => {
  it("shows a badge for each state", () => {
    setup([doc("a", "ready"), doc("b", "pending"), doc("c", "processing")]);

    expect(screen.getByText("Ready")).toBeTruthy();
    expect(screen.getByText("Queued")).toBeTruthy();
    expect(screen.getByText("Processing")).toBeTruthy();
  });

  it("explains a failed document in plain words, not the raw code", () => {
    setup([doc("a", "failed", "no_text")]);

    expect(screen.getByText("Failed")).toBeTruthy();
    expect(screen.getByText(/no readable text/i)).toBeTruthy();
    expect(screen.queryByText("no_text")).toBeNull();
  });

  it("has a safe message for a code it does not know", () => {
    setup([doc("a", "failed", "something_new")]);

    expect(screen.getByText(/could not be processed/i)).toBeTruthy();
  });
});

describe("actions", () => {
  it("delete calls onRemove", () => {
    const { onRemove } = setup([doc("a", "ready")]);

    fireEvent.click(screen.getByRole("button", { name: "Delete a.pdf" }));

    expect(onRemove).toHaveBeenCalledWith("a");
  });

  it("a chosen file goes to onUpload", () => {
    const { onUpload } = setup([]);
    const file = new File(["hi"], "n.txt");
    const input = document.getElementById("document-file-input") as HTMLInputElement;

    fireEvent.change(input, { target: { files: [file] } });

    expect(onUpload).toHaveBeenCalledWith(file);
  });

  it("hands the file over without waiting for the upload to finish", () => {
    // A promise that never settles: if the handler awaited it, the browser's
    // file window would stay open.
    const onUpload = vi.fn(() => new Promise<void>(() => {}));
    setup([], { onUpload });
    const file = new File(["hi"], "n.txt");
    const input = document.getElementById("document-file-input") as HTMLInputElement;

    expect(() => fireEvent.change(input, { target: { files: [file] } })).not.toThrow();

    expect(onUpload).toHaveBeenCalledTimes(1);
    // the input is reset at once, so the same file can be chosen again
    expect(input.value).toBe("");
  });

  it("the picker only offers the four accepted types", () => {
    setup([]);
    const input = document.getElementById("document-file-input") as HTMLInputElement;

    expect(input.accept).toBe(".pdf,.docx,.txt,.md");
  });

  it("shows an upload error and lets the user dismiss it", () => {
    const { onDismissError } = setup([], { error: "File is larger than 10 MB." });

    expect(screen.getByRole("alert").textContent).toContain("File is larger than 10 MB.");
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));

    expect(onDismissError).toHaveBeenCalled();
  });

  it("disables the upload button while a file is being sent", () => {
    setup([], { uploading: true });

    const button = screen.getByRole("button", { name: "Uploading…" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it("offers a retry when the list could not load", () => {
    const { onReload } = setup([], { status: "error" });

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(onReload).toHaveBeenCalled();
  });
});