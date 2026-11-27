import type { ReactNode } from "react";

// Line icons shared by the whole app. They are decoration only: the button or
// the text next to them carries the meaning.
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.9"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

export const PlusIcon = () => (
  <Icon><path d="M12 5v14M5 12h14" /></Icon>
);

export const CloseIcon = () => (
  <Icon><path d="M6 6l12 12M18 6L6 18" /></Icon>
);

export const ChatIcon = () => (
  <Icon><path d="M21 11.5a8.4 8.4 0 0 1-12.3 7.4L3 21l2.1-5.7A8.4 8.4 0 1 1 21 11.5z" /></Icon>
);

export const TrashIcon = () => (
  <Icon><path d="M4 7h16M10 11v6M14 11v6M5.5 7l1 12a2 2 0 0 0 2 1.8h7a2 2 0 0 0 2-1.8l1-12M9 7V5a1.5 1.5 0 0 1 1.5-1.5h3A1.5 1.5 0 0 1 15 5v2" /></Icon>
);

export const LogoutIcon = () => (
  <Icon><path d="M9 21H5.5A1.5 1.5 0 0 1 4 19.5v-15A1.5 1.5 0 0 1 5.5 3H9M16 17l5-5-5-5M21 12H9" /></Icon>
);

export const MenuIcon = () => (
  <Icon><path d="M4 7h16M4 12h16M4 17h16" /></Icon>
);

export const FileIcon = () => (
  <Icon><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8zM14 3v5h5M9 13h6M9 17h4" /></Icon>
);

export const PaperclipIcon = () => (
  <Icon><path d="m21.4 11.1-8.5 8.5a5.5 5.5 0 0 1-7.8-7.8l8.5-8.5a3.7 3.7 0 0 1 5.2 5.2l-8.5 8.5a1.8 1.8 0 0 1-2.6-2.6l7.8-7.8" /></Icon>
);

export const UploadIcon = () => (
  <Icon><path d="M12 16V4M7 9l5-5 5 5M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" /></Icon>
);

export const ArrowUpIcon = () => (
  <Icon><path d="M12 19V5M6 11l6-6 6 6" /></Icon>
);

export const ArrowRightIcon = () => (
  <Icon><path d="M5 12h14M13 6l6 6-6 6" /></Icon>
);

export const StopIcon = () => (
  <Icon><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor" /></Icon>
);

export const InfoIcon = () => (
  <Icon><circle cx="12" cy="12" r="9" /><path d="M12 11v5M12 8h.01" /></Icon>
);

export const EyeIcon = () => (
  <Icon><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z" /><circle cx="12" cy="12" r="3" /></Icon>
);

export const EyeOffIcon = () => (
  <Icon><path d="M4 4l16 16M9.9 5.2A10.6 10.6 0 0 1 12 5c6.4 0 10 7 10 7a17.6 17.6 0 0 1-3 3.8M6.5 6.7C3.6 8.7 2 12 2 12s3.6 7 10 7a10 10 0 0 0 4.2-.9M9.9 9.9a3 3 0 0 0 4.2 4.2" /></Icon>
);
