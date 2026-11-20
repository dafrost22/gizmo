from gizmo.brain import HAIKU, SONNET, ModelSpec, ReplyStream, fallbacks, model_spec, render_prompt, split_actions, thinking_off
from gizmo.personalities import BUILTIN


def test_the_prompt_is_the_personalitys_then_how_gizmo_works():
    prompt = BUILTIN["explorer"]["prompt"]
    on_robot = render_prompt(prompt)
    assert on_robot.startswith(prompt) and "HOW YOU WORK" in on_robot and "see(question)" in on_robot
    remote = render_prompt(prompt, robot_tools=False)
    assert "see(question)" not in remote and "load that skill first" in remote
    bare = render_prompt("You are a test.", has_mcp=False, has_skills=False, robot_tools=False)
    assert "<server>_<tool>" not in bare and "Skills:" not in bare and "None beyond answering" in bare


def test_the_model_comes_from_the_settings_with_bedrock_as_the_default():
    assert model_spec({}) == ModelSpec("bedrock", HAIKU)
    assert model_spec({"model": {"provider": "bedrock", "bedrock": {"model": SONNET}}}).label == "Claude Sonnet 5.5"
    local = model_spec({"model": {"provider": "openai", "openai": {
        "base_url": "http://box:11434/v1", "model": "qwen3:14b", "params": {"reasoning_effort": "none"}}}})
    assert (local.provider, local.model, local.base_url, local.params) == ("openai", "qwen3:14b", "http://box:11434/v1",
                                                                           {"reasoning_effort": "none"})
    # AgentCore hosts the brain; the model it (and the stand-in on the robot) runs is the Bedrock choice.
    assert model_spec({"model": {"provider": "agentcore", "bedrock": {"model": SONNET}}}) == ModelSpec("bedrock", SONNET)


def test_the_cloud_stands_by_for_a_model_somewhere_else():
    assert [s.model for s in fallbacks(ModelSpec("bedrock", HAIKU))] == [SONNET]
    assert [s.model for s in fallbacks(ModelSpec("bedrock", SONNET))] == [HAIKU]
    assert [s.model for s in fallbacks(ModelSpec("openai", "qwen3:14b"))] == [HAIKU, SONNET]
    assert thinking_off(HAIKU) == {"thinking": {"type": "disabled"}}
    assert thinking_off(SONNET) == {"thinking": {"type": "between_tools"}} and thinking_off("us.amazon.nova-pro-v1:0") is None


def test_split_actions_takes_leading_tags():
    assert split_actions("[emotion:cheerful] [look:left] Hi there!") == (
        [("emotion", "cheerful"), ("look", "left")], "Hi there!")
    assert split_actions("[curious] Hmm. [aside] Yes.") == ([("emotion", "curious")], "Hmm. Yes.")


def test_reply_stream_speaks_sentences_as_they_finish():
    acted, said = [], []
    reply = ReplyStream(acted.extend, said.append)
    for piece in ["[emotion:cur", "ious] Jupiter is the big", "gest planet. It has ", "storms."]:
        reply.feed(piece)
    assert acted == [("emotion", "curious")] and said == ["Jupiter is the biggest planet."]
    reply.end_message()
    assert said[-1] == "It has storms."


def test_the_answer_is_the_last_message():
    reply = ReplyStream(lambda a: None, lambda s: None)
    reply.feed("Let me check where the station is. ")
    reply.end_message()  # a tool call came next
    reply.feed("It's over the Pacific Ocean. ")
    reply.end_message()
    assert reply.answer == "It's over the Pacific Ocean."
    assert len(reply.spoken) == 2


def test_a_personality_with_sound_clips_is_told_how_to_play_them():
    from gizmo.brain import ReplyStream, acting, render_prompt, sounds_in, split_actions

    plain = render_prompt("You are a test.")
    assert "[sound:" not in plain  # no clips: the model never hears of sound tags
    told = render_prompt("You are a test.", sounds={"ta-da": "Ta-da!", "drum-roll": "a drum roll"})
    assert '[sound:<name>] play one of your recordings, ahead of your words: ta-da ("Ta-da!"), drum-roll ("a drum roll").' in told
    assert "don't write them as well" in told and told.index("[sound:<name>]") < told.index("Tools:")
    # The tag leads the reply like a gesture. The brain only reports it: whoever speaks plays the recording.
    actions, say = split_actions("[sound:ta-da] [emotion:proud] Here it is.")
    assert actions == [("sound", "ta-da"), ("emotion", "proud")] and say == "Here it is."
    assert sounds_in(actions) == ["ta-da"] and sounds_in([("emotion", "proud"), ("sound", "")]) == []
    assert split_actions("[sound:ta-da]") == ([("sound", "ta-da")], "")  # a reply can be just a recording
    # Streamed (the home-box pipeline): the clip is asked for as soon as its tag is complete, before any sentence.
    order = []

    class Body:
        emotions, dances = [], []

    reply = ReplyStream(acting(Body(), lambda name: order.append(("sound", name))), lambda s: order.append(("say", s)))
    for piece in ("[sou", "nd:ta-da] He", "re it is. And more."):
        reply.feed(piece)
    reply.end_message()
    assert order == [("sound", "ta-da"), ("say", "Here it is."), ("say", "And more.")]


def test_a_reply_that_opens_with_a_recordings_words_plays_the_recording():
    from gizmo.brain import ReplyStream, acting, clip_line, lead_with_clip

    sounds = {"ta-da": "Ta-da!", "drums": "A drum roll, please."}
    assert clip_line("ta da", sounds) == "ta-da" and clip_line("A drum roll please!", sounds) == "drums"
    assert clip_line("Ta-da, everyone!", sounds) is None and clip_line("", sounds) is None and clip_line("Ta-da!", None) is None
    # The words alone, or first: the recording plays and a voice says only what's left.
    assert lead_with_clip([("emotion", "proud")], "Ta-da!", sounds) == ([("emotion", "proud"), ("sound", "ta-da")], "")
    assert lead_with_clip([], "Ta-da! Here is your report.", sounds) == ([("sound", "ta-da")], "Here is your report.")
    # The tag and the words both: once, not twice.
    assert lead_with_clip([("sound", "ta-da")], "Ta-da! Done.", sounds) == ([("sound", "ta-da")], "Done.")
    # Anywhere but the start, the words stay words (a whole-reply voice can't be paused mid-reply for a recording).
    assert lead_with_clip([], "Here it is. Ta-da!", sounds) == ([], "Here it is. Ta-da!")
    assert lead_with_clip([], "Hello there.", {}) == ([], "Hello there.")
    # Streamed, the same: the first sentence of a message.
    order = []

    class Body:
        emotions, dances = [], []

    reply = ReplyStream(acting(Body(), lambda name: order.append(("sound", name))), lambda s: order.append(("say", s)), sounds)
    reply.feed("[emotion:proud] Ta-da! Here it ")
    reply.feed("is. Ta-da! ")
    reply.end_message()
    assert order == [("sound", "ta-da"), ("say", "Here it is."), ("say", "Ta-da!")]
    assert reply.actions == [("emotion", "proud"), ("sound", "ta-da")] and reply.answer == "Here it is. Ta-da!"


def test_a_reply_that_ends_with_a_recordings_words_plays_it_after_the_voice():
    import asyncio

    from gizmo.brain import end_with_clip, make_think_tool

    sounds = {"ta-da": "Ta-da!", "drums": "A drum roll, please."}
    assert end_with_clip("Here it is. Ta-da!", sounds) == ("Here it is.", ["ta-da"])
    assert end_with_clip("Wait for it. A drum roll, please. Ta-da!", sounds) == ("Wait for it.", ["drums", "ta-da"])
    assert end_with_clip("Ta-da! Here it is.", sounds) == ("Ta-da! Here it is.", [])  # not at the end
    assert end_with_clip("Ta-da!", sounds) == ("Ta-da!", [])  # alone, it's the opening case
    assert end_with_clip("And then, ta-da!", sounds) == ("And then, ta-da!", [])  # not a sentence of its own
    assert end_with_clip("Here it is. Ta-da!", None) == ("Here it is. Ta-da!", [])

    class Brain:
        def __init__(self, say, actions=()):
            self.say, self.actions = say, list(actions)

        async def think(self, heard):
            return {"say": self.say, "actions": self.actions, "model": "m", "seconds": 0.1}

    def run(brain, own_voice=False):
        played = []

        async def speak(text, started=False):
            played.append(("own voice", text, started))
            return own_voice

        think = make_think_tool(brain, speak=speak, sound=lambda names: played.append(("now", names)),
                                sound_after=lambda names: played.append(("after", names)), sounds=sounds)
        return asyncio.run(think(heard="hello")), played

    # Nova speaks: it's handed the words without the recording's, and the recording is queued for afterwards.
    said, played = run(Brain("Here it is. Ta-da!"))
    assert said == "Here it is." and played == [("own voice", "Here it is. Ta-da!", False), ("after", ["ta-da"])]
    # A tag: the recording first, then every word goes to Nova.
    said, played = run(Brain("Here it is.", [("sound", "ta-da")]))
    assert said == "Here it is." and played[0] == ("now", ["ta-da"]) and played[1] == ("own voice", "Here it is.", True)
    # The personality's own voice says the whole reply (a voice server swaps the recording in itself), so nothing more.
    said, played = run(Brain("Here it is. Ta-da!"), own_voice=True)
    assert said == "" and played == [("own voice", "Here it is. Ta-da!", False)]
