import time

from gizmo.listening import Listening


def make(mode="wake", until=0.0):
    listening = Listening(mode, ["Gizmo", "Explorer"])
    listening.speaking_until = lambda: until
    return listening


def test_names_match_fuzzily():
    listening = make()
    for heard in ["Gizmo, what's the biggest planet?", "hey gismo", "Is that Gizmo's?", "GIZMO!", "okay explorer"]:
        assert listening.names_in(heard), heard
    for heard in ["what's the biggest planet", "is more coming", "the cosmos is big", "kismet"]:
        assert not listening.names_in(heard), heard


def test_free_talk_answers_everything():
    assert make("free").addressed("anything at all")


def test_follow_up_window_opens_when_gizmo_stops_talking():
    assert not make(until=0.0).addressed("what about Mars")
    assert make(until=time.monotonic() - 5).addressed("what about Mars")
    talking = make(until=time.monotonic() + 3)
    assert not talking.addressed("what about Mars") and talking.addressed("Gizmo, stop")
    assert not make(until=time.monotonic() - 60).addressed("what about Mars")


def test_typed_lines_always_go_through():
    listening = make()
    listening.typed("What does the flag of Canada look like?")
    assert listening.addressed("what does the flag of canada look like")
    assert not listening.addressed("tell me about dinosaurs")


def test_a_typed_line_run_into_room_speech_still_goes_through():
    # Nova joins a typed line onto whatever it was hearing at the time, with no space between them.
    listening = make()
    listening.typed("Tell me about Saturn.")
    assert listening.addressed("we print them in housetell me about saturn")
    assert not listening.addressed("we print them in house")
    listening.typed("ok")  # too short to look for inside other speech
    assert not listening.addressed("is that ok with everyone here today")


def test_a_question_makes_yeah_an_answer():
    listening = make()
    listening.replied("Do you want to know about Saturn's rings?")
    assert listening.asked
    listening.replied("Saturn has rings.")
    assert not listening.asked
