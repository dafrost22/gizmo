"""Captions: the words being spoken, timed against the reply's audio, for the console to light up."""
import asyncio
import base64

import numpy as np
from strands.bidi.types.events import BidiAudioDeltaEvent, BidiBargeInEvent, BidiTranscriptDeltaEvent, BidiTranscriptStartEvent

from gizmo.robot_io import Captions, RobotSpeaker


def test_a_segment_is_its_words_and_the_audio_queued_until_the_next_words():
    c = Captions()
    c.words("Tokyo is fourteen hours ahead.")
    c.audio(2.0)
    c.audio(1.5)
    c.words("  Expect clear\nskies. ")
    c.audio(3.0)
    assert c.state()["segments"] == [{"text": "Tokyo is fourteen hours ahead.", "start": 0.0, "end": 3.5},
                                     {"text": "Expect clear skies.", "start": 3.5, "end": 6.5}]
    assert c.state()["audio"] == 6.5


def test_words_with_no_audio_between_them_are_one_stretch():
    c = Captions()
    c.words("Hello.")
    c.words("")  # nothing said
    c.words("How are you?")
    c.audio(2.0)
    assert c.state()["segments"] == [{"text": "Hello. How are you?", "start": 0.0, "end": 2.0}]


def test_audio_ahead_of_any_words_pushes_them_back():
    c = Captions()
    c.audio(1.2)  # a sound clip: it has no words to light up
    c.words("That was a recording.")
    c.audio(2.0)
    assert c.state()["segments"] == [{"text": "That was a recording.", "start": 1.2, "end": 3.2}]


def test_where_the_voice_is_allows_for_audio_sent_but_not_yet_heard():
    c = Captions()
    c.words("One two three.")
    c.audio(3.0)
    c.played_s = 1.0
    assert c.state(ahead_s=0.15)["at"] == 0.85
    assert c.state(ahead_s=-4.0)["at"] == 1.0  # the speaker's clock is in the past: nothing is waiting
    c.played_s = 9.0
    assert c.state()["at"] == 3.0  # never past the audio there is
    before = c.reply
    c.new()
    assert c.state() == {"reply": before + 1, "started": c.started, "at": 0.0, "audio": 0.0, "segments": []} and c.started


class Media:
    def get_output_audio_samplerate(self):
        return 16000

    def push_audio_sample(self, chunk):
        pass


class Mini:
    media = Media()


def audio(seconds: float) -> BidiAudioDeltaEvent:
    pcm = np.zeros(int(16000 * seconds), dtype=np.int16)
    return BidiAudioDeltaEvent(base64.b64encode(pcm.tobytes()).decode(), "pcm", 16000, 1, "a1")


def test_the_speaker_times_nova_sonics_sentences_and_a_voice_servers():
    async def run():
        speaker = RobotSpeaker(Mini())
        first = speaker.captions.reply
        for event in (BidiTranscriptStartEvent("assistant", "t1"),
                      BidiTranscriptDeltaEvent("Tokyo is fourteen hours ahead.", "assistant", "t1"), audio(2.0),
                      BidiTranscriptDeltaEvent(" Expect clear skies.", "assistant", "t1"), audio(1.0),
                      BidiTranscriptDeltaEvent("what the person said", "user", "t2")):  # not Gizmo's words
            await speaker(event)
        state = speaker.caption_state()
        assert state["reply"] == first + 1  # the reply began in silence: a new one
        assert state["segments"] == [{"text": "Tokyo is fourteen hours ahead.", "start": 0.0, "end": 2.0},
                                     {"text": "Expect clear skies.", "start": 2.0, "end": 3.0}]
        assert state["at"] == 0.0  # nothing has been sent to the robot yet

        await speaker(BidiBargeInEvent())  # someone talked over it: those words are over
        assert speaker.caption_state()["segments"] == [] and speaker.caption_state()["reply"] == first + 2

        speaker.mute_nova = True  # the personality's own voice speaks: Nova's words and audio are dropped
        await speaker(BidiTranscriptDeltaEvent("Nova's own answer.", "assistant", "t3"))
        await speaker(audio(1.0))
        speaker.play_clone(np.zeros(8000, np.float32), "In my own voice.")
        speaker.play_clone(np.zeros(4000, np.float32))  # a sound clip: audio, no words
        speaker.play_clone(np.zeros(16000, np.float32), "And a second sentence.")
        assert speaker.caption_state()["segments"] == [{"text": "In my own voice.", "start": 0.0, "end": 0.75},
                                                       {"text": "And a second sentence.", "start": 0.75, "end": 1.75}]

    asyncio.run(run())
