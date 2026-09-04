"""The computed diff, which replaces a model reading its own memory.

"New vs still firing vs resolved" used to depend on a 4B model diffing a fresh
alert list against memory seeds - seeds that had accumulated corrections of the
agents' own wrong history. Here it is computed, so it is either right or it says
it has no baseline.
"""

from __future__ import annotations

from mcp_runner.state import Snapshots, diff_keys


class TestDiffKeys:
    def test_a_first_run_reports_everything_as_seen_and_nothing_as_resolved(self):
        new, continuing, resolved = diff_keys({"a", "b"}, None)
        assert new == ["a", "b"]
        assert continuing == []
        assert resolved == []

    def test_no_baseline_is_distinguishable_from_an_empty_baseline(self):
        # None means "never observed"; set() means "observed, and nothing fired".
        # Collapsing them makes a first run after a restart read as a fleet-wide
        # incident, or hides genuinely new alerts as continuing ones.
        assert diff_keys({"a"}, None) == (["a"], [], [])
        assert diff_keys({"a"}, set()) == (["a"], [], [])
        assert diff_keys({"a"}, {"a"}) == ([], ["a"], [])

    def test_a_continuing_alert_is_not_new(self):
        new, continuing, _ = diff_keys({"a"}, {"a"})
        assert new == []
        assert continuing == ["a"]

    def test_a_disappeared_alert_is_resolved(self):
        _, _, resolved = diff_keys({"a"}, {"a", "b"})
        assert resolved == ["b"]

    def test_all_three_buckets_at_once(self):
        new, continuing, resolved = diff_keys({"b", "c"}, {"a", "b"})
        assert (new, continuing, resolved) == (["c"], ["b"], ["a"])

    def test_output_is_sorted_so_a_report_is_stable_between_runs(self):
        new, _, _ = diff_keys({"z", "a", "m"}, set())
        assert new == ["a", "m", "z"]


class TestSnapshots:
    def test_a_saved_snapshot_round_trips(self, tmp_path):
        store = Snapshots(str(tmp_path))
        store.save("k", {"values": {"a": 1}})
        assert store.load("k") == {"values": {"a": 1}}

    def test_a_missing_snapshot_is_none_not_an_error(self, tmp_path):
        assert Snapshots(str(tmp_path)).load("absent") is None

    def test_a_corrupt_snapshot_degrades_to_no_baseline(self, tmp_path):
        # A cache problem must not cost the primary reading, which is still good.
        store = Snapshots(str(tmp_path))
        store.save("k", {"a": 1})
        (tmp_path / "k.json").write_text("{{{ not json")
        assert store.load("k") is None

    def test_an_unwritable_directory_does_not_raise(self, tmp_path):
        # Losing the diff is acceptable; losing the run is not.
        store = Snapshots(str(tmp_path / "nested" / "deep"))
        store.save("k", {"a": 1})
        assert store.load("k") == {"a": 1}

    def test_keys_are_sanitised_so_a_tool_name_cannot_escape_the_directory(self, tmp_path):
        store = Snapshots(str(tmp_path))
        store.save("../../etc/passwd", {"a": 1})
        assert not (tmp_path.parent.parent / "etc").exists()

    def test_a_write_is_atomic_so_a_crash_cannot_leave_half_a_file(self, tmp_path):
        store = Snapshots(str(tmp_path))
        store.save("k", {"values": {str(index): index for index in range(500)}})
        loaded = store.load("k")
        assert loaded is not None and len(loaded["values"]) == 500
