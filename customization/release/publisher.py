#!/usr/bin/env python3
"""Verify native GitHub Releases and publish the signed GeodomDesk update channel."""
import argparse
import base64
import contextlib
import concurrent.futures
import datetime
import errno
import fcntl
import hashlib
import html
import http.client
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release import channel
LIMIT = 512 * 1024 * 1024
START = "<!-- rustdesk-downloads:start -->"
END = "<!-- rustdesk-downloads:end -->"
USER_AGENT = "GeodomDesk-Publisher/1"
RANGE_CHUNK_BYTES = 4 * 1024 * 1024
OFFICIAL_ASSET_TIMEOUT = 25
ASSET_IDLE_TIMEOUT = 10
DEFAULT_ASSET_ACCELERATORS = ("https://gh-proxy.org/", "https://ghfast.top/")
API_RETRY_DELAYS = (0.5, 1.5)


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
    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        **(headers or {}),
    }
    for attempt in range(len(API_RETRY_DELAYS) + 1):
        request = urllib.request.Request(url, headers=req_headers)
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise ValueError("GitHub response exceeds size limit")
                return json.loads(data), response.headers.get("ETag")
        except urllib.error.HTTPError as error:
            if attempt >= len(API_RETRY_DELAYS) or error.code not in (500, 502, 503, 504):
                raise
            error.close()
            logging.warning("GitHub API %s failed with HTTP %d; retrying", url, error.code)
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
            if attempt >= len(API_RETRY_DELAYS):
                raise
            logging.warning("GitHub API %s failed (%s); retrying", url, type(error).__name__)
        time.sleep(API_RETRY_DELAYS[attempt])


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


def accelerator_prefixes(config):
    if "github_asset_accelerators" not in config:
        prefixes = list(DEFAULT_ASSET_ACCELERATORS)
    else:
        prefixes = config["github_asset_accelerators"]
    if not isinstance(prefixes, list) or len(prefixes) > 5:
        raise ValueError("Configure up to five GitHub asset accelerator prefixes")
    result = []
    for prefix in prefixes:
        if not isinstance(prefix, str) or not re.fullmatch(
            r"https://[A-Za-z0-9.-]+(?::443)?(?:/[A-Za-z0-9_/-]*)?", prefix
        ):
            raise ValueError("GitHub asset accelerator requires an HTTPS prefix")
        prefix = prefix.rstrip("/") + "/"
        if prefix not in result:
            result.append(prefix)
    return tuple(result)


def prefer_accelerators(config):
    value = config.get("github_prefer_accelerators", True)
    if not isinstance(value, bool):
        raise ValueError("github_prefer_accelerators must be a boolean")
    return value


class DownloadRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, accelerator_host=None):
        self.accelerator_host = accelerator_host

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.port not in (None, 443)
            or not (
                host == "github.com"
                or host.endswith(".githubusercontent.com")
                or (
                    self.accelerator_host is not None
                    and (host == self.accelerator_host or host.endswith("." + self.accelerator_host))
                )
            )
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


class _AssetDeadline(TimeoutError):
    pass


def _asset_timeout(deadline):
    if deadline is None:
        return 60
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _AssetDeadline("Asset download time limit exceeded")
    return min(remaining, ASSET_IDLE_TIMEOUT)


def _asset_blocks(response, limit, deadline):
    while limit:
        _asset_timeout(deadline)
        block = response.read1(min(limit, 256 * 1024))
        _asset_timeout(deadline)
        if not block:
            break
        limit -= len(block)
        yield block


def _resolve_range_url(url, expected_size, headers, opener, deadline=None):
    request = urllib.request.Request(
        url, headers={**headers, "Range": "bytes=0-0"}
    )
    with opener.open(request, timeout=_asset_timeout(deadline)) as response:
        if (
            response.status != 206
            or response.headers.get("Content-Range") != f"bytes 0-0/{expected_size}"
        ):
            raise ValueError("Unexpected GitHub range response")
        return response.geturl()


def _download_asset(asset, final, url, headers, deadline=None, accelerator_host=None):
    size = asset["size"]
    headers = {**headers, "User-Agent": USER_AGENT, "Accept": "application/octet-stream"}
    fd, temp = tempfile.mkstemp(prefix=".download-", dir=final.parent)
    try:
        request = urllib.request.Request(url, headers=headers)
        if size > RANGE_CHUNK_BYTES:
            request.add_header("Range", "bytes=0-0")
        opener = urllib.request.build_opener(DownloadRedirect(accelerator_host))
        length = 0
        with os.fdopen(fd, "wb") as stream:
            with opener.open(request, timeout=_asset_timeout(deadline)) as response:
                ranged = response.status == 206
                if ranged:
                    if response.headers.get("Content-Range") != f"bytes 0-0/{size}":
                        raise ValueError("Unexpected GitHub range response")
                    range_url = response.geturl()
                else:
                    for block in _asset_blocks(response, size + 1, deadline):
                        length += len(block)
                        if length > size:
                            raise ValueError("Download exceeds declared size")
                        stream.write(block)
            if ranged:
                stream.truncate(size)
                download_ranges(
                    range_url, size, stream.fileno(), headers, opener=opener,
                    resolve_url=lambda: _resolve_range_url(url, size, headers, opener, deadline),
                    deadline=deadline,
                )
                length = size
        _asset_timeout(deadline)
        if length != size or channel.sha256(temp) != asset["digest"][7:]:
            raise ValueError("Release asset size or SHA256 mismatch")
        os.replace(temp, final)
        return final
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)


def download(asset, cache, headers, accelerators=(), prefer_accelerators=True, route_state=None):
    digest = asset["digest"][7:]
    final = cache / (digest + "-" + asset["name"])
    if (
        final.is_file()
        and final.stat().st_size == asset["size"]
        and channel.sha256(final) == digest
    ):
        return final
    if not accelerators:
        return _download_asset(asset, final, asset["url"], headers)

    # select_asset has bound metadata to the configured GitHub repo and release.
    browser_url = asset["browser_download_url"]
    if not re.fullmatch(
        r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/"
        r"[0-9]+\.[0-9]+\.[0-9]+/[A-Za-z0-9._-]+", browser_url
    ):
        raise ValueError("Expected a public GitHub release asset URL")
    ordered = list(accelerators)
    if route_state is not None:
        preferred = route_state.get("preferred")
        failed = route_state.get("failed", set())
        ordered.sort(key=lambda prefix: (prefix != preferred, prefix in failed))
    accel_sources = [
        (
            prefix + browser_url,
            {},
            max(90, asset["size"] / (512 * 1024)),
            urllib.parse.urlsplit(prefix).hostname,
            prefix,
        )
        for prefix in ordered
    ]
    official_budget = max(90, asset["size"] / (256 * 1024)) if prefer_accelerators else OFFICIAL_ASSET_TIMEOUT
    official_source = (asset["url"], headers, official_budget, None, None)
    sources = [*accel_sources, official_source] if prefer_accelerators else [official_source, *accel_sources]
    for url, source_headers, budget, accelerator_host, prefix in sources:
        host = accelerator_host or "api.github.com"
        try:
            path = _download_asset(
                asset, final, url, source_headers, time.monotonic() + budget, accelerator_host
            )
        except (OSError, http.client.HTTPException, ValueError) as error:
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if route_state is not None and prefix is not None:
                route_state.setdefault("failed", set()).add(prefix)
                if route_state.get("preferred") == prefix:
                    route_state["preferred"] = None
            logging.warning("Asset %s download failed via %s (%s)", asset["name"], host, type(error).__name__)
            continue
        if accelerator_host:
            if route_state is not None and prefix is not None:
                route_state["preferred"] = prefix
                route_state.get("failed", set()).discard(prefix)
            logging.info("Verified asset %s via %s", asset["name"], host)
        return path
    raise RuntimeError(f"Could not download and verify GitHub release asset {asset['name']}")


def download_ranges(url, size, fd, headers, opener=None, resolve_url=None, deadline=None):
    base_headers = {**headers, "User-Agent": USER_AGENT, "Accept": "application/octet-stream"}

    def scoped_headers(target_url):
        if urllib.parse.urlsplit(target_url).hostname != "api.github.com":
            return {k: v for k, v in base_headers.items() if k.lower() != "authorization"}
        return base_headers

    opener = opener or urllib.request.build_opener(DownloadRedirect())
    url_lock = threading.Lock()
    state = {"url": url}

    class _IncompleteRangeError(ValueError):
        pass

    def part(start):
        end = min(start + RANGE_CHUNK_BYTES, size) - 1
        for attempt in range(3):
            with url_lock:
                target_url = state["url"]
            request = urllib.request.Request(
                target_url,
                headers={**scoped_headers(target_url), "Range": f"bytes={start}-{end}"},
            )
            try:
                with opener.open(request, timeout=_asset_timeout(deadline)) as response:
                    if (
                        response.status != 206
                        or response.headers.get("Content-Range")
                        != f"bytes {start}-{end}/{size}"
                    ):
                        raise ValueError("GitHub did not honor the requested download range")
                    data = b"".join(_asset_blocks(response, end - start + 2, deadline))
                if len(data) != end - start + 1 or os.pwrite(fd, data, start) != len(data):
                    raise _IncompleteRangeError("Incomplete release asset download range")
                return
            except _AssetDeadline:
                raise
            except urllib.error.HTTPError as err:
                err.close()
                if attempt >= 2 or err.code not in (401, 403, 410, 429, 500, 502, 503, 504):
                    raise
                if err.code in (401, 403, 410) and resolve_url is not None:
                    with url_lock:
                        if state["url"] == target_url:
                            state["url"] = resolve_url()
                else:
                    time.sleep(0.2 * (attempt + 1))
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, _IncompleteRangeError):
                if attempt >= 2:
                    raise
                time.sleep(0.2 * (attempt + 1))

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(part, range(0, size, RANGE_CHUNK_BYTES)):
            pass


def prune_cache(cache, keep_paths, max_extra=4):
    keep = {Path(p).resolve() for p in keep_paths}
    extras = [p for p in cache.iterdir() if p.is_file() and p.resolve() not in keep]
    extras.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in extras[max_extra:]:
        with contextlib.suppress(OSError):
            stale.unlink()


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
    channel.release_targets(items)
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


def validate_legacy_windows(config):
    raw = config.get("legacy_windows")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("legacy_windows configuration must be an object")
    url = raw.get("url")
    if not isinstance(url, str) or not url.startswith(channel.BASE_URL + "/"):
        raise ValueError("legacy_windows.url must be under the GeodomDesk channel BASE_URL")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F or ch in ('"', "'", "<", ">") for ch in url):
        raise ValueError("Invalid characters in legacy_windows.url")
    version = raw.get("version") or ""
    if not isinstance(version, str):
        raise ValueError("legacy_windows.version must be a string")
    version = version.strip()
    if version:
        channel.release_parts(version)
    return {"url": url, "version": version}


def download_names(version, native_config):
    return {
        "windows": (
            channel.file_name(version, "windows", "exe")
            if native_config
            else f"geodomdesk-{version}-windows-x64-setup.exe"
        ),
        "ubuntu": (
            channel.file_name(version, "linux", "deb")
            if native_config
            else f"geodomdesk-{version}-ubuntu-amd64.run"
        ),
        "android": channel.file_name(version, "android", "apk"),
    }


def page(version, native_config=True, legacy_windows=None, changelog=""):
    names = download_names(version, native_config)
    compatibility_link = ""
    if legacy_windows is not None:
        ver_suffix = f" · {html.escape(legacy_windows['version'])}" if legacy_windows["version"] else ""
        compatibility_link = (
            f'<a class="card" href="{html.escape(legacy_windows["url"], quote=True)}" download>'
            f'<strong>Windows 旧系统兼容版（Win7 / 8 / Server 2008 R2–2012 R2）</strong>'
            f'<span>x64{ver_suffix} · 已停止更新 · 下载归档 ↗</span></a>'
        )
    style = 'body{margin:0;background:#f4f5f4;color:#192a32;font:16px system-ui,"Microsoft YaHei",sans-serif}main{max-width:760px;margin:10vh auto;padding:32px}header{display:flex;align-items:center;gap:18px}header img{width:64px;height:64px}h1{font-size:32px;margin:0}header p{margin:8px 0;color:#687981}.links{display:grid;gap:16px;margin:40px 0}.changelog{margin:0 0 32px;border:1px solid #d8dfdd;border-radius:14px;padding:22px 24px;background:#fff}.changelog h2{margin:0 0 12px;font-size:18px}.changelog-body{margin:0;color:#2c3e46;font-size:14px;line-height:1.65;white-space:pre-wrap;word-break:break-word}a{color:inherit}a.card{display:flex;justify-content:space-between;align-items:center;text-decoration:none;border:1px solid #d8dfdd;border-radius:14px;padding:24px;background:#fff}.card span{color:#257678}.foot{color:#687981;font-size:13px;display:flex;gap:24px}a:focus-visible{outline:3px solid #169baf;outline-offset:4px}'
    channel.release_parts(version)
    display_version = html.escape(version)
    normalized_changelog = channel.normalize_changelog(changelog)
    changelog_section = ""
    if normalized_changelog:
        changelog_section = (
            '<section class="changelog" aria-labelledby="changelog-title">'
            '<h2 id="changelog-title">更新日志</h2>'
            f'<div class="changelog-body">{html.escape(normalized_changelog)}</div>'
            "</section>"
        )
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>GeodomDesk · 远程工具</title><style>{style}</style><main><header><img src="../../geodomdesk.png" alt=""><div><h1>GeodomDesk</h1><p>远程工具 · {display_version}</p></div></header><div class="links"><a class="card" href="{names["windows"]}" download><strong>Windows（Win10 / 11 / Server 2016+）</strong><span>x64 · 下载安装包 ↗</span></a>{compatibility_link}<a class="card" href="{names["ubuntu"]}" download><strong>Ubuntu</strong><span>amd64 · 下载 DEB ↗</span></a><a class="card" href="{names["android"]}" download><strong>Android</strong><span>ARM64 · 下载 APK ↗</span></a></div>{changelog_section}<div class="foot"><a href="geodomdesk-{version}-source.tar.gz">源代码</a><a href="https://geodom.cn">geodom.cn</a></div></main></html>""".encode()


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


def publish_directory(stage, destination, manifest):
    try:
        os.replace(stage, destination)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        # systemd can bind-mount two paths separately even on the same device.
        # The complete release has already been verified before it reaches this path.
        with tempfile.TemporaryDirectory(prefix=".publish-", dir=destination.parent) as temporary:
            copied = Path(temporary) / "release"
            shutil.copytree(stage, copied)
            if not public_complete(copied, manifest):
                raise ValueError("Copied publication verification failed")
            os.replace(copied, destination)


def publish_pointer(public, manifest):
    version = manifest["version"]
    release_id = manifest.get("release_id", version)
    names = download_names(release_id, manifest.get("native_config", False))
    latest = {
        "url": channel.release_url(release_id),
        "version": version,
        "build_number": manifest.get("build_number", 0),
        "metadata_url": channel.release_url(release_id) + channel.METADATA_NAME,
        "windows_url": channel.release_url(release_id) + names["windows"],
        "ubuntu_url": channel.release_url(release_id) + names["ubuntu"],
        "android_url": channel.release_url(release_id) + names["android"],
        "native_config": manifest.get("native_config", False),
    }
    published_at = manifest.get("published_at")
    if isinstance(published_at, str) and published_at.strip():
        latest["published_at"] = published_at.strip()
    changelog = channel.normalize_changelog(manifest.get("changelog", ""))
    if changelog:
        latest["changelog"] = changelog
    # All targets are in immutable, verified directories before exposing the pointer.
    save_json(public / "latest.json", latest)
    atomic_bytes(
        public / "index.html",
        (
            f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="robots" content="noindex,nofollow"><meta http-equiv="refresh" content="0;url=releases/{release_id}/"><title>GeodomDesk</title><a href="releases/{release_id}/">GeodomDesk {html.escape(release_id)}</a></html>'
        ).encode(),
    )


def portal_fragment(version, native_config=True, legacy_windows=None):
    channel.release_parts(version)
    names = download_names(version, native_config)
    compatibility_link = ""
    if legacy_windows is not None:
        ver_suffix = f" {html.escape(legacy_windows['version'])}" if legacy_windows["version"] else ""
        compatibility_link = (
            f'<a href="{html.escape(legacy_windows["url"], quote=True)}" download>'
            f'下载旧系统兼容版{ver_suffix}（Win7 / 8 / Server 2008 R2–2012 R2 · 已停止更新）'
            '<svg aria-hidden="true"><use href="#download"/></svg></a>'
        )
    return (
        f'<section class="client-downloads" aria-labelledby="clients-title">'
        f'<div class="section-heading"><h2 id="clients-title">远程工具</h2><p>GeodomDesk {html.escape(version)}</p></div>'
        f'<div class="client-list">'
        f'<div class="client-row"><div class="client-name"><h3>网页版</h3><p>免安装 · 浏览器访问</p></div><div class="client-links"><a href="/geodomdesk/">打开网页版<svg aria-hidden="true"><use href="#arrow"/></svg></a></div></div>'
        f'<div class="client-row"><div class="client-name"><h3>Windows</h3><p>x64 · Win10 / 11 / Server 2016+</p></div><div class="client-links"><a href="{channel.release_url(version)}{names["windows"]}" download>下载 Windows 标准版<svg aria-hidden="true"><use href="#download"/></svg></a>{compatibility_link}</div></div>'
        f'<div class="client-row"><div class="client-name"><h3>Ubuntu</h3><p>amd64</p></div><div class="client-links"><a href="{channel.release_url(version)}{names["ubuntu"]}" download>下载 Ubuntu 版<svg aria-hidden="true"><use href="#download"/></svg></a></div></div>'
        f'<div class="client-row"><div class="client-name"><h3>Android</h3><p>ARM64</p></div><div class="client-links"><a href="{channel.release_url(version)}{names["android"]}" download>下载 Android 版<svg aria-hidden="true"><use href="#download"/></svg></a></div></div>'
        f'</div></section>'
    )


def portal_update(portal, version, native_config=True, legacy_windows=None):
    fragment = portal_fragment(version, native_config, legacy_windows)
    before = portal.read_bytes()
    text = before.decode("utf-8")
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError("Download section markers are missing or ambiguous")
    changed = text.split(START)[0] + START + "\n" + fragment + "\n" + END + text.split(END)[1]
    if portal.read_bytes() != before:
        raise RuntimeError("Portal changed during publication")
    atomic_bytes(portal, changed.encode())


@contextlib.contextmanager
def _failed_status_on_error(state):
    try:
        yield
    except Exception:
        save_json(state / "status.json", {"status": "failed", "checked_at": now()})
        raise


def run(config):
    state = Path(config["state_dir"])
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with _failed_status_on_error(state):
            _run_locked(config, state)


def _run_locked(config, state):
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
        legacy_windows = validate_legacy_windows(config)
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
        accelerators = accelerator_prefixes(config)
        prefer_accel = prefer_accelerators(config)
        route_state = {}
        asset = select_asset(release, "geodomdesk-build.json", api)
        if asset["size"] > 64 * 1024:
            raise ValueError("Build descriptor exceeds size limit")
        descriptor = download(asset, cache, headers, accelerators, prefer_accel, route_state)
        keep_cache = [descriptor]
        build = json.loads(descriptor.read_text())
        items = validate_build(build, release, config)
        release_id = build.get("release_id", version)
        build_number = build.get("build_number") or 0
        if (
            current
            and current.get("version")
            and (channel.version_tuple(version), build_number)
            < (channel.version_tuple(current["version"]), current.get("build_number") or 0)
        ):
            raise ValueError("Refusing to roll back the GeodomDesk build revision")
        changelog_body = channel.normalize_changelog(release.get("body") or "")
        if changelog_body == (
            "Follows the official RustDesk version with GeodomDesk customization. "
            "Immutable custom build assets are listed in geodomdesk-build.json."
        ):
            changelog_body = ""
        changelog = channel.normalize_changelog(build.get("changelog") or "") or changelog_body
        destination = public / "releases" / release_id
        already_live = False
        if destination.exists():
            manifest = json.loads((destination / "publication.json").read_text())
            if manifest["build_sha256"] != channel.sha256(descriptor) or not public_complete(
                destination, manifest
            ):
                raise ValueError("Immutable release changed or damaged; publish a new version")
            already_live = (
                bool(current)
                and current.get("url") == channel.release_url(release_id)
                and (current.get("build_number") or 0) == build_number
                and bool(current.get("native_config", False)) == bool(manifest.get("native_config", False))
                and not current.get("windows_server_2008_r2_url")
                and not current.get("windows_legacy_url")
                and (current.get("published_at") or "") == (manifest.get("published_at") or "")
                and channel.normalize_changelog(current.get("changelog", ""))
                == channel.normalize_changelog(manifest.get("changelog", ""))
                and (public / "latest.json").is_file()
                and (public / "index.html").is_file()
                and (public / "geodomdesk.png").is_file()
            )
        else:
            if build.get("native_config") is not True:
                raise ValueError("New releases must contain compiled fixed connection settings")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (
                shutil.disk_usage(state).free < 1024 * 1024 * 1024
                or shutil.disk_usage(destination.parent).free < 1024 * 1024 * 1024
            ):
                raise ValueError("At least 1 GiB free disk is required")
            # Build privately, then publish atomically on the destination mount.
            with tempfile.TemporaryDirectory(prefix="build-", dir=state) as temporary:
                stage = Path(temporary) / "release"
                stage.mkdir()
                payloads = {}
                stage_digests = {}
                for item in items:
                    asset = select_asset(release, item["file_name"], api)
                    if (
                        item.get("size") != asset["size"]
                        or item.get("sha256") != asset["digest"][7:]
                    ):
                        raise ValueError("Build descriptor and GitHub asset digest disagree")
                    payload = download(asset, cache, headers, accelerators, prefer_accel, route_state)
                    keep_cache.append(payload)
                    staged = stage / item["file_name"]
                    shutil.copyfile(payload, staged)
                    stage_digest = channel.sha256(staged)
                    if stage_digest != asset["digest"][7:]:
                        raise ValueError("Copied release artifact SHA256 mismatch")
                    stage_digests[item["file_name"]] = stage_digest
                    payloads[item["file_name"]] = (payload, asset)
                updates = []
                targets = channel.release_targets(build["artifacts"])
                for platform, arch, fmt in targets:
                    name = channel.file_name(release_id, platform, fmt)
                    payload, asset = payloads[name]
                    updates.append(
                        channel.artifact(
                            stage / name,
                            release_id,
                            platform,
                            arch,
                            fmt,
                            digest=stage_digests[name],
                        )
                    )
                    if fmt == "exe":
                        with payload.open("rb") as stream:
                            if stream.read(2) != b"MZ":
                                raise ValueError("Invalid native Windows executable")
                    elif platform == "linux":
                        check_deb(payload, version, build_number)
                    else:
                        check_apk(payload)
                published_at = (release.get("published_at") or "").strip() or now()
                metadata, signature = channel.signed_metadata(
                    release_id, published_at, updates, seed, changelog=changelog
                )
                (stage / channel.METADATA_NAME).write_bytes(metadata)
                (stage / (channel.METADATA_NAME + ".sig")).write_bytes(signature)
                (stage / "index.html").write_bytes(
                    page(release_id, True, legacy_windows, changelog=changelog)
                )
                files = [
                    {
                        "name": p.name,
                        "size": p.stat().st_size,
                        "sha256": stage_digests.get(p.name) or channel.sha256(p),
                    }
                    for p in stage.iterdir()
                    if p.is_file()
                ]
                manifest = {
                    "version": version,
                    "release_id": release_id,
                    "build_number": build_number,
                    "native_config": True,
                    "published_at": published_at,
                    "build_sha256": channel.sha256(descriptor),
                    "files": files,
                }
                if changelog:
                    manifest["changelog"] = changelog
                (stage / "publication.json").write_bytes(channel.stable_json(manifest))
                if not public_complete(stage, manifest):
                    raise ValueError("Final publication verification failed")
                publish_directory(stage, destination, manifest)
        if not already_live:
            atomic_bytes(public / "geodomdesk.png", (ROOT / "assets/geodomdesk.png").read_bytes())
            publish_pointer(public, manifest)
        if config.get("activate_portal"):
            portal_text = portal.read_text(encoding="utf-8")
            expected_fragment = portal_fragment(
                release_id, manifest.get("native_config", False), legacy_windows
            )
            if (
                not already_live
                or channel.release_url(release_id) not in portal_text
                or expected_fragment not in portal_text
            ):
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
                portal_update(
                    portal,
                    release_id,
                    manifest.get("native_config", False),
                    legacy_windows,
                )
        save_json(
            state / "status.json", {"status": "published", "version": version, "checked_at": now()}
        )
        prune_cache(cache, keep_cache)
        logging.info("GeodomDesk %s published with signed update metadata", version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/geodomdesk-publisher/config.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = json.loads(Path(args.config).read_text())
    try:
        run(config)
    except BlockingIOError:
        logging.info("Another publisher run holds the lock")
    except Exception:
        logging.exception("Publication failed; the previous update release remains available")
        raise


if __name__ == "__main__":
    main()
