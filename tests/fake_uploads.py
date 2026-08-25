"""Real files in static/uploads for tests that seed image rows.

A ContentImage row and the file it names are two different things, and
since the missing-file fix the site treats a row with no file as what it
is: not a photograph. A fixture that inserts `seed0.png` without ever
writing one is therefore testing a broken attachment, which is not what
those files mean to be testing.

`hold(names)` writes them and gives them back on exit, so a killed run
leaves nothing behind that a later run would trip over. The names are
prefixed on the way in, so a fixture can call itself `seed0.png` without
any chance of colliding with a real upload in a developer's own
static/uploads.
"""
import contextlib
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
UPLOAD_DIR = os.path.join(ROOT, "static", "uploads")
SOURCE = os.path.join(ROOT, "static", "img", "ebwa-logo.png")


def write(names):
    """Put a real (small, valid) image at each of these filenames."""
    made = []
    for name in names:
        if not name:
            continue
        path = os.path.join(UPLOAD_DIR, name)
        if os.path.isfile(path):
            continue
        shutil.copyfile(SOURCE, path)
        made.append(path)
    return made


def remove(paths):
    for path in paths:
        if os.path.isfile(path):
            os.remove(path)


def fill_dangling():
    """Write a file for EVERY image reference in the test database.

    Reuses the site's own dangling-reference finder, so a fixture can
    invent whatever filenames it likes and they all become real without
    this helper knowing the patterns. Call it once, after the fixtures
    are committed, inside an app context; pass the result to remove() in
    teardown.
    """
    from app import dangling_uploads
    return write(sorted({row[4] for row in dangling_uploads()}))


@contextlib.contextmanager
def hold(names):
    """Write these files for the life of the block, then take them away."""
    made = write(names)
    try:
        yield
    finally:
        remove(made)
