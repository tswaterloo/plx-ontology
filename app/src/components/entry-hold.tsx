"use client";

// An order or PO from create mode, held until each check is settled (answer.instruction.entry):
// every value the BC entry needs, cited from the records, a check, or confirmed by you. You
// pick the customer, supplier or item record (a suggestion or a search), or enter the
// quantity, or the price with its currency. Confirming writes nothing to BC: with no check
// left the entry goes to the write desk, where a colleague who holds the write grant
// approves it.

import { useState } from "react";
import type { ReactNode } from "react";
import type { EntryField, EntryHold as Hold, EntryOption, InstructWrite, Suggestion } from "@/lib/types";
import { confirmEntryValue, suggest } from "@/lib/api";
import { ApiError, plainError } from "@/lib/errors";
import { Button } from "./ui/button";
import { ShowAll } from "@/components/show-all";

const RECORD_TYPE: Partial<Record<EntryField["field"], string>> = { customer: "Customer", supplier: "Supplier", item: "Product" };
const INPUT = "min-w-0 rounded-[3px] border border-border bg-bg px-2 py-1 text-[13px]";

const problem = (err: unknown) => (err instanceof ApiError && err.detail && err.status < 500 ? err.detail : plainError(err));

function when(at: string | null | undefined): string {
  if (!at) return "";
  const d = new Date(at);
  return Number.isNaN(d.getTime()) ? at : d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function optionWords(o: EntryOption): string {
  const per = o.currency ? ` ${o.currency}${o.unit ? ` per ${o.unit}` : ""}` : o.unit ? ` ${o.unit}` : "";
  return `${o.value}${per} (${o.basis}${o.date ? `, ${o.date}` : ""})`;
}

/** The form that settles one check: a record picked, or a number (and currency) entered. */
function Settle({ entryId, f, onDone }: { entryId: string; f: EntryField; onDone: (h: Hold) => void }) {
  const type = RECORD_TYPE[f.field];
  const firstCurrency = (f.options ?? []).find((o) => o.currency)?.currency ?? "";
  const [value, setValue] = useState("");
  const [currency, setCurrency] = useState(firstCurrency);
  const [picked, setPicked] = useState<{ record: string; label: string } | null>(null);
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<Suggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const id = `entry-${entryId}-${f.key}`;

  async function search(q: string) {
    setQuery(q);
    if (!type || q.trim().length < 2) return setFound([]);
    try {
      const got = await suggest(q.trim(), undefined, 20);
      setFound(got.suggestions.filter((s) => s.type === type));
    } catch (err) {
      setError(problem(err));
    }
  }

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      const body = type ? { key: f.key, record: picked?.record ?? null } : { key: f.key, value: value.trim(), currency: f.field === "unit_price" ? currency.trim() : null };
      onDone(await confirmEntryValue(entryId, body));
    } catch (err) {
      setError(problem(err));
    } finally {
      setBusy(false);
    }
  }

  const ready = type ? !!picked : !!value.trim() && (f.field !== "unit_price" || !!currency.trim());
  return (
    <div className="mt-1 space-y-1.5" data-testid="entry-settle">
      {(f.options ?? []).length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {(f.options ?? []).map((o, i) => (
            <Button
              key={i}
              size="sm"
              variant="outline"
              disabled={busy}
              onClick={() => {
                if (type && o.record) setPicked({ record: o.record, label: o.value });
                else {
                  setValue(o.value);
                  if (o.currency) setCurrency(o.currency);
                }
              }}
            >
              Use {optionWords(o)}
            </Button>
          ))}
        </div>
      )}
      {type ? (
        <>
          <label className="sr-only" htmlFor={`${id}-search`}>
            Search {type === "Product" ? "items" : `${type.toLowerCase()}s`}
          </label>
          <input id={`${id}-search`} className={`${INPUT} w-64`} value={query} onChange={(e) => void search(e.target.value)} placeholder={`Search ${type === "Product" ? "items" : `${type.toLowerCase()}s`}`} />
          {found.length > 0 && (
            <ul className="text-[13px] leading-5" data-testid="entry-found">
              <ShowAll items={found} first={6} as="li">{(s) => (
                <li key={s.object_id}>
                  <button type="button" className="text-accent hover:underline" onClick={() => setPicked({ record: s.object_id, label: s.label })}>
                    {s.label}
                  </button>
                  {s.sublabel && <span className="text-muted"> {s.sublabel}</span>}
                </li>
              )}</ShowAll>
            </ul>
          )}
          {picked && <p className="text-[13px] leading-5">Picked: {picked.label}</p>}
        </>
      ) : (
        <div className="flex gap-1.5">
          <label className="sr-only" htmlFor={`${id}-value`}>
            {f.label}
          </label>
          <input id={`${id}-value`} className={`${INPUT} w-28`} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} placeholder={f.field === "unit_price" ? "Price" : "Quantity"} />
          {f.field === "unit_price" && (
            <>
              <label className="sr-only" htmlFor={`${id}-currency`}>
                Currency
              </label>
              <input id={`${id}-currency`} className={`${INPUT} w-16 uppercase`} maxLength={3} value={currency} onChange={(e) => setCurrency(e.target.value)} placeholder="CAD" />
            </>
          )}
        </div>
      )}
      <Button size="sm" disabled={busy || !ready} onClick={() => void confirm()}>
        Confirm {f.label.toLowerCase()}
      </Button>
      {error && <p className="text-[12px] leading-4 text-muted">{error}</p>}
    </div>
  );
}

function Status({ f }: { f: EntryField }) {
  if (f.status === "cited") return <span className="text-muted">From the records</span>;
  if (f.status === "confirmed") return <span>Confirmed by {f.confirmed_by ?? "you"}{f.confirmed_at ? `, ${when(f.confirmed_at)}` : ""}</span>;
  return <span className="text-muted">{f.reason}</span>;
}

export function EntryHold({ entry, renderWrite }: { entry: Hold; renderWrite: (w: InstructWrite) => ReactNode }) {
  const [hold, setHold] = useState(entry);
  const prepared = hold.write?.status === "prepared";
  return (
    <section className="space-y-2 border-l-2 border-accent pl-3" data-testid="entry-hold">
      <p className="text-[13px] leading-5 font-medium">{hold.label}</p>
      <p className="text-[12px] leading-4 text-muted">
        {prepared
          ? "Every check is settled. The entry is on the write desk; nothing is written to BC from here."
          : hold.open > 0
            ? `${hold.open} to settle. Confirming a value prepares the entry only; nothing is written to BC.`
            : "Every check is settled, but the entry could not go to the write desk:"}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[32rem] text-[13px] leading-5" data-testid="entry-fields">
          <thead className="text-left text-[12px] text-muted">
            <tr>
              <th className="pr-4 font-normal">Field</th>
              <th className="pr-4 font-normal">Value</th>
              <th className="font-normal">From</th>
            </tr>
          </thead>
          <tbody>
            {hold.fields.map((f) => (
              <tr key={f.key} data-status={f.status} className="align-top">
                <td className="pr-4">{f.label}</td>
                <td className={f.status === "check" ? "pr-4 text-muted" : "pr-4 font-medium"}>
                  {f.value ?? "—"}
                  {f.value && f.currency ? ` ${f.currency}` : ""}
                </td>
                <td className="text-[12px]">
                  <Status f={f} />
                  {!prepared && f.status !== "cited" && <Settle entryId={hold.entry_id} f={f} onDone={setHold} />}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {hold.write && renderWrite(hold.write)}
    </section>
  );
}
