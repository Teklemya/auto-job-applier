import pytest
from google.genai import errors
from pydantic import BaseModel

from auto_job_applier import llm
from auto_job_applier.store import JobStore


class Answer(BaseModel):
    value: str


class FakeProvider:
    """Returns queued results; an Exception in the queue is raised instead."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def generate(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


@pytest.fixture
def store(tmp_path):
    s = JobStore(tmp_path / "jobs.db")
    yield s
    s.close()


def _budgeted(provider, store, clock, budget=5, interval=4.5):
    return llm.BudgetedLLM(
        provider,
        store,
        name="m",
        daily_budget=budget,
        min_interval=interval,
        backoff=60,
        clock=clock,
        sleep=clock.sleep,
        today=lambda: "2026-10-17",
    )


def _ask(b):
    return b.generate(system="s", prompt="p", schema=Answer)


def test_calls_are_spaced_by_min_interval(store):
    clock = FakeClock()
    b = _budgeted(FakeProvider(Answer(value="a"), Answer(value="b")), store, clock)
    assert _ask(b).value == "a"
    clock.now += 1.0  # 1s of other work
    assert _ask(b).value == "b"
    assert clock.slept == [pytest.approx(3.5)]


def test_daily_budget_stops_runaway_loops(store):
    clock = FakeClock()
    provider = FakeProvider(*[Answer(value="x")] * 10)
    b = _budgeted(provider, store, clock, budget=3)
    for _ in range(3):
        _ask(b)
    with pytest.raises(llm.QuotaExhausted, match="budget"):
        _ask(b)
    assert len(provider.calls) == 3


def test_budget_is_shared_across_runs_via_the_store(store):
    clock = FakeClock()
    _ask(_budgeted(FakeProvider(Answer(value="x")), store, clock, budget=1))
    with pytest.raises(llm.QuotaExhausted):
        _ask(_budgeted(FakeProvider(Answer(value="x")), store, clock, budget=1))
    assert store.llm_usage("2026-10-17", "m") == (1, 0)


def test_rate_limit_retries_once_after_backoff(store):
    clock = FakeClock()
    provider = FakeProvider(llm.RateLimited("429"), Answer(value="ok"))
    assert _ask(_budgeted(provider, store, clock)).value == "ok"
    assert 60 in clock.slept
    assert store.llm_usage("2026-10-17", "m") == (2, 1)


def test_persistent_rate_limit_is_quota_exhausted(store):
    clock = FakeClock()
    provider = FakeProvider(llm.RateLimited("429"), llm.RateLimited("429"))
    with pytest.raises(llm.QuotaExhausted, match="429"):
        _ask(_budgeted(provider, store, clock))
    assert len(provider.calls) == 2


# --- Gemini provider, against a fake google-genai client (never the real API) ---


class _Resp:
    def __init__(self, parsed=None, text=""):
        self.parsed, self.text = parsed, text


class _Models:
    def __init__(self, result):
        self.result, self.kwargs = result, None

    def generate_content(self, **kw):
        self.kwargs = kw
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Client:
    def __init__(self, result):
        self.models = _Models(result)


def test_gemini_requests_structured_output_with_attachment():
    client = _Client(_Resp(parsed=Answer(value="hi")))
    g = llm.GeminiProvider(model="gemini-test", client=client)
    out = g.generate(
        system="be brief", prompt="q", schema=Answer, attachments=[(b"%PDF", "application/pdf")]
    )
    assert out == Answer(value="hi")
    kw = client.models.kwargs
    assert kw["model"] == "gemini-test"
    assert kw["config"].response_schema is Answer
    assert kw["config"].response_mime_type == "application/json"
    assert kw["config"].system_instruction == "be brief"
    assert kw["contents"][0].inline_data.mime_type == "application/pdf"
    assert kw["contents"][-1] == "q"


def test_gemini_falls_back_to_parsing_text():
    g = llm.GeminiProvider(model="m", client=_Client(_Resp(parsed=None, text='{"value": "t"}')))
    assert g.generate(system="s", prompt="p", schema=Answer) == Answer(value="t")


def test_gemini_429_is_rate_limited():
    err = errors.ClientError(
        429, {"error": {"code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED"}}
    )
    g = llm.GeminiProvider(model="m", client=_Client(err))
    with pytest.raises(llm.RateLimited):
        g.generate(system="s", prompt="p", schema=Answer)


def test_gemini_bad_output_is_llm_error():
    g = llm.GeminiProvider(model="m", client=_Client(_Resp(parsed=None, text="not json")))
    with pytest.raises(llm.LLMError):
        g.generate(system="s", prompt="p", schema=Answer)


def test_missing_api_key_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="GEMINI_API_KEY"):
        llm.GeminiProvider(model="m")


@pytest.mark.parametrize("code", [500, 503, 504])
def test_gemini_overload_is_rate_limited(code):
    err = errors.ServerError(code, {"error": {"code": code, "message": "high demand"}})
    g = llm.GeminiProvider(model="m", client=_Client(err))
    with pytest.raises(llm.RateLimited):
        g.generate(system="s", prompt="p", schema=Answer)


def test_budgets_are_per_model(store):
    clock = FakeClock()
    lite = llm.BudgetedLLM(
        FakeProvider(Answer(value="x")),
        store,
        name="lite",
        daily_budget=1,
        min_interval=0,
        clock=clock,
        sleep=clock.sleep,
        today=lambda: "2026-10-17",
    )
    flash = llm.BudgetedLLM(
        FakeProvider(Answer(value="y")),
        store,
        name="flash",
        daily_budget=1,
        min_interval=0,
        clock=clock,
        sleep=clock.sleep,
        today=lambda: "2026-10-17",
    )
    _ask(lite)
    assert _ask(flash).value == "y"  # lite's budget doesn't touch flash's


def test_gemini_thinking_level_is_passed():
    client = _Client(_Resp(parsed=Answer(value="hi")))
    llm.GeminiProvider(model="m", thinking="minimal", client=client).generate(
        system="s", prompt="p", schema=Answer
    )
    assert client.models.kwargs["config"].thinking_config.thinking_level.value == "MINIMAL"


def test_gemini_no_thinking_config_by_default():
    client = _Client(_Resp(parsed=Answer(value="hi")))
    llm.GeminiProvider(model="m", client=client).generate(system="s", prompt="p", schema=Answer)
    assert client.models.kwargs["config"].thinking_config is None


def test_make_llm_uses_the_roles_model(example_profile_data, write_profile, store, monkeypatch):
    from auto_job_applier.profile import load_profile

    monkeypatch.setenv("GEMINI_API_KEY", "test")
    profile = load_profile(write_profile(example_profile_data))
    fit_llm, answers_llm = (
        llm.make_llm(profile, store, "fit"),
        llm.make_llm(profile, store, "answers"),
    )
    assert fit_llm.provider.model == profile.llm.fit.model == "gemini-3.5-flash-lite"
    assert answers_llm.provider.model == profile.llm.answers.model == "gemini-3.8-flash"
    assert fit_llm.daily_budget == 450 and answers_llm.daily_budget == 18


def test_failed_calls_still_count_against_the_budget(store):
    clock = FakeClock()
    b = _budgeted(FakeProvider(llm.LLMError("bad output")), store, clock)
    with pytest.raises(llm.LLMError):
        _ask(b)
    assert store.llm_usage("2026-10-17", "m") == (1, 0)


def test_quota_day_follows_pacific_time():
    from datetime import UTC, datetime

    # 1am Eastern on the 18th is still the 17th in California, where Google resets quotas
    assert llm.quota_day(datetime(2026, 10, 18, 5, 0, tzinfo=UTC)) == "2026-10-17"
    assert llm.quota_day(datetime(2026, 10, 18, 8, 0, tzinfo=UTC)) == "2026-10-18"
