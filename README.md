# gh_identity

Single-file, stdlib-only Python interface for portable GitHub identity and operations.

- **No third-party Python runtime dependency**
- Prefers authenticated **`gh`** when available
- Falls back to stdlib **`urllib`** for supported REST operations, including anonymous public reads
- Keeps repository / PR / SHA / review / comment / workflow / run identity explicit
- Bounded, digest-first output for long GitHub threads
- Guarded writes with dry-run/verification and mutation-uncertainty handling
- Designed to absorb the proven GitHub-operation patterns currently spread across browser-test-kit, Ironmate, Flutter, mcp-toolcall-lab and Aoi

## Quick start

```bash
python gh_identity.py capabilities
python gh_identity.py repo myon-bioinformatics/gh_identity
python gh_identity.py comments OWNER/REPO 9
python gh_identity.py variable-get OWNER/REPO NAME

# Explicit write
python gh_identity.py variable-set OWNER/REPO NAME VALUE --write
```

Use `--transport gh` or `--transport urllib` to force a transport; default is `auto`.

See [SPEC.md](SPEC.md) for the v0.1 contract.
