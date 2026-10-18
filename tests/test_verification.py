"""Provider transport and verification boundaries; all external calls are mocked."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
import json

import httpx
import pytest

from backend import gemini_adapter
from backend.gemini_adapter import GeminiProvider, InvalidProviderResponse, ProviderUnavailable
from backend.verification import validate_evaluation, validate_extraction, validate_requirements


REQUIREMENTS = [
    {"id": "r1", "text": "Inspect every carton."},
    {"id": "r2", "text": "Stop the line if a leak is found."},
]
TRANSCRIPT = "I will inspect every carton. I will stop the line if a leak is found."
SECRET = "test-only-key-never-a-live-credential"


def evaluation():
    return {
        "requirements": [
            {"id": "r1", "status": "matched", "evidence": "I will inspect every carton."},
            {"id": "r2", "status": "matched", "evidence": "I will stop the line if a leak is found."},
        ],
        "complete": True,
        "extra_actions": [],
        "uncertainties": [],
        "summary": "Both requirements were explicitly retold.",
    }


def envelope(payload):
    return {"candidates": [{
        "finishReason": "STOP",
        "content": {"role": "model", "parts": [{"text": json.dumps(payload, ensure_ascii=False)}]},
    }]}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setenv("PROVIDER_PROCESSING_ENABLED", "true")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")


def provider_with(payload):
    def handler(request):
        return httpx.Response(200, json=envelope(payload))
    return GeminiProvider(transport=httpx.MockTransport(handler))


def test_generic_requirements_preserve_safety_equipment_and_stop_conditions():
    requirements = [
        {"id": "ppe", "text": "Wear the assigned face shield before opening the valve."},
        {"id": "stop", "text": "If pressure exceeds 2 bar, stop and call the supervisor."},
    ]
    result = validate_extraction({"requirements": requirements, "complete": True, "uncertainties": []})
    assert result["requirements"] == requirements


@pytest.mark.parametrize("invalid", [
    [], None, "inspect", ({"id": "r1", "text": "Inspect."},),
    [{"id": 1, "text": "Inspect."}],
    [{"id": "r1", "text": 12}],
    [{"id": "r1", "text": "   "}],
    [{"id": "r1", "text": "Inspect.", "critical": True}],
    [{"id": "r1", "text": "Inspect."}, {"id": "r1", "text": "Stop."}],
    [{"id": "r1", "text": "Inspect all."}, {"id": "r2", "text": " inspect ALL. "}],
    [{"id": "../r1", "text": "Inspect."}],
    [{"text": "Inspect."}],
])
def test_requirements_reject_empty_duplicates_extra_fields_and_coercion(invalid):
    with pytest.raises(InvalidProviderResponse):
        validate_requirements(invalid)


def test_complete_explicit_coverage_passes_without_mutating_inputs():
    raw = evaluation()
    before = deepcopy(raw)
    result = validate_evaluation(REQUIREMENTS, TRANSCRIPT, raw)
    assert result["passed"] is True and result["decision"] == "passed"
    assert raw == before and "passed" not in raw


@pytest.mark.parametrize("mutation", ["omitted", "duplicated", "unknown", "empty"])
def test_every_requirement_id_must_appear_exactly_once(mutation):
    raw = evaluation()
    if mutation == "omitted":
        raw["requirements"].pop()
    elif mutation == "duplicated":
        raw["requirements"][1]["id"] = "r1"
    elif mutation == "unknown":
        raw["requirements"][1]["id"] = "other"
    else:
        raw["requirements"] = []
    with pytest.raises(InvalidProviderResponse, match="cover|invalid"):
        validate_evaluation(REQUIREMENTS, TRANSCRIPT, raw)


@pytest.mark.parametrize("mutation", ["bool_string", "extra_flag", "extra_row_field", "status", "null_issues"])
def test_model_cannot_author_approval_flags_or_use_coerced_and_extra_fields(mutation):
    raw = evaluation()
    if mutation == "bool_string":
        raw["complete"] = "true"
    elif mutation == "extra_flag":
        raw["passed"] = True
    elif mutation == "extra_row_field":
        raw["requirements"][0]["confidence"] = 1.0
    elif mutation == "status":
        raw["requirements"][0]["status"] = "approved"
    else:
        raw["uncertainties"] = None
    with pytest.raises(InvalidProviderResponse):
        validate_evaluation(REQUIREMENTS, TRANSCRIPT, raw)


@pytest.mark.parametrize("evidence", ["", "   ", "Inspect every carton.", "The worker agreed."])
def test_matched_evidence_must_quote_the_actual_transcript(evidence):
    raw = evaluation()
    raw["requirements"][0]["evidence"] = evidence
    with pytest.raises(InvalidProviderResponse):
        validate_evaluation(REQUIREMENTS, TRANSCRIPT, raw)


@pytest.mark.parametrize("mutation,decision", [
    ("incomplete", "review_required"), ("uncertainty", "review_required"),
    ("uncertain_row", "review_required"), ("extra_action", "discrepancy"),
    ("missing", "discrepancy"), ("contradiction", "discrepancy"),
])
def test_uncertainty_missing_and_extra_actions_block_passing(mutation, decision):
    raw = evaluation()
    if mutation == "incomplete":
        raw["complete"] = False
    elif mutation == "uncertainty":
        raw["uncertainties"] = ["The term for the valve is ambiguous."]
    elif mutation == "extra_action":
        raw["extra_actions"] = ["The worker also plans to restart the line."]
    else:
        raw["requirements"][0]["status"] = "uncertain" if mutation == "uncertain_row" else mutation
        raw["requirements"][0]["evidence"] = ""
    result = validate_evaluation(REQUIREMENTS, TRANSCRIPT, raw)
    assert result["passed"] is False and result["decision"] == decision


@pytest.mark.parametrize("name,value", [
    ("GEMINI_API_KEY", ""), ("GEMINI_API_KEY", "<YOUR_GEMINI_API_KEY>"),
    ("GEMINI_API_KEY", "your_api_key"), ("PROVIDER_PROCESSING_ENABLED", "false"),
    ("PROVIDER_PROCESSING_ENABLED", "1"), ("GEMINI_MODEL", ""),
    ("GEMINI_MODEL", "../evil?key=oops"), ("GEMINI_MODEL", "gemini-placeholder"),
])
def test_incomplete_configuration_never_calls_network_or_local_parser(configured, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    calls = []
    def handler(request):
        calls.append(request)
        raise AssertionError("Network must not be called")
    from backend import engine
    monkeypatch.setattr(engine, "extract_local", lambda _: pytest.fail("No local fallback is permitted"))
    provider = GeminiProvider(transport=httpx.MockTransport(handler))
    assert provider.configured is False
    with pytest.raises(ProviderUnavailable):
        asyncio.run(provider.extract_requirements("Inspect every carton."))
    assert calls == []


def test_request_uses_key_header_fixed_host_strict_schema_and_no_key_in_url(configured):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "generativelanguage.googleapis.com"
        assert request.url.path == "/v1beta/models/gemini-test-model:generateContent"
        assert not request.url.query and SECRET not in str(request.url)
        assert request.headers["x-goog-api-key"] == SECRET
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        schema = body["generationConfig"]["responseJsonSchema"]
        assert schema["additionalProperties"] is False
        assert "passed" not in schema["properties"]
        assert body["generationConfig"]["candidateCount"] == 1
        assert body["contents"][0]["role"] == "user"
        assert "negation" in body["systemInstruction"]["parts"][0]["text"]
        return httpx.Response(200, json=envelope(evaluation()))
    result = asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))
    assert result["passed"] and len(calls) == 1


def test_transcription_uses_original_audio_and_server_response_only(configured):
    original_audio = b"mock-container-bytes-not-real-audio"
    spoken = "हर कार्टन की जाँच करूँगा।"
    def handler(request):
        body = json.loads(request.content)
        parts = body["contents"][0]["parts"]
        assert len(parts) == 1 and set(parts[0]) == {"inline_data"}
        assert parts[0]["inline_data"]["mime_type"] == "audio/webm"
        assert base64.b64decode(parts[0]["inline_data"]["data"]) == original_audio
        return httpx.Response(200, json=envelope({"transcript": spoken, "speech_detected": True, "uncertainties": []}))
    provider = GeminiProvider(transport=httpx.MockTransport(handler))
    assert asyncio.run(provider.transcribe(original_audio, "audio/webm;codecs=opus")) == spoken


@pytest.mark.parametrize("transcription", [
    {"transcript": "", "speech_detected": False, "uncertainties": []},
    {"transcript": "possibly twelve", "speech_detected": True, "uncertainties": ["Number unclear"]},
    {"transcript": "inspect", "speech_detected": "true", "uncertainties": []},
    {"transcript": "inspect", "speech_detected": True, "uncertainties": [], "passed": True},
    {"transcript": "   ", "speech_detected": True, "uncertainties": []},
])
def test_no_speech_uncertain_or_invalid_transcription_requires_rerecording(configured, transcription):
    with pytest.raises(InvalidProviderResponse):
        asyncio.run(provider_with(transcription).transcribe(b"mock-audio", "audio/wav"))


@pytest.mark.parametrize("audio,mime", [(b"", "audio/wav"), ("fake-bytes", "audio/wav"), (b"x", "text/plain")])
def test_invalid_audio_is_rejected_before_transmission(configured, audio, mime):
    def handler(request):
        pytest.fail("Invalid recording must not be transmitted")
    with pytest.raises(InvalidProviderResponse):
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).transcribe(audio, mime))


def test_provider_negation_fixture_returns_discrepancy_despite_shared_words(configured):
    transcript = "I will not inspect every carton. I will stop the line if a leak is found."
    raw = evaluation()
    raw["requirements"][0] = {"id": "r1", "status": "contradiction", "evidence": "I will not inspect every carton."}
    raw["summary"] = "The worker explicitly negated the required inspection."
    result = asyncio.run(provider_with(raw).evaluate(REQUIREMENTS, transcript))
    assert result["decision"] == "discrepancy" and result["passed"] is False


@pytest.mark.parametrize("status", [301, 400, 401, 429, 500, 503])
def test_http_failures_are_sanitized_and_never_fall_back_or_retry(configured, status):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET + TRANSCRIPT, headers={"location": "https://example.com/leak"})
    with pytest.raises(ProviderUnavailable) as exc:
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))
    assert SECRET not in str(exc.value) and TRANSCRIPT not in str(exc.value)
    assert len(calls) == 1


def test_transport_error_and_whole_request_timeout_are_sanitized(configured, monkeypatch):
    def error(request):
        raise httpx.ConnectError(SECRET + TRANSCRIPT, request=request)
    with pytest.raises(ProviderUnavailable) as exc:
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(error)).evaluate(REQUIREMENTS, TRANSCRIPT))
    assert SECRET not in str(exc.value) and TRANSCRIPT not in str(exc.value)
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope(evaluation()))
    monkeypatch.setattr(gemini_adapter, "REQUEST_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(ProviderUnavailable, match="failed"):
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(slow)).evaluate(REQUIREMENTS, TRANSCRIPT))


@pytest.mark.parametrize("mutation", ["no_candidates", "two_candidates", "truncated", "blocked", "thought", "tool", "invalid_json", "duplicate_json", "nonfinite", "not_object", "bad_role"])
def test_malformed_blocked_or_truncated_model_output_fails_closed(configured, mutation):
    raw = envelope(evaluation())
    candidate = raw["candidates"][0]
    if mutation == "no_candidates":
        raw["candidates"] = []
    elif mutation == "two_candidates":
        raw["candidates"].append(deepcopy(candidate))
    elif mutation == "truncated":
        candidate["finishReason"] = "MAX_TOKENS"
    elif mutation == "blocked":
        raw["promptFeedback"] = {"blockReason": "SAFETY"}
    elif mutation == "thought":
        candidate["content"]["parts"][0]["thought"] = True
    elif mutation == "tool":
        candidate["content"]["parts"] = [{"functionCall": {"name": "approve", "args": {}}}]
    elif mutation == "bad_role":
        candidate["content"]["role"] = "user"
    else:
        candidate["content"]["parts"][0]["text"] = {
            "invalid_json": "```json\n{}\n```", "duplicate_json": '{"complete":true,"complete":false}',
            "nonfinite": '{"complete": NaN}', "not_object": "[]",
        }[mutation]
    def handler(request):
        return httpx.Response(200, json=raw)
    with pytest.raises(InvalidProviderResponse):
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))


def test_provider_rejects_missing_coverage_instead_of_manufacturing_a_match(configured):
    raw = evaluation()
    raw["requirements"].pop()
    with pytest.raises(InvalidProviderResponse, match="exactly once"):
        asyncio.run(provider_with(raw).evaluate(REQUIREMENTS, TRANSCRIPT))


def test_response_byte_limit_and_non_json_content_type_are_enforced(configured, monkeypatch):
    monkeypatch.setattr(gemini_adapter, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(InvalidProviderResponse, match="size limit"):
        asyncio.run(provider_with(evaluation()).evaluate(REQUIREMENTS, TRANSCRIPT))
    def handler(request):
        return httpx.Response(200, text="<html>" + SECRET + "</html>")
    with pytest.raises(InvalidProviderResponse) as exc:
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))
    assert SECRET not in str(exc.value)


def test_provider_does_not_repair_invalid_extraction(configured):
    raw = {"requirements": REQUIREMENTS, "complete": "true", "uncertainties": []}
    with pytest.raises(InvalidProviderResponse):
        asyncio.run(provider_with(raw).extract_requirements("Inspect every carton. Stop for leaks."))


@pytest.mark.parametrize("injected", [{"permission_to_work": True}, {"decision": "passed"}])
def test_provider_cannot_add_permission_or_server_decision_fields(configured, injected):
    """Exercise the HTTP boundary: a plausible model result cannot grant authority."""
    raw = evaluation()
    raw.update(injected)
    with pytest.raises(InvalidProviderResponse, match="verification response is invalid"):
        asyncio.run(provider_with(raw).evaluate(REQUIREMENTS, TRANSCRIPT))


def test_mixed_language_negation_fixture_preserves_evidence_and_blocks_pass(configured):
    """This is a response-boundary fixture, not a real-model language benchmark."""
    transcript = "मैं हर carton inspect नहीं करूँगा। leak मिलते ही line रोक दूँगा।"
    raw = evaluation()
    raw["requirements"] = [
        {"id": "r1", "status": "contradiction", "evidence": "मैं हर carton inspect नहीं करूँगा।"},
        {"id": "r2", "status": "matched", "evidence": "leak मिलते ही line रोक दूँगा।"},
    ]
    raw["summary"] = "Inspection was negated; the stop condition was retold."
    result = asyncio.run(provider_with(raw).evaluate(REQUIREMENTS, transcript))
    assert result["passed"] is False and result["decision"] == "discrepancy"
    assert result["requirements"] == raw["requirements"]


def test_response_disconnect_discards_complete_looking_first_chunk_and_closes_stream(configured):
    """Do not accept a result before the bounded HTTP stream completes normally."""
    calls = []

    class InterruptedStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield json.dumps(envelope(evaluation())).encode("utf-8")
            raise httpx.ReadError(SECRET + TRANSCRIPT)

        async def aclose(self):
            self.closed = True

    stream = InterruptedStream()

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

    with pytest.raises(ProviderUnavailable) as exc:
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))
    assert stream.closed and len(calls) == 1
    assert SECRET not in str(exc.value) and TRANSCRIPT not in str(exc.value)


def test_caller_cancellation_propagates_and_closes_inflight_provider_stream(configured):
    """Cancelled work must not turn into a passing result or an automatic retry."""
    async def scenario():
        paused = asyncio.Event()
        calls = []

        class PausedStream(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                yield b'{"candidates":['
                paused.set()
                await asyncio.Event().wait()

            async def aclose(self):
                self.closed = True

        stream = PausedStream()

        def handler(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=stream)

        provider = GeminiProvider(transport=httpx.MockTransport(handler))
        pending = asyncio.create_task(provider.evaluate(REQUIREMENTS, TRANSCRIPT))
        try:
            await asyncio.wait_for(paused.wait(), timeout=1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert stream.closed and len(calls) == 1
        finally:
            if not pending.done():
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending

    asyncio.run(scenario())


def test_optional_pacing_spaces_provider_calls_without_retrying(configured, monkeypatch):
    monkeypatch.setenv("GEMINI_MIN_INTERVAL_SECONDS", "0.3")
    times = []
    def handler(request):
        times.append(asyncio.get_event_loop().time())
        return httpx.Response(429, text="quota")
    provider = GeminiProvider(transport=httpx.MockTransport(handler))
    async def twice():
        for _ in range(2):
            with pytest.raises(ProviderUnavailable):
                await provider.evaluate(REQUIREMENTS, TRANSCRIPT)
    asyncio.run(twice())
    assert len(times) == 2 and times[1] - times[0] >= 0.29


def test_provider_schema_inlines_references_and_drops_only_list_caps():
    from backend.gemini_adapter import provider_schema
    from backend.verification import Evaluation
    original = Evaluation.model_json_schema()
    sent = provider_schema(original)
    text = json.dumps(sent)
    assert "$ref" not in text and "$defs" not in text and "maxItems" not in text
    assert sent["additionalProperties"] is False and "passed" not in sent["properties"]
    item = sent["properties"]["requirements"]["items"]
    assert item["properties"]["status"]["enum"] == ["matched", "contradiction", "missing", "uncertain"]
    assert item["additionalProperties"] is False
    assert "$defs" in original  # the strict local model is unchanged


def test_vertex_backend_uses_service_account_token_and_project_endpoint(configured, monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT", "echo-project-1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "runtime-token", "expires_in": 3600})
        assert request.url.host == "aiplatform.googleapis.com"
        assert request.url.path == "/v1/projects/echo-project-1/locations/global/publishers/google/models/gemini-test-model:generateContent"
        assert request.headers["Authorization"] == "Bearer runtime-token"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200, json=envelope(evaluation()))
    provider = GeminiProvider(transport=httpx.MockTransport(handler))
    assert provider.configured
    async def twice():
        await provider.evaluate(REQUIREMENTS, TRANSCRIPT)
        return await provider.evaluate(REQUIREMENTS, TRANSCRIPT)
    assert asyncio.run(twice())["passed"]
    assert [call.url.host for call in calls].count("metadata.google.internal") == 1  # token cached


def test_vertex_backend_fails_closed_without_credentials(configured, monkeypatch):
    monkeypatch.setenv("GEMINI_BACKEND", "vertex")
    monkeypatch.setenv("VERTEX_PROJECT", "echo-project-1")
    def handler(request):
        return httpx.Response(404, text="no metadata")
    with pytest.raises(ProviderUnavailable):
        asyncio.run(GeminiProvider(transport=httpx.MockTransport(handler)).evaluate(REQUIREMENTS, TRANSCRIPT))
    monkeypatch.setenv("GEMINI_BACKEND", "other")
    assert not GeminiProvider().configured
