"""Automatic dependency PRs must be tested without publishing or deploying them."""
import json
from pathlib import Path
import re
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class UpgradePolicyTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "renovate.json").read_text())
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/build.yml").read_text())

    def test_dependency_prs_run_checks_before_merging(self):
        # PyYAML's YAML 1.1 parser reads the Actions key 'on' as boolean True.
        events = self.workflow.get("on", self.workflow.get(True))
        self.assertIn("pull_request", events)
        self.assertIn("main", events["pull_request"]["branches"])
        checks = self.workflow["jobs"]["backend-tests"]
        self.assertNotIn("if", checks, "dependency PRs must run the backend contracts")
        scripts = "\n".join(step.get("run", "") for step in checks["steps"])
        self.assertIn("pip install", scripts)
        self.assertIn("test_service_refactor.py", scripts)
        self.assertIn("test_production_allocation.py", scripts)
        self.assertIn("test_optimizer.py --offline", scripts, "native optimizer must actually solve cases")
        self.assertTrue(self.config["automerge"])
        self.assertFalse(self.config["ignoreTests"])
        self.assertFalse(self.config["platformAutomerge"], "Renovate waits for checks itself")
        self.assertFalse(self.config["dependencyDashboardApproval"])
        self.assertTrue(self.config["ignoreUnstable"])

    def test_only_pushes_can_publish_or_deploy(self):
        publisher = self.workflow["jobs"]["build"]
        self.assertEqual(publisher["if"], "github.event_name == 'push'")
        self.assertIn("backend-tests", publisher["needs"])
        for name, job in self.workflow["jobs"].items():
            publishing = any(step.get("with", {}).get("push") is True
                             or "DISCORD_WEBHOOK" in step.get("env", {})
                             for step in job["steps"])
            if publishing:
                self.assertEqual(job.get("if"), "github.event_name == 'push'", name)

    def test_runtime_and_ci_use_same_explicit_python_release(self):
        runtime = re.search(r"^FROM (python:\S+)", (ROOT / "Dockerfile").read_text(), re.M)[1]
        ci = self.workflow["jobs"]["backend-tests"]["container"]
        self.assertEqual(runtime, ci)
        self.assertRegex(runtime, r"^python:\d+\.\d+\.\d+-slim$")
        matching = [r for r in self.config["packageRules"]
                    if "python" in r.get("matchPackageNames", [])]
        self.assertTrue(matching)
        self.assertTrue(matching[-1].get("groupName"))
        self.assertNotIn("matchManagers", matching[-1], "Dockerfile and workflow images must group together")

    def test_playwright_package_and_browser_image_stay_together(self):
        package = json.loads((ROOT / "browser-tests/package.json").read_text())
        version = package["devDependencies"]["@playwright/test"]
        image = (ROOT / "browser-tests/Dockerfile").read_text().splitlines()[0]
        self.assertIn(f":v{version}-", image)
        matching = [r for r in self.config["packageRules"]
                    if {"@playwright/test", "mcr.microsoft.com/playwright"}
                    <= set(r.get("matchPackageNames", []))]
        self.assertTrue(matching)
        self.assertTrue(matching[-1].get("groupName"))


if __name__ == "__main__":
    unittest.main()
