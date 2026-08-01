"""browser_pagination — recomputing the filter after a song's data changes.

The reported bug: create a Cinema config while ``{cinema}:y`` is the active
search and the song never appears. ``_render_list`` redraws ``self.filtered``
but never recomputes it, so the song kept the membership it had when the query
was last typed — and the one place you can create a config for a song the
filter is currently hiding is the Visualizer, which is exactly where you'd be
when it matters.

What's held here is that membership follows the data both ways, and that it
does so without throwing the user off the page they were looking at.

Widget stand-ins throughout; no window is ever opened.
"""

from __future__ import annotations

import pytest

from libraries import cinema_video
from libraries.browser_pagination import BrowserPaginationMixin

ID = "dQw4w9WgXcQ"


class _SearchVar:
    def __init__(self, text: str = ""):
        self._text = text

    def get(self) -> str:
        return self._text

    def set(self, text: str) -> None:
        self._text = text


class _Canvas:
    def __init__(self):
        self.moved_to = None

    def yview(self):
        return (0.42, 1.0)

    def update_idletasks(self):
        pass

    def yview_moveto(self, pos):
        self.moved_to = pos


class _StatusBar:
    def config(self, **_kw):
        pass


class _FakeBrowser(BrowserPaginationMixin):
    """The slice of SongBrowser ``_refilter_in_place`` actually touches."""

    def __init__(self, songs, query: str = "", page_size: int = 50):
        self.songs = list(songs)
        self.filtered = list(songs)
        self.search_var = _SearchVar(query)
        self.canvas = _Canvas()
        self.status_bar = _StatusBar()
        self.page = 0
        self.page_size = page_size
        self.player_stats = {}
        self.favorite_ids = set()
        self._selected_folders = set()
        self.selected_indices = set()
        self.selected_index = None
        self._pending_install_id = None
        self._pending_playlist_url = None
        self._favorites_only = False
        self.renders = 0

    def _apply_view_filters(self, songs):
        return songs

    def _render_list(self):
        self.renders += 1

    @property
    def folders(self) -> list[str]:
        return [s.folder.name for s in self.filtered]


@pytest.fixture()
def songs(song_factory):
    """Three songs, none with a Cinema config; ``add_cinema`` gives one one."""
    made = [song_factory(f"song{i} (Test Song - Test Mapper)")[1] for i in range(3)]

    def add_cinema(song):
        cinema_video.create_config(song.folder, ID, "Some Video", "Chan", 212)
        song._parse()
        assert song.has_cinema

    return made, add_cinema


# ── Membership follows the data ──────────────────────────────────────────────

def test_a_new_config_joins_the_cinema_y_filter(songs):
    """The reported bug. Under {cinema}:y the song isn't in ``filtered`` to
    begin with, so redrawing could never have shown it."""
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{cinema}:y")
    browser.filtered = []
    add_cinema(made[1])

    browser._refilter_in_place(made[1])

    assert browser.folders == ["song1 (Test Song - Test Mapper)"]


def test_a_new_config_leaves_the_cinema_n_filter(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{cinema}:n")
    add_cinema(made[1])

    browser._refilter_in_place(made[1])

    assert made[1] not in browser.filtered
    assert len(browser.filtered) == 2


def test_the_other_songs_are_left_where_they_were(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{cinema}:n")
    add_cinema(made[1])

    browser._refilter_in_place(made[1])

    assert browser.folders == ["song0 (Test Song - Test Mapper)",
                              "song2 (Test Song - Test Mapper)"]


def test_an_empty_query_keeps_every_song(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made)
    add_cinema(made[0])

    browser._refilter_in_place(made[0])

    assert browser.filtered == made


def test_a_query_that_does_not_mention_cinema_is_unaffected(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{mapper}:\"Test Mapper\"")
    add_cinema(made[0])

    browser._refilter_in_place(made[0])

    assert len(browser.filtered) == 3


def test_the_list_is_redrawn(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{cinema}:y")
    add_cinema(made[0])

    browser._refilter_in_place(made[0])

    assert browser.renders == 1


# ── The same defect in the other editors ─────────────────────────────────────

def test_a_new_custom_tag_joins_the_custom_filter(songs):
    """The custom-tags dialog can add a tag to a song the current {custom}
    filter is hiding — the same shape as the Cinema bug."""
    made, _ = songs
    browser = _FakeBrowser(made, query="{custom}:sightread")
    browser.filtered = []
    made[2].custom_tags = frozenset({"sightread"})

    browser._refilter_in_place()

    assert browser.filtered == [made[2]]


def test_a_removed_custom_tag_leaves_the_custom_filter(songs):
    made, _ = songs
    for song in made:
        song.custom_tags = frozenset({"sightread"})
    browser = _FakeBrowser(made, query="{custom}:sightread")
    made[1].custom_tags = frozenset()

    browser._refilter_in_place()

    assert made[1] not in browser.filtered
    assert len(browser.filtered) == 2


def test_a_rename_that_leaves_the_query_drops_the_row(songs):
    """Edit Info can rename a song out of the search that found it; the row
    would otherwise sit there claiming a match it no longer has."""
    made, _ = songs
    browser = _FakeBrowser(made, query='{mapper}:"Test Mapper"')
    made[0].mapper = "Someone Else"

    browser._refilter_in_place(made[0])

    assert made[0] not in browser.filtered
    assert len(browser.filtered) == 2


def test_a_rename_that_matches_the_query_adds_the_row(songs):
    made, _ = songs
    browser = _FakeBrowser(made, query='{artist}:"Novel Artist"')
    browser.filtered = []
    made[1].author = "Novel Artist"

    browser._refilter_in_place(made[1])

    assert browser.filtered == [made[1]]


# ── The user's place is kept ─────────────────────────────────────────────────

def test_scroll_position_is_restored(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made)
    add_cinema(made[0])

    browser._refilter_in_place(made[0])

    assert browser.canvas.moved_to == 0.42


def test_the_page_follows_the_song_that_changed(songs, song_factory):
    """Without this the song can land on a page the user isn't looking at,
    which reads exactly like the bug being fixed."""
    many = [song_factory(f"m{i:02d} (Test Song - Test Mapper)")[1] for i in range(10)]
    _, add_cinema = songs
    browser = _FakeBrowser(many, query="{cinema}:y", page_size=4)
    browser.filtered = []
    for song in many[:6]:
        add_cinema(song)

    browser._refilter_in_place(many[5])

    assert browser.page == 1  # index 5 of 6 matches, page size 4


def test_the_page_is_held_when_the_song_is_not_given(songs, song_factory):
    many = [song_factory(f"m{i:02d} (Test Song - Test Mapper)")[1] for i in range(10)]
    browser = _FakeBrowser(many, page_size=4)
    browser.page = 2

    browser._refilter_in_place()

    assert browser.page == 2


def test_the_page_is_clamped_when_the_list_shrinks(songs, song_factory):
    """A song leaving the filter can empty the last page out from under the
    user; landing on a blank list looks like everything vanished."""
    many = [song_factory(f"m{i:02d} (Test Song - Test Mapper)")[1] for i in range(5)]
    browser = _FakeBrowser(many, query="{cinema}:n", page_size=4)
    browser.page = 1
    cinema_video.create_config(many[4].folder, ID, "V", "C", 1)
    many[4]._parse()

    browser._refilter_in_place(many[4])

    assert browser.page == 0
    assert len(browser.filtered) == 4


def test_selection_indices_are_remapped(songs):
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="{cinema}:n")
    browser._selected_folders = {str(made[2].folder)}
    add_cinema(made[0])

    browser._refilter_in_place(made[0])

    # song0 dropped out, so song2 is now index 1 rather than 2.
    assert browser.selected_indices == {1}
    assert browser.selected_index == 1


# ── The special query forms own their own list ───────────────────────────────

def test_a_pending_install_is_left_alone(songs):
    """``filtered`` is empty on purpose there — the row on screen is an install
    prompt, not a song, and recomputing would fight it."""
    made, add_cinema = songs
    browser = _FakeBrowser(made, query="abc1")
    browser.filtered = []
    browser._pending_install_id = "abc1"

    browser._refilter_in_place(made[0])

    assert browser.filtered == []
    assert browser.renders == 0


def test_a_pending_playlist_url_is_left_alone(songs):
    made, _ = songs
    browser = _FakeBrowser(made, query="bsplaylist://playlist/https://x.test/p.bplist")
    browser.filtered = []
    browser._pending_playlist_url = "https://x.test/p.bplist"

    browser._refilter_in_place(made[0])

    assert browser.filtered == []
    assert browser.renders == 0


@pytest.mark.parametrize("query", [
    "https://beatsaver.com/maps/2e7f",
    "beatsaver://2e7f",
])
def test_a_song_id_url_query_is_left_alone(songs, query):
    """The ID search sets ``filtered`` to the one matching song; the tag filter
    was never consulted and mustn't be applied on top of it now."""
    made, _ = songs
    browser = _FakeBrowser(made, query=query)
    browser.filtered = [made[0]]

    browser._refilter_in_place(made[0])

    assert browser.filtered == [made[0]]
    assert browser.renders == 0
