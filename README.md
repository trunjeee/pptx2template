# pptx2template

Turn an ordinary PowerPoint deck into a reusable multi-layout **.potx template**. It uses
plain rules over the OOXML structure, so there are no AI calls and no network access. It
runs offline and gives the same result every time on Windows, macOS and Linux.

```
pip install .
pptx2template deck.pptx --dry-run        # inspect what it inferred
pptx2template deck.pptx -o deck.potx     # build the template
```

## Example

[`examples/trj.pptx`](examples/trj.pptx) is a real design exported to PowerPoint, so its cards,
gradients and icons are pictures. Run `pptx2template examples/trj.pptx` (or open it in the app):
the flat pictures become layout decoration, the two photos become picture placeholders, and a
new slide made from any layout looks like the original slide with empty fields. The deck uses the
free [Onest](https://fonts.google.com/specimen/Onest) font. Without it, PowerPoint substitutes
another font and the text spacing looks off, in the original deck as well as in the template.
Embedding fonts (*File → Options → Save → Embed fonts*) before converting carries them into the
template.

## App (any OS)

```
pptx2template-ui                 # opens the interface in your browser
pptx2template-ui deck.pptx       # ...with a deck already loaded
```

The interface is a small local web app. A server on `127.0.0.1` serves one page that opens
in your default browser, so it looks and works the same on Windows, macOS and Linux. Nothing
leaves your computer, and the server refuses requests from other sites. In the app you can:

* drop a `.pptx` in and see every slide drawn as a schematic, with each shape outlined by
  what it will become (text placeholder, picture placeholder, layout decoration, slide content);
* click a shape to change its role, or force a text role (title, body…);
* see which slides were grouped into which layout, and rename layouts in place;
* change the grouping tolerance and build the `.potx` (the checks below run on every build).

When you open a file by path (command line, or dropping it onto the app icon on Windows), your
edits are saved automatically to `<deck>.overrides.yaml` next to it. For files opened from the
browser, use the **overrides.yaml** button to download them.

**Standalone builds, no Python needed:** `pyinstaller packaging/pptx2template.spec` builds
`pptx2template.exe` on Windows (no console window; it exits once the browser tab has been
closed for a minute or two, or when you press ⏻), `pptx2template.app` on macOS, and a single
binary on Linux. PyInstaller can only build for the OS it runs on, so the **build apps**
workflow (`.github/workflows/release.yml`) builds all three. Run it by hand from the Actions
tab, or push a `v*` tag to attach the builds to a release. Unsigned builds trigger a warning
on first launch: on Windows, SmartScreen ("More info → Run anyway"); on macOS, Gatekeeper
(right-click → Open).

## How it works

Each group of similar slides becomes one slide layout. Decoration ("chrome") is baked into
the layout, text becomes typed placeholders, and photos become picture placeholders with
the original shape, so a new photo is cropped the same way. Tables, charts and video stay on
the slides as content. The sample slides are kept and rewired to the new layouts
(`--no-slides` ships only the layouts).

> **Tip: rename your slides before you run the tool.** It's the one change that makes the
> biggest difference to the output. A custom slide name (`<p:cSld name>`, set from the
> Outline/Slide panel or with a VBA one-liner) becomes the layout name. Without one, layouts
> get generic names like `Layout-TitleImageText-02`.

## How it decides

The tool checks every top-level shape against these rules, and the first one that matches wins:

| # | Rule | Verdict |
|---|------|---------|
| – | table / chart / SmartArt frame | content, stays on slide |
| 1 | has real text (`<a:t>` with a letter or digit) | **placeholder**. Text wins over fill, so a coloured "Category" pill keeps its fill as the placeholder's own `spPr` |
| 2 | `<p:pic>` | **picture placeholder** with the picture's geometry (video/audio stays on the slide) |
| 3 | `<p:sp>` with `<a:blipFill>` (photo inside a shape) | **picture placeholder** with the shape's geometry (rounded rect, custom shape…). A photo you insert later gets the same crop |
| 2–3 | picture smaller than 2% of the slide | **chrome**: a logo or icon, not a photo |
| 2–3 | picture that is only a flat colour or a smooth gradient (no fine detail) | **chrome**: a card or background exported as an image, not a photo |
| 4 | solid / gradient / pattern / theme fill, no text | **chrome** |
| 5 | no fill, no text (spacers, a lone `·` glyph) | **chrome** |
| 6 | group without editable text (logo lockups) | **chrome**, moved as a whole |

A shape filled with a flat colour is still rule 4 (static chrome). Only picture fills become
placeholders. On the sample slides, a photo-filled shape is rewritten as an equivalent `p:pic`
bound to the placeholder, so it looks the same and "Change Picture" works.

Placeholder types: an existing title placeholder wins, otherwise the shape with the largest
font becomes `title` (ties go to the top-most). Everything else becomes `body` with
`idx` 1…n in reading order (top to bottom, left to right). Date, footer and slide-number
placeholders keep their type.

Two safety guards leave chrome on the slide instead of moving it into the layout. The report
shows each case:

* chrome drawn **over** a photo or picture placeholder (for example a gradient overlay). Layout shapes always
  render beneath slide shapes, so moving it would change the stacking order.
* chrome that is **animated** on the slide, since the slide's timeline refers to it.

**Grouping.** Each slide gets a signature: (shape kind, verdict, bounding box), plus a
fingerprint of the chrome's appearance and of what the slide inherits (master, layout
decoration, background). Two slides whose signatures pair up within `--tolerance` EMU
(default 50 000 ≈ 1.4 mm) share a layout. Union-find builds the groups. Each layout is built
from the fullest slide in its group.

**Naming.** An override wins first, then the first custom slide name in the group, then a
name built from the shape mix (`Layout-<Title|Image|Table|Chart|Text>-NN`).

## Formatting carries over to new slides

When someone adds a slide from a layout and types into an empty placeholder, PowerPoint
ignores the sample text's run formatting. It only reads the placeholder's
`<a:lstStyle><a:lvlNpPr><a:defRPr>`. So every generated placeholder gets a full `lstStyle`,
flattened from everything the original text inherited:

* text boxes: the presentation's default text style, the autoshape's `fontRef` colour and
  font, and the box's own formatting. Explicit `buNone`, zero indents, line spacing and top
  anchoring stop the master's body bullets from leaking in.
* existing placeholders: the old layout placeholder's style, or the whole master chain if
  the placeholder moved to another master or changed between title and body.

Decoration that slides inherited from their *old* layout or master is copied into the new
layout too, along with the background and colour mapping. That way nothing disappears when
the old layouts are dropped.

## Overrides

Run `--dry-run` first, then put corrections in a sidecar file named after the deck
(`deck.overrides.yaml`, `.yml` or `.json`). The tool picks it up automatically, or you can
pass it with `--overrides`:

```yaml
shapes:
  "Rectangle 13":      # matched by shape name
    force: chrome      # chrome | placeholder | media
  "Text 7":
    force: placeholder
    type: body         # title | body | subTitle | pic | obj | dt | ftr | sldNum ...
layouts:
  cluster_3:           # cluster id from the dry-run report
    name: "Destination"
```

## Verification

Every build runs these checks. If any fails, nothing is written:

1. **Well-formedness:** every XML part is parsed with `xml.dom.minidom` before zipping.
2. **Structural round-trip:** the result is reopened with `python-pptx` (as a
   presentation-typed copy, because python-pptx refuses `template.main` files). The check
   confirms that layout names and placeholder types/idx match the plan, and that every slide
   placeholder resolves to a placeholder on its layout.
3. **Text diff:** every paragraph of real text in the original deck must still be on its slide.

`--visual-check` also renders the original and the result with LibreOffice
(`soffice` + `pdftoppm` + Pillow) and reports slides whose pixels differ. If those tools
aren't installed, the check is skipped with a warning.

## Library use

```python
from pptx2template import analyze, convert, Overrides

print(analyze("deck.pptx").report())
result = convert("deck.pptx", overrides=Overrides.load("deck.overrides.yaml"))
assert not result.problems
open("deck.potx", "wb").write(result.data)
```

## Development

```
pip install -e .[test]
pytest
python tests/fixtures/build_fixtures.py   # regenerate the sample decks
```

Module map: `ui/` (web interface: `server.py` + `model.py` + `static/`), `parser.py` (unzip, shape extraction, inheritance), `classify.py` (rules table,
overrides), `cluster.py` (signatures, grouping, naming), `styles.py` (property inheritance
merging), `build.py` (layouts, master, slide rewrite), `package.py` (content-type flip,
well-formedness, rezip), `verify.py`, `report.py`, `cli.py`.

Known limit: if a deck was exported as flattened images (rounded corners baked into PNG
transparency, colour cards saved as pictures), the tool can only see rectangles. Those images
become rectangular picture placeholders. Use `force: chrome` in the overrides file for
pictures that are really decoration.

Not done yet: the theme part (`theme1.xml`) is carried over unchanged. Hard-coded colours
are not converted into theme colours.

License: MIT
