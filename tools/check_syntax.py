# Syntax + import check for the whole addon.  This is deliberately NOT a test
# suite: it only proves every module parses and that the package imports inside
# a real Blender.  Behaviour is covered by tests/*.py.
#
# Usage: blender --background --factory-startup --python tools/check_syntax.py

import ast
import os
import sys

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(WS, "material_bakery")

bad = []
count = 0
for root, dirs, files in os.walk(PACKAGE):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for name in sorted(files):
        if not name.endswith(".py"):
            continue
        path = os.path.join(root, name)
        count += 1
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        try:
            ast.parse(source, filename=path)
        except SyntaxError as exc:
            bad.append("{}: line {}: {}".format(os.path.relpath(path, WS), exc.lineno,
                                                exc.msg))
print("parsed {} file(s)".format(count))
for entry in bad:
    print("  SYNTAX", entry)

sys.path.insert(0, WS)
try:
    import material_bakery
except Exception as exc:
    print("  IMPORT FAILED: {}: {}".format(type(exc).__name__, exc))
    raise
print("package imported")

failed = bool(bad)
print("SYNTAX CHECKS " + ("FAILED" if failed else "PASSED"))
if failed:
    sys.exit(1)
