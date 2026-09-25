"""Multimodal Voice AI Agent for HRMS.

Integrates Sarvam AI Speech-to-Text (saaras:v2 / saaras:v1) and Text-to-Speech (bulbul:v1),
enabling voice-driven attendance punches, leave inquiries, and conversational HR interactions.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

SARVAM_API_URL = "https://api.sarvam.ai"


class VoiceAgent:
    """Multimodal Voice Agent orchestrating Sarvam AI speech and conversational audio pipelines."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_language: str = "en-IN",
        target_speaker: str = "meera",
    ) -> None:
        self.api_key = api_key
        self.default_language = default_language
        self.target_speaker = target_speaker

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language_code: Optional[str] = None,
        model: str = "saaras:v2",
    ) -> Dict[str, Any]:
        """Transcribe speech audio bytes to text with timestamp alignments."""
        lang = language_code or self.default_language
        if not self.api_key:
            # Fallback mock for testing and offline environments
            return {
                "transcript": "Apply sick leave for tomorrow due to fever",
                "language_code": lang,
                "confidence": 0.98,
                "model": model,
                "mocked": True,
            }

        headers = {
            "api-subscription-key": self.api_key,
        }

        files = {
            "file": ("audio.wav", audio_bytes, "audio/wav"),
        }
        data = {
            "language_code": lang,
            "model": model,
            "with_diarization": "false",
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SARVAM_API_URL}/speech-to-text",
                headers=headers,
                files=files,
                data=data,
            )
            resp.raise_for_status()
            res_json = resp.json()
            return {
                "transcript": res_json.get("transcript", ""),
                "language_code": res_json.get("language_code", lang),
                "confidence": res_json.get("confidence", 1.0),
                "model": model,
                "mocked": False,
            }

    async def synthesize_speech(
        self,
        text: str,
        target_language: Optional[str] = None,
        speaker: Optional[str] = None,
        model: str = "bulbul:v1",
    ) -> Dict[str, Any]:
        """Synthesize text into natural spoken audio via Sarvam bulbul model."""
        lang = target_language or self.default_language
        spk = speaker or self.target_speaker

        if not self.api_key:
            # Offline simulated waveform for tests
            dummy_wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00"
            return {
                "audio_base64": base64.b64encode(dummy_wav).decode("utf-8"),
                "audio_format": "audio/wav",
                "speaker": spk,
                "language_code": lang,
                "mocked": True,
            }

        headers = {
            "Content-Type": "application/json",
            "api-subscription-key": self.api_key,
        }
        payload = {
            "inputs": [text],
            "target_language_code": lang,
            "speaker": spk,
            "model": model,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{SARVAM_API_URL}/text-to-speech",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            res_json = resp.json()
            audios = res_json.get("audios", [])
            audio_b64 = audios[0] if audios else ""

            return {
                "audio_base64": audio_b64,
                "audio_format": "audio/wav",
                "speaker": spk,
                "language_code": lang,
                "mocked": False,
            }

    async def handle_voice_turn(
        self,
        audio_bytes: bytes,
        supervisor_agent: Any,
        user_id: str,
        language_code: Optional[str] = None,
    ) -> Dict[str, Any]:
        """End-to-end voice turn: Audio In -> Transcribe -> Agent Reasoning -> Speech Out."""
        # 1. Transcribe Audio
        stt_result = await self.transcribe_audio(audio_bytes, language_code=language_code)
        user_prompt = stt_result.get("transcript", "")

        # 2. Invoke Cognitive Agent Fleet
        chat_res = await supervisor_agent.process_turn(
            user_message=user_prompt,
            user_id=user_id,
        )
        reply_text = chat_res.get("reply", "")

        # 3. Synthesize Voice Reply
        tts_result = await self.synthesize_speech(
            text=reply_text,
            target_language=stt_result.get("language_code", self.default_language),
        )

        return {
            "transcript_in": user_prompt,
            "reply_text": reply_text,
            "reply_audio_base64": tts_result.get("audio_base64"),
            "agent_intent": chat_res.get("routed_agent"),
            "actions": chat_res.get("actions", []),
        }
