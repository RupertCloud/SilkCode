from pathlib import Path

import install as installer


def test_installer_puts_silkcode_and_chromium_in_one_environment(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    calls = []

    def fake_create(self, target):
        target = Path(target)
        (target / "bin").mkdir(parents=True)
        (target / "pyvenv.cfg").write_text("home = test\n")
        (target / "bin" / "python").write_text("")
        command = target / "bin" / "silkcode"
        command.write_text("#!/bin/sh\n")
        command.chmod(0o755)

    monkeypatch.setattr(installer.venv.EnvBuilder, "create", fake_create)
    monkeypatch.setattr(installer, "run", lambda command: calls.append(command))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    launcher = installer.install(".", root)

    python = str(root / "bin" / "python")
    assert calls == [
        [python, "-m", "pip", "install", "--upgrade", "."],
        [python, "-m", "playwright", "install", "chromium"],
        [python, "-m", "pip", "install", "--upgrade", "graphifyy"],
    ]
    assert launcher == tmp_path / ".local" / "bin" / "silkcode"
    assert launcher.resolve() == root / "bin" / "silkcode"


def test_installer_reuses_an_existing_environment(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    (root / "bin").mkdir(parents=True)
    (root / "pyvenv.cfg").write_text("home = test\n")
    (root / "bin" / "python").write_text("")
    (root / "bin" / "silkcode").write_text("")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(installer, "run", lambda command: None)
    monkeypatch.setattr(installer.venv.EnvBuilder, "create",
                        lambda *args: (_ for _ in ()).throw(AssertionError("recreated venv")))

    installer.install("git+https://example.test/SilkCode", root)
