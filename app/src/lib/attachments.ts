// Files and images attached to one question. Each file is sent to the server at once, as the
// bytes of one request streamed to disk (POST /api/ask/files; any size), and the question then
// names it by its id (operating/ask_uploads.py, operating/ask_attachments.py). The server
// decides what a file is from its bytes; a picture over the model's size is scaled down there.
import { removeAskFile, uploadAskFile } from "./api";

export const ACCEPT = ".pdf,.xlsx,.docx,.pptx,.csv,.txt,.md,.png,.jpg,.jpeg,.gif,.webp,application/pdf,text/csv,text/plain,image/png,image/jpeg,image/gif,image/webp,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.openxmlformats-officedocument.presentationml.presentation";

export interface Attachment {
  id: string;
  name: string;
  size: number;
  image: boolean;
  /** The id the server gave the file once it had it (what the Ask request carries). */
  upload: string;
}

const EXT = /\.(pdf|xlsx|docx|csv|txt|md|png|jpe?g|gif|webp)$/i;
const IMAGE = /^image\/(png|jpeg|gif|webp)$/;

export function sizeWords(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Why a file cannot be attached, or null when it can (its kind only: a file has no size limit). */
export function refusal(file: { name: string; size: number; type: string }, have: { size: number }[]): string | null {
  const image = IMAGE.test(file.type);
  if (!image && !EXT.test(file.name)) return `${file.name}: attach a PDF, a Word file, a spreadsheet (.xlsx, .csv), text or an image.`;
  return null;
}

/** Take a file off a question: the server forgets what it kept. */
export function dropFile(file: Attachment): void {
  void removeAskFile(file.upload).catch(() => undefined);
}

/** `files` sent to the server and added to `have`; what was left out, in words. */
export async function addFiles(files: Iterable<File>, have: Attachment[]): Promise<{ next: Attachment[]; problems: string[] }> {
  const next = [...have];
  const problems: string[] = [];
  for (const file of files) {
    // a pasted screenshot arrives as "image.png"; name it plainly
    const name = file.name && file.name !== "image.png" ? file.name : `Pasted image ${next.length + 1}.png`;
    const why = refusal({ name, size: file.size, type: file.type }, next);
    if (why) {
      problems.push(why);
      continue;
    }
    try {
      const sent = await uploadAskFile(file, name);
      next.push({ id: `${Date.now()}-${next.length}`, name, size: file.size, image: IMAGE.test(file.type), upload: sent.id });
    } catch {
      problems.push(`${name} could not be sent: attach it again.`);
    }
  }
  return { next, problems };
}
