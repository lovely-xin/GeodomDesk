set +e
set +o pipefail
set +o posix
set -u
umask 077
for gd_tool in env base64 timeout mktemp sha256sum wc head; do command -v "$gd_tool" >/dev/null || exit 70; done
gd_dir=$(mktemp -d "${TMPDIR:-/tmp}/geodomdesk-cli-ssh.XXXXXXXX") || exit 70
trap 'rm -rf -- "$gd_dir"' EXIT
printf '%s:READY\n' '__SESSION__'
while IFS=' ' read -r gd_op gd_marker gd_timeout gd_digest; do
    [[ $gd_op == RUN && $gd_marker =~ ^[a-f0-9]{32}$ && $gd_timeout =~ ^[0-9]{1,3}$ && $gd_digest =~ ^[a-f0-9]{64}$ ]] || exit 70
    ((gd_timeout >= 1 && gd_timeout <= 300)) || exit 70
    IFS= read -r gd_payload || exit 70
    ((${#gd_payload} <= 87384)) || exit 70
    printf '%s' "$gd_payload" | base64 -d > "$gd_dir/script" || exit 70
    gd_hash=$(sha256sum "$gd_dir/script")
    [[ ${gd_hash%% *} == "$gd_digest" ]] || exit 70
    printf '%s:START\n' "$gd_marker"
    (ulimit -f 257 || exit 70; exec env -u BASH_ENV -u ENV -u SHELLOPTS -u BASHOPTS timeout --signal=TERM --kill-after=2 "$gd_timeout" bash --noprofile --norc -c 'bash --noprofile --norc "$1"; gd_code=$?; printf "%s" "$gd_code" > "$2"; exit "$gd_code"' gdexec "$gd_dir/script" "$gd_dir/status" </dev/null) > "$gd_dir/out" 2> "$gd_dir/err"
    gd_exit=$?
    gd_error=0
    if (( $(wc -c < "$gd_dir/out") > 262144 || $(wc -c < "$gd_dir/err") > 262144 )); then gd_error=1; fi
    if [[ ! -f $gd_dir/status ]]; then gd_error=2; fi
    for gd_channel in out err; do
        if [[ $gd_channel == out ]]; then gd_prefix=O; else gd_prefix=E; fi
        head -c 262144 "$gd_dir/$gd_channel" | base64 -w 3072 |
            while IFS= read -r gd_line; do printf '%s:%s:%s\n' "$gd_marker" "$gd_prefix" "$gd_line"; done
    done
    rm -f -- "$gd_dir/script" "$gd_dir/out" "$gd_dir/err" "$gd_dir/status"
    printf '%s:END:%d:%d\n' "$gd_marker" "$gd_exit" "$gd_error"
done
