"""Unit tests for Voice AI Agent (Sarvam AI multimodal pipeline)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hrms_plugin.agents.voice import VoiceAgent


@pytest.mark.asyncio
async def test_voice_agent_mock_stt_and_tts():
    """Verify VoiceAgent STT and TTS offline fallback execution."""
    agent = VoiceAgent(api_key=None, default_language="en-IN", target_speaker="meera")

    # 1. Test STT
    dummy_audio = b"dummy_wav_content_bytes"
    stt_res = await agent.transcribe_audio(dummy_audio)
    assert stt_res["mocked"] is True
    assert "transcript" in stt_res
    assert len(stt_res["transcript"]) > 0

    # 2. Test TTS
    tts_res = await agent.synthesize_speech("Your leave has been approved successfully.")
    assert tts_res["mocked"] is True
    assert "audio_base64" in tts_res
    assert tts_res["speaker"] == "meera"


@pytest.mark.asyncio
async def test_voice_agent_handle_voice_turn():
    """Verify multimodal voice turn routing through SupervisorAgent."""
    voice_agent = VoiceAgent(api_key=None)

    mock_supervisor = MagicMock()
    mock_supervisor.process_turn = AsyncMock(
        return_value={
            "reply": "I have marked your check-in at 09:15 AM.",
            "routed_agent": "LeaveAttendanceAgent",
            "actions": [{"type": "RECORD_PUNCH", "timestamp": "09:15:00"}],
        }
    )

    result = await voice_agent.handle_voice_turn(
        audio_bytes=b"dummy_bytes",
        supervisor_agent=mock_supervisor,
        user_id="emp_001",
    )

    assert "transcript_in" in result
    assert result["reply_text"] == "I have marked your check-in at 09:15 AM."
    assert result["agent_intent"] == "LeaveAttendanceAgent"
    assert "reply_audio_base64" in result
