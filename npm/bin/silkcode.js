#!/usr/bin/env node
/* The npx door into Silk Code.
 *
 * Silk Code is a Python application; this launcher exists so `npx silkcode`
 * works on a machine that has Node but has never heard of it. On first run
 * it finds a Python 3.10+, hands over to the bundled install.py - the same
 * installer the README's curl line downloads, which sets up Silk Code and
 * its Chromium in an isolated environment - and then execs the real CLI.
 * Every later run skips straight to the exec: this file stays a doorway,
 * never a second implementation of anything.
 *
 * No npm dependencies, deliberately: a bootstrap that installs an agent
 * with shell access should have nothing in its own supply chain to audit
 * beyond this one file.
 */
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

// Mirrors runtime_dir()/paths() in install.py - the one layout, two places.
function runtimeDir() {
  if (process.env.SILKCODE_INSTALL_DIR) {
    return path.resolve(process.env.SILKCODE_INSTALL_DIR);
  }
  if (process.platform === "win32") {
    const base = process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(base, "SilkCode", "runtime");
  }
  return path.join(os.homedir(), ".silkcode", "runtime");
}

function runtimeCommand() {
  return process.platform === "win32"
    ? path.join(runtimeDir(), "Scripts", "silkcode.exe")
    : path.join(runtimeDir(), "bin", "silkcode");
}

function findPython() {
  if (process.env.SILKCODE_PYTHON) return [process.env.SILKCODE_PYTHON];
  const candidates = process.platform === "win32"
    ? [["py", "-3"], ["python"], ["python3"]]
    : [["python3"], ["python"]];
  const probe = "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)";
  for (const candidate of candidates) {
    const r = spawnSync(candidate[0], [...candidate.slice(1), "-c", probe],
                        { stdio: "ignore" });
    if (r.status === 0) return candidate;
  }
  return null;
}

function installerPath() {
  // install.py is copied in beside package.json when the package is built
  // (see prepack); running from a repository checkout finds the original.
  const bundled = path.join(__dirname, "..", "install.py");
  if (fs.existsSync(bundled)) return bundled;
  return path.join(__dirname, "..", "..", "install.py");
}

function bootstrap() {
  const python = findPython();
  if (python === null) {
    console.error(
      "Silk Code needs Python 3.10 or newer, and none was found on PATH.\n" +
      "Install Python from https://python.org (or your package manager),\n" +
      "then run this again. To point at a specific interpreter, set\n" +
      "SILKCODE_PYTHON=/path/to/python.");
    process.exit(1);
  }
  console.error(`First run: installing Silk Code and its Chromium into ${runtimeDir()}`);
  console.error("(a few minutes; later runs start instantly)\n");
  const r = spawnSync(python[0], [...python.slice(1), installerPath()],
                      { stdio: "inherit" });
  if (r.status !== 0 || !fs.existsSync(runtimeCommand())) {
    console.error("\nSilk Code's installer did not finish; the messages above say why.");
    process.exit(r.status || 1);
  }
}

if (!fs.existsSync(runtimeCommand())) bootstrap();
const run = spawnSync(runtimeCommand(), process.argv.slice(2), { stdio: "inherit" });
process.exit(run.status === null ? 1 : run.status);
