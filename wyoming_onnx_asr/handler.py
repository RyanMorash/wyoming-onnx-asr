"""Event handler for clients of the server."""

import asyncio
import logging
import os
import tempfile
import wave
from typing import Any, Mapping, Optional

import numpy as np
import soundfile as sf
from onnx_asr.adapters import AsrAdapter
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStop
from wyoming.event import Event
from wyoming.info import Describe, Info
from wyoming.server import AsyncEventHandler

_LOGGER = logging.getLogger(__name__)


class NemoAsrEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        wyoming_info: Info,
        models: Mapping[str, AsrAdapter[Any]],
        model_lock: asyncio.Lock,
        *args,
        initial_prompt: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.wyoming_info_event = wyoming_info.event()
        self.models = models
        self.model_lock = model_lock
        self.initial_prompt = initial_prompt
        self.request_language: Optional[str] = None
        self._wav_dir = tempfile.TemporaryDirectory()
        self._wav_path = os.path.join(self._wav_dir.name, "speech.wav")
        self._wav_file: Optional[wave.Wave_write] = None

    def close(self) -> None:
        """Cleanup temporary resources."""
        if self._wav_file is not None:
            self._wav_file.close()
            self._wav_file = None
        if self._wav_dir is not None:
            self._wav_dir.cleanup()
            self._wav_dir = None

    def __enter__(self) -> "NemoAsrEventHandler":
        return self

    def __exit__(
        self,
        exc_type: Optional[type],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()
    async def handle_event(self, event: Event) -> bool:
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)

            if self._wav_file is None:
                self._wav_file = wave.open(self._wav_path, "wb")
                self._wav_file.setframerate(chunk.rate)
                self._wav_file.setsampwidth(chunk.width)
                self._wav_file.setnchannels(chunk.channels)

            self._wav_file.writeframes(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            _LOGGER.debug(
                "Audio stopped. Transcribing with initial prompt=%s",
                self.initial_prompt,
            )
            assert self._wav_file is not None

            self._wav_file.close()
            self._wav_file = None

            waveform, sample_rate = sf.read(self._wav_path, dtype="float32")
            # Make mono by averaging the channels
            if len(waveform.shape) > 1:
                waveform = np.mean(waveform, axis=1)

            # Decide on language and model
            lang = self.request_language or "en"
            model = None

            _LOGGER.info(f"Language requested: {lang}")
            _LOGGER.info(f"Available models: {list(self.models.keys())}")

            if lang == "en" and "en" in self.models:
                model = self.models["en"]
                _LOGGER.info(f"Selected English model for language '{lang}'")
            elif "multi" in self.models:
                model = self.models["multi"]
                _LOGGER.info(f"Selected multilingual model for language '{lang}'")
            elif "en" in self.models:
                model = self.models["en"]
                _LOGGER.info(f"Fallback to English model for language '{lang}'")

            if model is None:
                if self.request_language:
                    _LOGGER.error(
                        "Language '%s' requested but no suitable model is available",
                        self.request_language,
                    )
                    _LOGGER.error("Available models: %s", list(self.models.keys()))
                    error_msg = f"Language '{self.request_language}' is not supported. Available models: {list(self.models.keys())}"
                else:
                    _LOGGER.error("No ASR model loaded - server misconfiguration")
                    error_msg = "No ASR model is available for transcription"

                # Send error response instead of raising exception
                await self.write_event(Transcript(text=f"ERROR: {error_msg}").event())
                return False

            async with self.model_lock:
                try:
                    _LOGGER.info(
                        f"Starting transcription with model for language '{lang}'"
                    )
                    text = model.recognize(
                        waveform.astype(np.float32), sample_rate=sample_rate, language=lang
                    )
                    _LOGGER.info(
                        f"Transcription completed successfully for language '{lang}'"
                    )
                except Exception as e:
                    _LOGGER.error("Model recognition failed: %s", str(e))
                    error_msg = f"Transcription failed: {str(e)}"
                    await self.write_event(
                        Transcript(text=f"ERROR: {error_msg}").event()
                    )
                    return False

            _LOGGER.info(f"{lang}:{text}")

            await self.write_event(Transcript(text=text).event())
            _LOGGER.debug("Completed request")

            # Reset request language after transcription
            self.request_language = None

            return False

        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            # Extract language (may be None) and save to request_language
            self.request_language = transcribe.language
            return True

        if Describe.is_type(event.type):
            await self.write_event(self.wyoming_info_event)
            _LOGGER.debug("Sent info")
            return True

        return True
