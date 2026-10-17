"""LLM access (D7, D10): Gemini Flash on Google's free tier, behind a small interface.

`BudgetedLLM` wraps any provider with the free-tier guards: calls spaced `min_interval`
apart, a daily call budget kept in the store (so it holds across runs), and one backoff retry
on a 429. After that the quota counts as exhausted and callers must wait or stage a draft,
never guess.
"""

import os
import time
from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from .profile import Profile
    from .store import JobStore

Attachment = tuple[bytes, str]  # (data, mime type)


class LLMError(Exception):
    """Misconfiguration or unusable output."""


class RateLimited(Exception):
    """The provider answered 429, or 5xx (overloaded). Worth one retry after a backoff."""


class QuotaExhausted(Exception):
    """No more calls today: the daily budget is used up or the provider keeps refusing."""


class LLM(Protocol):
    def generate[M: BaseModel](
        self,
        *,
        system: str,
        prompt: str,
        schema: type[M],
        attachments: list[Attachment] | None = None,
    ) -> M: ...


class GeminiProvider:
    def __init__(self, model: str, thinking: str | None = None, client=None):
        if client is None:
            if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
                raise LLMError(
                    "GEMINI_API_KEY is not set. Create a free key at "
                    "https://aistudio.google.com/apikey and export GEMINI_API_KEY=..."
                )
            from google import genai

            client = genai.Client()
        self.model, self.thinking, self.client = model, thinking, client

    def generate[M: BaseModel](
        self,
        *,
        system: str,
        prompt: str,
        schema: type[M],
        attachments: list[Attachment] | None = None,
    ) -> M:
        from google.genai import errors, types

        contents = [types.Part.from_bytes(data=d, mime_type=m) for d, m in attachments or []]
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.3,
            thinking_config=(
                types.ThinkingConfig(thinking_level=self.thinking.upper())
                if self.thinking
                else None
            ),
        )
        try:
            resp = self.client.models.generate_content(
                model=self.model, contents=[*contents, prompt], config=config
            )
        except errors.APIError as e:
            if e.code == 429 or e.code >= 500:
                raise RateLimited(f"Gemini {e.code}: {e.message or e.status}") from e
            raise LLMError(f"Gemini error {e.code}: {e}") from e
        if isinstance(resp.parsed, schema):
            return resp.parsed
        try:
            return schema.model_validate_json(resp.text or "")
        except ValidationError as e:
            raise LLMError(f"Gemini returned unusable output: {e}") from e


class BudgetedLLM:
    def __init__(
        self,
        provider: LLM,
        store: "JobStore",
        *,
        name: str,  # usage is counted per model: free quotas are per model
        daily_budget: int,
        min_interval: float,
        backoff: float = 60,  # one RPM window
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Callable[[], str] = lambda: date.today().isoformat(),
    ):
        self.provider, self.store, self.name = provider, store, name
        self.daily_budget, self.min_interval, self.backoff = daily_budget, min_interval, backoff
        self.clock, self.sleep, self.today = clock, sleep, today
        self._last: float | None = None

    def generate[M: BaseModel](self, **kw) -> M:
        for attempt in range(2):
            day = self.today()
            if self.store.llm_usage(day, self.name)[0] >= self.daily_budget:
                raise QuotaExhausted(
                    f"daily budget of {self.daily_budget} calls to {self.name} used"
                )
            if self._last is not None:
                wait = self._last + self.min_interval - self.clock()
                if wait > 0:
                    self.sleep(wait)
            self._last = self.clock()
            try:
                result = self.provider.generate(**kw)
            except RateLimited as e:
                self.store.count_llm_call(day, self.name, rate_limited=True)
                if attempt == 0:
                    self.sleep(self.backoff)
                    continue
                raise QuotaExhausted(f"{e} (still failing after a retry)") from None
            except Exception:
                self.store.count_llm_call(day, self.name)  # failed calls still use quota
                raise
            self.store.count_llm_call(day, self.name)
            return result
        raise AssertionError("unreachable")


def make_llm(
    profile: "Profile", store: "JobStore", role: Literal["fit", "answers"]
) -> "BudgetedLLM":
    s = profile.llm.fit if role == "fit" else profile.llm.answers
    return BudgetedLLM(
        GeminiProvider(s.model, s.thinking),
        store,
        name=s.model,
        daily_budget=s.daily_call_budget,
        min_interval=s.min_interval_seconds,
    )
