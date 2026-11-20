import math

from gizmo.direction import DoATracker, aim, offset_deg, plan_turn


def test_the_arrays_angle_becomes_a_turn_left_or_right():
    assert offset_deg(math.pi / 2) == 0  # straight ahead
    assert round(offset_deg(0.0)) == 90 and round(offset_deg(math.pi)) == -90  # 0 is the array's left
    assert round(offset_deg(0.0, sign=-1.0)) == -90  # a robot whose array is the other way round


def test_a_small_offset_isnt_worth_a_turn_and_a_big_one_brings_the_body():
    assert plan_turn(0.0, 5.0) is None
    assert plan_turn(0.0, 30.0) == (30.0, 0.0)  # the head alone
    head, body = plan_turn(0.0, 80.0)
    assert (head, body) == (80.0, 40.0)  # the body follows, the head leading it by 40
    head, body = plan_turn(60.0, 60.0)  # already turned: the offset is from where the head points now
    assert (head, body) == (120.0, 80.0)
    head, body = aim(-200.0)
    assert body == -120.0 and head == -180.0  # the body's limit, and the head no more than 60 past it


def test_the_bearing_is_the_median_of_recent_speech_and_ignores_the_rest():
    doa = DoATracker("http://nowhere")  # not started: frames are added by hand
    assert doa.bearing(100.0) is None
    for i, angle in enumerate([1.0, 1.1, 0.9, 1.0, 2.9]):  # one stray reading among four that agree
        doa.add(99.0 + i * 0.1, angle, True)
    doa.add(99.6, 0.2, False)  # no speech in this frame (or Gizmo itself was talking)
    doa.add(99.7, 0.01, True)  # the array's end stop, not a real bearing
    assert doa.bearing(100.0) == 1.0
    assert doa.bearing(110.0) is None  # that was too long ago to be this turn's voice
    assert len(doa.heard(100.0)[0]) == 5  # for the trace: how many readings it went on


def test_a_short_phrase_falls_back_on_how_loud_the_mic_was():
    doa = DoATracker("http://nowhere")
    doa.add(99.0, 1.2, True)  # the chip flagged one reading: a stray, on its own
    assert doa.bearing(100.0) is None
    doa.add(99.1, 0.6, False, loud=True)
    doa.add(99.2, 0.7, False, loud=True)  # but the mic heard a voice-loud sound for two more
    assert doa.bearing(100.0) == 0.7
    doa.add(99.3, 1.3, True)  # two flagged readings are trusted over the loud ones
    assert doa.bearing(100.0) == 1.25
