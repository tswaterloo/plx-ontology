// Files and images attached to one question (operating/ask_attachments.py reads them on the
// server, in memory, for that answer only). The browser checks the same limits first so a
// person hears at once what will not be sent; the server decides again from the bytes.

export const MAX_FILES = 5;
export const MAX_BYTES = 8 * 1024 * 1024;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
export const MAX_TOTAL = 10 * 1024 * 1024;

export const ACCEPT = ".pdf,.xlsx,.docx,.csv,.txt,.md,.png,.jpg,.jpeg,.gif,.webp,application/pdf,text/csv,text/plain,image/png,image/jpeg,image/gif,image/webp,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.openxmlformats-officedocument.wordprocessingml.document";

export interface Attachment {
  id: string;
  name: string;
  size: number;
  image: boolean;
  /** The file's bytes, base64 (what the Ask request carries). */
  data: string;
}

const EXT = /\.(pdf|xlsx|docx|csv|txt|md|png|jpe?g|gif|webp)$/i;
const IMAGE = /^image\/(png|jpeg|gif|webp)$/;

export function sizeWords(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** Why a file cannot be attached beside `have`, or null when it can. */
export function refusal(file: { name: string; size: number; type: string }, have: { size: number }[]): string | null {
  const image = IMAGE.test(file.type);
  if (!image && !EXT.test(file.name)) return `${file.name}: attach a PDF, a Word file, a spreadsheet (.xlsx, .csv), text or an image.`;
  if (have.length >= MAX_FILES) return `Up to ${MAX_FILES} attachments a question.`;
  const limit = image ? MAX_IMAGE_BYTES : MAX_BYTES;
  if (file.size > limit) return `${file.name} is larger than ${limit / (1024 * 1024)} MB.`;
  if (have.reduce((n, f) => n + f.size, 0) + file.size > MAX_TOTAL) return `Attachments together can be up to ${MAX_TOTAL / (1024 * 1024)} MB.`;
  return null;
}

function base64Of(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).replace(/^data:[^,]*,/, ""));
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

/** `files` added to `have` within the limits; what was left out, in words. */
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
      next.push({ id: `${Date.now()}-${next.length}`, name, size: file.size, image: IMAGE.test(file.type), data: await base64Of(file) });
    } catch {
      problems.push(`${name} could not be read.`);
    }
  }
  return { next, problems };
}
