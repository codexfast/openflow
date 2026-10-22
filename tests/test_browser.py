import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from flow_mcp.browser import (
    ACCOUNT_PROBE,
    FLOW_URL,
    BrowserError,
    browser_command,
    browser_directory,
    browser_session,
    classify_state,
    inspect_page,
    locate_chromium,
    native_setup,
    profile_lock,
    profile_paths,
    run_browser,
)

PROJECT = "12345678-1234-1234-1234-123456789abc"


def test_default_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert browser_directory() == tmp_path / "openflow" / "browser"


def test_default_profile_paths(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENFLOW_PROFILE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert profile_paths() == (
        tmp_path / "openflow" / "browser" / "profile",
        tmp_path / "openflow" / "browser" / "profile.lock",
    )


def test_existing_profile_used_in_place(monkeypatch, tmp_path):
    profile = tmp_path / "other" / "profile"
    profile.mkdir(parents=True)
    sentinel = profile / "Local State"
    sentinel.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OPENFLOW_PROFILE", str(profile))
    assert profile_paths() == (profile, profile.with_name("profile.lock"))
    # Selecting an existing profile must not copy or relocate its contents.
    assert sorted(p.name for p in profile.iterdir()) == ["Local State"]


def test_relative_profile_rejected(monkeypatch):
    from flow_mcp.browser import BrowserError, profile_paths

    monkeypatch.setenv("OPENFLOW_PROFILE", "relative/profile")
    with pytest.raises(BrowserError, match="absolute"):
        profile_paths()


def test_lock_exclusion_and_release(tmp_path):
    directory = tmp_path / "browser"
    with profile_lock(directory) as profile:
        assert profile == directory / "profile"
        if os.name != "nt":
            assert directory.stat().st_mode & 0o777 == 0o700
            assert profile.stat().st_mode & 0o777 == 0o700
        with pytest.raises(BrowserError, match="in use"), profile_lock(directory):
            pass
    with pytest.raises(ValueError), profile_lock(directory):
        raise ValueError("failed operation")
    with profile_lock(directory):
        pass


def test_symlink_profile_rejected(tmp_path):
    directory = tmp_path / "browser"
    directory.mkdir()
    target = tmp_path / "personal"
    target.mkdir()
    (directory / "profile").symlink_to(target, target_is_directory=True)
    with pytest.raises(BrowserError, match="symlink"), profile_lock(directory):
        pass


@pytest.mark.parametrize(
    "url",
    [
        "http://flow.google.com/",
        "https://flow.google.com.evil/",
        "https://flow.google.com:443/",
        "https://user@flow.google.com/",
        "https://labs.google/not-flow",
        "https://evil/",
        "https://[invalid",
    ],
)
def test_foreign_urls_never_authenticated(url):
    assert classify_state(url, True, True).state == "unknown"
    assert not classify_state(url, True, True).account_detected


def test_state_classification():
    for url in (
        f"https://flow.google.com/project/{PROJECT}?secret=private",
        f"https://labs.google/fx/pt/tools/flow/project/{PROJECT}",
    ):
        state = classify_state(url, True, True)
        assert state.state == "account_observed"
        assert state.project_id == PROJECT
        assert "private" not in str(state.to_dict())
    assert classify_state("https://accounts.google.com/challenge", True, True).state == (
        "login_required"
    )
    assert classify_state("https://flow.google.com/", False, False).state == "unknown"


async def test_inspect_only_evaluates_recognized_flow():
    page = MagicMock(url="https://accounts.google.com/signin")
    page.evaluate = AsyncMock()
    assert (await inspect_page(page, True, wait_seconds=0)).state == "login_required"
    page.evaluate.assert_not_called()
    page.url = "https://flow.google.com/"
    page.evaluate.return_value = True
    assert (await inspect_page(page, True, wait_seconds=0)).account_detected
    page.evaluate.assert_awaited_once_with(ACCOUNT_PROBE)
    page.evaluate.return_value = False
    assert (await inspect_page(page, True, wait_seconds=0)).state == "unknown"


@pytest.fixture
def fake_playwright(monkeypatch, tmp_path):
    api = pytest.importorskip("playwright.async_api")
    context = MagicMock()
    context.close = AsyncMock()
    page = MagicMock(url="https://flow.google.com/")
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(return_value=True)
    context.pages = [page]
    playwright = MagicMock()
    playwright.chromium.launch_persistent_context = AsyncMock(return_value=context)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(api, "async_playwright", lambda: manager)
    monkeypatch.setattr("flow_mcp.browser.browser_directory", lambda: tmp_path / "browser")
    return playwright, context, page


async def test_session_closes_on_failure_and_releases_lock(fake_playwright, tmp_path):
    playwright, context, _ = fake_playwright
    with pytest.raises(ValueError):
        async with browser_session(True):
            raise ValueError("failure")
    context.close.assert_awaited_once()
    playwright.chromium.launch_persistent_context.assert_awaited_once_with(
        str(tmp_path / "browser" / "profile"),
        channel="chromium",
        headless=True,
        accept_downloads=False,
        ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
    )
    with profile_lock(tmp_path / "browser"):
        pass


async def test_session_closes_on_cancellation(fake_playwright):
    _, context, _ = fake_playwright
    started = asyncio.Event()

    async def work():
        async with browser_session(False):
            started.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(work())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    context.close.assert_awaited_once()


async def test_inspect_and_navigation_timeout(fake_playwright):
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    _, context, page = fake_playwright
    state = await run_browser(headless=True, inspect=True)
    assert state.account_detected
    page.goto.assert_awaited_once()
    context.close.assert_awaited_once()
    page.goto.side_effect = PlaywrightTimeout("sensitive error")
    state = await run_browser(headless=True, inspect=True)
    assert state.state == "navigation_failed"
    assert "sensitive" not in str(state)


async def test_foreground_waits_for_close(fake_playwright):
    _, context, _ = fake_playwright
    context.on.side_effect = lambda event, callback: callback(context)
    assert await run_browser(headless=False, inspect=False) is None
    context.close.assert_awaited_once()


async def test_locate_chromium_success(fake_playwright, tmp_path):
    playwright, _, _ = fake_playwright
    chrome_bin = tmp_path / "chrome"
    chrome_bin.write_text("")
    playwright.chromium.executable_path = str(chrome_bin)
    assert await locate_chromium() == chrome_bin


async def test_locate_chromium_missing_binary(fake_playwright, tmp_path):
    playwright, _, _ = fake_playwright
    playwright.chromium.executable_path = str(tmp_path / "missing_chrome")
    with pytest.raises(BrowserError, match="Chromium could not start"):
        await locate_chromium()


async def test_locate_chromium_missing_playwright(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    with pytest.raises(BrowserError, match="uv sync --extra browser"):
        await locate_chromium()


async def test_native_setup_subprocess_execution(fake_playwright, tmp_path, monkeypatch):
    playwright, _, _ = fake_playwright
    chrome_bin = tmp_path / "chrome"
    chrome_bin.write_text("")
    playwright.chromium.executable_path = str(chrome_bin)

    mock_process = AsyncMock()
    mock_process.returncode = 0
    mock_process.wait = AsyncMock(return_value=0)
    mock_create_subprocess = AsyncMock(return_value=mock_process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_create_subprocess)

    await native_setup()

    mock_create_subprocess.assert_awaited_once()
    args, kwargs = mock_create_subprocess.call_args
    assert args[0] == str(chrome_bin)
    assert f"--user-data-dir={tmp_path / 'browser' / 'profile'}" in args
    assert FLOW_URL in args
    # Ensure native keyring is preserved (no basic/mock keychain or automation flags)
    assert "--password-store=basic" not in args
    assert "--use-mock-keychain" not in args
    assert "--enable-automation" not in args
    assert kwargs.get("stdout") == asyncio.subprocess.DEVNULL
    assert kwargs.get("stderr") == asyncio.subprocess.DEVNULL
    # Ensure profile lock was released
    with profile_lock(tmp_path / "browser"):
        pass


async def test_native_setup_cancellation(fake_playwright, tmp_path, monkeypatch):
    playwright, _, _ = fake_playwright
    chrome_bin = tmp_path / "chrome"
    chrome_bin.write_text("")
    playwright.chromium.executable_path = str(chrome_bin)

    mock_process = MagicMock()
    mock_process.returncode = None
    started = asyncio.Event()

    async def fake_wait():
        started.set()
        await asyncio.Event().wait()

    mock_process.wait = AsyncMock(side_effect=fake_wait)

    def do_terminate():
        mock_process.returncode = -15
        mock_process.wait.side_effect = None
        mock_process.wait.return_value = -15

    mock_process.terminate = MagicMock(side_effect=do_terminate)
    mock_process.kill = MagicMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_process))

    task = asyncio.create_task(native_setup())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mock_process.terminate.assert_called_once()
    # Profile lock released on cancellation
    with profile_lock(tmp_path / "browser"):
        pass


async def test_native_setup_kill_fallback_on_timeout(fake_playwright, tmp_path, monkeypatch):
    playwright, _, _ = fake_playwright
    chrome_bin = tmp_path / "chrome"
    chrome_bin.write_text("")
    playwright.chromium.executable_path = str(chrome_bin)

    mock_process = MagicMock()
    mock_process.returncode = None
    started = asyncio.Event()

    async def hanging_wait():
        started.set()
        await asyncio.sleep(100)

    mock_process.wait = AsyncMock(side_effect=hanging_wait)

    def do_kill():
        mock_process.returncode = -9
        mock_process.wait.side_effect = None
        mock_process.wait.return_value = -9

    mock_process.terminate = MagicMock()
    mock_process.kill = MagicMock(side_effect=do_kill)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_process))

    from flow_mcp import browser

    monkeypatch.setattr(browser, "STOP_TIMEOUT", 0.01)

    task = asyncio.create_task(native_setup())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mock_process.terminate.assert_called_once()
    mock_process.kill.assert_called_once()


async def test_native_setup_failure_exits_with_browser_error(
    fake_playwright, tmp_path, monkeypatch
):
    playwright, _, _ = fake_playwright
    chrome_bin = tmp_path / "chrome"
    chrome_bin.write_text("")
    playwright.chromium.executable_path = str(chrome_bin)

    mock_process = AsyncMock()
    mock_process.returncode = 1
    mock_process.wait = AsyncMock(return_value=1)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=mock_process))

    with pytest.raises(BrowserError, match="Chromium could not start"):
        await native_setup()
    # Lock released even after failure
    with profile_lock(tmp_path / "browser"):
        pass


async def test_browser_command_routes_setup(monkeypatch):
    mock_setup = AsyncMock()
    monkeypatch.setattr("flow_mcp.browser.native_setup", mock_setup)
    result = await browser_command(setup=True)
    assert result is None
    mock_setup.assert_awaited_once_with()
