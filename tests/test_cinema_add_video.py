"""browser_actions — the "Add Cinema Video…" flow and the menu slot it sits in.

Creating a config from a link is the one Cinema operation that used to require
opening the game. The parts worth guarding here are the ones a unit test can
actually reach: which menu entry appears for which song state, that a
non-YouTube paste never reaches yt-dlp, and — the invariant the whole flow is
ordered around — that a failed download leaves no manifest behind claiming the
song has a video it doesn't.

Widget stand-ins throughout; no window is ever opened.
"""

from __future__ import annotations

import json
import types

import pytest

from libraries import browser_actions, cinema_video
from libraries.browser_actions import BrowserActionsMixin

ID = "dQw4w9WgXcQ"
URL = f"https://www.youtube.com/watch?v={ID}"


# ── Stand-ins ────────────────────────────────────────────────────────────────

class _RecordingMenu:
    """Captures what would have been shown, instead of showing it."""

    def __init__(self, *args, **kw):
        self.entries: list[tuple[str, dict]] = []
        self.popped = False

    def add_command(self, label=None, **kw):
        self.entries.append((label, kw))

    def add_separator(self):
        pass

    def tk_popup(self, *_args):
        self.popped = True

    @property
    def labels(self) -> list[str]:
        return [label for label, _ in self.entries]

    def state_of(self, label: str) -> str:
        return next(kw.get("state", "normal") for lbl, kw in self.entries if lbl == label)


class _StatusBar:
    def __init__(self):
        self.text = ""

    def config(self, text=""):
        self.text = text


class _Canvas:
    def yview(self):
        return (0.0, 1.0)

    def update_idletasks(self):
        pass

    def yview_moveto(self, _pos):
        pass


class _FakeBrowser(BrowserActionsMixin):
    """The narrow slice of SongBrowser the Cinema actions actually touch."""

    def __init__(self, clipboard: str = ""):
        self._queue: list = []
        self._cinema_downloads_active: set[str] = set()
        self._cinema_configs_created: set[str] = set()
        self.player_dat_path = None
        self.status_bar = _StatusBar()
        self.canvas = _Canvas()
        self._clipboard = clipboard
        self.rendered = 0
        self.refiltered: list = []
        self.editor_opened: list = []
        self.offset_notified: list = []
        self.ffmpeg_warmed = 0

    # SongBrowser bits the code under test calls
    def _is_favorite(self, _song):
        return False

    def _render_list(self):
        self.rendered += 1

    def _refilter_in_place(self, song=None):
        self.refiltered.append(song)
        self.rendered += 1

    def clipboard_get(self):
        if self._clipboard is None:
            raise RuntimeError("clipboard is empty")
        return self._clipboard

    def _open_cinema_offset_editor(self, song):
        self.editor_opened.append(song)

    def _notify_cinema_offset_changed(self, song):
        self.offset_notified.append(song)

    def _ensure_ffmpeg(self, on_ready=None, on_unavailable=None):
        self.ffmpeg_warmed += 1
        return True


def _event(shift: bool = False):
    return types.SimpleNamespace(state=0x1 if shift else 0, x_root=0, y_root=0)


@pytest.fixture()
def menu_class(monkeypatch):
    """Swap tk.Menu for a recorder and hand back the instances it made."""
    made: list[_RecordingMenu] = []

    def _factory(*args, **kw):
        menu = _RecordingMenu(*args, **kw)
        made.append(menu)
        return menu

    monkeypatch.setattr(browser_actions.tk, "Menu", _factory)
    return made


# ── The menu slot ────────────────────────────────────────────────────────────

def test_song_without_a_config_offers_to_add_one(song_factory, menu_class):
    _, song = song_factory()
    _FakeBrowser()._show_context_menu(_event(), song)
    labels = menu_class[0].labels
    assert "Add Cinema Video…" in labels
    assert "Download Video" not in labels


def test_add_appears_with_or_without_shift(song_factory, menu_class):
    # Adding isn't destructive, so unlike the edit menu it isn't hidden behind
    # a modifier — a user who has never held Shift can still find it.
    _, song = song_factory()
    _FakeBrowser()._show_context_menu(_event(shift=True), song)
    assert "Add Cinema Video…" in menu_class[0].labels


def test_add_is_disabled_while_a_download_is_running(song_factory, menu_class):
    _, song = song_factory()
    browser = _FakeBrowser()
    browser._cinema_downloads_active.add(str(song.folder))
    browser._show_context_menu(_event(), song)
    assert menu_class[0].state_of("Add Cinema Video…") == "disabled"


def test_undownloaded_mapper_config_still_offers_download(song_factory, menu_class):
    # The mapper shipped a config; there is nothing to create, only to fetch.
    folder, song = song_factory()
    cinema_video.create_config(folder, ID, "Some Video", "Chan", 212)
    song._parse()
    _FakeBrowser()._show_context_menu(_event(), song)
    labels = menu_class[0].labels
    assert "Download Video" in labels
    assert "Add Cinema Video…" not in labels


def test_playable_video_offers_replace_behind_shift(song_factory, menu_class):
    folder, song = song_factory()
    cinema_video.create_config(folder, ID, "Some Video", "Chan", 212)
    (folder / "Some Video.mp4").write_bytes(b"\x00fake")
    song._parse()
    assert song.has_playable_cinema_video

    _FakeBrowser()._show_context_menu(_event(shift=True), song)
    shift_labels = menu_class[0].labels
    assert "Cinema Offset…" in shift_labels
    assert "Replace Cinema Video…" in shift_labels

    _FakeBrowser()._show_context_menu(_event(), song)
    plain_labels = menu_class[1].labels
    assert "Replace Cinema Video…" not in plain_labels
    assert "Add Cinema Video…" not in plain_labels


# ── Clipboard prefill ────────────────────────────────────────────────────────

def test_clipboard_prefills_a_youtube_link():
    assert _FakeBrowser(URL)._clipboard_youtube_url() == URL


def test_clipboard_prefill_ignores_anything_else():
    assert _FakeBrowser("just some copied text")._clipboard_youtube_url() == ""
    assert _FakeBrowser("")._clipboard_youtube_url() == ""


def test_clipboard_prefill_survives_an_unreadable_clipboard():
    # tk raises rather than returning "" when the clipboard holds an image
    # or nothing at all; that must not take the dialog down with it.
    assert _FakeBrowser(None)._clipboard_youtube_url() == ""


# ── The paste step ───────────────────────────────────────────────────────────

@pytest.fixture()
def captured_dialogs(monkeypatch):
    """Route every dialog the flow uses through recorded stand-ins."""
    calls = {"asked": [], "errors": [], "confirms": []}
    answers = {"string": None, "yes_no": True}

    def _ask_string(title, prompt, **kw):
        calls["asked"].append((title, prompt, kw))
        return answers["string"]

    def _show_error(title, message, **kw):
        calls["errors"].append((title, message))

    def _ask_yes_no(title, message, **kw):
        calls["confirms"].append((title, message))
        return answers["yes_no"]

    monkeypatch.setattr(browser_actions.dialogs, "ask_string", _ask_string)
    monkeypatch.setattr(browser_actions.dialogs, "show_error", _show_error)
    monkeypatch.setattr(browser_actions.dialogs, "ask_yes_no", _ask_yes_no)
    return calls, answers


def _no_yt_dlp_allowed(browser):
    """Make any attempt to reach yt-dlp a test failure."""
    def _boom(_on_ready):
        raise AssertionError("yt-dlp should not have been reached")
    browser._ensure_yt_dlp = _boom


def test_cancelling_the_paste_dialog_does_nothing(song_factory, captured_dialogs):
    calls, answers = captured_dialogs
    answers["string"] = None
    folder, song = song_factory()
    browser = _FakeBrowser()
    _no_yt_dlp_allowed(browser)
    browser._add_cinema_video(song)
    assert not calls["errors"]
    assert cinema_video.find_config(folder) is None


def test_a_non_youtube_link_is_rejected_before_any_download(song_factory, captured_dialogs):
    calls, answers = captured_dialogs
    answers["string"] = "https://vimeo.com/123456789"
    _, song = song_factory()
    browser = _FakeBrowser()
    _no_yt_dlp_allowed(browser)
    browser._add_cinema_video(song)
    assert len(calls["errors"]) == 1
    assert "YouTube" in calls["errors"][0][1]


def test_a_timestamped_link_seeds_the_offset(song_factory, captured_dialogs):
    calls, answers = captured_dialogs
    answers["string"] = f"https://youtu.be/{ID}?t=1m30s"
    _, song = song_factory()
    browser = _FakeBrowser()

    seen: dict = {}
    browser._ensure_yt_dlp = lambda on_ready: on_ready("yt-dlp")
    browser._fetch_cinema_metadata = lambda yt, s, vid, off: seen.update(
        video_id=vid, offset_ms=off)

    browser._add_cinema_video(song)
    # video_time = song_time + offset/1000, so a link pointed at 1:30 starts
    # the video at 1:30 — usually within a second of right.
    assert seen == {"video_id": ID, "offset_ms": 90_000}


def test_replace_confirms_before_clobbering(song_factory, captured_dialogs):
    calls, answers = captured_dialogs
    answers["yes_no"] = False
    answers["string"] = URL
    folder, song = song_factory()
    cinema_video.create_config(folder, ID, "Mapper's Video", "Chan", 212)
    song._parse()

    browser = _FakeBrowser()
    _no_yt_dlp_allowed(browser)
    browser._add_cinema_video(song, replace=True)

    assert len(calls["confirms"]) == 1
    assert not calls["asked"]  # declined before the paste dialog opened
    _, data = cinema_video.load_config(folder)
    assert data["title"] == "Mapper's Video"


def test_add_does_not_confirm(song_factory, captured_dialogs):
    calls, answers = captured_dialogs
    answers["string"] = None
    _, song = song_factory()
    browser = _FakeBrowser()
    _no_yt_dlp_allowed(browser)
    browser._add_cinema_video(song)
    assert not calls["confirms"]
    assert len(calls["asked"]) == 1


# ── Ordering: the manifest is written last ───────────────────────────────────

def _download_result(browser, song, ok: bool, duration_s: int = 212):
    title = "Some Video"
    video_file = cinema_video.derive_video_filename(song.folder, title, ID)
    browser._on_cinema_video_downloaded(
        song, ID, title, "Some Channel", duration_s, 0,
        video_file, song.folder / video_file, ok, "yt-dlp said no",
    )
    return video_file


def test_a_failed_download_leaves_no_manifest(song_factory, captured_dialogs):
    calls, _ = captured_dialogs
    folder, song = song_factory()
    browser = _FakeBrowser()
    _download_result(browser, song, ok=False)

    # The whole flow is ordered around this: a config written before the
    # download would leave the song advertising a video it doesn't have,
    # recoverable only by deleting the file by hand.
    assert cinema_video.find_config(folder) is None
    assert len(calls["errors"]) == 1
    assert not browser.editor_opened


def test_a_failed_download_cleans_up_its_partial_file(song_factory, captured_dialogs):
    # --no-part means a download that dies partway writes a truncated file at
    # the real name. Left there, yt-dlp would call the next attempt
    # "already downloaded" and exit 0 on a broken video.
    folder, song = song_factory()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00half a video")

    _download_result(_FakeBrowser(), song, ok=False)
    assert not (folder / video_file).exists()


def test_a_successful_download_writes_the_manifest_and_opens_the_editor(
    song_factory, captured_dialogs
):
    calls, _ = captured_dialogs
    folder, song = song_factory()
    browser = _FakeBrowser()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00fake")

    _download_result(browser, song, ok=True)

    path = cinema_video.find_config(folder)
    assert path is not None
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["videoID"] == ID
    assert data["videoFile"] == video_file
    assert "configByMapper" not in data

    assert not calls["errors"]
    assert song.has_playable_cinema_video       # re-parsed, so the row updates
    assert browser.rendered == 1
    assert browser.offset_notified == [song]
    # A fresh config is at offset 0 and therefore almost certainly out of
    # sync, so the editor is the next step, not an afterthought.
    assert browser.editor_opened == [song]


def test_writing_the_config_recomputes_the_filter(song_factory, captured_dialogs):
    """Not just a redraw: the song's {cinema} value has flipped, and under
    ``{cinema}:y`` it isn't in ``filtered`` at all until the filter is re-run —
    so the row the user just created would never appear."""
    folder, song = song_factory()
    browser = _FakeBrowser()
    (folder / cinema_video.derive_video_filename(folder, "Some Video", ID)).write_bytes(b"x")

    _download_result(browser, song, ok=True)

    assert browser.refiltered == [song]


# ── Backups: only where there is something to restore to ─────────────────────

def test_a_config_created_from_scratch_is_marked_as_such(
    song_factory, captured_dialogs
):
    folder, song = song_factory()
    browser = _FakeBrowser()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00fake")

    _download_result(browser, song, ok=True)

    # The offset editor reads this to decide whether saving writes a .bak.
    assert browser._cinema_configs_created == {str(folder)}
    assert not (folder / "cinema-video.json.bak").exists()


def test_replacing_a_config_is_not_marked_as_created(
    song_factory, captured_dialogs
):
    # create_config already backed the mapper's config up; that .bak is the
    # real thing to restore to, and the editor must leave it alone rather
    # than treat this as a from-scratch config.
    folder, song = song_factory()
    cinema_video.create_config(folder, ID, "Mapper's Video", "Chan", 212)
    song._parse()

    browser = _FakeBrowser()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00fake")
    _download_result(browser, song, ok=True)

    assert browser._cinema_configs_created == set()
    bak = folder / "cinema-video.json.bak"
    assert json.loads(bak.read_text(encoding="utf-8"))["title"] == "Mapper's Video"


def test_saving_a_from_scratch_config_writes_no_backup(tmp_path):
    # The .bak undoes an edit back to what was on disk before we touched the
    # song. For a config we just wrote, that is nothing — a backup would only
    # offer to restore our own unsynced offset-0 config.
    cinema_video.create_config(tmp_path, ID, "T", "A", 212)
    cinema_video.save_offset(tmp_path, 1_500, backup=False)

    assert not (tmp_path / "cinema-video.json.bak").exists()
    _, data = cinema_video.load_config(tmp_path)
    assert data["offset"] == 1_500


def test_backup_is_still_the_default(tmp_path):
    # Every other caller — a mapper's config, one Cinema wrote in-game —
    # depends on the .bak, so opting out has to be explicit.
    cinema_video.create_config(tmp_path, ID, "T", "A", 212)
    cinema_video.save_offset(tmp_path, 1_500)
    assert (tmp_path / "cinema-video.json.bak").exists()


def test_a_missing_duration_is_probed_from_the_downloaded_file(
    song_factory, captured_dialogs, monkeypatch
):
    # Livestreams and some music-service uploads have no duration in
    # --dump-json; the file is on disk by then, so probe it.
    import libraries.audio_utils as audio_utils
    monkeypatch.setattr(audio_utils, "get_audio_duration", lambda _p: 187.6)

    folder, song = song_factory()
    browser = _FakeBrowser()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00fake")

    _download_result(browser, song, ok=True, duration_s=0)
    _, data = cinema_video.load_config(folder)
    assert data["duration"] == 187


def test_an_unprobeable_duration_still_writes_the_config(
    song_factory, captured_dialogs, monkeypatch
):
    # song_data tolerates 0 and the editor probes the file itself, so a
    # duration we can't determine is not a reason to fail the whole create.
    import libraries.audio_utils as audio_utils
    monkeypatch.setattr(audio_utils, "get_audio_duration", lambda _p: None)

    folder, song = song_factory()
    browser = _FakeBrowser()
    video_file = cinema_video.derive_video_filename(folder, "Some Video", ID)
    (folder / video_file).write_bytes(b"\x00fake")

    _download_result(browser, song, ok=True, duration_s=0)
    _, data = cinema_video.load_config(folder)
    assert data["duration"] == 0
