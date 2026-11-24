"""The model's context window as the Ask counts it, defined once (guardrail 4).

The window is the one bound a read may stop at (owner 11-23): what is longer is paged, never
dropped, and the answer says how many pages it took. ``CONTEXT_CHARS`` is what one call holds
of reads (characters, about 100,000 tokens); ``PROMPT_FILES_CHARS`` is the share of a question's
first prompt that the person's attached files may take, so the question and the Ask's own reads
keep room: a file's text past it is kept as the person's file and read on with
``plx.read_document`` (offset pages on, find searches it)."""
CONTEXT_CHARS = 400_000
PROMPT_FILES_CHARS = CONTEXT_CHARS // 2
