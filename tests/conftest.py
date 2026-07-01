import sys
from pathlib import Path

# Make the single-file `sunlog` module importable from tests.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
