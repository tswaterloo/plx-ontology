#!/bin/sh
# refuse the capture backup when the mail capture DB is far smaller than the backed-up copy (a re-formatted /data with fresh small stores)
live=$(stat -c %s /data/plx-state/mail-capture/capture.db 2>/dev/null || echo 0)
[ "$live" -gt 5000000000 ] || exit 1
exit 0
