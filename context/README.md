# Context

The product's working context lives here, in one place. Read it before changing anything
a user sees.

| File | What it is | Who updates it |
|---|---|---|
| [`OVERVIEW.md`](OVERVIEW.md) | A plain-language picture tour of a working day | Whoever changes a workflow |
| [`HOW-TO-USE.md`](HOW-TO-USE.md) | Step-by-step guide, simple enough for anyone | Whoever changes what a user sees |
| [`RELEASE-NOTES.md`](RELEASE-NOTES.md) | What changed in each release and what must keep working | Every release, before it ships |

## Discipline

1. **No release without a release note.** Say what changed, what was fixed, and what
   earlier behaviour must still work. Before changing something, read the notes so a
   new version does not break the last one.
2. **No user-facing change without updating the how-to guide** in the same change.
3. **Feedback is collected in the app** (Give feedback, and the Feedback page). Feedback
   that arrives another way, by email, in a meeting or in person, is entered there too.
4. Keep it short and current. Remove what is no longer true.
