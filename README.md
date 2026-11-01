# ImageGen

Shared skill for generating raster images (website hero art, section
illustrations, textures, icons) with the Codex CLI's built-in image
generation. It works from Claude Code, scripts and Codex. Every run uses the
invoking OS user, that user's Codex login and quota, and an output path the
caller chooses inside its own assigned roots.

## Skills

| Skill | Purpose |
| --- | --- |
| [`codex-imagegen`](.agents/skills/codex-imagegen/SKILL.md) | Generate one PNG per request and save it to an explicit path |

## Requirements

- The `codex` CLI on `PATH`, with the `image_generation` feature enabled
  (`codex features list`).
- A Codex login for the invoking user in the effective Codex home:
  `$CODEX_HOME` when it is set, otherwise `~/.codex`. `codex login status`
  does not prove that the token still works. A revoked token shows up only
  as a failed run, which the wrapper reports with exit code 3.
- Python 3.12 or later, standard library only.

## Invocation

From Claude Code or a script, run the wrapper at its library path:

```bash
python3 /space/share/libs/_ImageGen/.agents/skills/codex-imagegen/scripts/codex_imagegen.py \
  --prompt "Minimal hero background, low-chroma sand to slate grade, no text" \
  --aspect landscape \
  --staging-root /caller/tmp/root \
  --out /caller/tmp/root/drafts/hero.png
```

The wrapper starts one headless run:

```bash
codex exec --json --ephemeral --skip-git-repo-check -s read-only -C <staging>
```

The run gets no extra permissions. The built-in tool generates the image
server-side, and Codex saves it, so a read-only sandbox is enough.

Inside a Codex session, the skill tells the agent to call the native
`image_gen` tool and then place the result with `--from-thread`, without
starting a nested `codex exec`. See the [skill](.agents/skills/codex-imagegen/SKILL.md)
for all options, exit codes and prompt guidance.

## Output and staging

- The wrapper writes only `--out` and its `--out.json` sidecar. The sidecar
  holds the prompt, options, size, alpha flag, UTC timestamp and Codex
  version, and `--no-sidecar` skips it. Both files are written atomically.
  An existing file is replaced only with `--force`. After a failed run,
  `--out` is left untouched.
- `--out` must end in `.png`, because Codex returns PNG. The parent
  directory must already exist.
- The Codex run's working root is a fresh, empty directory under
  `--staging-root`, which should be the caller's tmp root. It is removed
  after every run and never contains the caller's source tree.
- Observed sizes are 1254×1254 for square and 1536×1024 for landscape
  (portrait is the mirror). The wrapper checks orientation, and checks for an
  alpha channel when `--transparent` is passed.

## Quota and privacy

- Each image uses the invoking user's Codex quota. A run takes about one to
  three minutes.
- Prompts and reference images go to the Codex service under that user's
  account. Do not put secrets or personal data into prompts or references.
- Codex keeps its own copy of every generated image under
  `$CODEX_HOME/generated_images/<thread id>/`, even with `--ephemeral`. The
  wrapper leaves Codex state alone. That user can delete old thread
  directories there when they are no longer needed.
- The wrapper never reads Codex credentials. It relies only on Codex's own
  exit status and events.

## Linking into a consumer project

A consumer project links the skill into both discovery directories, using
absolute paths, and commits the links:

```bash
cd /absolute/consumer/project/root
mkdir -p .agents/skills .claude/skills
ln -s /space/share/libs/_ImageGen/.agents/skills/codex-imagegen .agents/skills/codex-imagegen
ln -s /space/share/libs/_ImageGen/.agents/skills/codex-imagegen .claude/skills/codex-imagegen
git add .agents/skills/codex-imagegen .claude/skills/codex-imagegen
```

The library checkout stays read-only for consumers. Project-specific facts,
such as which tmp root to use as staging or where approved assets go, belong
in the consumer's own documentation.

## Tests

Offline tests cover argument handling, event parsing (using recorded,
credential-free `codex exec --json` samples) and the full wrapper path
against a stub `codex`. They use no network and no quota:

```bash
TMPDIR=/caller/tmp/root python3 -m unittest discover -s tests
```

## Known limits

- The built-in tool takes no size, quality or format arguments. The shape is
  requested in the prompt and verified afterwards; exact pixel sizes are not
  controllable.
- `codex exec --json` reports no image event. The wrapper finds the file
  through the run's `thread.started` id.
- Transparent output can have defects, such as holes cut into the subject.
  Check alpha images on a mid-gray background before using them.
