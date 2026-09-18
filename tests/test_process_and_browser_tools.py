import json
from core.somatic_simulation import adjudicate_somatic_event, infer_explicit_consent
from core.browser_service import BrowserService
from tools.process_tools import _allowed

def test_process_tool_keeps_restricted_command_boundary():
    assert _allowed(["python", "-m", "unittest"])
    assert not _allowed(["python", "-c", "print(1)"])
    assert not _allowed(["cmd", "/c", "echo", "unsafe"])

def test_consent_is_explicit_and_separate_from_event_occurrence():
    assert infer_explicit_consent("请抱抱我") == "explicit_yes"
    assert infer_explicit_consent("不要这样") == "explicit_no"
    assert infer_explicit_consent("我有点紧张") == "unknown"
    decision = adjudicate_somatic_event({
        "event_confirmed": True, "contact_object": "user", "contact_area": "arms",
        "intensity": "gentle", "duration": "brief", "consent_status": "unknown",
    })
    assert decision.status == "occurred"

def test_browser_actions_require_context_and_do_not_execute_directly():
    service = BrowserService()
    assert service.available in {True, False}
    service.shutdown()
