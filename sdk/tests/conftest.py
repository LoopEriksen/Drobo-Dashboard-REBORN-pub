"""Let the suites import drobo_nasd when run straight from this directory."""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
