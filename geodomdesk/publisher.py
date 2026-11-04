#!/usr/bin/env python3
"""Package custom GitHub Releases and publish GeodomDesk's signed update channel."""
import argparse
import base64
import contextlib
import concurrent.futures
import datetime
import fcntl
import hashlib
import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import channel
import installer

ROOT = Path(__file__).resolve().parent
LIMIT = 512 * 1024 * 1024
START = "<!-- rustdesk-downloads:start -->"
END = "<!-- rustdesk-downloads:end -->"
USER_AGENT = "GeodomDesk-Publisher/1"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def atomic_bytes(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def save_json(path, value, mode=0o644):
    atomic_bytes(path, channel.stable_json(value), mode)


def validate_repository(repository):
    if not isinstance(repository, str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository
    ):
        raise ValueError("Configure repository as owner/repository")
    if (
        any(part in (".", "..") for part in repository.split("/"))
        or repository.lower() == "rustdesk/rustdesk"
    ):
        raise ValueError("Use your own GeodomDesk build repository")
    return repository


def request_json(url, headers=None):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
            **(headers or {}),
        },
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("GitHub response exceeds size limit")
        return json.loads(data), response.headers.get("ETag")


def release_info(config, state):
    repo = validate_repository(config["repository"])
    headers = {}
    token_path = config.get("github_token_file")
    if token_path:
        token = Path(token_path).read_text().strip()
        if not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid GitHub token file")
        headers["Authorization"] = "Bearer " + token
    cached_path = state / "github-release.json"
    cached = None
    if cached_path.exists():
        try:
            cached = json.loads(cached_path.read_text())
            if cached.get("repository") != repo:
                cached = None
            elif cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
        except (ValueError, KeyError, TypeError):
            cached = None
    api = "https://api.github.com/repos/" + repo
    try:
        release, etag = request_json(api + "/releases/latest", headers)
    except urllib.error.HTTPError as error:
        if error.code != 304 or not cached:
            raise
        release = cached["release"]
    else:
        save_json(cached_path, {"repository": repo, "etag": etag, "release": release}, 0o600)
    version = release.get("tag_name")
    channel.version_tuple(version)
    if (
        release.get("draft")
        or release.get("prerelease")
        or release.get("html_url") != f"https://github.com/{repo}/releases/tag/{version}"
    ):
        raise ValueError("Expected a stable release from the configured build repository")
    # Authorization is only sent to api.github.com; DownloadRedirect removes it.
    return release, api, {k: v for k, v in headers.items() if k != "If-None-Match"}


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != "https" or not (
            parsed.hostname == "github.com"
            or (parsed.hostname or "").endswith(".githubusercontent.com")
        ):
            raise ValueError("Unexpected GitHub asset redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


def select_asset(release, name, api):
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise ValueError("Unsafe release asset name")
    found = [a for a in release.get("assets", []) if a.get("name") == name]
    if len(found) != 1:
        raise ValueError("Release is missing a unique asset: " + name)
    asset = found[0]
    repo = api.removeprefix("https://api.github.com/repos/")
    if (
        not re.fullmatch(re.escape(api) + r"/releases/assets/[0-9]+", asset.get("url", ""))
        or asset.get("browser_download_url")
        != f'https://github.com/{repo}/releases/download/{release["tag_name"]}/{name}'
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", asset.get("digest", ""))
        or type(asset.get("size")) is not int
        or not 0 < asset["size"] <= LIMIT
    ):
        raise ValueError("Invalid GitHub release asset: " + name)
    return asset


def download(asset, cache, headers):
    digest = asset["digest"][7:]
    final = cache / (digest + "-" + asset["name"])
    if (
        final.is_file()
        and final.stat().st_size == asset["size"]
        and channel.sha256(final) == digest
    ):
        return final
    fd, temp = tempfile.mkstemp(prefix=".download-", dir=cache)
    try:
        request = urllib.request.Request(
            asset["url"],
            headers={**headers, "User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        )
        if asset["size"] > 4 * 1024 * 1024:
            request.add_header("Range", "bytes=0-0")
        opener = urllib.request.build_opener(DownloadRedirect())
        length = 0
        with os.fdopen(fd, "wb") as stream:
            with opener.open(request, timeout=60) as response:
                ranged = response.status == 206
                if ranged:
                    if response.headers.get("Content-Range") != f'bytes 0-0/{asset["size"]}':
                        raise ValueError("Unexpected GitHub range response")
                    url = response.geturl()
                else:
                    for block in iter(lambda: response.read(1024 * 1024), b""):
                        length += len(block)
                        if length > asset["size"]:
                            raise ValueError("Download exceeds declared size")
                        stream.write(block)
            if ranged:
                stream.truncate(asset["size"])
                download_ranges(url, asset["size"], stream.fileno(), headers)
                length = asset["size"]
        if length != asset["size"] or channel.sha256(temp) != digest:
            raise ValueError("Release asset size or SHA256 mismatch")
        os.replace(temp, final)
        return final
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def download_ranges(url, size, fd, headers):
    headers = {**headers, "User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    if urllib.parse.urlsplit(url).hostname != "api.github.com":
        headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}

    def part(start):
        end = min(start + 1024 * 1024, size) - 1
        request = urllib.request.Request(url, headers={**headers, "Range": f"bytes={start}-{end}"})
        opener = urllib.request.build_opener(DownloadRedirect())
        with opener.open(request, timeout=60) as response:
            if (
                response.status != 206
                or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}"
            ):
                raise ValueError("GitHub did not honor the requested download range")
            data = response.read(end - start + 2)
        if len(data) != end - start + 1 or os.pwrite(fd, data, start) != len(data):
            raise ValueError("Incomplete release asset download range")

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(part, range(0, size, 1024 * 1024)):
            pass


def validate_build(build, release, config):
    version = release["tag_name"]
    release_id = build.get("release_id", version)
    if channel.release_parts(release_id) != (version, build.get("build_number", 0)):
        raise ValueError("Build revision does not match the official version")
    expected = {
        "schema_version": 1,
        "app": "GeodomDesk",
        "package_id": "geodomdesk",
        "repository": config["repository"],
        "version": version,
        "base_url": channel.BASE_URL,
        "key_id": channel.KEY_ID,
        "public_key": config["public_key"],
    }
    if any(build.get(k) != v for k, v in expected.items()):
        raise ValueError("Build identity/version/update key does not match this GeodomDesk channel")
    if not re.fullmatch(r"[0-9a-f]{40}", build.get("upstream_commit", "")):
        raise ValueError("Source commit is required")
    items = build.get("artifacts", [])
    if len(items) != len(channel.TARGETS) or {
        (a.get("platform"), a.get("arch"), a.get("format")) for a in items
    } != set(channel.TARGETS):
        raise ValueError("Release requires Windows x64 EXE, Ubuntu amd64 DEB and Android ARM64 APK")
    for item in items:
        if item.get("file_name") != channel.file_name(release_id, item["platform"], item["format"]):
            raise ValueError("Unexpected GeodomDesk payload name")
    source = build.get("source", {})
    if source.get("file_name") != f"geodomdesk-{release_id}-source.tar.gz":
        raise ValueError("Provide the corresponding source snapshot, including modified submodules")
    return items + [source]


def check_deb(payload, version, build_number=0):
    result = subprocess.run(
        ["dpkg-deb", "-f", str(payload), "Package", "Version", "Architecture"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    expected = {
        "Package: geodomdesk",
        "Version: " + version + (f"+geodom.{build_number}" if build_number else ""),
        "Architecture: amd64",
    }
    if set(result.splitlines()) != expected:
        raise ValueError("Expected an isolated geodomdesk package/version/architecture")
    listing = subprocess.run(
        ["dpkg-deb", "-c", str(payload)], capture_output=True, text=True, check=True, timeout=30
    ).stdout
    for name in [
        "/usr/bin/rustdesk",
        "/usr/share/rustdesk/",
        "/rustdesk.service",
        "/rustdesk.desktop",
    ]:
        if name in listing:
            raise ValueError("DEB would overwrite stock RustDesk: " + name)
    with tempfile.TemporaryDirectory(prefix="geodomdesk-control-") as tmp:
        subprocess.run(["dpkg-deb", "-e", str(payload), tmp], check=True, timeout=30)
        for path in Path(tmp).iterdir():
            if path.name != "control" and "rustdesk" in path.read_text(errors="replace").lower():
                raise ValueError("DEB maintainer script references stock RustDesk")


def check_apk(payload):
    with zipfile.ZipFile(payload) as archive:
        for name in ["AndroidManifest.xml", "classes.dex", "lib/arm64-v8a/librustdesk.so"]:
            if not archive.getinfo(name).file_size:
                raise ValueError("Android release is missing its native application payload")


def page(version):
    # The local download page remains concise; latest.json is the only mutable pointer.
    return (
        """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>GeodomDesk · 远程工具</title><style>body{margin:0;background:#f4f5f4;color:#192a32;font:16px system-ui,"Microsoft YaHei",sans-serif}main{max-width:760px;margin:10vh auto;padding:32px}header{display:flex;align-items:center;gap:18px}header img{width:64px;height:64px}h1{font-size:32px;margin:0}header p{margin:8px 0;color:#687981}.links{display:grid;gap:16px;margin:40px 0}a{color:inherit}a.card{display:flex;justify-content:space-between;align-items:center;text-decoration:none;border:1px solid #d8dfdd;border-radius:14px;padding:24px;background:#fff}.card span{color:#257678}.foot{color:#687981;font-size:13px;display:flex;gap:24px}a:focus-visible{outline:3px solid #169baf;outline-offset:4px}</style><main><header><img src="../../geodomdesk.png" alt=""><div><h1>GeodomDesk</h1><p>远程工具 · """
        + html.escape(channel.release_parts(version)[0])
        + """</p></div></header><div class="links"><a class="card" href="geodomdesk-"""
        + version
        + """-windows-x64-setup.exe" download><strong>Windows</strong><span>x64 · 下载安装包 ↗</span></a><a class="card" href="geodomdesk-"""
        + version
        + """-ubuntu-amd64.run" download><strong>Ubuntu</strong><span>amd64 · 下载安装包 ↗</span></a><a class="card" href="geodomdesk-"""
        + version
        + """-android-arm64.apk" download><strong>Android</strong><span>ARM64 · 下载 APK ↗</span></a></div><div class="foot"><a href="geodomdesk-"""
        + version
        + """-source.tar.gz">源代码</a><a href="https://geodom.cn">geodom.cn</a></div></main></html>"""
    ).encode()


def public_complete(directory, manifest):
    for item in manifest["files"]:
        path = directory / item["name"]
        if (
            not path.is_file()
            or path.stat().st_size != item["size"]
            or channel.sha256(path) != item["sha256"]
        ):
            return False
    return True


def publish_pointer(public, manifest):
    version = manifest["version"]
    release_id = manifest.get("release_id", version)
    latest = {
        "url": channel.release_url(release_id),
        "version": version,
        "build_number": manifest.get("build_number", 0),
        "metadata_url": channel.release_url(release_id) + channel.METADATA_NAME,
        "windows_url": channel.release_url(release_id)
        + f"geodomdesk-{release_id}-windows-x64-setup.exe",
        "ubuntu_url": channel.release_url(release_id) + f"geodomdesk-{release_id}-ubuntu-amd64.run",
        "android_url": channel.release_url(release_id)
        + f"geodomdesk-{release_id}-android-arm64.apk",
    }
    # All targets are in immutable, verified directories before exposing the pointer.
    save_json(public / "latest.json", latest)
    atomic_bytes(
        public / "index.html",
        (
            f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="robots" content="noindex,nofollow"><meta http-equiv="refresh" content="0;url=releases/{release_id}/"><title>GeodomDesk</title><a href="releases/{release_id}/">GeodomDesk {version}</a></html>'
        ).encode(),
    )


def portal_update(portal, version):
    before = portal.read_bytes()
    text = before.decode("utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("Download section markers are missing or ambiguous")
    fragment = f"""<section class="client-downloads" aria-labelledby="clients-title"><div class="section-heading"><h2 id="clients-title">远程工具</h2><p>GeodomDesk {channel.release_parts(version)[0]}</p></div><div class="client-list"><div class="client-row"><div class="client-name"><h3>Windows</h3><p>x64</p></div><div class="client-links"><a href="{channel.release_url(version)}geodomdesk-{version}-windows-x64-setup.exe" download>下载 Windows 版<svg aria-hidden="true"><use href="#download"/></svg></a></div></div><div class="client-row"><div class="client-name"><h3>Ubuntu</h3><p>amd64</p></div><div class="client-links"><a href="{channel.release_url(version)}geodomdesk-{version}-ubuntu-amd64.run" download>下载 Ubuntu 版<svg aria-hidden="true"><use href="#download"/></svg></a></div></div><div class="client-row"><div class="client-name"><h3>Android</h3><p>ARM64</p></div><div class="client-links"><a href="{channel.release_url(version)}geodomdesk-{version}-android-arm64.apk" download>下载 Android 版<svg aria-hidden="true"><use href="#download"/></svg></a></div></div></div></section>"""
    changed = text.split(START)[0] + START + "\n" + fragment + "\n" + END + text.split(END)[1]
    if portal.read_bytes() != before:
        raise RuntimeError("Portal changed during publication")
    atomic_bytes(portal, changed.encode())


def run(config):
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not config.get("enabled") or not config.get("repository"):
            status = "staged" if config.get("repository") else "waiting-for-build-repository"
            save_json(
                state / "status.json",
                {"status": status, "checked_at": now()},
            )
            logging.info("GeodomDesk publisher %s; current downloads unchanged", status)
            return
        seed = Path(config["signing_key"]).read_bytes()
        if base64.b64encode(channel.public_key(seed)).decode() != config["public_key"]:
            raise ValueError("Signing key does not match the compiled client trust key")
        installer.validate_server(config)
        public = Path(config["public_dir"])
        portal = Path(config["portal_file"])
        if (
            public.is_symlink()
            or public.resolve() != portal.parent.resolve() / "downloads/geodomdesk"
        ):
            raise ValueError("Unexpected GeodomDesk publication path")
        public.mkdir(parents=True, exist_ok=True)
        release, api, headers = release_info(config, state)
        version = release["tag_name"]
        current = (
            json.loads((public / "latest.json").read_text())
            if (public / "latest.json").exists()
            else None
        )
        if (
            current
            and current.get("version")
            and channel.version_tuple(version) < channel.version_tuple(current["version"])
        ):
            raise ValueError("Refusing to downgrade the GeodomDesk update channel")
        cache = state / "cache"
        cache.mkdir(exist_ok=True)
        asset = select_asset(release, "geodomdesk-build.json", api)
        if asset["size"] > 64 * 1024:
            raise ValueError("Build descriptor exceeds size limit")
        descriptor = download(asset, cache, headers)
        build = json.loads(descriptor.read_text())
        items = validate_build(build, release, config)
        release_id = build.get("release_id", version)
        build_number = build.get("build_number", 0)
        if (
            current
            and current.get("version")
            and (channel.version_tuple(version), build_number)
            < (channel.version_tuple(current["version"]), current.get("build_number", 0))
        ):
            raise ValueError("Refusing to roll back the GeodomDesk build revision")
        destination = public / "releases" / release_id
        if destination.exists():
            manifest = json.loads((destination / "publication.json").read_text())
            if manifest["build_sha256"] != channel.sha256(descriptor) or not public_complete(
                destination, manifest
            ):
                raise ValueError("Immutable release changed or damaged; publish a new version")
        else:
            if shutil.disk_usage(state).free < 1024 * 1024 * 1024:
                raise ValueError("At least 1 GiB free disk is required")
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Stage on the public filesystem so the complete directory rename is atomic.
            if state.stat().st_dev != destination.parent.stat().st_dev:
                raise ValueError(
                    "Build state and publication must be on the same filesystem for atomic releases"
                )
            with tempfile.TemporaryDirectory(prefix="build-", dir=state) as temporary:
                stage = Path(temporary) / "release"
                stage.mkdir()
                payloads = {}
                for item in items:
                    asset = select_asset(release, item["file_name"], api)
                    if (
                        item.get("size") != asset["size"]
                        or item.get("sha256") != asset["digest"][7:]
                    ):
                        raise ValueError("Build descriptor and GitHub asset digest disagree")
                    payload = download(asset, cache, headers)
                    shutil.copyfile(payload, stage / item["file_name"])
                    payloads[item["file_name"]] = (payload, asset)
                updates = []
                for platform, arch, fmt in channel.TARGETS:
                    name = channel.file_name(release_id, platform, fmt)
                    payload, asset = payloads[name]
                    updates.append(channel.artifact(stage / name, release_id, platform, arch, fmt))
                    if platform == "windows":
                        installer.make_windows(
                            payload,
                            asset,
                            config,
                            version,
                            stage,
                            ROOT / "LICENSE.client",
                            stage / f"geodomdesk-{release_id}-windows-x64-setup.exe",
                        )
                    elif platform == "linux":
                        check_deb(payload, version, build_number)
                        installer.make_linux(
                            payload,
                            asset,
                            "deb",
                            "x86_64",
                            config,
                            version,
                            stage / f"geodomdesk-{release_id}-ubuntu-amd64.run",
                        )
                    else:
                        check_apk(payload)
                metadata, signature = channel.signed_metadata(
                    release_id, release["published_at"], updates, seed
                )
                (stage / channel.METADATA_NAME).write_bytes(metadata)
                (stage / (channel.METADATA_NAME + ".sig")).write_bytes(signature)
                (stage / "index.html").write_bytes(page(release_id))
                # Never publish the generated installer script separately: it can contain a preset password.
                for temporary_name in ["install-windows.ps1", "installer.nsi"]:
                    (stage / temporary_name).unlink(missing_ok=True)
                files = [
                    {"name": p.name, "size": p.stat().st_size, "sha256": channel.sha256(p)}
                    for p in stage.iterdir()
                    if p.is_file()
                ]
                manifest = {
                    "version": version,
                    "release_id": release_id,
                    "build_number": build_number,
                    "published_at": now(),
                    "build_sha256": channel.sha256(descriptor),
                    "files": files,
                }
                (stage / "publication.json").write_bytes(channel.stable_json(manifest))
                if not public_complete(stage, manifest):
                    raise ValueError("Final publication verification failed")
                os.replace(stage, destination)
        (public / "geodomdesk.png").write_bytes((ROOT / "assets/geodomdesk.png").read_bytes())
        publish_pointer(public, manifest)
        if config.get("activate_portal"):
            # The activation command disables the former producer before setting this flag.
            for unit in ["geodom-rustdesk-builder.timer", "geodom-rustdesk-builder.service"]:
                if (
                    subprocess.run(
                        ["systemctl", "is-active", "--quiet", unit], timeout=30
                    ).returncode
                    == 0
                ):
                    raise RuntimeError(
                        "Stop the former builder before activating this portal producer"
                    )
            portal_update(portal, release_id)
        save_json(
            state / "status.json", {"status": "published", "version": version, "checked_at": now()}
        )
        logging.info("GeodomDesk %s published with signed update metadata", version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/geodomdesk-builder/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(Path(args.config).read_text())
    try:
        run(config)
    except BlockingIOError:
        logging.info("Another publisher run holds the lock")
    except Exception:
        save_json(
            Path(config["state_dir"]) / "status.json", {"status": "failed", "checked_at": now()}
        )
        logging.exception("Publication failed; the previous update release remains available")
        raise


if __name__ == "__main__":
    main()
