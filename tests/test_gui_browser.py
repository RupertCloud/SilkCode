"""Real-browser GUI tests (Playwright + Chromium). Skipped when Playwright
or a Chromium executable is unavailable (e.g. plain CI runners)."""

import json
import os
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")

from conftest import sse_response  # noqa: E402

from silkcode.gui.server import GuiHandler, GuiState  # noqa: E402


def _chromium_path():
    for candidate in (os.environ.get("CHROMIUM_PATH"), "/opt/pw-browsers/chromium"):
        if candidate and Path(candidate).exists():
            return candidate
    return None


@pytest.fixture
def browser():
    with playwright_sync.sync_playwright() as p:
        launch_kwargs = {}
        path = _chromium_path()
        if path:
            launch_kwargs["executable_path"] = path
        try:
            browser = p.chromium.launch(**launch_kwargs)
        except Exception as exc:  # no usable chromium on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture
def gui_url(tmp_path, stub_server, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("# demo\n")
    monkeypatch.setenv("SILKCODE_HOME", str(home))

    def turn(reply, filename):
        return [
            sse_response(tool_calls=[("write_file", json.dumps({"path": filename, "content": "x"}))],
                         usage={"prompt_tokens": 10, "completion_tokens": 5}),
            sse_response(content=reply, usage={"prompt_tokens": 20, "completion_tokens": 5}),
        ]

    # enough scripted turns for both sessions
    server = stub_server(turn("Reply in session one.", "one.txt")
                         + turn("Reply in session two.", "two.txt")
                         + turn("Second reply in session one.", "one-b.txt"))
    server.thread.start()
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                                "default_model": "stub-model"}},
    }))

    state = GuiState(str(workspace), None, "edit")

    class Handler(GuiHandler):
        pass

    Handler.state = state
    Handler.html = (Path(__file__).resolve().parents[1] / "silkcode" / "gui" / "app.html").read_bytes()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    server.httpd.shutdown()
    server.httpd.server_close()


def send_and_wait(page, text, expected_reply):
    page.fill("#input", text)
    page.click("#send")
    page.wait_for_selector(f".msg.assistant:has-text('{expected_reply}')", timeout=15000)


def test_environment_page_renders(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")

    # run a turn so there is usage to show
    send_and_wait(page, "first request", "Reply in session one.")

    # each message block carries a copy button that copies its text
    copy_btns = page.locator("#messages .msg-wrap .msg-copy")
    assert copy_btns.count() >= 2  # the user prompt and the assistant reply
    first = copy_btns.first
    assert first.get_attribute("title") == "Copy message"

    page.click("#env-btn")
    page.wait_for_selector("#env-modal.open")
    page.wait_for_function(
        "() => document.querySelectorAll('#env-credentials tr').length > 1")

    creds = page.text_content("#env-credentials")
    assert "deepseek" in creds and "stub" in creds
    assert "sk-" not in creds  # no secret material on the page

    assert "tokens across" in page.text_content("#env-usage-totals")
    assert "session(s) open in this daemon" in page.text_content("#env-live-summary")
    assert "stub-model" in page.text_content("#env-live")

    # storing a key from the page shows it masked, never in full
    page.fill("#env-credentials tr:nth-child(2) input.keyin", "sk-live-test-4242")
    page.click("#env-credentials tr:nth-child(2) button.keybtn")
    page.wait_for_function(
        "() => document.getElementById('env-credentials').textContent.includes('…4242')")
    assert "sk-live-test" not in page.text_content("#env-credentials")

    page.click("#env-close")
    assert not page.is_visible("#env-modal.open")


def test_composer_visible_on_small_windows(browser, gui_url):
    # a small laptop window: the header wraps and vertical space is tight
    page = browser.new_page(viewport={"width": 900, "height": 560})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    for i in range(40):
        page.evaluate("""(i) => {
            const d = document.createElement('div');
            d.className = 'msg assistant';
            d.textContent = 'filler ' + i;
            document.getElementById('messages').appendChild(d);
        }""", i)
    box = page.locator("#composer").bounding_box()
    assert box is not None, "composer missing"
    assert box["height"] >= 50, f"composer squeezed to {box['height']}px"
    assert box["y"] + box["height"] <= 560 + 1, f"composer below the viewport: {box}"
    assert page.is_visible("#send")


def test_composer_always_visible_and_sessions_switch(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    page.goto(gui_url)
    page.wait_for_selector("#input")

    # chat bar visible on load
    assert page.locator("#composer").bounding_box() is not None
    assert page.is_visible("#input")

    # session 1: run a turn, then flood the transcript; composer must stay visible
    send_and_wait(page, "first request", "Reply in session one.")
    page.evaluate("""() => {
        for (let i = 0; i < 60; i++) {
            const d = document.createElement('div');
            d.className = 'msg assistant';
            d.textContent = 'filler message ' + i;
            document.getElementById('messages').appendChild(d);
        }
    }""")
    box = page.locator("#composer").bounding_box()
    assert box is not None and box["y"] + box["height"] <= 720 + 1, \
        f"composer pushed out of the viewport: {box}"
    assert page.is_visible("#send")

    # create a second session: the + button now asks for a project first;
    # confirming with nothing selected reuses the current project
    first_label = page.input_value("#session-select")
    page.click("#new-session")
    page.wait_for_selector("#project-modal.open")
    page.click("#project-confirm")
    page.wait_for_function(
        "sel => document.querySelector('#session-select').value !== sel", arg=first_label)
    # the conversation is empty; a workspace-lock notice is expected because
    # this second session opens the project the first one already holds
    page.wait_for_function(
        "() => !document.getElementById('messages').textContent.includes('Reply in session one.')")
    assert page.locator("#messages .msg.user, #messages .msg.assistant").count() == 0
    notice = page.locator("#messages .msg.notice")
    if notice.count():
        text = notice.first.text_content()
        assert "already open in session-" in text, text  # a readable sentence

    send_and_wait(page, "second session request", "Reply in session two.")
    assert page.locator(".msg.assistant", has_text="Reply in session one.").count() == 0

    # switch BACK to session 1: its transcript must render
    page.select_option("#session-select", value=first_label)
    page.wait_for_selector(".msg.assistant:has-text('Reply in session one.')", timeout=10000)
    assert page.locator(".msg.assistant", has_text="Reply in session two.").count() == 0

    # and FORTH to session 2 again
    two_value = page.locator("#session-select option").all()[0].get_attribute("value")
    values = [o.get_attribute("value") for o in page.locator("#session-select option").all()]
    other = [v for v in values if v != first_label][0]
    page.select_option("#session-select", value=other)
    page.wait_for_selector(".msg.assistant:has-text('Reply in session two.')", timeout=10000)
    assert page.locator(".msg.assistant", has_text="Reply in session one.").count() == 0

    # back to 1 once more and continue the conversation there
    page.select_option("#session-select", value=first_label)
    page.wait_for_selector(".msg.assistant:has-text('Reply in session one.')", timeout=10000)
    send_and_wait(page, "again in one", "Second reply in session one.")
