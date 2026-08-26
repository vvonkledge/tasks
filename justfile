# Where `just install` puts the `tasks` command.
bindir := env('XDG_BIN_HOME', env('HOME') / '.local/bin')

[private]
default:
    @just --list

# Put `tasks` on your PATH
install:
    #!/usr/bin/env bash
    set -euo pipefail
    mkdir -p '{{bindir}}'
    chmod +x tasks.py
    ln -sfn "$PWD/tasks.py" '{{bindir}}/tasks'
    echo "linked {{bindir}}/tasks -> $PWD/tasks.py"
    case ":$PATH:" in
        *":{{bindir}}:"*) ;;
        *) echo "{{bindir}} is not on your PATH. Add to your shell profile:"
           echo '  export PATH="{{bindir}}:$PATH"'
           exit 0 ;;
    esac
    # Unlike datafile, linking is not the whole install. tasks.py locates its
    # storage engine relative to the path it was invoked as, and abspath() does
    # not resolve symlinks, so from {{bindir}} both the "next to tasks.py" and
    # the "../datafile" candidates now point at {{bindir}}. A sibling checkout
    # that works via `uv run tasks.py` stops being found the moment you link.
    # The bare home view is read-only and exits 1 when the engine is missing.
    if tasks >/dev/null; then
        tasks --version
    else
        echo
        echo "linked, but datafile.py is not reachable from {{bindir}}." >&2
        echo "Pick one:" >&2
        echo "  (cd ../datafile && just install)   # put datafile on PATH too" >&2
        echo "  export TASKS_DATAFILE=\"$(cd .. && pwd)/datafile/datafile.py\"" >&2
        exit 1
    fi

# Remove the `tasks` command
uninstall:
    #!/usr/bin/env bash
    set -euo pipefail
    link='{{bindir}}/tasks'
    if [ ! -L "$link" ] && [ ! -e "$link" ]; then
        echo "not installed: $link"
    elif [ "$(readlink "$link" || true)" = "$PWD/tasks.py" ]; then
        rm "$link"
        echo "removed $link"
    else
        echo "refusing to remove $link: not a link to $PWD/tasks.py" >&2
        exit 1
    fi
