"""Installer packaging helpers, adapted from the verified Geodom wrapper builder."""

import base64, hashlib, json, os, re, shlex, shutil, subprocess, urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_server(config):
    for key in ["host", "relay", "api", "key"]:
        if not isinstance(config.get(key, ""), str):
            raise ValueError("Invalid " + key + ": expected a string")

    def endpoint(value, schemes):
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9\[][A-Za-z0-9.:/\[\]_-]{0,1023}", value
        ):
            return False
        parsed = urllib.parse.urlsplit(value if "://" in value else "//" + value)
        try:
            port = parsed.port
        except ValueError:
            return False
        return (
            bool(parsed.hostname)
            and (not parsed.scheme or parsed.scheme in schemes)
            and not parsed.username
            and not parsed.password
            and (port is None or 0 < port < 65536)
        )

    if not endpoint(config["host"], ("ws", "wss")):
        raise ValueError("Invalid ID server")
    if config.get("relay") and not endpoint(config["relay"], ("ws", "wss")):
        raise ValueError("Invalid relay server")
    if config.get("api") and (
        not config["api"].startswith(("https://", "http://"))
        or not endpoint(config["api"], ("http", "https"))
    ):
        raise ValueError("Invalid API server")
    if len(base64.b64decode(config["key"], validate=True)) != 32:
        raise ValueError("Invalid public key")
    return {k: config.get(k, "") for k in ["host", "relay", "api", "key"]}


def export_config(config):
    return base64.urlsafe_b64encode(
        json.dumps(validate_server(config), separators=(",", ":")).encode()
    ).decode()[::-1]


def permanent_password(config):
    password = config.get("permanent_password", "")
    if not isinstance(password, str) or (
        password and (not 6 <= len(password) <= 128 or any(c in password for c in "\r\n\0"))
    ):
        raise ValueError("Invalid permanent password: use 6 to 128 characters without line breaks")
    return password


def websocket_port(config):
    value = config.get("websocket_port", "")
    if value == "":
        return ""
    if (
        isinstance(value, bool)
        or not isinstance(value, (str, int))
        or not re.fullmatch(r"[0-9]{1,5}", str(value))
        or not 0 < int(value) < 65536
    ):
        raise ValueError("Invalid WebSocket port")
    return str(int(value))


def render_template(name, values):
    text = (ROOT / name).read_text(encoding="utf-8-sig")

    def replace(match):
        key = match.group(1)
        if key not in values:
            raise ValueError("Unfilled template value in " + name)
        return str(values[key])

    return re.sub(r"@@([A-Z_]+)@@", replace, text)


def run_tool(args, timeout):
    try:
        return subprocess.run(args, check=True, timeout=timeout, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "Build tool failed: "
            + str(args[0])
            + "\n"
            + (e.stdout + e.stderr).decode(errors="replace")[-6000:]
        ) from e


def make_linux(payload, asset, kind, arch, config, version, destination):
    deb_arch = {"x86_64": "amd64", "aarch64": "arm64"}
    header = render_template(
        "linux-installer.sh.in",
        {
            "VERSION": version,
            "ARCH": deb_arch[arch] if kind == "deb" else arch,
            "KIND": kind,
            "DIGEST": asset["digest"][7:],
            "HOST": config["host"],
            "RELAY": config.get("relay", ""),
            "API": config.get("api", ""),
            "KEY": config["key"],
            "CONFIG": export_config(config),
            "WS_PORT": websocket_port(config),
            "PASSWORD_SH": shlex.quote(permanent_password(config)),
        },
    ).encode()
    # Keep the header length fixed while avoiding shell octal interpretation.
    header = header.replace(
        b"PAYLOAD_OFFSET=0000000000", b"PAYLOAD_OFFSET=" + str(len(header)).encode().ljust(10, b" ")
    )
    with Path(destination).open("wb") as out, payload.open("rb") as src:
        out.write(header)
        shutil.copyfileobj(src, out, 1024 * 1024)
    os.chmod(destination, 0o755)
    run_tool(["sh", str(destination), "--check"], 60)
    return destination


def make_windows(payload, asset, config, version, stage, license_file, destination):
    script = stage / "install-windows.ps1"
    password = permanent_password(config)
    script.write_text(
        render_template(
            "install-windows.ps1.in",
            {
                "CONFIG": export_config(config),
                "HOST": config["host"],
                "RELAY": config.get("relay", ""),
                "API": config.get("api", ""),
                "KEY": config["key"],
                "DIGEST": asset["digest"][7:],
                "VERSION": version,
                "WS_PORT": websocket_port(config),
                "PASSWORD_PS": password.replace("'", "''"),
            },
        ),
        encoding="utf-8-sig",
    )
    nsi = stage / "installer.nsi"
    nsi.write_text(
        render_template(
            "windows-installer.nsi.in",
            {
                "VERSION": version,
                "HOST": config["host"],
                "OUTPUT": destination,
                "LICENSE": license_file,
                "PAYLOAD": payload,
                "SCRIPT": script,
                "ICON": ROOT / "assets/geodomdesk.ico",
                "PASSWORD_NOTICE": (
                    "全新安装时设置预置永久密码；已有安装保留原密码和验证模式。"
                    if password
                    else "安装器不预设永久访问密码。"
                ),
            },
        ),
        encoding="utf-8-sig",
    )
    run_tool(["makensis", "-V2", str(nsi)], 300)
    extracted = stage / "exe-verification"
    run_tool(["7z", "x", "-y", "-o" + str(extracted), str(destination)], 90)
    found = list(extracted.rglob("geodomdesk-client.exe"))
    if len(found) != 1 or sha(found[0]) != asset["digest"][7:]:
        raise ValueError("Embedded Windows payload failed validation")
    ps = list(extracted.rglob("install.ps1"))
    if len(ps) != 1 or sha(ps[0]) != sha(script):
        raise ValueError("Embedded configuration script mismatch")
    shutil.rmtree(extracted)
    return destination
