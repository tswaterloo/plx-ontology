# Context

All the context about this product lives here, in one place. People and agents read it
before changing anything. The more relevant context is here, the better the answers and
the fewer tokens an agent spends rediscovering it.

| File | What it is | Who updates it |
|---|---|---|
| [`HOW-TO-USE.md`](HOW-TO-USE.md) | Step-by-step guide, simple enough for anyone | Whoever changes what a user sees |
| [`RELEASE-NOTES.md`](RELEASE-NOTES.md) | What changed in each release and what must keep working | Every release, before it ships |
| [`FEEDBACK.md`](FEEDBACK.md) | All feedback, requests and scope asks, from every channel | Whoever receives feedback |

## Discipline

1. **No release without a release note.** Say what changed, what was fixed, and what
   earlier behaviour must still work. Before changing something, read the notes so a
   new version does not break the last one.
2. **No user-facing change without updating the how-to guide** in the same change.
3. **All feedback lands in `FEEDBACK.md`**, whatever the channel: the app, email,
   meetings or in person. If it is not written down here, it does not exist.
4. Keep it short and current. Remove what is no longer true.
