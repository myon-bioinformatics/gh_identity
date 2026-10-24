"use strict";
/** Thin Node entry (Node 24+): runs gh_identity.py through the shared vendor/py_launch.cjs. */
const path = require("node:path");
const { main } = require("./vendor/py_launch.cjs");

const PY_SCRIPT = path.join(__dirname, "gh_identity.py");
const PY_LAUNCH = { envVar: "GH_IDENTITY_PYTHON", noSite: true };

if (require.main === module) {
  main(PY_SCRIPT, PY_LAUNCH);
}

module.exports = { PY_SCRIPT, PY_LAUNCH };
