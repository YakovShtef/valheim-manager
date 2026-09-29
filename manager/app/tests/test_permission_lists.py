"""The adminlist / bannedlist / permittedlist files on the /config volume.

The manager is a SECOND writer on files the game server owns and may rewrite. Two
rules follow, and both are tested here: comments survive a rewrite, and the file
lands mode 0664 so the game server keeps write access to it.
"""

from __future__ import annotations

import os
import stat

import pytest

from app.permission_lists import (
    ADMIN,
    BANNED,
    PERMITTED,
    ListFile,
    PermissionListError,
    PermissionLists,
    normalise_typed_id,
    overwriting_env_vars,
    to_file_id,
)

POSIX_ONLY = pytest.mark.skipif(
    os.name != "posix", reason="file modes are only meaningful on POSIX"
)

HEADER = "// List admin players ID  ONE per line"
A = "V_76561198086248026"
B = "V_76561198000000001"


@pytest.fixture()
def lists(tmp_path):
    (tmp_path / "adminlist.txt").write_text(HEADER + "\n", encoding="utf-8")
    (tmp_path / "bannedlist.txt").write_text(
        "// List banned players ID  ONE per line\n", encoding="utf-8"
    )
    (tmp_path / "permittedlist.txt").write_text(
        "// List permitted players ID ONE per line\n", encoding="utf-8"
    )
    return PermissionLists(tmp_path)


def test_a_fresh_file_has_a_comment_and_no_ids(lists):
    assert lists.read(ADMIN) == ListFile(kind=ADMIN, ids=(), comments=(HEADER,), parked=())


def test_adding_an_id_keeps_the_comment(lists):
    lists.add(ADMIN, A)
    assert lists.read(ADMIN) == ListFile(kind=ADMIN, ids=(A,), comments=(HEADER,), parked=())


def test_the_comment_survives_in_the_written_text(lists, tmp_path):
    lists.add(ADMIN, A)
    text = (tmp_path / "adminlist.txt").read_text(encoding="utf-8")
    assert text.splitlines() == [HEADER, A]


def test_adding_the_same_id_twice_does_not_duplicate_it(lists):
    lists.add(ADMIN, A)
    assert lists.add(ADMIN, A).ids == (A,)


def test_removing_an_id_leaves_the_others(lists):
    lists.add(ADMIN, A)
    lists.add(ADMIN, B)
    assert lists.remove(ADMIN, A).ids == (B,)


def test_removing_an_absent_id_is_not_an_error(lists):
    assert lists.remove(ADMIN, A).ids == ()


def test_ids_are_case_sensitive(lists):
    """Platform ids are case-sensitive; treating V_ and v_ as one would silently
    grant or withhold admin."""
    lists.add(ADMIN, A)
    assert lists.remove(ADMIN, A.lower()).ids == (A,)


@POSIX_ONLY
def test_the_file_is_written_group_writable(lists, tmp_path):
    """0664, or the game server -- a different uid in the same group -- loses write
    access to a file it owns."""
    lists.add(ADMIN, A)
    mode = stat.S_IMODE(os.stat(tmp_path / "adminlist.txt").st_mode)
    assert mode == 0o664


def test_a_missing_file_is_created_on_demand(tmp_path):
    lists = PermissionLists(tmp_path)
    lists.add(BANNED, A)
    assert (tmp_path / "bannedlist.txt").read_text(encoding="utf-8").strip() == A


def test_parked_entries_are_read_back_separately(tmp_path):
    (tmp_path / "permittedlist.txt").write_text(
        f"// List permitted players ID ONE per line\n// disabled-by-manager {A}\n",
        encoding="utf-8",
    )
    read = PermissionLists(tmp_path).read(PERMITTED)
    assert read.ids == ()
    assert read.parked == (A,)


def test_parked_entries_round_trip(tmp_path):
    lists = PermissionLists(tmp_path)
    lists.write(PERMITTED, ids=(), parked=(A,))
    assert lists.read(PERMITTED).parked == (A,)


def test_an_unknown_list_kind_is_refused(lists):
    with pytest.raises(PermissionListError):
        lists.read("friends")


def test_the_manager_asks_for_group_writable_modes_whatever_the_platform(
    tmp_path, monkeypatch
):
    """The companion to test_the_file_is_written_group_writable, which cannot run on a
    Windows checkout: whatever the filesystem then does with it, the manager has to
    *ask* for a group-writable permission list on every file it writes.

    Why group-write is the whole mechanism: the manager runs as uid 10001 in the game's
    group (gid 1000) and cannot chown (cap_drop: [ALL] removes CAP_CHOWN), so after the
    manager's first write the file is owned by 10001:1000 and the game server keeps write
    access only through the group bit."""
    import app.permission_lists as permission_lists_module

    calls: list[tuple[str, int]] = []
    real_chmod = permission_lists_module.os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        calls.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(permission_lists_module.os, "chmod", recording_chmod)

    lists = PermissionLists(tmp_path)
    lists.add(ADMIN, A)

    assert calls, "nothing had its mode set at all"
    for path, mode in calls:
        assert mode & stat.S_IWGRP, f"{path} was written without the group write bit"


def test_a_steam_id_from_the_log_gains_the_v_prefix():
    assert to_file_id("76561198086248026", "steam") == "V_76561198086248026"


def test_an_unknown_platform_gets_no_invented_prefix():
    """Guessing here would write a line that silently grants nobody anything."""
    assert to_file_id("something", "unknown") is None


def test_an_already_prefixed_id_is_left_alone():
    assert to_file_id("V_76561198086248026", "steam") == "V_76561198086248026"


def test_a_pasted_prefixed_id_is_accepted_as_typed():
    assert normalise_typed_id("  V_76561198086248026 ") == ("V_76561198086248026", None)


def test_a_pasted_xbox_style_id_is_accepted_without_interpretation():
    assert normalise_typed_id("Xbox_2535123456789") == ("Xbox_2535123456789", None)


def test_a_bare_steam_id_is_refused_as_ambiguous():
    """The F2 panel is the authoritative source; a bare number could be anything."""
    file_id, refusal = normalise_typed_id("76561198086248026")
    assert file_id is None
    assert "V_76561198086248026" in refusal


def test_an_empty_entry_is_refused():
    file_id, refusal = normalise_typed_id("   ")
    assert file_id is None
    assert refusal


def test_an_id_with_a_space_in_it_is_refused():
    file_id, refusal = normalise_typed_id("V_765 611")
    assert file_id is None
    assert refusal


def test_case_is_never_altered():
    assert normalise_typed_id("v_76561198086248026") == ("v_76561198086248026", None)


def test_no_conflict_when_the_variables_are_unset():
    assert overwriting_env_vars({"SERVER_NAME": "Midgard"}) == []


def test_a_set_adminlist_ids_is_reported():
    assert overwriting_env_vars({"ADMINLIST_IDS": "76561198086248026"}) == ["ADMINLIST_IDS"]


def test_an_empty_value_is_not_a_conflict():
    assert overwriting_env_vars({"ADMINLIST_IDS": "  "}) == []


def test_all_three_are_checked():
    settings = {"ADMINLIST_IDS": "a", "BANNEDLIST_IDS": "b", "PERMITTEDLIST_IDS": "c"}
    assert overwriting_env_vars(settings) == [
        "ADMINLIST_IDS",
        "BANNEDLIST_IDS",
        "PERMITTEDLIST_IDS",
    ]
