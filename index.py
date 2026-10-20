import sys
from pathlib import Path

# Make sure the project root is in the Python path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import create_app

app = create_app(start_workers=False, seed_demo=True)
