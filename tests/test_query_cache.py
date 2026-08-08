import os
import unittest
from dataclasses import replace
from pathlib import Path

from tests._support import TempWorkspace
from videohelpersuite.path_policy import (
    AuthorizedPath,
    PathAccessDenied,
    PathCapability,
)
from videohelpersuite.query_cache import AuthorizedMetadataCache


class _Policy:
    def __init__(self, *, denied=False):
        self.denied = denied
        self.calls = 0

    def reauthorize_path(self, authorized):
        self.calls += 1
        if self.denied:
            raise PathAccessDenied(authorized.capability, "test denial")
        return replace(authorized, canonical=os.path.realpath(authorized.canonical))


class AuthorizedMetadataCacheTests(unittest.TestCase):
    def setUp(self):
        self.workspace = TempWorkspace()

    def tearDown(self):
        self.workspace.cleanup()

    def _authorized(self, path):
        return AuthorizedPath(
            requested=str(path),
            canonical=os.path.realpath(path),
            capability=PathCapability.PREVIEW_MEDIA,
            root_id="output",
        )

    def _file(self, name, content=b"x"):
        path = self.workspace.path / name
        path.write_bytes(content)
        return path

    def test_hit_reauthorizes_and_returns_an_isolated_copy(self):
        path = self._file("clip.mp4")
        policy = _Policy()
        cache = AuthorizedMetadataCache(max_entries=2)
        source = {"frames": 10, "size": [64, 48]}

        cache.put(self._authorized(path), source, policy)
        source["frames"] = 999
        hit = cache.get(self._authorized(path), policy)
        hit["size"][0] = 999

        self.assertEqual(cache.get(self._authorized(path), policy), {"frames": 10, "size": [64, 48]})
        self.assertEqual(policy.calls, 3)

    def test_size_or_mtime_change_invalidates_entry(self):
        path = self._file("clip.mp4")
        policy = _Policy()
        cache = AuthorizedMetadataCache(max_entries=2)
        authorized = self._authorized(path)
        cache.put(authorized, {"frames": 1}, policy)

        path.write_bytes(b"changed-size")

        self.assertIsNone(cache.get(authorized, policy))
        self.assertEqual(len(cache), 0)

    def test_denied_hit_is_invalidated_and_never_returned(self):
        path = self._file("clip.mp4")
        cache = AuthorizedMetadataCache(max_entries=2)
        cache.put(self._authorized(path), {"frames": 1}, _Policy())

        with self.assertRaises(PathAccessDenied):
            cache.get(self._authorized(path), _Policy(denied=True))

        self.assertEqual(len(cache), 0)

    def test_lru_bound_evicts_oldest_entry(self):
        paths = [self._file(f"clip-{index}.mp4") for index in range(3)]
        policy = _Policy()
        cache = AuthorizedMetadataCache(max_entries=2)

        for index, path in enumerate(paths):
            cache.put(self._authorized(path), {"frames": index}, policy)

        self.assertEqual(len(cache), 2)
        self.assertIsNone(cache.get(self._authorized(paths[0]), policy))
        self.assertEqual(cache.get(self._authorized(paths[1]), policy)["frames"], 1)

    @unittest.skipUnless(os.name == "nt", "Windows case-folded identity semantics")
    def test_windows_case_variants_share_one_enforcement_identity(self):
        path = self._file("CaseClip.mp4")
        policy = _Policy()
        cache = AuthorizedMetadataCache(max_entries=2)
        authorized = self._authorized(path)
        cache.put(authorized, {"frames": 1}, policy)

        variant = replace(authorized, canonical=authorized.canonical.swapcase())

        self.assertEqual(cache.get(variant, policy), {"frames": 1})
        self.assertEqual(len(cache), 1)


if __name__ == "__main__":
    unittest.main()
