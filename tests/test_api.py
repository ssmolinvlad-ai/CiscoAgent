import asyncio
import json

import httpx
import pytest

from ptagent.documents import Assignment
from ptagent.models import LabPlan
from ptagent.planner import APIPlanner


def test_api_receives_vision_and_environment_key(monkeypatch):
    monkeypatch.setenv("LAB_AI_ENDPOINT","https://example.invalid/v1/chat/completions")
    monkeypatch.setenv("LAB_AI_KEY","only-test-key")
    monkeypatch.setenv("LAB_AI_MODEL","configured-vision-model")
    payload={"devices":[{"name":"PC1","kind":"pc","model":"PC-PT"}],"checks":[{"id":"p1","description":"ping","kind":"ping","device":"PC1","target":"127.0.0.1"}]}
    seen=[]
    def handle(request):
        seen.append(json.loads(request.content))
        assert request.headers["authorization"]=="Bearer only-test-key"
        return httpx.Response(200,json={"choices":[{"message":{"content":json.dumps(payload)}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    plan=asyncio.run(APIPlanner().plan(Assignment("assignment",[b"png-bytes"])))
    assert isinstance(plan,LabPlan)
    assert seen[0]["model"]=="configured-vision-model"
    assert seen[0]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_ai_error_body_cannot_leak_key(monkeypatch):
    monkeypatch.setenv("LAB_AI_ENDPOINT","https://example.invalid/v1/chat/completions")
    monkeypatch.setenv("LAB_AI_KEY","private-value")
    monkeypatch.setenv("LAB_AI_MODEL","configured-model")
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(lambda r:httpx.Response(401,text="private-value")),**kwargs))
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(APIPlanner().plan(Assignment("test")))
    assert "private-value" not in str(caught.value)


def test_invalid_ai_plan_has_one_bounded_correction(monkeypatch):
    monkeypatch.setenv("LAB_AI_ENDPOINT","https://example.invalid/v1/chat/completions")
    monkeypatch.setenv("LAB_AI_KEY","test-key")
    monkeypatch.setenv("LAB_AI_MODEL","configured-model")
    calls=[]
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200,json={"choices":[{"message":{"content":"{}"}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,"AsyncClient",lambda **kwargs:original(transport=httpx.MockTransport(handle),**kwargs))
    with pytest.raises(ValueError,match="дважды"):
        asyncio.run(APIPlanner().plan(Assignment("task")))
    assert len(calls)==2
    assert len(calls[1]["messages"])==4
