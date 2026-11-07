#!/usr/bin/env python3
"""Regenerate the local ingest config from a checkout's examples/ingest_all_sources.json.
Paths become absolute (relative to examples/); mail scope: history window, OCR deferred; portal every 2 h."""
import json, os, sys
repo = sys.argv[1]; out_dir = sys.argv[2]
ex = os.path.join(repo, "examples")
d = json.load(open(os.path.join(ex, "ingest_all_sources.json")))
def absify(v):
    p = os.path.normpath(os.path.join(ex, v))
    return p if os.path.exists(p) else v
for k in ("capture_audience", "mirror_audience"):
    if isinstance(d.get(k), str): d[k] = absify(d[k])
for sec in ("mail", "sharepoint", "sharepoint_lists", "teams", "bc", "portal", "mc"):
    s = d.get(sec)
    if isinstance(s, dict):
        for k, v in list(s.items()):
            if isinstance(v, str) and v.endswith(".json"): s[k] = absify(v)
mail_scope = json.load(open(d["mail"]["scope"]))
mail_scope["history"] = "window"; mail_scope["ocr"] = "deferred"
ms = os.path.join(out_dir, "mail_scope.local.json"); json.dump(mail_scope, open(ms, "w"), indent=1)
d["mail"]["scope"] = ms
d["portal"]["min_interval_seconds"] = 7200
cfg = os.path.join(out_dir, "ingest_all_main.local.json"); json.dump(d, open(cfg, "w"), indent=1)
print(cfg)
