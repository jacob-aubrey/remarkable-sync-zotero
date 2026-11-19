"""Exercise native image-card rendering in a disposable profile, including retries.

Pass the usual test_zotero_plugin_live arguments. --source-pdf optionally checks
a retained problem PDF without opening or modifying its original library.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import test_zotero_plugin_live as harness


CAPTURE = """
          if (command.action === 'selected_sync_transport') {
            const original = ui.latest;
            ui.latest = {...original, config: {...original.config, tablet_root: 'synthetic-root'}};
            let error;
            try { await ui.action('sync_selected', {targets: [{cloud: 'synthetic'}], connection: ['stale', 'scope', 'fixture']}); }
            catch (value) { error = value.message; }
            finally { ui.latest = original; }
            if (!error?.startsWith('The connection changed.')) throw Error('The packaged selected-sync transport failed before scope validation: ' + error);
            response.transport = true;
          } else if (command.action === 'capture_images') {
            const paper = new Zotero.Item('journalArticle');
            paper.libraryID = Zotero.Libraries.userLibraryID;
            paper.setField('title', 'Image capture regression'); await paper.saveTx();
            const pdf = await Zotero.Attachments.importFromFile({file: Zotero.File.pathToFile(command.source), parentItemID: paper.id});
            pdf.setNote('Notable pilot: ' + 'c'.repeat(64)); await pdf.saveTx();
            await Zotero.PDFWorker.import(pdf.id, true);
            await Zotero.PDFWorker.renderAttachmentAnnotations(pdf.id, true);
            if (command.prepareMerge) {
              const highlights = pdf.getAnnotations().filter(a => a.annotationType === 'highlight')
                .sort((a, b) => a.annotationSortIndex.localeCompare(b.annotationSortIndex));
              highlights[0].addTag('kept-first'); await highlights[0].saveTx();
              highlights[1].addTag('kept-second'); await highlights[1].saveTx();
              const note = new Zotero.Item('note'); note.parentID = paper.id;
              note.setNote('<p>Keep this saved note unchanged. ' + highlights[1].key + '</p>'); await note.saveTx();
              ui.captureFixture = {oldKeys: highlights.map(a => a.key), noteID: note.id, text: note.getNote()};
            }
            response.capture = {key: pdf.key, cards: pdf.getAnnotations()
              .filter(a => a.annotationType === 'image')
              .map(a => ({key: a.key, position: JSON.parse(a.annotationPosition), path: Zotero.Annotations.getCacheImagePath(a)}))};
          } else if (command.action === 'capture_state') {
            const pdf = await Zotero.Items.getByLibraryAndKeyAsync(Zotero.Libraries.userLibraryID, command.key);
            await pdf.loadAllData();
            const annotations = pdf.getAnnotations().filter(a => a.annotationType === 'highlight');
            const {oldKeys, noteID, text} = ui.captureFixture;
            const yellow = annotations.find(a => a.hasTag('kept-first'));
            if (!yellow || !yellow.hasTag('kept-second') || annotations.length !== 2) throw Error('Grouped highlights lost tags or retained duplicate rows');
            if (Zotero.Items.get(noteID).getNote() !== text) throw Error('Grouping rewrote a saved note');
            const uri = Zotero.URI.getLibraryURI(pdf.libraryID) + '/items/' + oldKeys[1];
            const aliases = await Zotero.Relations.getByObject('item', uri);
            if (!aliases.some(row => row.predicate === 'dc:replaces' && row.subject.key === yellow.key)) throw Error('The old annotation link has no surviving alias');
            const reader = await Zotero.Reader.open(pdf.id, {annotationID: oldKeys[1]});
            await reader._initPromise;
            const deadline = Date.now() + 20000;
            while (!reader._internalReader._state.selectedAnnotationIDs.includes(yellow.key) && Date.now() < deadline) await Zotero.Promise.delay(100);
            if (!reader._internalReader._state.selectedAnnotationIDs.includes(yellow.key)) throw Error('The saved annotation link did not open the grouped passage');
            response.preserved = true;
          } else if (command.action === 'open') {
"""


def checks(source, client, root, checked):
    import pymupdf as fitz
    from PIL import Image

    assert harness.command(root, 'selected_sync_transport')['transport']
    checked('real_packaged_UI_and_helper_dispatch_selected_sync_without_full_sync_only_arguments')

    path = root / 'image-capture-source.pdf'
    if source:
        shutil.copyfile(source, path)
    else:
        with fitz.open() as pdf:
            for index in range(4):
                page = pdf.new_page(width=600, height=800)
                page.insert_text((300, 250), 'Source figure content')
                ink = page.add_ink_annot([[(70, 210), (90, 260), (160, 210), (70, 210)]])
                ink.set_colors(stroke=(0, 0, 0)); ink.set_border(width=3); ink.update()
                card = page.add_rect_annot(fitz.Rect(60, 200, 460, 290))
                card.set_border(width=0); card.set_info(content='Handwritten note')
                card.update(opacity=0)
                pdf.xref_set_key(card.xref, 'Zotero:Key', fitz.get_pdf_str('CARD234' + str(index + 2)))
                if index == 0:
                    page.insert_text((70, 350), ['First line of a selected passage', 'Second line of the same passage', 'A separate color passage'], fontsize=11)
                    for line, color in zip([350, 365.114, 380.228], [(1, .84, 0), (1, .84, 0), (1, .4, .7)]):
                        annot = page.add_highlight_annot(fitz.Rect(70, line - 12, 250, line + 4))
                        annot.set_colors(stroke=color); annot.update()
            pdf.save(path)
    result = harness.command(root, 'capture_images', source=str(path), prepareMerge=not source)['capture']
    assert result['cards'], result
    summaries = []
    original_images = {}
    for card in result['cards']:
        cache = Path(card['path'])
        assert cache.is_file(), card
        with Image.open(cache) as image:
            original_images[cache] = image.convert('RGB').tobytes()
            # The synthetic handwriting is left of the source figure text.
            gray = image.convert('L')
            left = gray.crop((0, 0, int(gray.width * .45), gray.height))
            dark = sum(count for value, count in enumerate(left.histogram()) if value < 100)
            summaries.append({'page': card['position']['pageIndex'], 'left_dark_pixels': dark})
            if not source:
                assert dark > 500, summaries
            Image.new('RGB', image.size, 'white').save(cache)
    (root / 'image-capture-report.json').write_text(json.dumps(summaries, indent=2), encoding='utf8')
    client.authorize()
    client.refresh_reader(result['key'])
    for cache, expected in original_images.items():
        with Image.open(cache) as image:
            assert image.convert('RGB').tobytes() == expected, 'A stale, valid PNG survived the refresh: ' + cache.name
    checked('native_image_cards_include_ink_on_every_page_and_survive_refresh')
    if not source:
        import hashlib
        from note_organizer.zotero_pilot import Ledger, upload_attachment
        updated = root / 'grouped-image-capture.pdf'
        with fitz.open(path) as pdf:
            page = pdf[0]
            marks = list(page.annots(types=[fitz.PDF_ANNOT_HIGHLIGHT]))
            quads = [fitz.Quad(mark.vertices) for mark in marks[:2]]
            for mark in marks[:2]:
                page.delete_annot(mark)
            merged = page.add_highlight_annot(quads)
            merged.set_colors(stroke=(1, .84, 0)); merged.update()
            pdf.save(updated)
        upload_attachment(client, Ledger(root / 'image-upload'), 'fixture', result['key'], updated,
                          client.file_md5(result['key']), hashlib.md5(updated.read_bytes()).hexdigest())
        client.refresh_reader(result['key'])
        assert harness.command(root, 'capture_state', key=result['key'])['preserved']
        checked('regrouping_preserves_annotation_keys_tags_saved_notes_and_old_source_links')


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--source-pdf', type=Path)
    parser.add_argument('--full-reader-checks', action='store_true')
    args, remaining = parser.parse_known_args()
    harness.BOOTSTRAP = harness.BOOTSTRAP.replace("          if (command.action === 'open') {", CAPTURE, 1)
    native_reader_checks = harness.reader_sync_checks
    def run_checks(client, root, checked):
        checks(args.source_pdf, client, root, checked)
        if args.full_reader_checks:
            native_reader_checks(client, root, checked)
    harness.reader_sync_checks = run_checks
    sys.argv = [sys.argv[0], *remaining, '--reader-sync-checks']
    harness.main()


if __name__ == '__main__':
    main()
