import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:plezy/media/media_item.dart';
import 'package:plezy/media/media_kind.dart';
import 'package:plezy/media/shared_episode_file.dart';
import 'package:plezy/media/media_part.dart';
import 'package:plezy/media/media_version.dart';
import 'package:plezy/media/media_source_info.dart';
import 'package:plezy/services/episode_file_boundaries.dart';
import 'package:plezy/sources/adapters/local_source.dart';
import 'package:plezy/sources/local_file_relink.dart';
import 'package:plezy/sources/cue_album.dart';
import 'package:plezy/sources/source_models.dart';
import 'package:plezy/sources/source_transport.dart';
import 'package:plezy/providers/playback_state_provider.dart';
import 'package:plezy/models/plex/play_queue_response.dart';

MediaVersion version({String path = '/TV/S01E01-E03.mkv', int size = 1024}) => MediaVersion(
  id: 'version',
  parts: [MediaPart(id: '1', file: path, sizeBytes: size, durationMs: 4200000)],
);
PlexMediaItem episode(int n, {MediaVersion? media}) => PlexMediaItem(
  id: 'episode-$n',
  serverId: 'server',
  parentId: 'season',
  parentIndex: 1,
  kind: MediaKind.episode,
  index: n,
  playQueueItemId: n,
  mediaVersions: [media ?? version()],
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() => SharedPreferences.setMockInitialValues({}));
  test('unequal triple episode boundaries preserve each clock and refuse estimates', () async {
    final item = episode(2);
    final bounds = EpisodeFileBounds.identify('p', item, item.mediaVersions!.single, 1, [])!;
    expect(bounds.slice(2, await bounds.starts([])), isNull);
    final chapters = [
      MediaChapter(id: 1, title: 'Episode 2', startTimeOffset: 600000),
      MediaChapter(id: 2, title: 'S01E03: Last story', startTimeOffset: 2700000),
    ];
    final starts = await bounds.starts(chapters);
    expect(bounds.slice(1, starts), (start: 0, end: 600000));
    expect(bounds.slice(2, starts), (start: 600000, end: 2700000));
    expect(bounds.slice(3, starts), (start: 2700000, end: 4200000));
    await bounds.mark(2, 650000, chapters);
    expect((await bounds.starts(chapters))[2], 650000);
    await expectLater(bounds.mark(3, 500000, chapters), throwsFormatException);
    final same = EpisodeFileBounds.identify('p', episode(1), version(), 1, [])!;
    expect((await same.starts(chapters))[2], 650000);
    final otherCut = EpisodeFileBounds.identify('p', item, version(size: 2048), 1, [])!;
    expect(await otherCut.starts([]), {1: 0});
    final otherProfile = EpisodeFileBounds.identify('other', item, version(), 1, [])!;
    expect(await otherProfile.starts([]), {1: 0});
  });
  test('corrupt maps, ambiguous chapters and mislabeled seasons do not fabricate a cut', () async {
    final b = EpisodeFileBounds.identify('p', episode(2), version(), 1, [])!;
    await (await SharedPreferences.getInstance()).setString(b.key, '{bad');
    expect(await b.starts([]), {1: 0});
    expect(
      await b.starts([
        MediaChapter(id: 1, title: 'Episode 2', startTimeOffset: 1000),
        MediaChapter(id: 2, title: 'Episode 2', startTimeOffset: 2000),
      ]),
      {1: 0},
    );
    expect(EpisodeFileBounds.identify('p', episode(2), version(path: '/TV/S02E01-E03.mkv'), 1, []), isNull);
    final repeated = EpisodeFileBounds.identify('p', episode(2), version(path: '/TV/S01E01E02E03.mkv'), 1, [])!;
    expect(repeated.last, 3);
  });
  test('episode filenames with audio and release numbers do not create multi-episode cuts', () {
    for (final path in ['/TV/Show S01E01 10bit.mkv', '/TV/Show S01E01 2.0.mkv', '/TV/Show S01E01 - 10bit.mkv']) {
      expect(EpisodeFileBounds.identify('p', episode(1), version(path: path), 1, []), isNull, reason: path);
    }
    expect(EpisodeFileBounds.identify('p', episode(1), version(path: '/TV/Show S01E01-03.mkv'), 1, [])?.last, 3);
  });
  test('chapter labels from another season do not set episode start times', () async {
    final bounds = EpisodeFileBounds.identify('p', episode(2), version(), 1, [])!;
    final starts = await bounds.starts([
      MediaChapter(id: 1, title: 'S02E02: Recap', startTimeOffset: 2000),
      MediaChapter(id: 2, title: 'S01E02: The episode', startTimeOffset: 600000),
      MediaChapter(id: 3, title: 'S03E03', startTimeOffset: 2000000),
    ]);
    expect(starts, {1: 0, 2: 600000});
  });
  test('overlapping manual marks for the same file preserve both edits and validate their order', () async {
    final first = EpisodeFileBounds.identify('p', episode(2), version(), 1, [])!;
    final second = EpisodeFileBounds.identify('p', episode(3), version(), 1, [])!;
    await Future.wait([first.mark(2, 600000, []), second.mark(3, 1200000, [])]);
    expect(await first.starts([]), {1: 0, 2: 600000, 3: 1200000});
    final edit = first.mark(2, 900000, []);
    await expectLater(second.mark(3, 800000, []), throwsFormatException);
    await edit;
    expect(await first.starts([]), {1: 0, 2: 900000, 3: 1200000});
  });

  test('queue visits bounded shared-file siblings in both directions', () async {
    final q = PlaybackStateProvider();
    addTearDown(q.dispose);
    final items = [for (var n = 1; n <= 3; n++) episode(n), episode(4, media: version(path: '/TV/S01E04.mkv'))];
    await q.setPlaybackFromPlayQueue(
      PlayQueueResponse(
        playQueueShuffled: false,
        playQueueID: 1,
        playQueueSelectedItemID: 1,
        playQueueTotalCount: 4,
        items: items,
      ),
      null,
    );
    expect((await q.getNextEpisode(items[0].globalKey)).item?.id, 'episode-4');
    expect((await q.getNextEpisode(items[0].globalKey, boundedEpisodes: {1, 2, 3})).item?.id, 'episode-2');
    expect((await q.getPreviousEpisode(items[2].globalKey, boundedEpisodes: {1, 2, 3})).item?.id, 'episode-2');
  });
  test('CUE sheets create searchable ordered track slices sharing one download', () async {
    final entries = await cueAlbumEntries(
      bytes: utf8.encode('''PERFORMER "Artist"
TITLE "Live album"
FILE "Concert.flac" WAVE
  TRACK 01 AUDIO
    TITLE "Opening"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Encore"
    INDEX 00 01:59:00
    INDEX 01 02:00:37
'''),
      source: 'local',
      parent: '',
      sheet: 'Concert.cue',
      lookup: (name) async => PublicationFile(id: name, name: name, mimeType: '', size: 1024, revision: 'physical'),
    );
    expect(entries.map((e) => e.title), ['Live album', 'Opening', 'Encore']);
    final opening = entries[1].files.single, encore = entries[2].files.single;
    expect(opening.cueEnd, 9037);
    expect(encore.cueStart, 9037);
    expect(opening.fingerprint, encore.fingerprint);
    expect(cuePlaybackAddress(opening, '/NAS/été,live.flac'), startsWith('edl://%20%'));
    expect(PublicationFile.fromJson(encore.toJson()).cueStart, 9037);
  });
  test('CUE sheets reject traversal, bad frame times, reversed and duplicate tracks', () async {
    for (final body in [
      'FILE "../bad.flac" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00',
      'FILE "bad.flac" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:75',
      'FILE "bad.flac" WAVE\nTRACK 01 AUDIO\nINDEX 01 02:00:00\nTRACK 02 AUDIO\nINDEX 01 01:00:00',
      'FILE "bad.flac" WAVE\nTRACK 01 AUDIO\nINDEX 01 00:00:00\nTRACK 01 AUDIO\nINDEX 01 01:00:00',
    ]) {
      expect(
        await cueAlbumEntries(
          bytes: utf8.encode(body),
          source: 's',
          parent: '',
          sheet: 'a.cue',
          lookup: (name) async => PublicationFile(id: name, name: name, mimeType: ''),
        ),
        isEmpty,
      );
    }
  });
  test('folder relink verifies cached bytes, preserves ids, and invalidates unverified editions', () async {
    final root = await Directory.systemTemp.createTemp('relink-test');
    addTearDown(() => root.delete(recursive: true));
    await Directory('${root.path}/moved').create();
    await File('${root.path}/moved/book.cbz').writeAsString('new!');
    final cache = await File('${root.path}/cached.cbz').writeAsString('old!');
    const oldFile = PublicationFile(id: 'old/book.cbz', name: 'book.cbz', mimeType: '', size: 4, revision: 'old');
    const oldEntry = SourceEntry(
      sourceId: 's',
      id: 'old/book.cbz',
      title: 'Book',
      kind: SourceItemKind.comic,
      files: [oldFile],
    );
    Future<List<LocalFileRelink>> preview(File? cached) => LocalFileRelink.preview(
      root: root.path,
      oldFolder: 'old',
      newFolder: '${root.path}/moved',
      entries: [oldEntry],
      offlineCopy: (_) async => cached,
    );
    expect(await preview(cache), isEmpty, reason: 'same filename and size do not establish the same edition');
    final unknown = (await preview(null)).single;
    expect(unknown.verified, isFalse);
    await cache.writeAsString('new!');
    final verified = (await preview(cache)).single;
    expect(verified.verified, isTrue);
    expect(LocalFileRelink.decode(jsonEncode([verified.toJson()])).single.verified, isTrue);
    Future<PublicationFile> browse(LocalFileRelink row) async {
      final adapter = LocalSourceAdapter(
        FileSource(
          id: 's',
          profileId: 'p',
          kind: FileSourceKind.local,
          name: 'Library',
          root: root.path,
          options: {
            'fileRelinks': jsonEncode([row.toJson()]),
          },
        ),
        SourceTransport(),
      );
      addTearDown(adapter.close);
      return (await adapter.browse(parent: 'moved')).entries.single.files.single;
    }

    expect((await browse(verified)).id, oldFile.id);
    expect((await browse(verified)).fingerprint, oldFile.fingerprint);
    expect((await browse(unknown)).revision, isNot('old'));
  });
  test('bounded episode chapters and skip markers stay on its own clock', () {
    final raw = PlaybackExtras(
      chapters: [
        MediaChapter(id: 1, startTimeOffset: 0, endTimeOffset: 600000, title: 'Episode 1'),
        MediaChapter(id: 2, startTimeOffset: 600000, title: 'Episode 2'),
        MediaChapter(id: 3, startTimeOffset: 1200000, title: 'Scene'),
        MediaChapter(id: 4, startTimeOffset: 2700000, title: 'Episode 3'),
      ],
      markers: [
        MediaMarker(id: 1, type: 'credits', startTimeOffset: 550000, endTimeOffset: 600000),
        MediaMarker(id: 2, type: 'intro', startTimeOffset: 600000, endTimeOffset: 650000),
        MediaMarker(id: 3, type: 'credits', startTimeOffset: 2600000, endTimeOffset: 2800000),
        MediaMarker(id: 4, type: 'intro', startTimeOffset: 2700000, endTimeOffset: 2750000),
      ],
    );
    final clipped = EpisodeFileBounds.clipExtras(raw, startMs: 600000, durationMs: 2100000);
    expect(clipped.chapters.map((c) => c.startTimeOffset), [0, 600000]);
    expect(clipped.chapters.map((c) => c.endTimeOffset), [600000, 2100000]);
    expect(clipped.markers.map((m) => m.id), [2, 3]);
    expect(clipped.markers.last.endTimeOffset, 2100000);
    expect(raw.chapters[1].startTimeOffset, 600000);
  });
  test('download sharing requires the exact selected original file', () {
    expect(sameSelectedEpisodeFile(episode(1), version(), episode(2), version()), isTrue);
    expect(sameSelectedEpisodeFile(episode(1), version(), episode(2), version(size: 2048)), isFalse);
    expect(sameSelectedEpisodeFile(episode(1), version(), episode(2), version(path: '/other/S01E01-E03.mkv')), isFalse);
    expect(sameSelectedEpisodeFile(episode(1), version(), episode(2), null), isFalse);
  });
  test('a relocated source root reconnects even when the old root no longer exists', () async {
    final directory = await Directory.systemTemp.createTemp('moved-root');
    addTearDown(() => directory.delete(recursive: true));
    final oldRoot = await Directory('${directory.path}/old').create();
    await File('${oldRoot.path}/book.cbz').writeAsString('book');
    final cached = await File('${directory.path}/cached').writeAsString('book');
    final root = await oldRoot.rename('${directory.path}/new');
    const file = PublicationFile(id: 'book.cbz', name: 'book.cbz', mimeType: '', size: 4, revision: 'original');
    final rows = await LocalFileRelink.preview(
      root: oldRoot.path,
      oldFolder: '.',
      newFolder: root.path,
      entries: [
        const SourceEntry(sourceId: 's', id: 'book.cbz', title: 'Book', kind: SourceItemKind.comic, files: [file]),
      ],
      offlineCopy: (_) async => cached,
    );
    expect(rows.single.path, 'book.cbz');
    expect(rows.single.verified, isTrue);
  });
}
