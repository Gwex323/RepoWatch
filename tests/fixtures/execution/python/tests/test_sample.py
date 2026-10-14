import os
import unittest
from pathlib import Path


class IsolationTest(unittest.TestCase):
    def test_runtime(self):
        self.assertEqual(os.getuid(), 1000)
        self.assertNotIn("GITHUB_TOKEN", os.environ)
        self.assertFalse(Path("/var/run/docker.sock").exists())
        self.assertFalse(Path("/workspace/project/.git").exists())
        with self.assertRaises(OSError):
            Path("/root-host-write").write_text("forbidden")
        Path("/workspace/home/retained").write_text("fixture")
