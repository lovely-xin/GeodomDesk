set +e
set +o pipefail
set +o posix
set -u
umask 077
for gd_tool in env base64 timeout mktemp sha256sum wc head cat; do command -v "$gd_tool" >/dev/null || exit 70; done
gd_b64_mode="raw"
if base64 -w 3072 </dev/null >/dev/null 2>&1; then
    gd_b64_mode="gnu"
elif base64 -b 3072 </dev/null >/dev/null 2>&1; then
    gd_b64_mode="bsd"
elif command -v fold >/dev/null 2>&1 && command -v tr >/dev/null 2>&1; then
    gd_b64_mode="fold"
fi
unset BASH_ENV ENV SHELLOPTS BASHOPTS
gd_dir=""
if [[ -d /dev/shm && -w /dev/shm ]]; then
    gd_dir=$(mktemp -d "/dev/shm/geodomdesk-cli-ssh.XXXXXXXX" 2>/dev/null || true)
fi
if [[ -z $gd_dir ]]; then
    gd_dir=$(mktemp -d "${TMPDIR:-/tmp}/geodomdesk-cli-ssh.XXXXXXXX") || exit 70
fi
trap 'rm -rf -- "$gd_dir"' EXIT
trap 'rm -rf -- "$gd_dir"; exit 143' HUP INT TERM
gd_emit_b64() {
    if (( BASH_VERSINFO[0] >= 4 )); then
        local -a gd_lines
        while mapfile -t -n 64 gd_lines && (( ${#gd_lines[@]} )); do
            local gd_line gd_off gd_len
            for gd_line in "${gd_lines[@]}"; do
                gd_off=0
                gd_len=${#gd_line}
                (( gd_len == 0 )) && continue
                while (( gd_off < gd_len )); do
                    printf '%s:%s:%s\n' "$gd_marker" "$gd_prefix" "${gd_line:gd_off:3072}"
                    ((gd_off += 3072))
                done
            done
        done
    else
        while IFS= read -r gd_line || [[ -n $gd_line ]]; do
            gd_off=0
            gd_len=${#gd_line}
            if (( gd_len == 0 )); then continue; fi
            while (( gd_off < gd_len )); do
                printf '%s:%s:%s\n' "$gd_marker" "$gd_prefix" "${gd_line:gd_off:3072}"
                ((gd_off += 3072))
            done
        done
    fi
}
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
    {
        {
            timeout -s TERM -k 2 "$gd_timeout" \
                sh -c 'bash --noprofile --norc "$1"; gd_code=$?; printf "%s" "$gd_code" > "$2"; exit "$gd_code"' gdexec "$gd_dir/script" "$gd_dir/status" \
                </dev/null 2>&1 1>&3 | { head -c 262145 > "$gd_dir/err"; cat > /dev/null; }
            exit "${PIPESTATUS[0]}"
        } 3>&1 | { head -c 262145 > "$gd_dir/out"; cat > /dev/null; }
        gd_exit=${PIPESTATUS[0]}
    }
    gd_error=0
    gd_out_sz=0
    gd_err_sz=0
    if [[ -s $gd_dir/out ]]; then gd_out_sz=$(wc -c < "$gd_dir/out"); fi
    if [[ -s $gd_dir/err ]]; then gd_err_sz=$(wc -c < "$gd_dir/err"); fi
    if (( gd_out_sz > 262144 || gd_err_sz > 262144 )); then gd_error=1; fi
    if [[ ! -f $gd_dir/status ]]; then gd_error=2; fi
    for gd_channel in out err; do
        [[ -s $gd_dir/$gd_channel ]] || continue
        if [[ $gd_channel == out ]]; then gd_prefix=O; gd_sz=$gd_out_sz; else gd_prefix=E; gd_sz=$gd_err_sz; fi
        if (( gd_sz <= 262144 )); then
            case "$gd_b64_mode" in
                gnu)
                    base64 -w 3072 < "$gd_dir/$gd_channel" | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 )); then exit 70; fi
                    ;;
                bsd)
                    base64 -b 3072 < "$gd_dir/$gd_channel" | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 )); then exit 70; fi
                    ;;
                fold)
                    base64 < "$gd_dir/$gd_channel" | tr -d '\r\n' | fold -w 3072 | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 || PIPESTATUS[2] != 0 || PIPESTATUS[3] != 0 )); then exit 70; fi
                    ;;
                *)
                    base64 < "$gd_dir/$gd_channel" | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 )); then exit 70; fi
                    ;;
            esac
        else
            case "$gd_b64_mode" in
                gnu)
                    head -c 262144 "$gd_dir/$gd_channel" | base64 -w 3072 | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 || PIPESTATUS[2] != 0 )); then exit 70; fi
                    ;;
                bsd)
                    head -c 262144 "$gd_dir/$gd_channel" | base64 -b 3072 | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 || PIPESTATUS[2] != 0 )); then exit 70; fi
                    ;;
                fold)
                    head -c 262144 "$gd_dir/$gd_channel" | base64 | tr -d '\r\n' | fold -w 3072 | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 || PIPESTATUS[2] != 0 || PIPESTATUS[3] != 0 || PIPESTATUS[4] != 0 )); then exit 70; fi
                    ;;
                *)
                    head -c 262144 "$gd_dir/$gd_channel" | base64 | gd_emit_b64
                    if (( PIPESTATUS[0] != 0 || PIPESTATUS[1] != 0 || PIPESTATUS[2] != 0 )); then exit 70; fi
                    ;;
            esac
        fi
    done
    : > "$gd_dir/out"
    : > "$gd_dir/err"
    rm -f -- "$gd_dir/script" "$gd_dir/status"
    printf '%s:END:%d:%d\n' "$gd_marker" "$gd_exit" "$gd_error"
done
