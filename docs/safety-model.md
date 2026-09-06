# Safety model

Franklin & McGrath treats model output, documents, imported APEX files, result artifacts, and
requested output paths as **untrusted**. The compiler, deterministic graders, and typed
tool engine are trusted only after their source and fixture validation gates pass.

Agents get typed, scoped tools rather than a shell, arbitrary filesystem writes, direct
posting, payment, or filing access. Every requested tool call—successful or rejected—is
an append-only Action and consumes the applicable episode budget. Client and engagement
ownership is enforced at compilation and runtime, including reference and event paths.

Material actions use an explicit approval boundary. A proposal is not execution; an
approved message is bound to its full immutable draft state and auto-executes only once
through an audited system Action. `finish_episode` is atomic: a failed snapshot cannot
leave a false terminal state.

The harness and reporting paths use canonical, secret-sanitised artifacts and no-follow
filesystem checks. These controls detect common traversal, symlink, replacement, and
TOCTOU attacks, but they are not a guarantee against an adversary who controls every
local trust anchor or the host operating system. Containerised document parsing is
recommended where the documented `SANDBOX=podman` mode is available.

Critical failures provide a fail-closed scoring layer; they do not make a model safe for
production work. For supported versions, disclosure guidance, and the full limitation
statement, read [SECURITY.md](../SECURITY.md).
