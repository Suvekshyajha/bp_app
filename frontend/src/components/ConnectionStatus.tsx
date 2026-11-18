import type { ConnectionStatus as Status } from "../types";

const labels: Record<Status, string> = {
  connecting: "Connecting",
  connected: "Connected",
  reconnecting: "Reconnecting",
  offline: "Offline",
};

export function ConnectionStatus({ status }: { status: Status }) {
  return (
    <div className={"connection-status connection-status--" + status} role="status" aria-live="polite">
      <span className="connection-dot" aria-hidden="true" />
      <span>{labels[status]}</span>
    </div>
  );
}
