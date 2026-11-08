"""Edge 浏览连续打卡模块的独立契约测试。"""
from types import SimpleNamespace

import utils.edge_streak as edge_streak
from utils import tasks


SAMPLE_PROFILE_EMAIL = "target@example.invalid"


def test_edge_streak_exports_public_api_directly():
    assert edge_streak.EdgeStreakStatus.COMPLETED.value == "completed"
    assert edge_streak.EdgeStreakResult(edge_streak.EdgeStreakStatus.COMPLETED).success
    assert callable(edge_streak.do_edge_browsing_streak_detail)
    assert callable(edge_streak.do_edge_browsing_streak)
    assert callable(edge_streak.read_streak_progress)


def test_tasks_compatibility_exports_are_identical_to_edge_streak():
    assert tasks.EdgeStreakStatus is edge_streak.EdgeStreakStatus
    assert tasks.EdgeStreakResult is edge_streak.EdgeStreakResult
    assert tasks.EdgeStreakEntry is edge_streak.EdgeStreakEntry
    assert tasks.EdgeStreakActivation is edge_streak.EdgeStreakActivation
    assert tasks.do_edge_browsing_streak_detail is edge_streak.do_edge_browsing_streak_detail
    assert tasks.do_edge_browsing_streak is edge_streak.do_edge_browsing_streak
    assert tasks.valid_edge_progress is edge_streak.valid_edge_progress


def test_valid_edge_progress_is_consistent_across_import_paths():
    for progress in ((0, 30), (30, 30), (0, 0), (31, 30), None):
        assert (
            tasks.valid_edge_progress(progress)
            == edge_streak.valid_edge_progress(progress)
        )


def test_edge_profile_identity_is_available_from_both_import_paths(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text("{}", encoding="utf-8")
    (tmp_path / "Local State").write_text(
        '{"profile":{"info_cache":{"Default":{"user_name":"%s"}}}}'
        % SAMPLE_PROFILE_EMAIL,
        encoding="utf-8",
    )

    assert edge_streak.edge_profile_identity(tmp_path) == SAMPLE_PROFILE_EMAIL
    assert tasks.edge_profile_identity(tmp_path) == SAMPLE_PROFILE_EMAIL


def test_do_edge_browsing_streak_detail_returns_identity_mismatch(monkeypatch):
    page = SimpleNamespace(url="https://rewards.bing.com/dashboard")
    monkeypatch.setattr(
        edge_streak, "edge_profile_identity", lambda *_args: "other@example.invalid"
    )

    result = edge_streak.do_edge_browsing_streak_detail(
        page, expected_email=SAMPLE_PROFILE_EMAIL
    )

    assert result.status is edge_streak.EdgeStreakStatus.IDENTITY_MISMATCH
    assert result.identity == "other@example.invalid"
    assert result.progress is None
    assert result.success is False


def test_tasks_mobile_progress_uses_edge_streak_shared_reader(monkeypatch):
    calls = []

    def fake_reader(page, label):
        calls.append((page, label))
        return 1, 1

    monkeypatch.setattr(edge_streak, "read_streak_progress", fake_reader)
    page = SimpleNamespace()

    assert tasks.do_mobile_app_checkin(page) == 1
    assert calls == [(page, "mobile")]
