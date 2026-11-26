// Early, friendly checks before a file is sent. The server checks again, so
// these only save the user a round trip. Keep them in line with the
// orchestrator's MAX_UPLOAD_MB and ALLOWED_EXTENSIONS.
const configured = Number(import.meta.env.VITE_MAX_UPLOAD_MB);

export const MAX_UPLOAD_MB =
  Number.isFinite(configured) && configured > 0 ? configured : 10;

export const ACCEPTED_EXTENSIONS = [".pdf", ".docx", ".txt", ".md"];

// The server accepts at most 5 document ids per message
export const MAX_SELECTED_DOCUMENTS = 5;

// Returns a message when the file should not be sent, or null when it is fine
export function checkFile(file: { name: string; size: number }): string | null {
  const name = file.name.toLowerCase();

  if (!ACCEPTED_EXTENSIONS.some((extension) => name.endsWith(extension))) {
    return "Only .pdf, .docx, .txt and .md files are accepted.";
  }
  if (file.size === 0) return "The file is empty.";
  if (file.size > MAX_UPLOAD_MB * 1024 * 1024) {
    return `File is larger than ${MAX_UPLOAD_MB} MB.`;
  }
  return null;
}
