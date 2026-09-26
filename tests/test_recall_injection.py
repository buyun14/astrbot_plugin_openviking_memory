"""Tests for recall injection placement (prefix-cache friendly tail context)."""

from __future__ import annotations

import asyncio

import pytest

from ov_client.injection import inject_recall_block

BLOCK = "<openviking-context>\nrecalled memory\n</openviking-context>"


class FakeRequest:
    def __init__(self, system_prompt: str = "SYS") -> None:
        self.system_prompt = system_prompt
        self.extra_user_content_parts: list[object] = []


class FakeTextPart:
    def __init__(self, text: str) -> None:
        self.text = text
        self.temp = False

    def mark_as_temp(self) -> FakeTextPart:
        self.temp = True
        return self


def test_recall_goes_to_tail_not_system_prompt():
    req = FakeRequest()
    req.extra_user_content_parts.append(FakeTextPart(text="user message"))

    inject_recall_block(req, BLOCK, text_part_cls=FakeTextPart)

    assert req.system_prompt == "SYS"
    assert [p.text for p in req.extra_user_content_parts] == ["user message", BLOCK]
    assert req.extra_user_content_parts[-1].temp is True


def test_recall_falls_back_to_system_prompt_without_content_parts():
    class BareRequest:
        system_prompt = "SYS"

    req = BareRequest()

    inject_recall_block(req, BLOCK, text_part_cls=FakeTextPart)

    assert req.system_prompt == f"SYS\n\n{BLOCK}"


def test_recall_part_is_not_persisted_in_history():
    entities = pytest.importorskip("astrbot.core.provider.entities")
    message_mod = pytest.importorskip("astrbot.core.agent.message")

    req = entities.ProviderRequest(prompt="hello", system_prompt="SYS")
    inject_recall_block(req, BLOCK)

    assert req.system_prompt == "SYS"

    assembled = asyncio.run(req.assemble_context())
    assert assembled["role"] == "user"
    texts = [part["text"] for part in assembled["content"] if part["type"] == "text"]
    assert texts[0] == "hello"
    assert texts[-1] == BLOCK

    message = message_mod.Message.model_validate(assembled)
    dumped = message_mod.dump_messages_with_checkpoints([message])[0]
    persisted = "".join(part["text"] for part in dumped["content"] if part.get("type") == "text")
    assert "hello" in persisted
    assert "openviking-context" not in persisted
