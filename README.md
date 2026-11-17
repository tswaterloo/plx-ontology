# PLX Workbench

The Workbench is one place where people at PLX can see their work, ask questions about the
business and get answers they can check. It can also prepare an email or a change for you.
Nothing is sent or changed until a person says yes.

## What it is

PLX keeps its information in many programs. Orders are in one, invoices in another, email
in a third and spreadsheets somewhere else. To answer a plain question such as "why is
this order late?", someone used to open five programs and piece the story together by hand.

The Workbench does that reading for you. It keeps a copy of what those programs hold, notes
exactly where each piece came from, and connects the pieces that belong together. This
email is about that order. That order is for this customer. This batch came from that
supplier.

When it tells you something, it shows you where it found it, so you never have to take its
word for it.

## Who it is for

It is for anyone at PLX who deals with customers, suppliers or the shop floor.

Owners and managers see what is stuck across the company and what each team is
waiting on. They can ask about any customer, order or supplier and get a short answer with
the sources attached.

Customer service and sales staff can see where an order is, what was promised and what the
customer last wrote. A reply is usually ready to send.

Purchasing sees which purchase orders are running late and what the supplier last said
about the ship date. A reminder to the supplier can be drafted in one step.

Production and quality staff can follow what goes into a product, which batch went where,
the test results, and the paperwork a shipment needs.

Finance sees invoices and payments matched to the orders they belong to.

## Where the information comes from

The Workbench only reads from these programs. It does not change anything in them on its
own.

```mermaid
flowchart LR
    A["Orders, stock and invoices<br/>Business Central, the Portal"] --> W
    A2["Sage and FileMaker,<br/>read through the Portal<br/>reporting service"] --> W
    B["Email, Teams chats,<br/>calendars"] --> W
    C["Files and lists<br/>SharePoint"] --> W
    D["Tasks and approvals<br/>Mission Control, Planner"] --> W
    E["The company staff list"] --> W
    W(("Workbench")) --> Y["One picture,<br/>with every source shown"]
```

Most of these programs are connected today. A few more, such as meeting transcripts and some
shipping and online sales services, are prepared but not switched on yet.

## Getting started

You can use the Workbench in a web browser or in the desktop app for Windows and Mac. Both
show the same pages. The desktop app can be downloaded from the Help page in the
Workbench. It is not yet signed by Microsoft or Apple, so your computer may ask you to
confirm the first time you open it.

For now you sign in with a private access link that the Workbench team sends you. Signing
in with your normal Microsoft work account will replace the links later.

Once you are in, a typical morning looks like this:

1. The My work page lists what needs you today, from every program, in one place. For example,
   a purchase order that is two days late and a customer email asking for a delivery date.
2. You open the late purchase order. Its page shows the supplier, the lines, the recent
   emails about it, and when the ship date last moved.
3. On the Ask page you type "what did the supplier last say about this order?" The answer quotes
   the email and links to it.
4. You type "ask them for a new ship date". A draft appears. You change what you like and
   confirm it, or leave it.

Every customer, order, product, batch and supplier has its own page with the documents
that belong to it. A product's page lists its formula down to the raw materials and who
supplies them. The Meetings page shows your calendar, with a short preparation sheet for
each meeting.

You can also tell the Workbench how you like things done, such as your usual sign-off or a
phrase you never use, and your drafts will follow it. The Memory page shows everything it
remembers about you, and you can correct or remove any of it. If you send the same kind of
reply or document often, save it as a template and new drafts will start from it.

## Asking a question

You type a question the way you would ask a colleague. Before you see the answer, the
Workbench checks every fact in it against the record it came from. A fact that does not
match is taken out, and the answer tells you what it could not find.

```mermaid
flowchart LR
    Q["You ask a question"] --> R["It reads only the records<br/>you are allowed to see"]
    R --> A["It writes an answer"]
    A --> C{"Does each fact match<br/>its record?"}
    C -- yes --> S["You get the answer,<br/>with a link for each fact"]
    C -- no --> D["That fact is removed and<br/>the answer says what is missing"]
    D --> S
```

## Autopilot

Some work comes up again and again. A supplier sends a new ship date, a customer emails a
purchase order, a payment arrives. An autopilot notices these and prepares the next step
for you, written the way you write, and puts it in My work.

Today every autopilot only prepares work. A person reads it and decides.

An autopilot can only be allowed to do more after it has been checked on at least 15 real
cases from recent days it had not been tuned on, and found right at least 9 times out of 10. Even then, it only does so for you if you
switch it on yourself. You can stop all autopilots at once at any time.

Some things never count as routine and always need a person: anything about money, prices
or credit, who can see or do what, new customers or suppliers, and matters about people.
Every step an autopilot takes is written down in a log you can read.

```mermaid
flowchart LR
    E["Something happens:<br/>an email, an order, a payment"] --> P["The autopilot<br/>prepares the next step"]
    P --> M{"Has it passed its check,<br/>and have you switched it on?"}
    M -- no --> H["It waits in My work<br/>for you to decide"]
    M -- yes --> G["It goes through the same<br/>checks as a person would"]
    H --> L["Written in the log"]
    G --> L
```

## Keeping information private

Two rules decide what anyone sees.

First, a small number of people at PLX are left out completely. Any email, chat, file or
meeting they are part of is dropped before the Workbench reads it, so it is never stored
or searched. If the Workbench cannot tell whether an item involves one of them, it does not
read the item.

Second, you only see what you could already open in the original program. If a record
exists that you are not allowed to open, the Workbench may tell you that something is
hidden, but never what it is. Hidden records are never used to shape your answers either.

## Behind the scenes

The Workbench keeps reading the connected programs throughout the day, so a change usually
shows up within minutes. Everything it reads is kept in one database on a company server,
together with a note of where each piece came from and who may see it. Updates to the
Workbench are switched in with a restart of under half a minute.

## Questions and feedback

If something looks wrong or could be better, choose Give feedback in your account menu
(or Help, then Give feedback, in the desktop app) and say so in a sentence or two. It goes
straight to the team that builds the Workbench, and the Feedback page shows what you have
sent. For access or a new access link, ask your manager or the person who set up your
account.

---

Engineers: architecture, code layout, running, testing and releases are in
[docs/TECHNICAL.md](docs/TECHNICAL.md).
