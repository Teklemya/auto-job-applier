import pytest

from auto_job_applier.profile import ProfileError, load_profile


def test_example_profile_loads(example_profile_data, write_profile):
    profile = load_profile(write_profile(example_profile_data))

    # Owner facts the whole system depends on (R11)
    assert profile.work_authorization.authorized_to_work_us is True
    assert profile.work_authorization.requires_sponsorship_now is False
    assert profile.work_authorization.requires_sponsorship_future is False
    # Shadow mode is on by default (S1)
    assert profile.settings.submit is False
    assert profile.preferences.salary_floor == 120_000
    assert profile.resume_path.exists()


def test_missing_email_is_a_clear_error(example_profile_data, write_profile):
    del example_profile_data["contact"]["email"]
    with pytest.raises(ProfileError) as exc:
        load_profile(write_profile(example_profile_data))
    assert "contact.email" in str(exc.value)


def test_nonexistent_resume_is_a_clear_error(example_profile_data, write_profile):
    example_profile_data["resume_path"] = "/nope/resume.pdf"
    with pytest.raises(ProfileError) as exc:
        load_profile(write_profile(example_profile_data))
    assert "resume_path" in str(exc.value)
    assert "/nope/resume.pdf" in str(exc.value)


def test_bad_email_is_rejected(example_profile_data, write_profile):
    example_profile_data["contact"]["email"] = "not-an-email"
    with pytest.raises(ProfileError, match="contact.email"):
        load_profile(write_profile(example_profile_data))


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "missing.yaml")


def test_unknown_keys_are_rejected(example_profile_data, write_profile):
    # Typos like `requires_sponsership_future` must not silently fall back to a default.
    example_profile_data["work_authorization"]["requires_sponsership_future"] = False
    with pytest.raises(ProfileError, match="requires_sponsership_future"):
        load_profile(write_profile(example_profile_data))
