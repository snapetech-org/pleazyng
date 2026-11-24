import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:path_provider_platform_interface/path_provider_platform_interface.dart';
import 'package:plezy/services/subtitle_ai_service.dart';
import 'package:plezy/services/subtitle_retimer.dart';
import 'package:plezy/services/subtitle_source_transcript_cache.dart';
import 'package:plezy/services/subtitle_word_timing.dart';

import '../test_helpers/io_fakes.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late Directory root;
  setUp(() async {
    root = await Directory.systemTemp.createTemp('pleazyng-transcript-');
    final previous = PathProviderPlatform.instance;
    PathProviderPlatform.instance = FakePathProvider(root);
    addTearDown(() async {
      PathProviderPlatform.instance = previous;
      await root.delete(recursive: true);
    });
  });

  SubtitleSourceTranscriptCacheIdentity identity(String profile, String audio) =>
      SubtitleSourceTranscriptCacheIdentity.create(
        profileId: profile,
        audioSha256: audio * 64,
        provider: SubtitleInferenceProvider.cpu,
        languageCode: 'en',
      );

  SubtitleTranscriptionResult result({bool timed = false}) {
    final cues = [
      for (final n in [1, 2])
        SubtitleCue(id: SubtitleCue.idFor('recognizer', n), index: n, startMs: 0, endMs: 1000, text: 'cue $n'),
    ];
    return SubtitleTranscriptionResult(
      cues: cues,
      durationMs: 1000,
      requestedProvider: SubtitleInferenceProvider.cpu,
      runtimeProvider: 'cpu',
      fellBackToCpu: false,
      detectedLanguage: 'en',
      translatedToEnglish: false,
      wordTimings: timed
          ? [
              SubtitleWordTiming(
                startMs: 100,
                endMs: 200,
                text: 'hello',
                confidence: null,
                unit: SubtitleWordTimingUnit.word,
                cueId: cues.last.id,
              ),
            ]
          : [],
    );
  }

  test('explicit word references survive cue normalization and a disk round trip', () async {
    final key = identity('one', 'a');
    final stored = await SubtitleSourceTranscriptCacheService.store(
      profileId: 'one',
      identity: key,
      result: result(timed: true),
    );
    expect(stored.persisted, isTrue);
    expect(stored.wordTimings.single.cueId, stored.cues.last.id);
    await SubtitleSourceTranscriptCacheService.store(
      profileId: 'two',
      identity: identity('two', 'b'),
      result: result(),
    );
    final loaded = await SubtitleSourceTranscriptCacheService.load(profileId: 'one', identity: key);
    expect(loaded!.wordTimings.single.cueIndex, 2);
    expect(loaded.wordTimings.single.cueId, stored.cues.last.id);
  });

  test('a cache identity can only be used inside its original profile', () async {
    final key = identity('one', 'c');
    await SubtitleSourceTranscriptCacheService.store(profileId: 'one', identity: key, result: result());
    expect(await SubtitleSourceTranscriptCacheService.load(profileId: 'two', identity: key), isNull);
    expect(await SubtitleSourceTranscriptCacheService.load(profileId: null, identity: key), isNull);
    await expectLater(
      SubtitleSourceTranscriptCacheService.store(profileId: 'two', identity: key, result: result()),
      throwsArgumentError,
    );
  });

  test('cache maintenance preserves another writer and evicts old profile entries', () async {
    await SubtitleSourceTranscriptCacheService.store(
      profileId: 'one',
      identity: identity('one', '1'),
      result: result(),
    );
    final canonical = (await root.list(recursive: true).where((e) => e is File).first) as File;
    final profile = canonical.parent;
    final stage = await profile.createTemp('.transcript-write-');
    final active = await File('${stage.path}/data').writeAsString('in progress');
    final legacy = await File('${canonical.path}.pending').writeAsString('old');
    await legacy.setLastModified(DateTime.now().subtract(const Duration(days: 2)));
    for (var n = 2; n <= 10; n++) {
      await SubtitleSourceTranscriptCacheService.store(
        profileId: 'one',
        identity: identity('one', n.toRadixString(16)),
        result: result(),
      );
    }
    expect(await active.readAsString(), 'in progress');
    expect(await legacy.exists(), isFalse);
    expect(await profile.list().where((e) => e.path.endsWith('.json')).length, 8);
  });

  test('overlapping stores keep only the latest in-memory transcript', () async {
    final first = identity('one', 'd'), second = identity('two', 'e');
    await Future.wait([
      SubtitleSourceTranscriptCacheService.store(profileId: 'one', identity: first, result: result()),
      SubtitleSourceTranscriptCacheService.store(profileId: 'two', identity: second, result: result()),
    ]);
    await for (final file in root.list(recursive: true)) {
      if (file is File) await file.delete();
    }
    expect(await SubtitleSourceTranscriptCacheService.load(profileId: 'one', identity: first), isNull);
    expect(await SubtitleSourceTranscriptCacheService.load(profileId: 'two', identity: second), isNotNull);
  });
}
