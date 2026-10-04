import subprocess
import zipfile

from repvblicvs_engine.privacy import scan_public_export
from repvblicvs_engine.publication import check_git


def repository(path):
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path


def git(path, *arguments):
    return subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True, text=True).stdout.strip()


def test_worktree_cleanup_cannot_mask_staged_private_content(tmp_path):
    repo = repository(tmp_path)
    p = repo / "README.md"
    p.write_text("# Session-" + "retrospective\nInternal report\n")
    git(repo, "add", "README.md")
    p.write_text("# Product\nUseful data tools\n")
    assert check_git(repo)["allowed"]
    assert not check_git(repo, staged=True)["allowed"]


def test_force_added_private_runtime_is_blocked_even_without_secret_patterns(tmp_path):
    repo = repository(tmp_path)
    p = repo / ".private" / "runtime.json"
    p.parent.mkdir()
    p.write_text('{"ordinary": "text"}')
    git(repo, "add", str(p.relative_to(repo)))
    assert not check_git(repo, staged=True)["allowed"]


def test_clean_index_cannot_hide_private_committed_tree(tmp_path):
    repo = repository(tmp_path)
    p = repo / "README.md"
    p.write_text("# Executive-" + "handoff\nInternal\n")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.name=Synthetic Test", "-c", "user.email=test" + "@" + "example.invalid", "commit", "-qm", "Synthetic fixture")
    head = git(repo, "rev-parse", "HEAD")
    p.write_text("# Public product\n")
    git(repo, "add", "README.md")
    assert check_git(repo, staged=True)["allowed"]
    assert not check_git(repo, revision=head)["allowed"]


def test_packaged_internal_documentation_is_blocked(tmp_path):
    p = tmp_path / "fixture.whl"
    with zipfile.ZipFile(p, "w") as archive:
        archive.writestr("fixture.dist-info/METADATA", "The own" + "er also authorized existing prepaid credits.")
    report = scan_public_export(p)
    assert not report["allowed"]
    assert any(x["classification"] == "internal_operating_details" for x in report["findings"])


def test_report_filename_blocks_even_redacted_staged_content(tmp_path):
    repo = repository(tmp_path)
    p = repo / ("repvblicvs-acquisition-" + "update.md")
    p.write_text("A redacted operating summary\n")
    git(repo, "add", p.name)
    assert not check_git(repo, staged=True)["allowed"]


def test_renaming_report_does_not_make_its_heading_public(tmp_path):
    repo = repository(tmp_path)
    p = repo / "notes.md"
    p.write_text("# Repvblicvs acquisition " + "status — private\nRedacted\n")
    git(repo, "add", p.name)
    assert not check_git(repo, staged=True)["allowed"]


def test_archive_cannot_hide_redacted_report_filename(tmp_path):
    p = tmp_path / "fixture.whl"
    with zipfile.ZipFile(p, "w") as archive:
        archive.writestr("docs/FINISHING_" + "REPORT.md", "Redacted\n")
    report = scan_public_export(p)
    assert not report["allowed"]
    assert any(x["classification"] == "private_operating_report_filename" for x in report["findings"])


def test_deterministic_worker_leaves_connector_obligation_for_frontend(tmp_path):
    from repvblicvs_engine.daemon import Worker
    from repvblicvs_engine.scheduler import claim_operator
    from repvblicvs_engine.store import Store
    store = Store(tmp_path)
    obligation = store.enqueue({"kind": "operator_review", "payload": {"purpose": "Synthetic submission"}, "priority": 100})
    task = store.enqueue({"kind": "csv_cleanup", "payload": {"csv_text": "a\n1\n"}})
    assert Worker(store).step()["id"] == task["id"]
    waiting = store.get_task(obligation["id"])
    assert waiting["status"] == "queued" and waiting["attempts"] == 0
    assert claim_operator(store, "synthetic-lead", "synthetic-receipt", obligation["id"])["task"]["id"] == obligation["id"]
