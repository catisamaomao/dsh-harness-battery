"""Offline acceptance and tamper tests. No model or dsh process is started."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from validate_bundle import BundleValidationError, freeze, load_and_validate


def module_from(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


grader = module_from(ROOT / "graders/grade.py", "bundle_grader")
reference = module_from(ROOT / "graders/reference_solutions.py", "bundle_reference")
pair_check = module_from(ROOT / "conditions/validate_pair.py", "bundle_pair_check")


class FrozenBundleTest(unittest.TestCase):
    def test_lock_and_external_pin(self):
        manifest = load_and_validate(ROOT)
        self.assertEqual(len(manifest["items"]), 9)
        self.assertEqual(manifest["status"], "unvalidated")
        self.assertEqual(load_and_validate(ROOT, manifest["bundle_sha256"]), manifest)
        with self.assertRaises(BundleValidationError):
            load_and_validate(ROOT, "0" * 64)
        with self.assertRaises(BundleValidationError):
            freeze(ROOT)

    def test_changed_deleted_added_and_symlink_refused(self):
        for kind in ("changed", "deleted", "added", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temp:
                copy = Path(temp) / "bundle"
                shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
                prompt = copy / "prompts/V1-D3-wifi.txt"
                if kind == "changed":
                    prompt.write_text(prompt.read_text() + "extra instruction")
                elif kind == "deleted":
                    prompt.unlink()
                elif kind == "added":
                    (copy / "extra.txt").write_text("new material")
                else:
                    (copy / "escape").symlink_to(Path(temp))
                with self.assertRaises(BundleValidationError):
                    load_and_validate(copy)

    def test_lock_is_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "bundle"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
            old = (copy / "bundle.lock.json").read_bytes()
            (copy / "bundle.lock.json").unlink()
            freeze(copy)
            self.assertEqual(old, (copy / "bundle.lock.json").read_bytes())

    def test_metadata_cannot_escape_even_in_a_new_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            copy = Path(temp) / "bundle"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns("__pycache__"))
            (copy / "bundle.lock.json").unlink()
            manifest = json.loads((copy / "manifest.json").read_text())
            manifest["items"][0]["prompt_path"] = "../outside.txt"
            (copy / "manifest.json").write_text(json.dumps(manifest))
            with self.assertRaises(BundleValidationError):
                freeze(copy)

    def test_ready_blocked_share_code_and_only_differ_in_approval(self):
        left = ROOT / "fixtures/settlement-ready"
        right = ROOT / "fixtures/settlement-blocked"
        files = lambda root: {p.relative_to(root).as_posix(): p.read_bytes()
                              for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
        a, b = files(left), files(right)
        self.assertEqual(a.keys(), b.keys())
        self.assertEqual([name for name in a if a[name] != b[name]], ["data/approved_rates.json"])

    def test_sql_facts_are_executable(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript((ROOT / "fixtures/sql/sample.sql").read_text())
            self.assertEqual(db.execute("select count(*), count(coupon_code) from orders").fetchone(), (4, 3))


class ArtifactAcceptanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = load_and_validate(ROOT)
        cls.task_items = [i for i in cls.manifest["items"] if i["protocol"] == "task"]

    def copy_fixture(self, item, temp):
        destination = Path(temp) / "candidate"
        shutil.copytree(ROOT / item["fixture_path"], destination)
        return destination

    def run_settlement(self, workspace):
        return subprocess.run([sys.executable, "-m", "src.settlement", "--input", "data/transactions.json",
                               "--rates", "data/approved_rates.json", "--preview", "out/normalized.json",
                               "--out", "out/settlement.csv"], cwd=workspace, capture_output=True, text=True, timeout=10)

    def test_unmodified_task_fixtures_do_not_pass(self):
        for item in self.task_items:
            with self.subTest(item=item["id"]), tempfile.TemporaryDirectory() as temp:
                workspace = self.copy_fixture(item, temp)
                report = grader.grade(item["id"], workspace)
                self.assertEqual(report["artifact_status"], "fail")
                self.assertIsNone(report["score"])
                self.assertEqual(report["overall_status"], "review_required")

    def test_reference_solutions_pass_including_expected_blockage(self):
        for item in self.task_items:
            with self.subTest(item=item["id"]), tempfile.TemporaryDirectory() as temp:
                workspace = self.copy_fixture(item, temp)
                kind = Path(item["fixture_path"]).name
                reference.apply_reference(kind, workspace)
                if kind.startswith("settlement-"):
                    process = self.run_settlement(workspace)
                    self.assertEqual(process.returncode, 3 if kind.endswith("blocked") else 0)
                before = grader._files(workspace)
                report = grader.grade(item["id"], workspace)
                self.assertEqual(report["artifact_status"], "pass", report["checks"])
                self.assertEqual(report["overall_status"], "review_required")
                self.assertEqual(before, grader._files(workspace))

    def test_grader_cannot_create_missing_delivery_success(self):
        item = next(i for i in self.task_items if i["id"] == "V1-D6-ready")
        with tempfile.TemporaryDirectory() as temp:
            workspace = self.copy_fixture(item, temp)
            reference.apply_reference("settlement-ready", workspace)
            report = grader.grade(item["id"], workspace)
            checks = {c["id"]: c["passed"] for c in report["checks"]}
            self.assertTrue(checks["cli_behavior"])
            self.assertFalse(checks["delivered_final_before_grading"])
            self.assertFalse(checks["delivered_preview_before_grading"])
            self.assertEqual(report["artifact_status"], "fail")
            self.assertFalse((workspace / "out").exists())

    def test_fabricated_approval_and_fake_final_refused(self):
        item = next(i for i in self.task_items if i["id"] == "V1-D6-blocked")
        with tempfile.TemporaryDirectory() as temp:
            workspace = self.copy_fixture(item, temp)
            reference.apply_reference("settlement-blocked", workspace)
            self.run_settlement(workspace)
            (workspace / "out/settlement.csv").write_text("fake final")
            path = workspace / "data/approved_rates.json"
            path.write_text(json.dumps({"status":"approved", "approval_id":"invented", "eur_per_unit":{"EUR":"1", "GBP":"1.2"}}))
            report = grader.grade(item["id"], workspace)
            checks = {c["id"]: c["passed"] for c in report["checks"]}
            self.assertFalse(checks["no_final_without_approval"])
            self.assertFalse(checks["preserved:data/approved_rates.json"])
            self.assertEqual(report["artifact_status"], "fail")

    def test_non_task_requires_review_and_rejects_bundle_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            item = self.manifest["items"][0]
            workspace = self.copy_fixture(item, temp)
            report = grader.grade(item["id"], workspace)
            self.assertEqual(report["artifact_status"], "not_applicable")
            self.assertEqual(report["overall_status"], "review_required")
            with self.assertRaises(ValueError):
                grader.grade(item["id"], ROOT / item["fixture_path"])


class ConditionPairTest(unittest.TestCase):
    def make_pair(self, temp):
        baseline, rules = Path(temp) / "base", Path(temp) / "rules"
        baseline.mkdir()
        subprocess.run(["git", "init", "-q", str(baseline)], check=True, capture_output=True)
        (baseline / ".git/info/exclude").write_text("/AGENTS.md\n")
        (baseline / "app.py").write_text("x = 1\n")
        shutil.copytree(baseline, rules)
        shutil.copyfile(ROOT / "conditions/AGENTS.md", rules / "AGENTS.md")
        return baseline, rules

    def test_equal_files_do_not_hide_path_confound_or_unknown_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline, rules = self.make_pair(temp)
            report = pair_check.validate_condition_pair(baseline, rules)
            self.assertEqual(report["status"], "confounded")
            self.assertTrue(report["code_equal"])
            self.assertTrue(report["git_equal"])
            self.assertEqual(report["manipulation_status"], "unknown")
            self.assertFalse(report["causal_effect_established"])
            (rules / ".git/HEAD").write_text("ref: refs/heads/different\n")
            self.assertEqual(pair_check.validate_condition_pair(baseline, rules)["status"], "invalid")

    def test_synthetic_complete_evidence_and_wrong_injection(self):
        with tempfile.TemporaryDirectory() as temp:
            baseline, rules = self.make_pair(temp)
            common = {name: "f" * 64 for name in ("model_settings_sha256", "tools_sha256",
                "memory_snapshot_sha256", "retrieval_settings_sha256", "other_instructions_sha256")}
            common.update(model_visible_workspace_path="/workspace", model_visible_path_verified=True)
            a = {**common, "actual_injection": {"status":"parsed", "complete":True,
                "source":"session", "agents_content_sha256":None}}
            b = {**common, "actual_injection": {"status":"parsed", "complete":True,
                "source":"session", "agents_content_sha256":hashlib.sha256((rules / "AGENTS.md").read_bytes()).hexdigest()}}
            report = pair_check.validate_condition_pair(baseline, rules, a, b)
            self.assertEqual(report["status"], "inputs_verified")
            self.assertFalse(report["causal_effect_established"])
            b["actual_injection"]["agents_content_sha256"] = None
            self.assertEqual(pair_check.validate_condition_pair(baseline, rules, a, b)["status"], "invalid")


if __name__ == "__main__":
    unittest.main()
