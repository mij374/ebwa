"""Re-stamp static/fonts/*.woff2 with a content hash, and fix the CSS.

A url() inside a static stylesheet cannot carry a Jinja-computed token
the way asset_version() gives one to the stylesheet itself, so the token
lives in the FILENAME instead. nginx serves /static with `expires 30d`,
which is only safe while a changed file means a changed URL.

Run after adding or replacing a font file:

    python tools/hash-fonts.py

It is idempotent: a file whose name already carries the right hash is
left alone, and only names that actually move are rewritten in the CSS.
"""
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS = os.path.join(ROOT, "static", "fonts")
CSS = os.path.join(ROOT, "static", "css", "style.css")
STAMPED = re.compile(r"^(?P<stem>.+?)(?:\.(?P<hash>[0-9a-f]{8}))?\.woff2$")


def main():
    css = open(CSS, encoding="utf-8").read()
    moved, unchanged = [], 0
    for name in sorted(os.listdir(FONTS)):
        m = STAMPED.match(name)
        if not m:
            continue
        data = open(os.path.join(FONTS, name), "rb").read()
        token = hashlib.sha256(data).hexdigest()[:8]
        wanted = "%s.%s.woff2" % (m.group("stem"), token)
        if name == wanted:
            unchanged += 1
            continue
        os.rename(os.path.join(FONTS, name), os.path.join(FONTS, wanted))
        css = css.replace("../fonts/%s" % name, "../fonts/%s" % wanted)
        moved.append((name, wanted))
    if moved:
        open(CSS, "w", encoding="utf-8", newline="").write(css)
    print("%d font file(s) already stamped correctly." % unchanged)
    for old, new in moved:
        print("  %s -> %s" % (old, new))
    missing = [f for f in os.listdir(FONTS)
               if f.endswith(".woff2") and f not in css]
    if missing:
        print("\nNOT REFERENCED BY THE STYLESHEET (dead files?):")
        for f in missing:
            print("  ", f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
