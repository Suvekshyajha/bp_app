import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Notice } from "../hooks/useDocuments";
import { Toasts } from "./toasts";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

const success: Notice = { id: 1, kind: "success", message: '"a.pdf" was uploaded and is ready for questions.' };
const failure: Notice = { id: 2, kind: "error", message: '"b.pdf" could not be processed.' };

describe("Toasts", () => {
  it("renders nothing when there is nothing to say", () => {
    const { container } = render(<Toasts notices={[]} onDismiss={vi.fn()} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows a success as a status and a failure as an alert", () => {
    render(<Toasts notices={[success, failure]} onDismiss={vi.fn()} />);

    expect(screen.getByRole("status").textContent).toContain("ready for questions");
    expect(screen.getByRole("alert").textContent).toContain("could not be processed");
  });

  it("the close button dismisses that notice", () => {
    const onDismiss = vi.fn();
    render(<Toasts notices={[success]} onDismiss={onDismiss} />);

    fireEvent.click(screen.getByRole("button", { name: "Dismiss notification" }));

    expect(onDismiss).toHaveBeenCalledWith(1);
  });

  it("goes away by itself: a success quickly, a failure later", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(<Toasts notices={[success, failure]} onDismiss={onDismiss} />);

    act(() => void vi.advanceTimersByTime(5000));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledWith(1);

    act(() => void vi.advanceTimersByTime(5000));
    expect(onDismiss).toHaveBeenCalledTimes(2);
    expect(onDismiss).toHaveBeenCalledWith(2);
  });
});