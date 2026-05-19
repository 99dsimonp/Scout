import tempfile
import unittest

from scout.runtime_lock import RuntimeLock, RuntimeLockError


class RuntimeLockTests(unittest.TestCase):
    def test_runtime_lock_rejects_second_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            with RuntimeLock(tmp):
                with self.assertRaises(RuntimeLockError):
                    with RuntimeLock(tmp):
                        pass

            with RuntimeLock(tmp):
                pass


if __name__ == "__main__":
    unittest.main()
