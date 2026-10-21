"""Run all regression tests inside Blender's Python environment."""

from pathlib import Path
import sys
import unittest


if __name__ == '__main__':
    tests = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests))
    suite = unittest.defaultTestLoader.discover(str(tests), pattern='test_*.py')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise RuntimeError('G-code importer regression checks failed')
