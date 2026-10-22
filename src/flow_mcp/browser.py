from __future__ import annotations

import asyncio
import os
import re
import signal
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, Page

FLOW_URL = "https://flow.google.com/"
PROJECT = re.compile(r"/project/([a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})(?:/|$)", re.I)
FLOW_PATH = re.compile(r"^/fx/(?:[a-z-]+/)?tools/flow(?:/|$)")

# Same account evidence and visibility rules as extension/background.js:inspectPage.
# Return a boolean only: account identifiers never cross the page boundary.
ACCOUNT_PROBE = r"""() => {
  const visible = node => {
    if (!node.getClientRects().length) return false;
    for (let current = node; current; current = current.parentElement) {
      const style = getComputedStyle(current);
      if (current.hidden || current.getAttribute('aria-hidden') === 'true' ||
          style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility) ||
          style.opacity === '0') return false;
    }
    return true;
  };
  const emails = new Set();
  let ambiguous = false;
  for (const node of document.querySelectorAll(
      'button[aria-label], a[aria-label], [role="button"][aria-label]')) {
    const label = node.getAttribute('aria-label') || '';
    if (!/^(?:Google Account|Conta do Google):\s/i.test(label) || !visible(node)) continue;
    const matches = label.match(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi) || [];
    if (matches.length !== 1) ambiguous = true;
    else emails.add(matches[0].toLowerCase());
  }
  return !ambiguous && emails.size === 1;
}"""


class BrowserError(Exception):
    pass


@dataclass(frozen=True)
class BrowserState:
    state: Literal["account_observed", "login_required", "unknown", "navigation_failed"]
    headless: bool
    flow_origin: str | None = None
    project_id: str | None = None
    account_detected: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def browser_directory() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "openflow" / "browser"


def profile_paths() -> tuple[Path, Path]:
    """Return the Chromium profile directory and its lock file.

    OPENFLOW_PROFILE selects an existing profile in place; its cookies are never
    copied. The lock lives beside the profile so external profiles keep their
    own single-instance guard.
    """
    override = os.environ.get("OPENFLOW_PROFILE")
    if override:
        profile = Path(override).expanduser()
        if not profile.is_absolute():
            raise BrowserError("OPENFLOW_PROFILE must be an absolute path.")
        return profile, profile.with_name(profile.name + ".lock")
    directory = browser_directory()
    return directory / "profile", directory / "profile.lock"


def classify_state(url: str, account_detected: bool, headless: bool) -> BrowserState:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.port is not None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return BrowserState("unknown", headless)
        if parsed.hostname == "accounts.google.com":
            return BrowserState("login_required", headless)
        is_flow = parsed.hostname == "flow.google.com" or (
            parsed.hostname == "labs.google" and FLOW_PATH.match(parsed.path) is not None
        )
        if not is_flow:
            return BrowserState("unknown", headless)
        project = PROJECT.search(parsed.path)
        return BrowserState(
            "account_observed" if account_detected else "unknown",
            headless,
            f"https://{parsed.hostname}",
            project.group(1) if project else None,
            account_detected,
        )
    except ValueError:
        return BrowserState("unknown", headless)


@contextmanager
def profile_lock(directory: Path) -> Iterator[Path]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink():
        raise BrowserError("Browser data directory must not be a symlink.")
    directory.chmod(0o700)
    profile = directory / "profile"
    if profile.is_symlink():
        raise BrowserError("Browser profile must not be a symlink.")
    profile.mkdir(exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    with with_lock(profile, directory / "profile.lock") as locked:
        yield locked


@contextmanager
def with_lock(profile: Path, lock: Path) -> Iterator[Path]:
    """Lock an existing profile in place without copying or relinking it."""
    if profile.is_symlink():
        raise BrowserError("Browser profile must not be a symlink.")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            # Windows locking needs a byte, and locks from the current file position.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise BrowserError("Browser profile is in use; close the other instance.") from None
        else:
            import fcntl

            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BrowserError("Browser profile is in use; close the other instance.") from None
        # Keep the lock file: unlinking it would allow locking different inodes.
        yield profile
    finally:
        os.close(fd)


@asynccontextmanager
async def browser_session(headless: bool) -> AsyncIterator[BrowserContext]:
    try:
        from playwright.async_api import Error, async_playwright
    except ImportError:
        raise BrowserError(
            "Browser support is optional. Run: uv sync --extra browser; "
            "uv run --extra browser playwright install chromium --no-shell"
        ) from None
    profile, lock = profile_paths()
    with with_lock(profile, lock) as profile:
        async with async_playwright() as playwright:
            try:
                context = await playwright.chromium.launch_persistent_context(
                    str(profile),
                    channel="chromium",
                    headless=headless,
                    accept_downloads=False,
                    # Match manual Chromium's OS credential store for saved session cookies.
                    ignore_default_args=["--password-store=basic", "--use-mock-keychain"],
                )
            except Error:
                raise BrowserError(
                    "Chromium could not start. Install it with: "
                    "uv run --extra browser playwright install chromium --no-shell. "
                    "Check OS browser dependencies and DISPLAY for headed mode; "
                    "also ensure this profile is not open in another browser."
                ) from None
            try:
                yield context
            finally:
                await context.close()


async def inspect_page(page: Page, headless: bool, wait_seconds: float = 15) -> BrowserState:
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        # Do not inspect account data on Google login or unrelated pages.
        state = classify_state(page.url, False, headless)
        if state.state == "login_required":
            return state
        if state.flow_origin is not None:
            detected = await page.evaluate(ACCOUNT_PROBE)
            state = classify_state(page.url, detected is True, headless)
            if state.account_detected:
                return state
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return state
        await asyncio.sleep(min(0.25, remaining))


async def run_browser(*, headless: bool, inspect: bool) -> BrowserState | None:
    from playwright.async_api import Error
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    async with browser_session(headless) as context:
        closed = asyncio.Event()
        context.on("close", lambda _: closed.set())
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=30_000)
            if inspect:
                async with asyncio.timeout(20):
                    return await inspect_page(page, headless)
        except (TimeoutError, PlaywrightTimeout, Error):
            if inspect:
                return BrowserState("navigation_failed", headless)
            raise BrowserError(
                "Flow could not be opened; check the network and try again."
            ) from None
        if not inspect:
            await closed.wait()
    return None


STOP_TIMEOUT = 3.0


@contextmanager
def _handle_sigterm() -> Iterator[None]:
    # asyncio.run handles Ctrl+C. Also cancel gracefully on SIGTERM where supported.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    installed = False
    if task is not None and os.name != "nt":
        try:
            loop.add_signal_handler(signal.SIGTERM, task.cancel)
            installed = True
        except (ValueError, RuntimeError):
            pass
    try:
        yield
    finally:
        if installed:
            try:
                loop.remove_signal_handler(signal.SIGTERM)
            except (ValueError, RuntimeError):
                pass


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
        async with asyncio.timeout(STOP_TIMEOUT):
            await asyncio.shield(process.wait())
    except (TimeoutError, asyncio.CancelledError, ProcessLookupError, OSError):
        pass
    if process.returncode is None:
        try:
            process.kill()
            await asyncio.shield(process.wait())
        except (ProcessLookupError, OSError):
            pass


async def locate_chromium() -> Path:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError:
        raise BrowserError(
            "Browser support is optional. Run: uv sync --extra browser; "
            "uv run --extra browser playwright install chromium --no-shell"
        ) from None
    try:
        async with async_playwright() as playwright:
            executable = Path(str(playwright.chromium.executable_path))
    except (PlaywrightError, OSError):
        raise BrowserError(
            "Chromium could not start. Install it with: "
            "uv run --extra browser playwright install chromium --no-shell. "
            "Check OS browser dependencies and DISPLAY for headed mode; "
            "also ensure this profile is not open in another browser."
        ) from None
    is_file = await asyncio.to_thread(executable.is_file)
    if not is_file:
        raise BrowserError(
            "Chromium could not start. Install it with: "
            "uv run --extra browser playwright install chromium --no-shell. "
            "Check OS browser dependencies and DISPLAY for headed mode; "
            "also ensure this profile is not open in another browser."
        )
    return executable


async def native_setup() -> None:
    with _handle_sigterm():
        executable = await locate_chromium()
        profile, lock = profile_paths()
        with with_lock(profile, lock) as profile:
            cmd = [
                str(executable),
                f"--user-data-dir={profile}",
                FLOW_URL,
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except (OSError, FileNotFoundError):
                raise BrowserError(
                    "Chromium could not start. Install it with: "
                    "uv run --extra browser playwright install chromium --no-shell. "
                    "Check OS browser dependencies and DISPLAY for headed mode; "
                    "also ensure this profile is not open in another browser."
                ) from None

            try:
                await process.wait()
            finally:
                await _stop_process(process)

            if (
                process.returncode != 0
                and process.returncode not in (-signal.SIGINT, -signal.SIGTERM, 130, 143)
            ):
                raise BrowserError(
                    "Chromium could not start. Install it with: "
                    "uv run --extra browser playwright install chromium --no-shell. "
                    "Check OS browser dependencies and DISPLAY for headed mode; "
                    "also ensure this profile is not open in another browser."
                )


async def browser_command(
    *,
    headless: bool = False,
    inspect: bool = False,
    setup: bool = False,
) -> BrowserState | None:
    with _handle_sigterm():
        # Check the optional extra before run_browser imports its exception types.
        try:
            import playwright.async_api  # noqa: F401
        except ImportError:
            raise BrowserError(
                "Browser support is optional. Run: uv sync --extra browser; "
                "uv run --extra browser playwright install chromium --no-shell"
            ) from None
        if setup:
            await native_setup()
            return None
        return await run_browser(headless=headless, inspect=inspect)
