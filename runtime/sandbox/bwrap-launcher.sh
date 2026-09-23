#!/bin/bash
# Fixed companion to launcher.mjs for SRT 0.0.77's generated invocation.
# denyRead('/') expands into writable tmpfs masks of the root's children.
# Remount surviving masks read-only AFTER child binds. This is non-recursive:
# authorized writable child mounts stay writable. Vendor SRT remains unchanged.
set -eu
args=("$@")
count=${#args[@]}
if (( count < 4 )) || [[ "${args[count-4]}" != '--' || "${args[count-2]}" != '-c' ]]; then
  printf '%s\n' 'Unsupported SRT bubblewrap invocation' >&2
  exit 125
fi
mount_paths=()
mount_kinds=()
remember_mount() {
  local i
  for ((i=0; i<${#mount_paths[@]}; i++)); do
    if [[ "${mount_paths[i]}" == "$1" ]]; then
      mount_kinds[i]=$2
      return
    fi
  done
  mount_paths+=("$1")
  mount_kinds+=("$2")
}
scan_mounts() {
  local options=("$@") i=0 count=$# option fd token
  local profile=()
  while (( i < count )); do
    option=${options[i]}
    case "$option" in
      --args)
        (( i + 1 < count )) || return 125
        fd=${options[i+1]}
        [[ "$fd" =~ ^[0-9]+$ ]] || return 125
        profile=()
        # Reopening creates an independent file offset; bwrap still receives
        # the original descriptor and the original unmodified argument list.
        while IFS= read -r -d '' token; do profile+=("$token"); done < "/proc/self/fd/$fd" || return 125
        scan_mounts "${profile[@]}" || return 125
        i=$((i+2));;
      --tmpfs|--dev|--proc)
        (( i + 1 < count )) || return 125
        remember_mount "${options[i+1]}" "$option"
        i=$((i+2));;
      --bind|--ro-bind|--dev-bind)
        (( i + 2 < count )) || return 125
        remember_mount "${options[i+2]}" "$option"
        i=$((i+3));;
      --setenv) (( i + 2 < count )) || return 125; i=$((i+3));;
      --unsetenv|--cap-add|--cap-drop)
        (( i + 1 < count )) || return 125; i=$((i+2));;
      --new-session|--die-with-parent|--unshare-net|--unshare-pid|--unshare-user)
        i=$((i+1));;
      *) printf 'Unsupported bubblewrap option: %s\n' "$option" >&2; return 125;;
    esac
  done
}
scan_mounts "${args[@]:0:count-4}"
remounts=(--remount-ro /)
for ((i=0; i<${#mount_paths[@]}; i++)); do
  if [[ "${mount_kinds[i]}" == '--tmpfs' ]]; then
    remounts+=(--remount-ro "${mount_paths[i]}")
  fi
done
exec /usr/bin/bwrap "${args[@]:0:count-4}" "${remounts[@]}" "${args[@]:count-4}"
