import tempfile
import unittest
from pathlib import Path

from runtime_cleanup import cleanup_runtime_artifacts


class RuntimeCleanupTests(unittest.TestCase):
    def test_only_known_runtime_artifact_directories_are_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            artifact_dirs = [
                root / "runtime_debug",
                root / "runtime_media_cache",
                root / "debug_run_video",
            ]
            for index, directory in enumerate(artifact_dirs):
                directory.mkdir()
                (directory / f"artifact-{index}.bin").write_bytes(b"1234")

            unrelated = root / "images"
            unrelated.mkdir()
            (unrelated / "template.png").write_bytes(b"keep")

            result = cleanup_runtime_artifacts(root)

            self.assertEqual(result, {"directories": 3, "files": 3, "bytes": 12})
            self.assertTrue(unrelated.is_dir())
            self.assertTrue((unrelated / "template.png").is_file())
            self.assertTrue(all(not directory.exists() for directory in artifact_dirs))


if __name__ == "__main__":
    unittest.main()
