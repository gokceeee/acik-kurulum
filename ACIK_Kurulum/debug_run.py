import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

try:
    from acik_onboarding.app import run
    run(ROOT)
except Exception:
    traceback.print_exc()
    input("Hata olustu, devam etmek icin Enter'a basin...")
