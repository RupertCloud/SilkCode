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

from silkcode.gui.server import GuiHandler, GuiState, _stamped_app_html  # noqa: E402


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
    # stamped, as run_gui serves it; the raw file's UI_VERSION would not match
    # the build /api/state reports and every page would open with the stale-
    # version banner covering the header
    Handler.html = _stamped_app_html()
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

    # create a second conversation directly inside the selected project
    first_label = page.input_value("#session-select")
    page.click("#new-session")
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
    """The header used to carry the workspace as text (`#cwd`); it is a Project
    switcher now. The guarantee is the same either way — a short label you can
    read at a glance, and the full path still recoverable on hover."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#tree div", timeout=15000)
    page.wait_for_function(
        "() => document.querySelectorAll('#project-select option').length > 0")

    shown = page.locator("#project-select option[value]:checked").text_content()
    full = page.locator("#project-select").get_attribute("title")
    assert full and full.startswith("/"), f"the full path is not recoverable: {full!r}"
    assert len(shown) <= len(full) + 20, f"the label is not a label: {shown!r}"
    assert not shown.endswith("/"), f"truncation left a dangling separator: {shown!r}"
    assert full.rstrip("/").endswith(shown.split("  (")[0]), \
        f"the label {shown!r} is not the tail of {full!r}"


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


def test_push_is_not_weighted_like_a_settings_button(browser, gui_url):
    """Push leaves this machine and cannot be taken back. It sat at the same
    weight as Environment purely because both are buttons."""
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    page.goto(gui_url)
    page.wait_for_selector("#input")

    push = page.locator("#push-btn")
    assert "outward" in (push.get_attribute("class") or ""), \
        "Push carries no marking distinguishing it from a settings control"
    assert "leaves your machine" in (push.get_attribute("title") or "")

    # it renders differently, not just semantically
    styles = page.evaluate("""() => {
        const of = id => {
            const s = getComputedStyle(document.getElementById(id));
            return {border: s.borderTopColor, weight: s.fontWeight};
        };
        return {push: of('push-btn'), env: of('env-btn')};
    }""")
    assert styles["push"]["border"] != styles["env"]["border"], \
        "Push looks identical to a settings button"

    # and the header is grouped rather than one undifferentiated row
    assert page.locator("header .hgroup").count() >= 2
    groups = page.evaluate(
        """() => Array.from(document.querySelectorAll('header .hgroup'))
                     .map(g => g.getAttribute('aria-label'))""")
    assert all(groups), "each header group should be named for screen readers"


def test_the_diff_panel_does_not_print_raw_porcelain(browser, gui_url):
    """`git status --short --branch` leads with "## main...origin/main".
    Printed above "(no changes)" it reads as though something is there."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.click("#tabs button[data-tab='diff']")
    page.wait_for_function(
        "() => document.getElementById('viewer-pre').textContent.trim().length > 0")

    text = page.text_content("#viewer-pre")
    assert "## " not in text, f"porcelain branch line shown to the user: {text!r}"
    # the fixture workspace is not a repository, so what should appear is
    # git's own complaint — not "1 changed file" whose name is the error
    assert "not a git repository" in text, text
    assert "changed file" not in text, "a git failure was rendered as a file list"


def test_the_diff_panel_lists_what_changed(browser, gui_url, tmp_path):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")

    # the fixture workspace is not a repo; point the panel at a real one
    page.route("**/api/diff*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "status": "## main...origin/main [ahead 1]\n M src/app.py\n?? extra.txt",
            "diff": "diff --git a/src/app.py b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n",
        })))
    page.click("#tabs button[data-tab='diff']")
    page.wait_for_function(
        "() => document.getElementById('viewer-pre').textContent.includes('changed file')")

    text = page.text_content("#viewer-pre")
    assert "2 changed files on main" in text
    assert "ahead 1" in text, "tracking state is worth surfacing"
    assert "src/app.py" in text and "extra.txt" in text
    assert "## " not in text


def test_the_diff_panel_says_plainly_when_there_is_nothing(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    page.wait_for_selector("#input")
    page.route("**/api/diff*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"status": "## main...origin/main", "diff": "(no changes)"})))
    page.click("#tabs button[data-tab='diff']")
    page.wait_for_function(
        "() => document.getElementById('viewer-pre').textContent.includes('No uncommitted')")

    text = page.text_content("#viewer-pre")
    assert "No uncommitted changes on main." in text
    assert "## " not in text


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


# ---- the switcher shows this project, not every project ---------------------

@pytest.fixture
def two_project_gui(tmp_path, stub_server, monkeypatch):
    """A daemon opened on `alpha`, with saved sessions in `alpha` and `beta`.

    Session files live per machine, not per project, so before this was scoped
    the switcher listed `beta`'s work while you were looking at `alpha`.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("SILKCODE_HOME", str(home))
    alpha, beta = tmp_path / "alpha", tmp_path / "beta"
    for d in (alpha, beta):
        d.mkdir()
        (d / "README.md").write_text("# demo\n")

    server = stub_server([])
    server.thread.start()
    (home / "config.json").write_text(json.dumps({
        "default_model": "stub",
        "providers": {"stub": {"type": "openai_compat", "base_url": server.base_url,
                               "default_model": "stub-model"}},
    }))

    from silkcode.sessions import SessionStore, new_session
    store = SessionStore()
    for project, title in ((alpha, "alpha work"), (beta, "beta work"),
                           (beta, "more beta work")):
        store.save(new_session(store.new_id(), title=title, model="stub/stub-model",
                               cwd=str(project), mode="edit", instance="127.0.0.1:1"))

    state = GuiState(str(alpha), None, "edit")

    class Handler(GuiHandler):
        pass

    Handler.state = state
    Handler.html = _stamped_app_html()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", alpha, beta
    httpd.shutdown()
    httpd.server_close()
    server.httpd.shutdown()
    server.httpd.server_close()


def wait_for_switcher(page):
    """#input is in the static HTML, so waiting on it proves nothing about
    init() having run. Wait for the switcher to actually hold sessions."""
    page.wait_for_function(
        "document.querySelectorAll('#session-select option').length > 0")


def test_the_switcher_lists_only_the_open_projects_sessions(browser, two_project_gui):
    url, _alpha, _beta = two_project_gui
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url)
    wait_for_switcher(page)

    options = page.locator("#session-select option").all_text_contents()
    assert any("alpha work" in o for o in options), f"the open project is missing: {options}"
    assert not any("beta work" in o for o in options), \
        f"another project's sessions are in the switcher: {options}"


def test_other_projects_are_reached_through_project_cards_not_conversations(browser, two_project_gui):
    url, _alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(url)
    wait_for_switcher(page)

    assert page.locator("#session-select option[value='__all__']").count() == 0
    page.locator(f".project-card[data-path='{beta}']").click()
    page.wait_for_function("() => [...document.querySelectorAll('#session-select option')].some(o => o.textContent.includes('beta work'))")
    options = page.locator("#session-select option").all_text_contents()
    assert any("beta work" in text for text in options)
    assert not any("alpha work" in text for text in options)


def test_a_daemon_with_one_project_shows_no_reveal(browser, gui_url):
    """The entry only appears when there is something behind it — an empty
    "other projects" row would be noise in every single-project install."""
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    wait_for_switcher(page)
    assert page.locator("#session-select option[value='__all__']").count() == 0


def test_mobile_layout_has_compact_header_and_switchable_panes(browser, gui_url):
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto(gui_url)
    wait_for_switcher(page)

    assert page.locator("#mobile-menu").is_visible()
    assert page.locator("#chat").is_visible()
    assert not page.locator("#files").is_visible()
    assert page.locator("body").evaluate("el => el.scrollWidth <= el.clientWidth")

    page.click("#mobile-menu")
    assert page.locator(".mobile-actions").is_visible()
    assert page.locator("#session-select").is_visible()
    page.keyboard.press("Escape")
    assert not page.locator(".mobile-actions").is_visible()

    page.click("#mobile-tabs button[data-pane='files']")
    assert page.locator("#files").is_visible()
    assert not page.locator("#chat").is_visible()


def test_secondary_panels_live_in_a_collapsed_details_drawer(browser, gui_url):
    page = browser.new_page(viewport={"width": 1280, "height": 800})
    page.goto(gui_url)
    wait_for_switcher(page)

    assert not page.locator("#details").is_visible()
    assert page.locator("#chat").is_visible()
    page.click("#details-toggle")
    assert page.locator("#details").is_visible()
    assert page.locator("#bottom").is_visible()

    page.click("#details-head button[data-detail='files']")
    assert page.locator("#file-details").is_visible()
    assert not page.locator("#activity").is_visible()
    page.click("#details-head button[data-detail='activity']")
    assert page.locator("#activity").is_visible()
    assert not page.locator("#bottom").is_visible()
    page.click("#details-close")
    assert not page.locator("#details").is_visible()


# ---- project is a control, not a caption ------------------------------------

def test_the_project_is_a_switcher_beside_session_model_and_mode(browser, two_project_gui):
    """Session, Model and Mode were all dropdowns; Project — the thing that
    scopes every file tool, git command and test run — was static text."""
    url, alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-select option').length > 0")

    options = page.locator("#project-select option").all_text_contents()
    assert any(alpha.name in o for o in options), options
    assert any(beta.name in o for o in options), \
        "a project with sessions was not offered in the switcher"
    assert any("Open another" in o for o in options)

    cards = page.locator("#project-cards .project-card")
    assert cards.count() >= 2
    assert any(alpha.name in text for text in cards.all_text_contents())
    assert any(beta.name in text for text in cards.all_text_contents())
    assert page.locator("#project-cards .project-card.current").count() == 1


def test_project_card_switches_repository_in_one_click(browser, two_project_gui):
    url, _alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-cards .project-card').length >= 2")

    page.locator(f".project-card[data-path='{beta}']").click()
    page.wait_for_function(
        "t => document.querySelector('#project-select').title === t", arg=str(beta))
    assert page.locator("#project-cards .project-card.current").get_attribute("data-path") == str(beta)


def test_project_card_close_frees_non_current_project(browser, two_project_gui):
    url, _alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-cards .project-card').length >= 2")

    page.on("dialog", lambda dialog: dialog.accept())
    card = page.locator(f".project-card[data-path='{beta}']")
    assert card.locator(".project-close").is_enabled()
    card.locator(".project-close").click()
    page.wait_for_function(
        "p => !document.querySelector(`.project-card[data-path='${p}']`)", arg=str(beta))
    assert page.locator(f".project-card[data-path='{beta}']").count() == 0
    assert page.locator(".project-card.current .project-close").is_disabled()


def test_switching_project_moves_the_session_and_rescopes_its_list(browser, two_project_gui):
    url, alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-select option').length > 0")

    page.select_option("#project-select", str(beta))
    # Two waits, because init() renders these at different times: the header
    # first, then the session list after an await. Waiting on a bare option
    # count matched the pre-move render, which is how this test first "passed".
    page.wait_for_function(
        "t => document.querySelector('#project-select').title === t", arg=str(beta))
    page.wait_for_function(
        "[...document.querySelectorAll('#session-select option')]"
        ".some(o => o.textContent.includes('beta work'))")

    assert page.locator("#project-select").get_attribute("title") == str(beta)
    sessions = page.locator("#session-select option").all_text_contents()
    assert any("beta work" in s for s in sessions), sessions
    assert not any("__all__" in (s or "") for s in sessions)


def test_the_picker_opens_where_a_local_user_can_act(browser, two_project_gui):
    """With no GitHub connection the picker used to open onto "No repositories
    yet. Connect GitHub, then refresh." — an error about a service the user may
    not use, in place of the thing they came for."""
    url, alpha, beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-select option').length > 0")

    page.click("#new-session")
    page.wait_for_selector("#project-modal.open")
    page.wait_for_function(
        "document.querySelectorAll('#project-recent-list button').length > 0")

    assert page.locator("#project-tabs .ptab.active").text_content() == "Local directory"
    assert page.locator("#project-recent-wrap").is_visible(), \
        "recent projects are hidden again"
    recents = page.locator("#project-recent-list button").all_text_contents()
    assert any(alpha.name in r for r in recents), recents


def test_github_project_tab_can_be_selected_and_connects(browser, two_project_gui):
    url, _alpha, _beta = two_project_gui
    page = browser.new_page(viewport={"width": 1200, "height": 800})
    page.goto(url)
    wait_for_switcher(page)
    page.click("#project-add")
    page.wait_for_selector("#project-modal.open")

    page.click("#project-tabs button[data-pgtab='github']")
    assert page.locator("#project-tabs button[data-pgtab='github']").get_attribute("class") == "ptab active"
    assert page.locator("[data-pgpane='github']").is_visible()
    assert not page.locator("[data-pgpane='local']").is_visible()

    page.click("#project-github-connect")
    assert page.locator("#github-modal").evaluate("el => el.classList.contains('open')")


def test_github_repository_selection_is_visible_and_fetches(browser, two_project_gui):
    url, _alpha, _beta = two_project_gui
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.route("**/api/projects", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps([{
            "kind": "github", "spec": "github:acme/widget",
            "label": "github/acme/widget", "github_owner_repo": "acme/widget",
            "local_path": "/managed/projects/acme-widget", "downloaded": False,
        }])))
    opened = []
    page.route("**/api/project/open", lambda route: (
        opened.append(route.request.post_data_json()["project"]),
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"session_id": 1}))))
    page.goto(url)
    wait_for_switcher(page)
    page.click("#project-add")
    page.wait_for_selector("#project-github-list .project-row")

    row = page.locator("#project-github-list .project-row")
    row.click()
    assert "selected" in row.get_attribute("class")
    assert row.get_attribute("aria-pressed") == "true"
    assert page.locator("#project-confirm").text_content() == "Fetch & open"
    assert page.locator("#project-confirm").get_attribute("aria-live") == "polite"
    assert "clone locally" in row.text_content()
    assert "/managed/projects/acme-widget" in row.get_attribute("title")

    page.click("#project-confirm")
    page.wait_for_function("() => !document.getElementById('project-modal').classList.contains('open')")
    assert opened == ["github:acme/widget"]


def test_new_conversation_and_open_project_are_separate_actions(browser, two_project_gui):
    url, _alpha, _beta = two_project_gui
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(url)
    page.wait_for_function("document.querySelectorAll('#project-select option').length > 0")

    old = page.locator("#session-select").input_value()
    page.click("#new-session")
    page.wait_for_function("old => document.querySelector('#session-select').value !== old",
                           arg=old)
    assert not page.locator("#project-modal").evaluate("el => el.classList.contains('open')")

    page.select_option("#project-select", "__open__")
    page.wait_for_selector("#project-modal.open")
    assert page.locator("#project-modal h3").text_content() == "Open project"

    # cancelling must put the switcher back, not leave it claiming a move
    page.click("#project-cancel")
    assert page.locator("#project-select").input_value() != "__all__"
    assert page.locator("#project-select").input_value() != "__open__"
