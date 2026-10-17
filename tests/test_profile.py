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


def test_load_targeting_works_without_a_complete_profile(write_profile):
    from auto_job_applier.profile import load_targeting

    t = load_targeting(write_profile({"targeting": {"hubs": ["Omaha"]}}))
    assert t.hubs == ["Omaha"]
    assert "Olathe, KS" in t.kc_metro  # defaults kept


def test_load_targeting_defaults_when_profile_missing(tmp_path):
    from auto_job_applier.profile import load_targeting

    assert "Austin, TX" in load_targeting(tmp_path / "missing.yaml").hubs


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string\n"])
def test_non_mapping_yaml_is_a_clear_error(tmp_path, text):
    from auto_job_applier.profile import load_targeting

    path = tmp_path / "profile.yaml"
    path.write_text(text)
    with pytest.raises(ProfileError, match="mapping"):
        load_targeting(path)
    with pytest.raises(ProfileError, match="mapping"):
        load_profile(path)


def test_relative_resume_path_resolves_under_aja_home(
    aja_home, example_profile_data, write_profile
):
    aja_home.mkdir(parents=True)
    (aja_home / "resume.pdf").write_bytes(b"%PDF")
    example_profile_data["resume_path"] = "resume.pdf"
    profile = load_profile(write_profile(example_profile_data))
    assert profile.resume_path == aja_home / "resume.pdf"


def test_fit_and_llm_defaults(example_profile_data, write_profile):
    profile = load_profile(write_profile(example_profile_data))
    assert profile.settings.fit_skip == ["mismatch"]
    assert profile.llm.fit.model == "gemini-3.5-flash-lite"
    assert profile.llm.fit.daily_call_budget == 450
    assert profile.llm.answers.model == "gemini-3.8-flash"
    assert profile.llm.answers.daily_call_budget == 18
    assert profile.llm.answers.min_interval_seconds == 12.5  # 5 requests/minute
    assert profile.llm.fit_batch_size == 8


def test_unknown_fit_verdict_is_a_clear_error(example_profile_data, write_profile):
    example_profile_data["settings"]["fit_skip"] = ["mismach"]
    with pytest.raises(ProfileError) as exc:
        load_profile(write_profile(example_profile_data))
    assert "settings.fit_skip" in str(exc.value)
