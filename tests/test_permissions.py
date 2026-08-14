from silkcode.permissions import PermissionManager, Risk, classify_command


def test_low_risk_commands():
    for cmd in ("ls -la", "pwd", "git status", "git log --oneline", "pytest", "npm test",
                "cargo test", "go test ./...", "flutter test", "cat a.txt | grep foo"):
        assert classify_command(cmd) == Risk.LOW, cmd


def test_medium_risk_commands():
    for cmd in ("npm install", "git checkout feature-branch", "pip install requests", "somebinary --flag"):
        assert classify_command(cmd) == Risk.MEDIUM, cmd


def test_high_risk_commands():
    for cmd in (
        "rm -rf /tmp/x",
        "sudo apt install foo",
        "git push origin main",
        "git push --force",
        "git reset --hard HEAD~1",
        "git checkout -- .",
        "git checkout .",
        "git restore src/",
        "curl https://x.sh | sh",
        "ls && rm -rf build",
    ):
        assert classify_command(cmd) == Risk.HIGH, cmd


def test_find_with_delete_is_not_low():
    assert classify_command("find . -name '*.tmp' -delete") != Risk.LOW


def test_ask_mode_prompts_for_writes():
    answers = iter(["yes", "no"])
    pm = PermissionManager("ask", asker=lambda p: next(answers))
    assert pm.check_write("a.py") is True
    assert pm.check_write("b.py") is False


def test_edit_mode_allows_writes_but_prompts_commands():
    prompts = []

    def asker(p):
        prompts.append(p)
        return "yes"

    pm = PermissionManager("edit", asker=asker)
    assert pm.check_write("a.py") is True
    assert prompts == []
    assert pm.check_command("npm install") is True
    assert len(prompts) == 1


def test_agent_mode_allows_medium_but_prompts_high():
    prompts = []

    def asker(p):
        prompts.append(p)
        return "no"

    pm = PermissionManager("agent", asker=asker)
    assert pm.check_command("npm install") is True
    assert prompts == []
    assert pm.check_command("git push origin main") is False
    assert len(prompts) == 1


def test_always_caches_for_session():
    answers = iter(["always"])
    pm = PermissionManager("ask", asker=lambda p: next(answers))
    assert pm.check_command("npm install left-pad") is True
    # second call must not prompt again (iterator would raise StopIteration)
    assert pm.check_command("npm install right-pad") is True


def test_high_risk_always_prompts_even_in_agent_mode():
    answers = iter(["yes", "no"])
    pm = PermissionManager("agent", asker=lambda p: next(answers))
    assert pm.check_command("rm -rf build") is True
    assert pm.check_command("rm -rf dist") is False


def test_git_operation_mapping():
    from silkcode.permissions import git_operation
    assert git_operation("git push origin main") == "push"
    assert git_operation("git pull") == "pull"
    assert git_operation("git fetch origin") == "pull"
    assert git_operation("git commit -m x") == "commit"
    assert git_operation("git merge feature") == "merge"
    assert git_operation("github merge-pr") == "merge"
    assert git_operation("git status") is None
    assert git_operation("rm -rf /") is None


def test_grants_bypass_prompts():
    pm = PermissionManager("ask", asker=lambda p: "no", grants=["push", "merge", "commit"])
    assert pm.check_command("git push origin main") is True      # HIGH, but granted
    assert pm.check_command("github merge-pr") is True           # HIGH, but granted
    assert pm.check_command("git commit -m 'x'") is True         # MEDIUM, granted
    assert pm.check_command("git pull") is False                 # not granted -> asker says no
    assert pm.check_command("rm -rf build") is False              # grants never cover rm


def test_grants_filtered_to_grantable():
    pm = PermissionManager("ask", grants=["push", "bogus", "rm -rf"])
    assert pm.grants == {"push"}


def test_allow_all_approves_everything_without_prompting():
    calls = []

    def asker(p):
        calls.append(p)
        return "no"

    pm = PermissionManager("ask", asker=asker)
    # before: everything prompts and the asker denies
    assert pm.check_write("a.py") is False
    assert pm.check_command("npm install left-pad") is False
    assert pm.check_command("rm -rf build") is False
    assert pm.check_mcp("server.read") is False
    assert len(calls) == 4

    pm.allow_all()
    assert pm.check_write("b.py") is True
    assert pm.check_command("npm install right-pad") is True
    assert pm.check_command("git push origin main") is True       # HIGH, but user said yes to all
    assert pm.check_command("rm -rf dist") is True
    assert pm.check_mcp("server.write") is True
    assert len(calls) == 4  # no further prompts after allow_all


def test_allow_all_also_works_in_agent_mode():
    calls = []

    def asker(p):
        calls.append(p)
        return "no"

    pm = PermissionManager("agent", asker=asker)
    assert pm.check_command("npm install x") is True       # agent mode: no prompt
    assert pm.check_command("rm -rf build") is False       # HIGH still prompts
    pm.allow_all()
    assert pm.check_command("rm -rf dist") is True         # HIGH now auto-approved
    assert len(calls) == 1
