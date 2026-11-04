#!/usr/bin/env python3
"""Compile the actual patched update parser/verifier in a small native test crate."""
import argparse
import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import channel
import prepare_client

ROOT = Path(__file__).resolve().parent


def verify(source=None, update_lock=False):
    with tempfile.TemporaryDirectory(prefix="geodomdesk-rust-check-") as tmp:
        work = Path(tmp)
        if source is None:
            source = work / "client"
            for name in json.loads((ROOT / "source-lock.json").read_text()):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((ROOT / "reference" / name).read_bytes())
            (source / "res").mkdir(exist_ok=True)
            (source / "flutter/windows/runner/resources").mkdir(parents=True)
            with contextlib.redirect_stdout(io.StringIO()):
                prepare_client.prepare(source, "1.5.1")
        crate = work / "crate"
        (crate / "src/update_metadata").mkdir(parents=True)
        (crate / "fixtures").mkdir()
        for name in [
            "src/update_metadata.rs",
            "src/geodomdesk_update.rs",
            "src/geodomdesk_auth.rs",
        ]:
            (crate / name).write_bytes((source / name).read_bytes())
        (crate / "src/geodomdesk_import_data.rs").write_bytes(
            (source / "src/geodomdesk_import/data.rs").read_bytes()
        )
        # artifact policy is unchanged by preparation; use the full source if present.
        policy = source / "src/update_metadata/artifact.rs"
        if not policy.exists():
            policy = ROOT / "reference/src/update_metadata/artifact.rs"
        (crate / "src/update_metadata/artifact.rs").write_bytes(policy.read_bytes())
        common = (source / "src/common.rs").read_text(encoding="utf-8")
        begin = common.index("pub(crate) fn display_version_from_release_id(")
        end = common.index("\nfn clear_software_update_url()", begin)
        parser = common[begin:end]
        client = (source / "src/client.rs").read_text(encoding="utf-8")
        begin = client.index("pub fn check_if_retry(")
        end = client.index("\n#[cfg(test)]", begin)
        retry = client[begin:end]
        (crate / "Cargo.toml").write_text(
            """[package]
name = "geodomdesk-update-verification"
version = "0.0.0"
edition = "2021"
rust-version = "1.85"

[dependencies]
anyhow = "1"
base64 = "0.22"
serde = "1"
serde_derive = "1"
serde_json = "1"
sodiumoxide = "0.2"
url = "=2.5.4"
toml = "0.5"
"""
        )
        (crate / "src/lib.rs").write_text(
            """#![allow(dead_code)]
extern crate self as hbb_common;
pub use anyhow;
pub use anyhow::bail;
pub use base64;
pub use sodiumoxide;
pub use toml;
pub type ResultType<T> = anyhow::Result<T>;
fn get_version_number(s: &str) -> i64 {
    s.split('.').filter_map(|n| n.parse::<i64>().ok()).fold(0, |v, n| v * 100000 + n)
}
mod common {
    use crate::{bail, get_version_number, ResultType};
"""
            + parser
            + """
}
mod geodomdesk_update;
mod geodomdesk_auth;
mod geodomdesk_import_data;
mod update_metadata;
fn use_ws() -> bool { true }
"""
            + retry
            + """
#[cfg(test)]
mod account_retry_tests {
    use super::*;
    #[test]
    fn account_errors_never_retry_even_on_relay() {
        for message in ["Please log in before starting a remote connection", "Invalid account or password"] {
            for relay in [false, true] {
                assert!(!check_if_retry("error", "Connection Error", message, relay));
            }
        }
    }
    #[test]
    fn preserves_transport_retry_and_offline_handling() {
        assert!(check_if_retry("error", "Connection Error", "Connection reset 10054", true));
        assert!(!check_if_retry("error", "Connection Error", "Remote desktop is offline", false));
        assert!(!check_if_retry("error", "Connection Error", "Incoming only mode", false));
    }
}
"""
        )
        release_id = "1.5.2-r102"
        artifacts = []
        for platform, arch, fmt in channel.TARGETS:
            name = channel.file_name(release_id, platform, fmt)
            artifacts.append(
                {
                    "platform": platform,
                    "arch": arch,
                    "format": fmt,
                    "file_name": name,
                    "url": channel.release_url(release_id) + name,
                    "size": 1234,
                    "sha256": "a" * 64,
                }
            )
        seed = bytes(range(32))
        metadata, signature = channel.signed_metadata(
            release_id, "2026-11-04T00:00:00Z", artifacts, seed
        )
        fixtures = {"valid": json.loads(metadata)}
        for name, field, value in [
            ("wrong-app", "app", "rustdesk"),
            ("wrong-package", "package_id", "rustdesk"),
            ("wrong-version", "version", "1.5.3"),
        ]:
            fixtures[name] = {**fixtures["valid"], field: value}
        for name, values in fixtures.items():
            data = channel.stable_json(values)
            signed = {
                **json.loads(signature),
                "signature": base64.b64encode(
                    Ed25519PrivateKey.from_private_bytes(seed).sign(
                        channel.SIGNATURE_CONTEXT + data
                    )
                ).decode(),
            }
            (crate / "fixtures" / f"{name}.json").write_bytes(data)
            (crate / "fixtures" / f"{name}.sig").write_bytes(channel.stable_json(signed))
        verifier_test = """
#[cfg(test)]
mod geodomdesk_contract_tests {
    use super::*;
    fn verify(name: &str, platform: &str, format: &str, key: [u8;32]) -> ResultType<VerifiedUpdateArtifact> {
        let metadata = std::fs::read(format!("fixtures/{name}.json"))?;
        let signature = std::fs::read(format!("fixtures/{name}.sig"))?;
        let keys = [TrustedUpdateKey { key_id: "geodomdesk-2026-01", algorithm: "ed25519", public_key: key }];
        verify_update_metadata_with_options(&metadata, &signature, VerificationOptions {
            requirements: MetadataRequirements { expected_version: "1.5.2", expected_release_id: "1.5.2-r102",
                artifact: UpdateArtifactQuery { platform, arch: "x86_64", format } },
            expected_package_id: PUBLIC_UPDATE_PACKAGE_ID,
            artifact_url_policy: ArtifactUrlPolicy::ExactPrefix("https://www.qcjcpt.com/downloads/geodomdesk/releases/1.5.2-r102/"),
            trusted_keys: &keys,
        })
    }
    const TEST_KEY: [u8;32] = KEY_BYTES;
    #[test] fn verifies_python_windows_metadata() { assert_eq!(verify("valid","windows","exe",TEST_KEY).unwrap().size, 1234); }
    #[test] fn verifies_python_ubuntu_metadata() { assert!(verify("valid","linux","deb",TEST_KEY).is_ok()); }
    #[test] fn rejects_rustdesk_app() { assert!(verify("wrong-app","windows","exe",TEST_KEY).is_err()); }
    #[test] fn rejects_rustdesk_package() { assert!(verify("wrong-package","windows","exe",TEST_KEY).is_err()); }
    #[test] fn rejects_wrong_version() { assert!(verify("wrong-version","windows","exe",TEST_KEY).is_err()); }
    #[test] fn rejects_wrong_public_key() { assert!(verify("valid","windows","exe",[0;32]).is_err()); }
    #[test] fn rejects_wrong_platform_format() { assert!(verify("valid","windows","deb",TEST_KEY).is_err()); }
}
""".replace(
            "KEY_BYTES", str(list(channel.public_key(seed)))
        )
        with (crate / "src/update_metadata.rs").open("a") as stream:
            stream.write(verifier_test)
        cargo = shutil.which("cargo")
        if not cargo:
            candidate = Path("/home/xp/.cargo/bin/cargo")
            cargo = str(candidate) if candidate.is_file() else "cargo"
        lock_path = ROOT / "client-tests/Cargo.lock"
        if lock_path.exists():
            shutil.copyfile(lock_path, crate / "Cargo.lock")
        env = {**os.environ, "CARGO_RESOLVER_INCOMPATIBLE_RUST_VERSIONS": "fallback"}
        subprocess.run(
            [cargo, "test", "--manifest-path", str(crate / "Cargo.toml")]
            + (["--locked"] if lock_path.exists() and not update_lock else []),
            cwd=crate,
            check=True,
            env=env,
        )
        lock_path.parent.mkdir(exist_ok=True)
        shutil.copyfile(crate / "Cargo.lock", lock_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--update-lock", action="store_true")
    args = parser.parse_args()
    verify(args.source.resolve() if args.source else None, args.update_lock)
