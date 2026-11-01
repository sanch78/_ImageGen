---
name: codex-imagegen
description: Generate raster images (website hero art, section illustrations, textures, icons, mood images) with Codex's built-in image generation, as the invoking user with that user's Codex login, and save each one to an explicit path. Use from Claude Code or scripts through the bundled wrapper, or from a Codex session through the native image tool. Not for vector, code-native or text-heavy graphics.
---

# Codex image generation

Generates one PNG per request and writes it only to the path the caller
names. The image comes from Codex's built-in `image_gen` tool and runs on the
invoking OS user's Codex login and quota. Paths below are relative to this
skill's directory. Open the skill at its real library path, because its
relative paths resolve only there.

## Before generating

- Pick the output path inside the caller's own assigned roots: drafts and
  previews in its tmp tree, and approved assets wherever its project places
  them. The parent directory must already exist. An existing file is never
  replaced unless `--force` is passed, and `--force` needs the user's
  explicit request to replace that file.
- Each image consumes the user's Codex quota. Generate only what the task
  needs, and iterate by changing one thing at a time.
- Do not use this for icons or logos that must match an existing SVG set,
  for diagrams, or for copy-heavy images. Build those in code. Keep page
  text in HTML rather than baking it into the image.

## From Claude Code or a script

```bash
python3 scripts/codex_imagegen.py \
  --prompt-file /caller/tmp/hero.prompt.txt \
  --aspect landscape \
  --staging-root /caller/tmp \
  --out /caller/tmp/drafts/hero.png
```

- `--prompt TEXT` or `--prompt-file FILE`: the request (see below).
- `--aspect square|landscape|portrait`: square is 1:1, landscape and
  portrait are about 3:2 and 2:3. The wrapper checks the result.
- `--transparent`: asks for a transparent background and fails if the PNG
  has no alpha channel.
- `--ref IMAGE` (repeatable): reference images attached to the request,
  used for style, composition or a subject to vary.
- `--staging-root DIR`: an existing directory in the caller's tmp root. The
  Codex run gets a fresh empty subdirectory there as its read-only working
  root, and the subdirectory is removed afterwards.
- `--force`, `--no-sidecar`, `--model`, `--timeout SECONDS` (default 600).

A run usually takes one to three minutes, so give the shell call a timeout
of up to 10 minutes. On success the wrapper prints `PATH (WxH)` and writes
`PATH.json` with the prompt, options, size, alpha flag, UTC timestamp and
Codex version.

| Exit | Meaning |
| --- | --- |
| 0 | Image written |
| 2 | Bad arguments, existing output, or missing input |
| 3 | Codex login missing or expired: the user runs `codex login` |
| 4 | Codex refused or produced no image (policy refusal, for example) |
| 5 | Codex usage or rate limit |
| 1 | Anything else, including a wrong aspect or missing alpha |

On any non-zero exit nothing is written to `--out`. Report the message to
the user as it is. Do not retry a refusal with a reworded prompt that
evades it.

## From a Codex session

Use the native tool directly; do not start a nested `codex exec`.

1. Call `image_gen` once per requested image, with the prompt built as
   below. For a transparent background, ask the tool for real transparency.
   To vary a local image, load it with `view_image` first.
2. Place the result, which also validates it:

   ```bash
   python3 scripts/codex_imagegen.py --from-thread \
     --aspect landscape --note "<the prompt you used>" \
     --out /caller/tmp/drafts/hero.png
   ```

   `--from-thread` takes the newest PNG that this thread saved under
   `$CODEX_HOME/generated_images/<thread id>/`. The output directory must be
   writable in the session's sandbox; if it is not, ask the user instead of
   widening the sandbox.

## Writing the prompt

Use short labeled lines and keep what the user asked for. Add detail only
where it materially helps:

```text
Asset type: landing page hero background
Subject: <main subject, or "abstract" for backgrounds>
Style/medium: <photo / flat illustration / 3D render / texture>
Composition: <framing; where the empty space for page copy goes>
Lighting/mood: <soft daylight, low contrast, ...>
Color palette: <the page's palette as color names or hex values>
Constraints: no text, no logos, no watermark
```

For website assets:

- **Heroes and section art:** state where copy will sit and leave calm
  negative space there. Match the page's existing palette and image grade.
  Prefer an art-directed photo, product visual or one strong scene over
  generic decoration.
- **Textures and backgrounds:** use low-chroma, palette-matched grades,
  paper or grain, or subtle grids. For tiling textures, ask for an even
  pattern without a focal point or vignette.
- **Icons and spot illustrations:** ask for a centered subject with
  generous padding, a clear silhouette and one consistent style across the
  set. Use `--transparent` when the asset sits on varying backgrounds.
- **Sets:** repeat the same style, palette and lighting lines in every
  prompt, and change only the subject.
- **Avoid by default:** purple-to-blue "AI" gradients, neon glows, floating
  orbs and blobs, fake UI or dashboards, stock-photo poses, and any text
  inside the image.

Inspect each result before using it: subject, composition, palette and any
stray text. For transparent images, check the edges and look for holes cut
into the subject on a mid-gray background.
