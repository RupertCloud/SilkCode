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


def test_code_blocks_render_with_copy_and_run_buttons(browser, gui_url):
    """Fenced code inside a message render as their OWN special code bubbles —
    distinct cards in the thread, not inline boxes."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    # #input is in the static HTML, so it says nothing about the app having
    # booted. Boot ends by clearing #messages and re-rendering the transcript,
    # which would wipe anything injected before it - the file tree is loaded
    # after that clear, so tree content means the clear has already happened.
    page.wait_for_selector("#tree div", timeout=15000)

    # inject a message that mixes prose with shell and non-shell code fences
    page.evaluate("""() => {
        const raw = 'Install deps:\\n\\n```bash\\nnpm install\\nnpm test\\n```\\n\\nDone.\\n\\n```python\\nprint(1)\\n```';
        const el = addMsg('assistant', raw);
        el.__raw = raw;
        formatAllMessages();
    }""")

    # the original mixed bubble is replaced: each fence becomes its own
    # .code-bubble (full-width special card), prose stays in normal bubbles
    assert page.locator("#messages .code-bubble").count() == 2
    assert page.locator("#messages pre code").nth(0).text_content() == "npm install\nnpm test"
    # the prose 'Install deps:' / 'Done.' remain as assistant bubbles
    assert page.locator("#messages .msg.assistant", has_text="Install deps").count() == 1
    assert page.locator("#messages .msg.assistant", has_text="Done.").count() == 1

    # shell block gets a run button; the python one does not
    run_btns = page.locator("#messages .code-bubble .cb-icorun")
    assert run_btns.count() == 1
    assert run_btns.first.text_content().strip() == "▶ run"
    assert page.locator("#messages .code-bubble .cb-copy").count() == 2


def test_code_bubbles_are_not_squashed_by_the_message_column(browser, gui_url):
    """#messages is a column flex container, so a code bubble without
    flex-shrink:0 is compressed below its content height and the code is
    clipped — silently, and worse the less vertical room there is."""
    page = browser.new_page(viewport={"width": 820, "height": 640})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.wait_for_selector("#tree div", timeout=15000)

    page.evaluate("""() => {
        const raw = "Change:\\n\\n```python\\ndef fmt(n):\\n    for u in ['B','KB','MB']:\\n"
                  + "        if n < 1024: return n\\n        n /= 1024\\n```\\n";
        const el = addMsg('assistant', raw);
        el.__raw = raw;
        formatAllMessages();
    }""")

    sizes = page.evaluate("""() => Array.from(
        document.querySelectorAll('#messages .code-bubble')).map(b => ({
            bubble: b.getBoundingClientRect().height,
            head: b.querySelector('.cb-head').getBoundingClientRect().height,
            content: b.querySelector('pre').scrollHeight,
        }))""")
    assert sizes, "no code bubble was rendered"
    for s in sizes:
        assert s["bubble"] >= s["head"] + s["content"] - 2, (
            f"code bubble squashed to {s['bubble']}px for "
            f"{s['head'] + s['content']}px of content — the code is clipped")


def test_prose_between_fences_carries_no_blank_lines(browser, gui_url):
    """Bubbles use white-space: pre-wrap, so the blank lines that separate a
    fence from its prose become empty rows — a tall empty box with one line
    of text at the bottom."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.wait_for_selector("#tree div", timeout=15000)

    page.evaluate("""() => {
        const raw = "First line:\\n\\n```bash\\nls\\n```\\n\\nSecond line:\\n\\n```bash\\npwd\\n```";
        const el = addMsg('assistant', raw);
        el.__raw = raw;
        formatAllMessages();
    }""")

    texts = page.evaluate(
        """() => Array.from(document.querySelectorAll('#messages .msg.assistant'))
                     .map(m => m.textContent)""")
    assert texts, "no prose bubble survived the split"
    for text in texts:
        assert text == text.strip(), f"prose bubble kept padding: {text!r}"
        assert text.strip(), "an empty prose bubble was created"

    heights = page.evaluate(
        """() => Array.from(document.querySelectorAll('#messages .msg.assistant'))
                     .map(m => m.getBoundingClientRect().height)""")
    assert max(heights) < 60, f"a one-line prose bubble is {max(heights)}px tall"


def test_the_interface_is_navigable_without_sight_or_a_mouse(browser, gui_url):
    """Icon-only controls need names, and a streaming reply has to be
    announced — otherwise a screen-reader user gets an unlabelled button row
    and silence while the agent works."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#tree div", timeout=15000)

    # the transcript announces itself as it grows
    messages = page.locator("#messages")
    assert messages.get_attribute("aria-live") == "polite"
    assert messages.get_attribute("role") == "log"
    assert messages.get_attribute("aria-label")

    # every control whose label is an icon carries an accessible name
    unnamed = page.evaluate("""() => Array.from(document.querySelectorAll('button'))
        .filter(b => b.offsetParent !== null)
        .filter(b => {
            const text = (b.textContent || '').replace(/[^\\p{L}\\p{N}]/gu, '').trim();
            return !text && !b.getAttribute('aria-label') && !b.getAttribute('title');
        })
        .map(b => b.id || b.className)""")
    assert unnamed == [], f"controls with no accessible name: {unnamed}"

    # the composer's input is named and its hint is associated, not just nearby
    assert page.locator("#input").get_attribute("aria-label")
    described = page.locator("#input").get_attribute("aria-describedby")
    assert described and page.locator(f"#{described}").count() == 1


def test_the_activity_rail_explains_itself_when_empty(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#tree div", timeout=15000)
    assert page.is_visible("#timeline-empty")

    page.evaluate("() => addToolMsg('read_file', '{\"path\": \"app.py\"}')")
    page.evaluate("""() => {
        const d = document.createElement('div');
        d.className = 'act'; d.textContent = 'read_file';
        document.getElementById('timeline').appendChild(d);
    }""")
    assert not page.is_visible("#timeline-empty"), \
        "the empty-state text should give way to real activity"


def test_the_workspace_path_is_shortened_but_recoverable(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#tree div", timeout=15000)
    page.wait_for_function("() => document.getElementById('cwd').textContent.length > 0")

    shown = page.locator("#cwd").text_content()
    full = page.locator("#cwd").get_attribute("title")
    assert full and len(full) >= len(shown)
    assert not shown.endswith("/"), f"truncation left a dangling separator: {shown!r}"
    if shown != full:
        assert shown.startswith("…/"), shown
        assert full.endswith(shown.lstrip("…/")), "the tail shown must be the real tail"


def test_a_key_can_be_added_for_every_provider_without_scrolling_sideways(browser, gui_url):
    """The key input existed but was unreachable: long endpoint URLs pushed
    the actions column past the modal's right edge, so the field and its
    Save button were off-screen.

    Filling an element by selector does not notice that — Playwright will
    happily type into something clipped — so this asserts the input is
    actually within the modal's box before using it.
    """
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.click("#env-btn")
    page.wait_for_selector("#env-modal.open")
    page.wait_for_function(
        "() => document.querySelectorAll('#env-credentials tr').length > 1")

    modal = page.locator("#env-modal .modal").bounding_box()
    rows = page.locator("#env-credentials tr").count()
    assert rows > 5, "expected a row per provider"

    for i in range(2, rows + 1):
        row = f"#env-credentials tr:nth-child({i})"
        for control in ("input.keyin", "button.keybtn"):
            box = page.locator(f"{row} {control}").first.bounding_box()
            assert box is not None, f"{control} missing on row {i}"
            right = box["x"] + box["width"]
            assert right <= modal["x"] + modal["width"] + 1, (
                f"row {i}'s {control} is {right - (modal['x'] + modal['width']):.0f}px "
                "past the modal's edge — unreachable without scrolling sideways")

    # and it still does the job: the key is stored, shown masked, never in full
    page.fill("#env-credentials tr:nth-child(2) input.keyin", "sk-live-secret-9911")
    page.click("#env-credentials tr:nth-child(2) button.keybtn")
    page.wait_for_function(
        "() => document.getElementById('env-credentials').textContent.includes('…9911')")
    assert "sk-live-secret" not in page.text_content("#env-credentials")


def test_the_credentials_table_says_where_a_missing_key_would_come_from(browser, gui_url):
    """An unset provider printed "— · $DEEPSEEK_API_KEY" — the placeholder
    for "no source" next to the answer."""
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.click("#env-btn")
    page.wait_for_selector("#env-modal.open")
    page.wait_for_function(
        "() => document.querySelectorAll('#env-credentials tr').length > 1")

    text = page.text_content("#env-credentials")
    assert "$DEEPSEEK_API_KEY" in text, "the variable it reads should be named"
    assert "— · $" not in text, "an em-dash placeholder was printed next to a real source"


def test_the_projects_howto_opens_and_hands_off_to_the_picker(browser, gui_url):
    """The how-to is reachable from the PROJECT pane, explains creation (which
    only the terminal can do) with a copyable command, and can hand the user
    straight to the project picker."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#tree div", timeout=15000)

    page.click("#projects-help-btn")
    page.wait_for_selector("#projects-help-modal.open")
    body = page.text_content("#projects-help-modal")
    for expected in ("silkcode new", "SILKCODE.md", "python-cli", "workspace lock"):
        assert expected in body, f"the how-to never mentions {expected!r}"

    # the modal scrolls inside itself rather than growing past the window
    overflowing = page.evaluate(
        """() => { const m = document.querySelector('#projects-help-modal .modal');
                   return m.getBoundingClientRect().height > window.innerHeight; }""")
    assert not overflowing, "the how-to modal is taller than the window"

    # Escape closes it, like every other dismissible surface
    page.keyboard.press("Escape")
    page.wait_for_selector("#projects-help-modal", state="hidden")

    # and "Open a project…" swaps the how-to for the picker
    page.click("#projects-help-btn")
    page.wait_for_selector("#projects-help-modal.open")
    page.click("#projects-help-open")
    page.wait_for_selector("#project-modal.open")
    assert not page.is_visible("#projects-help-modal .modal")
