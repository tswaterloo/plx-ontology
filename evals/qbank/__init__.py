"""Question bank: thousands of generated and mined Ask questions with automatic truth.

Generated items (``families``) are natural-language templates with many surface forms,
instantiated on real entities sampled from the served Postgres (stratified by activity so
rare entities appear) and answered by their own read-only truth SQL; mined items
(``mine``) are questions staff asked each other in Teams and mail whose subject is a
record, answered from that record (never from the reply text). ``bank`` splits every item
by hash into TUNE and HOLDOUT and refreshes truth; ``runner`` samples a stratified set,
runs the live Ask path on it and grades it with ``grade``.

Templates and code carry no entity, value or answer: every entity comes from the data at
build time and every answer from the truth SQL at refresh time. The bank itself (questions,
truth) lives outside the repository (``QBANK_DIR``, default ``/data/scratch/qbank``).
"""
