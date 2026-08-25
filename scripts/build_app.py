#!/usr/bin/env python3
"""Inject members.json into template.html to produce the single-file app."""
import json, sys, pathlib
tpl = pathlib.Path('app/template.html').read_text()
data = pathlib.Path('app/members.json').read_text()
assert '</script' not in data, 'payload would break out of the script tag'
out = pathlib.Path('app/index.html')
out.write_text(tpl.replace('__DATA__', data))
print(f"wrote {out} ({out.stat().st_size:,} bytes)")
