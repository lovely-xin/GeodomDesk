gd_marker=__MARKER__
printf '\n%s:READY\n' "$gd_marker"
gd_data=()
for ((gd_index=1; gd_index<=__LINES__; gd_index++)); do
    IFS= read -r gd_line || exit 1
    gd_data+=("$gd_line")
    if ((gd_index % __BATCH__ == 0)); then
        printf '\n%s:ACK\n' "$gd_marker"
    fi
done
gd_payload=$(printf '%s' "${gd_data[@]}")
gd_hash=$(printf '%s' "$gd_payload" | sha256sum)
if [[ ${gd_hash%% *} != __DIGEST__ ]]; then
    printf '\n%s:ERROR:Legacy terminal script checksum mismatch\n' "$gd_marker"
    exit 1
fi
gd_script=$(printf '%s' "$gd_payload" | base64 -d && printf '.') || exit 1
gd_script=${gd_script%.}
printf '\n%s:START\n' "$gd_marker"
bash --noprofile --norc -c "$gd_script" </dev/null 2>&1 | base64 -w 64 |
    while IFS= read -r gd_line; do
        printf '%s:DATA:%s\n' "$gd_marker" "$gd_line"
    done
gd_status=("${PIPESTATUS[@]}")
if ((gd_status[1] != 0 || gd_status[2] != 0)); then
    printf '\n%s:ERROR:Cannot encode legacy command output\n' "$gd_marker"
    exit 1
fi
printf '\n%s:END:%d\n' "$gd_marker" "${gd_status[0]}"
