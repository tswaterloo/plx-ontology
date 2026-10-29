# How to use the PLX Ontology console

## 1. Start it

1. Open a terminal in this folder.
2. To try it with sample cases (no PLX data, no network), type:

   ```
   python3 -m operator_ui.tests.case_fixtures --fixture --seed-fixtures \
       --state-dir /tmp/plx-demo --principal staff:operations
   ```

   Use a new, empty folder for `--state-dir`.
3. It prints a web address. Open that address in your browser.

On the PLX machine the console runs over real data:
`python3 -m operator_ui.case_console --state-dir <state folder>` plus the access files
your administrator set up.

The console only runs on your own computer. It cannot be opened from another machine.

## 2. Ask a question

1. Pick a case or an object (a customer, an order, a lot).
2. Read the answer. Each line shows where it came from (Portal, Business Central,
   an email or a document). Click the source to check it.
3. If something is missing, the console says **missing** or **blocked** and why.
   That is on purpose: it never fills gaps with guesses.

## 3. What it will not do

- It will not change anything in Portal, Business Central, email or Teams.
- It will not approve or send anything. Approvals stay with a person.
- It only shows what your permissions allow.

## 4. Something looks wrong?

Write it down in [`FEEDBACK.md`](FEEDBACK.md): what you asked, what you expected,
and what you saw.
