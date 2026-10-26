from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from flow_mcp.jobs import Jobs
from flow_mcp.managed import ManagedBrowser, runtime_source
from flow_mcp.manager import BridgeError, Manager
from flow_mcp.operations import ImageInput
from flow_mcp.service import Operations

A = str(uuid4())
B = str(uuid4())


def driver_at(project=None):
    page = MagicMock()
    page.url = "https://flow.google.com/" + (f"project/{project}" if project else "")
    page.wait_for_function = AsyncMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(
        return_value={"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"}
    )
    manager = Manager()
    driver = ManagedBrowser(manager, page)
    driver.session = manager.register_local(driver, str(uuid4()))
    driver.session.selected = driver.scope()
    return manager, driver, page


async def test_navigation_happens_before_preview_and_confirm_never_navigates():
    manager, driver, page = driver_at()

    async def goto(url, **kwargs):
        page.url = url
        driver.navigated(page.main_frame)

    page.goto.side_effect = goto
    operations = Operations(manager, Jobs(":memory:"))
    preview = await operations.submit(
        "generate_image", driver.session.id, A, ImageInput(prompt="x"), False, None
    )
    assert preview["state"] == "preview"
    assert driver.session.selected.project_id == A
    page.goto.assert_awaited_once()
    old_document = driver.document_id
    await manager.prepare_project(driver.session, B)
    assert driver.document_id != old_document
    count = page.goto.await_count
    with pytest.raises(BridgeError):
        await operations.submit(
            "generate_image", driver.session.id, A, ImageInput(prompt="x"), True, preview["job_id"]
        )
    assert page.goto.await_count == count
    assert page.evaluate.await_count >= 1


async def test_wrong_project_blocks_runtime_execution():
    _, driver, page = driver_at(A)
    wrong = driver.scope().model_copy(update={"project_id": B})
    with pytest.raises(BridgeError, match="scope_changed"):
        await driver.dispatch("generate_image", {"scope": wrong.model_dump(), "input": {}})
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    "operation",
    [
        "generate_image",
        "generate_video",
        "upscale_image",
        "upscale_video",
        "upload",
        "poll",
        "find_project_media",
    ],
)
async def test_dispatch_reuses_https_runtime_and_preserves_outcome(operation):
    _, driver, page = driver_at(A)
    scope = driver.scope()
    outcome = {
        "scope": scope.model_dump(),
        "state": "not_submitted",
        "error": "native_recaptcha_automation_unsupported",
    }
    page.evaluate.side_effect = [
        {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"},
        outcome,
        {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"},
    ]
    assert await driver.dispatch(operation, {"scope": scope.model_dump(), "input": {}}) == outcome
    args = page.evaluate.await_args_list[1].args
    assert args[0] == runtime_source()
    assert args[1][0] == operation
    assert args[1][1]["scope"] == scope.model_dump()


async def test_account_switch_invalidates_session():
    manager, driver, page = driver_at(A)
    await driver.verify_scope(driver.scope())
    epoch = driver.session.generation
    page.evaluate.return_value = {
        "email": "other@example.com",
        "observed_at": "2026-10-17T00:00:00.000Z",
    }
    with pytest.raises(BridgeError, match="scope_changed"):
        await driver.verify_scope(driver.scope())
    assert driver.session.generation > epoch
    assert driver.session.selected is None


def test_browser_status_does_not_reuse_account_after_navigation():
    manager, driver, page = driver_at(A)
    driver.email = "test@example.com"
    assert manager.browser_status(driver.session.id)["auth_state"] == "observed"
    page.url = "https://flow.google.com/project/" + B + "?private=secret#fragment"
    driver.navigated(page.main_frame)
    result = manager.browser_status(driver.session.id)
    assert result["auth_state"] == "unknown"
    assert result["project_id"] == B
    assert result["url"] == "https://flow.google.com/project/" + B
    assert "secret" not in str(result)


async def test_same_project_keeps_document_and_no_navigation():
    _, driver, page = driver_at(A)
    previous = driver.scope()
    await driver.prepare_project(A)
    assert driver.scope() == previous
    page.goto.assert_not_called()


def test_runtime_has_no_extension_or_bypass_injection():
    source = runtime_source()
    assert "export async function" not in source
    assert "chrome.scripting" in source  # Comments explain original serialization boundary.
    assert "Object.defineProperty" not in source
    assert "flowOperation(operation, params, knownOperationErrors.source)" in source


async def test_init_script_is_registered_before_navigation():
    context = AsyncMock()
    context.add_init_script = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=context)
    context.__aexit__ = AsyncMock(return_value=False)
    page = MagicMock()
    page.url = "https://flow.google.com/"
    page.wait_for_function = AsyncMock()
    page.goto = AsyncMock()
    page.evaluate = AsyncMock(
        return_value={"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"}
    )
    context.pages = [page]
    import flow_mcp.managed as managed

    original = managed.browser_session
    managed.browser_session = lambda headless: context
    try:
        manager = Manager()
        async with managed.managed_browser(manager):
            context.add_init_script.assert_awaited_once()
            assert "__fk_hijack" in context.add_init_script.await_args.args[0]
            assert context.add_init_script.await_count == 1
            assert page.goto.await_count == 1
    finally:
        managed.browser_session = original


@pytest.mark.parametrize("operation", ["inspect", "refresh_account"])
@pytest.mark.parametrize("credits", [0, 471, None, -1, True, "471"])
async def test_account_reads_credits_by_https_without_cached_fallback(operation, credits):
    _, driver, page = driver_at(A)
    scope = driver.scope()
    evidence = {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"}
    page.evaluate.side_effect = [
        evidence,
        {"scope": scope.model_dump(), "state": "completed", "credits": credits},
        evidence,
        evidence,
    ]
    result = await driver.dispatch(operation, {"scope": scope.model_dump()})
    valid = type(credits) is int and credits >= 0
    assert result["credits"] == (credits if valid else None)
    assert result["credits_source"] == ("flow_credits_rpc" if valid else "unavailable")
    assert page.evaluate.await_args_list[1].args[1][0] == "get_credits"


async def test_credit_read_account_switch_is_rejected():
    _, driver, page = driver_at(A)
    scope = driver.scope()
    page.evaluate.side_effect = [
        {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"},
        {"scope": scope.model_dump(), "state": "completed", "credits": 471},
        {"email": "other@example.com", "observed_at": "2026-10-17T00:00:01.000Z"},
    ]
    with pytest.raises(BridgeError, match="scope_changed"):
        await driver.dispatch("inspect", {"scope": scope.model_dump()})


@pytest.mark.parametrize("operation,input_values", [
    ("create_project", {"title": "Título managed"}),
    ("generate_image", {"input": ImageInput(prompt="x", seed=42).model_dump()}),
])
async def test_managed_dispatch_forwards_optional_parameters_to_shared_runtime(
    operation, input_values,
):
    _, driver, page = driver_at(None if operation == "create_project" else A)
    scope = driver.scope()
    evidence = {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"}
    page.evaluate.side_effect = [
        evidence, {"scope": scope.model_dump(), "state": "not_submitted"}, evidence,
    ]
    result = await driver.dispatch(operation, {"scope": scope.model_dump(), **input_values})
    assert result["state"] == "not_submitted"
    runtime_call = page.evaluate.await_args_list[1]
    assert runtime_call.args[0] == driver.source
    assert runtime_call.args[1] == [operation, {
        "scope": scope.model_dump(), **input_values, "diagnostics": True,
    }]


async def test_credit_timeout_returns_unknown_not_previous_balance():
    _, driver, page = driver_at(A)
    scope = driver.scope()
    evidence = {"email": "test@example.com", "observed_at": "2026-10-17T00:00:00.000Z"}
    page.evaluate.side_effect = [evidence, TimeoutError(), evidence, evidence]
    result = await driver.dispatch("refresh_account", {"scope": scope.model_dump()})
    assert result["credits"] is None
    assert result["credits_source"] == "unavailable"
