import pytest

from gizmo.personalities import GIZMO_PROMPT, Personalities, ServerPick


@pytest.fixture
def people(tmp_path):
    return Personalities(tmp_path)


def test_builtins_are_gizmo_and_the_demo(people):
    assert people.keys() == ["gizmo", "explorer"] and people.names() == {"gizmo": "Gizmo", "explorer": "Young explorer"}
    assert not people.get("gizmo")["edited"] and people.get("gizmo")["builtin"]


def test_each_builtin_ships_with_a_voice_and_its_own_tools(people):
    assert people.get("gizmo")["voice"]["nova"] == "matthew" and people.get("explorer")["voice"]["nova"] == "tiffany"
    gizmo, explorer = people.selection("gizmo"), people.selection("explorer")
    assert set(gizmo.servers) == {"explorer"} and gizmo.servers["explorer"] == ServerPick()  # all of it
    assert set(explorer.servers) == {"explorer", "countries"}
    countries = explorer.servers["countries"]
    assert countries.has_tool("get_country") and not countries.has_tool("list_languages") and countries.has_skill("any://uri")


def test_a_change_is_kept_and_reset_takes_all_of_it_back(people):
    people.save("gizmo", {"name": "Gizmo two", "prompt": "You are a test.", "greeting": "Hello.",
                          "voice": {"kind": "nova", "nova": "amy"},
                          "tools": {"servers": {"kb": {"on": True, "tools": ["b", "a"], "skills": None}}, "skills": ["notes"]}})
    p = people.get("gizmo")
    assert (p["name"], p["prompt"], p["greeting"], p["voice"]["nova"], p["edited"]) == ("Gizmo two", "You are a test.", "Hello.", "amy", True)
    assert p["tools"] == {"servers": {"kb": {"on": True, "tools": ["a", "b"], "skills": None}}, "skills": ["notes"]}
    people.reset("gizmo")
    p = people.get("gizmo")
    assert (p["name"], p["prompt"], p["greeting"], p["voice"]["nova"], p["edited"]) == ("Gizmo", GIZMO_PROMPT, "", "matthew", False)
    assert set(p["tools"]["servers"]) == {"explorer"}


def test_an_empty_prompt_goes_back_to_the_builtins_and_a_new_one_needs_its_own(people):
    people.save("gizmo", {"prompt": "  "})
    assert people.get("gizmo")["prompt"] == GIZMO_PROMPT
    with pytest.raises(ValueError):
        people.save("helper", {"name": "Helper"})
    with pytest.raises(ValueError):
        people.save("Not A Key", {"prompt": "x"})
    people.save("helper", {"name": "Helper", "prompt": "You help."})
    assert people.keys() == ["gizmo", "explorer", "helper"] and not people.get("helper")["builtin"]
    assert people.get("helper")["voice"] == people.get("helper")["voice"] | {"kind": "nova", "nova": "tiffany"}
    assert people.selection("helper").servers == {}  # nothing until it's switched on
    people.delete("helper")
    assert not people.exists("helper")
    with pytest.raises(ValueError):
        people.delete("gizmo")


def test_switching_a_server_off_keeps_its_picks_but_takes_it_out(people):
    tools = people.get("explorer")["tools"]
    tools["servers"]["countries"]["on"] = False
    people.save("explorer", {"tools": tools})
    assert set(people.selection("explorer").servers) == {"explorer"}
    assert len(people.get("explorer")["tools"]["servers"]["countries"]["tools"]) == 7


def test_a_new_server_or_skill_is_switched_on_and_a_removed_one_forgotten(people):
    people.use("gizmo", server="kb")
    people.use("gizmo", skill="notes")
    assert people.selection("gizmo").servers["kb"] == ServerPick() and people.selection("gizmo").skills == {"notes"}
    people.forget(server="kb", renamed_to="docs")
    assert "docs" in people.selection("gizmo").servers and "kb" not in people.selection("gizmo").servers
    people.forget(server="docs")
    people.forget(skill="notes")
    assert set(people.selection("gizmo").servers) == {"explorer"} and not people.selection("gizmo").skills
    assert not people.get("gizmo")["edited"]  # back to how it shipped, so not "edited"
    people.forget(server="explorer")
    assert not people.selection("explorer").servers.get("explorer") and people.get("explorer")["edited"]


def test_the_voice_is_novas_unless_it_has_one_of_its_own(people):
    assert not people.own_voice("gizmo")
    assert people.voice_ref("gizmo", fallback=False) is None
    assert people.voice_ref("gizmo").builtin["voice"] == "af_heart"  # the home box has no Nova to speak for it
    people.save("explorer", {"voice": {"kind": "builtin"}})
    ref = people.voice_ref("explorer", fallback=False)
    assert people.own_voice("explorer") and ref.builtin["voice"] == "am_puck" and ref.audio is None
    people.save("gizmo", {"voice": {"kind": "clone"}})
    assert people.voice_ref("gizmo", fallback=False) is None  # set to clone, but no clip yet
    people.set_clip("gizmo", "Hello there.", "a.wav", b"RIFF-a")
    ref = people.voice_ref("gizmo", fallback=False)
    assert ref.ref_text == "Hello there." and ref.audio.read_bytes() == b"RIFF-a" and ref.builtin is None
    with pytest.raises(ValueError):
        people.save("gizmo", {"voice": {"kind": "loud"}})
    with pytest.raises(ValueError):
        people.save("gizmo", {"voice": {"nova": "nobody"}})


def test_builtin_voice_tuning_is_clamped(people):
    people.save("gizmo", {"voice": {"kind": "builtin", "builtin": {"voice": "af_heart", "pitch": 9, "speed": "1.2"}}})
    assert people.get("gizmo")["voice"]["builtin"] == {"voice": "af_heart", "mix": None, "mix_amount": 0.0, "speed": 1.2,
                                                       "pitch": 6, "robot": 0.0}
    with pytest.raises(ValueError):
        people.save("gizmo", {"voice": {"builtin": {"voice": "nope"}}})


def test_a_new_clip_keeps_the_old_one_as_a_version_to_load_back_in(people):
    people.set_clip("gizmo", "First take.", "a.wav", b"RIFF-first")
    people.note_seconds("gizmo", 4.0)
    people.set_clip("gizmo", "Second take.", "b.wav", b"RIFF-second")
    versions = people.clip_versions("gizmo")
    assert [v["ref_text"] for v in versions] == ["First take."] and versions[0]["seconds"] == 4.0
    assert people.summary("gizmo")["clip"]["ref_text"] == "Second take."
    assert (people.root / "gizmo" / versions[0]["file"]).read_bytes() == b"RIFF-first"

    people.restore_clip("gizmo", versions[0]["id"])
    clip = people.summary("gizmo")["clip"]
    assert clip["ref_text"] == "First take." and clip["seconds"] == 4.0
    assert (people.root / "gizmo" / clip["file"]).read_bytes() == b"RIFF-first"
    assert [v["ref_text"] for v in people.clip_versions("gizmo")] == ["Second take."]  # the replaced one is kept

    people.delete_clip_version("gizmo", people.clip_versions("gizmo")[0]["id"])
    assert people.clip_versions("gizmo") == []
    with pytest.raises(ValueError):
        people.restore_clip("gizmo", "nope")
    people.reset("gizmo")
    assert people.summary("gizmo")["clip"]["ref_text"] == "First take."  # a reset leaves the clips


def test_only_a_new_clip_makes_a_version(people):
    people.set_clip("gizmo", "Take one.", "a.wav", b"RIFF-a")
    people.set_clip("gizmo", "Take one, fixed.")  # the transcript only
    assert people.clip_versions("gizmo") == []
    assert people.summary("gizmo")["clip"]["ref_text"] == "Take one, fixed."


def test_a_skill_ticked_on_its_own_carries_its_approval(people):
    uri, other = "skill://refunds/SKILL.md", "skill://other/SKILL.md"
    people.save("gizmo", {"tools": {"servers": {"kb": {"on": True, "tools": None, "skills": [uri],
                                                         "approved": {uri: "sha256:aa", other: "sha256:bb"}}}, "skills": []}})
    assert people.get("gizmo")["tools"]["servers"]["kb"]["approved"] == {uri: "sha256:aa"}  # only for what's ticked
    pick = people.selection("gizmo").servers["kb"]
    assert pick.has_skill(uri) and pick.approves(uri, "sha256:aa")
    assert not pick.approves(uri, "sha256:edited") and not pick.approves(uri, None) and not pick.approves(other, "sha256:bb")
    people.approve("gizmo", "kb", uri, "sha256:edited")  # looked at again, and approved as it is now
    assert people.selection("gizmo").servers["kb"].approves(uri, "sha256:edited")
    with pytest.raises(ValueError):
        people.approve("gizmo", "kb", other, "sha256:bb")  # not ticked
    # All of a server's skills on is trust in the server: nothing is approved one by one, so nothing is kept.
    people.save("gizmo", {"tools": {"servers": {"kb": {"on": True, "tools": None, "skills": None,
                                                         "approved": {uri: "sha256:aa"}}}, "skills": []}})
    assert "approved" not in people.get("gizmo")["tools"]["servers"]["kb"]
    assert people.selection("gizmo").servers["kb"].approves("any://uri", None)
    with pytest.raises(ValueError):
        people.approve("gizmo", "kb", uri, "sha256:aa")
