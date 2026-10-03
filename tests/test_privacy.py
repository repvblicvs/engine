import io
import tarfile
import zipfile

from repvblicvs_engine.privacy import scan_public_export


def test_clean_export_passes_and_values_are_never_in_findings(tmp_path):
    (tmp_path / "README.md").write_text("# Public product\nDeterministic delivery workflows.\n")
    assert scan_public_export(tmp_path)["allowed"]
    secret = "gh" + "p_" + "Z" * 36
    address = "private.person" + "@" + "example.com"
    (tmp_path / "private.txt").write_text(secret + "\n" + address + "\n" + "/Us" + "ers/private-person/project/file\n" + "CUSTOMER_" + "CONFIDENTIAL\n")
    report = scan_public_export(tmp_path)
    assert not report["allowed"]
    assert {item["classification"] for item in report["findings"]} >= {"credential", "email", "private_path", "customer_material"}
    assert secret not in str(report)
    assert address not in str(report)


def test_allowlisted_business_email_is_specific(tmp_path):
    address = "business" + "@" + "example.com"
    (tmp_path / "contact.md").write_text(address)
    assert scan_public_export(tmp_path, allowed_emails=[address])["allowed"]
    assert not scan_public_export(tmp_path)["allowed"]


def test_archive_contents_are_scanned_without_extracting(tmp_path):
    secret = "sk_" + "live_" + "x" * 24
    archive_path = tmp_path / "delivery.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("private.txt", secret)
        archive.writestr("../escape.txt", "should not extract")
    report = scan_public_export(archive_path)
    assert not report["allowed"]
    assert {item["classification"] for item in report["findings"]} >= {"credential", "archive_unsafe_path"}
    assert not (tmp_path.parent / "escape.txt").exists()
    assert secret not in str(report)


def test_tar_special_entries_and_symlinks_are_blocked(tmp_path):
    tar_path = tmp_path / "bundle.tar"
    with tarfile.open(tar_path, "w") as archive:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "outside"
        archive.addfile(member)
    assert scan_public_export(tar_path)["findings"][0]["classification"] == "archive_special_file"
    link = tmp_path / "alias"
    link.symlink_to(tar_path)
    assert scan_public_export(link)["findings"][0]["classification"] == "symlink_not_audited"


def test_scan_limits_and_missing_inputs_are_not_clean(tmp_path):
    (tmp_path / "large.txt").write_text("x" * 20)
    assert not scan_public_export(tmp_path, max_bytes=10)["allowed"]
    assert not scan_public_export(tmp_path / "missing")["allowed"]


def test_binary_export_requires_manual_review(tmp_path):
    (tmp_path / "image.bin").write_bytes(b"\x00\xff\x04")
    report = scan_public_export(tmp_path)
    assert report["findings"][0]["classification"] == "binary_requires_manual_review"


def test_private_filenames_are_redacted_in_every_finding(tmp_path):
    address = "private.person" + "@" + "example.com"
    path = tmp_path / (address + ".txt")
    path.write_text("CUSTOMER_" + "CONFIDENTIAL")
    report = scan_public_export(path)
    assert not report["allowed"]
    assert address not in str(report)
    assert all(item["file"] == "[redacted filename]" for item in report["findings"])
