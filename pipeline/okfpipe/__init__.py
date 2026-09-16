"""okfpipe -- a repo-agnostic Python hygiene / knowledge / benchmark-task pipeline.

Three stages, each independently runnable:

    stage 1  okfpipe.hygiene    pin deps, containerize, generate tests, lint
    stage 2  okfpipe.knowledge  emit repo_graph.json + .okf/ knowledge layer
    stage 3  okfpipe.tasksrc    mine + build + validate benchmark tasks

Nothing in this package may hard-code the identity of a particular repository.
Every repo-specific value is discovered at runtime by okfpipe.hygiene.detect.
"""

__version__ = "1.0.0"

# Bumping this invalidates cached artifacts written by earlier runs.
SCHEMA_VERSION = "okf/1"
