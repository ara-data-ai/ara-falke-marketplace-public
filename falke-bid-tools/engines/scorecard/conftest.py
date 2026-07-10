"""Root conftest: ensure the skill package is importable without install.

Adds the skill root to sys.path so `import scorecard` works when running
`pytest` from the skill directory or via the project tree.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
