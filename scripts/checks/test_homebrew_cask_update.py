#!/usr/bin/env python3
"""Protect the custom tap from Cask downgrades and same-version replacement."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from check_homebrew_cask_update import check_update  # noqa: E402


class HomebrewCaskUpdateTests(unittest.TestCase):
    def check_case(self, current: str | None, candidate: str, incoming: str) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current_path = root / "Casks/snapetech-pleazyng.rb"
            current_path.parent.mkdir()
            candidate_path = root / "candidate.rb"
            candidate_path.write_text(candidate)
            if current is not None:
                current_path.write_text(current)
            check_update(incoming, current_path, candidate_path)

    @staticmethod
    def cask(version: str, digest: str = "a" * 64) -> str:
        return f'cask "snapetech-pleazyng" do\n  version "{version}"\n  sha256 "{digest}"\nend\n'

    def test_newer_version_advances_tap(self) -> None:
        self.check_case(self.cask("3.0.49"), self.cask("3.0.51", "b" * 64), "3.0.51")

    def test_older_version_cannot_roll_back_tap(self) -> None:
        with self.assertRaisesRegex(ValueError, "roll the tap back"):
            self.check_case(self.cask("3.0.52"), self.cask("3.0.51"), "3.0.51")

    def test_same_version_is_allowed_only_when_bytes_match(self) -> None:
        candidate = self.cask("3.0.51")
        self.check_case(candidate, candidate, "3.0.51")
        with self.assertRaisesRegex(ValueError, "immutable Cask"):
            self.check_case(candidate, self.cask("3.0.51", "b" * 64), "3.0.51")

    def test_existing_cask_must_have_one_stable_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "unique stable version"):
            self.check_case('cask "snapetech-pleazyng" do\nend\n', self.cask("3.0.51"), "3.0.51")


if __name__ == "__main__":
    unittest.main()
